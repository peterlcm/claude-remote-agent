#!/usr/bin/env python3
"""
模拟云端服务端 - 用于本地测试
"""
import asyncio
import json
import logging
from typing import Dict, Set
import uuid

import websockets
from websockets.server import WebSocketServerProtocol

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("mock_server")


class MockCloudServer:
    """模拟云端服务"""

    def __init__(self, host: str = "localhost", port: int = 8765):
        self.host = host
        self.port = port
        self.clients: Dict[str, WebSocketServerProtocol] = {}
        self.task_results: Dict[str, dict] = {}
        self._shutdown = False

    async def handle_client(self, websocket: WebSocketServerProtocol):
        """处理客户端连接"""
        client_id = websocket.request_headers.get("X-Client-ID", "unknown")
        logger.info(f"Client connected: {client_id}")

        # 保存客户端
        self.clients[client_id] = websocket

        try:
            # 消息循环
            async for message in websocket:
                if isinstance(message, str):
                    await self.handle_message(client_id, message)

        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Client disconnected: {client_id}")
        except Exception as e:
            logger.exception(f"Error handling client {client_id}: {e}")
        finally:
            self.clients.pop(client_id, None)
            logger.info(f"Client removed: {client_id}")

    async def handle_message(self, client_id: str, raw_message: str):
        """处理客户端消息"""
        try:
            data = json.loads(raw_message)
            msg_type = data.get("type")
            msg_id = data.get("id")
            payload = data.get("payload", {})

            logger.info(f"Received from {client_id}: {msg_type}")

            if msg_type == "agent.register":
                # 响应注册
                logger.info(f"Agent registered: {payload}")
                await self.send_message(client_id, {
                    "type": "agent.register_ack",
                    "payload": {"success": True}
                })

            elif msg_type == "heartbeat":
                # 心跳响应
                await self.send_message(client_id, {
                    "type": "heartbeat.ack",
                    "payload": {"server_time": data.get("timestamp")}
                })

            elif msg_type == "task.started":
                logger.info(f"Task {msg_id} started on {client_id}")

            elif msg_type == "task.progress":
                logger.info(f"Task {msg_id} progress: {payload}")

            elif msg_type == "task.completed":
                logger.info(f"Task {msg_id} completed!")
                logger.info(f"  Success: {payload.get('success')}")
                logger.info(f"  Duration: {payload.get('duration_ms')}ms")
                logger.info(f"  Turns: {payload.get('num_turns')}")
                if payload.get("usage"):
                    logger.info(f"  Usage: {payload['usage']}")
                if payload.get("result"):
                    result_preview = payload["result"][:200].replace("\n", " ")
                    logger.info(f"  Result preview: {result_preview}...")
                self.task_results[msg_id] = payload

            elif msg_type == "task.failed":
                logger.error(f"Task {msg_id} failed: {payload.get('error')}")
                self.task_results[msg_id] = payload

            elif msg_type == "task.cancelled":
                logger.info(f"Task {msg_id} cancelled")

            elif msg_type == "error":
                logger.error(f"Error from {client_id}: {payload.get('error')}")

        except json.JSONDecodeError:
            logger.error(f"Invalid JSON from {client_id}: {raw_message}")
        except Exception as e:
            logger.exception(f"Error handling message from {client_id}: {e}")

    async def send_message(self, client_id: str, message: dict):
        """发送消息到客户端"""
        if client_id not in self.clients:
            logger.warning(f"Client {client_id} not found")
            return False

        try:
            await self.clients[client_id].send(json.dumps(message))
            return True
        except Exception as e:
            logger.error(f"Failed to send message to {client_id}: {e}")
            return False

    async def send_task(self, client_id: str, prompt: str,
                        model: str = "sonnet", max_turns: int = 5,
                        output_format: str = "text") -> str:
        """发送任务到客户端"""
        task_id = str(uuid.uuid4())

        message = {
            "type": "task.execute",
            "id": task_id,
            "payload": {
                "prompt": prompt,
                "workdir": ".",
                "options": {
                    "model": model,
                    "max_turns": max_turns,
                    "output_format": output_format
                }
            }
        }

        if await self.send_message(client_id, message):
            logger.info(f"Task {task_id} sent to {client_id}")
            return task_id
        return None

    async def cancel_task(self, client_id: str, task_id: str):
        """取消任务"""
        await self.send_message(client_id, {
            "type": "task.cancel",
            "id": task_id
        })

    def get_connected_clients(self) -> list:
        """获取已连接的客户端列表"""
        return list(self.clients.keys())

    async def start(self):
        """启动服务"""
        logger.info(f"Starting mock cloud server on {self.host}:{self.port}")

        async with websockets.serve(self.handle_client, self.host, self.port):
            logger.info("Server started, waiting for clients...")
            await asyncio.Future()  # 永久运行

    def stop(self):
        """停止服务"""
        self._shutdown = True


async def interactive_shell(server: MockCloudServer):
    """交互式命令行"""
    await asyncio.sleep(1)  # 等待服务启动

    print("\n" + "=" * 50)
    print("  Mock Cloud Server - 交互式控制台")
    print("=" * 50)
    print("可用命令:")
    print("  list               - 列出已连接的客户端")
    print("  send <prompt>      - 发送任务到客户端")
    print("  cancel <task_id>   - 取消任务")
    print("  result <task_id>   - 查看任务结果")
    print("  help               - 显示帮助")
    print("  exit               - 退出服务")
    print("=" * 50 + "\n")

    last_client = None

    while True:
        try:
            line = await asyncio.get_event_loop().run_in_executor(
                None, lambda: input("> ").strip()
            )

            if not line:
                continue

            parts = line.split(maxsplit=1)
            cmd = parts[0].lower()

            if cmd == "list":
                clients = server.get_connected_clients()
                if clients:
                    print(f"\n已连接的客户端 ({len(clients)}):")
                    for c in clients:
                        print(f"  - {c}")
                    last_client = clients[0]
                else:
                    print("\n暂无客户端连接")
                print()

            elif cmd == "send":
                clients = server.get_connected_clients()
                if not clients:
                    print("错误: 没有客户端连接")
                    continue

                if len(parts) < 2:
                    print("使用方法: send <prompt>")
                    continue

                target = last_client or clients[0]
                task_id = await server.send_task(target, parts[1])
                if task_id:
                    print(f"任务已发送: {task_id}")
                    print(f"等待执行结果... (使用 'result {task_id}' 查看)\n")
                else:
                    print("任务发送失败\n")

            elif cmd == "cancel":
                if len(parts) < 2:
                    print("使用方法: cancel <task_id>")
                    continue
                clients = server.get_connected_clients()
                if clients:
                    await server.cancel_task(clients[0], parts[1])
                    print(f"取消指令已发送: {parts[1]}\n")

            elif cmd == "result":
                if len(parts) < 2:
                    print("使用方法: result <task_id>")
                    continue
                task_id = parts[1]
                if task_id in server.task_results:
                    result = server.task_results[task_id]
                    print(f"\n任务结果: {task_id}")
                    print(f"  成功: {result.get('success')}")
                    print(f"  耗时: {result.get('duration_ms')}ms")
                    print(f"  迭代: {result.get('num_turns')} 次")
                    if result.get("usage"):
                        print(f"  使用量: {result['usage']}")
                    print("\n  结果内容:")
                    print("-" * 50)
                    print(result.get("result", ""))
                    print("-" * 50 + "\n")
                else:
                    print(f"任务结果未找到: {task_id}\n")

            elif cmd == "help":
                print("\n可用命令:")
                print("  list               - 列出已连接的客户端")
                print("  send <prompt>      - 发送任务到客户端")
                print("  cancel <task_id>   - 取消任务")
                print("  result <task_id>   - 查看任务结果")
                print("  help               - 显示帮助")
                print("  exit               - 退出服务\n")

            elif cmd == "exit":
                print("正在停止服务...")
                server.stop()
                break

            else:
                print(f"未知命令: {cmd} (输入 'help' 查看帮助)\n")

        except EOFError:
            break
        except Exception as e:
            logger.exception(f"Shell error: {e}")


async def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="模拟云端服务端")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--no-shell", action="store_true", help="禁用交互式控制台")
    args = parser.parse_args()

    server = MockCloudServer(host=args.host, port=args.port)

    # 启动服务和控制台
    tasks = [server.start()]

    if not args.no_shell:
        tasks.append(interactive_shell(server))

    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
