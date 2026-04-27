"""
Claude Code executor wrapper
"""
import asyncio
import json
import os
import logging
import time
import uuid
import pty
import termios
import fcntl
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Awaitable, Tuple

from config import config
from protocol import TaskOptions, TaskResult, TaskProgress, UserConfirmationRequest, ConfirmationOption

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
                  progress_callback: Optional[Callable[[TaskProgress], Awaitable[None]]] = None,
                  confirmation_callback: Optional[Callable[[UserConfirmationRequest], Awaitable[str]]] = None,
                  workdir: Optional[str] = None,
                  task_id: Optional[str] = None) -> TaskResult:
        """
        Execute Claude Code task

        Args:
            prompt: Task prompt
            options: Task options
            context: Additional context
            progress_callback: Progress callback function
            confirmation_callback: User confirmation callback function
            workdir: Working directory (overrides default)

        Returns:
            TaskResult: Task result
        """
        start_time = time.time()
        self._cancelled = False
        self._task_id = task_id

        # Determine working directory
        current_workdir = Path(workdir).resolve() if workdir else self.workdir

        try:
            # Build command
            cmd = self._build_command(prompt, options, context)

            # Use stdbuf to disable stdout buffering for realtime output
            # This allows us to read output line-by-line incrementally
            full_cmd = ["stdbuf", "-o0"] + cmd

            logger.info(f"Executing Claude command: {' '.join(full_cmd[:3])}...")
            logger.info(f"Workdir: {current_workdir}")

            # Create pseudo-terminal so Claude thinks it's running in an interactive terminal
            # This prevents the 3s timeout on stdin input when waiting for confirmation
            master_fd, slave_fd = pty.openpty()

            # Set terminal attributes
            term_attr = pty.tcgetattr(slave_fd)
            pty.tcsetattr(slave_fd, termios.TCSADRAIN, term_attr)

            # Make non-blocking
            fcntl.fcntl(master_fd, fcntl.F_SETFL, fcntl.fcntl(master_fd, fcntl.F_GETFL) | os.O_NONBLOCK)

            # Execute command with pty
            process = await asyncio.create_subprocess_exec(
                *full_cmd,
                cwd=str(current_workdir),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=self._get_env()
            )

            # Close our copy of slave_fd after process creation
            # Because the process already has its own copy
            # If we don't close it, PTY will hang waiting for EOF
            os.close(slave_fd)

            self._current_process = process

            # Create async file objects for reading/writing
            master = os.fdopen(master_fd, 'r+b', 0)

            # Convert to asyncio stream
            loop = asyncio.get_event_loop()
            reader = asyncio.StreamReader(loop=loop)
            reader_protocol = asyncio.StreamReaderProtocol(reader)
            transport, _ = await loop.connect_read_pipe(
                lambda: reader_protocol, master
            )

            # Write side
            write_transport = transport
            writer = asyncio.StreamWriter(write_transport, reader_protocol, None, loop)

            # Read output incrementally for realtime progress
            stdout_buffer: list[str] = []
            stderr_buffer: list[str] = []

            async def read_output():
                """Read output from pty (combines stdout/stderr since pty merges them)"""
                last_sent_time = 0
                last_sent_length = 0
                buffer = ''
                while True:
                    try:
                        chunk = await reader.read(1024)
                        if not chunk:
                            # EOF
                            break
                        buffer += chunk.decode("utf-8", errors="replace")

                        # Split into lines
                        while '\n' in buffer:
                            line, buffer = buffer.split('\n', 1)
                            line_str = line + '\n'
                            stdout_buffer.append(line_str)

                            # Check if this looks like an interactive confirmation prompt
                            # Only trigger on lines that end with a question mark and contain y/n
                            if confirmation_callback:
                                lower_line = line_str.lower()
                                # More strict matching to avoid false positives
                                is_confirmation = (
                                    ('do you want to proceed' in lower_line and '?' in line_str) or
                                    ('continue?' in lower_line) or
                                    ('proceed?' in lower_line) or
                                    ('are you sure' in lower_line and '?' in line_str) or
                                    ('(y/n)' in lower_line) or
                                    ('[y/n]' in lower_line) or
                                    ('y/n' in lower_line and '?' in line_str)
                                )
                                if is_confirmation:
                                    # This looks like a real confirmation request
                                    request_id = str(uuid.uuid4())[:8]
                                    task_id = self._task_id or str(uuid.uuid4())[:8]

                                    request = UserConfirmationRequest(
                                        request_id=request_id,
                                        task_id=task_id,
                                        title="需要确认",
                                        message="Claude 需要您确认是否继续执行",
                                        prompt=line.strip(),
                                        options=[
                                            ConfirmationOption(label="确认 (y)", value="y"),
                                            ConfirmationOption(label="取消 (N)", value="n")
                                        ],
                                        timeout=300  # 5 minutes for user to respond
                                    )

                                    logger.info(f"Confirmation requested for task {task_id}: {line.strip()}")

                                    # Call the confirmation callback to get user response
                                    try:
                                        response = await confirmation_callback(request)
                                    except Exception as e:
                                        logger.error(f"Confirmation callback failed: {e}")
                                        response = None

                                    if response:
                                        # Write the response back to pty
                                        try:
                                            writer.write((response + "\n").encode("utf-8"))
                                            await writer.drain()
                                            logger.info(f"User confirmation sent: {response}")
                                        except Exception as e:
                                            logger.error(f"Failed to write to stdin: {e}")

                                    # Add response to stdout buffer for logging
                                    stdout_buffer.append(f"[USER_CONFIRMATION: {response}]\n")

                        # Throttled progress update: send at most every 0.5 seconds
                        if progress_callback:
                            current_length = len(stdout_buffer)
                            now = time.time()
                            if current_length > last_sent_length and (now - last_sent_time) >= 0.5:
                                full_output = "".join(stdout_buffer)
                                current_turn = len(stdout_buffer) // 5 + 1
                                progress = TaskProgress(
                                    turn=current_turn,
                                    max_turns=options.max_turns,
                                    status="working",
                                    message=full_output
                                )
                                try:
                                    await progress_callback(progress)
                                    # Give event loop a chance to actually send the data
                                    await asyncio.sleep(0)
                                except Exception as e:
                                    logger.error(f"Progress callback failed: {e}")
                                last_sent_length = current_length
                                last_sent_time = now

                    except OSError as e:
                        # Errno 5 is expected when PTY slave is closed after process exit
                        # This happens because when all slave fds are closed, master read gets errno 5
                        if e.errno == 5:  # Input/output error - this is normal
                            logger.debug(f"Task {self._task_id}: Got expected EOF on PTY read (errno 5), stopping read")
                            break
                        logger.error(f"OSError reading from pty: {e}")
                        break
                    except Exception as e:
                        logger.error(f"Unexpected error reading from pty: {e}")
                        break

                # Send final progress update to ensure all output is pushed
                if progress_callback and len(stdout_buffer) > last_sent_length:
                    full_output = "".join(stdout_buffer)
                    current_turn = len(stdout_buffer) // 5 + 1
                    try:
                        await progress_callback(TaskProgress(
                            turn=current_turn,
                            max_turns=options.max_turns,
                            status="working",
                            message=full_output
                        ))
                    except Exception as e:
                        logger.error(f"Final progress callback failed: {e}")

            # With pty, stderr is merged into stdout
            async def read_stderr():
                """No-op since pty merges streams"""
                pass

            try:
                # Read output from pty (stderr is merged into stdout)
                output_task = asyncio.create_task(read_output())
                stderr_task = asyncio.create_task(read_stderr())

                # Wait for read to complete
                await asyncio.gather(output_task, stderr_task)

                # Wait for process to complete
                await asyncio.wait_for(process.wait(), timeout=options.timeout)
            except asyncio.TimeoutError:
                logger.error("Claude execution timed out")
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                # Cleanup
                writer.close()
                await writer.wait_closed()
                # slave_fd already closed earlier
                return TaskResult(
                    success=False,
                    result="",
                    duration_ms=int((time.time() - start_time) * 1000)
                )

            # Combine output - with pty, stderr is merged into stdout
            stdout = "".join(stdout_buffer)
            stderr = ""  # Already merged

            # Cleanup
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as e:
                logger.debug(f"Cleanup warning on writer: {e}")
            # slave_fd already closed earlier

            # Parse output
            result = self._parse_output(
                stdout,
                stderr,
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
            # Cleanup pty
            if 'writer' in locals():
                writer.close()
            # slave_fd already closed earlier
            return TaskResult(
                success=False,
                result="",
                duration_ms=int((time.time() - start_time) * 1000)
            )
        except Exception as e:
            logger.exception(f"Claude execution error: {e}")
            # Cleanup pty
            if 'writer' in locals():
                writer.close()
            # slave_fd already closed earlier
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
                       progress_callback: Optional[Callable[[TaskProgress], Awaitable[None]]] = None,
                       confirmation_callback: Optional[Callable[[UserConfirmationRequest], Awaitable[str]]] = None) -> TaskResult:
        """Run task with concurrency limit"""
        async with self.semaphore:
            runner = ClaudeRunner(workdir)

            # Create task
            task = asyncio.create_task(
                runner.run(prompt, options, context, progress_callback, confirmation_callback, workdir, task_id)
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
