/**
 * 配置管理 — dotenv + 环境变量 + CLI 参数覆盖
 */

import dotenv from 'dotenv';
import { execSync } from 'child_process';
import path from 'path';

dotenv.config();

// ---------------------------------------------------------------------------
// 子配置接口
// ---------------------------------------------------------------------------

export interface ClaudeConfig {
  model: string;
  maxTurns: number;
  timeout: number;
  permissionMode: string;
  autoApproveTools: string[];
}

export interface AgentConfig {
  serverUrl: string;
  agentToken: string;
  clientId: string;
  heartbeatInterval: number;
  reconnectDelay: number;
  maxReconnectAttempts: number;
}

export interface LogConfig {
  level: string;
  file: string;
}

// ---------------------------------------------------------------------------
// 全局配置
// ---------------------------------------------------------------------------

const VERSION = '1.0.0';

const SUPPORTED_TOOLS = [
  'Read', 'Edit', 'Write', 'Bash',
  'WebSearch', 'WebFetch',
];

function getEnvStr(key: string, def: string): string {
  return process.env[key] ?? def;
}

function getEnvInt(key: string, def: number): number {
  const raw = process.env[key];
  if (raw == null) return def;
  const n = parseInt(raw, 10);
  return Number.isNaN(n) ? def : n;
}

function buildClaudeConfig(): ClaudeConfig {
  const rawTools = getEnvStr('CLAUDE_AUTO_APPROVE_TOOLS', 'Read,Glob,Grep');
  return {
    model: getEnvStr('CLAUDE_MODEL', 'sonnet'),
    maxTurns: getEnvInt('CLAUDE_MAX_TURNS', 10),
    timeout: getEnvInt('CLAUDE_TIMEOUT', 300),
    permissionMode: getEnvStr('CLAUDE_PERMISSION_MODE', 'default'),
    autoApproveTools: rawTools.split(',').map(t => t.trim()).filter(Boolean),
  };
}

function buildAgentConfig(): AgentConfig {
  return {
    serverUrl: getEnvStr('SERVER_URL', 'ws://localhost:8000/ws/client'),
    agentToken: getEnvStr('AGENT_TOKEN', ''),
    clientId: getEnvStr('CLIENT_ID', 'default'),
    heartbeatInterval: getEnvInt('HEARTBEAT_INTERVAL', 30),
    reconnectDelay: getEnvInt('RECONNECT_DELAY', 5),
    maxReconnectAttempts: getEnvInt('MAX_RECONNECT_ATTEMPTS', 0),
  };
}

function buildLogConfig(): LogConfig {
  return {
    level: getEnvStr('LOG_LEVEL', 'INFO'),
    file: getEnvStr('LOG_FILE', path.join('logs', 'agent.log')),
  };
}

export class Config {
  readonly claude: ClaudeConfig;
  readonly agent: AgentConfig;
  readonly log: LogConfig;
  readonly VERSION = VERSION;
  readonly SUPPORTED_TOOLS = SUPPORTED_TOOLS;

  constructor(overrides?: Partial<{
    serverUrl: string;
    agentToken: string;
    clientId: string;
    debug: boolean;
  }>) {
    this.claude = buildClaudeConfig();
    this.agent = buildAgentConfig();
    this.log = buildLogConfig();

    if (overrides?.serverUrl) this.agent.serverUrl = overrides.serverUrl;
    if (overrides?.agentToken) this.agent.agentToken = overrides.agentToken;
    if (overrides?.clientId) this.agent.clientId = overrides.clientId;
    if (overrides?.debug) this.log.level = 'DEBUG';
  }

  getClaudeVersion(): string {
    try {
      const out = execSync('claude --version', {
        timeout: 5000,
        encoding: 'utf-8',
      });
      return out.trim();
    } catch {
      return 'unknown';
    }
  }
}
