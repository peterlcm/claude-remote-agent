/**
 * 消息协议定义 — 与 Python 版 protocol.py 完全兼容
 */

// ---------------------------------------------------------------------------
// 消息类型
// ---------------------------------------------------------------------------

export type MessageType =
  | 'agent.register'
  | 'agent.register_ack'
  | 'heartbeat'
  | 'heartbeat.ack'
  | 'task.execute'
  | 'task.started'
  | 'task.progress'
  | 'task.event'
  | 'task.completed'
  | 'task.failed'
  | 'task.cancel'
  | 'task.cancelled'
  | 'error'
  | 'user_confirmation.request'
  | 'user_confirmation.response';

// ---------------------------------------------------------------------------
// 数据模型接口
// ---------------------------------------------------------------------------

export interface ConfirmationOption {
  label: string;
  value: string;
}

export interface UserConfirmationRequest {
  request_id: string;
  task_id: string;
  title: string;
  message: string;
  prompt: string;
  options: ConfirmationOption[];
  timeout: number;
  source?: string;
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  tool_use_id?: string;
}

export interface UserConfirmationResponse {
  request_id: string;
  task_id: string;
  value: string;
  timestamp: number;
}

export interface TaskOptions {
  model: string;
  max_turns: number;
  effort: string | null;
  allowed_tools: string[] | null;
  output_format: string;
  timeout: number;
  continue_last: boolean;
  session_id: string | null;
  mode: string;
}

export interface TaskPayload {
  prompt: string;
  context?: string | null;
  workdir: string;
  options: TaskOptions;
}

export interface TaskResult {
  success: boolean;
  result: string;
  structured_output: Record<string, unknown> | null;
  usage: Record<string, unknown>;
  duration_ms: number;
  num_turns: number;
  session_id: string | null;
}

export interface TaskProgress {
  turn: number;
  max_turns: number;
  status: string;
  message: string | null;
}

export interface TaskEvent {
  task_id: string;
  seq: number;
  event_type: string;
  payload: Record<string, unknown>;
  timestamp: number;
}

export interface Message {
  type: MessageType;
  id: string | null;
  payload: Record<string, unknown>;
  timestamp: number;
}

// ---------------------------------------------------------------------------
// 序列化 / 反序列化
// ---------------------------------------------------------------------------

export function messageToJson(msg: Message): string {
  return JSON.stringify(msg);
}

export function messageFromJson(jsonStr: string): Message {
  return JSON.parse(jsonStr) as Message;
}

// ---------------------------------------------------------------------------
// 时间戳辅助
// ---------------------------------------------------------------------------

function unixTimestamp(): number {
  return Date.now() / 1000;
}

// ---------------------------------------------------------------------------
// 构建函数
// ---------------------------------------------------------------------------

export function buildRegisterMessage(
  clientId: string,
  version: string,
  claudeVersion: string,
  supportedTools: string[],
): Message {
  return {
    type: 'agent.register',
    id: null,
    payload: {
      client_id: clientId,
      version,
      capabilities: {
        claude_version: claudeVersion,
        supported_tools: supportedTools,
      },
    },
    timestamp: unixTimestamp(),
  };
}

export function buildHeartbeatMessage(
  status: string = 'idle',
  activeTasks: number = 0,
): Message {
  return {
    type: 'heartbeat',
    id: null,
    payload: { status, active_tasks: activeTasks },
    timestamp: unixTimestamp(),
  };
}

export function buildTaskStartedMessage(taskId: string): Message {
  return {
    type: 'task.started',
    id: taskId,
    payload: { started_at: unixTimestamp() },
    timestamp: unixTimestamp(),
  };
}

export function buildTaskProgressMessage(
  taskId: string,
  progress: TaskProgress,
): Message {
  return {
    type: 'task.progress',
    id: taskId,
    payload: { ...progress },
    timestamp: unixTimestamp(),
  };
}

export function buildTaskEventMessage(
  taskId: string,
  seq: number,
  eventType: string,
  payload: Record<string, unknown>,
): Message {
  return {
    type: 'task.event',
    id: taskId,
    payload: {
      task_id: taskId,
      seq,
      event_type: eventType,
      payload,
      timestamp: unixTimestamp(),
    },
    timestamp: unixTimestamp(),
  };
}

export function buildTaskCompletedMessage(
  taskId: string,
  result: TaskResult,
): Message {
  return {
    type: 'task.completed',
    id: taskId,
    payload: { ...result },
    timestamp: unixTimestamp(),
  };
}

export function buildTaskFailedMessage(
  taskId: string,
  error: string,
  errorCode: string = 'UNKNOWN',
  partialOutput: string = '',
): Message {
  return {
    type: 'task.failed',
    id: taskId,
    payload: { error, error_code: errorCode, partial_output: partialOutput },
    timestamp: unixTimestamp(),
  };
}

export function buildTaskCancelledMessage(taskId: string): Message {
  return {
    type: 'task.cancelled',
    id: taskId,
    payload: { cancelled_at: unixTimestamp() },
    timestamp: unixTimestamp(),
  };
}

export function buildErrorMessage(
  error: string,
  errorCode: string = 'UNKNOWN',
): Message {
  return {
    type: 'error',
    id: null,
    payload: { error, error_code: errorCode },
    timestamp: unixTimestamp(),
  };
}

export function buildUserConfirmationRequest(
  request: UserConfirmationRequest,
): Message {
  return {
    type: 'user_confirmation.request',
    id: request.task_id,
    payload: { ...request },
    timestamp: unixTimestamp(),
  };
}

export function buildUserConfirmationResponse(
  response: UserConfirmationResponse,
): Message {
  return {
    type: 'user_confirmation.response',
    id: response.task_id,
    payload: { ...response },
    timestamp: unixTimestamp(),
  };
}
