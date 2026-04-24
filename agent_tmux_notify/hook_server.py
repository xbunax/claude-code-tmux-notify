"""Async HTTP hook server, event store, pane correlator, and payload dumper.

Receives Claude Code hook events via HTTP POST, routes them by event type,
and supports blocking PermissionRequest handling with popup integration.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import logging
import time
from collections import deque
from typing import Any, Callable, Coroutine

log = logging.getLogger(__name__)


@dataclasses.dataclass
class HookEvent:
    session_id: str
    hook_event_name: str
    tool_name: str | None = None
    tool_input: dict | None = None
    cwd: str | None = None
    transcript_path: str | None = None
    raw_payload: dict | None = None
    timestamp: float = dataclasses.field(default_factory=time.monotonic)


class HookStore:
    """In-memory store for recent hook events, keyed by session_id."""

    def __init__(self, ttl: float = 30.0, max_per_session: int = 20) -> None:
        self._ttl = ttl
        self._max = max_per_session
        self._store: dict[str, deque[HookEvent]] = {}

    def put(self, event: HookEvent) -> None:
        self._expire()
        q = self._store.setdefault(event.session_id, deque(maxlen=self._max))
        q.append(event)

    def get_latest(
        self, session_id: str, event_name: str | None = None
    ) -> HookEvent | None:
        self._expire()
        q = self._store.get(session_id)
        if not q:
            return None
        if event_name is None:
            return q[-1]
        for ev in reversed(q):
            if ev.hook_event_name == event_name:
                return ev
        return None

    def pop_latest(
        self, session_id: str, event_name: str | None = None
    ) -> HookEvent | None:
        self._expire()
        q = self._store.get(session_id)
        if not q:
            return None
        if event_name is None:
            return q.pop()
        # Find and remove the latest matching event
        for i in range(len(q) - 1, -1, -1):
            if q[i].hook_event_name == event_name:
                ev = q[i]
                del q[i]
                return ev
        return None

    def get_recent(
        self, session_id: str, event_name: str, n: int = 5
    ) -> list[HookEvent]:
        """Return the last *n* events of *event_name* for a session."""
        self._expire()
        q = self._store.get(session_id)
        if not q:
            return []
        result: list[HookEvent] = []
        for ev in reversed(q):
            if ev.hook_event_name == event_name:
                result.append(ev)
                if len(result) >= n:
                    break
        result.reverse()
        return result

    def _expire(self) -> None:
        now = time.monotonic()
        cutoff = now - self._ttl
        empty_keys: list[str] = []
        for sid, q in self._store.items():
            while q and q[0].timestamp < cutoff:
                q.popleft()
            if not q:
                empty_keys.append(sid)
        for k in empty_keys:
            del self._store[k]


class PaneCorrelator:
    """Maps Claude Code session_ids to tmux pane_ids via CWD matching."""

    def __init__(self) -> None:
        self._cwd_to_panes: dict[str, set[str]] = {}
        self._pane_to_cwd: dict[str, str] = {}
        self._session_to_pane: dict[str, str] = {}
        self._pane_to_session: dict[str, str] = {}

    def register_pane(self, pane_id: str, cwd: str) -> None:
        old_cwd = self._pane_to_cwd.get(pane_id)
        if old_cwd and old_cwd != cwd:
            s = self._cwd_to_panes.get(old_cwd)
            if s:
                s.discard(pane_id)
                if not s:
                    del self._cwd_to_panes[old_cwd]
        self._cwd_to_panes.setdefault(cwd, set()).add(pane_id)
        self._pane_to_cwd[pane_id] = cwd

    def unregister_pane(self, pane_id: str) -> None:
        cwd = self._pane_to_cwd.pop(pane_id, None)
        if cwd:
            s = self._cwd_to_panes.get(cwd)
            if s:
                s.discard(pane_id)
                if not s:
                    del self._cwd_to_panes[cwd]
        sid = self._pane_to_session.pop(pane_id, None)
        if sid:
            self._session_to_pane.pop(sid, None)

    def _pick_unbound(self, panes: set[str]) -> str | None:
        """From a set of candidate panes, pick the single unbound one."""
        if len(panes) == 1:
            return next(iter(panes))
        unbound = [p for p in panes if p not in self._pane_to_session]
        if len(unbound) == 1:
            return unbound[0]
        return None

    def correlate(self, event: HookEvent) -> str | None:
        """Try to match a hook event to a pane_id. Caches on success."""
        # Fast path: already bound
        if event.session_id in self._session_to_pane:
            return self._session_to_pane[event.session_id]
        if not event.cwd:
            return None
        # Exact CWD match
        panes = self._cwd_to_panes.get(event.cwd)
        if panes:
            pane_id = self._pick_unbound(panes)
            if pane_id:
                self._bind(event.session_id, pane_id)
                return pane_id
        # Subdirectory fallback
        for registered_cwd, candidate_panes in self._cwd_to_panes.items():
            if event.cwd.startswith(registered_cwd + "/") or registered_cwd.startswith(event.cwd + "/"):
                pane_id = self._pick_unbound(candidate_panes)
                if pane_id:
                    self._bind(event.session_id, pane_id)
                    return pane_id
        return None

    def get_session_id(self, pane_id: str) -> str | None:
        return self._pane_to_session.get(pane_id)

    def _bind(self, session_id: str, pane_id: str) -> None:
        self._session_to_pane[session_id] = pane_id
        self._pane_to_session[pane_id] = session_id
        log.debug("Bound session %s <-> pane %s", session_id, pane_id)


# ---------------------------------------------------------------------------
# Payload dumper
# ---------------------------------------------------------------------------

class PayloadDumper:
    """Append raw hook payloads to a JSONL file for debugging."""

    def __init__(self, path: str) -> None:
        self._path = path

    def dump(self, data: dict) -> None:
        entry = {
            "_ts": datetime.datetime.now().isoformat(),
            "_event": data.get("hook_event_name", ""),
            **data,
        }
        try:
            with open(self._path, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            log.warning("Failed to dump payload to %s", self._path)


# ---------------------------------------------------------------------------
# Pending permission requests (blocking PermissionRequest support)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class PendingPermission:
    event: HookEvent
    pane_id: str | None
    future: asyncio.Future
    raw_payload: dict


class PendingPermissions:
    """Registry for in-flight PermissionRequest events awaiting user decision."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingPermission] = {}

    def register(self, key: str, pp: PendingPermission) -> None:
        self._pending[key] = pp

    def resolve(self, key: str, decision: dict) -> bool:
        pp = self._pending.pop(key, None)
        if pp and not pp.future.done():
            pp.future.set_result(decision)
            return True
        return False

    def get(self, key: str) -> PendingPermission | None:
        return self._pending.get(key)


# ---------------------------------------------------------------------------
# Minimal async HTTP server (stdlib only)
# ---------------------------------------------------------------------------

_HTTP_200 = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 2\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b"{}"
)

_HTTP_400 = (
    b"HTTP/1.1 400 Bad Request\r\n"
    b"Content-Length: 0\r\n"
    b"Connection: close\r\n"
    b"\r\n"
)

_HTTP_405 = (
    b"HTTP/1.1 405 Method Not Allowed\r\n"
    b"Content-Length: 0\r\n"
    b"Connection: close\r\n"
    b"\r\n"
)


def _build_json_response(data: dict) -> bytes:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    header = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    return header + body


# Callback type for permission request, notification, and pretooluse
PermissionCallback = Callable[[str, "PendingPermission"], Coroutine[Any, Any, None]]
NotificationCallback = Callable[[HookEvent, str | None, dict], Coroutine[Any, Any, None]]
PreToolUseCallback = Callable[[HookEvent, str | None, dict], Coroutine[Any, Any, None]]


class HookServer:
    """Lightweight async HTTP server that receives Claude Code hook events.

    Routes events by hook_event_name:
    - PermissionRequest: blocks until user decides (via popup), returns decision
    - Notification: returns 200 immediately, fires async callback
    - Others: returns 200 immediately, stores for correlation
    """

    def __init__(
        self,
        store: HookStore,
        correlator: PaneCorrelator,
        host: str = "127.0.0.1",
        port: int = 19836,
        pending_permissions: PendingPermissions | None = None,
        on_permission_request: PermissionCallback | None = None,
        on_notification: NotificationCallback | None = None,
        on_pretooluse: PreToolUseCallback | None = None,
        dumper: PayloadDumper | None = None,
    ) -> None:
        self.store = store
        self.correlator = correlator
        self.host = host
        self.port = port
        self.pending_permissions = pending_permissions or PendingPermissions()
        self._on_permission_request = on_permission_request
        self._on_notification = on_notification
        self._on_pretooluse = on_pretooluse
        self._dumper = dumper
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        try:
            self._server = await asyncio.start_server(
                self._handle, self.host, self.port
            )
            log.info("Hook server listening on %s:%d", self.host, self.port)
        except OSError as e:
            log.warning("Hook server failed to bind %s:%d: %s — running without hooks", self.host, self.port, e)
            self._server = None

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            log.info("Hook server stopped")

    @property
    def running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            # Read request line + headers
            header_data = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=5.0
            )
            header_text = header_data.decode("utf-8", errors="replace")
            lines = header_text.split("\r\n")
            request_line = lines[0] if lines else ""

            # Only accept POST
            if not request_line.startswith("POST "):
                writer.write(_HTTP_405)
                await writer.drain()
                return

            # Parse Content-Length
            content_length = 0
            for line in lines[1:]:
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())
                    break

            # Read body
            body = b""
            if content_length > 0:
                body = await asyncio.wait_for(
                    reader.readexactly(content_length), timeout=5.0
                )

            # Parse JSON
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                writer.write(_HTTP_400)
                await writer.drain()
                return

            # Dump raw payload if enabled
            if self._dumper:
                self._dumper.dump(data)

            # Validate session_id
            session_id = data.get("session_id", "")
            if not session_id:
                writer.write(_HTTP_400)
                await writer.drain()
                return

            # Build HookEvent
            event = HookEvent(
                session_id=session_id,
                hook_event_name=data.get("hook_event_name", ""),
                tool_name=data.get("tool_name"),
                tool_input=data.get("tool_input"),
                cwd=data.get("cwd"),
                transcript_path=data.get("transcript_path"),
                raw_payload=data,
            )

            # Store and correlate
            self.store.put(event)
            pane_id = self.correlator.correlate(event)

            hook_name = event.hook_event_name
            log.debug(
                "Hook received: %s %s (session=%s, pane=%s)",
                hook_name,
                event.tool_name or "",
                event.session_id[:8],
                pane_id or "?",
            )

            # --- Route by event type ---

            if hook_name == "PermissionRequest" and self._on_permission_request:
                await self._handle_permission_request(
                    writer, event, pane_id, data
                )
                return

            if hook_name == "Notification" and self._on_notification:
                # Return 200 immediately, fire callback async
                writer.write(_HTTP_200)
                await writer.drain()
                asyncio.create_task(
                    self._on_notification(event, pane_id, data)
                )
                return

            if hook_name == "PreToolUse" and self._on_pretooluse:
                # Return 200 immediately, fire callback async
                writer.write(_HTTP_200)
                await writer.drain()
                asyncio.create_task(
                    self._on_pretooluse(event, pane_id, data)
                )
                return

            # Default: return 200 immediately (Stop, etc.)
            writer.write(_HTTP_200)
            await writer.drain()

        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:
            log.exception("Hook server handler error")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_permission_request(
        self,
        writer: asyncio.StreamWriter,
        event: HookEvent,
        pane_id: str | None,
        raw_payload: dict,
    ) -> None:
        """Handle PermissionRequest: block until user decides, return decision."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict] = loop.create_future()
        request_key = f"{event.session_id}:{time.monotonic()}"

        pp = PendingPermission(
            event=event,
            pane_id=pane_id,
            future=future,
            raw_payload=raw_payload,
        )
        self.pending_permissions.register(request_key, pp)

        # Fire the callback (shows popup, resolves future when user decides)
        # Wrap in error-safe handler so exceptions don't leave the future unresolved
        async def _safe_permission_callback() -> None:
            try:
                await self._on_permission_request(request_key, pp)
            except Exception:
                log.exception(
                    "Permission callback failed for session %s",
                    event.session_id[:8],
                )
                if not future.done():
                    future.set_result({})

        asyncio.create_task(_safe_permission_callback())

        # Block HTTP connection until user decides or timeout
        try:
            decision = await asyncio.wait_for(future, timeout=300.0)
        except asyncio.TimeoutError:
            log.warning("PermissionRequest timed out for session %s", event.session_id[:8])
            decision = {}
            self.pending_permissions.resolve(request_key, decision)

        # Return decision as HTTP response
        log.info(
            "Returning decision for session %s: %s",
            event.session_id[:8], decision,
        )
        if decision:
            response_body = {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": decision,
                }
            }
            response = _build_json_response(response_body)
        else:
            response = _HTTP_200  # empty = no decision, Claude Code falls back to terminal
        try:
            writer.write(response)
            await writer.drain()
        except (ConnectionError, OSError) as e:
            log.warning(
                "Failed to send decision for session %s: %s",
                event.session_id[:8], e,
            )
