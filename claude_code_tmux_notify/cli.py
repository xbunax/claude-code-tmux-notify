"""Claude Code tmux monitor — entry point."""

import argparse
import asyncio
import logging
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor Claude Code CLI instances in tmux panes"
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.5,
        help="Seconds between state polls (default: 1.5)",
    )
    parser.add_argument(
        "--discovery-interval",
        type=float,
        default=30.0,
        help="Seconds between pane discovery scans (default: 30)",
    )
    parser.add_argument(
        "--debounce",
        type=float,
        default=3.0,
        help="Seconds to confirm NEEDS_INPUT before popup (default: 3)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config TOML file (default: ~/.config/claude-code-tmux-notify/config.toml)",
    )
    parser.add_argument(
        "--hook-port",
        type=int,
        default=None,
        help="Hook server port (default: 19836, from config)",
    )
    parser.add_argument(
        "--no-hook-server",
        action="store_true",
        help="Disable the hook HTTP server",
    )
    parser.add_argument(
        "--setup-hooks",
        action="store_true",
        help="Configure Claude Code hooks in ~/.claude/settings.json and exit",
    )
    args = parser.parse_args()

    # --setup-hooks: configure and exit
    if args.setup_hooks:
        from claude_code_tmux_notify.setup_hooks import setup_hooks
        port = args.hook_port or 19836
        setup_hooks(port=port)
        sys.exit(0)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    from claude_code_tmux_notify.monitor import Monitor

    monitor = Monitor(
        poll_interval=args.poll_interval,
        discovery_interval=args.discovery_interval,
        debounce_seconds=args.debounce,
        config_path=args.config,
    )

    # Apply CLI overrides to hook server config
    if args.no_hook_server:
        monitor.config.hook_server.enabled = False
        monitor.hook_server = None
    if args.hook_port is not None and monitor.hook_server is not None:
        monitor.config.hook_server.port = args.hook_port
        monitor.hook_server.port = args.hook_port

    try:
        asyncio.run(monitor.run())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
