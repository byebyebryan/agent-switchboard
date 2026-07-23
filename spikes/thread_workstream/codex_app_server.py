"""Small bounded Codex App Server client used only by spike harnesses."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import select
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any


RESPONSE_TIMEOUT_SECONDS = 15.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0
MAX_STDOUT_BYTES = 8 * 1024 * 1024


class AppServerError(RuntimeError):
    """The isolated provider contract could not be observed safely."""


class CodexAppServer:
    def __init__(self, codex: str, environment: Mapping[str, str]) -> None:
        self._resources = contextlib.ExitStack()
        self._stderr = self._resources.enter_context(
            tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 - ExitStack owns it
        )
        self._process = subprocess.Popen(
            [codex, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            env=dict(environment),
            bufsize=0,
        )
        self._buffer = b""
        self._received = 0
        self._next_id = 1
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "agent-switchboard-thread-workstream-spike",
                    "version": "1",
                },
                "capabilities": {},
            },
        )
        self.send({"method": "initialized", "params": {}})

    def send(self, value: Mapping[str, Any]) -> None:
        if self._process.stdin is None:
            raise AppServerError("provider app server input is unavailable")
        encoded = json.dumps(value, separators=(",", ":")).encode() + b"\n"
        try:
            self._process.stdin.write(encoded)
            self._process.stdin.flush()
        except BrokenPipeError as error:
            raise AppServerError("provider app server closed its input") from error

    def _receive(self, deadline: float) -> dict[str, Any]:
        if self._process.stdout is None:
            raise AppServerError("provider app server output is unavailable")
        while True:
            if b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise AppServerError("provider app server returned invalid JSON") from error
                if not isinstance(value, dict):
                    raise AppServerError("provider app server returned a non-object")
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError("provider app server response timed out")
            ready, _, _ = select.select([self._process.stdout], [], [], remaining)
            if not ready:
                raise AppServerError("provider app server response timed out")
            chunk = os.read(self._process.stdout.fileno(), 65536)
            if not chunk:
                raise AppServerError("provider app server closed its output")
            self._received += len(chunk)
            if self._received > MAX_STDOUT_BYTES:
                raise AppServerError("provider app server output exceeded its bound")
            self._buffer += chunk

    def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self.send({"id": request_id, "method": method, "params": dict(params)})
        deadline = time.monotonic() + RESPONSE_TIMEOUT_SECONDS
        while True:
            message = self._receive(deadline)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise AppServerError(f"provider app server rejected {method}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise AppServerError("provider app server returned an invalid result")
            return result

    def thread_read(
        self,
        provider_identity: str,
        *,
        include_turns: bool,
    ) -> dict[str, Any]:
        result = self.request(
            "thread/read",
            {"threadId": provider_identity, "includeTurns": include_turns},
        )
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != provider_identity:
            raise AppServerError("thread/read did not preserve exact identity")
        return thread

    def thread_list(self) -> list[dict[str, Any]]:
        result = self.request(
            "thread/list",
            {"limit": 20},
        )
        data = result.get("data")
        if not isinstance(data, list) or not all(
            isinstance(thread, dict) for thread in data
        ):
            raise AppServerError("thread/list returned invalid data")
        return data

    def set_name(self, provider_identity: str, name: str) -> bool:
        self.request(
            "thread/name/set",
            {"threadId": provider_identity, "name": name},
        )
        return self.thread_read(provider_identity, include_turns=False).get("name") == name

    def close(self) -> None:
        if self._process.stdin is not None and not self._process.stdin.closed:
            with contextlib.suppress(BrokenPipeError):
                self._process.stdin.close()
        try:
            self._process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        finally:
            if self._process.stdout is not None:
                self._process.stdout.close()
            self._resources.close()

    def __enter__(self) -> CodexAppServer:
        return self

    def __exit__(
        self, _exc_type: object, _exc_value: object, _traceback: object
    ) -> None:
        self.close()


def provider_version(codex: str) -> str:
    result = subprocess.run(
        [codex, "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    version = result.stdout.strip()
    if not version or "\n" in version:
        raise AppServerError("provider version output is malformed")
    return version


def schema_fingerprint(codex: str, environment: Mapping[str, str]) -> str:
    """Hash canonical installed schemas without retaining generated files."""

    with tempfile.TemporaryDirectory(prefix="asb-codex-schema-") as raw:
        destination = Path(raw)
        subprocess.run(
            [
                codex,
                "app-server",
                "generate-json-schema",
                "--out",
                str(destination),
            ],
            check=True,
            capture_output=True,
            timeout=30,
            env=dict(environment),
        )
        documents: list[tuple[str, object]] = []
        for path in sorted(destination.glob("*.json")):
            documents.append((path.name, json.loads(path.read_bytes())))
        if not documents:
            raise AppServerError("provider generated no contract schemas")
        encoded = json.dumps(
            documents,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def latest_plan(thread: Mapping[str, Any]) -> str | None:
    """Return the last completed structured Plan item from a private thread read."""

    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    found: str | None = None
    for turn in turns:
        if not isinstance(turn, Mapping):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if (
                isinstance(item, Mapping)
                and item.get("type") == "plan"
                and isinstance(item.get("text"), str)
                and item["text"].strip()
            ):
                found = item["text"]
    return found


__all__ = [
    "AppServerError",
    "CodexAppServer",
    "latest_plan",
    "provider_version",
    "schema_fingerprint",
]
