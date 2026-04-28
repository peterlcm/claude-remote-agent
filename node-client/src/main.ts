#!/usr/bin/env node
/**
 * CLI 入口 — 参数解析、信号处理、启动代理
 */

import { Command } from 'commander';
import { Config } from './config';
import { setupLogger, getLogger } from './logger';
import { ClaudeRemoteAgent } from './agent-client';

const program = new Command();

program
  .name('claude-remote-agent')
  .description('Claude Remote Agent 客户端 — 云端长连接代理')
  .version('1.0.0')
  .option('-s, --server <url>', 'WebSocket 服务地址')
  .option('-t, --token <token>', '认证 Token')
  .option('-c, --client-id <id>', '客户端标识符')
  .option('-d, --debug', '启用调试日志')
  .parse();

const opts = program.opts();

const config = new Config({
  serverUrl: opts.server,
  agentToken: opts.token,
  clientId: opts.clientId,
  debug: opts.debug || false,
});

setupLogger(config.log.level, config.log.file);

const logger = getLogger();

const agent = new ClaudeRemoteAgent(config);

// 信号处理（跨平台）
process.on('SIGINT', () => {
  agent.shutdown().catch((err) => logger.error('Shutdown error: %s', err));
});

process.on('SIGTERM', () => {
  agent.shutdown().catch((err) => logger.error('Shutdown error: %s', err));
});

agent.start().catch((err) => {
  logger.fatal({ err }, 'Fatal error');
  process.exit(1);
});
