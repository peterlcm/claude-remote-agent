# Claude Remote Agent

云端长连接代理系统 - 通过本机连接远程云服务，接收云端下发指令并调用Claude Code执行，将结果返回云端。

## 系统架构

```
┌─────────────┐         WebSocket         ┌─────────────────────┐
│  云端服务   │ ◄───────────────────────► │  Claude Remote Agent │
└─────────────┘                           └─────────────────────┘
                                                  │
                                                  ▼
                                          ┌───────────────┐
                                          │  Claude Code  │
                                          └───────────────┘
```

## 功能特性

- ✅ **WebSocket长连接** - 自动重连、心跳保活
- ✅ **云端指令接收** - 支持同步和异步任务
- ✅ **Claude Code封装** - 完整的CLI参数支持
- ✅ **结构化输出** - JSON格式结果上报
- ✅ **任务队列** - 并发任务管理
- ✅ **日志记录** - 完整的执行日志
- ✅ **健康检查** - 实时状态上报
- ✅ **任务取消** - 支持中断运行中任务

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env` 并配置：

```bash
# 云端WebSocket服务地址
SERVER_URL=ws://your-cloud-server.com/ws/agent

# 客户端认证Token
AGENT_TOKEN=your-agent-token

# Claude配置
CLAUDE_MODEL=sonnet
CLAUDE_MAX_TURNS=10
CLAUDE_EFFORT=medium

# 客户端配置
CLIENT_ID=agent-001
HEARTBEAT_INTERVAL=30
```

### 3. 启动代理

```bash
python main.py
```

## 消息协议

### 云端 → 客户端 (下行消息)

#### 1. 执行任务指令

```json
{
  "type": "task.execute",
  "id": "task-uuid-123",
  "payload": {
    "prompt": "分析这段代码并找出bug",
    "context": "附加上下文信息...",
    "workdir": "/path/to/project",
    "options": {
      "model": "sonnet",
      "max_turns": 10,
      "effort": "medium",
      "allowed_tools": ["Read", "Edit", "Bash"],
      "output_format": "json"
    }
  }
}
```

#### 2. 取消任务

```json
{
  "type": "task.cancel",
  "id": "task-uuid-123"
}
```

#### 3. 心跳响应

```json
{
  "type": "heartbeat.ack"
}
```

### 客户端 → 云端 (上行消息)

#### 1. 连接注册

```json
{
  "type": "agent.register",
  "payload": {
    "client_id": "agent-001",
    "version": "1.0.0",
    "capabilities": {
      "claude_version": "2.1.118",
      "supported_tools": ["Read", "Edit", "Write", "Bash", "WebSearch"]
    }
  }
}
```

#### 2. 心跳上报

```json
{
  "type": "heartbeat",
  "payload": {
    "timestamp": 1712345678,
    "status": "idle",
    "active_tasks": 0
  }
}
```

#### 3. 任务开始

```json
{
  "type": "task.started",
  "id": "task-uuid-123",
  "payload": {
    "started_at": 1712345678
  }
}
```

#### 4. 任务进度

```json
{
  "type": "task.progress",
  "id": "task-uuid-123",
  "payload": {
    "turn": 3,
    "max_turns": 10,
    "status": "thinking"
  }
}
```

#### 5. 任务完成

```json
{
  "type": "task.completed",
  "id": "task-uuid-123",
  "payload": {
    "success": true,
    "result": "分析结果...",
    "structured_output": {...},
    "usage": {
      "input_tokens": 1200,
      "output_tokens": 800,
      "total_cost_usd": 0.015
    },
    "duration_ms": 45000,
    "num_turns": 5
  }
}
```

#### 6. 任务失败

```json
{
  "type": "task.failed",
  "id": "task-uuid-123",
  "payload": {
    "error": "Timeout exceeded",
    "error_code": "TIMEOUT",
    "partial_output": "..."
  }
}
```

## 高级配置

### Claude Code选项

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `model` | 模型名称 | sonnet |
| `max_turns` | 最大迭代次数 | 10 |
| `effort` | 推理深度 | medium |
| `allowed_tools` | 允许的工具列表 | null |
| `output_format` | 输出格式 | text |
| `timeout` | 超时时间(秒) | 300 |

### 支持的工具

- `Read` - 文件读取
- `Edit` - 文件编辑
- `Write` - 文件创建
- `Bash` - Shell命令
- `WebSearch` - 网页搜索
- `WebFetch` - 网页内容获取

## 测试

### 启动本地模拟服务端

```bash
python mock_server.py
```

### 启动客户端连接本地服务

```bash
python main.py --server ws://localhost:8765
```

### 使用测试脚本发送任务

```bash
python test_client.py "帮我写一个Python脚本"
```

## 故障排查

### Claude Code命令失败

1. 检查Claude Code版本: `claude --version`
2. 验证认证状态: `claude auth status`
3. 测试基础功能: `claude -p "Hello" --max-turns 1`

### WebSocket连接失败

1. 检查网络连接
2. 验证服务端地址和端口
3. 检查认证Token

### 任务执行超时

1. 增加 `timeout` 参数
2. 减少 `max_turns`
3. 使用 `effort: low` 降低推理深度

## 许可证

MIT License
