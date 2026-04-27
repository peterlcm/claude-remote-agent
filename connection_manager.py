"""
WebSocket 连接管理器
"""
import json
import asyncio
from datetime import datetime
from typing import Dict, Set, Optional, Callable
from fastapi import WebSocket

from models import SessionLocal, ProxyClient, Task, Agent, TaskEvent
from protocol import (
    Message, TaskResult, TaskProgress,
    build_task_started_message, build_task_progress_message,
    build_task_completed_message, build_task_failed_message,
    build_task_cancelled_message, build_error_message
)


class ConnectionManager:
    """连接管理器"""

    def __init__(self):
        # 客户端连接: client_id -> WebSocket
        self.active_connections: Dict[str, WebSocket] = {}
        # 前端管理界面连接
        self.frontend_connections: Set[WebSocket] = set()
        # 任务回调: task_id -> callback
        self.task_callbacks: Dict[str, Callable] = {}

    async def connect_client(self, client_id: str, websocket: WebSocket):
        """客户端连接"""
        self.active_connections[client_id] = websocket

        # 更新数据库状态
        db = SessionLocal()
        try:
            client = db.query(ProxyClient).filter(ProxyClient.id == client_id).first()
            if client:
                client.is_online = True
                client.last_connected_at = datetime.utcnow()
                client.last_heartbeat_at = datetime.utcnow()
                db.commit()
                print(f"✅ 客户端已连接: {client_id} ({client.name})")
        finally:
            db.close()

        # 通知前端
        await self.broadcast_to_frontend({
            "type": "client_connected",
            "client_id": client_id
        })

    async def disconnect_client(self, client_id: str):
        """客户端断开连接"""
        if client_id in self.active_connections:
            del self.active_connections[client_id]

        # 更新数据库状态
        db = SessionLocal()
        try:
            client = db.query(ProxyClient).filter(ProxyClient.id == client_id).first()
            if client:
                client.is_online = False
                db.commit()
                print(f"❌ 客户端已断开: {client_id} ({client.name})")
        finally:
            db.close()

        # 通知前端
        await self.broadcast_to_frontend({
            "type": "client_disconnected",
            "client_id": client_id
        })

    async def connect_frontend(self, websocket: WebSocket):
        """前端管理界面连接"""
        self.frontend_connections.add(websocket)
        print(f"📱 前端管理界面已连接 (当前: {len(self.frontend_connections)})")

    async def disconnect_frontend(self, websocket: WebSocket):
        """前端管理界面断开连接"""
        if websocket in self.frontend_connections:
            self.frontend_connections.remove(websocket)
            print(f"📱 前端管理界面已断开 (当前: {len(self.frontend_connections)})")

    async def broadcast_to_frontend(self, message: dict):
        """广播消息到所有前端"""
        for connection in list(self.frontend_connections):
            try:
                await connection.send_json(message)
            except:
                # 移除失效连接
                self.frontend_connections.discard(connection)

    async def send_to_client(self, client_id: str, message: Message) -> bool:
        """发送消息到指定客户端"""
        if client_id not in self.active_connections:
            return False

        try:
            await self.active_connections[client_id].send_text(message.to_json())
            return True
        except Exception as e:
            print(f"❌ 发送消息到客户端失败: {client_id}, 错误: {e}")
            return False

    async def handle_client_message(self, client_id: str, raw_message):
        """处理客户端消息"""
        try:
            # 支持 dict 或 str 类型的消息
            if isinstance(raw_message, dict):
                message = Message(**raw_message)
            else:
                message = Message.from_json(raw_message)
            db = SessionLocal()

            try:
                # 心跳消息
                if message.type == "heartbeat":
                    client = db.query(ProxyClient).filter(ProxyClient.id == client_id).first()
                    if client:
                        client.last_heartbeat_at = datetime.utcnow()
                        db.commit()

                # 注册确认
                elif message.type == "agent.register_ack":
                    print(f"✅ 客户端 {client_id} 注册确认")

                # 任务开始
                elif message.type == "task.started":
                    task_id = message.id
                    task = db.query(Task).filter(Task.id == task_id).first()
                    if task:
                        task.status = "running"
                        task.started_at = datetime.utcnow()
                        db.commit()
                        await self.broadcast_to_frontend({
                            "type": "task_started",
                            "task_id": task_id,
                            "client_id": client_id
                        })

                # 任务进度（高层状态）
                elif message.type == "task.progress":
                    task_id = message.id
                    await self.broadcast_to_frontend({
                        "type": "task_progress",
                        "task_id": task_id,
                        "progress": message.payload
                    })

                # 任务事件（流式细粒度）
                elif message.type == "task.event":
                    task_id = message.id or message.payload.get("task_id")
                    seq = int(message.payload.get("seq") or 0)
                    event_type = message.payload.get("event_type") or "unknown"
                    inner_payload = message.payload.get("payload") or {}
                    event_ts = message.payload.get("timestamp")

                    try:
                        record = TaskEvent(
                            task_id=task_id,
                            seq=seq,
                            event_type=event_type,
                            event_ts=event_ts,
                        )
                        record.set_payload(inner_payload)
                        db.add(record)
                        db.commit()
                    except Exception as exc:
                        # 唯一约束冲突等情况下回滚后忽略，避免影响后续广播
                        db.rollback()
                        print(f"⚠️ 任务事件入库失败 task={task_id} seq={seq}: {exc}")

                    await self.broadcast_to_frontend({
                        "type": "task_event",
                        "task_id": task_id,
                        "client_id": client_id,
                        "seq": seq,
                        "event_type": event_type,
                        "payload": inner_payload,
                        "timestamp": event_ts,
                    })

                # 任务完成
                elif message.type == "task.completed":
                    task_id = message.id
                    task = db.query(Task).filter(Task.id == task_id).first()
                    if task:
                        payload = message.payload
                        task.status = "completed"
                        task.result = payload.get("result", "")
                        task.duration_ms = payload.get("duration_ms", 0)
                        task.num_turns = payload.get("num_turns", 0)
                        task.session_id = payload.get("session_id")
                        task.completed_at = datetime.utcnow()

                        if payload.get("structured_output"):
                            task.set_structured_output(payload["structured_output"])
                        if payload.get("usage"):
                            task.set_usage(payload["usage"])

                        db.commit()

                        await self.broadcast_to_frontend({
                            "type": "task_completed",
                            "task_id": task_id,
                            "client_id": client_id,
                            "result": payload
                        })

                        print(f"✅ 任务完成: {task_id}, 耗时: {task.duration_ms}ms")

                # 任务失败
                elif message.type == "task.failed":
                    task_id = message.id
                    task = db.query(Task).filter(Task.id == task_id).first()
                    if task:
                        task.status = "failed"
                        task.error_message = message.payload.get("error", "")
                        task.error_code = message.payload.get("error_code", "")
                        task.result = message.payload.get("partial_output", "")
                        task.completed_at = datetime.utcnow()
                        db.commit()

                        await self.broadcast_to_frontend({
                            "type": "task_failed",
                            "task_id": task_id,
                            "client_id": client_id,
                            "error": message.payload
                        })

                        print(f"❌ 任务失败: {task_id}, 错误: {task.error_message}")

                # 任务取消
                elif message.type == "task.cancelled":
                    task_id = message.id
                    task = db.query(Task).filter(Task.id == task_id).first()
                    if task:
                        task.status = "cancelled"
                        task.completed_at = datetime.utcnow()
                        db.commit()

                        await self.broadcast_to_frontend({
                            "type": "task_cancelled",
                            "task_id": task_id,
                            "client_id": client_id
                        })

                # 用户确认请求
                elif message.type == "user_confirmation.request":
                    # 直接广播给所有前端，让前端显示确认对话框
                    await self.broadcast_to_frontend({
                        "type": "user_confirmation_request",
                        "client_id": client_id,
                        "request": message.payload
                    })

            finally:
                db.close()

        except Exception as e:
            print(f"❌ 处理客户端消息失败: {e}")

    async def send_task_to_client(self, client_id: str, task_id: str, prompt: str,
                                   context: str = None, options: dict = None) -> bool:
        """发送任务到客户端"""
        if client_id not in self.active_connections:
            print(f"❌ 客户端不在线: {client_id}")
            return False

        try:
            message = Message(
                type="task.execute",
                id=task_id,
                payload={
                    "prompt": prompt,
                    "context": context or "",
                    "options": options or {}
                }
            )
            await self.active_connections[client_id].send_text(message.to_json())
            print(f"📤 任务已发送到客户端: {task_id} -> {client_id}")
            return True
        except Exception as e:
            print(f"❌ 发送任务失败: {e}")
            return False

    def is_client_online(self, client_id: str) -> bool:
        """检查客户端是否在线"""
        return client_id in self.active_connections

    def get_online_clients(self) -> list:
        """获取所有在线客户端ID"""
        return list(self.active_connections.keys())


# 全局连接管理器
manager = ConnectionManager()
