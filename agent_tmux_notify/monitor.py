"""Async monitoring loop: discover panes and handle hook-driven popups."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile

from . import tmux
from .config import Config, load_config
from .detector import (
    ClaudePane,
    CompiledParseRules,
    TriggerEvent,
    build_trigger_event_from_hook,
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

log = logging.getLogger(__name__)


class Monitor:
    def __init__(
        self,
        discovery_interval: float = 30.0,
        config_path: str | None = None,
    ) -> None:
        self.discovery_interval = discovery_interval
        self.config = load_config(config_path)
        self.parse_rules = CompiledParseRules(self.config.parse_rules)

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
                self.hook_store,
                self.correlator,
                host=hcfg.host,
                port=hcfg.port,
                pending_permissions=self.pending_permissions,
                on_permission_request=self._on_permission_request,
                on_notification=self._on_notification,
                on_pretooluse=self._on_pretooluse,
                dumper=dumper,
            )

        self.panes: dict[str, ClaudePane] = {}
        self.active_popups: set[str] = set()
        # Serialize popup display per pane so each tool gets its own popup.
        self._pane_locks: dict[str, asyncio.Lock] = {}

    async def _is_claude_pane_focused(self, pane_id: str | None = None) -> bool:
        """Check if a Claude Code pane is currently focused."""
        active = await tmux.get_active_pane_id()
        if not active:
            return False
        if pane_id:
            return active == pane_id
        return active in self.panes

    async def _is_focus_suppressed(self, pane_id: str | None, event_name: str) -> bool:
        """Return True when a focused pane should suppress UI for this event."""
        if not pane_id:
            return False
        if await tmux.is_pane_focused(pane_id):
            log.info("SKIP(focused): event=%s pane=%s", event_name, pane_id)
            return True
        return False

    async def run(self) -> None:
        log.info("Claude tmux monitor starting (hook-driven mode)…")
        await self._bind_startup_panes()

        try:
            async with asyncio.TaskGroup() as tg:
                if self.hook_server:
                    tg.create_task(self._start_hook_server())
                else:
                    log.warning("Hook server disabled; no popups will be triggered")
                tg.create_task(self._discover_loop())
        except asyncio.CancelledError:
            if self.hook_server:
                await self.hook_server.stop()
            log.info("Monitor shutting down.")

    async def _start_hook_server(self) -> None:
        """Start the hook HTTP server. Non-fatal on failure."""
        assert self.hook_server is not None
        await self.hook_server.start()
        if self.hook_server.running:
            server = self.hook_server._server
            if server:
                await server.serve_forever()

    async def _bind_startup_panes(self) -> None:
        """Discover and bind Claude Code PIDs to tmux panes at startup."""
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

    async def _discover(self) -> None:
        try:
            claude_panes = await find_claude_panes()
        except RuntimeError as e:
            log.error("tmux not available: %s", e)
            return

        current_ids = {cp.pane.global_pane_id for cp in claude_panes}
        for cp in claude_panes:
            gid = cp.pane.global_pane_id
            if gid not in self.panes:
                log.info("Discovered Claude Code in %s (pid %d)", gid, cp.claude_pid)
                self.panes[gid] = cp
            try:
                cwd = await tmux.get_pane_cwd(gid)
                if cwd:
                    self.correlator.register_pane(gid, cwd)
            except RuntimeError:
                pass

        for pane_id in list(self.panes):
            if pane_id not in current_ids:
                log.info("Claude Code gone from %s", pane_id)
                self.panes.pop(pane_id, None)
                self.correlator.unregister_pane(pane_id)

    async def _discover_loop(self) -> None:
        while True:
            await asyncio.sleep(self.discovery_interval)
            await self._discover()

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
        sid = pp.event.session_id[:8]

        log.info(
            "PermissionRequest callback: session=%s, pane=%s, tool=%s, cwd=%s",
            sid,
            pane_id or "None",
            pp.event.tool_name or "?",
            pp.event.cwd or "?",
        )

        if not pane_id and pp.event.cwd:
            log.info("Correlation failed for session %s, re-discovering…", sid)
            await self._discover()
            pane_id = self.correlator.correlate(pp.event)
            if pane_id:
                log.info("Re-correlation succeeded: session %s -> pane %s", sid, pane_id)
            else:
                log.info("Re-correlation still failed for session %s", sid)

        if await self._is_focus_suppressed(pane_id, "PermissionRequest"):
            log.info("PermissionRequest suppressed for focused pane: session=%s", sid)
            self.pending_permissions.resolve(request_key, {})
            return

        lock = self._pane_locks.setdefault(pane_id, asyncio.Lock()) if pane_id else None
        if lock:
            await lock.acquire()
            log.info(
                "Acquired pane lock for session=%s, pane=%s, tool=%s",
                sid,
                pane_id,
                pp.event.tool_name or "?",
            )
            if await self._is_focus_suppressed(pane_id, "PermissionRequest"):
                log.info("PermissionRequest suppressed after lock: session=%s", sid)
                self.pending_permissions.resolve(request_key, {})
                lock.release()
                return

        try:
            pretooluse_events = self.hook_store.get_recent(
                pp.event.session_id, "PreToolUse"
            )

            buffer_options: list[str] = []
            buffer_selected = 0
            if pane_id:
                buffer_options, buffer_selected = await extract_options_from_buffer(
                    pane_id, self.parse_rules, self.config.buffer_lines
                )

            event = await build_trigger_event_from_hook(
                pane_id,
                pp.event,
                pp.raw_payload,
                pretooluse_context=pretooluse_events,
                buffer_options=buffer_options,
                buffer_selected_index=buffer_selected,
            )

            if event.scenario == "plan":
                result = await self._show_plan_popup(pane_id or "", event)
            else:
                result = await self._show_popup_and_get_result(pane_id or "", event)

            if result == "focus" and pane_id:
                await self._focus_pane(pane_id)

            decision = self._map_result_to_decision(result, event, pp.raw_payload)
            log.info(
                "Permission decision for session %s: result=%r -> decision=%s",
                pp.event.session_id[:8],
                result,
                decision,
            )
            self.pending_permissions.resolve(request_key, decision)
        finally:
            if lock:
                lock.release()

    async def _on_notification(
        self, event: HookEvent, pane_id: str | None, raw_payload: dict
    ) -> None:
        """Called by hook server when a Notification arrives."""
        notification_type = raw_payload.get("notification_type", "")

        if notification_type == "permission_prompt":
            return

        if notification_type == "idle_prompt":
            await self._show_idle_notification(pane_id, raw_payload)
            return

        active = await tmux.get_active_pane_id()
        if not pane_id:
            if active:
                pane_id = active
            else:
                log.debug("Notification received but no pane to show it on")
                return

        if await self._is_focus_suppressed(pane_id, "Notification"):
            return

        message = raw_payload.get("message", "Claude Code notification")
        title = raw_payload.get("title", "Notification")
        try:
            safe_msg = message.replace('"', '\\"')
            cmd = ["bash", "-c", f'echo "{safe_msg}"; sleep 3']
            pcfg = self.config.popup
            target = active if active else pane_id
            await tmux.display_popup(
                target,
                cmd,
                width="50",
                height="3",
                title=f" {title} ",
                x=pcfg.x,
                y=pcfg.y,
            )
        except Exception:
            log.debug("Notification popup failed for %s", pane_id)

    async def _show_idle_notification(
        self, pane_id: str | None, raw_payload: dict
    ) -> None:
        """Show a notification popup when Claude is waiting for input."""
        if not pane_id:
            if await self._is_claude_pane_focused():
                return
            return

        if await self._is_focus_suppressed(pane_id, "Notification(idle_prompt)"):
            return

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

    def _map_result_to_decision(
        self, result: str | None, event: TriggerEvent, raw_payload: dict
    ) -> dict:
        """Map popup result to hook decision with option text cross-validation."""
        if not result:
            return {}

        if result.startswith("option:"):
            idx = int(result.split(":", 1)[1])
            selected_text = event.options[idx] if idx < len(event.options) else ""

            text_lower = selected_text.lower()
            if "deny" in text_lower:
                return {"behavior": "deny"}
            if "always allow" in text_lower or "always approve" in text_lower:
                return self._build_always_allow_decision(selected_text, raw_payload)
            if "allow" in text_lower or "approve" in text_lower:
                return {"behavior": "allow"}

            if idx == 0:
                return {"behavior": "allow"}
            return {"behavior": "deny"}

        if result == "focus":
            return {}

        if result.startswith("custom:"):
            text = result.split(":", 1)[1]
            return {"behavior": "deny", "message": text}

        return {}

    def _build_always_allow_decision(self, selected_text: str, raw_payload: dict) -> dict:
        """Build decision with updatedPermissions from permission_suggestions."""
        suggestions = raw_payload.get("permission_suggestions", [])
        for suggestion in suggestions:
            if not isinstance(suggestion, dict):
                continue
            rules = suggestion.get("rules", [])
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                content = rule.get("ruleContent", "")
                if content and content in selected_text:
                    return {
                        "behavior": "allow",
                        "updatedPermissions": [suggestion],
                    }

        return {"behavior": "allow"}

    async def _show_popup_and_get_result(
        self, pane_id: str, event: TriggerEvent
    ) -> str | None:
        """Show popup and return the raw result string."""
        if pane_id and pane_id in self.active_popups:
            log.info("_show_popup: SKIP pane=%s already in active_popups", pane_id)
            return None
        if pane_id:
            self.active_popups.add(pane_id)

        try:
            result_file = tempfile.mktemp(prefix="agent-tmux-notify-", suffix=".txt")
            config_file = tempfile.mktemp(
                prefix="agent-tmux-notify-cfg-", suffix=".json"
            )
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
                if not target:
                    log.info(
                        "_show_popup: no target pane (active=%s, pane_id=%r)",
                        active,
                        pane_id,
                    )
                    return None
                rc = await tmux.display_popup(
                    target,
                    cmd,
                    title=title,
                    width=pcfg.width,
                    height=pcfg.height,
                    x=pcfg.x,
                    y=pcfg.y,
                )

                if rc == 0 and os.path.exists(result_file):
                    with open(result_file) as f:
                        return f.read().strip()
                log.info(
                    "_show_popup: popup returned rc=%d, result_file_exists=%s",
                    rc,
                    os.path.exists(result_file),
                )
                return None
            finally:
                for file_path in (result_file, config_file):
                    if os.path.exists(file_path):
                        os.unlink(file_path)
        except Exception:
            log.exception("Popup error for %s", pane_id)
            return None
        finally:
            if pane_id:
                self.active_popups.discard(pane_id)

    async def _show_plan_popup(self, pane_id: str, event: TriggerEvent) -> str | None:
        """Plan scenario popup with Ctrl-G edit loop. Returns final result."""
        if pane_id and pane_id in self.active_popups:
            return None
        if pane_id:
            self.active_popups.add(pane_id)

        try:
            while True:
                result_file = tempfile.mktemp(prefix="agent-tmux-notify-", suffix=".txt")
                config_file = tempfile.mktemp(
                    prefix="agent-tmux-notify-cfg-", suffix=".json"
                )
                try:
                    config = event.to_dict()
                    config["result_file"] = result_file
                    with open(config_file, "w") as f:
                        json.dump(config, f, ensure_ascii=False)

                    popup_script = os.path.join(os.path.dirname(__file__), "popup.py")
                    cmd = [sys.executable, popup_script, "--config", config_file]

                    title = f" Claude Code: plan — {event.project_name} "
                    pcfg = self.config.popup
                    active = await tmux.get_active_pane_id()
                    target = active if active else pane_id
                    if not target:
                        return None
                    rc = await tmux.display_popup(
                        target,
                        cmd,
                        title=title,
                        width=pcfg.width,
                        height=pcfg.height,
                        x=pcfg.x,
                        y=pcfg.y,
                    )

                    if rc == 0 and os.path.exists(result_file):
                        with open(result_file) as f:
                            result = f.read().strip()

                        if result.startswith("edit_plan:"):
                            plan_path = result.split(":", 1)[1]
                            await self._open_plan_editor(pane_id, plan_path)
                            if event.plan_file_path:
                                from .detector import _read_plan_file

                                event.content = _read_plan_file(event.plan_file_path)
                            continue

                        return result

                    return None
                finally:
                    for file_path in (result_file, config_file):
                        if os.path.exists(file_path):
                            os.unlink(file_path)
        except Exception:
            log.exception("Plan popup error for %s", pane_id)
            return None
        finally:
            if pane_id:
                self.active_popups.discard(pane_id)

    async def _open_plan_editor(self, pane_id: str, plan_path: str) -> None:
        """Open nvim in a centered tmux popup to edit the plan file."""
        active = await tmux.get_active_pane_id()
        target = active if active else pane_id
        await tmux.display_popup(
            target,
            ["nvim", plan_path],
            width="80%",
            height="80%",
            title=" Edit Plan ",
        )

    async def _focus_pane(self, pane_id: str) -> None:
        """Switch tmux focus to the given pane."""
        try:
            await tmux.select_pane(pane_id)
            log.info("Focused pane %s", pane_id)
        except RuntimeError:
            log.warning("Failed to focus pane %s", pane_id)
