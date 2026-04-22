# claude-code-tmux-notify

在 tmux 中监控 Claude Code CLI 实例，当需要用户输入时自动弹出交互窗口。

## 功能

- **自动发现**：启动时扫描所有 tmux pane，绑定正在运行的 Claude Code 进程（PID）
- **持续轮询**：每 1.5 秒检测一次 pane 状态，识别权限申请、Plan 审批、任务完成等场景
- **弹窗交互**：检测到需要输入时，在当前 pane 旁弹出 curses TUI，展示上下文、问题和选项
- **Hook 集成**：接收 Claude Code hooks 推送的结构化数据（tool_name、tool_input），与 buffer 检测合并，提供更丰富的弹窗内容
- **双确认模式**：可配置为 hook + buffer 同时匹配才弹窗，减少误触发
- **Rich Markdown 渲染**：弹窗上下文使用 rich 库渲染 markdown，支持语法高亮
- **可配置触发词**：每个场景的触发条件（正则 + 关键词）均可在配置文件中自定义
- **聚焦跳转**：弹窗中可选择直接跳转到对应 pane

## 安装

需要 Python 3.13+ 和 [uv](https://github.com/astral-sh/uv)。

```bash
git clone <repo>
cd claude-code-tmux-notify
uv sync
```

### 配置 Claude Code Hooks（推荐）

一键将 hook 配置写入 `~/.claude/settings.json`，让 Claude Code 主动推送事件：

```bash
uv run python main.py --setup-hooks
```

这会为 `PreToolUse`、`PermissionRequest`、`Notification`、`Stop` 四个事件注册 HTTP hook，指向本地 `127.0.0.1:19836`。已有的 hooks 配置不会被覆盖。

不配置 hooks 也能正常使用（仅依赖 buffer 检测），但弹窗内容会少一些结构化信息。

## 使用

```
uv run python main.py [选项]

选项：
  --poll-interval FLOAT       状态轮询间隔，秒（默认 1.5）
  --discovery-interval FLOAT  pane 发现扫描间隔，秒（默认 30）
  --debounce FLOAT            确认 NEEDS_INPUT 前的等待时间，秒（默认 3）
  --config PATH               配置文件路径（默认 ~/.config/claude-code-tmux-notify/config.toml）
  --hook-port INT             Hook 服务器端口（默认 19836）
  --no-hook-server            禁用 Hook HTTP 服务器
  --setup-hooks               配置 Claude Code hooks 后退出
  -v, --verbose               开启 debug 日志
```

开启详细日志：

```bash
uv run python main.py -v
```

## 弹窗操作

| 按键 | 动作 |
|------|------|
| `↑` / `↓` | 移动选择 |
| `Enter` | 确认选项 |
| `1`–`9` | 数字快选（对应 Claude Code 的选项编号） |
| `Esc` | 取消，关闭弹窗 |

选项列表末尾固定追加两个特殊选项：
- **[聚焦到此 pane]**：切换 tmux 焦点到对应的 Claude Code pane
- **[自定义输入...]**：进入文本输入模式，发送任意内容

## 配置

配置文件位于 `~/.config/claude-code-tmux-notify/config.toml`，所有字段均可选，缺省时使用内置默认值。

### 弹窗位置

```toml
[popup]
width  = "80%"
height = "60%"
x      = "R"    # R = 右侧，C = 居中，或像素值
y      = "0"    # 0 = 顶部，或像素值
```

### Hook 服务器

```toml
[hook_server]
enabled      = true         # 是否启用 hook HTTP 服务器
host         = "127.0.0.1"
port         = 19836
ttl          = 30.0         # hook 事件过期时间，秒
require_hook = false        # true = 双确认模式，hook + buffer 都匹配才弹窗
```

### 触发词配置

每个场景支持 `patterns`（正则）和 `keywords`（子串匹配）两种方式，任意一种命中即触发。

```toml
[triggers.permission]
patterns = ['Do you want to .*\?', 'Would you like to .*\?']
keywords = ["Do you want to", "Would you like to"]

[triggers.plan]
keywords = ["approve this plan", "approve the plan"]

[triggers.completed]
patterns = ['(Brewed|Crunched|Swooped|Drizzled) for\s+']
```

> TOML 中正则建议使用单引号（literal string），避免反斜杠转义问题。

### 触发场景说明

| 场景 | 触发时机 | 弹窗行为 |
|------|----------|----------|
| `permission` | Claude Code 申请执行权限 | 展示工具调用内容，等待确认 |
| `plan` | Claude Code 提交 Plan 等待审批 | 展示 Plan 摘要，等待确认 |
| `completed` | 任务执行完成 | 短暂显示完成通知（2 秒后自动关闭） |

## 架构

```
main.py                CLI 入口，解析参数，启动 Monitor
claude_code_tmux_notify/
  monitor.py           主循环：发现、轮询、hook 合并、弹窗调度
  detector.py          buffer 解析：TriggerMatcher、TriggerEvent、HookData
  hook_server.py       HTTP hook 服务器：HookServer、HookStore、PaneCorrelator
  setup_hooks.py       CLI 工具：配置 ~/.claude/settings.json 中的 hooks
  config.py            TOML 配置加载：PopupConfig、HookServerConfig、TriggersConfig
  popup.py             curses TUI 弹窗，rich markdown 渲染
  responder.py         将弹窗选择转换为 tmux send-keys 发回 Claude Code
  tmux.py              tmux CLI 异步封装
```

### 数据流

```
Claude Code ──[hook HTTP POST]──► HookServer (localhost:19836)
    │                                  │
    │                                  ▼
    │                             HookStore (内存, 30s TTL)
    │                                  │
    │                                  │ ◄── PaneCorrelator (CWD 匹配)
    │                                  │
    ├──[tmux buffer]──► Monitor ──► detector.parse_buffer()
    │                                  │
    │                                  ▼
    │                        合并 hook + buffer → TriggerEvent
    │                                  │
    │                                  ▼
    │                             popup.py (rich markdown)
    │                                  │
    │                                  ▼
    │                             responder.py (tmux send-keys)
```

### TriggerEvent JSON 结构

弹窗通过临时 JSON 文件接收数据：

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

`hook_data` 在 hook 服务器未启用或未匹配到事件时为 `null`。
