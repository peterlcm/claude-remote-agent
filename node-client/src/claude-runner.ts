/**
 * Claude Code 执行器 — 基于 stream-json 协议的 NDJSON 事件流
 *
 * 移植自 Python 版 claude_runner.py
 */

import { spawn, ChildProcess } from 'child_process';
import * as readline from 'readline';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import { getLogger } from './logger';
import { Semaphore, withSemaphore } from './semaphore';
import {
  TaskOptions,
  TaskResult,
  TaskProgress,
} from './protocol';

const logger = getLogger();

// ---------------------------------------------------------------------------
// 回调类型
// ---------------------------------------------------------------------------

export type EventCallback = (eventType: string, payload: Record<string, unknown>) => Promise<void>;
export type ProgressCallback = (progress: TaskProgress) => Promise<void>;

// ---------------------------------------------------------------------------
// ClaudeRunner
// ---------------------------------------------------------------------------

export class ClaudeRunner {
  readonly workdir: string;
  private _currentProcess: ChildProcess | null = null;
  private _taskId: string | null = null;
  private _cancelled = false;

  constructor(workdir: string = '.') {
    this.workdir = path.resolve(workdir);
  }

  async run(params: {
    prompt: string;
    options: TaskOptions;
    context?: string | null;
    progressCallback?: ProgressCallback | null;
    eventCallback?: EventCallback | null;
    workdir?: string | null;
    taskId?: string | null;
    mcpConfigPath?: string | null;
    permissionTool?: string | null;
    permissionMode?: string;
    autoApproveTools?: string[] | null;
  }): Promise<TaskResult> {
    const {
      prompt, options, context,
      progressCallback, eventCallback,
      taskId, mcpConfigPath, permissionTool,
      permissionMode = 'default',
      autoApproveTools,
    } = params;

    const startTime = Date.now();
    this._cancelled = false;
    this._taskId = taskId ?? null;

    const currentWorkdir = params.workdir
      ? path.resolve(params.workdir)
      : this.workdir;

    try {
      const cmd = this._buildCommand(
        prompt, options, context,
        mcpConfigPath, permissionTool, permissionMode, autoApproveTools,
      );
      cmd[0] = this._resolveExecutable(cmd[0]);

      logger.info('Executing Claude command: %s ...', cmd.slice(0, 4).join(' '));
      logger.info('Workdir: %s', currentWorkdir);

      const isWindowsBatch = process.platform === 'win32' &&
        /\.(cmd|bat|ps1)$/i.test(cmd[0]);

      const child = spawn(cmd[0], cmd.slice(1), {
        cwd: currentWorkdir,
        stdio: ['pipe', 'pipe', 'pipe'],
        env: this._getEnv(),
        shell: isWindowsBatch,
        windowsHide: true,
      });

      // Close stdin immediately to avoid Claude CLI's 3s wait warning
      child.stdin?.end();
      this._currentProcess = child;

      const state: Record<string, unknown> = {
        session_id: null,
        model: null,
        turn: 0,
        result_event: null,
        assistant_text_chunks: [] as string[],
        stderr_chunks: [] as string[],
      };

      const stdoutPromise = this._consumeStdout(
        child, options, state, eventCallback ?? null, progressCallback ?? null,
      );
      const stderrPromise = this._consumeStderr(
        child, state, eventCallback ?? null,
      );
      const exitPromise = new Promise<void>((resolve) => {
        child.on('close', () => resolve());
      });

      // 超时控制
      const timeoutMs = (options.timeout || 300) * 1000;
      let timedOut = false;

      const allDone = Promise.all([stdoutPromise, stderrPromise, exitPromise]);

      const timeoutPromise = new Promise<void>((_, reject) => {
        setTimeout(() => {
          timedOut = true;
          if (child.pid != null) {
            try { child.kill('SIGKILL'); } catch { /* already dead */ }
          }
          reject(new Error('timeout'));
        }, timeoutMs);
      });

      try {
        await Promise.race([allDone, timeoutPromise]);
      } catch (e) {
        if (timedOut || (e as Error).message === 'timeout') {
          logger.error('Claude execution timed out after %ds', options.timeout);
          child.kill('SIGKILL');
          return this._buildTimeoutResult(state, startTime);
        }
        throw e;
      }

      const returnCode = child.exitCode ?? -1;
      return this._buildResult(state, returnCode, startTime);

    } catch (e) {
      if ((e as Error).name === 'AbortError' || this._cancelled) {
        logger.info('Task cancelled: %s', this._taskId);
        this._killProcess();
        return {
          success: false,
          result: 'cancelled',
          structured_output: null,
          usage: {},
          duration_ms: Date.now() - startTime,
          num_turns: 0,
          session_id: null,
        };
      }
      logger.error('Claude execution error: %s', e);
      this._killProcess();
      return {
        success: false,
        result: `runner error: ${e}`,
        structured_output: null,
        usage: {},
        duration_ms: Date.now() - startTime,
        num_turns: 0,
        session_id: null,
      };
    } finally {
      this._currentProcess = null;
    }
  }

  cancel(): void {
    this._cancelled = true;
    if (this._currentProcess && this._currentProcess.pid != null) {
      logger.info('Killing Claude process for task %s', this._taskId);
      try { this._currentProcess.kill('SIGKILL'); } catch { /* already dead */ }
    }
  }

  isRunning(): boolean {
    return this._currentProcess != null && this._currentProcess.pid != null && !this._currentProcess.killed;
  }

  private _killProcess(): void {
    if (this._currentProcess && this._currentProcess.pid != null && !this._currentProcess.killed) {
      try { this._currentProcess.kill('SIGKILL'); } catch { /* already dead */ }
    }
  }

  // ------------------------------------------------------------------ stream

  private _consumeStdout(
    child: ChildProcess,
    options: TaskOptions,
    state: Record<string, unknown>,
    eventCallback: EventCallback | null,
    progressCallback: ProgressCallback | null,
  ): Promise<void> {
    return new Promise((resolve) => {
      const stdout = child.stdout;
      if (!stdout) { resolve(); return; }

      const rl = readline.createInterface({ input: stdout });
      rl.on('line', (line) => {
        const text = line.trim();
        if (!text) return;
        let event: Record<string, unknown>;
        try {
          event = JSON.parse(text);
        } catch {
          logger.debug('Non-JSON line on Claude stdout: %s', text.slice(0, 200));
          if (eventCallback) {
            this._safeEmit(eventCallback, 'stdout_text', { text });
          }
          return;
        }
        this._dispatchEvent(event, state, options, eventCallback, progressCallback)
          .catch((err) => logger.error('dispatch error: %s', err));
      });
      rl.on('error', (err) => {
        logger.debug('stdout readline error: %s', (err as Error).message);
      });
      rl.on('close', () => resolve());
    });
  }

  private _consumeStderr(
    child: ChildProcess,
    state: Record<string, unknown>,
    eventCallback: EventCallback | null,
  ): Promise<void> {
    return new Promise((resolve) => {
      const stderr = child.stderr;
      if (!stderr) { resolve(); return; }

      const rl = readline.createInterface({ input: stderr });
      rl.on('line', (line) => {
        const text = line.trimEnd();
        if (!text) return;
        (state.stderr_chunks as string[]).push(text);
        logger.debug('claude stderr: %s', text);
        if (eventCallback) {
          this._safeEmit(eventCallback, 'stderr', { text });
        }
      });
      rl.on('error', (err) => {
        logger.debug('stderr readline error: %s', (err as Error).message);
      });
      rl.on('close', () => resolve());
    });
  }

  // ------------------------------------------------------------------ dispatch

  private async _dispatchEvent(
    event: Record<string, unknown>,
    state: Record<string, unknown>,
    options: TaskOptions,
    eventCallback: EventCallback | null,
    progressCallback: ProgressCallback | null,
  ): Promise<void> {
    const evtType = event.type as string | undefined;
    const subtype = event.subtype as string | undefined;

    if (evtType === 'system' && subtype === 'init') {
      state.session_id = (event as Record<string, unknown>).session_id ?? state.session_id;
      state.model = (event as Record<string, unknown>).model ?? state.model;
      await this._emitEvent(eventCallback, 'session_init', {
        session_id: state.session_id,
        model: state.model,
        permission_mode: (event as Record<string, unknown>).permissionMode,
        tools: (event as Record<string, unknown>).tools,
        mcp_servers: (event as Record<string, unknown>).mcp_servers,
        cwd: (event as Record<string, unknown>).cwd,
      });
      await this._emitProgress(progressCallback, options, state, 'thinking');
    } else if (evtType === 'system' && subtype === 'api_retry') {
      await this._emitEvent(eventCallback, 'api_retry', {
        attempt: (event as Record<string, unknown>).attempt,
        max_retries: (event as Record<string, unknown>).max_retries,
        retry_delay_ms: (event as Record<string, unknown>).retry_delay_ms,
        error_status: (event as Record<string, unknown>).error_status,
        error: (event as Record<string, unknown>).error,
      });
    } else if (evtType === 'system') {
      await this._emitEvent(eventCallback, `system_${subtype || 'event'}`, event);
    } else if (evtType === 'stream_event') {
      await this._handleStreamEvent(event as Record<string, unknown>, eventCallback);
    } else if (evtType === 'assistant') {
      state.turn = ((state.turn as number) || 0) + 1;
      const message = (event as Record<string, unknown>).message as Record<string, unknown> || {};
      const content = (message.content as unknown[]) || [];
      await this._emitEvent(eventCallback, 'assistant_message', {
        message_id: message.id,
        model: message.model,
        stop_reason: message.stop_reason,
        usage: message.usage,
        content,
        turn: state.turn,
      });
      for (const block of content as Record<string, unknown>[]) {
        if (block && typeof block === 'object' && block.type === 'text') {
          (state.assistant_text_chunks as string[]).push((block.text as string) || '');
        }
      }
      await this._emitProgress(progressCallback, options, state, 'working');
    } else if (evtType === 'user') {
      const message = (event as Record<string, unknown>).message as Record<string, unknown> || {};
      const content = (message.content as unknown[]) || [];
      await this._emitEvent(eventCallback, 'tool_result', {
        content,
        message_id: message.id,
      });
    } else if (evtType === 'rate_limit_event') {
      await this._emitEvent(eventCallback, 'rate_limit', {
        rate_limit_info: (event as Record<string, unknown>).rate_limit_info,
      });
    } else if (evtType === 'result') {
      state.result_event = event;
      await this._emitEvent(eventCallback, 'result', {
        subtype: (event as Record<string, unknown>).subtype,
        result: (event as Record<string, unknown>).result,
        is_error: (event as Record<string, unknown>).is_error,
        session_id: (event as Record<string, unknown>).session_id,
        duration_ms: (event as Record<string, unknown>).duration_ms,
        duration_api_ms: (event as Record<string, unknown>).duration_api_ms,
        num_turns: (event as Record<string, unknown>).num_turns,
        total_cost_usd: (event as Record<string, unknown>).total_cost_usd,
        usage: (event as Record<string, unknown>).usage,
      });
      const isError = Boolean((event as Record<string, unknown>).is_error);
      await this._emitProgress(
        progressCallback, options, state,
        isError ? 'failed' : 'completed',
      );
    } else {
      await this._emitEvent(eventCallback, evtType || 'unknown', event);
    }
  }

  private async _handleStreamEvent(
    event: Record<string, unknown>,
    eventCallback: EventCallback | null,
  ): Promise<void> {
    const inner = (event.event as Record<string, unknown>) || {};
    const innerType = inner.type as string | undefined;
    const delta = (inner.delta as Record<string, unknown>) || {};
    const index = inner.index as number | undefined;
    const contentBlock = (inner.content_block as Record<string, unknown>) || {};

    if (innerType === 'message_start') {
      await this._emitEvent(eventCallback, 'message_start', { message: inner.message });
    } else if (innerType === 'content_block_start') {
      await this._emitEvent(eventCallback, 'content_block_start', { index, content_block: contentBlock });
    } else if (innerType === 'content_block_delta') {
      const dType = delta.type as string | undefined;
      if (dType === 'text_delta') {
        await this._emitEvent(eventCallback, 'text_delta', { index, text: delta.text || '' });
      } else if (dType === 'input_json_delta') {
        await this._emitEvent(eventCallback, 'tool_input_delta', { index, partial_json: delta.partial_json || '' });
      } else {
        await this._emitEvent(eventCallback, `delta_${dType || 'unknown'}`, { index, delta });
      }
    } else if (innerType === 'content_block_stop') {
      await this._emitEvent(eventCallback, 'content_block_stop', { index });
    } else if (innerType === 'message_delta') {
      await this._emitEvent(eventCallback, 'message_delta', { delta, usage: inner.usage });
    } else if (innerType === 'message_stop') {
      await this._emitEvent(eventCallback, 'message_stop', {});
    } else {
      await this._emitEvent(eventCallback, `stream_${innerType || 'unknown'}`, inner);
    }
  }

  // ------------------------------------------------------------------ helpers

  private async _emitEvent(
    eventCallback: EventCallback | null,
    eventType: string,
    payload: Record<string, unknown>,
  ): Promise<void> {
    if (!eventCallback) return;
    await this._safeEmit(eventCallback, eventType, payload);
  }

  private async _safeEmit(
    eventCallback: EventCallback,
    eventType: string,
    payload: Record<string, unknown>,
  ): Promise<void> {
    try {
      await eventCallback(eventType, payload);
    } catch (err) {
      logger.error('event_callback failed for %s: %s', eventType, err);
    }
  }

  private async _emitProgress(
    progressCallback: ProgressCallback | null,
    options: TaskOptions,
    state: Record<string, unknown>,
    status: string,
  ): Promise<void> {
    if (!progressCallback) return;
    const progress: TaskProgress = {
      turn: (state.turn as number) || 0,
      max_turns: options.max_turns,
      status,
      message: null,
    };
    try {
      await progressCallback(progress);
    } catch (err) {
      logger.error('progress_callback failed: %s', err);
    }
  }

  // ------------------------------------------------------------------ command

  private _buildCommand(
    prompt: string,
    options: TaskOptions,
    context: string | null | undefined,
    mcpConfigPath: string | null | undefined,
    permissionTool: string | null | undefined,
    permissionMode: string,
    autoApproveTools: string[] | null | undefined,
  ): string[] {
    let fullPrompt = prompt;
    if (context) {
      fullPrompt = `${prompt}\n\n[Context]\n${context}`;
    }

    const cmd: string[] = [
      'claude', '-p', fullPrompt,
      '--output-format', 'stream-json',
      '--verbose',
      '--include-partial-messages',
    ];

    if (options.model) cmd.push('--model', options.model);
    if (options.max_turns) cmd.push('--max-turns', String(options.max_turns));
    // 注意：Haiku 模型不支持 reasoning_effort 参数
    const model = options.model || '';
    const isHaiku = model.toLowerCase().includes('haiku');
    const validEfforts = ['low', 'medium', 'high', 'max'];
    if (!isHaiku && options.effort && validEfforts.includes(options.effort)) {
      cmd.push('--effort', options.effort);
    }

    const allowed = [...(options.allowed_tools || [])];
    if (autoApproveTools) {
      for (const tool of autoApproveTools) {
        if (tool && !allowed.includes(tool)) allowed.push(tool);
      }
    }
    if (allowed.length > 0) {
      cmd.push('--allowedTools', allowed.join(','));
    }

    if (mcpConfigPath) cmd.push('--mcp-config', mcpConfigPath);
    if (permissionTool) cmd.push('--permission-prompt-tool', permissionTool);
    if (permissionMode) cmd.push('--permission-mode', permissionMode);

    if (options.continue_last) cmd.push('--continue');
    if (options.session_id) cmd.push('--resume', options.session_id);

    return cmd;
  }

  private _resolveExecutable(exe: string): string {
    if (path.isAbsolute(exe) && fs.existsSync(exe)) return exe;

    if (process.platform !== 'win32') {
      return this._which(exe) || exe;
    }

    // Windows: 优先 .cmd / .bat / .exe
    for (const ext of ['.cmd', '.bat', '.exe']) {
      const candidate = this._which(exe + ext);
      if (candidate) return candidate;
    }

    const candidate = this._which(exe);
    if (candidate && candidate.toLowerCase().endsWith('.ps1')) {
      for (const ext of ['.cmd', '.bat', '.exe']) {
        const sibling = candidate.replace(/\.ps1$/i, ext);
        if (fs.existsSync(sibling)) return sibling;
      }
      logger.warn(
        { exe, candidate },
        'Resolved %s but it cannot be launched directly; subprocess does not support PowerShell scripts',
      );
    }
    return candidate || exe;
  }

  /** 简易 PATH 查找 */
  private _which(exe: string): string | null {
    const pathEnv = process.env.PATH || '';
    const sep = process.platform === 'win32' ? ';' : ':';
    for (const dir of pathEnv.split(sep)) {
      const candidate = path.join(dir, exe);
      try {
        if (fs.statSync(candidate).isFile()) return candidate;
      } catch { /* not found */ }
    }
    return null;
  }

  private _getEnv(): Record<string, string> {
    const env: Record<string, string> = {};
    for (const [k, v] of Object.entries(process.env)) {
      if (v !== undefined) env[k] = v;
    }
    env.PYTHONIOENCODING = 'utf-8';
    env.LANG = env.LANG || 'en_US.UTF-8';
    env.LC_ALL = env.LC_ALL || 'en_US.UTF-8';
    return env;
  }

  // ------------------------------------------------------------------ result

  private _buildResult(
    state: Record<string, unknown>,
    returnCode: number | null,
    startTime: number,
  ): TaskResult {
    const resultEvent = (state.result_event as Record<string, unknown>) || {};
    const isError = Boolean(resultEvent.is_error);
    const success = (returnCode === 0) && !isError && Boolean(resultEvent);

    const text = (resultEvent.result as string) ||
      (state.assistant_text_chunks as string[]).join('') || '';

    let usage: Record<string, unknown> = (resultEvent.usage as Record<string, unknown>) || {};
    if ('total_cost_usd' in resultEvent) {
      usage = { ...usage };
      if (!('total_cost_usd' in usage)) {
        usage.total_cost_usd = resultEvent.total_cost_usd;
      }
    }

    return {
      success,
      result: text || '',
      structured_output: Object.keys(resultEvent).length > 0 ? resultEvent : null,
      usage,
      duration_ms: Date.now() - startTime,
      num_turns: (resultEvent.num_turns as number) || (state.turn as number) || 0,
      session_id: (resultEvent.session_id as string) || (state.session_id as string) || null,
    };
  }

  private _buildTimeoutResult(
    state: Record<string, unknown>,
    startTime: number,
  ): TaskResult {
    const text = (state.assistant_text_chunks as string[]).join('') || 'Claude execution timed out';
    return {
      success: false,
      result: text,
      structured_output: { type: 'timeout' },
      usage: {},
      duration_ms: Date.now() - startTime,
      num_turns: (state.turn as number) || 0,
      session_id: (state.session_id as string) || null,
    };
  }
}

// ---------------------------------------------------------------------------
// ClaudeRunnerManager
// ---------------------------------------------------------------------------

interface RunnerEntry {
  runner: ClaudeRunner;
  abort: AbortController;
}

export class ClaudeRunnerManager {
  private readonly maxConcurrent: number;
  private readonly semaphore: Semaphore;
  private readonly runners: Map<string, RunnerEntry> = new Map();

  constructor(maxConcurrent: number = 3) {
    this.maxConcurrent = maxConcurrent;
    this.semaphore = new Semaphore(maxConcurrent);
  }

  async runTask(params: {
    taskId: string;
    prompt: string;
    options: TaskOptions;
    context?: string | null;
    workdir?: string;
    progressCallback?: ProgressCallback | null;
    eventCallback?: EventCallback | null;
    mcpConfigPath?: string | null;
    permissionTool?: string | null;
    permissionMode?: string;
    autoApproveTools?: string[] | null;
  }): Promise<TaskResult> {
    return withSemaphore(this.semaphore, async () => {
      const runner = new ClaudeRunner(params.workdir || '.');
      const abort = new AbortController();
      this.runners.set(params.taskId, { runner, abort });

      try {
        return await runner.run({
          prompt: params.prompt,
          options: params.options,
          context: params.context,
          progressCallback: params.progressCallback,
          eventCallback: params.eventCallback,
          workdir: params.workdir,
          taskId: params.taskId,
          mcpConfigPath: params.mcpConfigPath,
          permissionTool: params.permissionTool,
          permissionMode: params.permissionMode,
          autoApproveTools: params.autoApproveTools,
        });
      } finally {
        this.runners.delete(params.taskId);
      }
    });
  }

  cancelTask(taskId: string): boolean {
    const entry = this.runners.get(taskId);
    if (!entry) return false;
    entry.runner.cancel();
    entry.abort.abort();
    return true;
  }

  getActiveCount(): number {
    return this.runners.size;
  }

  getRunningTasks(): string[] {
    return [...this.runners.keys()];
  }
}
