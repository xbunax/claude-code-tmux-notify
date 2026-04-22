"""Async monitoring loop: discover Claude Code panes, poll state, trigger popups."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import time

from . import tmux
from .config import Config, load_config
from .detector import (
    ClaudePane,
    CompiledTriggers,
    DetectedState,
    PaneState,
    TriggerEvent,
    build_trigger_event,
    detect_state,
    find_claude_panes,
)
from .hook_server import HookServer, HookStore, PaneCorrelator
from .responder import send_response

log = logging.getLogger(__name__)


class Monitor:
    def __init__(
        self,
        poll_interval: float = 1.5,
        discovery_interval: float = 30.0,
        debounce_seconds: float = 3.0,
        config_path: str | None = None,
    ) -> None:
        self.poll_interval = poll_interval
        self.discovery_interval = discovery_interval
        self.debounce_seconds = debounce_seconds
        self.config = load_config(config_path)
        self.triggers = CompiledTriggers(self.config.triggers)

        # Hook server components
        hcfg = self.config.hook_server
        self.hook_store = HookStore(ttl=hcfg.ttl)
        self.correlator = PaneCorrelator()
        self.hook_server: HookServer | None = None
        if hcfg.enabled:
            self.hook_server = HookServer(
                self.hook_store, self.correlator,
                host=hcfg.host, port=hcfg.port,
            )

        self.panes: dict[str, ClaudePane] = {}  # pane_id -> ClaudePane
        self.prev_states: dict[str, DetectedState] = {}
        self.active_popups: set[str] = set()
        # Debounce: pane_id -> first time NEEDS_INPUT was seen
        self._input_first_seen: dict[str, float] = {}
        # Track already-notified prompts: pane_id -> prompt_question
        # Prevents re-showing popup after user dismisses with ESC
        self._notified_prompts: dict[str, str] = {}

    async def run(self) -> None:
        log.info("Claude tmux monitor starting…")
        await self._bind_startup_panes()

        try:
            async with asyncio.TaskGroup() as tg:
                if self.hook_server:
                    tg.create_task(self._start_hook_server())
                tg.create_task(self._discover_loop())
                tg.create_task(self._poll_loop())
        except asyncio.CancelledError:
            if self.hook_server:
                await self.hook_server.stop()
            log.info("Monitor shutting down.")

    async def _start_hook_server(self) -> None:
        """Start the hook HTTP server. Non-fatal on failure."""
        assert self.hook_server is not None
        await self.hook_server.start()
        if self.hook_server.running:
            # Keep the task alive while the server runs
            server = self.hook_server._server
            if server:
                await server.serve_forever()

    # --- Startup ---

    async def _bind_startup_panes(self) -> None:
        """Phase 1: discover and bind Claude Code PIDs to tmux panes at startup."""
        try:
            await self._discover()
        except RuntimeError as e:
            log.error("tmux not available: %s", e)
            return

        if self.panes:
            log.info(
                "Bound %d Claude Code pane(s) at startup: %s",
                len(self.panes),
                ", ".join(
                    f"{pid} (pid {cp.claude_pid})" for pid, cp in self.panes.items()
                ),
            )
        else:
            log.info("No Claude Code panes found at startup. Will keep scanning.")

    # --- Discovery ---

    async def _discover(self) -> None:
        try:
            claude_panes = await find_claude_panes()
        except RuntimeError as e:
            log.error("tmux not available: %s", e)
            return

        current_ids = {cp.pane.pane_id for cp in claude_panes}
        for cp in claude_panes:
            if cp.pane.pane_id not in self.panes:
                log.info("Discovered Claude Code in %s (pid %d)", cp.pane.pane_id, cp.claude_pid)
                self.panes[cp.pane.pane_id] = cp
            # Register/update pane CWD for hook correlation
            try:
                cwd = await tmux.get_pane_cwd(cp.pane.pane_id)
                if cwd:
                    self.correlator.register_pane(cp.pane.pane_id, cwd)
            except RuntimeError:
                pass
        for pid in list(self.panes):
            if pid not in current_ids:
                log.info("Claude Code gone from %s", pid)
                self.panes.pop(pid, None)
                self.prev_states.pop(pid, None)
                self._input_first_seen.pop(pid, None)
                self._notified_prompts.pop(pid, None)
                self.correlator.unregister_pane(pid)

    async def _discover_loop(self) -> None:
        while True:
            await asyncio.sleep(self.discovery_interval)
            await self._discover()

    # --- Polling ---

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            for pane_id in list(self.panes):
                try:
                    await self._poll_pane(pane_id)
                except Exception:
                    log.exception("Error polling %s", pane_id)

    async def _poll_pane(self, pane_id: str) -> None:
        if pane_id in self.active_popups:
            return

        try:
            state = await detect_state(pane_id, self.triggers, self.config.buffer_lines)
        except RuntimeError:
            self.panes.pop(pane_id, None)
            self.prev_states.pop(pane_id, None)
            self._input_first_seen.pop(pane_id, None)
            self._notified_prompts.pop(pane_id, None)
            return

        prev = self.prev_states.get(pane_id)
        self.prev_states[pane_id] = state

        # --- NEEDS_INPUT with debounce ---
        if state.state == PaneState.NEEDS_INPUT:
            prompt_key = state.prompt_question or ""
            # Already notified for this exact prompt — don't re-show
            if self._notified_prompts.get(pane_id) == prompt_key:
                return
            now = time.monotonic()
            if pane_id not in self._input_first_seen:
                self._input_first_seen[pane_id] = now
                log.debug("NEEDS_INPUT first seen on %s", pane_id)
                return
            elapsed = now - self._input_first_seen[pane_id]
            if elapsed < self.debounce_seconds:
                return
            del self._input_first_seen[pane_id]
            if pane_id not in self.active_popups:
                active = await tmux.get_active_pane_id()
                if active == pane_id:
                    log.debug("Skipping popup for %s — pane is focused", pane_id)
                    return
                self._notified_prompts[pane_id] = prompt_key
                # Look up hook data for this pane
                hook_event = None
                session_id = self.correlator.get_session_id(pane_id)
                if session_id:
                    hook_event = self.hook_store.pop_latest(session_id)
                # Dual-confirmation: always require both buffer + hook
                if hook_event is None:
                    log.debug("Skipping popup for %s — no hook event (dual-confirm)", pane_id)
                    return
                event = await build_trigger_event(pane_id, state, hook_event)
                asyncio.create_task(self._show_popup(pane_id, state, event))
        else:
            self._input_first_seen.pop(pane_id, None)
            self._notified_prompts.pop(pane_id, None)

        # --- COMPLETED notification ---
        if (
            state.state == PaneState.COMPLETED
            and prev is not None
            and prev.state == PaneState.WORKING
        ):
            active = await tmux.get_active_pane_id()
            if active != pane_id:
                asyncio.create_task(self._notify_completed(pane_id))

    # --- Popup ---

    async def _show_popup(
        self, pane_id: str, state: DetectedState, event: TriggerEvent
    ) -> None:
        if pane_id in self.active_popups:
            return
        self.active_popups.add(pane_id)

        try:
            while True:
                log.info(
                    "Showing popup for %s: %s (%d options)",
                    pane_id,
                    event.scenario,
                    len(event.options),
                )

                result_file = tempfile.mktemp(prefix="claude-code-tmux-notify-", suffix=".txt")
                config_file = tempfile.mktemp(prefix="claude-code-tmux-notify-cfg-", suffix=".json")
                try:
                    config = event.to_dict()
                    config["result_file"] = result_file
                    with open(config_file, "w") as f:
                        json.dump(config, f, ensure_ascii=False)

                    popup_script = os.path.join(os.path.dirname(__file__), "popup.py")
                    cmd = [sys.executable, popup_script, "--config", config_file]

                    title = f" Claude Code: {event.scenario} — {event.project_name} "
                    pcfg = self.config.popup
                    active = await tmux.get_active_pane_id()
                    target = active if active else pane_id
                    rc = await tmux.display_popup(
                        target, cmd, title=title,
                        width=pcfg.width, height=pcfg.height,
                        x=pcfg.x, y=pcfg.y,
                    )

                    if rc == 0 and os.path.exists(result_file):
                        with open(result_file) as f:
                            result = f.read().strip()
                        log.info("Popup result for %s: %s", pane_id, result)
                        action = await self._handle_popup_result(pane_id, state, result)
                        if action == "reshow":
                            # Re-detect state to get fresh plan content
                            try:
                                state = await detect_state(
                                    pane_id, self.triggers, self.config.buffer_lines
                                )
                                if state.state != PaneState.NEEDS_INPUT:
                                    log.info("Pane %s no longer needs input after edit", pane_id)
                                    break
                                hook_event = None
                                session_id = self.correlator.get_session_id(pane_id)
                                if session_id:
                                    hook_event = self.hook_store.get_latest(session_id)
                                event = await build_trigger_event(pane_id, state, hook_event)
                            except RuntimeError:
                                break
                            continue
                    else:
                        log.info("Popup cancelled for %s", pane_id)
                    break
                finally:
                    for f in (result_file, config_file):
                        if os.path.exists(f):
                            os.unlink(f)
        except Exception:
            log.exception("Popup error for %s", pane_id)
        finally:
            self.active_popups.discard(pane_id)

    async def _handle_popup_result(
        self, pane_id: str, state: DetectedState, result: str
    ) -> str | None:
        """Handle popup result. Returns 'reshow' if the popup should be re-displayed."""
        if result.startswith("option:"):
            idx = int(result.split(":", 1)[1])
            await send_response(
                pane_id,
                choice_index=idx,
                current_selected=state.selected_index,
                total_options=len(state.options),
                triggers=self.triggers,
                buffer_lines=self.config.buffer_lines,
            )
        elif result.startswith("custom:"):
            text = result.split(":", 1)[1]
            await send_response(
                pane_id,
                choice_index=-1,
                current_selected=state.selected_index,
                total_options=len(state.options),
                custom_text=text,
                triggers=self.triggers,
                buffer_lines=self.config.buffer_lines,
            )
        elif result == "focus":
            await self._focus_pane(pane_id)
        elif result.startswith("edit_plan:"):
            plan_path = result.split(":", 1)[1]
            await self._open_plan_editor(pane_id, plan_path)
            return "reshow"
        return None

    async def _open_plan_editor(self, pane_id: str, plan_path: str) -> None:
        """Open nvim in a centered tmux popup to edit the plan file."""
        active = await tmux.get_active_pane_id()
        target = active if active else pane_id
        await tmux.display_popup(
            target, ["nvim", plan_path],
            width="80%", height="80%",
            title=" Edit Plan ",
        )

    async def _focus_pane(self, pane_id: str) -> None:
        """Switch tmux focus to the given pane."""
        try:
            await tmux.select_pane(pane_id)
            log.info("Focused pane %s", pane_id)
        except RuntimeError:
            log.warning("Failed to focus pane %s", pane_id)

    # --- Completion notification ---

    async def _notify_completed(self, pane_id: str) -> None:
        log.info("Task completed in %s", pane_id)
        try:
            cmd = ["bash", "-c", 'echo "✓ Claude Code task completed"; sleep 2']
            pcfg = self.config.popup
            active = await tmux.get_active_pane_id()
            target = active if active else pane_id
            await tmux.display_popup(
                target, cmd, width="40", height="3", title=" Done ",
                x=pcfg.x, y=pcfg.y,
            )
        except Exception:
            log.debug("Completion notification failed for %s", pane_id)
