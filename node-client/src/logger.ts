/**
 * 日志 — pino 双输出（控制台 + 文件）
 */

import pino from 'pino';
import path from 'path';
import fs from 'fs';

let _logger: pino.Logger | null = null;

export function setupLogger(level: string, filePath: string): pino.Logger {
  const dir = path.dirname(filePath);
  fs.mkdirSync(dir, { recursive: true });

  const targets: pino.TransportTargetOptions[] = [
    {
      target: 'pino-pretty',
      options: { colorize: true, translateTime: 'SYS:standard' },
      level: level.toLowerCase(),
    },
    {
      target: 'pino/file',
      options: { destination: filePath, mkdir: true },
      level: level.toLowerCase(),
    },
  ];

  const transport = pino.transport({ targets });
  _logger = pino({ level: level.toLowerCase() }, transport);
  return _logger;
}

export function getLogger(): pino.Logger {
  if (!_logger) {
    _logger = pino({ level: 'info' }, pino.transport({
      target: 'pino-pretty',
      options: { colorize: true },
    }));
  }
  return _logger;
}
