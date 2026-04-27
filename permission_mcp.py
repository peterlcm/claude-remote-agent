"""
Permission MCP server.

This module is launched by Claude CLI as a stdio MCP server (configured via
``--mcp-config``). It exposes a single tool ``approve`` that Claude calls every
time it needs permission to use another tool. The server forwards each request
to the parent agent process (``agent_client``) over a local TCP loopback
channel, then returns the user's allow/deny response back to Claude.

The MCP wire protocol is JSON-RPC 2.0 over stdio - implemented directly here to
avoid pinning a specific ``mcp`` SDK version. Only the minimal subset Claude
Code needs is implemented: ``initialize``, ``notifications/initialized``,
``tools/list``, ``tools/call``.

Environment variables consumed:
    AGENT_IPC_HOST  - host for the parent IPC server (default 127.0.0.1)
    AGENT_IPC_PORT  - TCP port (required)
    AGENT_IPC_TOKEN - shared secret echoed in every IPC frame
    AGENT_MCP_DEBUG - if set to "1", verbose logs go to stderr
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from typing import Any, Dict, Optional


PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "remote-agent-permission"
SERVER_VERSION = "1.0.0"


def _log(msg: str) -> None:
    """Log to stderr - stdout is reserved for MCP traffic."""
    if os.environ.get("AGENT_MCP_DEBUG") == "1":
        sys.stderr.write(f"[permission_mcp] {msg}\n")
        sys.stderr.flush()


class IpcClient:
    """Persistent NDJSON client to the parent agent process."""

    def __init__(self, host: str, port: int, token: str):
        self.host = host
        self.port = port
        self.token = token
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._pending: Dict[str, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        async with self._lock:
            if self._writer is not None and not self._writer.is_closing():
                return
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
            hello = {"type": "hello", "token": self.token, "role": "permission_mcp"}
            await self._send_raw(hello)
            self._reader_task = asyncio.create_task(self._read_loop())
            _log(f"connected to agent ipc at {self.host}:{self.port}")

    async def _send_raw(self, frame: Dict[str, Any]) -> None:
        if not self._writer:
            raise RuntimeError("ipc not connected")
        data = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        self._writer.write(data)
        await self._writer.drain()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            try:
                line = await self._reader.readline()
            except Exception as exc:
                _log(f"ipc read error: {exc}")
                break
            if not line:
                _log("ipc connection closed by peer")
                break
            try:
                frame = json.loads(line.decode("utf-8"))
            except Exception as exc:
                _log(f"ipc bad frame: {exc}")
                continue
            req_id = frame.get("request_id")
            if frame.get("type") == "approve_response" and req_id in self._pending:
                fut = self._pending.pop(req_id)
                if not fut.done():
                    fut.set_result(frame)
        # Connection closed: cancel all pending futures so callers don't hang.
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("agent ipc closed"))
        self._pending.clear()

    async def request_approval(self,
                               tool_name: str,
                               tool_input: Dict[str, Any],
                               tool_use_id: Optional[str],
                               timeout: int = 600) -> Dict[str, Any]:
        await self.connect()
        request_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        frame = {
            "type": "approve_request",
            "token": self.token,
            "request_id": request_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_use_id": tool_use_id,
            "timeout": timeout,
            "ts": time.time(),
        }
        try:
            await self._send_raw(frame)
        except Exception as exc:
            self._pending.pop(request_id, None)
            raise
        try:
            return await asyncio.wait_for(fut, timeout=timeout + 5)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            return {"behavior": "deny", "message": "permission request timed out"}


class StdioMcpServer:
    """Minimal MCP server speaking JSON-RPC 2.0 over stdio."""

    def __init__(self, ipc: IpcClient):
        self.ipc = ipc
        self._stdout_lock = asyncio.Lock()

    async def serve(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        # Wrap stdin/stdout for asyncio.
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        while True:
            line = await reader.readline()
            if not line:
                _log("stdin EOF, exiting")
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except Exception as exc:
                _log(f"stdin non-json: {exc} :: {text[:200]}")
                continue
            asyncio.create_task(self._dispatch(msg))

    async def _dispatch(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        msg_id = msg.get("id")
        try:
            if method == "initialize":
                await self._respond(msg_id, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                })
            elif method == "notifications/initialized":
                # Notification - no response.
                return
            elif method == "tools/list":
                await self._respond(msg_id, {
                    "tools": [self._approve_tool_descriptor()],
                })
            elif method == "tools/call":
                params = msg.get("params") or {}
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if name != "approve":
                    await self._respond_error(msg_id, -32601, f"unknown tool: {name}")
                    return
                result_text = await self._handle_approve(arguments)
                await self._respond(msg_id, {
                    "content": [{"type": "text", "text": result_text}],
                    "isError": False,
                })
            elif method in ("ping",):
                await self._respond(msg_id, {})
            elif msg_id is None:
                # Unknown notification - drop silently.
                return
            else:
                await self._respond_error(msg_id, -32601, f"method not found: {method}")
        except Exception as exc:
            _log(f"dispatch error on {method}: {exc}")
            if msg_id is not None:
                await self._respond_error(msg_id, -32603, f"internal error: {exc}")

    def _approve_tool_descriptor(self) -> Dict[str, Any]:
        return {
            "name": "approve",
            "description": (
                "Ask the Remote Agent operator whether Claude may run the "
                "requested tool with the given input. Returns an allow/deny "
                "decision in the format expected by --permission-prompt-tool."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string"},
                    "input": {"type": "object"},
                    "tool_use_id": {"type": "string"},
                },
                "required": ["tool_name", "input"],
            },
        }

    async def _handle_approve(self, args: Dict[str, Any]) -> str:
        tool_name = str(args.get("tool_name") or "")
        tool_input = args.get("input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {"value": tool_input}
        tool_use_id = args.get("tool_use_id")

        try:
            response = await self.ipc.request_approval(
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=tool_use_id,
            )
        except Exception as exc:
            _log(f"approval ipc error: {exc}")
            return json.dumps({
                "behavior": "deny",
                "message": f"permission ipc error: {exc}",
            })

        behavior = response.get("behavior") or "deny"
        if behavior == "allow":
            updated = response.get("updated_input") or response.get("updatedInput") or tool_input
            return json.dumps({"behavior": "allow", "updatedInput": updated})
        message = response.get("message") or "denied by operator"
        return json.dumps({"behavior": "deny", "message": message})

    async def _respond(self, msg_id: Any, result: Dict[str, Any]) -> None:
        await self._write_frame({"jsonrpc": "2.0", "id": msg_id, "result": result})

    async def _respond_error(self, msg_id: Any, code: int, message: str) -> None:
        await self._write_frame({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        })

    async def _write_frame(self, frame: Dict[str, Any]) -> None:
        data = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._stdout_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()


async def _main_async() -> int:
    host = os.environ.get("AGENT_IPC_HOST", "127.0.0.1")
    port_str = os.environ.get("AGENT_IPC_PORT", "")
    token = os.environ.get("AGENT_IPC_TOKEN", "")
    if not port_str or not token:
        sys.stderr.write(
            "permission_mcp: missing AGENT_IPC_PORT/AGENT_IPC_TOKEN; refusing to start\n"
        )
        return 2
    try:
        port = int(port_str)
    except ValueError:
        sys.stderr.write(f"permission_mcp: bad AGENT_IPC_PORT={port_str}\n")
        return 2

    ipc = IpcClient(host=host, port=port, token=token)
    server = StdioMcpServer(ipc=ipc)
    try:
        await server.serve()
    except KeyboardInterrupt:
        pass
    return 0


def main() -> None:
    if sys.platform == "win32":
        # ProactorEventLoop required for subprocess pipes on Windows.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    code = asyncio.run(_main_async())
    sys.exit(code)


if __name__ == "__main__":
    main()
