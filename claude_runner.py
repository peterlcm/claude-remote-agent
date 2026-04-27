"""
Claude Code executor wrapper
"""
import asyncio
import json
import os
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from config import config
from protocol import TaskOptions, TaskResult, TaskProgress

logger = logging.getLogger(__name__)


class ClaudeRunner:
    """Claude Code executor"""

    def __init__(self, workdir: str = "."):
        self.workdir = Path(workdir).resolve()
        self._current_process: Optional[asyncio.subprocess.Process] = None
        self._task_id: Optional[str] = None
        self._cancelled = False

    async def run(self,
                  prompt: str,
                  options: TaskOptions,
                  context: Optional[str] = None,
                  progress_callback: Optional[Callable[[TaskProgress], None]] = None,
                  workdir: Optional[str] = None) -> TaskResult:
        """
        Execute Claude Code task

        Args:
            prompt: Task prompt
            options: Task options
            context: Additional context
            progress_callback: Progress callback function
            workdir: Working directory (overrides default)

        Returns:
            TaskResult: Task result
        """
        start_time = time.time()
        self._cancelled = False

        # Determine working directory
        current_workdir = Path(workdir).resolve() if workdir else self.workdir

        try:
            # Build command
            cmd = self._build_command(prompt, options, context)

            logger.info(f"Executing Claude command: {' '.join(cmd[:3])}...")
            logger.info(f"Workdir: {current_workdir}")

            # Execute command
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(current_workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._get_env()
            )

            self._current_process = process

            # Wait for completion (with timeout)
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=options.timeout
                )
            except asyncio.TimeoutError:
                logger.error("Claude execution timed out")
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                return TaskResult(
                    success=False,
                    result="",
                    duration_ms=int((time.time() - start_time) * 1000)
                )

            # Parse output
            result = self._parse_output(
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                process.returncode
            )

            result.duration_ms = int((time.time() - start_time) * 1000)

            logger.info(f"Task completed in {result.duration_ms}ms, "
                       f"success={result.success}, turns={result.num_turns}")

            return result

        except asyncio.CancelledError:
            logger.info("Task cancelled")
            if self._current_process and self._current_process.returncode is None:
                self._current_process.kill()
                await self._current_process.wait()
            return TaskResult(
                success=False,
                result="",
                duration_ms=int((time.time() - start_time) * 1000)
            )
        except Exception as e:
            logger.exception(f"Claude execution error: {e}")
            return TaskResult(
                success=False,
                result="",
                duration_ms=int((time.time() - start_time) * 1000)
            )
        finally:
            self._current_process = None

    def cancel(self):
        """Cancel currently running task"""
        self._cancelled = True
        if self._current_process and self._current_process.returncode is None:
            logger.info("Killing Claude process")
            self._current_process.kill()

    def _build_command(self,
                       prompt: str,
                       options: TaskOptions,
                       context: Optional[str] = None) -> list:
        """Build Claude command line arguments"""
        cmd = ["claude", "-p", prompt]

        # Auto-approve permissions (critical for headless execution)
        cmd.extend(["--permission-mode", "auto"])

        # Model selection
        if options.model:
            cmd.extend(["--model", options.model])

        # Max turns
        if options.max_turns:
            cmd.extend(["--max-turns", str(options.max_turns)])

        # Reasoning effort - only add if explicitly set (some APIs may not support)
        if options.effort and options.effort in ["low", "medium", "high"]:
            cmd.extend(["--effort", options.effort])

        # Allowed tools
        if options.allowed_tools:
            cmd.extend(["--allowedTools", ",".join(options.allowed_tools)])

        # Output format
        if options.output_format == "json":
            cmd.extend(["--output-format", "json"])

        # Continue session
        if options.continue_last:
            cmd.append("--continue")

        # Resume specific session
        if options.session_id:
            cmd.extend(["--resume", options.session_id])

        # No session persistence (avoid accumulation)
        cmd.append("--no-session-persistence")

        return cmd

    def _get_env(self) -> Dict[str, str]:
        """Get execution environment variables"""
        env = os.environ.copy()

        # Ensure Claude uses correct encoding
        env["PYTHONIOENCODING"] = "utf-8"
        env["LANG"] = "en_US.UTF-8"

        return env

    def _parse_output(self, stdout: str, stderr: str,
                      returncode: int) -> TaskResult:
        """
        Parse Claude Code output

        Args:
            stdout: Standard output
            stderr: Standard error
            returncode: Exit code

        Returns:
            TaskResult: Parsed result
        """
        result = TaskResult(success=returncode == 0)

        # If it's JSON output format, try to parse
        if stdout.strip().startswith("{") and stdout.strip().endswith("}"):
            try:
                data = json.loads(stdout)
                result.result = data.get("result", "")
                result.structured_output = data
                result.num_turns = data.get("num_turns", 0)
                result.session_id = data.get("session_id")

                # Extract usage information
                if "usage" in data:
                    result.usage = data["usage"]
                elif "total_cost_usd" in data:
                    result.usage["total_cost_usd"] = data["total_cost_usd"]

                # Check result type
                if data.get("type") == "result":
                    result.success = data.get("subtype") == "success"

            except json.JSONDecodeError:
                # Not valid JSON, treat as plain text
                result.result = stdout
        else:
            # Plain text output
            result.result = stdout

        # If failed, include error information
        if returncode != 0:
            logger.error(f"Claude exited with code {returncode}: {stderr}")
            result.result = stderr or stdout
            result.success = False

        return result

    def is_running(self) -> bool:
        """Check if there is a task running"""
        return (self._current_process is not None and
                self._current_process.returncode is None)


class ClaudeRunnerManager:
    """Claude executor manager"""

    def __init__(self, max_concurrent: int = 3):
        self.max_concurrent = max_concurrent
        self.runners: Dict[str, Tuple[ClaudeRunner, asyncio.Task]] = {}
        self.semaphore = asyncio.Semaphore(max_concurrent)

    async def run_task(self,
                       task_id: str,
                       prompt: str,
                       options: TaskOptions,
                       context: Optional[str] = None,
                       workdir: str = ".",
                       progress_callback: Optional[Callable] = None) -> TaskResult:
        """Run task with concurrency limit"""
        async with self.semaphore:
            runner = ClaudeRunner(workdir)

            # Create task
            task = asyncio.create_task(
                runner.run(prompt, options, context, progress_callback)
            )

            self.runners[task_id] = (runner, task)

            try:
                result = await task
                return result
            finally:
                self.runners.pop(task_id, None)

    def cancel_task(self, task_id: str) -> bool:
        """Cancel specified task"""
        if task_id in self.runners:
            runner, task = self.runners[task_id]
            runner.cancel()
            task.cancel()
            return True
        return False

    def get_active_count(self) -> int:
        """Get active task count"""
        return len(self.runners)

    def get_running_tasks(self) -> list:
        """Get running task ID list"""
        return list(self.runners.keys())
