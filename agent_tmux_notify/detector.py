"""Detect Claude Code panes and parse their state from tmux buffer content."""

from __future__ import annotations

import dataclasses
import enum
import logging
import os
import re

from . import tmux
from .config import TriggersConfig, TriggerScenario, default_triggers
from .hook_server import HookEvent

log = logging.getLogger(__name__)


class PaneState(enum.Enum):
    IDLE = "idle"
    WORKING = "working"
    COMPLETED = "completed"
    NEEDS_INPUT = "needs_input"
    UNKNOWN = "unknown"


@dataclasses.dataclass
class DetectedState:
    state: PaneState
    prompt_type: str | None = None  # "permission" | "plan"
    prompt_question: str | None = None
    options: list[str] = dataclasses.field(default_factory=list)
    selected_index: int = 0
    context_lines: list[str] = dataclasses.field(default_factory=list)
    plan_file_path: str | None = None
    help_text: str | None = None


@dataclasses.dataclass
class ClaudePane:
    pane: tmux.PaneInfo
    claude_pid: int


@dataclasses.dataclass
class HookData:
    session_id: str
    hook_event_name: str
    tool_name: str | None = None
    tool_input: dict | None = None
    cwd: str | None = None


@dataclasses.dataclass
class TriggerEvent:
    project_name: str
    session_name: str
    pane_id: str
    scenario: str          # "permission" | "plan" | "completed"
    content: list[str]     # 上下文行
    question: str
    options: list[str]
    selected_index: int
    hook_data: HookData | None = None
    plan_file_path: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# --- Structural patterns (not user-configurable) ---

# Permission prompt: solid line separator ─
_PERMISSION_SEP_RE = re.compile(r"^─{10,}$")
# Plan approval: dashed line separator ╌
_PLAN_SEP_RE = re.compile(r"^╌{10,}$")
# Numbered option line: optional ❯ prefix, then "N. text"
_OPTION_RE = re.compile(r"^\s*(❯)?\s*(\d+)\.\s+(.+)$")
# Spinner: ✽ Drizzling… (2m 13s · ↓ 3.3k tokens)
_SPINNER_RE = re.compile(r"^[✽✢✻⏺]\s+\S+…")
# Idle prompt
_IDLE_RE = re.compile(r"^❯\s*$")
# Help text at bottom of prompts
_HELP_RE = re.compile(r"^\s*(Esc to cancel|ctrl-g to edit|shift\+tab)")
# Plan file path patterns:
#   "Plan saved to: ~/.claude/plans/xxx.md"
#   "ctrl-g to edit in Nvim · ~/.claude/plans/xxx.md"
_PLAN_PATH_PATTERNS = [
    re.compile(r"Plan saved to:\s*(\S+\.md)"),
    re.compile(r"ctrl-g to edit.*?·\s*(\S+\.md)"),
]


def _read_plan_from_buffer(tail: list[str]) -> tuple[list[str], str | None]:
    """Extract plan file path from buffer lines and read the file content.

    Returns (content_lines, expanded_file_path).
    """
    for line in tail:
        for pat in _PLAN_PATH_PATTERNS:
            m = pat.search(line)
            if m:
                path = os.path.expanduser(m.group(1))
                try:
                    with open(path) as f:
                        return f.read().splitlines(), path
                except OSError:
                    log.debug("Failed to read plan file: %s", path)
                    return [], path
    return [], None


# --- Configurable trigger matchers ---

class TriggerMatcher:
    """Compiled trigger patterns for a single scenario."""

    def __init__(self, scenario: TriggerScenario) -> None:
        self._regexes = [re.compile(p) for p in scenario.patterns]
        self._keywords = scenario.keywords

    def matches(self, text: str) -> bool:
        return (
            any(rx.search(text) for rx in self._regexes)
            or any(kw in text for kw in self._keywords)
        )


class CompiledTriggers:
    """Pre-compiled triggers for all scenarios."""

    def __init__(self, config: TriggersConfig) -> None:
        self.permission = TriggerMatcher(config.permission)
        self.plan = TriggerMatcher(config.plan)
        self.completed = TriggerMatcher(config.completed)


async def find_claude_panes() -> list[ClaudePane]:
    """Scan all tmux panes and return those running Claude Code."""
    panes = await tmux.list_panes()
    result: list[ClaudePane] = []
    for pane in panes:
        descendants = await tmux.get_descendant_pids(pane.pid)
        for pid, comm in descendants:
            if comm == "claude":
                result.append(ClaudePane(pane=pane, claude_pid=pid))
                break
    return result


async def extract_options_from_buffer(
    pane_id: str, triggers: CompiledTriggers, buffer_lines: int = 100
) -> tuple[list[str], int]:
    """从 tmux buffer 解析选项列表和当前选中索引。

    Returns (options, selected_index)。解析失败返回 ([], 0)。
    """
    try:
        text = await tmux.capture_pane(pane_id, lines=buffer_lines)
    except RuntimeError:
        return [], 0

    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return [], 0

    tail = lines[-60:]

    # 从底部向上找到 prompt 行
    proceed_idx = None
    for i in range(len(tail) - 1, -1, -1):
        stripped = tail[i].strip()
        if triggers.plan.matches(stripped) or triggers.permission.matches(stripped):
            proceed_idx = i
            break

    if proceed_idx is None:
        return [], 0

    # 扫描 prompt 下方的选项
    options: list[str] = []
    selected_index = 0
    scan_end = min(proceed_idx + 16, len(tail))
    for k in range(proceed_idx + 1, scan_end):
        s = tail[k].strip()
        if not s:
            continue
        m = _OPTION_RE.match(tail[k])
        if m:
            if m.group(1):  # ❯ present
                selected_index = len(options)
            options.append(m.group(3).strip())
            continue
        if _HELP_RE.match(s):
            break

    return options, selected_index


def _read_plan_file(path: str) -> list[str]:
    """Read plan file content, return lines or empty list on failure."""
    expanded = os.path.expanduser(path)
    try:
        with open(expanded) as f:
            return f.read().splitlines()
    except OSError:
        log.debug("Failed to read plan file: %s", expanded)
        return []


async def build_trigger_event_from_hook(
    pane_id: str | None,
    hook_event: HookEvent,
    raw_payload: dict,
    pretooluse_context: list[HookEvent] | None = None,
    buffer_options: list[str] | None = None,
    buffer_selected_index: int = 0,
) -> TriggerEvent:
    """Build a TriggerEvent from hook data, with optional PreToolUse context and buffer options."""
    cwd = None
    project_name = "unknown"
    session_name = ""
    if pane_id:
        cwd = await tmux.get_pane_cwd(pane_id)
        project_name = os.path.basename(cwd) if cwd else "unknown"
        session_name = await tmux.get_session_name(pane_id)

    tool_name = hook_event.tool_name or ""
    tool_input = hook_event.tool_input or {}

    # PreToolUse 上下文补充：优先用 PermissionRequest 自身的 tool_input，
    # 仅在缺失时从最近的 PreToolUse 补充
    if not tool_input and pretooluse_context:
        latest = pretooluse_context[-1]
        if latest.tool_input:
            tool_input = latest.tool_input
        if not tool_name and latest.tool_name:
            tool_name = latest.tool_name

    # Build question from payload
    question = raw_payload.get("message", "")
    if not question:
        if tool_name:
            question = f"Allow {tool_name}?"
        else:
            question = "Permission requested"

    # 选项优先级：buffer > hook payload fallback
    if buffer_options:
        options = list(buffer_options)
        selected_index = buffer_selected_index
    else:
        # 从 permission_suggestions 构建选项（兜底）
        options = ["Allow"]
        suggestions = raw_payload.get("permission_suggestions", [])
        for sug in suggestions:
            if isinstance(sug, dict):
                rules = sug.get("rules", [])
                for rule in rules:
                    if isinstance(rule, dict):
                        content = rule.get("ruleContent", "")
                        if content:
                            options.append(f"Always Allow: {content}")
        options.append("Deny")
        selected_index = 0

    # Determine scenario + plan content
    content: list[str] = []
    plan_file_path: str | None = None

    if tool_name == "ExitPlanMode":
        scenario = "plan"
        # 从 tool_input 提取 planFilePath 并读取内容
        pfp = tool_input.get("planFilePath", "")
        if pfp:
            plan_file_path = os.path.expanduser(pfp)
            content = _read_plan_file(pfp)
    else:
        scenario = "permission"

    hd = HookData(
        session_id=hook_event.session_id,
        hook_event_name=hook_event.hook_event_name,
        tool_name=tool_name or None,
        tool_input=tool_input or None,
        cwd=hook_event.cwd,
    )

    return TriggerEvent(
        project_name=project_name,
        session_name=session_name,
        pane_id=pane_id or "",
        scenario=scenario,
        content=content,
        question=question,
        options=options,
        selected_index=selected_index,
        hook_data=hd,
        plan_file_path=plan_file_path,
    )


async def build_trigger_event(
    pane_id: str,
    state: DetectedState,
    hook_event: object | None = None,
) -> TriggerEvent:
    """Build a structured TriggerEvent from a DetectedState and pane metadata.

    If a HookEvent is provided, its structured data is included as hook_data.
    """
    cwd = await tmux.get_pane_cwd(pane_id)
    project_name = os.path.basename(cwd) if cwd else "unknown"
    session_name = await tmux.get_session_name(pane_id)

    hd: HookData | None = None
    if hook_event is not None:
        hd = HookData(
            session_id=getattr(hook_event, "session_id", ""),
            hook_event_name=getattr(hook_event, "hook_event_name", ""),
            tool_name=getattr(hook_event, "tool_name", None),
            tool_input=getattr(hook_event, "tool_input", None),
            cwd=getattr(hook_event, "cwd", None),
        )

    return TriggerEvent(
        project_name=project_name,
        session_name=session_name,
        pane_id=pane_id,
        scenario=state.prompt_type or "permission",
        content=state.context_lines,
        question=state.prompt_question or "",
        options=state.options,
        selected_index=state.selected_index,
        hook_data=hd,
        plan_file_path=state.plan_file_path,
    )


def parse_buffer(text: str, triggers: CompiledTriggers) -> DetectedState:
    """Parse tmux buffer text and determine Claude Code state."""
    lines = text.splitlines()
    if not lines:
        return DetectedState(state=PaneState.UNKNOWN)

    # Strip trailing blank lines — tmux panes often have terminal padding
    # that pushes real content (separators, tool info) out of the tail window.
    while lines and not lines[-1].strip():
        lines.pop()

    # Scan from bottom up for the key patterns.
    # We work on the last ~60 lines to keep it fast.
    tail = lines[-60:]

    # --- 1. Check for NEEDS_INPUT (highest priority) ---
    # Look for a line matching permission or plan triggers
    proceed_idx = None
    matched_scenario: str | None = None
    for i in range(len(tail) - 1, -1, -1):
        stripped = tail[i].strip()
        if triggers.plan.matches(stripped):
            proceed_idx = i
            matched_scenario = "plan"
            break
        if triggers.permission.matches(stripped):
            proceed_idx = i
            matched_scenario = "permission"
            break

    if proceed_idx is not None:
        # Secondary check: if a spinner appears AFTER the matched prompt,
        # Claude Code is already working (the prompt was already answered).
        # In that case, skip NEEDS_INPUT and fall through to WORKING detection.
        for k in range(proceed_idx + 1, len(tail)):
            if _SPINNER_RE.match(tail[k].strip()):
                log.debug(
                    "Pattern matched at line %d but spinner found at line %d — "
                    "Claude is working, suppressing NEEDS_INPUT",
                    proceed_idx, k,
                )
                proceed_idx = None
                break

    if proceed_idx is not None:
        # Determine prompt type by scanning upward for separator (authoritative)
        prompt_type = matched_scenario
        for j in range(proceed_idx - 1, max(proceed_idx - 30, -1), -1):
            s = tail[j].strip()
            if _PERMISSION_SEP_RE.match(s):
                prompt_type = "permission"
                break
            if _PLAN_SEP_RE.match(s):
                prompt_type = "plan"
                break

        # Extract options below the question
        options: list[str] = []
        selected_index = 0
        help_text = None
        # Scan at most 15 lines below the question for options.
        # This prevents picking up numbered lines from conversation history
        # that appear far below the actual prompt.
        scan_end = min(proceed_idx + 16, len(tail))
        for k in range(proceed_idx + 1, scan_end):
            s = tail[k].strip()
            if not s:
                continue
            m = _OPTION_RE.match(tail[k])
            if m:
                if m.group(1):  # ❯ present
                    selected_index = len(options)
                options.append(m.group(3).strip())
                continue
            if _HELP_RE.match(s):
                help_text = s
                break

        # Extract context lines above the question (tool info, plan summary, etc.)
        sep_idx = proceed_idx  # default: start from question
        for j in range(proceed_idx - 1, max(proceed_idx - 30, -1), -1):
            s = tail[j].strip()
            if _PERMISSION_SEP_RE.match(s) or _PLAN_SEP_RE.match(s):
                sep_idx = j + 1
                break

        if prompt_type == "plan":
            # Plan: find the plan file path from buffer and read it directly
            context_lines, plan_file_path = _read_plan_from_buffer(tail)
        else:
            # Permission: grab content between separator and question (tool info)
            context_lines = [l for l in tail[sep_idx:proceed_idx] if l.strip()]
            plan_file_path = None

        question = tail[proceed_idx].strip()

        return DetectedState(
            state=PaneState.NEEDS_INPUT,
            prompt_type=prompt_type,
            prompt_question=question,
            options=options,
            selected_index=selected_index,
            context_lines=context_lines,
            plan_file_path=plan_file_path,
            help_text=help_text,
        )

    # --- 2. Check for WORKING ---
    for line in reversed(tail[-15:]):
        stripped = line.strip()
        if _SPINNER_RE.match(stripped):
            return DetectedState(state=PaneState.WORKING)

    # --- 3. Check for COMPLETED ---
    for line in reversed(tail[-15:]):
        stripped = line.strip()
        if triggers.completed.matches(stripped):
            return DetectedState(state=PaneState.COMPLETED)

    # --- 4. Check for IDLE ---
    for line in reversed(tail[-10:]):
        if _IDLE_RE.match(line.strip()):
            return DetectedState(state=PaneState.IDLE)

    return DetectedState(state=PaneState.UNKNOWN)


async def detect_state(pane_id: str, triggers: CompiledTriggers, buffer_lines: int = 100) -> DetectedState:
    """Capture a pane's buffer and detect its Claude Code state."""
    text = await tmux.capture_pane(pane_id, lines=buffer_lines)
    return parse_buffer(text, triggers)
