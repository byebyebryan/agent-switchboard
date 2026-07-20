"""Explicit, bounded stdio MCP adapter for optional external memory search."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
import unicodedata
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

from .config import MemoryConfig
from .protocol import MAX_AGENT_MEMORY_TEXT_BYTES

_MAX_STDOUT_BYTES: Final = 1024 * 1024
_MAX_STDERR_BYTES: Final = 4 * 1024
_MCP_PROTOCOL_VERSION: Final = "2025-11-25"


@dataclass(frozen=True, slots=True)
class MemoryAdapterResult:
    available: bool
    text: str = ""
    truncated: bool = False
    issues: tuple[dict[str, str], ...] = ()


class _MemoryAdapterError(RuntimeError):
    pass


def _issue(code: str, message: str) -> MemoryAdapterResult:
    return MemoryAdapterResult(
        available=False,
        issues=({"code": code, "path": "memory", "message": message},),
    )


def _request(identifier: int, method: str, params: object | None = None) -> bytes:
    record: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": method,
    }
    if params is not None:
        record["params"] = params
    return json.dumps(record, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _notification(method: str) -> bytes:
    return (
        json.dumps(
            {"jsonrpc": "2.0", "method": method}, separators=(",", ":"), sort_keys=True
        ).encode()
        + b"\n"
    )


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=0.5)


def _exchange(
    config: MemoryConfig,
    *,
    query: str,
    project: str,
    limit: int,
    environment: Mapping[str, str] | None,
) -> dict[str, object]:
    child_environment = dict(os.environ if environment is None else environment)
    for name in tuple(child_environment):
        if name.startswith("AGENT_SWITCHBOARD_"):
            child_environment.pop(name, None)
    try:
        process = subprocess.Popen(
            config.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_environment,
            start_new_session=True,
        )
    except OSError as error:
        raise _MemoryAdapterError("memory adapter could not be started") from error
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        process.stdin.write(
            _request(
                1,
                "initialize",
                {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "agent-switchboard", "version": "0.1"},
                },
            )
        )
        process.stdin.flush()
    except OSError as error:
        _stop(process)
        raise _MemoryAdapterError("memory adapter input failed") from error

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + config.timeout_seconds
    stdout = bytearray()
    stdout_size = 0
    stderr_size = 0
    responses: dict[int, dict[str, object]] = {}
    tool_sent = False
    try:
        while time.monotonic() < deadline:
            for key, _ in selector.select(max(0.0, deadline - time.monotonic())):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    stderr_size += len(chunk)
                    if stderr_size > _MAX_STDERR_BYTES:
                        raise _MemoryAdapterError(
                            "memory adapter diagnostics exceeded limit"
                        )
                    continue
                stdout.extend(chunk)
                stdout_size += len(chunk)
                if stdout_size > _MAX_STDOUT_BYTES:
                    raise _MemoryAdapterError("memory adapter output exceeded limit")
                while b"\n" in stdout:
                    raw, _, remainder = stdout.partition(b"\n")
                    stdout[:] = remainder
                    if not raw:
                        continue
                    try:
                        message = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise _MemoryAdapterError(
                            "memory adapter returned invalid JSON"
                        ) from error
                    if not isinstance(message, dict):
                        raise _MemoryAdapterError(
                            "memory adapter returned an invalid message"
                        )
                    if message.get("jsonrpc") != "2.0":
                        raise _MemoryAdapterError(
                            "memory adapter returned an invalid JSON-RPC message"
                        )
                    identifier = message.get("id")
                    if isinstance(identifier, int) and not isinstance(identifier, bool):
                        if identifier not in {1, 2} or identifier in responses:
                            raise _MemoryAdapterError(
                                "memory adapter returned an unexpected response"
                            )
                        if identifier == 2 and not tool_sent:
                            raise _MemoryAdapterError(
                                "memory adapter returned a response out of order"
                            )
                        responses[identifier] = message
                if 1 in responses and not tool_sent:
                    initialization = responses[1]
                    initialization_result = initialization.get("result")
                    if "error" in initialization or not isinstance(
                        initialization_result, dict
                    ):
                        raise _MemoryAdapterError(
                            "memory adapter initialization failed"
                        )
                    if initialization_result.get("protocolVersion") not in {
                        "2025-11-25",
                        "2025-06-18",
                        "2025-03-26",
                    }:
                        raise _MemoryAdapterError(
                            "memory adapter selected an unsupported protocol"
                        )
                    try:
                        process.stdin.write(
                            _notification("notifications/initialized")
                            + _request(
                                2,
                                "tools/call",
                                {
                                    "name": config.tool,
                                    "arguments": {
                                        "query": query,
                                        "limit": limit,
                                        "project": project,
                                    },
                                },
                            )
                        )
                        process.stdin.flush()
                        process.stdin.close()
                    except OSError as error:
                        raise _MemoryAdapterError(
                            "memory adapter input failed"
                        ) from error
                    tool_sent = True
                if tool_sent and 2 in responses:
                    response = responses[2]
                    if "error" in response:
                        raise _MemoryAdapterError("memory adapter tool call failed")
                    result = response.get("result")
                    if not isinstance(result, dict):
                        raise _MemoryAdapterError(
                            "memory adapter returned no tool result"
                        )
                    return result
            if process.poll() is not None and not selector.get_map():
                break
        raise _MemoryAdapterError("memory adapter timed out or exited early")
    finally:
        selector.close()
        _stop(process)


def _bounded_text(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_AGENT_MEMORY_TEXT_BYTES:
        return value, False
    candidate = encoded[:MAX_AGENT_MEMORY_TEXT_BYTES]
    while True:
        try:
            return candidate.decode("utf-8"), True
        except UnicodeDecodeError as error:
            candidate = candidate[: error.start]


def search_memory(
    config: MemoryConfig,
    *,
    query: str,
    project: str,
    limit: int,
    environment: Mapping[str, str] | None = None,
) -> MemoryAdapterResult:
    """Call one configured MCP tool and fail closed into an unavailable result."""

    if not config.enabled:
        return _issue("memory_disabled", "The optional memory adapter is disabled.")
    try:
        result = _exchange(
            config,
            query=query,
            project=project,
            limit=limit,
            environment=environment,
        )
        if result.get("isError") is True:
            raise _MemoryAdapterError("memory adapter tool call failed")
        content = result.get("content")
        if not isinstance(content, list):
            raise _MemoryAdapterError("memory adapter returned invalid tool content")
        fragments: list[str] = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str):
                fragments.append(text)
        if not fragments:
            raise _MemoryAdapterError("memory adapter returned no text content")
        joined = "\n".join(fragments)
        if any(
            unicodedata.category(character) == "Cc" and character not in "\n\t"
            for character in joined
        ):
            raise _MemoryAdapterError("memory adapter returned unsafe text")
        text, truncated = _bounded_text(joined)
        return MemoryAdapterResult(True, text, truncated)
    except _MemoryAdapterError:
        return _issue(
            "memory_unavailable", "The optional memory adapter is unavailable."
        )


__all__ = ["MemoryAdapterResult", "search_memory"]
