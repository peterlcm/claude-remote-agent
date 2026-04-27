"""
消息协议定义
"""
import json
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field


class MessageType(str, Enum):
    """消息类型枚举"""
    # 注册与认证
    AGENT_REGISTER = "agent.register"
    AGENT_REGISTER_ACK = "agent.register_ack"

    # 心跳
    HEARTBEAT = "heartbeat"
    HEARTBEAT_ACK = "heartbeat.ack"

    # 任务相关
    TASK_EXECUTE = "task.execute"
    TASK_STARTED = "task.started"
    TASK_PROGRESS = "task.progress"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCEL = "task.cancel"
    TASK_CANCELLED = "task.cancelled"

    # 错误
    ERROR = "error"

    # 用户确认
    USER_CONFIRMATION_REQUEST = "user_confirmation.request"
    USER_CONFIRMATION_RESPONSE = "user_confirmation.response"


class ConfirmationOption(BaseModel):
    """确认选项"""
    label: str
    value: str


class UserConfirmationRequest(BaseModel):
    """用户确认请求"""
    request_id: str
    task_id: str
    title: str
    message: str
    prompt: str
    options: list[ConfirmationOption] = []
    timeout: int = 300  # 超时时间（秒）


class UserConfirmationResponse(BaseModel):
    """用户确认回应"""
    request_id: str
    task_id: str
    value: str
    timestamp: float = Field(default_factory=time.time)


class TaskOptions(BaseModel):
    """任务选项"""
    model: str = "sonnet"
    max_turns: int = 10
    effort: str = ""
    allowed_tools: Optional[List[str]] = None
    output_format: str = "text"
    timeout: int = 300
    continue_last: bool = False
    session_id: Optional[str] = None


class TaskPayload(BaseModel):
    """任务执行负载"""
    prompt: str
    context: Optional[str] = None
    workdir: str = "."
    options: TaskOptions = Field(default_factory=TaskOptions)


class TaskResult(BaseModel):
    """任务结果"""
    success: bool
    result: str = ""
    structured_output: Optional[Dict[str, Any]] = None
    usage: Dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = 0
    num_turns: int = 0
    session_id: Optional[str] = None


class TaskProgress(BaseModel):
    """任务进度"""
    turn: int = 0
    max_turns: int = 10
    status: str = "thinking"
    message: Optional[str] = None


class Message(BaseModel):
    """基础消息"""
    type: MessageType
    id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)

    def to_json(self) -> str:
        """转换为JSON字符串"""
        return self.model_dump_json(by_alias=True)

    @classmethod
    def from_json(cls, json_str: str) -> "Message":
        """从JSON字符串解析"""
        data = json.loads(json_str)
        return cls(**data)


def build_register_message(client_id: str, version: str,
                           claude_version: str,
                           supported_tools: List[str]) -> Message:
    """构建注册消息"""
    return Message(
        type=MessageType.AGENT_REGISTER,
        payload={
            "client_id": client_id,
            "version": version,
            "capabilities": {
                "claude_version": claude_version,
                "supported_tools": supported_tools
            }
        }
    )


def build_heartbeat_message(status: str = "idle",
                            active_tasks: int = 0) -> Message:
    """构建心跳消息"""
    return Message(
        type=MessageType.HEARTBEAT,
        payload={
            "status": status,
            "active_tasks": active_tasks
        }
    )


def build_task_started_message(task_id: str) -> Message:
    """构建任务开始消息"""
    return Message(
        type=MessageType.TASK_STARTED,
        id=task_id,
        payload={
            "started_at": time.time()
        }
    )


def build_task_progress_message(task_id: str,
                                progress: TaskProgress) -> Message:
    """构建任务进度消息"""
    return Message(
        type=MessageType.TASK_PROGRESS,
        id=task_id,
        payload=progress.model_dump()
    )


def build_task_completed_message(task_id: str,
                                 result: TaskResult) -> Message:
    """构建任务完成消息"""
    return Message(
        type=MessageType.TASK_COMPLETED,
        id=task_id,
        payload=result.model_dump()
    )


def build_task_failed_message(task_id: str,
                              error: str,
                              error_code: str = "UNKNOWN",
                              partial_output: str = "") -> Message:
    """构建任务失败消息"""
    return Message(
        type=MessageType.TASK_FAILED,
        id=task_id,
        payload={
            "error": error,
            "error_code": error_code,
            "partial_output": partial_output
        }
    )


def build_task_cancelled_message(task_id: str) -> Message:
    """构建任务取消消息"""
    return Message(
        type=MessageType.TASK_CANCELLED,
        id=task_id,
        payload={
            "cancelled_at": time.time()
        }
    )


def build_error_message(error: str,
                        error_code: str = "UNKNOWN") -> Message:
    """构建错误消息"""
    return Message(
        type=MessageType.ERROR,
        payload={
            "error": error,
            "error_code": error_code
        }
    )


def build_user_confirmation_request(request: UserConfirmationRequest) -> Message:
    """构建用户确认请求消息"""
    return Message(
        type=MessageType.USER_CONFIRMATION_REQUEST,
        id=request.task_id,
        payload=request.model_dump()
    )


def build_user_confirmation_response(response: UserConfirmationResponse) -> Message:
    """构建用户确认回应消息"""
    return Message(
        type=MessageType.USER_CONFIRMATION_RESPONSE,
        id=response.task_id,
        payload=response.model_dump()
    )
