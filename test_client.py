#!/usr/bin/env python3
"""
测试客户端 - 用于发送测试任务
"""
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

import websockets


class TestClient:
    """测试客户端"""

    def __init__(self, server_url: str = "ws://localhost:8765"):
        self.server_url = server_url
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.responses = []

    async def connect(self):
        """连接到服务端"""
        self.websocket = await websockets.connect(self.server_url)
        print(f"Connected to {self.server_url}")

    async def send_task(self, prompt: str,
                        model: str = "sonnet",
                        max_turns: int = 5,
                        output_format: str = "text",
                        wait: bool = True,
                        timeout: int = 300) -> Optional[dict]:
        """发送任务"""
        import uuid

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

        await self.websocket.send(json.dumps(message))
        print(f"Task sent: {task_id}")
        print(f"Prompt: {prompt[:80]}...")

        if wait:
            return await self.wait_for_result(task_id, timeout)
        return None

    async def wait_for_result(self, task_id: str, timeout: int) -> Optional[dict]:
        """等待任务结果"""
        print(f"Waiting for result (timeout: {timeout}s)...")

        start = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start < timeout:
            try:
                message = await asyncio.wait_for(
                    self.websocket.recv(),
                    timeout=1.0
                )
                data = json.loads(message)
                msg_type = data.get("type")
                msg_id = data.get("id")

                if msg_type == "task.started" and msg_id == task_id:
                    print(f"  Task started at {data['payload']['started_at']}")

                elif msg_type == "task.progress" and msg_id == task_id:
                    progress = data["payload"]
                    print(f"  Progress: turn {progress['turn']}/{progress['max_turns']} "
                          f"- {progress['status']}")

                elif msg_type == "task.completed" and msg_id == task_id:
                    print("\n" + "=" * 60)
                    print("TASK COMPLETED!")
                    print("=" * 60)
                    result = data["payload"]
                    print(f"Success: {result['success']}")
                    print(f"Duration: {result['duration_ms']}ms")
                    print(f"Turns: {result['num_turns']}")
                    if result.get("usage"):
                        print(f"Usage: {result['usage']}")
                    print("\nResult:")
                    print("-" * 60)
                    print(result.get("result", ""))
                    print("-" * 60)
                    return result

                elif msg_type == "task.failed" and msg_id == task_id:
                    print("\n" + "=" * 60)
                    print("TASK FAILED!")
                    print("=" * 60)
                    error = data["payload"]
                    print(f"Error: {error.get('error')}")
                    print(f"Code: {error.get('error_code')}")
                    if error.get("partial_output"):
                        print(f"\nPartial output:\n{error['partial_output']}")
                    print("-" * 60)
                    return data["payload"]

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"Error: {e}")
                break

        print("Timeout waiting for result")
        return None

    async def close(self):
        """关闭连接"""
        if self.websocket:
            await self.websocket.close()


async def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="Claude Remote Agent 测试客户端")
    parser.add_argument("prompt", nargs="?", help="要执行的任务提示")
    parser.add_argument("--server", "-s", default="ws://localhost:8765",
                        help="服务端地址")
    parser.add_argument("--model", "-m", default="sonnet",
                        help="模型名称 (sonnet/opus/haiku)")
    parser.add_argument("--max-turns", "-t", type=int, default=5,
                        help="最大迭代次数")
    parser.add_argument("--json", action="store_true",
                        help="使用JSON输出格式")
    parser.add_argument("--timeout", type=int, default=300,
                        help="超时时间(秒)")
    parser.add_argument("--test", action="store_true",
                        help="运行预设测试")

    args = parser.parse_args()

    client = TestClient(args.server)
    await client.connect()

    try:
        if args.test:
            # 运行预设测试
            print("\n" + "=" * 60)
            print("运行预设测试")
            print("=" * 60)

            tests = [
                ("简单问候", "用中文说一句问候的话"),
                ("代码生成", "用Python写一个快速排序函数"),
                ("文本摘要", "用3句话总结什么是人工智能"),
            ]

            for name, prompt in tests:
                print(f"\n--- 测试: {name} ---")
                await client.send_task(
                    prompt,
                    model=args.model,
                    max_turns=3,
                    wait=True,
                    timeout=60
                )
                await asyncio.sleep(1)

        elif args.prompt:
            # 执行用户指定的任务
            result = await client.send_task(
                args.prompt,
                model=args.model,
                max_turns=args.max_turns,
                output_format="json" if args.json else "text",
                timeout=args.timeout
            )

            if args.json and result:
                print("\nStructured output:")
                print(json.dumps(result.get("structured_output", {}),
                               indent=2, ensure_ascii=False))
        else:
            print("使用方法:")
            print(f"  {sys.argv[0]} \"你的任务提示\"")
            print(f"  {sys.argv[0]} --test  (运行预设测试)")
            print(f"\n示例:")
            print(f'  {sys.argv[0]} "用Python写一个HTTP服务器"')

    finally:
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
