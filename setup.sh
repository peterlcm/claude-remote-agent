#!/bin/bash
# Claude Remote Agent 快速启动脚本

set -e

echo "========================================"
echo "  Claude Remote Agent - 快速启动"
echo "========================================"

# 检查Python版本
if ! command -v python3 &> /dev/null; then
    echo "❌ 未找到 python3，请先安装 Python 3.8+"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | awk '{print $2}')
echo "✅ Python 版本: $PYTHON_VERSION"

# 检查Claude Code
if ! command -v claude &> /dev/null; then
    echo ""
    echo "⚠️  未找到 claude 命令"
    echo "   请先安装 Claude Code: npm install -g @anthropic-ai/claude-code"
    echo ""
    read -p "是否继续? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# 创建虚拟环境
echo ""
echo "📦 创建虚拟环境..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "✅ 虚拟环境已创建"
else
    echo "✅ 虚拟环境已存在"
fi

# 激活虚拟环境并安装依赖
echo ""
echo "📦 安装依赖..."
source venv/bin/activate
pip install -q -r requirements.txt
echo "✅ 依赖安装完成"

# 创建日志目录
mkdir -p logs

echo ""
echo "========================================"
echo "  环境准备完成！"
echo "========================================"
echo ""
echo "接下来的步骤:"
echo ""
echo "1. 测试 Claude Code (可选但推荐):"
echo "   python test_claude.py"
echo ""
echo "2. 启动模拟云端服务 (新终端):"
echo "   source venv/bin/activate"
echo "   python mock_server.py"
echo ""
echo "3. 启动代理客户端 (新终端):"
echo "   source venv/bin/activate"
echo "   python main.py"
echo ""
echo "4. 发送测试任务 (新终端):"
echo "   source venv/bin/activate"
echo "   python test_client.py \"用Python写一个Hello World\""
echo ""
echo "或者使用 mock_server 的交互式控制台直接发送任务"
echo ""
