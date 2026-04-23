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
    pane_id: str  # "session:window.pane"
    pid: int
    tty: str


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
    fmt = "#{session_name}:#{window_index}.#{pane_index}\t#{pane_pid}\t#{pane_tty}"
    _, out, _ = await _run("tmux", "list-panes", "-a", "-F", fmt)
    panes: list[PaneInfo] = []
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        panes.append(PaneInfo(pane_id=parts[0], pid=int(parts[1]), tty=parts[2]))
    return panes


async def capture_pane(pane_id: str, lines: int = 100) -> str:
    _, out, _ = await _run(
        "tmux", "capture-pane", "-t", pane_id, "-p", "-S", f"-{lines}"
    )
    return out


async def send_keys(pane_id: str, keys: list[str]) -> None:
    for key in keys:
        await _run("tmux", "send-keys", "-t", pane_id, key)


async def send_keys_literal(pane_id: str, text: str) -> None:
    await _run("tmux", "send-keys", "-t", pane_id, "-l", text)


async def get_active_pane_id() -> str | None:
    """Return the pane_id of the currently focused pane (session:window.pane)."""
    try:
        _, out, _ = await _run(
            "tmux", "display-message", "-p",
            "#{session_name}:#{window_index}.#{pane_index}",
        )
        result = out.strip()
        return result if result else None
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

    args = ["tmux", "display-popup", "-t", pane_id, "-w", width, "-h", height]
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
        "tmux", "display-message", "-t", pane_id, "-p", "#{pane_current_path}"
    )
    return out.strip()


async def get_session_name(pane_id: str) -> str:
    """Return the session name for a given pane."""
    _, out, _ = await _run(
        "tmux", "display-message", "-t", pane_id, "-p", "#{session_name}"
    )
    return out.strip()


async def select_pane(pane_id: str) -> None:
    """Switch tmux focus to the specified pane (handles cross-session/window)."""
    # pane_id format: "session:window.pane"
    # Switch session first so cross-session focus works
    session = pane_id.split(":")[0]
    window = pane_id.rsplit(".", 1)[0]
    await _run("tmux", "switch-client", "-t", session)
    await _run("tmux", "select-window", "-t", window)
    await _run("tmux", "select-pane", "-t", pane_id)


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
