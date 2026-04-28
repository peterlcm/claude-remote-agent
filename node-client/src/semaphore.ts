/**
 * 异步信号量 — 用于并发控制，替代 Python asyncio.Semaphore
 */

export class Semaphore {
  private _waiters: (() => void)[] = [];
  private _count: number;

  constructor(maxConcurrency: number) {
    this._count = maxConcurrency;
  }

  async acquire(): Promise<void> {
    if (this._count > 0) {
      this._count--;
      return;
    }
    return new Promise<void>(resolve => {
      this._waiters.push(resolve);
    });
  }

  release(): void {
    const waiter = this._waiters.shift();
    if (waiter) {
      waiter();
    } else {
      this._count++;
    }
  }
}

/**
 * acquire + 释放的辅助函数，类似 Python async with semaphore
 */
export async function withSemaphore<T>(
  sem: Semaphore,
  fn: () => Promise<T>,
): Promise<T> {
  await sem.acquire();
  try {
    return await fn();
  } finally {
    sem.release();
  }
}
