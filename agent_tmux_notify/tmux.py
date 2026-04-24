"""Async wrappers around tmux CLI commands."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import shlex
import sys

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class PaneInfo:
    pane_id: str      # tmux 唯一 pane ID，如 "%3"
    session_id: str   # tmux 唯一 session ID，如 "$0"
    window_id: str    # tmux 唯一 window ID，如 "@1"
    pid: int
    tty: str

    @property
    def global_pane_id(self) -> str:
        """全局唯一标识：session_id/window_id/pane_id，如 '$1/@1/%3'。"""
        return f"{self.session_id}/{self.window_id}/{self.pane_id}"


def _target(global_pane_id: str) -> str:
    """从 global_pane_id 提取 tmux -t 可用的 pane_id（%N 部分）。

    如果不含 '/'，原样返回（兼容裸 pane_id）。
    """
    if "/" in global_pane_id:
        return global_pane_id.rsplit("/", 1)[1]
    return global_pane_id


async def _run(
    *args: str, check: bool = True
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command {args!r} failed ({proc.returncode}): {err}")
    return proc.returncode, out, err


async def list_panes() -> list[PaneInfo]:
    fmt = "#{pane_id}\t#{session_id}\t#{window_id}\t#{pane_pid}\t#{pane_tty}"
    _, out, _ = await _run("tmux", "list-panes", "-a", "-F", fmt)
    panes: list[PaneInfo] = []
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        panes.append(PaneInfo(
            pane_id=parts[0], session_id=parts[1], window_id=parts[2],
            pid=int(parts[3]), tty=parts[4],
        ))
    return panes


async def capture_pane(pane_id: str, lines: int = 100) -> str:
    _, out, _ = await _run(
        "tmux", "capture-pane", "-t", _target(pane_id), "-p", "-S", f"-{lines}"
    )
    return out


async def send_keys(pane_id: str, keys: list[str]) -> None:
    t = _target(pane_id)
    for key in keys:
        await _run("tmux", "send-keys", "-t", t, key)


async def send_keys_literal(pane_id: str, text: str) -> None:
    await _run("tmux", "send-keys", "-t", _target(pane_id), "-l", text)


async def is_pane_focused(pane_id: str) -> bool:
    """检查 pane 是否是用户当前真正聚焦的 pane（跨 session 感知）。"""
    active = await get_active_pane_id()
    if not active:
        return False
    if "/" in pane_id:
        return active == pane_id
    return _target(active) == pane_id


async def _get_active_client_tty() -> str | None:
    """找到用户真正聚焦的 tmux client 的 tty。

    优先使用 client_flags 中的 "focused" 标记（tmux 3.3+），
    回退到 client_activity。
    """
    try:
        _, out, _ = await _run(
            "tmux", "list-clients", "-F",
            "#{client_activity}\t#{client_tty}\t#{client_flags}",
        )
        best_tty = None
        best_activity = -1
        for line in out.strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            activity = int(parts[0])
            tty = parts[1]
            flags = parts[2] if len(parts) >= 3 else ""
            if "focused" in flags:
                return tty
            if activity > best_activity:
                best_activity = activity
                best_tty = tty
        return best_tty
    except RuntimeError:
        return None


async def get_active_pane_id() -> str | None:
    """返回当前焦点 pane 的 global_pane_id（session_id/window_id/pane_id）。

    Uses ``list-clients`` to find the most recently active client, then
    ``display-message -c`` to get that client's actual focused pane.
    This is reliable even from a launchd daemon and handles multiple
    attached sessions correctly.
    """
    try:
        best_tty = await _get_active_client_tty()
        if not best_tty:
            return None

        # Get the active pane as global_pane_id
        _, out, _ = await _run(
            "tmux", "display-message", "-c", best_tty, "-p",
            "#{session_id}/#{window_id}/#{pane_id}",
        )
        gid = out.strip()
        return gid if gid else None
    except RuntimeError:
        return None


async def display_popup(
    pane_id: str,
    command: list[str],
    width: str = "80%",
    height: str = "60%",
    title: str = "",
    x: str | None = None,
    y: str | None = None,
) -> int:
    # tmux display-popup runs the command through sh -c, so we need to
    # properly quote the command as a single shell string.
    shell_cmd = shlex.join(command)
    # Add stderr logging for debugging
    log_file = "/tmp/agent-tmux-notify-popup.log"
    shell_cmd = f"{shell_cmd} 2>>{shlex.quote(log_file)}"

    args = ["tmux", "display-popup", "-t", _target(pane_id), "-w", width, "-h", height]
    if x is not None:
        args.extend(["-x", x])
    if y is not None:
        args.extend(["-y", y])
    args.append("-E")
    if title:
        args.extend(["-T", title])
    args.append(shell_cmd)
    rc, _, _ = await _run(*args, check=False)
    return rc


async def get_pane_cwd(pane_id: str) -> str:
    """Return the current working directory of a tmux pane."""
    _, out, _ = await _run(
        "tmux", "display-message", "-t", _target(pane_id), "-p", "#{pane_current_path}"
    )
    return out.strip()


async def get_session_name(pane_id: str) -> str:
    """Return the session name for a given pane."""
    _, out, _ = await _run(
        "tmux", "display-message", "-t", _target(pane_id), "-p", "#{session_name}"
    )
    return out.strip()


async def select_pane(pane_id: str) -> None:
    """切换 tmux 焦点到指定 pane（支持跨 session/window）。

    接受 global_pane_id（'$1/@1/%3'）或裸 pane_id（'%3'）。
    通过 -c client_tty 指定要切换的 client，避免 daemon 下切错。
    """
    parts = pane_id.split("/")
    if len(parts) == 3:
        sid, wid, pid = parts
    else:
        pid = _target(pane_id)
        sid = wid = ""

    client_tty = await _get_active_client_tty()
    if sid and client_tty:
        await _run("tmux", "switch-client", "-c", client_tty, "-t", sid)
    if wid:
        await _run("tmux", "select-window", "-t", wid)
    await _run("tmux", "select-pane", "-t", pid)


async def get_descendant_pids(root_pid: int) -> list[tuple[int, str]]:
    """Recursively find all descendant processes of *root_pid*.

    Returns list of (pid, command_name) pairs.
    """
    result: list[tuple[int, str]] = []
    queue = [root_pid]
    visited: set[int] = set()

    while queue:
        pid = queue.pop()
        if pid in visited:
            continue
        visited.add(pid)

        rc, out, _ = await _run("pgrep", "-P", str(pid), check=False)
        if rc != 0 or not out.strip():
            continue
        for child_str in out.strip().splitlines():
            child_pid = int(child_str.strip())
            # Get command name
            rc2, comm_out, _ = await _run(
                "ps", "-o", "comm=", "-p", str(child_pid), check=False
            )
            comm = comm_out.strip().split("/")[-1] if rc2 == 0 else ""
            result.append((child_pid, comm))
            queue.append(child_pid)

    return result
