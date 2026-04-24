"""Detect Claude Code panes and build popup events from hook/buffer data."""

from __future__ import annotations

import dataclasses
import logging
import os
import re

from . import tmux
from .config import ParseRuleConfig, ParseRulesConfig
from .hook_server import HookEvent

log = logging.getLogger(__name__)


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
    scenario: str  # "permission" | "plan" | "idle"
    content: list[str]
    question: str
    options: list[str]
    selected_index: int
    hook_data: HookData | None = None
    plan_file_path: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


_OPTION_RE = re.compile(r"^\s*(❯)?\s*(\d+)\.\s+(.+)$")
_HELP_RE = re.compile(r"^\s*(Esc to cancel|ctrl-g to edit|shift\+tab)")


class ParseRuleMatcher:
    """Compiled parse rules for a single prompt scenario."""

    def __init__(self, rule: ParseRuleConfig) -> None:
        self._regexes = [re.compile(p) for p in rule.patterns]
        self._keywords = rule.keywords

    def matches(self, text: str) -> bool:
        return any(rx.search(text) for rx in self._regexes) or any(
            kw in text for kw in self._keywords
        )


class CompiledParseRules:
    """Pre-compiled parse rules for prompt line matching in tmux buffers."""

    def __init__(self, config: ParseRulesConfig) -> None:
        self.permission = ParseRuleMatcher(config.permission)
        self.plan = ParseRuleMatcher(config.plan)


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
    pane_id: str, parse_rules: CompiledParseRules, buffer_lines: int = 100
) -> tuple[list[str], int]:
    """Parse tmux buffer options for the current permission/plan prompt."""
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

    proceed_idx = None
    for i in range(len(tail) - 1, -1, -1):
        stripped = tail[i].strip()
        if parse_rules.plan.matches(stripped) or parse_rules.permission.matches(stripped):
            proceed_idx = i
            break

    if proceed_idx is None:
        return [], 0

    options: list[str] = []
    selected_index = 0
    scan_end = min(proceed_idx + 16, len(tail))
    for k in range(proceed_idx + 1, scan_end):
        s = tail[k].strip()
        if not s:
            continue
        m = _OPTION_RE.match(tail[k])
        if m:
            if m.group(1):
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
    """Build a TriggerEvent from hook data with optional buffer-derived options."""
    project_name = "unknown"
    session_name = ""
    if pane_id:
        cwd = await tmux.get_pane_cwd(pane_id)
        project_name = os.path.basename(cwd) if cwd else "unknown"
        session_name = await tmux.get_session_name(pane_id)

    tool_name = hook_event.tool_name or ""
    tool_input = hook_event.tool_input or {}

    if not tool_input and pretooluse_context:
        latest = pretooluse_context[-1]
        if latest.tool_input:
            tool_input = latest.tool_input
        if not tool_name and latest.tool_name:
            tool_name = latest.tool_name

    question = raw_payload.get("message", "")
    if not question:
        if tool_name:
            question = f"Allow {tool_name}?"
        else:
            question = "Permission requested"

    if buffer_options:
        options = list(buffer_options)
        selected_index = buffer_selected_index
    else:
        options = ["Allow"]
        suggestions = raw_payload.get("permission_suggestions", [])
        for suggestion in suggestions:
            if isinstance(suggestion, dict):
                rules = suggestion.get("rules", [])
                for rule in rules:
                    if isinstance(rule, dict):
                        content = rule.get("ruleContent", "")
                        if content:
                            options.append(f"Always Allow: {content}")
        options.append("Deny")
        selected_index = 0

    content: list[str] = []
    plan_file_path: str | None = None

    if tool_name == "ExitPlanMode":
        scenario = "plan"
        plan_path = tool_input.get("planFilePath", "")
        if plan_path:
            plan_file_path = os.path.expanduser(plan_path)
            content = _read_plan_file(plan_path)
    else:
        scenario = "permission"

    hook_data = HookData(
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
        hook_data=hook_data,
        plan_file_path=plan_file_path,
    )
