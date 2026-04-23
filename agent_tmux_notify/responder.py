"""Send user's popup selection back to the Claude Code pane."""

from __future__ import annotations

import logging

from . import tmux
from .config import default_triggers
from .detector import CompiledTriggers, PaneState, detect_state

log = logging.getLogger(__name__)


async def send_response(
    pane_id: str,
    choice_index: int,
    current_selected: int,
    total_options: int,
    custom_text: str | None = None,
    triggers: CompiledTriggers | None = None,
    buffer_lines: int = 100,
) -> bool:
    """Map the user's popup choice to tmux send-keys on *pane_id*.

    If *custom_text* is set, sends literal text + Enter instead of arrow navigation.
    Returns True if keys were sent, False if skipped (state changed).
    """
    if triggers is None:
        triggers = CompiledTriggers(default_triggers())

    # Safety: re-check state before sending
    state = await detect_state(pane_id, triggers, buffer_lines)
    if state.state != PaneState.NEEDS_INPUT:
        log.warning("Pane %s no longer needs input, skipping send", pane_id)
        return False

    if custom_text is not None:
        # Tab to focus Claude Code's input field, then type the text
        await tmux.send_keys(pane_id, ["Tab"])
        await tmux.send_keys_literal(pane_id, custom_text)
        await tmux.send_keys(pane_id, ["Enter"])
        log.info("Sent custom text to %s: %s", pane_id, custom_text[:60])
        return True

    # Navigate to the target option using arrow keys.
    # Strategy: go Up enough to reach first item, then Down to target.
    up_count = current_selected
    down_count = choice_index

    keys: list[str] = []
    keys.extend(["Up"] * up_count)
    keys.extend(["Down"] * down_count)
    keys.append("Enter")

    await tmux.send_keys(pane_id, keys)
    log.info(
        "Sent option %d to %s (Up×%d, Down×%d, Enter)",
        choice_index + 1,
        pane_id,
        up_count,
        down_count,
    )
    return True
