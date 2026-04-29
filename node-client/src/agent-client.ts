/**
 * WebSocket 长连接客户端 — 核心调度器
 *
 * 移植自 Python 版 agent_client.py
 */

import WebSocket from 'ws';
import * as net from 'net';
import * as readline from 'readline';
import * as crypto from 'crypto';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import { Config } from './config';
import { getLogger } from './logger';
import { ClaudeRunnerManager, EventCallback, ProgressCallback } from './claude-runner';
import {
  Message,
  MessageType,
  TaskOptions,
  TaskPayload,
  TaskProgress,
  TaskResult,
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

const logger = getLogger();

const PERMISSION_TOOL_NAME = 'mcp__remote_agent__approve';

// ---------------------------------------------------------------------------
// Pending confirmation（替代 Python asyncio.Future）
// ---------------------------------------------------------------------------

interface PendingConfirmation {
  resolve: (value: string) => void;
  reject: (reason: unknown) => void;
  timer: ReturnType<typeof setTimeout>;
}

// ---------------------------------------------------------------------------
// ClaudeRemoteAgent
// ---------------------------------------------------------------------------

export class ClaudeRemoteAgent {
  readonly serverUrl: string;
  readonly agentToken: string;
  readonly clientId: string;

  private ws: WebSocket | null = null;
  readonly runnerManager: ClaudeRunnerManager;

  private _connected = false;
  private _registered = false;
  private _heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private _reconnectAttempts = 0;
  private _shutdown = false;

  private readonly _pendingConfirmations: Map<string, PendingConfirmation> = new Map();

  // MCP IPC
  private _ipcServer: net.Server | null = null;
  private _ipcHost = '127.0.0.1';
  private _ipcPort = 0;
  private _ipcToken = '';
  private _mcpConfigPath: string | null = null;
  private readonly _mcpClients: net.Socket[] = [];

  private readonly config: Config;

  constructor(config: Config) {
    this.config = config;
    this.serverUrl = config.agent.serverUrl;
    this.agentToken = config.agent.agentToken;
    this.clientId = config.agent.clientId;
    this.runnerManager = new ClaudeRunnerManager(3);
  }

  // ------------------------------------------------------------------ ws

  async connect(): Promise<boolean> {
    try {
      logger.info('Connecting to %s ...', this.serverUrl);

      const headers: Record<string, string> = {};
      if (this.agentToken) headers['Authorization'] = `Bearer ${this.agentToken}`;
      headers['X-Client-ID'] = this.clientId;
      headers['X-Client-Version'] = this.config.VERSION;

      this.ws = new WebSocket(this.serverUrl, { headers });

      return new Promise((resolve) => {
        this.ws!.on('open', () => {
          this._connected = true;
          logger.info('WebSocket connected successfully');
          this._sendRegistration();
          resolve(true);
        });

        this.ws!.on('error', (err) => {
          logger.error('Connection error: %s', (err as Error).message);
          this._connected = false;
          resolve(false);
        });

        this.ws!.on('message', (data: WebSocket.Data) => {
          this._handleMessage(data.toString()).catch((err) => {
            logger.error('handleMessage error: %s', err);
          });
        });

        this.ws!.on('close', () => {
          this._connected = false;
        });

        // ws 库自动处理 ping/pong
        this.ws!.on('ping', () => { /* auto pong handled by ws */ });
      });
    } catch (err) {
      logger.error('Connection failed: %s', err);
      this._connected = false;
      return false;
    }
  }

  private _sendRegistration(): void {
    try {
      const msg = buildRegisterMessage(
        this.clientId,
        this.config.VERSION,
        this.config.getClaudeVersion(),
        this.config.SUPPORTED_TOOLS,
      );
      this.sendMessage(msg);
      logger.info('Registration message sent');
    } catch (err) {
      logger.error('Failed to send registration: %s', err);
    }
  }

  sendMessage(message: Message): void {
    if (!this.ws || this._connected !== true) {
      logger.warn('Not connected, cannot send %s', message.type);
      return;
    }
    try {
      const jsonStr = messageToJson(message);
      logger.debug('Sending message: %s', message.type);
      this.ws.send(jsonStr);
    } catch (err) {
      logger.error('Failed to send message: %s', err);
      this._connected = false;
    }
  }

  // ------------------------------------------------------------------ confirm

  async requestUserConfirmation(request: UserConfirmationRequest): Promise<string> {
    const requestId = request.request_id;

    // 先注册 pending，再发送，最后等待
    const promise = new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pendingConfirmations.delete(requestId);
        resolve('timeout');
      }, request.timeout * 1000);

      this._pendingConfirmations.set(requestId, { resolve, reject, timer });
    });

    this.sendMessage(buildUserConfirmationRequest(request));
    logger.info('User confirmation requested: %s for task %s', requestId, request.task_id);

    return promise;
  }

  // ------------------------------------------------------------------ ipc

  async startIpcServer(): Promise<void> {
    if (this._ipcServer) return;

    this._ipcToken = crypto.randomBytes(24).toString('base64url');

    return new Promise((resolve, reject) => {
      const server = net.createServer((socket) => {
        this._handleIpcConnection(socket);
      });

      server.listen(0, this._ipcHost, () => {
        const addr = server.address() as net.AddressInfo;
        this._ipcPort = addr.port;
        this._ipcServer = server;
        logger.info('MCP IPC server listening on %s:%d', this._ipcHost, this._ipcPort);
        this._writeMcpConfig();
        resolve();
      });

      server.on('error', reject);
    });
  }

  private _writeMcpConfig(): void {
    const agentDir = path.resolve(__dirname);
    const mcpDir = path.join(agentDir, '..', 'data');
    fs.mkdirSync(mcpDir, { recursive: true });
    const cfgPath = path.join(mcpDir, `mcp_config_${this.clientId}.json`);

    const cfg = {
      mcpServers: {
        remote_agent: {
          command: process.execPath,
          args: [path.join(agentDir, 'permission-mcp.js')],
          env: {
            AGENT_IPC_HOST: this._ipcHost,
            AGENT_IPC_PORT: String(this._ipcPort),
            AGENT_IPC_TOKEN: this._ipcToken,
            NODE_OPTIONS: '',
          },
        },
      },
    };

    fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2), 'utf-8');
    this._mcpConfigPath = cfgPath;
    logger.info('MCP config written to %s', cfgPath);
  }

  private _handleIpcConnection(socket: net.Socket): void {
    const peer = `${socket.remoteAddress}:${socket.remotePort}`;
    logger.debug('Permission MCP IPC connected from %s', peer);

    let authed = false;
    this._mcpClients.push(socket);

    const rl = readline.createInterface({ input: socket });

    rl.on('line', (line) => {
      let frame: Record<string, unknown>;
      try {
        frame = JSON.parse(line);
      } catch (err) {
        logger.warn('Bad IPC frame: %s', err);
        return;
      }

      if (frame.token !== this._ipcToken) {
        logger.warn('IPC frame rejected: bad token');
        return;
      }

      const ftype = frame.type as string;
      if (ftype === 'hello') {
        authed = true;
        logger.debug('Permission MCP authed: role=%s', frame.role);
        return;
      }
      if (!authed) {
        logger.warn('IPC frame before hello, dropping: %s', ftype);
        return;
      }
      if (ftype === 'approve_request') {
        this._handleApproveRequest(frame, socket).catch((err) => {
          logger.error('handleApproveRequest error: %s', err);
        });
      } else {
        logger.debug('Unknown IPC frame type: %s', ftype);
      }
    });

    rl.on('error', (err) => {
      logger.debug('IPC readline error: %s', (err as Error).message);
    });

    rl.on('close', () => {
      const idx = this._mcpClients.indexOf(socket);
      if (idx >= 0) this._mcpClients.splice(idx, 1);
      try { socket.destroy(); } catch { /* already closed */ }
    });
  }

  private async _handleApproveRequest(
    frame: Record<string, unknown>,
    socket: net.Socket,
  ): Promise<void> {
    const requestId = (frame.request_id as string) || crypto.randomUUID();
    const toolName = String(frame.tool_name || '');
    const toolInput = (frame.tool_input as Record<string, unknown>) || {};
    const toolUseId = frame.tool_use_id as string | undefined;
    const timeout = Number(frame.timeout) || 600;

    const taskId = this._inferActiveTaskId() || toolUseId || 'unknown';

    const confirmationRequestId = `perm-${requestId}`;
    const confirm: UserConfirmationRequest = {
      request_id: confirmationRequestId,
      task_id: taskId,
      title: `工具确认: ${toolName || '未知工具'}`,
      message: 'Claude 正在请求使用以下工具，请确认是否允许。',
      prompt: this._formatToolInputPreview(toolName, toolInput),
      options: [
        { label: '允许', value: 'allow' },
        { label: '拒绝', value: 'deny' },
      ],
      timeout,
      source: 'permission_mcp',
      tool_name: toolName,
      tool_input: typeof toolInput === 'object' && !Array.isArray(toolInput) ? toolInput : { value: toolInput },
      tool_use_id: toolUseId,
    };

    const decision = await this.requestUserConfirmation(confirm);

    const responseFrame: Record<string, unknown> = {
      type: 'approve_response',
      request_id: requestId,
    };

    if (decision === 'allow') {
      responseFrame.behavior = 'allow';
      responseFrame.updated_input = toolInput;
    } else {
      responseFrame.behavior = 'deny';
      responseFrame.message = decision === 'timeout'
        ? 'permission request timed out'
        : `operator denied tool '${toolName}'`;
    }

    try {
      const data = JSON.stringify(responseFrame) + '\n';
      socket.write(data);
    } catch (err) {
      logger.error('Failed to send approve_response: %s', err);
    }
  }

  private _inferActiveTaskId(): string | null {
    const running = this.runnerManager.getRunningTasks();
    return running.length === 1 ? running[0] : null;
  }

  private _formatToolInputPreview(toolName: string, toolInput: unknown): string {
    let preview: string;
    try {
      preview = JSON.stringify(toolInput, null, 2);
    } catch {
      preview = String(toolInput);
    }
    if (preview.length > 1500) {
      preview = preview.slice(0, 1500) + '...';
    }
    return `${toolName}\n${preview}`;
  }

  // ------------------------------------------------------------------ heartbeat

  private _heartbeatLoop(): void {
    if (!this._connected || this._shutdown) return;

    try {
      const msg = buildHeartbeatMessage(
        this.runnerManager.getActiveCount() === 0 ? 'idle' : 'busy',
        this.runnerManager.getActiveCount(),
      );
      this.sendMessage(msg);
    } catch (err) {
      logger.error('Heartbeat error: %s', err);
    }
  }

  // ------------------------------------------------------------------ msg loop

  private async _handleMessage(rawMessage: string): Promise<void> {
    let message: Message;
    try {
      message = messageFromJson(rawMessage);
    } catch {
      logger.error('Invalid JSON message: %s', rawMessage.slice(0, 200));
      return;
    }

    logger.debug('Received message: %s', message.type);

    if (message.type === 'task.execute') {
      await this._handleTaskExecute(message);
    } else if (message.type === 'task.cancel') {
      await this._handleTaskCancel(message);
    } else if (message.type === 'heartbeat.ack') {
      // 忽略
    } else if (message.type === 'agent.register_ack') {
      this._registered = true;
      logger.info('Registration acknowledged by server');
    } else if (message.type === 'user_confirmation.response') {
      const requestId = message.payload.request_id as string;
      const entry = this._pendingConfirmations.get(requestId);
      if (entry) {
        clearTimeout(entry.timer);
        this._pendingConfirmations.delete(requestId);
        entry.resolve(message.payload.value as string);
        logger.info('User confirmation received for %s: %s', requestId, message.payload.value);
      } else {
        logger.warn('Unknown or completed confirmation request: %s', requestId);
      }
    } else {
      logger.warn('Unknown message type: %s', message.type);
    }
  }

  private async _handleTaskExecute(message: Message): Promise<void> {
    const taskId = message.id;
    if (!taskId) {
      logger.error('Task message missing ID');
      this.sendMessage(buildErrorMessage('Task ID is required', 'MISSING_TASK_ID'));
      return;
    }

    try {
      const payload = message.payload as unknown as TaskPayload;
      logger.info('Received task %s: %s ...', taskId, (payload.prompt || '').slice(0, 60));
      // 异步执行任务
      this._executeTask(taskId, payload).catch((err) => {
        logger.error('Task execution error: %s', err);
        this.sendMessage(buildTaskFailedMessage(taskId, String(err), 'INTERNAL_ERROR'));
      });
    } catch (err) {
      logger.error('Failed to parse task payload: %s', err);
      this.sendMessage(buildTaskFailedMessage(taskId, `Invalid task payload: ${err}`, 'INVALID_PAYLOAD'));
    }
  }

  private async _handleTaskCancel(message: Message): Promise<void> {
    const taskId = message.id;
    if (!taskId) return;
    logger.info('Cancel requested for task %s', taskId);
    if (this.runnerManager.cancelTask(taskId)) {
      this.sendMessage(buildTaskCancelledMessage(taskId));
    } else {
      logger.warn('Task %s not found or already completed', taskId);
    }
  }

  // ------------------------------------------------------------------ task

  private async _executeTask(taskId: string, payload: TaskPayload): Promise<void> {
    try {
      this.sendMessage(buildTaskStartedMessage(taskId));

      const seqCounter = { value: 0 };

      const eventCallback: EventCallback = async (eventType, evtPayload) => {
        seqCounter.value += 1;
        const msg = buildTaskEventMessage(taskId, seqCounter.value, eventType, evtPayload);
        this.sendMessage(msg);
      };

      const progressCallback: ProgressCallback = async (progress) => {
        this.sendMessage(buildTaskProgressMessage(taskId, progress));
      };

      const permissionMode = this.config.claude.permissionMode || 'default';
      const autoApproveTools = [...(this.config.claude.autoApproveTools || [])];

      const mcpConfig = this._mcpConfigPath || undefined;

      // 合并配置默认值，确保 options 有有效值
      const options = {
        model: payload.options?.model || this.config.claude.model,
        max_turns: payload.options?.max_turns || this.config.claude.maxTurns,
        allowed_tools: payload.options?.allowed_tools || null,
        output_format: payload.options?.output_format || 'text',
        timeout: payload.options?.timeout || this.config.claude.timeout,
        continue_last: payload.options?.continue_last || false,
        session_id: payload.options?.session_id || null,
        mode: payload.options?.mode || 'default',
      };

      const result = await this.runnerManager.runTask({
        taskId,
        prompt: payload.prompt,
        options,
        context: payload.context,
        workdir: payload.workdir,
        progressCallback,
        eventCallback,
        mcpConfigPath: mcpConfig,
        permissionTool: mcpConfig ? PERMISSION_TOOL_NAME : undefined,
        permissionMode,
        autoApproveTools: autoApproveTools.length > 0 ? autoApproveTools : undefined,
      });

      if (result.success) {
        this.sendMessage(buildTaskCompletedMessage(taskId, result));
      } else {
        this.sendMessage(buildTaskFailedMessage(
          taskId,
          result.result || 'Task execution failed',
          'EXECUTION_FAILED',
          result.result,
        ));
      }
    } catch (err) {
      logger.error('Task execution error: %s', err);
      this.sendMessage(buildTaskFailedMessage(taskId, String(err), 'INTERNAL_ERROR'));
    }
  }

  // ------------------------------------------------------------------ loops

  async start(): Promise<void> {
    logger.info('Starting Claude Remote Agent v%s', this.config.VERSION);
    logger.info('Client ID: %s', this.clientId);

    await this.startIpcServer();

    while (!this._shutdown) {
      const connected = await this.connect();

      if (connected) {
        this._reconnectAttempts = 0;
        this._heartbeatTimer = setInterval(
          () => this._heartbeatLoop(),
          this.config.agent.heartbeatInterval * 1000,
        );

        // 消息循环：等待连接关闭
        await new Promise<void>((resolve) => {
          if (!this.ws) { resolve(); return; }

          this.ws!.on('close', () => {
            this._connected = false;
            resolve();
          });

          this.ws!.on('error', () => {
            this._connected = false;
            resolve();
          });
        });

        if (this._heartbeatTimer) {
          clearInterval(this._heartbeatTimer);
          this._heartbeatTimer = null;
        }
      }

      if (this._shutdown) break;

      this._reconnectAttempts++;
      if (
        this.config.agent.maxReconnectAttempts > 0 &&
        this._reconnectAttempts >= this.config.agent.maxReconnectAttempts
      ) {
        logger.error('Max reconnect attempts reached, exiting');
        break;
      }

      logger.info(
        'Reconnecting in %ds (attempt %d) ...',
        this.config.agent.reconnectDelay,
        this._reconnectAttempts,
      );
      await new Promise((r) => setTimeout(r, this.config.agent.reconnectDelay * 1000));
    }
  }

  async shutdown(): Promise<void> {
    logger.info('Shutting down ...');
    this._shutdown = true;

    // 取消所有运行中的任务
    for (const taskId of this.runnerManager.getRunningTasks()) {
      this.runnerManager.cancelTask(taskId);
    }

    // resolve 挂起的确认
    for (const [id, entry] of this._pendingConfirmations) {
      clearTimeout(entry.timer);
      entry.resolve('cancelled');
    }
    this._pendingConfirmations.clear();

    // 关闭 MCP 客户端
    for (const sock of this._mcpClients) {
      try { sock.destroy(); } catch { /* */ }
    }
    this._mcpClients.length = 0;

    // 关闭 IPC 服务
    if (this._ipcServer) {
      this._ipcServer.close();
      this._ipcServer = null;
    }

    // 关闭 WebSocket
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }

    logger.info('Shutdown complete');
  }

  isConnected(): boolean {
    return this._connected;
  }

  isRegistered(): boolean {
    return this._registered;
  }
}
