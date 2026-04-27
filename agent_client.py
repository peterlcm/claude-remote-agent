"""
WebSocket 长连接客户端
"""
import asyncio
import json
import logging
import secrets
import socket
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from config import config
from protocol import (
    ConfirmationOption,
    Message,
    MessageType,
    TaskOptions,
    TaskPayload,
    TaskProgress,
    UserConfirmationRequest,
    UserConfirmationResponse,
    build_error_message,
    build_heartbeat_message,
    build_register_message,
    build_task_cancelled_message,
    build_task_completed_message,
    build_task_event_message,
    build_task_failed_message,
    build_task_progress_message,
    build_task_started_message,
    build_user_confirmation_request,
    build_user_confirmation_response,
)
from claude_runner import ClaudeRunnerManager

logger = logging.getLogger(__name__)


PERMISSION_TOOL_NAME = "mcp__remote_agent__approve"


class ClaudeRemoteAgent:
    """Claude 远程代理客户端"""

    def __init__(self, server_url: Optional[str] = None,
                 agent_token: Optional[str] = None,
                 client_id: Optional[str] = None):
        self.server_url = server_url or config.agent.server_url
        self.agent_token = agent_token or config.agent.agent_token
        self.client_id = client_id or config.agent.client_id

        self.websocket: Optional["websockets.WebSocketClientProtocol"] = None
        self.runner_manager = ClaudeRunnerManager(max_concurrent=3)

        self._connected = False
        self._registered = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._reconnect_attempts = 0
        self._shutdown = False

        # 挂起的用户确认请求: request_id -> asyncio.Future
        self._pending_confirmations: Dict[str, asyncio.Future] = {}

        # MCP IPC 状态
        self._ipc_server: Optional[asyncio.AbstractServer] = None
        self._ipc_host = "127.0.0.1"
        self._ipc_port: int = 0
        self._ipc_token: str = ""
        self._mcp_config_path: Optional[Path] = None
        self._mcp_clients: List[asyncio.StreamWriter] = []

    # ------------------------------------------------------------------ ws

    async def connect(self) -> bool:
        try:
            logger.info("Connecting to %s ...", self.server_url)

            headers: Dict[str, str] = {}
            if self.agent_token:
                headers["Authorization"] = f"Bearer {self.agent_token}"
            headers["X-Client-ID"] = self.client_id
            headers["X-Client-Version"] = config.VERSION

            try:
                self.websocket = await websockets.connect(
                    self.server_url,
                    additional_headers=headers,
                    ping_interval=30,
                    ping_timeout=10,
                )
            except TypeError:
                self.websocket = await websockets.connect(
                    self.server_url,
                    extra_headers=headers,
                    ping_interval=30,
                    ping_timeout=10,
                )

            self._connected = True
            logger.info("WebSocket connected successfully")

            await self._send_registration()
            return True

        except Exception as exc:
            logger.error("Connection failed: %s", exc)
            self._connected = False
            return False

    async def _send_registration(self):
        try:
            msg = build_register_message(
                client_id=self.client_id,
                version=config.VERSION,
                claude_version=config.get_claude_version(),
                supported_tools=config.SUPPORTED_TOOLS,
            )
            await self.send_message(msg)
            logger.info("Registration message sent")
        except Exception as exc:
            logger.error("Failed to send registration: %s", exc)

    async def send_message(self, message: Message):
        if not self.websocket or not self._connected:
            logger.warning("Not connected, cannot send %s", message.type)
            return
        try:
            json_str = message.to_json()
            logger.debug("Sending message: %s", message.type)
            await self.websocket.send(json_str)
        except Exception as exc:
            logger.error("Failed to send message: %s", exc)
            self._connected = False

    # ------------------------------------------------------------------ confirm

    async def request_user_confirmation(self, request: UserConfirmationRequest) -> str:
        """请求用户确认，等待回应（被 Claude 任务调用、被 MCP 路径复用）。"""
        request_id = request.request_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_confirmations[request_id] = future

        await self.send_message(build_user_confirmation_request(request))
        logger.info("User confirmation requested: %s for task %s",
                    request_id, request.task_id)

        try:
            result = await asyncio.wait_for(future, timeout=request.timeout)
            logger.info("User confirmation received: %s -> %s", request_id, result)
            return result
        except asyncio.TimeoutError:
            logger.warning("User confirmation timeout: %s", request_id)
            return "timeout"
        finally:
            self._pending_confirmations.pop(request_id, None)

    # ------------------------------------------------------------------ ipc

    async def _start_ipc_server(self) -> None:
        """Start a loopback TCP server for the Permission MCP child process."""
        if self._ipc_server is not None:
            return
        self._ipc_token = secrets.token_urlsafe(24)
        self._ipc_server = await asyncio.start_server(
            self._handle_ipc_connection, host=self._ipc_host, port=0,
        )
        sock = self._ipc_server.sockets[0]
        self._ipc_port = sock.getsockname()[1]
        logger.info("MCP IPC server listening on %s:%d", self._ipc_host, self._ipc_port)
        self._write_mcp_config()

    def _write_mcp_config(self) -> None:
        """Materialize an mcp-config JSON for Claude CLI to load on each task."""
        agent_dir = Path(__file__).parent.resolve()
        mcp_dir = agent_dir / "data"
        mcp_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = mcp_dir / f"mcp_config_{self.client_id}.json"
        cfg = {
            "mcpServers": {
                "remote_agent": {
                    "command": sys.executable,
                    "args": ["-m", "permission_mcp"],
                    "env": {
                        "AGENT_IPC_HOST": self._ipc_host,
                        "AGENT_IPC_PORT": str(self._ipc_port),
                        "AGENT_IPC_TOKEN": self._ipc_token,
                        "PYTHONPATH": str(agent_dir),
                        "PYTHONIOENCODING": "utf-8",
                    },
                }
            }
        }
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self._mcp_config_path = cfg_path
        logger.info("MCP config written to %s", cfg_path)

    async def _handle_ipc_connection(self,
                                     reader: asyncio.StreamReader,
                                     writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        logger.debug("Permission MCP IPC connected from %s", peer)
        authed = False
        self._mcp_clients.append(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    frame = json.loads(line.decode("utf-8"))
                except Exception as exc:
                    logger.warning("Bad IPC frame: %s", exc)
                    continue
                if frame.get("token") != self._ipc_token:
                    logger.warning("IPC frame rejected: bad token")
                    continue
                ftype = frame.get("type")
                if ftype == "hello":
                    authed = True
                    logger.debug("Permission MCP authed: role=%s",
                                 frame.get("role"))
                    continue
                if not authed:
                    logger.warning("IPC frame before hello, dropping: %s", ftype)
                    continue
                if ftype == "approve_request":
                    asyncio.create_task(self._handle_approve_request(frame, writer))
                else:
                    logger.debug("Unknown IPC frame type: %s", ftype)
        except Exception as exc:
            logger.debug("IPC connection error: %s", exc)
        finally:
            try:
                self._mcp_clients.remove(writer)
            except ValueError:
                pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_approve_request(self,
                                      frame: Dict[str, Any],
                                      writer: asyncio.StreamWriter) -> None:
        """Translate a permission_mcp approve_request into a user confirmation."""
        request_id = frame.get("request_id") or str(uuid.uuid4())
        tool_name = str(frame.get("tool_name") or "")
        tool_input = frame.get("tool_input") or {}
        tool_use_id = frame.get("tool_use_id")
        timeout = int(frame.get("timeout") or 600)

        # task_id 不在 MCP 调用上下文中显式可知；用 tool_use_id 兜底以便前端关联。
        task_id = self._infer_active_task_id() or (tool_use_id or "unknown")

        confirmation_request_id = f"perm-{request_id}"
        confirm = UserConfirmationRequest(
            request_id=confirmation_request_id,
            task_id=task_id,
            title=f"工具确认: {tool_name or '未知工具'}",
            message="Claude 正在请求使用以下工具，请确认是否允许。",
            prompt=self._format_tool_input_preview(tool_name, tool_input),
            options=[
                ConfirmationOption(label="允许", value="allow"),
                ConfirmationOption(label="拒绝", value="deny"),
            ],
            timeout=timeout,
            source="permission_mcp",
            tool_name=tool_name,
            tool_input=tool_input if isinstance(tool_input, dict) else {"value": tool_input},
            tool_use_id=tool_use_id,
        )

        decision = await self.request_user_confirmation(confirm)
        response_frame: Dict[str, Any] = {
            "type": "approve_response",
            "request_id": request_id,
        }
        if decision == "allow":
            response_frame["behavior"] = "allow"
            response_frame["updated_input"] = tool_input
        else:
            response_frame["behavior"] = "deny"
            if decision == "timeout":
                response_frame["message"] = "permission request timed out"
            else:
                response_frame["message"] = (
                    f"operator denied tool '{tool_name}'"
                )

        try:
            data = (json.dumps(response_frame, ensure_ascii=False) + "\n").encode("utf-8")
            writer.write(data)
            await writer.drain()
        except Exception as exc:
            logger.error("Failed to send approve_response: %s", exc)

    def _infer_active_task_id(self) -> Optional[str]:
        running = self.runner_manager.get_running_tasks()
        if len(running) == 1:
            return running[0]
        return None

    @staticmethod
    def _format_tool_input_preview(tool_name: str, tool_input: Any) -> str:
        try:
            preview = json.dumps(tool_input, ensure_ascii=False, indent=2)
        except Exception:
            preview = str(tool_input)
        if len(preview) > 1500:
            preview = preview[:1500] + "..."
        return f"{tool_name}\n{preview}"

    # ------------------------------------------------------------------ heartbeat

    async def _heartbeat_loop(self):
        while self._connected and not self._shutdown:
            try:
                msg = build_heartbeat_message(
                    status="idle" if self.runner_manager.get_active_count() == 0 else "busy",
                    active_tasks=self.runner_manager.get_active_count(),
                )
                await self.send_message(msg)
                await asyncio.sleep(config.agent.heartbeat_interval)
            except Exception as exc:
                logger.error("Heartbeat error: %s", exc)
                await asyncio.sleep(5)

    # ------------------------------------------------------------------ msg loop

    async def _handle_message(self, raw_message: str):
        try:
            message = Message.from_json(raw_message)
            logger.debug("Received message: %s", message.type)

            if message.type == MessageType.TASK_EXECUTE:
                await self._handle_task_execute(message)
            elif message.type == MessageType.TASK_CANCEL:
                await self._handle_task_cancel(message)
            elif message.type == MessageType.HEARTBEAT_ACK:
                pass
            elif message.type == MessageType.AGENT_REGISTER_ACK:
                self._registered = True
                logger.info("Registration acknowledged by server")
            elif message.type == MessageType.USER_CONFIRMATION_RESPONSE:
                request_id = message.payload.get("request_id")
                if request_id in self._pending_confirmations:
                    future = self._pending_confirmations[request_id]
                    if not future.done():
                        future.set_result(message.payload.get("value"))
                        logger.info("User confirmation received for %s: %s",
                                    request_id, message.payload.get("value"))
                else:
                    logger.warning("Unknown or completed confirmation request: %s",
                                   request_id)
            else:
                logger.warning("Unknown message type: %s", message.type)

        except json.JSONDecodeError:
            logger.error("Invalid JSON message: %s", raw_message)
        except Exception as exc:
            logger.exception("Error handling message: %s", exc)

    async def _handle_task_execute(self, message: Message):
        task_id = message.id
        if not task_id:
            logger.error("Task message missing ID")
            await self.send_message(build_error_message(
                "Task ID is required", "MISSING_TASK_ID"
            ))
            return

        try:
            payload = TaskPayload(**message.payload)
            logger.info("Received task %s: %s ...", task_id, payload.prompt[:60])
            asyncio.create_task(self._execute_task(task_id, payload))
        except Exception as exc:
            logger.exception("Failed to parse task payload: %s", exc)
            await self.send_message(build_task_failed_message(
                task_id, f"Invalid task payload: {exc}", "INVALID_PAYLOAD"
            ))

    async def _handle_task_cancel(self, message: Message):
        task_id = message.id
        if not task_id:
            return
        logger.info("Cancel requested for task %s", task_id)
        if self.runner_manager.cancel_task(task_id):
            await self.send_message(build_task_cancelled_message(task_id))
        else:
            logger.warning("Task %s not found or already completed", task_id)

    # ------------------------------------------------------------------ task

    async def _execute_task(self, task_id: str, payload: TaskPayload):
        try:
            await self.send_message(build_task_started_message(task_id))

            seq_counter = {"value": 0}

            async def event_callback(event_type: str, evt_payload: Dict[str, Any]) -> None:
                seq_counter["value"] += 1
                msg = build_task_event_message(
                    task_id=task_id,
                    seq=seq_counter["value"],
                    event_type=event_type,
                    payload=evt_payload,
                )
                await self.send_message(msg)

            async def progress_callback(progress: TaskProgress) -> None:
                await self.send_message(build_task_progress_message(task_id, progress))

            permission_mode = config.claude.permission_mode or "default"
            auto_approve_tools = list(config.claude.auto_approve_tools or [])

            mcp_config = str(self._mcp_config_path) if self._mcp_config_path else None

            result = await self.runner_manager.run_task(
                task_id=task_id,
                prompt=payload.prompt,
                options=payload.options,
                context=payload.context,
                workdir=payload.workdir,
                progress_callback=progress_callback,
                confirmation_callback=self.request_user_confirmation,
                event_callback=event_callback,
                mcp_config_path=mcp_config,
                permission_tool=PERMISSION_TOOL_NAME if mcp_config else None,
                permission_mode=permission_mode,
                auto_approve_tools=auto_approve_tools or None,
            )

            if result.success:
                await self.send_message(build_task_completed_message(task_id, result))
            else:
                await self.send_message(build_task_failed_message(
                    task_id,
                    error=result.result or "Task execution failed",
                    error_code="EXECUTION_FAILED",
                    partial_output=result.result,
                ))

        except Exception as exc:
            logger.exception("Task execution error: %s", exc)
            await self.send_message(build_task_failed_message(
                task_id, str(exc), "INTERNAL_ERROR"
            ))

    # ------------------------------------------------------------------ loops

    async def _message_loop(self):
        while self._connected and not self._shutdown:
            try:
                message = await self.websocket.recv()
                if isinstance(message, str):
                    await self._handle_message(message)
            except ConnectionClosedOK:
                logger.info("Connection closed normally")
                break
            except ConnectionClosedError as exc:
                logger.error("Connection closed with error: %s", exc)
                break
            except Exception as exc:
                logger.exception("Error in message loop: %s", exc)
                break

        self._connected = False

    async def start(self):
        logger.info("Starting Claude Remote Agent v%s", config.VERSION)
        logger.info("Client ID: %s", self.client_id)

        await self._start_ipc_server()

        while not self._shutdown:
            connected = await self.connect()

            if connected:
                self._reconnect_attempts = 0
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                await self._message_loop()

                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                    try:
                        await self._heartbeat_task
                    except asyncio.CancelledError:
                        pass

            if self._shutdown:
                break

            self._reconnect_attempts += 1
            if (config.agent.max_reconnect_attempts > 0 and
                    self._reconnect_attempts >= config.agent.max_reconnect_attempts):
                logger.error("Max reconnect attempts reached, exiting")
                break

            logger.info("Reconnecting in %ss (attempt %d) ...",
                        config.agent.reconnect_delay, self._reconnect_attempts)
            await asyncio.sleep(config.agent.reconnect_delay)

    async def shutdown(self):
        logger.info("Shutting down ...")
        self._shutdown = True

        for task_id in self.runner_manager.get_running_tasks():
            self.runner_manager.cancel_task(task_id)

        for future in self._pending_confirmations.values():
            if not future.done():
                future.set_result("cancelled")
        self._pending_confirmations.clear()

        for writer in list(self._mcp_clients):
            try:
                writer.close()
            except Exception:
                pass
        self._mcp_clients.clear()

        if self._ipc_server is not None:
            self._ipc_server.close()
            try:
                await self._ipc_server.wait_closed()
            except Exception:
                pass
            self._ipc_server = None

        if self.websocket:
            await self.websocket.close()

        logger.info("Shutdown complete")

    def is_connected(self) -> bool:
        return self._connected

    def is_registered(self) -> bool:
        return self._registered
