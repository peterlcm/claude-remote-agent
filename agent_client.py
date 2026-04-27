"""
WebSocket长连接客户端
"""
import asyncio
import json
import logging
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from config import config
from protocol import (
    Message, MessageType, TaskOptions, TaskPayload, TaskProgress,
    build_register_message, build_heartbeat_message,
    build_task_started_message, build_task_progress_message,
    build_task_completed_message, build_task_failed_message,
    build_task_cancelled_message, build_error_message
)
from claude_runner import ClaudeRunnerManager

logger = logging.getLogger(__name__)


class ClaudeRemoteAgent:
    """Claude远程代理客户端"""

    def __init__(self, server_url: Optional[str] = None,
                 agent_token: Optional[str] = None,
                 client_id: Optional[str] = None):
        self.server_url = server_url or config.agent.server_url
        self.agent_token = agent_token or config.agent.agent_token
        self.client_id = client_id or config.agent.client_id

        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.runner_manager = ClaudeRunnerManager(max_concurrent=3)

        self._connected = False
        self._registered = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._reconnect_attempts = 0
        self._shutdown = False

    async def connect(self) -> bool:
        """连接到云端服务"""
        try:
            logger.info(f"Connecting to {self.server_url}...")

            # 构建WebSocket连接头
            headers = {}
            if self.agent_token:
                headers["Authorization"] = f"Bearer {self.agent_token}"
            headers["X-Client-ID"] = self.client_id
            headers["X-Client-Version"] = config.VERSION

            # 兼容不同版本的 websockets 库
            try:
                self.websocket = await websockets.connect(
                    self.server_url,
                    additional_headers=headers,
                    ping_interval=30,
                    ping_timeout=10
                )
            except TypeError:
                self.websocket = await websockets.connect(
                    self.server_url,
                    extra_headers=headers,
                    ping_interval=30,
                    ping_timeout=10
                )

            self._connected = True
            logger.info("WebSocket connected successfully")

            # 发送注册消息
            await self._send_registration()

            return True

        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self._connected = False
            return False

    async def _send_registration(self):
        """发送注册消息"""
        try:
            msg = build_register_message(
                client_id=self.client_id,
                version=config.VERSION,
                claude_version=config.get_claude_version(),
                supported_tools=config.SUPPORTED_TOOLS
            )
            await self.send_message(msg)
            logger.info("Registration message sent")
        except Exception as e:
            logger.error(f"Failed to send registration: {e}")

    async def send_message(self, message: Message):
        """发送消息"""
        if not self.websocket or not self._connected:
            logger.warning("Not connected, cannot send message")
            return

        try:
            json_str = message.to_json()
            logger.debug(f"Sending message: {message.type}")
            await self.websocket.send(json_str)
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            self._connected = False

    async def _heartbeat_loop(self):
        """心跳循环"""
        while self._connected and not self._shutdown:
            try:
                msg = build_heartbeat_message(
                    status="idle" if self.runner_manager.get_active_count() == 0 else "busy",
                    active_tasks=self.runner_manager.get_active_count()
                )
                await self.send_message(msg)
                await asyncio.sleep(config.agent.heartbeat_interval)
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                await asyncio.sleep(5)

    async def _handle_message(self, raw_message: str):
        """处理收到的消息"""
        try:
            message = Message.from_json(raw_message)
            logger.debug(f"Received message: {message.type}")

            # 根据消息类型分发处理
            if message.type == MessageType.TASK_EXECUTE:
                await self._handle_task_execute(message)
            elif message.type == MessageType.TASK_CANCEL:
                await self._handle_task_cancel(message)
            elif message.type == MessageType.HEARTBEAT_ACK:
                # 心跳响应，无需处理
                pass
            elif message.type == MessageType.AGENT_REGISTER_ACK:
                self._registered = True
                logger.info("Registration acknowledged by server")
            else:
                logger.warning(f"Unknown message type: {message.type}")

        except json.JSONDecodeError:
            logger.error(f"Invalid JSON message: {raw_message}")
        except Exception as e:
            logger.exception(f"Error handling message: {e}")

    async def _handle_task_execute(self, message: Message):
        """处理任务执行消息"""
        task_id = message.id
        if not task_id:
            logger.error("Task message missing ID")
            await self.send_message(build_error_message(
                "Task ID is required", "MISSING_TASK_ID"
            ))
            return

        try:
            # 解析任务负载
            payload = TaskPayload(**message.payload)
            logger.info(f"Received task {task_id}: {payload.prompt[:50]}...")

            # 启动任务执行（不阻塞消息循环）
            asyncio.create_task(self._execute_task(task_id, payload))

        except Exception as e:
            logger.exception(f"Failed to parse task payload: {e}")
            await self.send_message(build_task_failed_message(
                task_id, f"Invalid task payload: {e}", "INVALID_PAYLOAD"
            ))

    async def _handle_task_cancel(self, message: Message):
        """处理任务取消消息"""
        task_id = message.id
        if not task_id:
            return

        logger.info(f"Cancel requested for task {task_id}")
        if self.runner_manager.cancel_task(task_id):
            await self.send_message(build_task_cancelled_message(task_id))
        else:
            logger.warning(f"Task {task_id} not found or already completed")

    async def _execute_task(self, task_id: str, payload: TaskPayload):
        """执行任务"""
        try:
            # 通知任务开始
            await self.send_message(build_task_started_message(task_id))

            # 进度回调
            async def progress_callback(progress: TaskProgress):
                await self.send_message(build_task_progress_message(
                    task_id, progress
                ))

            # 执行Claude Code
            result = await self.runner_manager.run_task(
                task_id=task_id,
                prompt=payload.prompt,
                options=payload.options,
                context=payload.context,
                workdir=payload.workdir,
                progress_callback=progress_callback
            )

            # 发送结果
            if result.success:
                await self.send_message(build_task_completed_message(
                    task_id, result
                ))
            else:
                await self.send_message(build_task_failed_message(
                    task_id,
                    error="Task execution failed",
                    error_code="EXECUTION_FAILED",
                    partial_output=result.result
                ))

        except Exception as e:
            logger.exception(f"Task execution error: {e}")
            await self.send_message(build_task_failed_message(
                task_id, str(e), "INTERNAL_ERROR"
            ))

    async def _message_loop(self):
        """消息接收循环"""
        while self._connected and not self._shutdown:
            try:
                message = await self.websocket.recv()
                if isinstance(message, str):
                    await self._handle_message(message)
            except ConnectionClosedOK:
                logger.info("Connection closed normally")
                break
            except ConnectionClosedError as e:
                logger.error(f"Connection closed with error: {e}")
                break
            except Exception as e:
                logger.exception(f"Error in message loop: {e}")
                break

        self._connected = False

    async def start(self):
        """启动客户端"""
        logger.info(f"Starting Claude Remote Agent v{config.VERSION}")
        logger.info(f"Client ID: {self.client_id}")

        while not self._shutdown:
            # 尝试连接
            connected = await self.connect()

            if connected:
                self._reconnect_attempts = 0

                # 启动心跳任务
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                # 启动消息循环
                await self._message_loop()

                # 清理
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                    try:
                        await self._heartbeat_task
                    except asyncio.CancelledError:
                        pass

            if self._shutdown:
                break

            # 重连逻辑
            self._reconnect_attempts += 1
            if (config.agent.max_reconnect_attempts > 0 and
                self._reconnect_attempts >= config.agent.max_reconnect_attempts):
                logger.error("Max reconnect attempts reached, exiting")
                break

            logger.info(f"Reconnecting in {config.agent.reconnect_delay}s "
                       f"(attempt {self._reconnect_attempts})...")
            await asyncio.sleep(config.agent.reconnect_delay)

    async def shutdown(self):
        """关闭客户端"""
        logger.info("Shutting down...")
        self._shutdown = True

        # 取消所有运行中的任务
        for task_id in self.runner_manager.get_running_tasks():
            self.runner_manager.cancel_task(task_id)

        # 关闭连接
        if self.websocket:
            await self.websocket.close()

        logger.info("Shutdown complete")

    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self._connected

    def is_registered(self) -> bool:
        """检查是否已注册"""
        return self._registered
