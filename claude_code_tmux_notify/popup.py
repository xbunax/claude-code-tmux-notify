"""Curses-based selector UI that runs inside a tmux display-popup.

Usage (launched by monitor.py via tmux display-popup):
    python -m claude_code_tmux_notify.popup --config /tmp/claude-code-tmux-notify-cfg-XXXX.json

Config JSON keys (TriggerEvent format):
    project_name, session_name, pane_id, scenario, content, question,
    options, selected_index, result_file
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import re
import sys
from io import StringIO

from rich.console import Console
from rich.markdown import Markdown
from rich.theme import Theme


FOCUS_OPTION = "聚焦到此 pane"
CUSTOM_OPTION = "自定义输入..."

# --- Color pair IDs (UI only) ---
C_SELECTED = 1
C_DIM = 2

# --- ANSI-to-curses color mapping ---
# We dynamically allocate curses color pairs for ANSI (fg, bg) combinations.
# Pair IDs 1-2 are reserved for UI; 10+ are for ANSI colors.
_ANSI_PAIR_OFFSET = 10
_ansi_pair_cache: dict[tuple[int, int], int] = {}

# Basic ANSI color (30-37) -> curses color
_ANSI_FG_TO_CURSES = {
    30: curses.COLOR_BLACK,
    31: curses.COLOR_RED,
    32: curses.COLOR_GREEN,
    33: curses.COLOR_YELLOW,
    34: curses.COLOR_BLUE,
    35: curses.COLOR_MAGENTA,
    36: curses.COLOR_CYAN,
    37: curses.COLOR_WHITE,
    # Bright variants (90-97) mapped to same colors
    90: curses.COLOR_BLACK,
    91: curses.COLOR_RED,
    92: curses.COLOR_GREEN,
    93: curses.COLOR_YELLOW,
    94: curses.COLOR_BLUE,
    95: curses.COLOR_MAGENTA,
    96: curses.COLOR_CYAN,
    97: curses.COLOR_WHITE,
}

# Basic ANSI background color (40-47, 100-107) -> curses color
_ANSI_BG_TO_CURSES = {
    40: curses.COLOR_BLACK,
    41: curses.COLOR_RED,
    42: curses.COLOR_GREEN,
    43: curses.COLOR_YELLOW,
    44: curses.COLOR_BLUE,
    45: curses.COLOR_MAGENTA,
    46: curses.COLOR_CYAN,
    47: curses.COLOR_WHITE,
    100: curses.COLOR_BLACK,
    101: curses.COLOR_RED,
    102: curses.COLOR_GREEN,
    103: curses.COLOR_YELLOW,
    104: curses.COLOR_BLUE,
    105: curses.COLOR_MAGENTA,
    106: curses.COLOR_CYAN,
    107: curses.COLOR_WHITE,
}

# ANSI SGR escape sequence pattern
_ANSI_RE = re.compile(r'\x1b\[([0-9;]*)m')


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.init_pair(C_SELECTED, curses.COLOR_CYAN, -1)
    curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)


def _get_ansi_pair(fg: int, bg: int) -> int:
    """Get or allocate a curses color pair for an ANSI (fg, bg) combination."""
    key = (fg, bg)
    if key in _ansi_pair_cache:
        return _ansi_pair_cache[key]
    pair_id = _ANSI_PAIR_OFFSET + len(_ansi_pair_cache)
    max_pairs = getattr(curses, 'COLOR_PAIRS', 256)
    if pair_id >= max_pairs - 1:
        return 0
    try:
        curses.init_pair(pair_id, fg, bg)
        _ansi_pair_cache[key] = pair_id
        return pair_id
    except (curses.error, ValueError):
        return 0


def _parse_ansi_line(line: str, default_bg: int = -1) -> list[tuple[str, int]]:
    """Parse a line with ANSI escape codes into (text, curses_attr) segments.

    *default_bg*: curses color index used as background when the segment has
    no explicit ANSI background.  Pass -1 (the default) for transparent.
    """
    segments: list[tuple[str, int]] = []
    text_attr = 0  # bold, dim, italic, underline, reverse
    cur_fg = -1    # -1 = default
    cur_bg = -1
    last_end = 0

    for m in _ANSI_RE.finditer(line):
        if m.start() > last_end:
            attr = _build_attr(text_attr, cur_fg, cur_bg, default_bg)
            segments.append((line[last_end:m.start()], attr))

        params_str = m.group(1)
        if not params_str:
            params = [0]
        else:
            params = [int(p) for p in params_str.split(';') if p]

        text_attr, cur_fg, cur_bg = _apply_sgr(params, text_attr, cur_fg, cur_bg)
        last_end = m.end()

    if last_end < len(line):
        attr = _build_attr(text_attr, cur_fg, cur_bg, default_bg)
        segments.append((line[last_end:], attr))

    return segments


def _build_attr(text_attr: int, fg: int, bg: int, default_bg: int = -1) -> int:
    """Combine text attributes with a (fg, bg) color pair.

    If *default_bg* is set and the segment has no explicit background,
    use *default_bg* so the text inherits the context-area background.
    """
    actual_bg = bg if bg != -1 else default_bg
    if fg == -1 and actual_bg == -1:
        return text_attr
    pair_id = _get_ansi_pair(fg, actual_bg)
    if pair_id:
        return text_attr | curses.color_pair(pair_id)
    return text_attr


def _apply_sgr(
    params: list[int], text_attr: int, fg: int, bg: int
) -> tuple[int, int, int]:
    """Apply SGR parameters, return updated (text_attr, fg, bg)."""
    i = 0
    while i < len(params):
        p = params[i]
        if p == 0:
            text_attr, fg, bg = 0, -1, -1
        elif p == 1:
            text_attr |= curses.A_BOLD
        elif p == 2:
            text_attr |= curses.A_DIM
        elif p == 3:
            text_attr |= curses.A_ITALIC if hasattr(curses, 'A_ITALIC') else 0
        elif p == 4:
            text_attr |= curses.A_UNDERLINE
        elif p == 7:
            text_attr |= curses.A_REVERSE
        elif p in _ANSI_FG_TO_CURSES:
            fg = _ANSI_FG_TO_CURSES[p]
        elif p in _ANSI_BG_TO_CURSES:
            bg = _ANSI_BG_TO_CURSES[p]
        elif p == 38 and i + 1 < len(params):
            # 38;5;N (256-color fg) or 38;2;R;G;B (truecolor fg)
            if params[i + 1] == 5 and i + 2 < len(params):
                fg = _256_to_basic(params[i + 2])
                i += 2
            elif params[i + 1] == 2 and i + 4 < len(params):
                fg = _rgb_to_basic(params[i + 2], params[i + 3], params[i + 4])
                i += 4
        elif p == 48 and i + 1 < len(params):
            # 48;5;N (256-color bg) or 48;2;R;G;B (truecolor bg)
            if params[i + 1] == 5 and i + 2 < len(params):
                bg = _256_to_basic(params[i + 2])
                i += 2
            elif params[i + 1] == 2 and i + 4 < len(params):
                bg = _rgb_to_basic(params[i + 2], params[i + 3], params[i + 4])
                i += 4
        elif p == 39:
            fg = -1
        elif p == 49:
            bg = -1
        elif p == 22:
            text_attr &= ~(curses.A_BOLD | curses.A_DIM)
        elif p == 23:
            if hasattr(curses, 'A_ITALIC'):
                text_attr &= ~curses.A_ITALIC
        elif p == 24:
            text_attr &= ~curses.A_UNDERLINE
        elif p == 27:
            text_attr &= ~curses.A_REVERSE
        i += 1
    return text_attr, fg, bg


def _256_to_basic(idx: int) -> int:
    """Map a 256-color index to a basic curses color."""
    if idx < 8:
        return [
            curses.COLOR_BLACK, curses.COLOR_RED, curses.COLOR_GREEN,
            curses.COLOR_YELLOW, curses.COLOR_BLUE, curses.COLOR_MAGENTA,
            curses.COLOR_CYAN, curses.COLOR_WHITE,
        ][idx]
    if idx < 16:
        return [
            curses.COLOR_BLACK, curses.COLOR_RED, curses.COLOR_GREEN,
            curses.COLOR_YELLOW, curses.COLOR_BLUE, curses.COLOR_MAGENTA,
            curses.COLOR_CYAN, curses.COLOR_WHITE,
        ][idx - 8]
    # 216-color cube + grayscale: rough approximation
    if idx < 232:
        idx -= 16
        b = idx % 6
        g = (idx // 6) % 6
        r = idx // 36
        return _rgb_to_basic(r * 51, g * 51, b * 51)
    # Grayscale
    return curses.COLOR_WHITE if idx >= 244 else curses.COLOR_BLACK


def _rgb_to_basic(r: int, g: int, b: int) -> int:
    """Map RGB to the nearest basic curses color."""
    colors = [
        (0, 0, 0, curses.COLOR_BLACK),
        (205, 0, 0, curses.COLOR_RED),
        (0, 205, 0, curses.COLOR_GREEN),
        (205, 205, 0, curses.COLOR_YELLOW),
        (0, 0, 238, curses.COLOR_BLUE),
        (205, 0, 205, curses.COLOR_MAGENTA),
        (0, 205, 205, curses.COLOR_CYAN),
        (229, 229, 229, curses.COLOR_WHITE),
    ]
    best = curses.COLOR_WHITE
    best_dist = float('inf')
    for cr, cg, cb, cc in colors:
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if d < best_dist:
            best_dist = d
            best = cc
    return best


# ---------------------------------------------------------------------------
# Rich markdown rendering
# ---------------------------------------------------------------------------

_RICH_THEME = Theme({
    "markdown.code": "cyan",
    "markdown.h1": "bold yellow underline",
    "markdown.h2": "bold yellow",
    "markdown.h3": "yellow",
})


def _render_markdown_ansi(
    context_lines: list[str], width: int, hook_data: dict | None = None
) -> list[str]:
    """Render hook data as markdown using rich, return ANSI-formatted lines.

    Only hook_data is used for content rendering; buffer context_lines are ignored.
    """
    parts: list[str] = []

    if hook_data:
        tool_name = hook_data.get("tool_name")
        tool_input = hook_data.get("tool_input")
        if tool_name:
            parts.append(f"**{tool_name}**")
            if tool_input:
                if tool_name == "Bash" and "command" in tool_input:
                    parts.append(f"```bash\n{tool_input['command']}\n```")
                elif tool_name in ("Read", "Write", "Edit", "Glob") and "file_path" in tool_input:
                    parts.append(f"`{tool_input['file_path']}`")
                elif tool_name == "Grep" and "pattern" in tool_input:
                    parts.append(f"`{tool_input['pattern']}`")
                else:
                    import json as _json
                    parts.append(f"```json\n{_json.dumps(tool_input, indent=2, ensure_ascii=False)}\n```")

    text = "\n".join(parts)
    if not text.strip():
        return []

    buf = StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        width=width,
        color_system="256",
        theme=_RICH_THEME,
    )
    md = Markdown(text, code_theme="monokai")
    console.print(md)
    output = buf.getvalue()
    # Strip trailing newlines but keep internal structure
    return output.rstrip("\n").splitlines()


def _draw_ansi_line(
    stdscr: curses.window, row: int, col: int,
    line: str, max_w: int, default_bg: int = -1,
) -> None:
    """Draw a single ANSI-formatted line into curses.

    *default_bg*: if set, fill the row with this background first and use it
    as the fallback background for text segments without an explicit bg.
    """
    # Fill the row with the default background so the entire line has it
    if default_bg != -1:
        bg_pair = _get_ansi_pair(-1, default_bg)
        if bg_pair:
            try:
                stdscr.addnstr(row, col, " " * max_w, max_w, curses.color_pair(bg_pair))
            except curses.error:
                pass

    segments = _parse_ansi_line(line, default_bg)
    x = col
    for text, attr in segments:
        remaining = max_w - (x - col)
        if remaining <= 0:
            break
        s = text[:remaining]
        try:
            stdscr.addnstr(row, x, s, remaining, attr)
        except curses.error:
            pass
        x += len(s)


# ---------------------------------------------------------------------------
# Main draw + UI
# ---------------------------------------------------------------------------

def _draw(
    stdscr: curses.window,
    options: list[str],
    selected: int,
    context: list[str],
    question: str,
    scenario: str,
    project_name: str,
    input_mode: bool = False,
    input_buf: str = "",
    hook_data: dict | None = None,
) -> None:
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()
    usable_w = max_x - 4  # 2 char padding each side

    row = 1

    # --- Layout: reserve space for options/input, question, and help ---
    options_height = len(options) + 3 if not input_mode else 6
    reserved = options_height + 2

    # --- Context rendered via rich markdown (with optional hook data) ---
    # Use a dark-gray background (256-color 236) to visually distinguish context
    ctx_bg = 236 if curses.COLORS >= 256 else curses.COLOR_BLACK
    ansi_lines = _render_markdown_ansi(context, usable_w, hook_data)
    for line in ansi_lines:
        if row >= max_y - reserved:
            break
        _draw_ansi_line(stdscr, row, 2, line, usable_w, default_bg=ctx_bg)
        row += 1

    if context or ansi_lines:
        row += 1

    # --- Question ---
    if row < max_y - reserved:
        stdscr.attron(curses.A_BOLD)
        try:
            stdscr.addnstr(row, 2, question[:usable_w], usable_w)
        except curses.error:
            pass
        stdscr.attroff(curses.A_BOLD)
        row += 2

    # --- Options ---
    if not input_mode:
        for i, opt in enumerate(options):
            if row >= max_y - 2:
                break
            is_sel = i == selected
            prefix = " > " if is_sel else "   "
            attr = (curses.color_pair(C_SELECTED) | curses.A_BOLD) if is_sel else 0
            try:
                stdscr.addnstr(row, 1, prefix, 3, attr)
                stdscr.addnstr(row, 4, opt[:usable_w - 4], usable_w - 4, attr)
            except curses.error:
                pass
            row += 1

        row += 1
        help_text = "↑↓ select · Enter confirm · Esc cancel"
        if row < max_y:
            try:
                stdscr.addnstr(
                    row, 2, help_text, usable_w,
                    curses.color_pair(C_DIM) | curses.A_DIM,
                )
            except curses.error:
                pass
    else:
        prompt_text = "输入内容 (Enter 发送, Esc 返回):"
        if row < max_y - 3:
            try:
                stdscr.addnstr(row, 2, prompt_text[:usable_w], usable_w)
            except curses.error:
                pass
            row += 2
            try:
                stdscr.addnstr(row, 2, "> " + input_buf[:usable_w - 4], usable_w)
            except curses.error:
                pass

    stdscr.refresh()


def _main(stdscr: curses.window, args: argparse.Namespace) -> str | None:
    curses.curs_set(0)
    curses.use_default_colors()
    _init_colors()
    stdscr.timeout(-1)

    options: list[str] = list(args.options)
    options.append(FOCUS_OPTION)
    options.append(CUSTOM_OPTION)

    selected = args.selected
    context = args.context
    question = args.question
    scenario = args.scenario
    project_name = args.project_name
    hook_data = args.hook_data

    input_mode = False
    input_buf = ""

    while True:
        _draw(
            stdscr, options, selected, context, question,
            scenario, project_name, input_mode, input_buf, hook_data,
        )

        try:
            key = stdscr.get_wch()
        except curses.error:
            continue

        if input_mode:
            if key == "\x1b":
                input_mode = False
                input_buf = ""
                curses.curs_set(0)
            elif key in ("\n", "\r") or key == curses.KEY_ENTER:
                if input_buf.strip():
                    return f"custom:{input_buf.strip()}"
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
                input_buf = input_buf[:-1]
            elif isinstance(key, str) and key.isprintable():
                input_buf += key
            continue

        if key == curses.KEY_UP:
            selected = max(0, selected - 1)
        elif key == curses.KEY_DOWN:
            selected = min(len(options) - 1, selected + 1)
        elif key in ("\n", "\r") or key == curses.KEY_ENTER:
            if options[selected] == CUSTOM_OPTION:
                input_mode = True
                curses.curs_set(1)
            elif options[selected] == FOCUS_OPTION:
                return "focus"
            else:
                return f"option:{selected}"
        elif key == "\x1b":
            return None
        elif isinstance(key, str) and "1" <= key <= "9":
            idx = int(key) - 1
            real_option_count = len(options) - 2
            if idx < real_option_count:
                return f"option:{idx}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)

    args.result_file = cfg["result_file"]
    args.options = cfg["options"]
    args.selected = cfg.get("selected_index", 0)
    args.context = cfg.get("content", [])
    args.question = cfg.get("question", "Do you want to proceed?")
    args.scenario = cfg.get("scenario", "permission")
    args.project_name = cfg.get("project_name", "")
    args.session_name = cfg.get("session_name", "")
    args.pane_id = cfg.get("pane_id", "")
    args.hook_data = cfg.get("hook_data")

    result = curses.wrapper(_main, args)

    if result is not None:
        with open(args.result_file, "w") as f:
            f.write(result)
        sys.exit(0)
    else:
        if os.path.exists(args.result_file):
            os.unlink(args.result_file)
        sys.exit(1)


if __name__ == "__main__":
    main()
