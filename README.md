# Claude Remote Agent

云端长连接代理系统：通过本机 Agent 接入云端服务，把云端下发的任务转发给本地 Claude Code CLI 执行，并把执行过程中的**逐条事件**、**工具确认请求**和**最终结果**实时回传到云端，支持 Web 管理界面查看与交互。

支持 **Windows / Linux / macOS** 全平台运行。

---

## 1. 系统架构

```
┌─────────────┐   REST/WS   ┌──────────────────────────────┐   WebSocket   ┌─────────────────────┐
│  浏览器/前端 │ ──────────► │  Server (FastAPI + SQLite)  │ ◄───────────► │  Claude Remote Agent │
└─────────────┘             │  - 任务/客户端/Agent CRUD    │               │  (agent_client)     │
                            │  - 任务事件入库 + 广播        │               └──────────┬──────────┘
                            │  - 用户确认转发              │                          │ 子进程
                            └──────────────────────────────┘                          ▼
                                                                       ┌──────────────────────────┐
                                                                       │   claude (Claude CLI)    │
                                                                       │   --output-format        │
                                                                       │     stream-json          │
                                                                       │   --permission-prompt-   │
                                                                       │     tool mcp__remote_…   │
                                                                       └──────────┬───────────────┘
                                                                                  │ stdio MCP
                                                                                  ▼
                                                                       ┌──────────────────────────┐
                                                                       │ permission_mcp 子进程    │
                                                                       │ ─ JSON-RPC 2.0 over stdio│
                                                                       │ ─ TCP loopback 回 Agent  │
                                                                       └──────────────────────────┘
```

核心数据通路：

1. 前端通过 REST 创建任务 → Server 找到目标 Agent，通过 WebSocket 下发 `task.execute`。
2. Agent 启动 `claude` CLI，以 `--output-format stream-json --include-partial-messages` 逐行解析 NDJSON 事件，并配 `--permission-prompt-tool mcp__remote_agent__approve` 把工具权限请求路由到 `permission_mcp.py`。
3. Agent 维护单调递增的 `seq`，把每条事件以 `task.event` 实时回传到 Server；Server 入库 `task_events` 表并广播到所有前端 WebSocket。
4. 当 Claude 需要使用工具（Bash/Edit/Write 等），CLI 调用 `mcp__remote_agent__approve`，请求经 stdio → permission_mcp → TCP 回到 Agent → 转 `user_confirmation.request` 给前端。
5. 用户在前端点"允许/拒绝" → 经 Server REST → Agent → MCP → CLI，整个回路打通。

---

## 2. 关键特性

- ✅ **跨平台 stream-json 解析**：使用 `asyncio.create_subprocess_exec`，零 PTY 依赖；Windows 自动使用 `WindowsProactorEventLoopPolicy` 并智能解析 npm 安装的 `claude.cmd` 包装脚本。
- ✅ **细粒度事件流**：会话初始化、模型 token 增量、工具入参增量、工具调用 / 结果、API 重试、最终 result 等都作为独立事件回传。
- ✅ **Permission MCP 工具确认**：标准 MCP（JSON-RPC 2.0 over stdio）实现，前端可结构化展示工具名 + 入参 JSON，由人工在线"允许/拒绝"。
- ✅ **事件持久化 + 断线补齐**：所有事件落地 `task_events`（带 `task_id+seq` 唯一约束），前端打开任务详情先 REST 拉历史、再 WS 增量追加；中途断线可按 `since_seq` 回补。
- ✅ **WebSocket 长连接**：客户端自动重连、心跳保活、空闲/忙碌状态上报。
- ✅ **并发任务管理**：`ClaudeRunnerManager` 通过信号量限制并发，可同时执行多个任务，互不阻塞。
- ✅ **完整管理后台**：客户端管理、Agent 配置、任务列表、任务详情（事件流卡片）、系统统计。

---

## 3. 项目结构

```
claude-remote-agent/
├── main.py                # 客户端入口（Asyncio + 跨平台事件循环）
├── agent_client.py        # WebSocket 客户端 + Permission MCP IPC server
├── claude_runner.py       # Claude CLI 子进程 + stream-json 解析 + 跨平台可执行解析
├── permission_mcp.py      # 独立的 stdio MCP server（线程化 stdin 读取，跨平台）
├── protocol.py            # 全部消息 Pydantic 模型与构造函数
├── config.py              # pydantic-settings 配置（CLAUDE_*/AGENT_*/LOG_*）
├── log_config.py          # 控制台 + 文件双通道日志
│
├── server.py              # FastAPI 服务端（WS + REST + 静态资源）
├── connection_manager.py  # 客户端连接管理 + 任务事件入库 + 前端广播
├── models.py              # SQLAlchemy ORM：proxy_clients/agents/tasks/task_events/task_logs
│
├── static/                # 管理后台 (HTML/JS/CSS)
│   ├── index.html
│   ├── app.js
│   └── styles.css
│
├── mock_server.py         # 离线模拟云端，可在控制台直接 list/send/result
├── verify_system.py       # 一键自检（claude + 协议 + WebSocket）
├── test_claude.py         # Claude CLI 基础功能测试
├── test_client.py         # 给已运行客户端发任务
├── integration_test.py    # 端到端集成测试
│
├── requirements.txt
├── .env.example
├── setup.sh               # Linux/macOS 一键环境配置
└── start_server.sh        # 后台启动 server (nohup)
```

---

## 4. 快速开始

### 4.1 环境要求

- Python ≥ 3.10
- Node.js ≥ 18 + 已安装并登录 [Claude Code CLI](https://docs.claude.com/en/docs/claude-code)（命令行可执行 `claude --version`）

### 4.2 安装依赖

```bash
# Linux/macOS
./setup.sh

# 或手动
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 4.3 配置

复制 `.env.example` → `.env` 后按需修改：

```ini
SERVER_URL=ws://localhost:8000/ws/client
AGENT_TOKEN=your-secret-token
CLIENT_ID=default

CLAUDE_MODEL=sonnet
CLAUDE_MAX_TURNS=10
CLAUDE_TIMEOUT=300

# 工具权限策略：default(逐次确认) / acceptEdits / bypassPermissions / plan
CLAUDE_PERMISSION_MODE=default
# 这些工具直接放进 --allowedTools，不弹确认
CLAUDE_AUTO_APPROVE_TOOLS=Read,Glob,Grep

HEARTBEAT_INTERVAL=30
RECONNECT_DELAY=5
LOG_LEVEL=INFO
LOG_FILE=logs/agent.log
```

### 4.4 启动服务端

```bash
python server.py
# 或后台
./start_server.sh
```

启动后访问：

- 管理界面：<http://localhost:8000>
- API 文档：<http://localhost:8000/docs>

### 4.5 启动 Agent（客户端）

```bash
python main.py            # 用 .env 配置
python main.py --debug    # 开启 DEBUG 日志
python main.py --server ws://localhost:8000/ws/client --token your-token
```

Windows PowerShell 同步可运行：

```powershell
python main.py --debug
```

---

## 5. 消息协议

所有消息以 JSON 形式经 WebSocket 收发，统一形如：

```json
{ "type": "<message_type>", "id": "<task_id?>", "payload": { ... }, "timestamp": 1745800000.123 }
```

### 5.1 客户端 ↔ 云端

| 方向 | type | 说明 |
|---|---|---|
| → 云 | `agent.register` | 客户端启动后注册（含 `client_id` / `claude_version` / `supported_tools`）|
| 云 → | `agent.register_ack` | 服务端确认注册 |
| → 云 | `heartbeat` | 周期性心跳（`status=idle/busy`、`active_tasks`）|
| 云 → | `heartbeat.ack` | 心跳响应 |
| 云 → | `task.execute` | 下发任务（`prompt` / `context` / `workdir` / `options`）|
| → 云 | `task.started` | 任务进入运行 |
| → 云 | `task.progress` | 高层状态变更（`turn` / `status=thinking/working/completed/failed`）|
| → 云 | `task.event` | **细粒度事件流**（核心），见 §5.3 |
| → 云 | `task.completed` | 任务正常完成（结果 + usage + 耗时）|
| → 云 | `task.failed` | 任务失败 |
| 云 → | `task.cancel` | 取消任务 |
| → 云 | `task.cancelled` | 取消确认 |
| → 云 | `user_confirmation.request` | 请求人工确认（含工具名 + 入参）|
| 云 → | `user_confirmation.response` | 用户回应（`allow`/`deny`）|
| → 云 | `error` | 通用错误 |

### 5.2 任务下发示例

```json
{
  "type": "task.execute",
  "id": "task-uuid-123",
  "payload": {
    "prompt": "分析当前仓库的代码并指出潜在 bug",
    "context": "可选的附加上下文",
    "workdir": ".",
    "options": {
      "model": "sonnet",
      "max_turns": 10,
      "allowed_tools": ["Read", "Edit", "Bash"],
      "output_format": "text",
      "timeout": 300,
      "continue_last": false,
      "session_id": null
    }
  }
}
```

### 5.3 `task.event` 事件流

`payload` 形如：

```json
{
  "task_id": "...",
  "seq": 42,
  "event_type": "tool_use",
  "payload": { /* 与 event_type 对应 */ },
  "timestamp": 1745800000.456
}
```

`seq` 由 Agent 单调递增，前端按序渲染；服务端入库 `task_events` 表（`UNIQUE(task_id, seq)`），可按 `?since_seq=` 回补丢失片段。

常见 `event_type`：

| 事件类型 | 含义 |
|---|---|
| `session_init` | 会话初始化（`session_id` / `model` / `permission_mode` / `tools` / `mcp_servers` / `cwd`）|
| `message_start` | Claude 流式响应开始 |
| `content_block_start` | 一个内容块（text 或 tool_use 或 thinking）开始 |
| `text_delta` | 模型文本 token 增量 |
| `thinking_delta` | 思考链增量（流式追加显示） |
| `tool_input_delta` | 工具入参 JSON 流式片段 |
| `assistant_message` | 一轮完整的助手消息（含 tool_use）|
| `tool_result` | 工具执行结果 |
| `api_retry` | Anthropic API 重试事件 |
| `rate_limit` | 速率限制信息 |
| `message_stop` / `content_block_stop` / `message_delta` | 流式结构边界 |
| `result` | 最终汇总（`subtype` / `result` / `duration_ms` / `usage` / `total_cost_usd` / ...）|
| `stderr` | 子进程 stderr 行（一般为 warning）|
| `stdout_text` | 非 JSON 的 stdout 行（兼容兜底）|

### 5.4 用户确认请求

```json
{
  "type": "user_confirmation.request",
  "id": "task-uuid-123",
  "payload": {
    "request_id": "perm-9c0e...",
    "task_id": "task-uuid-123",
    "title": "工具确认: Bash",
    "message": "Claude 正在请求使用以下工具，请确认是否允许。",
    "prompt": "Bash\n{\n  \"command\": \"echo hello\"\n}",
    "options": [
      {"label": "允许", "value": "allow"},
      {"label": "拒绝", "value": "deny"}
    ],
    "timeout": 600,
    "source": "permission_mcp",
    "tool_name": "Bash",
    "tool_input": {"command": "echo hello"},
    "tool_use_id": "call_xxx"
  }
}
```

回应：

```json
{
  "type": "user_confirmation.response",
  "id": "task-uuid-123",
  "payload": { "request_id": "perm-9c0e...", "task_id": "task-uuid-123", "value": "allow" }
}
```

---

## 6. 服务端 REST API（节选）

| 方法 & 路径 | 说明 |
|---|---|
| `GET  /api/clients` | 客户端列表 |
| `POST /api/clients` | 创建客户端 |
| `PUT/DELETE /api/clients/{id}` | 更新/删除 |
| `GET/POST/PUT/DELETE /api/agents[...]` | Agent CRUD |
| `POST /api/agents/{id}/bind-client` | 绑定 Agent 到客户端 |
| `GET  /api/tasks?limit=N` | 最近任务列表 |
| `POST /api/tasks` | 创建并下发任务 |
| `GET  /api/tasks/{id}` | 任务详情 |
| `GET  /api/tasks/{id}/events?since_seq=0&limit=2000` | **任务事件流（断线补齐）** |
| `POST /api/tasks/{id}/cancel` | 取消任务 |
| `DELETE /api/tasks/{id}` | 删除任务 |
| `GET  /api/stats` | 系统统计 |
| `POST /api/user-confirmation/respond` | 提交用户确认 |
| `WS   /ws/client` | Agent 连入点 |
| `WS   /ws/frontend` | 前端实时事件订阅 |

完整 OpenAPI 见 `/docs`。

---

## 7. 数据库

SQLite 文件位于 `data/claude_agent.db`，由 SQLAlchemy 在启动时自动建表。主要表：

| 表 | 说明 |
|---|---|
| `proxy_clients` | 远程代理客户端（在线状态、心跳、能力）|
| `agents` | Agent 配置（默认模型、max_turns、allowed_tools、绑定的 client）|
| `tasks` | 任务（prompt、状态、结果、usage、耗时、session_id）|
| `task_events` | 任务流式事件（task_id + seq 唯一）|
| `task_logs` | 任务文本日志 |

`data/` 目录还会存放 Agent 启动时为 Permission MCP 生成的 `mcp_config_<client_id>.json`，无需手工维护。

---

## 8. 测试 & 自检

```bash
# 完整自检（Claude CLI + 协议 + WebSocket 各跑一遍）
python verify_system.py

# 测 Claude CLI 基础功能
python test_claude.py

# 端到端集成测试
python integration_test.py

# 离线模拟云端
python mock_server.py        # 终端 1
python main.py --debug       # 终端 2
python test_client.py "你的提示词"   # 终端 3
```

`mock_server.py` 控制台支持：

- `list` 查看在线客户端
- `send '提示词'` 下发任务
- `result <task_id>` 查看结果

---

## 9. Windows 使用要点（已踩过的坑）

| 现象 | 原因 / 解决 |
|---|---|
| `FileNotFoundError [WinError 2]` 启不起 `claude` | npm 装的 `claude` 是 PowerShell 包装，`asyncio.create_subprocess_exec` 调不了 `.ps1`。`claude_runner._resolve_executable` 已自动改用同目录 `claude.cmd / .bat / .exe` |
| `Warning: no stdin data received in 3s, proceeding without it` | prompt 已通过 `-p` 传，CLI 等 stdin。`stdin=DEVNULL` 已修复，每条任务节省 3s |
| `OSError [WinError 6] 句柄无效` 后 `Available MCP tools: none` | Windows ProactorEventLoop 不支持 `connect_read_pipe(stdin)`。`permission_mcp.py` 改为后台线程读 stdin + `asyncio.Queue`，跨平台一致 |
| `8000 端口被占用` | 多次启动遗留进程，使用 `Get-WmiObject Win32_Process` 定位真实 PID 后 `Stop-Process` |

---

## 10. 故障排查

**Claude CLI 失败**

```bash
claude --version
claude auth status
claude -p "Hello" --max-turns 1
```

**WebSocket 无法连上**

- 检查 `SERVER_URL` 是否包含 `/ws/client`
- 确认 server 已启动且 8000 端口可达
- 确认 `AGENT_TOKEN` 与服务端一致

**MCP 握手未完成（`mcp_servers.status=pending`）**

- 必为 `permission_mcp` 进程启动失败：检查 `python -m permission_mcp` 在终端能否独立启动
- 确认 `data/mcp_config_<client_id>.json` 存在且 `command` 指向当前 venv 的 `python`

**任务超时**

- 调高 `options.timeout`
- 调低 `options.max_turns`

---

## 11. 许可证

MIT License
