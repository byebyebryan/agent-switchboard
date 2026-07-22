"""Minimal bounded Codex App Server client for zero-turn session identity."""

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
from typing import Any, Final
from uuid import UUID

MAX_LINE_BYTES: Final = 64 * 1024
MAX_STDOUT_BYTES: Final = 1024 * 1024
MAX_STDERR_BYTES: Final = 64 * 1024
MAX_MESSAGES: Final = 64
REQUEST_TIMEOUT_SECONDS: Final = 5.0
TOTAL_TIMEOUT_SECONDS: Final = 15.0


class CodexAppServerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _Client:
    def __init__(self, executable: str) -> None:
        try:
            self.process = subprocess.Popen(
                (executable, "app-server", "--stdio"),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
        except (OSError, ValueError) as error:
            raise CodexAppServerError(
                "codex_app_server_start_failed",
                "Codex App Server could not be started",
            ) from error
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        self.stdout = bytearray()
        self.stdout_seen = 0
        self.stderr_seen = 0
        self.messages = 0
        self.request_id = 0
        self.deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS

    def __enter__(self) -> _Client:
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "agent-switchboard",
                    "title": "Agent Switchboard",
                    "version": "0.3.0",
                }
            },
        )
        if not isinstance(result, Mapping):
            raise CodexAppServerError(
                "codex_initialize_invalid", "Codex initialize result is invalid"
            )
        self.notify("initialized", {})
        return self

    def __exit__(self, *_unused: object) -> None:
        self.close()

    def close(self) -> None:
        self.selector.close()
        if self.process.stdin is not None:
            with suppress(OSError):
                self.process.stdin.close()
        if self.process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                with suppress(subprocess.TimeoutExpired):
                    self.process.wait(timeout=1)

    def _send(self, value: Mapping[str, Any]) -> None:
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        if len(payload) > MAX_LINE_BYTES:
            raise CodexAppServerError(
                "codex_request_oversized", "Codex request exceeded its byte limit"
            )
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise CodexAppServerError(
                "codex_app_server_closed", "Codex App Server closed early"
            ) from error

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._send({"method": method, "params": dict(params)})

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = self.request_id
        self.request_id += 1
        self._send({"id": request_id, "method": method, "params": dict(params)})
        deadline = min(self.deadline, time.monotonic() + REQUEST_TIMEOUT_SECONDS)
        while True:
            message = self._next_message(deadline)
            if message.get("id") != request_id:
                continue
            error = message.get("error")
            if error is not None:
                raise CodexAppServerError(
                    "codex_request_rejected", "Codex rejected the App Server request"
                )
            result = message.get("result")
            if not isinstance(result, Mapping):
                raise CodexAppServerError(
                    "codex_response_invalid", "Codex response result is invalid"
                )
            return result

    def _next_message(self, deadline: float) -> Mapping[str, Any]:
        while True:
            newline = self.stdout.find(b"\n")
            if newline >= 0:
                raw = bytes(self.stdout[:newline])
                del self.stdout[: newline + 1]
                if not raw or len(raw) > MAX_LINE_BYTES or b"\r" in raw:
                    raise CodexAppServerError(
                        "codex_response_invalid", "Codex response framing is invalid"
                    )
                self.messages += 1
                if self.messages > MAX_MESSAGES:
                    raise CodexAppServerError(
                        "codex_response_overflow", "Codex emitted too many messages"
                    )
                try:
                    message = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise CodexAppServerError(
                        "codex_response_invalid", "Codex emitted invalid JSON"
                    ) from error
                if not isinstance(message, Mapping):
                    raise CodexAppServerError(
                        "codex_response_invalid", "Codex response is not an object"
                    )
                return message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAppServerError(
                    "codex_request_timeout", "Codex App Server request timed out"
                )
            events = self.selector.select(remaining)
            if not events:
                continue
            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                except OSError as error:
                    raise CodexAppServerError(
                        "codex_app_server_read_failed",
                        "Codex App Server output could not be read",
                    ) from error
                if not chunk:
                    raise CodexAppServerError(
                        "codex_app_server_closed", "Codex App Server closed early"
                    )
                if key.data == "stderr":
                    self.stderr_seen += len(chunk)
                    if self.stderr_seen > MAX_STDERR_BYTES:
                        raise CodexAppServerError(
                            "codex_stderr_overflow", "Codex diagnostics were oversized"
                        )
                else:
                    self.stdout_seen += len(chunk)
                    if self.stdout_seen > MAX_STDOUT_BYTES:
                        raise CodexAppServerError(
                            "codex_stdout_overflow", "Codex output was oversized"
                        )
                    self.stdout.extend(chunk)


def _title(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or len(normalized.encode()) > 512
        or any(unicodedata.category(character) == "Cc" for character in normalized)
    ):
        raise ValueError("Codex session title is invalid")
    return normalized


def reserve_named_session(executable: str, title: str) -> UUID:
    """Create, name, and verify one exact zero-turn Codex thread."""

    normalized = _title(title)
    with _Client(executable) as client:
        started = client.request("thread/start", {})
        thread = started.get("thread")
        if not isinstance(thread, Mapping) or thread.get("turns") not in (None, []):
            raise CodexAppServerError(
                "codex_precreate_invalid", "Codex returned a nonempty thread"
            )
        try:
            session_id = UUID(str(thread.get("id")))
        except ValueError as error:
            raise CodexAppServerError(
                "codex_precreate_invalid", "Codex returned no exact thread UUID"
            ) from error
        if session_id.int == 0:
            raise CodexAppServerError(
                "codex_precreate_invalid", "Codex returned a nil thread UUID"
            )
        try:
            client.request(
                "thread/name/set",
                {"threadId": str(session_id), "name": normalized},
            )
            retained = client.request(
                "thread/read", {"threadId": str(session_id), "includeTurns": True}
            ).get("thread")
            if (
                not isinstance(retained, Mapping)
                or retained.get("id") != str(session_id)
                or retained.get("name") != normalized
                or retained.get("turns") != []
            ):
                raise CodexAppServerError(
                    "codex_precreate_verification_failed",
                    "Codex zero-turn session verification failed",
                )
        except Exception:
            with suppress(Exception):
                client.request("thread/delete", {"threadId": str(session_id)})
            raise
        return session_id


def delete_empty_session(executable: str, session_id: UUID) -> None:
    """Delete only an exact verified zero-turn Codex thread."""

    with _Client(executable) as client:
        thread = client.request(
            "thread/read", {"threadId": str(session_id), "includeTurns": True}
        ).get("thread")
        if (
            not isinstance(thread, Mapping)
            or thread.get("id") != str(session_id)
            or thread.get("turns") != []
        ):
            raise CodexAppServerError(
                "codex_delete_unsafe", "Codex thread is not an exact zero-turn target"
            )
        client.request("thread/delete", {"threadId": str(session_id)})


__all__ = [
    "CodexAppServerError",
    "delete_empty_session",
    "reserve_named_session",
]
