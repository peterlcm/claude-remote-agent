# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Claude Remote Agent 是一个云端长连接代理系统，通过本机连接远程云服务，接收云端下发指令并调用 Claude Code 执行，将结果返回云端。

项目包含两个主要组件：
1. **客户端** (`main.py`): 通过 WebSocket 连接云端服务，接收并执行 Claude Code 任务
2. **服务端** (`server.py`): 基于 FastAPI 的管理服务器，使用 SQLite 持久化存储，提供 Web UI 和 REST API

## 常用命令

### 环境搭建
```bash
./setup.sh                     # 一键环境配置（创建虚拟环境、安装依赖）
pip install -r requirements.txt  # 手动安装依赖
```

### 启动客户端
```bash
source venv/bin/activate       # 激活虚拟环境
python main.py                 # 使用 .env 配置启动客户端
python main.py --debug         # 启用调试日志启动
python main.py --server ws://localhost:8765 --token your-token  # 自定义服务器
python main.py --help          # 查看所有选项
```

### 启动服务端（管理界面）
```bash
source venv/bin/activate
python server.py               # 在 8000 端口启动 FastAPI 服务器
./start_server.sh              # 使用 nohup 在后台启动服务
# 访问界面: http://localhost:8000
# API 文档: http://localhost:8000/docs
```

### 测试
```bash
python verify_system.py        # 完整系统验证（Claude + 协议 + WebSocket）
python test_claude.py          # 测试 Claude Code CLI 基础功能
python mock_server.py          # 启动本地模拟云端服务用于测试
python test_client.py "提示词"  # 向本地运行的客户端发送测试任务
python integration_test.py     # 完整端到端集成测试
python test_simple_client.py   # 简单客户端连接测试
```

## 代码架构

### 客户端侧（远程代理）

| 文件 | 职责 |
|------|------|
| `main.py` | 入口点、参数解析、信号处理、创建 `ClaudeRemoteAgent` |
| `agent_client.py` | WebSocket 核心客户端：连接管理、自动重连、心跳、消息路由、任务分发 |
| `claude_runner.py` | `ClaudeRunner` 封装 Claude Code CLI 执行；`ClaudeRunnerManager` 使用信号量管理并发任务 |
| `protocol.py` | 所有消息类型的 Pydantic 模型 + 构建消息的辅助函数 |
| `config.py` | 通过环境变量/.env 配置，使用 pydantic-settings |
| `log_config.py` | 日志配置（控制台 + 文件） |
| `permission_mcp.py` | MCP 权限服务器：被 Claude CLI 作为 `--permission-prompt-tool` 启动，通过本地 IPC 将权限请求转发给主代理进程，实现远程审批 |

### 服务端侧（管理API）

| 文件 | 职责 |
|------|------|
| `server.py` | FastAPI 应用：WebSocket 端点、客户端/代理/任务的 REST API |
| `connection_manager.py` | 管理活跃客户端连接、广播任务、处理入站消息 |
| `models.py` | SQLAlchemy ORM 模型：`ProxyClient`、`Agent`、`Task`、`TaskLog` |
| `mock_server.py` | 带交互式控制台的模拟云端服务器，用于本地开发测试 |

### 消息流程

1. **云端 → 代理**: 收到 `task.execute` 消息 → 客户端创建异步任务 → 发送 `task.started` → 调用 `claude` CLI → 通过 `task.progress` 推送进度 → 发送 `task.completed` 或 `task.failed`
2. **代理 → 云端**: 持续发送 `heartbeat` 消息报告空闲/忙碌状态和活跃任务数
3. **取消**: 云端发送 `task.cancel` → 客户端杀死进程 → 回复 `task.cancelled` 确认

### 关键设计点

- **Asyncio**: 全程基于 asyncio，更好地支持多任务并发
- **自动重连**: 断开连接后客户端自动尝试重连
- **并发控制**: `ClaudeRunnerManager` 使用信号量限制并发任务数（默认为 3）
- **权限审批模型**: 支持两种模式：
  - `auto`: 自动批准所有工具调用（无人值守）
  - `prompt`: 通过 `permission_mcp.py` MCP 服务器将权限请求转发回云端，由远程操作员审批
- **Windows 兼容**: `permission_mcp.py` 使用后台线程读取 stdin 解决 Windows ProactorEventLoop 不支持管道的问题
- **无会话持久化**: 使用 `--no-session-persistence` 避免累积会话数据

## 环境配置

复制 `.env.example` 为 `.env` 并配置：

- `SERVER_URL`: 云端 WebSocket 地址
- `AGENT_TOKEN`: 认证 Token
- `CLIENT_ID`: 唯一客户端标识符
- `CLAUDE_MODEL`: 默认模型 (sonnet/opus/haiku)
- `CLAUDE_MAX_TURNS`: 默认最大迭代次数
- `HEARTBEAT_INTERVAL`: 心跳间隔（秒）

## 使用本地模拟服务测试

1. 终端 1: `python mock_server.py`
2. 终端 2: `python main.py`
3. 终端 3: `python test_client.py "你的提示词"`
4. 或者在 mock_server 控制台使用交互命令：
   - `list` - 查看已连接客户端
   - `send '提示词'` - 发送任务
   - `result <任务ID>` - 查看任务结果

## 支持的 Claude Code 工具

- `Read` - 文件读取
- `Edit` - 文件编辑
- `Write` - 文件创建
- `Bash` - Shell 命令执行
- `WebSearch` - 网页搜索
- `WebFetch` - 网页内容获取
