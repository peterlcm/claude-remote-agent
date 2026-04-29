#!/usr/bin/env python3
"""
Claude Code功能测试脚本
用于验证本地Claude Code安装和配置是否正确
"""
import subprocess
import sys
import json


def run_command(cmd, timeout=30):
    """运行命令并返回结果"""
    print(f"$ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result
    except subprocess.TimeoutExpired:
        print("  ⏱️  超时")
        return None
    except Exception as e:
        print(f"  ❌ 错误: {e}")
        return None


def test_claude_version():
    """测试Claude Code版本"""
    print("\n" + "=" * 60)
    print("1. 检查 Claude Code 版本")
    print("=" * 60)

    result = run_command(["claude", "--version"])
    if result and result.returncode == 0:
        print(f"  ✅ 版本: {result.stdout.strip()}")
        return True
    else:
        print("  ❌ 未找到 claude 命令，请先安装 Claude Code")
        print("     安装方法: npm install -g @anthropic-ai/claude-code")
        return False


def test_claude_auth():
    """测试Claude Code认证状态"""
    print("\n" + "=" * 60)
    print("2. 检查认证状态")
    print("=" * 60)

    result = run_command(["claude", "auth", "status", "--text"])
    if result and result.returncode == 0:
        print(f"  {result.stdout.strip()}")
        return True
    else:
        print("  ⚠️  认证状态检查失败")
        if result and result.stderr:
            print(f"  {result.stderr.strip()}")
        return False


def test_simple_prompt():
    """测试简单提示词"""
    print("\n" + "=" * 60)
    print("3. 测试简单提示词执行")
    print("=" * 60)

    result = run_command([
        "claude", "-p", "用中文说一句话问候，不要超过20个字",
        "--max-turns", "1",
    ], timeout=60)

    if result:
        if result.returncode == 0:
            print("  ✅ 执行成功")
            print(f"  输出: {result.stdout.strip()[:100]}")
            return True
        else:
            print("  ❌ 执行失败")
            if result.stderr:
                print(f"  错误: {result.stderr.strip()}")
            return False
    return False


def test_json_output():
    """测试JSON输出格式"""
    print("\n" + "=" * 60)
    print("4. 测试JSON输出格式")
    print("=" * 60)

    result = run_command([
        "claude", "-p", "回答问题：2+2等于几？只返回数字",
        "--max-turns", "1",
        "--effort", "low",
        "--output-format", "json"
    ], timeout=60)

    if result and result.returncode == 0:
        try:
            data = json.loads(result.stdout)
            print(f"  ✅ JSON解析成功")
            print(f"  类型: {data.get('type')}")
            print(f"  成功: {data.get('subtype')}")
            print(f"  迭代: {data.get('num_turns')} 次")
            if "usage" in data:
                print(f"  Token使用: {data['usage']}")
            return True
        except json.JSONDecodeError:
            print(f"  ⚠️  输出不是有效的JSON: {result.stdout[:100]}...")
            return False
    else:
        print("  ❌ 执行失败")
        return False


def test_tools_allowed():
    """测试工具权限"""
    print("\n" + "=" * 60)
    print("5. 测试工具权限")
    print("=" * 60)

    result = run_command([
        "claude", "-p", "列出当前目录的文件列表",
        "--max-turns", "1",
        "--allowedTools", "Bash"
    ], timeout=60)

    if result and result.returncode == 0:
        print("  ✅ Bash工具可用")
        return True
    else:
        print("  ⚠️  Bash工具执行可能受限")
        return False


def main():
    """主函数"""
    print("Claude Remote Agent - 功能测试工具")

    tests = [
        ("版本检查", test_claude_version),
        ("认证状态", test_claude_auth),
        ("简单提示", test_simple_prompt),
        ("JSON输出", test_json_output),
        ("工具权限", test_tools_allowed),
    ]

    results = {}
    for name, test_func in tests:
        try:
            results[name] = test_func()
        except Exception as e:
            print(f"  ❌ 异常: {e}")
            results[name] = False

    # 汇总
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for name, passed_flag in results.items():
        status = "✅ 通过" if passed_flag else "❌ 失败"
        print(f"  {name}: {status}")

    print(f"\n总计: {passed}/{total} 项通过")

    if passed == total:
        print("\n🎉 所有测试通过！Claude Code运行正常。")
        print("   你现在可以启动 agent 了: python main.py")
    else:
        print("\n⚠️  部分测试失败，请根据上述信息排查问题。")
        print("   常见问题:")
        print("   1. 未安装 Claude Code: npm install -g @anthropic-ai/claude-code")
        print("   2. 未登录: claude auth login")
        print("   3. API Key问题: 检查 ANTHROPIC_API_KEY 环境变量")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
