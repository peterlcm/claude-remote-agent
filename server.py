#!/usr/bin/env python3
"""
Claude Remote Agent - Web 管理界面服务端
FastAPI + WebSocket 服务
"""
import os
import uuid
import json
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from models import (
    Base, engine, SessionLocal,
    ProxyClient, Agent, Task, TaskLog, TaskEvent, Conversation,
    create_default_client, get_or_create_default_agent,
    apply_pending_migrations,
)
from connection_manager import ConnectionManager
from protocol import (
    Message, MessageType, TaskPayload, TaskResult,
    UserConfirmationResponse, build_user_confirmation_response
)

# 初始化日志
import logging
logger = logging.getLogger("server")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)

# 创建数据库表
Base.metadata.create_all(bind=engine)
# 给老库补齐 Conversation 相关列（SQLite 不会自动 ALTER TABLE）
apply_pending_migrations()

# 确保默认数据存在
db = SessionLocal()
try:
    default_client = create_default_client(db)
    default_agent = get_or_create_default_agent(db, default_client.id)
finally:
    db.close()

# 初始化应用
app = FastAPI(title="Claude Remote Agent Manager", version="1.0.0")

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 连接管理器
manager = ConnectionManager()

# 静态文件目录
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# 数据库依赖
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 请求模型
class ClientCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None


class AgentCreateRequest(BaseModel):
    name: str
    client_id: Optional[str] = None
    description: Optional[str] = None
    default_model: str = "sonnet"
    max_turns: int = 10


class TaskCreateRequest(BaseModel):
    agent_id: str
    prompt: str
    context: Optional[str] = None
    model: Optional[str] = None
    max_turns: Optional[int] = None


class UserConfirmationRespondRequest(BaseModel):
    """用户确认回应请求"""
    client_id: str
    request_id: str
    task_id: str
    value: str


class ConversationCreateRequest(BaseModel):
    """创建对话请求"""
    agent_id: str
    prompt: str
    context: Optional[str] = None
    workdir: Optional[str] = None
    title: Optional[str] = None
    model: Optional[str] = None
    max_turns: Optional[int] = None


class ConversationMessageRequest(BaseModel):
    """对话内追问请求"""
    prompt: str
    context: Optional[str] = None
    model: Optional[str] = None
    max_turns: Optional[int] = None


# ============ 首页路由 ============
@app.get("/", response_class=HTMLResponse)
async def get_index():
    """管理界面首页"""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Claude Remote Agent</title>
        <meta charset="utf-8">
        <style>
            body { font-family: system-ui; max-width: 800px; margin: 50px auto; padding: 20px; }
            h1 { color: #333; }
            .status { padding: 20px; background: #f0f9ff; border-radius: 8px; }
        </style>
    </head>
    <body>
        <h1>🚀 Claude Remote Agent</h1>
        <div class="status">
            <p><strong>服务状态:</strong> ✅ 运行中</p>
            <p><strong>API 文档:</strong> <a href="/docs">/docs</a></p>
            <p><strong>前端界面开发中...</strong></p>
        </div>
    </body>
    </html>
    """)


# ============ WebSocket 路由 ============
@app.websocket("/ws/client")
async def websocket_client(websocket: WebSocket):
    """客户端 WebSocket 连接端点"""
    await websocket.accept()
    
    client_id = None
    try:
        # 等待注册消息
        data = await websocket.receive_json()
        msg = Message(**data)
        
        if msg.type != MessageType.AGENT_REGISTER:
            await websocket.send_json({
                "type": MessageType.ERROR,
                "payload": {"message": "First message must be register"}
            })
            return
        
        client_id = msg.payload.get("client_id")
        if not client_id:
            await websocket.send_json({
                "type": MessageType.ERROR,
                "payload": {"message": "Missing client_id"}
            })
            return
        
        # 验证或创建客户端
        db = SessionLocal()
        try:
            client = db.query(ProxyClient).filter(ProxyClient.id == client_id).first()
            if not client:
                # 自动创建新客户端
                client_name = msg.payload.get("name", f"客户端 {client_id[:8]}")
                client_description = msg.payload.get("description", "通过 WebSocket 自动注册的客户端")
                client = ProxyClient(
                    id=client_id,
                    name=client_name,
                    client_key=client_id,
                    description=client_description,
                    is_online=True,
                    last_connected_at=datetime.now()
                )
                db.add(client)
                db.commit()
                logger.info(f"自动创建新客户端: {client_id}")
        finally:
            db.close()
        
        # 注册连接
        await manager.connect_client(client_id, websocket)
        
        # 发送注册确认
        await websocket.send_json({
            "type": MessageType.AGENT_REGISTER_ACK,
            "payload": {"status": "ok", "client_id": client_id}
        })
        
        # 消息循环
        while True:
            data = await websocket.receive_json()
            await manager.handle_client_message(client_id, data)
            
    except WebSocketDisconnect:
        if client_id:
            await manager.disconnect_client(client_id)
    except Exception as e:
        logger.error(f"Client WebSocket error: {e}")
        if client_id:
            await manager.disconnect_client(client_id)


@app.websocket("/ws/frontend")
async def websocket_frontend(websocket: WebSocket):
    """前端管理界面 WebSocket 连接端点"""
    await websocket.accept()
    await manager.connect_frontend(websocket)
    try:
        while True:
            # 接收前端消息（如任务取消等）
            data = await websocket.receive_json()
            # TODO: 处理前端操作消息
            pass
    except WebSocketDisconnect:
        await manager.disconnect_frontend(websocket)


# ============ REST API - 客户端管理 ============
@app.get("/api/clients")
def list_clients(db=Depends(get_db)):
    """获取所有客户端列表"""
    clients = db.query(ProxyClient).all()
    return {
        "data": [
            {
                "id": c.id,
                "name": c.name,
                "description": c.description,
                "is_online": c.is_online,
                "last_connected_at": c.last_connected_at.isoformat() if c.last_connected_at else None,
                "version": c.version,
                "claude_version": c.claude_version,
                "created_at": c.created_at.isoformat()
            }
            for c in clients
        ]
    }


@app.post("/api/clients")
def create_client(req: ClientCreateRequest, db=Depends(get_db)):
    """创建新客户端"""
    client_id = str(uuid.uuid4())[:12]
    client_key = str(uuid.uuid4())
    
    client = ProxyClient(
        id=client_id,
        name=req.name,
        client_key=client_key,
        description=req.description
    )
    db.add(client)
    db.commit()
    db.refresh(client)
    
    return {
        "data": {
            "id": client.id,
            "name": client.name,
            "client_key": client_key,
            "description": client.description
        }
    }


@app.get("/api/clients/{client_id}")
def get_client(client_id: str, db=Depends(get_db)):
    """获取单个客户端信息"""
    client = db.query(ProxyClient).filter(ProxyClient.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    return {
        "data": {
            "id": client.id,
            "name": client.name,
            "description": client.description,
            "is_online": client.is_online,
            "capabilities": client.get_capabilities(),
            "last_connected_at": client.last_connected_at.isoformat() if client.last_connected_at else None,
            "version": client.version,
            "claude_version": client.claude_version,
            "created_at": client.created_at.isoformat(),
            "agent_count": len(client.agents),
            "agents": [
                {
                    "id": a.id,
                    "name": a.name,
                    "default_model": a.default_model,
                    "is_active": a.is_active,
                    "created_at": a.created_at.isoformat()
                }
                for a in client.agents
            ]
        }
    }


@app.delete("/api/clients/{client_id}")
def delete_client(client_id: str, db=Depends(get_db)):
    """删除客户端"""
    client = db.query(ProxyClient).filter(ProxyClient.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    # 检查是否有正在运行的任务
    running_tasks = db.query(Task).filter(
        Task.client_id == client_id,
        Task.status.in_(["pending", "queued", "running"])
    ).count()
    if running_tasks > 0:
        raise HTTPException(status_code=400, detail="Cannot delete client with running tasks")

    db.delete(client)
    db.commit()
    return {"data": {"success": True, "message": "Client deleted"}}


class ClientUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


@app.put("/api/clients/{client_id}")
def update_client(client_id: str, req: ClientUpdateRequest, db=Depends(get_db)):
    """更新客户端信息"""
    client = db.query(ProxyClient).filter(ProxyClient.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    if req.name is not None:
        client.name = req.name
    if req.description is not None:
        client.description = req.description
    client.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(client)

    return {
        "data": {
            "id": client.id,
            "name": client.name,
            "description": client.description,
            "is_online": client.is_online
        }
    }


# ============ REST API - Agent 管理 ============
@app.get("/api/agents")
def list_agents(db=Depends(get_db)):
    """获取所有 Agent 列表"""
    agents = db.query(Agent).all()
    return {
        "data": [
            {
                "id": a.id,
                "name": a.name,
                "description": a.description,
                "client_id": a.client_id,
                "default_model": a.default_model,
                "max_turns": a.max_turns,
                "is_active": a.is_active,
                "created_at": a.created_at.isoformat()
            }
            for a in agents
        ]
    }


@app.post("/api/agents")
def create_agent(req: AgentCreateRequest, db=Depends(get_db)):
    """创建新 Agent"""
    agent_id = str(uuid.uuid4())[:12]

    # 如果没有指定 client_id，使用默认客户端
    if not req.client_id:
        default_client = db.query(ProxyClient).first()
        if default_client:
            req.client_id = default_client.id

    agent = Agent(
        id=agent_id,
        name=req.name,
        description=req.description,
        client_id=req.client_id,
        default_model=req.default_model,
        max_turns=req.max_turns,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    
    return {"data": {"id": agent.id, "name": agent.name}}


@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str, db=Depends(get_db)):
    """删除 Agent"""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # 检查是否有正在运行的任务
    running_tasks = db.query(Task).filter(
        Task.agent_id == agent_id,
        Task.status.in_(["pending", "queued", "running"])
    ).count()
    if running_tasks > 0:
        raise HTTPException(status_code=400, detail="Cannot delete agent with running tasks")

    db.delete(agent)
    db.commit()
    return {"data": {"success": True, "message": "Agent deleted"}}


class AgentUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    default_model: Optional[str] = None
    max_turns: Optional[int] = None
    client_id: Optional[str] = None
    is_active: Optional[bool] = None


@app.put("/api/agents/{agent_id}")
def update_agent(agent_id: str, req: AgentUpdateRequest, db=Depends(get_db)):
    """更新 Agent 信息"""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if req.name is not None:
        agent.name = req.name
    if req.description is not None:
        agent.description = req.description
    if req.default_model is not None:
        agent.default_model = req.default_model
    if req.max_turns is not None:
        agent.max_turns = req.max_turns
    if req.client_id is not None:
        agent.client_id = req.client_id
    if req.is_active is not None:
        agent.is_active = req.is_active

    agent.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(agent)

    return {
        "data": {
            "id": agent.id,
            "name": agent.name,
            "description": agent.description,
            "client_id": agent.client_id,
            "default_model": agent.default_model,
            "max_turns": agent.max_turns,
            "is_active": agent.is_active
        }
    }


class BindClientRequest(BaseModel):
    client_id: Optional[str] = None


@app.post("/api/agents/{agent_id}/bind-client")
def bind_agent_to_client(agent_id: str, req: BindClientRequest, db=Depends(get_db)):
    """绑定 Agent 到客户端"""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # 如果提供了 client_id，验证客户端存在
    if req.client_id:
        client = db.query(ProxyClient).filter(ProxyClient.id == req.client_id).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")

    agent.client_id = req.client_id
    agent.updated_at = datetime.utcnow()
    db.commit()

    return {
        "data": {
            "success": True,
            "agent_id": agent.id,
            "client_id": agent.client_id,
            "message": "Agent绑定成功"
        }
    }


@app.get("/api/agents/monitor")
def get_agents_monitor(db=Depends(get_db)):
    """获取所有 Agent 的实时监控数据"""
    agents = db.query(Agent).all()
    result = []
    for a in agents:
        client_online = a.client_id in manager.active_connections if a.client_id else False
        client_info = manager.client_status.get(a.client_id, {}) if a.client_id else {}
        client_name = ""
        if a.client_id:
            c = db.query(ProxyClient).filter(ProxyClient.id == a.client_id).first()
            client_name = c.name if c else a.client_id

        running_tasks = db.query(Task).filter(
            Task.agent_id == a.id,
            Task.status == "running"
        ).all()

        # 以 Agent 维度判断状态：有 running 任务则 busy，否则 idle
        agent_active_count = len(running_tasks)
        agent_status = "busy" if agent_active_count > 0 else ("idle" if client_online else "offline")

        result.append({
            "agent_id": a.id,
            "agent_name": a.name,
            "description": a.description,
            "client_id": a.client_id,
            "client_name": client_name,
            "client_online": client_online,
            "client_status": agent_status,
            "active_tasks": agent_active_count,
            "default_model": a.default_model,
            "max_turns": a.max_turns,
            "is_active": a.is_active,
            "running_tasks": [
                {
                    "task_id": t.id,
                    "prompt": t.prompt[:80],
                    "started_at": t.started_at.isoformat() if t.started_at else None,
                }
                for t in running_tasks
            ],
            "recent_completed_tasks": [
                {
                    "task_id": t.id,
                    "prompt": t.prompt[:80],
                    "status": t.status,
                    "duration_ms": t.duration_ms,
                    "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                }
                for t in db.query(Task).filter(
                    Task.agent_id == a.id,
                    Task.status.in_(["completed", "failed", "cancelled"])
                ).order_by(Task.completed_at.desc()).limit(5).all()
            ],
            "last_heartbeat_at": client_info.get("last_heartbeat_at").isoformat()
                if client_info.get("last_heartbeat_at") else None,
        })
    return {"data": result}


@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str, db=Depends(get_db)):
    """获取 Agent 详情"""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # 按创建时间倒序排列任务
    tasks = db.query(Task).filter(Task.agent_id == agent_id).order_by(Task.created_at.desc()).limit(50).all()

    return {
        "data": {
            "id": agent.id,
            "name": agent.name,
            "description": agent.description,
            "client_id": agent.client_id,
            "client_name": agent.client.name if agent.client else None,
            "default_model": agent.default_model,
            "max_turns": agent.max_turns,
            "is_active": agent.is_active,
            "timeout": agent.timeout,
            "created_at": agent.created_at.isoformat(),
            "updated_at": agent.updated_at.isoformat() if agent.updated_at else None,
            "task_count": len(agent.tasks),
            "tasks": [
                {
                    "id": t.id,
                    "prompt": t.prompt[:100] + ("..." if len(t.prompt) > 100 else ""),
                    "status": t.status,
                    "duration_ms": t.duration_ms,
                    "num_turns": t.num_turns,
                    "created_at": t.created_at.isoformat(),
                    "completed_at": t.completed_at.isoformat() if t.completed_at else None
                }
                for t in tasks
            ]
        }
    }


# ============ REST API - 任务管理 ============
@app.get("/api/tasks")
def list_tasks(limit: int = 50, db=Depends(get_db)):
    """获取任务列表"""
    tasks = db.query(Task).order_by(Task.created_at.desc()).limit(limit).all()
    return {
        "data": [
            {
                "id": t.id,
                "agent_id": t.agent_id,
                "client_id": t.client_id,
                "conversation_id": t.conversation_id,
                "turn_index": t.turn_index,
                "prompt": t.prompt[:100] + "..." if len(t.prompt) > 100 else t.prompt,
                "status": t.status,
                "result": t.result[:100] + "..." if t.result and len(t.result) > 100 else t.result,
                "duration_ms": t.duration_ms,
                "created_at": t.created_at.isoformat(),
                "completed_at": t.completed_at.isoformat() if t.completed_at else None
            }
            for t in tasks
        ]
    }


@app.post("/api/tasks")
async def create_task(req: TaskCreateRequest, db=Depends(get_db)):
    """创建并执行新任务"""
    agent = db.query(Agent).filter(Agent.id == req.agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    # 检查客户端是否在线
    if not agent.client or not agent.client.is_online:
        raise HTTPException(status_code=400, detail="Client is offline")
    
    # 创建任务
    task_id = str(uuid.uuid4())
    task = Task(
        id=task_id,
        agent_id=agent.id,
        client_id=agent.client_id,
        prompt=req.prompt,
        context=req.context or "",
        options=json.dumps({
            "model": req.model or agent.default_model,
            "max_turns": req.max_turns if req.max_turns is not None else agent.max_turns,
        }),
        status="pending"
    )
    db.add(task)
    db.commit()

    # 发送任务到客户端
    options = {
        "model": req.model or agent.default_model,
        "max_turns": req.max_turns or agent.max_turns,
    }

    success = await manager.send_task_to_client(
        client_id=agent.client_id,
        task_id=task_id,
        prompt=req.prompt,
        context=req.context,
        options=options
    )
    
    if success:
        task.status = "queued"
        db.commit()
        return {"data": {"task_id": task_id, "status": "queued"}}
    else:
        task.status = "failed"
        db.commit()
        raise HTTPException(status_code=500, detail="Failed to send task to client")


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str, db=Depends(get_db)):
    """获取任务详情"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    logs = db.query(TaskLog).filter(TaskLog.task_id == task_id).order_by(TaskLog.created_at).all()
    
    return {
        "data": {
            "id": task.id,
            "agent_id": task.agent_id,
            "conversation_id": task.conversation_id,
            "turn_index": task.turn_index,
            "parent_session_id": task.parent_session_id,
            "session_id": task.session_id,
            "prompt": task.prompt,
            "context": task.context,
            "status": task.status,
            "result": task.result,
            "usage": task.get_usage(),
            "duration_ms": task.duration_ms,
            "num_turns": task.num_turns,
            "error_message": task.error_message,
            "created_at": task.created_at.isoformat(),
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "logs": [
                {
                    "type": l.log_type,
                    "message": l.message,
                    "created_at": l.created_at.isoformat()
                }
                for l in logs
            ]
        }
    }


@app.get("/api/tasks/{task_id}/events")
def list_task_events(task_id: str, since_seq: int = 0, limit: int = 2000,
                      db=Depends(get_db)):
    """获取指定任务的事件（升序，按 seq）。

    用于前端首屏拉历史 + 断线重连时按 since_seq 拉补齐。
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if limit <= 0 or limit > 5000:
        limit = 2000

    query = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task_id)
        .filter(TaskEvent.seq > since_seq)
        .order_by(TaskEvent.seq.asc())
        .limit(limit)
    )
    rows = query.all()
    last_seq = rows[-1].seq if rows else since_seq
    return {
        "data": {
            "task_id": task_id,
            "since_seq": since_seq,
            "last_seq": last_seq,
            "count": len(rows),
            "events": [
                {
                    "seq": e.seq,
                    "event_type": e.event_type,
                    "payload": e.get_payload(),
                    "timestamp": e.event_ts,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in rows
            ],
        }
    }


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, db=Depends(get_db)):
    """取消任务"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status in ["completed", "failed", "cancelled"]:
        raise HTTPException(status_code=400, detail="Task already finished")

    # 发送取消消息到客户端
    if task.client_id and manager.is_client_online(task.client_id):
        try:
            message = Message(
                type="task.cancel",
                id=task_id,
                payload={}
            )
            await manager.send_to_client(task.client_id, message)
            task.status = "cancelling"
            db.commit()
            logger.info(f"Task cancellation requested: {task_id}")
            return {"data": {"success": True, "message": "Cancellation requested"}}
        except Exception as e:
            logger.error(f"Failed to send cancellation for task {task_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed to send cancellation")
    else:
        # 客户端不在线，直接标记为取消
        task.status = "cancelled"
        task.completed_at = datetime.utcnow()
        db.commit()
        logger.info(f"Task cancelled (client offline): {task_id}")
        return {"data": {"success": True, "message": "Task cancelled (client offline)"}}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str, db=Depends(get_db)):
    """删除任务"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # 检查任务是否正在运行
    if task.status in ["pending", "queued", "running"]:
        # 如果正在运行，尝试取消
        if task.client_id and manager.is_client_online(task.client_id):
            try:
                message = Message(
                    type="task.cancel",
                    id=task_id,
                    payload={}
                )
                manager.active_connections[task.client_id].send_text(message.to_json())
            except:
                pass
        task.status = "cancelled"
        db.commit()

    db.delete(task)
    db.commit()
    return {"data": {"success": True, "message": "Task deleted"}}


# ============ REST API - 统计信息 ============
@app.get("/api/stats")
def get_stats(db=Depends(get_db)):
    """获取系统统计信息"""
    total_clients = db.query(ProxyClient).count()
    online_clients = db.query(ProxyClient).filter(ProxyClient.is_online == True).count()
    total_agents = db.query(Agent).count()
    total_tasks = db.query(Task).count()
    completed_tasks = db.query(Task).filter(Task.status == "completed").count()
    failed_tasks = db.query(Task).filter(Task.status == "failed").count()

    return {
        "data": {
            "clients": {"total": total_clients, "online": online_clients},
            "agents": {"total": total_agents},
            "tasks": {
                "total": total_tasks,
                "completed": completed_tasks,
                "failed": failed_tasks,
                "pending": total_tasks - completed_tasks - failed_tasks
            },
            "active_connections": len(manager.active_connections),
            "frontend_connections": len(manager.frontend_connections)
        }
    }



# ============ REST API - 对话管理 ============
def _build_task_options(agent: Agent,
                        model: Optional[str],
                        max_turns: Optional[int]) -> dict:
    """根据 Agent 默认值与显式覆盖构造发往客户端的 options dict。"""
    return {
        "model": model or agent.default_model,
        "max_turns": max_turns if max_turns is not None else agent.max_turns,
    }


def _summarize_title(prompt: str, max_len: int = 60) -> str:
    """从首条 prompt 中截取标题。"""
    text = (prompt or "").strip().replace("\n", " ")
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _serialize_conversation(conv: Conversation, include_tasks: bool = False) -> dict:
    """统一的 Conversation 序列化（避免 to_dict 漏字段）。"""
    data = {
        "id": conv.id,
        "agent_id": conv.agent_id,
        "client_id": conv.client_id,
        "workdir": conv.workdir,
        "claude_session_id": conv.claude_session_id,
        "last_session_id": conv.last_session_id,
        "title": conv.title,
        "status": conv.status,
        "turn_count": conv.turn_count,
        "last_prompt_at": conv.last_prompt_at.isoformat() if conv.last_prompt_at else None,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
    }
    if include_tasks:
        data["tasks"] = [
            {
                "id": t.id,
                "turn_index": t.turn_index,
                "prompt": t.prompt,
                "context": t.context,
                "status": t.status,
                "result": t.result,
                "error_message": t.error_message,
                "duration_ms": t.duration_ms,
                "num_turns": t.num_turns,
                "session_id": t.session_id,
                "parent_session_id": t.parent_session_id,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            }
            for t in sorted(conv.tasks, key=lambda x: (x.turn_index or 0, x.created_at))
        ]
    return data


@app.post("/api/conversations")
async def create_conversation(req: ConversationCreateRequest, db=Depends(get_db)):
    """创建一次新对话（开第一轮 Task）。"""
    agent = db.query(Agent).filter(Agent.id == req.agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not agent.client or not agent.client.is_online:
        raise HTTPException(status_code=400, detail="Client is offline")
    if not manager.is_client_online(agent.client_id):
        raise HTTPException(status_code=400, detail="Client is offline")

    workdir = (req.workdir or ".").strip() or "."
    conv_id = str(uuid.uuid4())
    now = datetime.utcnow()
    conv = Conversation(
        id=conv_id,
        agent_id=agent.id,
        client_id=agent.client_id,
        workdir=workdir,
        title=req.title or _summarize_title(req.prompt),
        status="active",
        turn_count=0,
        last_prompt_at=now,
    )
    db.add(conv)

    options = _build_task_options(agent, req.model, req.max_turns)

    task_id = str(uuid.uuid4())
    task = Task(
        id=task_id,
        agent_id=agent.id,
        client_id=agent.client_id,
        conversation_id=conv_id,
        turn_index=1,
        parent_session_id=None,
        prompt=req.prompt,
        context=req.context or "",
        workdir=workdir,
        options=json.dumps(options),
        status="pending",
    )
    db.add(task)
    db.commit()

    success = await manager.send_task_to_client(
        client_id=agent.client_id,
        task_id=task_id,
        prompt=req.prompt,
        context=req.context,
        options=options,
        workdir=workdir,
    )
    if success:
        task.status = "queued"
        db.commit()
        # 通知前端：新对话已就绪
        await manager.broadcast_to_frontend({
            "type": "conversation_created",
            "conversation_id": conv_id,
            "task_id": task_id,
        })
        return {"data": {
            "conversation_id": conv_id,
            "task_id": task_id,
            "status": "queued",
        }}
    task.status = "failed"
    conv.status = "lost_session"
    db.commit()
    raise HTTPException(status_code=500, detail="Failed to send task to client")


@app.get("/api/conversations")
def list_conversations(agent_id: Optional[str] = None,
                       status_filter: Optional[str] = None,
                       limit: int = 50,
                       db=Depends(get_db)):
    """列出对话。"""
    if limit <= 0 or limit > 500:
        limit = 50
    query = db.query(Conversation)
    if agent_id:
        query = query.filter(Conversation.agent_id == agent_id)
    if status_filter:
        query = query.filter(Conversation.status == status_filter)
    rows = (
        query.order_by(Conversation.last_prompt_at.desc().nullslast(),
                       Conversation.created_at.desc())
        .limit(limit)
        .all()
    )
    return {"data": [_serialize_conversation(c) for c in rows]}


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, db=Depends(get_db)):
    """对话详情，包含所有轮次。"""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    payload = _serialize_conversation(conv, include_tasks=True)
    payload["client_online"] = manager.is_client_online(conv.client_id)
    return {"data": payload}


@app.post("/api/conversations/{conversation_id}/messages")
async def append_conversation_message(conversation_id: str,
                                      req: ConversationMessageRequest,
                                      db=Depends(get_db)):
    """在已有对话上追问，复用 Claude 的 --resume 延续上下文。"""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.status != "active":
        raise HTTPException(status_code=400,
                            detail=f"Conversation is {conv.status}, cannot continue")
    if not conv.claude_session_id:
        raise HTTPException(status_code=400,
                            detail="First turn has not produced a session yet, please wait")
    if not manager.is_client_online(conv.client_id):
        raise HTTPException(status_code=400, detail="Bound client is offline")

    pending = db.query(Task).filter(
        Task.conversation_id == conversation_id,
        Task.status.in_(["pending", "queued", "running", "cancelling"]),
    ).count()
    if pending > 0:
        raise HTTPException(status_code=409,
                            detail="Previous turn is still running, please wait")

    agent = conv.agent
    if not agent:
        raise HTTPException(status_code=500, detail="Conversation agent missing")

    options = _build_task_options(agent, req.model, req.max_turns)
    options["session_id"] = conv.claude_session_id

    last_turn = db.query(Task).filter(Task.conversation_id == conversation_id) \
        .order_by(Task.turn_index.desc()).first()
    next_turn = (last_turn.turn_index if last_turn else 0) + 1

    task_id = str(uuid.uuid4())
    task = Task(
        id=task_id,
        agent_id=agent.id,
        client_id=conv.client_id,
        conversation_id=conversation_id,
        turn_index=next_turn,
        parent_session_id=conv.claude_session_id,
        prompt=req.prompt,
        context=req.context or "",
        workdir=conv.workdir,
        options=json.dumps(options),
        status="pending",
    )
    db.add(task)
    conv.last_prompt_at = datetime.utcnow()
    db.commit()

    success = await manager.send_task_to_client(
        client_id=conv.client_id,
        task_id=task_id,
        prompt=req.prompt,
        context=req.context,
        options=options,
        workdir=conv.workdir,
    )
    if not success:
        task.status = "failed"
        db.commit()
        raise HTTPException(status_code=500, detail="Failed to dispatch task to client")

    task.status = "queued"
    db.commit()
    await manager.broadcast_to_frontend({
        "type": "conversation_message_queued",
        "conversation_id": conversation_id,
        "task_id": task_id,
        "turn_index": next_turn,
    })
    return {"data": {
        "conversation_id": conversation_id,
        "task_id": task_id,
        "turn_index": next_turn,
        "status": "queued",
    }}


@app.post("/api/conversations/{conversation_id}/archive")
def archive_conversation(conversation_id: str, db=Depends(get_db)):
    """归档对话（不再可追问）。"""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv.status = "archived"
    conv.updated_at = datetime.utcnow()
    db.commit()
    return {"data": {"id": conv.id, "status": conv.status}}


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, db=Depends(get_db)):
    """删除对话（级联删除其下 Task 与事件）。"""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    pending = db.query(Task).filter(
        Task.conversation_id == conversation_id,
        Task.status.in_(["pending", "queued", "running", "cancelling"]),
    ).count()
    if pending > 0:
        raise HTTPException(status_code=400,
                            detail="Cannot delete conversation while a turn is running")

    # 先把任务事件清掉，再删 Task / Conversation
    task_ids = [t.id for t in conv.tasks]
    if task_ids:
        db.query(TaskEvent).filter(TaskEvent.task_id.in_(task_ids)).delete(synchronize_session=False)
        db.query(TaskLog).filter(TaskLog.task_id.in_(task_ids)).delete(synchronize_session=False)

    db.delete(conv)
    db.commit()
    return {"data": {"success": True, "id": conversation_id}}


# ============ REST API - 用户确认 ============
@app.post("/api/user-confirmation/respond")
async def user_confirmation_respond(req: UserConfirmationRespondRequest):
    """提交用户确认回应，转发给对应的客户端"""
    if req.client_id not in manager.active_connections:
        raise HTTPException(status_code=400, detail="Client is not online")

    # 构建回应消息并发送给客户端
    response = UserConfirmationResponse(
        request_id=req.request_id,
        task_id=req.task_id,
        value=req.value
    )

    message = build_user_confirmation_response(response)
    success = await manager.send_to_client(req.client_id, message)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to send response to client")

    return {
        "data": {
            "success": True,
            "message": "Response sent to client"
        }
    }


if __name__ == "__main__":
    import uvicorn
    print("🚀 Starting Claude Remote Agent Server...")
    print(f"📊 Dashboard: http://localhost:8000")
    print(f"📚 API Docs: http://localhost:8000/docs")
    uvicorn.run(app, host="0.0.0.0", port=8000)
