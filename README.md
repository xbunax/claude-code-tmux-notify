# agent-tmux-notify

[**中文**](README.zh.md) | English

Monitor Claude Code CLI instances inside tmux, automatically popping up an interactive TUI window when user input is needed.

## Showcase

### Permission Request


<p align="center">
  <img src="showcase/permission.gif" alt="Permission request demo" width="800">
</p>

### Plan Approval


<p align="center">
  <img src="showcase/plan.gif" alt="Plan approval demo" width="800">
</p>

## Features

- **Auto Discovery**: Scans all tmux panes on startup, binding to running Claude Code processes (PID)
- **Hook-Driven Core**: Receives `PreToolUse`, `PermissionRequest`, `Notification` and other events pushed by Claude Code hooks, directly triggering popups and sending user decisions back to Claude Code
- **Buffer-Assisted Parsing**: Reads recent tmux buffer lines only as auxiliary context to recover option lists and current selection
- **Popup Interaction**: When input is needed, opens a curses TUI next to the current pane, displaying context, questions, and options
- **Plan File Reading**: In plan approval scenarios, automatically reads plan files from `~/.claude/plans/` and renders the full Plan in the popup
- **Idle Notification**: Pops up a notification when Claude Code is idle waiting for input, with one-key focus to the corresponding pane
- **Rich Markdown Rendering**: Popup context is rendered with the `rich` library, supporting syntax highlighting
- **Configurable Parse Rules**: Parse rules (regex + keywords) for permission/plan prompts are customizable in the config file
- **Focus Jump**: In the popup, you can jump directly to the corresponding pane
- **macOS Service**: Provides launchd service configuration, supporting startup on boot and background operation

## Installation

### Homebrew (via tap)

```bash
brew tap xbunax/tap
brew install xbunax/tap/agent-tmux-notify
brew services start xbunax/tap/agent-tmux-notify
```

### From Source

Requires Python 3.13+ and [uv](https://github.com/astral-sh/uv).

```bash
git clone <repo>
cd agent-tmux-notify
```

### One-Click Install (Source Install)

Use `service.sh` to install the CLI tool, deploy configuration files, and register the launchd service:

```bash
./service.sh install
```

This will:
1. Install `agent-tmux-notify` as a global CLI command via `uv tool install`
2. Copy `config.toml.default` to `~/.config/agent-tmux-notify/config.toml` (with option to overwrite or diff if exists)
3. Register a macOS LaunchAgent, which can be managed via `service.sh` later

### Manual Install

```bash
uv sync                    # dev mode only
uv tool install -e .       # install as global CLI
```

### Configure Claude Code Hooks (Recommended)

Write hook settings into `~/.claude/settings.json` with one command, enabling Claude Code to push events proactively:

```bash
agent-tmux-notify --setup-hooks
```

This registers HTTP hooks for `PreToolUse`, `PermissionRequest`, `Notification`, and `Stop` events, pointing to `127.0.0.1:19836`. Existing hook configurations are not overwritten.

## Usage

```
agent-tmux-notify [options]

Options:
  --discovery-interval FLOAT  Pane discovery scan interval in seconds (default 30)
  --config PATH               Config file path (default ~/.config/agent-tmux-notify/config.toml)
  --hook-port INT             Hook server port (default 19836)
  --no-hook-server            Disable the Hook HTTP server
  --setup-hooks               Configure Claude Code hooks and exit
  --dump-hook-payloads        Write raw hook payloads to JSONL (debugging)
  --dump-path PATH            Hook payload dump file path (default /tmp/claude-code-hook-payloads.jsonl)
  -v, --verbose               Enable debug logging
```

Enable verbose logging:

```bash
agent-tmux-notify -v
```

### Service Management

For Homebrew installs, manage the launchd service via `brew services`:

```bash
brew services start xbunax/tap/agent-tmux-notify
brew services stop xbunax/tap/agent-tmux-notify
brew services restart xbunax/tap/agent-tmux-notify
brew services list
```

Log paths:
- `/opt/homebrew/var/log/agent-tmux-notify.log`
- `/opt/homebrew/var/log/agent-tmux-notify.error.log`

If installed from source with `service.sh`, you can still use:

```bash
./service.sh start
./service.sh stop
./service.sh restart
./service.sh status
./service.sh logs
./service.sh uninstall
```

## Popup Controls

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move selection |
| `Enter` | Confirm selection |
| `1`–`9` | Quick select by number (matches Claude Code option numbering) |
| `Ctrl-G` | Edit Plan file (plan scenario only) |
| `Esc` | Cancel, close popup |

Two special options are always appended at the end of the option list:
- **[Focus on this pane]**: Switch tmux focus to the corresponding Claude Code pane
- **[Custom input...]**: Enter text input mode to send arbitrary content (not shown in idle scenario)

## Configuration

The config file is located at `~/.config/agent-tmux-notify/config.toml`. All fields are optional; built-in defaults are used when omitted. See `config.toml.default` for the default template.

### Global Settings

```toml
# Number of buffer capture lines
buffer_lines = 25
```

### Popup Position

```toml
[popup]
width  = "25%"
height = "25%"
x      = "R"    # R = right, C = center, or pixel value
y      = "0"    # 0 = top, or pixel value
```

### Hook Server

```toml
[hook_server]
enabled        = true         # Whether to enable the hook HTTP server
host           = "127.0.0.1"
port           = 19836
ttl            = 30.0         # Hook event TTL in seconds
dump_payloads  = false        # Whether to dump raw hook payloads to file (debugging)
dump_path      = "/tmp/claude-code-hook-payloads.jsonl"
```

### Parse Rule Configuration

Each scenario supports `patterns` (regex) and `keywords` (substring match); either match will mark the prompt line for option parsing.

```toml
[parse_rules.permission]
patterns = ['Do you want to .*\?', 'Would you like to .*\?']
keywords = ["Do you want to", "Would you like to"]

[parse_rules.plan]
keywords = ["approve this plan", "approve the plan"]
```

> Use single quotes (literal strings) for regex in TOML to avoid backslash escaping issues.

### Trigger Scenarios

| Scenario | Trigger | Popup Behavior |
|----------|---------|----------------|
| `permission` | Claude Code requests execution permission | Shows tool call content, waits for confirmation (decisions sent back directly in hook mode) |
| `plan` | Claude Code submits a Plan for approval | Shows Plan summary, supports Ctrl-G to edit and re-submit for approval |
| `idle` | Claude Code idle, waiting for user input | Notification popup, one-key focus to the corresponding pane |

## Architecture

```
agent_tmux_notify/
  cli.py               CLI entry point, parses arguments, starts Monitor
  monitor.py           Main loop: discovery, hook handling, popup scheduling
  detector.py          Buffer-assisted option parsing, TriggerEvent/HookData, plan file reading
  hook_server.py       HTTP hook server: HookServer, HookStore, PaneCorrelator
  setup_hooks.py       CLI tool: configures hooks in ~/.claude/settings.json
  config.py            TOML config loading: PopupConfig, HookServerConfig, ParseRulesConfig
  popup.py             curses TUI popup, rich markdown rendering
  tmux.py              tmux CLI async wrapper
main.py                Legacy entry point (compatibility), same as cli.py
config.toml.default    Default configuration template
service.sh             Optional macOS launchd service helper for source installs
```

### Data Flow

```
Claude Code ──[hook HTTP POST]──► HookServer (localhost:19836)
    │                                  │
    │                          ┌───────┴───────┐
    │                          ▼               ▼
    │                    PreToolUse        PermissionRequest / Notification
    │                    (cache context)   (direct trigger popup)
    │                          │               │
    │                          └───────┬───────┘
    │                                  ▼
    │                        PaneCorrelator (CWD matches hook → pane)
    │                                  │
    └──[tmux buffer (aux)]──► detector.extract_options_from_buffer()
                                       │
                                       ▼
                            TriggerEvent → popup.py (rich markdown)
                                       │
                                       ▼
                            Decision sent back to HookServer (JSON response)
```

### TriggerEvent JSON Structure

The popup receives data via a temporary JSON file:

```json
{
  "project_name": "my-project",
  "session_name": "main",
  "pane_id": "main:0.1",
  "scenario": "permission",
  "content": ["Bash command", "  ls -la ~/.claude/"],
  "question": "Do you want to proceed?",
  "options": ["Yes", "No, and tell Claude what to do differently"],
  "selected_index": 0,
  "hook_data": {
    "session_id": "abc123",
    "hook_event_name": "PermissionRequest",
    "tool_name": "Bash",
    "tool_input": {"command": "ls -la ~/.claude/"},
    "cwd": "/Users/user/my-project"
  }
}
```

`hook_data` is `null` when the hook server is disabled or no hook event matched.

## Friendly Links

- https://linux.do
