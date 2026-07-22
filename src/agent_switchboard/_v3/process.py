"""Shell-free subprocess execution with strict time and stream bounds."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

MAX_STDOUT_BYTES: Final = 8 * 1024 * 1024 + 1
MAX_STDERR_BYTES: Final = 64 * 1024
READ_CHUNK_BYTES: Final = 64 * 1024
PROCESS_REAP_SECONDS: Final = 1.0


class ProcessError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


class _Overflow(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CommandOutput:
    stdout: bytes
    stderr: bytes
    exit_code: int


async def _read_bounded(
    stream: asyncio.StreamReader, *, limit: int, name: str
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise _Overflow(name)
        chunks.append(chunk)


async def _kill_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with suppress(TimeoutError, ProcessLookupError):
        await asyncio.wait_for(process.wait(), PROCESS_REAP_SECONDS)


async def run_bounded_command(
    argv: Sequence[str], *, timeout_seconds: float
) -> CommandOutput:
    command = tuple(argv)
    if not command:
        raise ProcessError("command_invalid", "command is empty", retryable=False)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as error:
        raise ProcessError(
            "executable_not_found", "executable was not found", retryable=False
        ) from error
    except PermissionError as error:
        raise ProcessError(
            "executable_permission_denied",
            "executable is not runnable",
            retryable=False,
        ) from error
    except OSError as error:
        raise ProcessError(
            "executable_start_failed", "executable failed to start", retryable=True
        ) from error

    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(
        _read_bounded(process.stdout, limit=MAX_STDOUT_BYTES, name="stdout")
    )
    stderr_task = asyncio.create_task(
        _read_bounded(process.stderr, limit=MAX_STDERR_BYTES, name="stderr")
    )
    wait_task = asyncio.create_task(process.wait())
    tasks = (stdout_task, stderr_task, wait_task)
    try:
        async with asyncio.timeout(timeout_seconds):
            stdout, stderr, exit_code = await asyncio.gather(*tasks)
    except BaseException as error:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _kill_group(process)
        if isinstance(error, TimeoutError):
            raise ProcessError(
                "command_timeout", "command timed out", retryable=True
            ) from error
        if isinstance(error, _Overflow):
            raise ProcessError(
                f"{error.args[0]}_overflow",
                f"command {error.args[0]} exceeded its byte limit",
                retryable=False,
            ) from error
        raise
    return CommandOutput(stdout, stderr, exit_code)


__all__ = ["CommandOutput", "ProcessError", "run_bounded_command"]
