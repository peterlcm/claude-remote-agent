/**
 * IPC 客户端 — TCP 回环 NDJSON 客户端，用于 permission-mcp 与主进程通信
 *
 * 移植自 Python 版 permission_mcp.py IpcClient
 */

import * as net from 'net';
import * as readline from 'readline';
import * as crypto from 'crypto';

function debugLog(msg: string): void {
  if (process.env.AGENT_MCP_DEBUG === '1') {
    process.stderr.write(`[ipc-client] ${msg}\n`);
  }
}

interface PendingEntry {
  resolve: (value: Record<string, unknown>) => void;
  reject: (reason: unknown) => void;
  timer: ReturnType<typeof setTimeout>;
}

export class IpcClient {
  private readonly host: string;
  private readonly port: number;
  private readonly token: string;
  private socket: net.Socket | null = null;
  private connected = false;
  private readonly pending: Map<string, PendingEntry> = new Map();
  private writeLock: Promise<void> = Promise.resolve();
  private rl: readline.Interface | null = null;

  constructor(host: string, port: number, token: string) {
    this.host = host;
    this.port = port;
    this.token = token;
  }

  async connect(): Promise<void> {
    if (this.socket && this.connected) return;

    return new Promise((resolve, reject) => {
      const sock = net.createConnection({ host: this.host, port: this.port });
      let settled = false;

      sock.on('connect', () => {
        this.socket = sock;
        this.connected = true;

        // 读取响应
        this.rl = readline.createInterface({ input: sock });
        this.rl.on('line', (line) => {
          try {
            const frame = JSON.parse(line) as Record<string, unknown>;
            const reqId = frame.request_id as string | undefined;
            if (frame.type === 'approve_response' && reqId && this.pending.has(reqId)) {
              const entry = this.pending.get(reqId)!;
              clearTimeout(entry.timer);
              this.pending.delete(reqId);
              entry.resolve(frame);
            }
          } catch (err) {
            debugLog(`bad frame: ${err}`);
          }
        });
        this.rl.on('error', (err) => {
          debugLog(`readline error: ${(err as Error).message}`);
        });

        // 发送 hello 帧
        const hello = { type: 'hello', token: this.token, role: 'permission_mcp' };
        this.sendRaw(hello).then(() => {
          debugLog(`connected to agent at ${this.host}:${this.port}`);
          settled = true;
          resolve();
        }).catch((err) => {
          if (!settled) { settled = true; reject(err); }
        });
      });

      sock.on('error', (err) => {
        this.connected = false;
        if (!settled) { settled = true; reject(err); }
      });

      sock.on('close', () => {
        this.connected = false;
        // 清理所有挂起的请求
        for (const [, entry] of this.pending) {
          clearTimeout(entry.timer);
          entry.reject(new Error('IPC connection closed'));
        }
        this.pending.clear();
      });
    });
  }

  async requestApproval(
    toolName: string,
    toolInput: Record<string, unknown>,
    toolUseId?: string,
    timeout: number = 600,
  ): Promise<Record<string, unknown>> {
    await this.connect();

    const requestId = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(requestId);
        resolve({ behavior: 'deny', message: 'permission request timed out' });
      }, (timeout + 5) * 1000);

      this.pending.set(requestId, { resolve, reject, timer });

      const frame = {
        type: 'approve_request',
        token: this.token,
        request_id: requestId,
        tool_name: toolName,
        tool_input: toolInput,
        tool_use_id: toolUseId ?? null,
        timeout,
        ts: Date.now() / 1000,
      };

      this.sendRaw(frame).catch((err) => {
        this.pending.delete(requestId);
        clearTimeout(timer);
        reject(err);
      });
    });
  }

  private async sendRaw(frame: Record<string, unknown>): Promise<void> {
    if (!this.socket || !this.connected) {
      throw new Error('IPC not connected');
    }
    const data = JSON.stringify(frame) + '\n';
    // 序列化写入，防止交错
    const prev = this.writeLock;
    let release: () => void;
    this.writeLock = new Promise<void>((r) => { release = r; });
    await prev;
    try {
      this.socket.write(data);
    } finally {
      release!();
    }
  }

  close(): void {
    if (this.rl) {
      this.rl.close();
      this.rl = null;
    }
    if (this.socket) {
      this.socket.destroy();
      this.socket = null;
      this.connected = false;
    }
  }
}
