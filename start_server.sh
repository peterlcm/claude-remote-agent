#!/bin/bash
# Claude Remote Agent 服务启动脚本

cd "$(dirname "$0")"
export PYTHONPATH=/usr/local/lib/python3.11/site-packages:/usr/local/lib64/python3.11/site-packages

echo "🚀 Starting Claude Remote Agent Server..."
echo "📊 Dashboard: http://localhost:8000"
echo "📚 API Docs: http://localhost:8000/docs"
echo "🔌 WebSocket: ws://localhost:8000/ws/frontend"
echo ""

python server.py
