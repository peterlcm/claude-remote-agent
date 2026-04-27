"""
配置管理模块
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings

# 加载环境变量
load_dotenv()

# 项目根目录
ROOT_DIR = Path(__file__).parent


class ClaudeConfig(BaseSettings):
    """Claude Code configuration"""
    model: str = Field(default="sonnet", validation_alias="CLAUDE_MODEL")
    max_turns: int = Field(default=10, validation_alias="CLAUDE_MAX_TURNS")
    effort: str = Field(default="", validation_alias="CLAUDE_EFFORT")
    timeout: int = Field(default=300, validation_alias="CLAUDE_TIMEOUT")


class AgentConfig(BaseSettings):
    """客户端配置"""
    server_url: str = Field(default="ws://localhost:8765", validation_alias="SERVER_URL")
    agent_token: str = Field(default="", validation_alias="AGENT_TOKEN")
    client_id: str = Field(default="agent-001", validation_alias="CLIENT_ID")
    heartbeat_interval: int = Field(default=30, validation_alias="HEARTBEAT_INTERVAL")
    reconnect_delay: int = Field(default=5, validation_alias="RECONNECT_DELAY")
    max_reconnect_attempts: int = Field(default=0, validation_alias="MAX_RECONNECT_ATTEMPTS")


class LogConfig(BaseSettings):
    """日志配置"""
    level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    file: str = Field(default="logs/agent.log", validation_alias="LOG_FILE")


class Config:
    """全局配置"""
    claude = ClaudeConfig()
    agent = AgentConfig()
    log = LogConfig()

    # 版本
    VERSION = "1.0.0"

    # 支持的工具
    SUPPORTED_TOOLS = [
        "Read", "Edit", "Write", "Bash",
        "WebSearch", "WebFetch"
    ]

    @classmethod
    def get_claude_version(cls) -> str:
        """获取Claude Code版本"""
        try:
            import subprocess
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return "unknown"


# 初始化配置实例
config = Config()
