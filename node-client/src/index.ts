/**
 * 公共 API 导出 — 供库方式使用
 */

export { ClaudeRemoteAgent } from './agent-client';
export { ClaudeRunner, ClaudeRunnerManager, EventCallback, ProgressCallback } from './claude-runner';
export { Config, ClaudeConfig, AgentConfig, LogConfig } from './config';
export {
  MessageType,
  Message,
  TaskOptions,
  TaskPayload,
  TaskResult,
  TaskProgress,
  TaskEvent,
  UserConfirmationRequest,
  UserConfirmationResponse,
  ConfirmationOption,
  messageToJson,
  messageFromJson,
  buildRegisterMessage,
  buildHeartbeatMessage,
  buildTaskStartedMessage,
  buildTaskProgressMessage,
  buildTaskEventMessage,
  buildTaskCompletedMessage,
  buildTaskFailedMessage,
  buildTaskCancelledMessage,
  buildErrorMessage,
  buildUserConfirmationRequest,
  buildUserConfirmationResponse,
} from './protocol';
export { Semaphore, withSemaphore } from './semaphore';
export { IpcClient } from './ipc-client';
