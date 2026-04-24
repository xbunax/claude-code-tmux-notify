#!/bin/bash
set -euo pipefail

LABEL="com.agent-tmux-notify"
PLIST_NAME="${LABEL}.plist"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_DST="$HOME/Library/LaunchAgents/${PLIST_NAME}"
LOG_DIR="$HOME/Library/Logs/agent-tmux-notify"
CLI_BIN="$HOME/.local/bin/agent-tmux-notify"

usage() {
    echo "Usage: $0 {install|uninstall|start|stop|restart|status|logs}"
    exit 1
}

install_config() {
    local CONFIG_DIR="$HOME/.config/agent-tmux-notify"
    local CONFIG_SRC="${SCRIPT_DIR}/config.toml.default"
    local CONFIG_DST="${CONFIG_DIR}/config.toml"
    mkdir -p "$CONFIG_DIR"
    if [[ -f "$CONFIG_DST" ]]; then
        echo "Config file already exists: $CONFIG_DST"
        read -rp "Overwrite? [y/N/d(diff)] " choice
        case "$choice" in
            y|Y) cp "$CONFIG_SRC" "$CONFIG_DST"; echo "Config overwritten." ;;
            d|D) diff "$CONFIG_DST" "$CONFIG_SRC" || true ;;
            *)   echo "Skipped." ;;
        esac
    else
        cp "$CONFIG_SRC" "$CONFIG_DST"
        echo "Config installed to $CONFIG_DST"
    fi
}

cmd_install() {
    echo "Installing CLI tool via uv..."
    uv tool install --editable "$SCRIPT_DIR" --force
    mkdir -p "$LOG_DIR"
    cat > "$PLIST_DST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${CLI_BIN}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>LANG</key>
        <string>en_US.UTF-8</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/stderr.log</string>
    <key>ThrottleInterval</key>
    <integer>5</integer>
</dict>
</plist>
EOF
    echo "Plist installed to $PLIST_DST"
    install_config
    echo "Run '$0 start' to start the service."
}

cmd_uninstall() {
    cmd_stop
    rm -f "$PLIST_DST"
    echo "Plist removed."
}

cmd_start() {
    if launchctl list "$LABEL" &>/dev/null; then
        echo "Service is already running."
        return
    fi
    launchctl load -w "$PLIST_DST"
    echo "Service started."
}

cmd_stop() {
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl unload "$PLIST_DST"
        echo "Service stopped."
    else
        echo "Service is not running."
    fi
}

cmd_restart() {
    cmd_stop
    cmd_start
}

cmd_status() {
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl list "$LABEL"
    else
        echo "Service is not loaded."
    fi
}

cmd_logs() {
    tail -f "$LOG_DIR/stdout.log" "$LOG_DIR/stderr.log"
}

[[ $# -lt 1 ]] && usage

case "$1" in
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_restart ;;
    status)    cmd_status ;;
    logs)      cmd_logs ;;
    *)         usage ;;
esac
