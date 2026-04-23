"""Configure Claude Code hooks in ~/.claude/settings.json.

Usage:
    python -m agent_tmux_notify.setup_hooks [--port PORT]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

DEFAULT_PORT = 19836
SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")

HOOK_EVENTS = ["PreToolUse", "PermissionRequest", "Notification"]
ALL_HOOK_EVENTS = ["PreToolUse", "PermissionRequest", "Notification", "Stop"]


def _build_hook_entry(port: int) -> dict:
    return {
        "matcher": "*",
        "hooks": [
            {
                "type": "http",
                "url": f"http://127.0.0.1:{port}/hook",
            }
        ],
    }


def setup_hooks(port: int = DEFAULT_PORT, all_events: bool = False) -> None:
    """Read, merge, and write hooks config into ~/.claude/settings.json."""
    events = ALL_HOOK_EVENTS if all_events else HOOK_EVENTS
    # Ensure directory exists
    settings_dir = os.path.dirname(SETTINGS_PATH)
    os.makedirs(settings_dir, exist_ok=True)

    # Read existing settings
    settings: dict = {}
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH) as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: failed to read {SETTINGS_PATH}: {e}", file=sys.stderr)
            print("Creating new settings file.", file=sys.stderr)

    hooks = settings.setdefault("hooks", {})
    hook_url = f"http://127.0.0.1:{port}/hook"
    entry = _build_hook_entry(port)

    added: list[str] = []
    skipped: list[str] = []

    for event in events:
        event_hooks = hooks.setdefault(event, [])
        # Check if our hook URL is already configured
        already = any(
            isinstance(mg, dict)
            and any(
                isinstance(h, dict) and h.get("url") == hook_url
                for h in mg.get("hooks", [])
            )
            for mg in event_hooks
        )
        if already:
            skipped.append(event)
        else:
            event_hooks.append(entry)
            added.append(event)

    # Write back
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Settings file: {SETTINGS_PATH}")
    print(f"Hook URL: {hook_url}")
    if added:
        print(f"Added hooks for: {', '.join(added)}")
    if skipped:
        print(f"Already configured: {', '.join(skipped)}")
    if not added and skipped:
        print("No changes needed.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure Claude Code hooks for agent-tmux-notify"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Hook server port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--all-events", action="store_true",
        help="Register all hook events (PreToolUse, Stop, etc.) for debugging",
    )
    args = parser.parse_args()
    setup_hooks(port=args.port, all_events=args.all_events)


if __name__ == "__main__":
    main()
