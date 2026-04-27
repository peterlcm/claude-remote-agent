"""
数据库模型定义
"""
import os
import uuid
import json
from datetime import datetime
from typing import Optional, List
from sqlalchemy import create_engine, Column, String, Integer, Boolean, DateTime, Text, ForeignKey, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

# 数据库路径
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "claude_agent.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class ProxyClient(Base):
    """代理客户端"""
    __tablename__ = "proxy_clients"

    id = Column(String(64), primary_key=True, index=True)
    name = Column(String(128), nullable=False, comment="客户端名称")
    client_key = Column(String(128), unique=True, nullable=False, comment="客户端认证密钥")
    description = Column(Text, nullable=True, comment="描述")
    is_online = Column(Boolean, default=False, comment="是否在线")
    last_connected_at = Column(DateTime, nullable=True, comment="最后连接时间")
    last_heartbeat_at = Column(DateTime, nullable=True, comment="最后心跳时间")
    version = Column(String(32), nullable=True, comment="客户端版本")
    claude_version = Column(String(32), nullable=True, comment="Claude版本")
    capabilities = Column(Text, nullable=True, comment="支持的能力(JSON)")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关联
    agents = relationship("Agent", back_populates="client", cascade="all, delete-orphan")

    def set_capabilities(self, caps: dict):
        self.capabilities = json.dumps(caps, ensure_ascii=False)

    def get_capabilities(self) -> dict:
        if self.capabilities:
            try:
                return json.loads(self.capabilities)
            except:
                pass
        return {}


class Agent(Base):
    """Agent 实例"""
    __tablename__ = "agents"

    id = Column(String(64), primary_key=True, index=True)
    name = Column(String(128), nullable=False, comment="Agent名称")
    description = Column(Text, nullable=True, comment="描述")
    client_id = Column(String(64), ForeignKey("proxy_clients.id"), nullable=True)
    default_model = Column(String(64), default="sonnet", comment="默认模型")
    max_turns = Column(Integer, default=10, comment="最大迭代次数")
    effort = Column(String(32), default="medium", comment="推理强度")
    timeout = Column(Integer, default=300, comment="超时时间(秒)")
    allowed_tools = Column(Text, nullable=True, comment="允许的工具(JSON)")
    is_active = Column(Boolean, default=True, comment="是否启用")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关联
    client = relationship("ProxyClient", back_populates="agents")
    tasks = relationship("Task", back_populates="agent", cascade="all, delete-orphan")

    def set_allowed_tools(self, tools: List[str]):
        self.allowed_tools = json.dumps(tools, ensure_ascii=False)

    def get_allowed_tools(self) -> List[str]:
        if self.allowed_tools:
            try:
                return json.loads(self.allowed_tools)
            except:
                pass
        return []


class Task(Base):
    """任务"""
    __tablename__ = "tasks"

    id = Column(String(64), primary_key=True, index=True)
    agent_id = Column(String(64), ForeignKey("agents.id"), nullable=False)
    client_id = Column(String(64), nullable=True, comment="冗余：客户端ID")
    prompt = Column(Text, nullable=False, comment="任务提示词")
    context = Column(Text, nullable=True, comment="上下文")
    workdir = Column(String(256), default=".", comment="工作目录")
    options = Column(Text, nullable=True, comment="任务选项(JSON)")

    # 状态
    status = Column(String(32), default="pending", comment="状态: pending/running/completed/failed/cancelled")
    error_message = Column(Text, nullable=True, comment="错误信息")
    error_code = Column(String(64), nullable=True, comment="错误码")

    # 结果
    result = Column(Text, nullable=True, comment="执行结果")
    structured_output = Column(Text, nullable=True, comment="结构化输出(JSON)")
    usage = Column(Text, nullable=True, comment="使用量统计(JSON)")
    duration_ms = Column(Integer, default=0, comment="耗时(毫秒)")
    num_turns = Column(Integer, default=0, comment="迭代次数")
    session_id = Column(String(128), nullable=True, comment="Claude会话ID")

    # 时间
    started_at = Column(DateTime, nullable=True, comment="开始时间")
    completed_at = Column(DateTime, nullable=True, comment="完成时间")
    created_at = Column(DateTime, default=datetime.utcnow, comment="创建时间")

    # 关联
    agent = relationship("Agent", back_populates="tasks")

    def set_options(self, opts: dict):
        self.options = json.dumps(opts, ensure_ascii=False)

    def get_options(self) -> dict:
        if self.options:
            try:
                return json.loads(self.options)
            except:
                pass
        return {}

    def set_structured_output(self, data: dict):
        self.structured_output = json.dumps(data, ensure_ascii=False)

    def get_structured_output(self) -> dict:
        if self.structured_output:
            try:
                return json.loads(self.structured_output)
            except:
                pass
        return {}

    def set_usage(self, data: dict):
        self.usage = json.dumps(data, ensure_ascii=False)

    def get_usage(self) -> dict:
        if self.usage:
            try:
                return json.loads(self.usage)
            except:
                pass
        return {}


class TaskLog(Base):
    """任务日志"""
    __tablename__ = "task_logs"

    id = Column(String(64), primary_key=True, default=lambda: str(uuid.uuid4())[:12])
    task_id = Column(String(64), index=True, nullable=False)
    log_type = Column(String(32), nullable=False, comment="日志类型: info/warning/error")
    message = Column(Text, nullable=False, comment="日志内容")
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    """初始化数据库"""
    Base.metadata.create_all(bind=engine)
    print(f"✅ 数据库初始化完成: {DB_PATH}")


def create_default_client(db):
    """创建默认客户端"""
    default_client = db.query(ProxyClient).filter(ProxyClient.id == "default").first()
    if not default_client:
        default_client = ProxyClient(
            id="default",
            name="默认客户端",
            client_key="default-key-12345",
            description="系统自动创建的默认客户端"
        )
        db.add(default_client)
        db.commit()
        db.refresh(default_client)
        print(f"✅ 创建默认客户端: {default_client.id}")
    return default_client


def get_or_create_default_agent(db, client_id: str):
    """获取或创建默认Agent"""
    default_agent = db.query(Agent).filter(Agent.id == "default").first()
    if not default_agent:
        default_agent = Agent(
            id="default",
            name="默认Agent",
            description="系统自动创建的默认Agent",
            client_id=client_id,
            default_model="sonnet",
            max_turns=10,
            effort=""
        )
        db.add(default_agent)
        db.commit()
        db.refresh(default_agent)
        print(f"✅ 创建默认Agent: {default_agent.id}")
    return default_agent


def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
