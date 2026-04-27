#!/usr/bin/env python3
"""
简化的测试客户端 - 用于验证 WebSocket 连接
"""
import asyncio
import json
import websockets

async def test_client():
    uri = "ws://localhost:8080/ws/client"
    client_id = "default"
    
    print(f"🔌 连接到服务器: {uri}")
    try:
        async with websockets.connect(uri) as websocket:
            print("✅ WebSocket 已连接")
            
            # 发送注册消息
            register_msg = {
                "type": "agent.register",
                "payload": {
                    "client_id": client_id,
                    "version": "1.0.0",
                    "capabilities": {}
                }
            }
            await websocket.send(json.dumps(register_msg))
            print(f"📤 已发送注册消息 (client_id={client_id})")
            
            # 等待响应
            response = await websocket.recv()
            print(f"📥 收到响应: {response}")
            
            # 保持连接
            print("🔗 连接已建立，保持连接中...")
            try:
                while True:
                    message = await websocket.recv()
                    print(f"📥 收到消息: {message[:200]}...")
            except websockets.exceptions.ConnectionClosed:
                print("❌ 连接已关闭")
                
    except Exception as e:
        print(f"❌ 连接失败: {e}")

if __name__ == "__main__":
    asyncio.run(test_client())
