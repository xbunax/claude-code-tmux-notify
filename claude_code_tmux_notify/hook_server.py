"""Async HTTP hook server, event store, and pane correlator.

Receives Claude Code hook events via HTTP POST, stores them in memory,
and correlates them with tmux panes by CWD matching.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from collections import deque

log = logging.getLogger(__name__)


@dataclasses.dataclass
class HookEvent:
    session_id: str
    hook_event_name: str
    tool_name: str | None = None
    tool_input: dict | None = None
    cwd: str | None = None
    transcript_path: str | None = None
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
        self._cwd_to_pane: dict[str, str] = {}
        self._pane_to_cwd: dict[str, str] = {}
        self._session_to_pane: dict[str, str] = {}
        self._pane_to_session: dict[str, str] = {}

    def register_pane(self, pane_id: str, cwd: str) -> None:
        self._cwd_to_pane[cwd] = pane_id
        self._pane_to_cwd[pane_id] = cwd

    def unregister_pane(self, pane_id: str) -> None:
        cwd = self._pane_to_cwd.pop(pane_id, None)
        if cwd:
            self._cwd_to_pane.pop(cwd, None)
        sid = self._pane_to_session.pop(pane_id, None)
        if sid:
            self._session_to_pane.pop(sid, None)

    def correlate(self, event: HookEvent) -> str | None:
        """Try to match a hook event to a pane_id. Caches on success."""
        # Fast path: already bound
        if event.session_id in self._session_to_pane:
            return self._session_to_pane[event.session_id]
        if not event.cwd:
            return None
        # Exact CWD match
        pane_id = self._cwd_to_pane.get(event.cwd)
        if pane_id:
            self._bind(event.session_id, pane_id)
            return pane_id
        # Subdirectory fallback
        for registered_cwd, pid in self._cwd_to_pane.items():
            if event.cwd.startswith(registered_cwd + "/") or registered_cwd.startswith(event.cwd + "/"):
                self._bind(event.session_id, pid)
                return pid
        return None

    def get_session_id(self, pane_id: str) -> str | None:
        return self._pane_to_session.get(pane_id)

    def _bind(self, session_id: str, pane_id: str) -> None:
        self._session_to_pane[session_id] = pane_id
        self._pane_to_session[pane_id] = session_id
        log.debug("Bound session %s <-> pane %s", session_id, pane_id)


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


class HookServer:
    """Lightweight async HTTP server that receives Claude Code hook events."""

    def __init__(
        self,
        store: HookStore,
        correlator: PaneCorrelator,
        host: str = "127.0.0.1",
        port: int = 19836,
    ) -> None:
        self.store = store
        self.correlator = correlator
        self.host = host
        self.port = port
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

            # Build HookEvent
            session_id = data.get("session_id", "")
            if not session_id:
                writer.write(_HTTP_400)
                await writer.drain()
                return

            event = HookEvent(
                session_id=session_id,
                hook_event_name=data.get("hook_event_name", ""),
                tool_name=data.get("tool_name"),
                tool_input=data.get("tool_input"),
                cwd=data.get("cwd"),
                transcript_path=data.get("transcript_path"),
            )

            self.store.put(event)
            self.correlator.correlate(event)
            log.debug(
                "Hook received: %s %s (session=%s)",
                event.hook_event_name,
                event.tool_name or "",
                event.session_id[:8],
            )

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
