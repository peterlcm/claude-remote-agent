"""
Claude Code executor wrapper based on stream-json protocol.

Uses Claude CLI's official `--output-format stream-json --verbose
--include-partial-messages` for newline-delimited JSON event streaming. This
makes the runner fully cross-platform (no PTY, no stdbuf, no termios) and
removes the need for terminal output regex parsing for confirmations.
"""
import asyncio
import json
import logging
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from protocol import (
    ConfirmationOption,
    TaskOptions,
    TaskProgress,
    TaskResult,
    UserConfirmationRequest,
)

logger = logging.getLogger(__name__)


# Async callback type aliases.
EventCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]
ProgressCallback = Callable[[TaskProgress], Awaitable[None]]
ConfirmationCallback = Callable[[UserConfirmationRequest], Awaitable[str]]


class ClaudeRunner:
    """Claude Code executor that consumes stream-json NDJSON events."""

    def __init__(self, workdir: str = "."):
        self.workdir = Path(workdir).resolve()
        self._current_process: Optional[asyncio.subprocess.Process] = None
        self._task_id: Optional[str] = None
        self._cancelled = False

    async def run(self,
                  prompt: str,
                  options: TaskOptions,
                  context: Optional[str] = None,
                  progress_callback: Optional[ProgressCallback] = None,
                  confirmation_callback: Optional[ConfirmationCallback] = None,
                  event_callback: Optional[EventCallback] = None,
                  workdir: Optional[str] = None,
                  task_id: Optional[str] = None,
                  mcp_config_path: Optional[str] = None,
                  permission_tool: Optional[str] = None,
                  permission_mode: str = "default",
                  auto_approve_tools: Optional[List[str]] = None) -> TaskResult:
        """Execute a Claude Code task and stream events via callbacks.

        Args:
            prompt: Task prompt.
            options: Task options.
            context: Optional extra context appended to the prompt.
            progress_callback: High-level status (turn/state) callback.
            confirmation_callback: Reserved for ad-hoc confirmation requests.
            event_callback: Per-event callback receiving (event_type, payload).
            workdir: Override working directory for this run.
            task_id: Task identifier.
            mcp_config_path: Path to MCP config file (for permission MCP).
            permission_tool: Fully qualified MCP permission tool name, e.g.
                ``mcp__remote_agent__approve``.
            permission_mode: Claude CLI permission mode (default/acceptEdits/...).
            auto_approve_tools: Tool names that should bypass permission prompts.
        """
        start_time = time.time()
        self._cancelled = False
        self._task_id = task_id

        current_workdir = Path(workdir).resolve() if workdir else self.workdir

        try:
            cmd = self._build_command(
                prompt=prompt,
                options=options,
                context=context,
                mcp_config_path=mcp_config_path,
                permission_tool=permission_tool,
                permission_mode=permission_mode,
                auto_approve_tools=auto_approve_tools,
            )

            cmd[0] = self._resolve_executable(cmd[0])

            logger.info("Executing Claude command: %s ...", " ".join(cmd[:4]))
            logger.info("Workdir: %s", current_workdir)

            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(current_workdir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._get_env(),
            )
            self._current_process = process

            # Aggregated state populated while consuming the stream.
            stream_state: Dict[str, Any] = {
                "session_id": None,
                "model": None,
                "turn": 0,
                "result_event": None,
                "assistant_text_chunks": [],
                "stderr_chunks": [],
            }

            stdout_task = asyncio.create_task(
                self._consume_stdout(
                    reader=process.stdout,
                    options=options,
                    state=stream_state,
                    event_callback=event_callback,
                    progress_callback=progress_callback,
                )
            )
            stderr_task = asyncio.create_task(
                self._consume_stderr(
                    reader=process.stderr,
                    state=stream_state,
                    event_callback=event_callback,
                )
            )

            try:
                await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task, process.wait()),
                    timeout=options.timeout,
                )
            except asyncio.TimeoutError:
                logger.error("Claude execution timed out after %ss", options.timeout)
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
                stdout_task.cancel()
                stderr_task.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                return self._build_timeout_result(stream_state, start_time)

            return_code = process.returncode if process.returncode is not None else -1
            return self._build_result(stream_state, return_code, start_time)

        except asyncio.CancelledError:
            logger.info("Task cancelled: %s", self._task_id)
            await self._kill_process()
            return TaskResult(
                success=False,
                result="cancelled",
                duration_ms=int((time.time() - start_time) * 1000),
            )
        except Exception as exc:
            logger.exception("Claude execution error: %s", exc)
            await self._kill_process()
            return TaskResult(
                success=False,
                result=f"runner error: {exc}",
                duration_ms=int((time.time() - start_time) * 1000),
            )
        finally:
            self._current_process = None

    def cancel(self) -> None:
        """Request cancellation of the currently running task."""
        self._cancelled = True
        if self._current_process and self._current_process.returncode is None:
            logger.info("Killing Claude process for task %s", self._task_id)
            try:
                self._current_process.kill()
            except ProcessLookupError:
                pass

    def is_running(self) -> bool:
        return (self._current_process is not None
                and self._current_process.returncode is None)

    async def _kill_process(self) -> None:
        if self._current_process and self._current_process.returncode is None:
            try:
                self._current_process.kill()
            except ProcessLookupError:
                pass
            try:
                await self._current_process.wait()
            except Exception:
                pass

    # ------------------------------------------------------------------ stream

    async def _consume_stdout(self,
                              reader: asyncio.StreamReader,
                              options: TaskOptions,
                              state: Dict[str, Any],
                              event_callback: Optional[EventCallback],
                              progress_callback: Optional[ProgressCallback]) -> None:
        """Read stdout NDJSON and dispatch each event to event_callback."""
        while True:
            try:
                line = await reader.readline()
            except (asyncio.LimitOverrunError, ValueError):
                # Some events (huge tool outputs) can exceed default 64KiB. Read
                # raw bytes until a newline and continue.
                line = await self._read_long_line(reader)
            if not line:
                break

            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                logger.debug("Non-JSON line on Claude stdout: %s", text[:200])
                if event_callback:
                    await self._safe_emit(event_callback, "stdout_text", {"text": text})
                continue

            await self._dispatch_event(event, state, options, event_callback, progress_callback)

    async def _read_long_line(self, reader: asyncio.StreamReader) -> bytes:
        """Fallback for events that exceed StreamReader's default buffer."""
        chunks: List[bytes] = []
        while True:
            try:
                chunk = await reader.readuntil(b"\n")
                chunks.append(chunk)
                break
            except asyncio.IncompleteReadError as err:
                chunks.append(err.partial)
                break
            except asyncio.LimitOverrunError as err:
                chunks.append(await reader.readexactly(err.consumed))
        return b"".join(chunks)

    async def _consume_stderr(self,
                              reader: asyncio.StreamReader,
                              state: Dict[str, Any],
                              event_callback: Optional[EventCallback]) -> None:
        while True:
            try:
                line = await reader.readline()
            except (asyncio.LimitOverrunError, ValueError):
                line = await self._read_long_line(reader)
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            if not text:
                continue
            state["stderr_chunks"].append(text)
            logger.debug("claude stderr: %s", text)
            if event_callback:
                await self._safe_emit(event_callback, "stderr", {"text": text})

    # ------------------------------------------------------------------ dispatch

    async def _dispatch_event(self,
                              event: Dict[str, Any],
                              state: Dict[str, Any],
                              options: TaskOptions,
                              event_callback: Optional[EventCallback],
                              progress_callback: Optional[ProgressCallback]) -> None:
        evt_type = event.get("type")
        subtype = event.get("subtype")

        if evt_type == "system" and subtype == "init":
            state["session_id"] = event.get("session_id") or state["session_id"]
            state["model"] = event.get("model") or state["model"]
            await self._emit_event(event_callback, "session_init", {
                "session_id": state["session_id"],
                "model": state["model"],
                "permission_mode": event.get("permissionMode"),
                "tools": event.get("tools"),
                "mcp_servers": event.get("mcp_servers"),
                "cwd": event.get("cwd"),
            })
            await self._emit_progress(progress_callback, options, state, status="thinking")

        elif evt_type == "system" and subtype == "api_retry":
            await self._emit_event(event_callback, "api_retry", {
                "attempt": event.get("attempt"),
                "max_retries": event.get("max_retries"),
                "retry_delay_ms": event.get("retry_delay_ms"),
                "error_status": event.get("error_status"),
                "error": event.get("error"),
            })

        elif evt_type == "system":
            # Forward unknown system subtypes verbatim for forward compatibility.
            await self._emit_event(event_callback, f"system_{subtype or 'event'}", event)

        elif evt_type == "stream_event":
            await self._handle_stream_event(event, event_callback)

        elif evt_type == "assistant":
            state["turn"] = state.get("turn", 0) + 1
            message = event.get("message") or {}
            content = message.get("content") or []
            await self._emit_event(event_callback, "assistant_message", {
                "message_id": message.get("id"),
                "model": message.get("model"),
                "stop_reason": message.get("stop_reason"),
                "usage": message.get("usage"),
                "content": content,
                "turn": state["turn"],
            })
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    state["assistant_text_chunks"].append(block.get("text", ""))
            await self._emit_progress(progress_callback, options, state, status="working")

        elif evt_type == "user":
            message = event.get("message") or {}
            content = message.get("content") or []
            await self._emit_event(event_callback, "tool_result", {
                "content": content,
                "message_id": message.get("id"),
            })

        elif evt_type == "rate_limit_event":
            await self._emit_event(event_callback, "rate_limit", {
                "rate_limit_info": event.get("rate_limit_info"),
            })

        elif evt_type == "result":
            state["result_event"] = event
            await self._emit_event(event_callback, "result", {
                "subtype": event.get("subtype"),
                "result": event.get("result"),
                "is_error": event.get("is_error"),
                "session_id": event.get("session_id"),
                "duration_ms": event.get("duration_ms"),
                "duration_api_ms": event.get("duration_api_ms"),
                "num_turns": event.get("num_turns"),
                "total_cost_usd": event.get("total_cost_usd"),
                "usage": event.get("usage"),
            })
            await self._emit_progress(progress_callback, options, state,
                                      status="completed" if not event.get("is_error") else "failed")

        else:
            await self._emit_event(event_callback, evt_type or "unknown", event)

    async def _handle_stream_event(self,
                                   event: Dict[str, Any],
                                   event_callback: Optional[EventCallback]) -> None:
        inner = event.get("event") or {}
        inner_type = inner.get("type")
        delta = inner.get("delta") or {}
        index = inner.get("index")
        content_block = inner.get("content_block") or {}

        if inner_type == "message_start":
            await self._emit_event(event_callback, "message_start", {
                "message": inner.get("message"),
            })
        elif inner_type == "content_block_start":
            await self._emit_event(event_callback, "content_block_start", {
                "index": index,
                "content_block": content_block,
            })
        elif inner_type == "content_block_delta":
            d_type = delta.get("type")
            if d_type == "text_delta":
                await self._emit_event(event_callback, "text_delta", {
                    "index": index,
                    "text": delta.get("text", ""),
                })
            elif d_type == "input_json_delta":
                await self._emit_event(event_callback, "tool_input_delta", {
                    "index": index,
                    "partial_json": delta.get("partial_json", ""),
                })
            else:
                await self._emit_event(event_callback, f"delta_{d_type or 'unknown'}", {
                    "index": index,
                    "delta": delta,
                })
        elif inner_type == "content_block_stop":
            await self._emit_event(event_callback, "content_block_stop", {"index": index})
        elif inner_type == "message_delta":
            await self._emit_event(event_callback, "message_delta", {
                "delta": delta,
                "usage": inner.get("usage"),
            })
        elif inner_type == "message_stop":
            await self._emit_event(event_callback, "message_stop", {})
        else:
            await self._emit_event(event_callback, f"stream_{inner_type or 'unknown'}", inner)

    # ------------------------------------------------------------------ helpers

    async def _emit_event(self,
                          event_callback: Optional[EventCallback],
                          event_type: str,
                          payload: Dict[str, Any]) -> None:
        if not event_callback:
            return
        await self._safe_emit(event_callback, event_type, payload)

    async def _safe_emit(self,
                         event_callback: EventCallback,
                         event_type: str,
                         payload: Dict[str, Any]) -> None:
        try:
            await event_callback(event_type, payload)
        except Exception as exc:
            logger.error("event_callback failed for %s: %s", event_type, exc)

    async def _emit_progress(self,
                             progress_callback: Optional[ProgressCallback],
                             options: TaskOptions,
                             state: Dict[str, Any],
                             status: str) -> None:
        if not progress_callback:
            return
        progress = TaskProgress(
            turn=state.get("turn", 0),
            max_turns=options.max_turns,
            status=status,
            message=None,
        )
        try:
            await progress_callback(progress)
        except Exception as exc:
            logger.error("progress_callback failed: %s", exc)

    # ------------------------------------------------------------------ command

    def _build_command(self,
                       prompt: str,
                       options: TaskOptions,
                       context: Optional[str],
                       mcp_config_path: Optional[str],
                       permission_tool: Optional[str],
                       permission_mode: str,
                       auto_approve_tools: Optional[List[str]]) -> List[str]:
        full_prompt = prompt
        if context:
            full_prompt = f"{prompt}\n\n[Context]\n{context}"

        cmd: List[str] = ["claude", "-p", full_prompt,
                          "--output-format", "stream-json",
                          "--verbose",
                          "--include-partial-messages"]

        if options.model:
            cmd += ["--model", options.model]
        if options.max_turns:
            cmd += ["--max-turns", str(options.max_turns)]
        if options.effort and options.effort in ("low", "medium", "high"):
            cmd += ["--effort", options.effort]

        allowed = list(options.allowed_tools or [])
        if auto_approve_tools:
            for tool in auto_approve_tools:
                if tool and tool not in allowed:
                    allowed.append(tool)
        if allowed:
            cmd += ["--allowedTools", ",".join(allowed)]

        if mcp_config_path:
            cmd += ["--mcp-config", str(mcp_config_path)]
        if permission_tool:
            cmd += ["--permission-prompt-tool", permission_tool]
        if permission_mode:
            cmd += ["--permission-mode", permission_mode]

        if options.continue_last:
            cmd.append("--continue")
        if options.session_id:
            cmd += ["--resume", options.session_id]

        cmd.append("--no-session-persistence")
        return cmd

    def _resolve_executable(self, exe: str) -> str:
        """Resolve an executable name to a launchable path.

        Windows ships npm-installed CLIs as ``.ps1`` / ``.cmd`` / ``.bat``
        shims. ``asyncio.create_subprocess_exec`` cannot invoke ``.ps1``
        because CreateProcess won't dispatch them, so we prefer ``.cmd`` /
        ``.bat`` / ``.exe`` siblings. On Unix we just trust shutil.which.
        """
        if Path(exe).is_absolute() and Path(exe).exists():
            return exe

        if sys.platform != "win32":
            return shutil.which(exe) or exe

        for ext in (".cmd", ".bat", ".exe"):
            candidate = shutil.which(exe + ext)
            if candidate:
                return candidate

        candidate = shutil.which(exe)
        if candidate and candidate.lower().endswith(".ps1"):
            sibling = Path(candidate).with_suffix(".cmd")
            if sibling.exists():
                return str(sibling)
            sibling = Path(candidate).with_suffix(".bat")
            if sibling.exists():
                return str(sibling)
            sibling = Path(candidate).with_suffix(".exe")
            if sibling.exists():
                return str(sibling)
            logger.warning(
                "Resolved %s to %s but it cannot be launched directly; "
                "asyncio subprocess does not support PowerShell scripts",
                exe, candidate,
            )
        return candidate or exe

    def _get_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        # Hint Unix locales while staying harmless on Windows.
        env.setdefault("LANG", "en_US.UTF-8")
        env.setdefault("LC_ALL", "en_US.UTF-8")
        return env

    # ------------------------------------------------------------------ result

    def _build_result(self,
                      state: Dict[str, Any],
                      return_code: int,
                      start_time: float) -> TaskResult:
        result_event = state.get("result_event") or {}
        is_error = bool(result_event.get("is_error"))
        success = (return_code == 0) and not is_error and bool(result_event)

        text = result_event.get("result") or "".join(state.get("assistant_text_chunks") or [])

        if not result_event and return_code != 0:
            stderr_text = "\n".join(state.get("stderr_chunks") or [])
            if stderr_text:
                text = stderr_text

        usage = result_event.get("usage") or {}
        if "total_cost_usd" in result_event:
            usage = dict(usage)
            usage.setdefault("total_cost_usd", result_event["total_cost_usd"])

        return TaskResult(
            success=success,
            result=text or "",
            structured_output=result_event or None,
            usage=usage,
            duration_ms=int((time.time() - start_time) * 1000),
            num_turns=int(result_event.get("num_turns") or state.get("turn") or 0),
            session_id=result_event.get("session_id") or state.get("session_id"),
        )

    def _build_timeout_result(self,
                              state: Dict[str, Any],
                              start_time: float) -> TaskResult:
        text = "".join(state.get("assistant_text_chunks") or []) or "Claude execution timed out"
        return TaskResult(
            success=False,
            result=text,
            structured_output={"type": "timeout"},
            duration_ms=int((time.time() - start_time) * 1000),
            num_turns=int(state.get("turn") or 0),
            session_id=state.get("session_id"),
        )


class ClaudeRunnerManager:
    """Concurrency-limited orchestrator for ClaudeRunner instances."""

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
                       progress_callback: Optional[ProgressCallback] = None,
                       confirmation_callback: Optional[ConfirmationCallback] = None,
                       event_callback: Optional[EventCallback] = None,
                       mcp_config_path: Optional[str] = None,
                       permission_tool: Optional[str] = None,
                       permission_mode: str = "default",
                       auto_approve_tools: Optional[List[str]] = None) -> "TaskResult":
        async with self.semaphore:
            runner = ClaudeRunner(workdir)
            task = asyncio.create_task(
                runner.run(
                    prompt=prompt,
                    options=options,
                    context=context,
                    progress_callback=progress_callback,
                    confirmation_callback=confirmation_callback,
                    event_callback=event_callback,
                    workdir=workdir,
                    task_id=task_id,
                    mcp_config_path=mcp_config_path,
                    permission_tool=permission_tool,
                    permission_mode=permission_mode,
                    auto_approve_tools=auto_approve_tools,
                )
            )
            self.runners[task_id] = (runner, task)
            try:
                return await task
            finally:
                self.runners.pop(task_id, None)

    def cancel_task(self, task_id: str) -> bool:
        if task_id in self.runners:
            runner, task = self.runners[task_id]
            runner.cancel()
            task.cancel()
            return True
        return False

    def get_active_count(self) -> int:
        return len(self.runners)

    def get_running_tasks(self) -> List[str]:
        return list(self.runners.keys())
