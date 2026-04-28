#!/usr/bin/env node
/**
 * MCP 权限服务器 — 独立入口，由 Claude CLI 作为子进程启动
 *
 * 实现 JSON-RPC 2.0 over stdio，通过 TCP 回环 IPC 与主代理进程通信。
 * 移植自 Python 版 permission_mcp.py
 */

import * as readline from 'readline';
import * as os from 'os';
import { IpcClient } from './ipc-client';

const PROTOCOL_VERSION = '2024-11-05';
const SERVER_NAME = 'remote-agent-permission';
const SERVER_VERSION = '1.0.0';

function debugLog(msg: string): void {
  if (process.env.AGENT_MCP_DEBUG === '1') {
    process.stderr.write(`[permission_mcp] ${msg}\n`);
  }
}

// ---------------------------------------------------------------------------
// StdioMcpServer
// ---------------------------------------------------------------------------

class StdioMcpServer {
  private ipc: IpcClient;
  private writeLock: Promise<void> = Promise.resolve();

  constructor(ipc: IpcClient) {
    this.ipc = ipc;
  }

  async serve(): Promise<void> {
    return new Promise((resolve) => {
      const rl = readline.createInterface({ input: process.stdin });

      rl.on('line', (line) => {
        const text = line.trim();
        if (!text) return;
        let msg: Record<string, unknown>;
        try {
          msg = JSON.parse(text);
        } catch (e) {
          debugLog(`stdin non-json: ${e}`);
          return;
        }
        this.dispatch(msg).catch((err) => {
          debugLog(`dispatch error: ${err}`);
        });
      });

      rl.on('close', () => {
        debugLog('stdin EOF, exiting');
        resolve();
      });
    });
  }

  private async dispatch(msg: Record<string, unknown>): Promise<void> {
    const method = msg.method as string | undefined;
    const msgId = msg.id;

    try {
      if (method === 'initialize') {
        await this.respond(msgId, {
          protocolVersion: PROTOCOL_VERSION,
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
        });
      } else if (method === 'notifications/initialized') {
        return;
      } else if (method === 'tools/list') {
        await this.respond(msgId, {
          tools: [this.approveToolDescriptor()],
        });
      } else if (method === 'tools/call') {
        const params = (msg.params as Record<string, unknown>) || {};
        const name = params.name as string;
        const arguments_ = (params.arguments as Record<string, unknown>) || {};
        if (name !== 'approve') {
          await this.respondError(msgId, -32601, `unknown tool: ${name}`);
          return;
        }
        const resultText = await this.handleApprove(arguments_);
        await this.respond(msgId, {
          content: [{ type: 'text', text: resultText }],
          isError: false,
        });
      } else if (method === 'ping') {
        await this.respond(msgId, {});
      } else if (msgId == null) {
        // 未知通知，静默丢弃
        return;
      } else {
        await this.respondError(msgId, -32601, `method not found: ${method}`);
      }
    } catch (err) {
      debugLog(`dispatch error on ${method}: ${err}`);
      if (msgId != null) {
        await this.respondError(msgId, -32603, `internal error: ${err}`);
      }
    }
  }

  private approveToolDescriptor(): Record<string, unknown> {
    return {
      name: 'approve',
      description:
        'Ask the Remote Agent operator whether Claude may run the ' +
        'requested tool with the given input. Returns an allow/deny ' +
        'decision in the format expected by --permission-prompt-tool.',
      inputSchema: {
        type: 'object',
        properties: {
          tool_name: { type: 'string' },
          input: { type: 'object' },
          tool_use_id: { type: 'string' },
        },
        required: ['tool_name', 'input'],
      },
    };
  }

  private async handleApprove(args: Record<string, unknown>): Promise<string> {
    const toolName = String(args.tool_name || '');
    let toolInput = args.input || {};
    if (typeof toolInput !== 'object' || Array.isArray(toolInput)) {
      toolInput = { value: toolInput };
    }
    const toolUseId = args.tool_use_id as string | undefined;

    try {
      const response = await this.ipc.requestApproval(
        toolName,
        toolInput as Record<string, unknown>,
        toolUseId,
      );

      const behavior = (response.behavior as string) || 'deny';
      if (behavior === 'allow') {
        const updated = (response.updated_input || response.updatedInput || toolInput) as Record<string, unknown>;
        return JSON.stringify({ behavior: 'allow', updatedInput: updated });
      }
      const message = (response.message as string) || 'denied by operator';
      return JSON.stringify({ behavior: 'deny', message });
    } catch (err) {
      debugLog(`approval ipc error: ${err}`);
      return JSON.stringify({
        behavior: 'deny',
        message: `permission ipc error: ${err}`,
      });
    }
  }

  private async respond(msgId: unknown, result: Record<string, unknown>): Promise<void> {
    await this.writeFrame({ jsonrpc: '2.0', id: msgId, result });
  }

  private async respondError(msgId: unknown, code: number, message: string): Promise<void> {
    await this.writeFrame({
      jsonrpc: '2.0',
      id: msgId,
      error: { code, message },
    });
  }

  private async writeFrame(frame: Record<string, unknown>): Promise<void> {
    const data = JSON.stringify(frame) + '\n';
    // 序列化写入，防止交错
    const prev = this.writeLock;
    let release: () => void;
    this.writeLock = new Promise<void>((r) => { release = r; });
    await prev;
    try {
      process.stdout.write(data);
    } finally {
      release!();
    }
  }
}

// ---------------------------------------------------------------------------
// 主函数
// ---------------------------------------------------------------------------

async function main(): Promise<number> {
  const host = process.env.AGENT_IPC_HOST || '127.0.0.1';
  const portStr = process.env.AGENT_IPC_PORT || '';
  const token = process.env.AGENT_IPC_TOKEN || '';

  if (!portStr || !token) {
    process.stderr.write(
      'permission_mcp: missing AGENT_IPC_PORT/AGENT_IPC_TOKEN; refusing to start\n',
    );
    return 2;
  }

  const port = parseInt(portStr, 10);
  if (Number.isNaN(port)) {
    process.stderr.write(`permission_mcp: bad AGENT_IPC_PORT=${portStr}\n`);
    return 2;
  }

  const ipc = new IpcClient(host, port, token);
  const server = new StdioMcpServer(ipc);

  try {
    await server.serve();
  } catch (err) {
    debugLog(`server error: ${err}`);
    return 1;
  }
  return 0;
}

// 仅在直接运行时启动（作为 Claude CLI 的子进程）
if (require.main === module) {
  main().then((code) => process.exit(code)).catch(() => process.exit(1));
}
