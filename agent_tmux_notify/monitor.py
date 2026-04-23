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
    HookData,
    PaneState,
    TriggerEvent,
    build_trigger_event,
    build_trigger_event_from_hook,
    detect_state,
    extract_options_from_buffer,
    find_claude_panes,
)
from .hook_server import (
    HookEvent,
    HookServer,
    HookStore,
    PaneCorrelator,
    PayloadDumper,
    PendingPermission,
    PendingPermissions,
)
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
        self.pending_permissions = PendingPermissions()

        dumper = None
        if hcfg.dump_payloads:
            dumper = PayloadDumper(hcfg.dump_path)

        self.hook_server: HookServer | None = None
        if hcfg.enabled:
            self.hook_server = HookServer(
                self.hook_store, self.correlator,
                host=hcfg.host, port=hcfg.port,
                pending_permissions=self.pending_permissions,
                on_permission_request=self._on_permission_request,
                on_notification=self._on_notification,
                on_pretooluse=self._on_pretooluse,
                dumper=dumper,
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
                if self.config.buffer_detection.enabled:
                    tg.create_task(self._poll_loop())
                else:
                    log.info("Buffer detection disabled, using hook-driven mode only")
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

    # --- Buffer Polling (only when buffer_detection.enabled) ---

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
                # Hook data is optional enrichment, not a gate
                hook_event = None
                session_id = self.correlator.get_session_id(pane_id)
                if session_id:
                    hook_event = self.hook_store.pop_latest(session_id)
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

    # --- Hook-driven callbacks ---

    async def _on_pretooluse(
        self, event: HookEvent, pane_id: str | None, raw_payload: dict
    ) -> None:
        """Called by hook server when a PreToolUse arrives. Cache only, no popup."""
        log.debug(
            "PreToolUse cached: %s %s (session=%s)",
            event.tool_name or "?",
            (event.tool_input or {}).get("file_path", "")
            or (event.tool_input or {}).get("command", "")[:60]
            or "",
            event.session_id[:8],
        )

    async def _on_permission_request(
        self, request_key: str, pp: PendingPermission
    ) -> None:
        """Called by hook server when a PermissionRequest arrives."""
        pane_id = pp.pane_id

        # Skip if pane is focused
        if pane_id:
            active = await tmux.get_active_pane_id()
            if active == pane_id:
                log.debug("Skipping hook popup for %s — pane is focused", pane_id)
                self.pending_permissions.resolve(request_key, {})
                return

        # Skip if popup already active for this pane
        if pane_id and pane_id in self.active_popups:
            log.debug("Skipping hook popup for %s — popup already active", pane_id)
            self.pending_permissions.resolve(request_key, {})
            return

        # Get cached PreToolUse events for context enrichment
        pretooluse_events = self.hook_store.get_recent(
            pp.event.session_id, "PreToolUse"
        )

        # Parse tmux buffer for options (auxiliary)
        buffer_options: list[str] = []
        buffer_selected = 0
        if pane_id:
            buffer_options, buffer_selected = await extract_options_from_buffer(
                pane_id, self.triggers, self.config.buffer_lines
            )

        # Build TriggerEvent from hook data + PreToolUse context + buffer options
        event = await build_trigger_event_from_hook(
            pane_id, pp.event, pp.raw_payload,
            pretooluse_context=pretooluse_events,
            buffer_options=buffer_options,
            buffer_selected_index=buffer_selected,
        )

        # Plan scenario: support Ctrl-G edit loop
        if event.scenario == "plan":
            result = await self._show_plan_popup(pane_id or "", event)
        else:
            result = await self._show_popup_and_get_result(pane_id or "", event)

        # Focus pane if requested
        if result == "focus" and pane_id:
            await self._focus_pane(pane_id)

        # Map result to hook decision (with cross-validation)
        decision = self._map_result_to_decision(result, event, pp.raw_payload)
        self.pending_permissions.resolve(request_key, decision)

    async def _on_notification(
        self, event: HookEvent, pane_id: str | None, raw_payload: dict
    ) -> None:
        """Called by hook server when a Notification arrives."""
        notification_type = raw_payload.get("notification_type", "")

        if notification_type == "permission_prompt":
            # Handled by PermissionRequest hook, ignore
            return

        if notification_type == "idle_prompt":
            await self._show_idle_notification(pane_id, raw_payload)
            return

        # Other notification types: simple popup
        if not pane_id:
            active = await tmux.get_active_pane_id()
            if active:
                pane_id = active
            else:
                log.debug("Notification received but no pane to show it on")
                return

        active = await tmux.get_active_pane_id()
        if active == pane_id:
            return

        message = raw_payload.get("message", "Claude Code notification")
        title = raw_payload.get("title", "Notification")
        try:
            safe_msg = message.replace('"', '\\"')
            cmd = ["bash", "-c", f'echo "{safe_msg}"; sleep 3']
            pcfg = self.config.popup
            target = active if active else pane_id
            await tmux.display_popup(
                target, cmd, width="50", height="3",
                title=f" {title} ",
                x=pcfg.x, y=pcfg.y,
            )
        except Exception:
            log.debug("Notification popup failed for %s", pane_id)

    # --- Idle notification ---

    async def _show_idle_notification(
        self, pane_id: str | None, raw_payload: dict
    ) -> None:
        """Show a notification popup when Claude is waiting for input."""
        if not pane_id:
            return

        active = await tmux.get_active_pane_id()
        if active == pane_id:
            return  # Already focused, no notification needed

        if pane_id in self.active_popups:
            return

        cwd = await tmux.get_pane_cwd(pane_id)
        project_name = os.path.basename(cwd) if cwd else "unknown"
        session_name = await tmux.get_session_name(pane_id)
        message = raw_payload.get("message", "Claude is waiting for your input")

        event = TriggerEvent(
            project_name=project_name,
            session_name=session_name,
            pane_id=pane_id,
            scenario="idle",
            content=[],
            question=message,
            options=[],
            selected_index=0,
        )

        result = await self._show_popup_and_get_result(pane_id, event)
        if result == "focus" or result == "option:0":
            await self._focus_pane(pane_id)

    # --- Decision mapping with cross-validation ---

    def _map_result_to_decision(
        self, result: str | None, event: TriggerEvent, raw_payload: dict
    ) -> dict:
        """Map popup result to hook decision, cross-validating buffer options with hook data."""
        if not result:
            return {}  # cancelled — Claude Code falls back to terminal

        if result.startswith("option:"):
            idx = int(result.split(":", 1)[1])
            selected_text = event.options[idx] if idx < len(event.options) else ""

            # Cross-validate: map buffer option text to hook decision
            text_lower = selected_text.lower()
            if "deny" in text_lower:
                return {"decision": "deny"}
            if "always allow" in text_lower or "always approve" in text_lower:
                return self._build_always_allow_decision(selected_text, raw_payload)
            if "allow" in text_lower or "approve" in text_lower:
                return {"decision": "allow"}

            # Fallback: first option = allow, others = deny
            if idx == 0:
                return {"decision": "allow"}
            return {"decision": "deny"}

        if result == "focus":
            return {}  # user wants to handle in terminal

        if result.startswith("custom:"):
            text = result.split(":", 1)[1]
            return {"decision": "deny", "message": text}

        return {}

    def _build_always_allow_decision(
        self, selected_text: str, raw_payload: dict
    ) -> dict:
        """Build decision with updatedPermissions from permission_suggestions."""
        suggestions = raw_payload.get("permission_suggestions", [])
        for sug in suggestions:
            if not isinstance(sug, dict):
                continue
            rules = sug.get("rules", [])
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                content = rule.get("ruleContent", "")
                # Match the "Always Allow: xxx" text back to the suggestion
                if content and content in selected_text:
                    return {
                        "decision": "allow",
                        "updatedPermissions": [sug],
                    }
        # Fallback: plain allow if no matching suggestion found
        return {"decision": "allow"}

    # --- Popup display ---

    async def _show_popup_and_get_result(
        self, pane_id: str, event: TriggerEvent
    ) -> str | None:
        """Show popup and return the raw result string."""
        if pane_id and pane_id in self.active_popups:
            return None
        if pane_id:
            self.active_popups.add(pane_id)

        try:
            result_file = tempfile.mktemp(
                prefix="agent-tmux-notify-", suffix=".txt"
            )
            config_file = tempfile.mktemp(
                prefix="agent-tmux-notify-cfg-", suffix=".json"
            )
            try:
                config = event.to_dict()
                config["result_file"] = result_file
                with open(config_file, "w") as f:
                    json.dump(config, f, ensure_ascii=False)

                popup_script = os.path.join(
                    os.path.dirname(__file__), "popup.py"
                )
                cmd = [sys.executable, popup_script, "--config", config_file]

                title = f" Claude Code: {event.scenario} — {event.project_name} "
                pcfg = self.config.popup
                active = await tmux.get_active_pane_id()
                target = active if active else pane_id
                if not target:
                    return None
                rc = await tmux.display_popup(
                    target, cmd, title=title,
                    width=pcfg.width, height=pcfg.height,
                    x=pcfg.x, y=pcfg.y,
                )

                if rc == 0 and os.path.exists(result_file):
                    with open(result_file) as f:
                        return f.read().strip()
                return None
            finally:
                for f in (result_file, config_file):
                    if os.path.exists(f):
                        os.unlink(f)
        except Exception:
            log.exception("Popup error for %s", pane_id)
            return None
        finally:
            if pane_id:
                self.active_popups.discard(pane_id)

    async def _show_plan_popup(
        self, pane_id: str, event: TriggerEvent
    ) -> str | None:
        """Plan scenario popup with Ctrl-G edit loop. Returns final result."""
        if pane_id and pane_id in self.active_popups:
            return None
        if pane_id:
            self.active_popups.add(pane_id)

        try:
            while True:
                result_file = tempfile.mktemp(
                    prefix="agent-tmux-notify-", suffix=".txt"
                )
                config_file = tempfile.mktemp(
                    prefix="agent-tmux-notify-cfg-", suffix=".json"
                )
                try:
                    config = event.to_dict()
                    config["result_file"] = result_file
                    with open(config_file, "w") as f:
                        json.dump(config, f, ensure_ascii=False)

                    popup_script = os.path.join(
                        os.path.dirname(__file__), "popup.py"
                    )
                    cmd = [sys.executable, popup_script, "--config", config_file]

                    title = f" Claude Code: plan — {event.project_name} "
                    pcfg = self.config.popup
                    active = await tmux.get_active_pane_id()
                    target = active if active else pane_id
                    if not target:
                        return None
                    rc = await tmux.display_popup(
                        target, cmd, title=title,
                        width=pcfg.width, height=pcfg.height,
                        x=pcfg.x, y=pcfg.y,
                    )

                    if rc == 0 and os.path.exists(result_file):
                        with open(result_file) as f:
                            result = f.read().strip()
                        log.info("Plan popup result for %s: %s", pane_id, result)

                        if result.startswith("edit_plan:"):
                            plan_path = result.split(":", 1)[1]
                            await self._open_plan_editor(pane_id, plan_path)
                            # Re-read plan file and rebuild event for reshow
                            if event.plan_file_path:
                                from .detector import _read_plan_file
                                event.content = _read_plan_file(event.plan_file_path)
                            continue  # reshow popup with updated content

                        return result
                    else:
                        log.info("Plan popup cancelled for %s", pane_id)
                        return None
                finally:
                    for f in (result_file, config_file):
                        if os.path.exists(f):
                            os.unlink(f)
        except Exception:
            log.exception("Plan popup error for %s", pane_id)
            return None
        finally:
            if pane_id:
                self.active_popups.discard(pane_id)

    async def _show_popup(
        self, pane_id: str, state: DetectedState, event: TriggerEvent
    ) -> None:
        """Buffer-detection mode: show popup and handle result with tmux send-keys."""
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

                result_file = tempfile.mktemp(prefix="agent-tmux-notify-", suffix=".txt")
                config_file = tempfile.mktemp(prefix="agent-tmux-notify-cfg-", suffix=".json")
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
