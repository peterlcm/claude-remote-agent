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
    ProxyClient, Agent, Task, TaskLog,
    create_default_client, get_or_create_default_agent
)
from connection_manager import ConnectionManager
from protocol import Message, MessageType, TaskPayload, TaskResult

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
    effort: str = "medium"


class TaskCreateRequest(BaseModel):
    agent_id: str
    prompt: str
    context: Optional[str] = None
    model: Optional[str] = None
    max_turns: Optional[int] = None
    effort: Optional[str] = None


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


# Validation helpers
def validate_effort(effort: Optional[str]) -> str:
    """Validate reasoning effort value, return default if invalid"""
    valid_efforts = ["low", "medium", "high"]
    if effort and effort in valid_efforts:
        return effort
    return "medium"


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
                "effort": a.effort,
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
        effort=validate_effort(req.effort)
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
    effort: Optional[str] = None
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
    if req.effort is not None:
        agent.effort = validate_effort(req.effort)
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
            "effort": agent.effort,
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
            "effort": agent.effort,
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
            "max_turns": req.max_turns or agent.max_turns,
            "effort": req.effort or agent.effort
        }),
        status="pending"
    )
    db.add(task)
    db.commit()
    
    # 发送任务到客户端
    # 注意：只在 effort 有效时传递，避免 API 错误
    effort_value = req.effort or agent.effort
    valid_efforts = ["low", "medium", "high"]
    
    options = {
        "model": req.model or agent.default_model,
        "max_turns": req.max_turns or agent.max_turns,
    }
    if effort_value and effort_value in valid_efforts:
        options["effort"] = effort_value
    
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


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, db=Depends(get_db)):
    """取消任务"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    if task.status in ["completed", "failed", "cancelled"]:
        raise HTTPException(status_code=400, detail="Task already finished")
    
    # 发送取消消息到客户端
    if task.client and manager.is_client_online(task.client_id):
        try:
            message = Message(
                type="task.cancel",
                id=task_id,
                payload={}
            )
            await manager.active_connections[task.client_id].send_text(message.to_json())
            task.status = "cancelling"
            db.commit()
            return {"data": {"success": True, "message": "Cancellation requested"}}
        except Exception as e:
            logger.error(f"Failed to send cancellation: {e}")
            raise HTTPException(status_code=500, detail="Failed to send cancellation")
    else:
        # 客户端不在线，直接标记为取消
        task.status = "cancelled"
        task.completed_at = datetime.utcnow()
        db.commit()
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


if __name__ == "__main__":
    import uvicorn
    print("🚀 Starting Claude Remote Agent Server...")
    print(f"📊 Dashboard: http://localhost:8000")
    print(f"📚 API Docs: http://localhost:8000/docs")
    uvicorn.run(app, host="0.0.0.0", port=8000)
