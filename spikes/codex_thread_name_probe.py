#!/usr/bin/env python3
"""Persist and verify the current Codex thread name without a shared server."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from typing import Any

RESPONSE_TIMEOUT_SECONDS = 10
SHUTDOWN_TIMEOUT_SECONDS = 5
MAX_STDOUT_BYTES = 1024 * 1024


class StdioAppServer:
    def __init__(self, codex: str) -> None:
        self.resources = contextlib.ExitStack()
        self.stderr = self.resources.enter_context(
            tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 - owned by ExitStack
        )
        try:
            self.process = subprocess.Popen(
                [codex, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.stderr,
                bufsize=0,
            )
        except Exception:
            self.resources.close()
            raise
        self.stdout_buffer = b""
        self.stdout_bytes = 0

    def send(self, value: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("Codex stdio App Server stdin is unavailable")
        payload = json.dumps(value, separators=(",", ":")).encode() + b"\n"
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except BrokenPipeError as error:
            raise RuntimeError(self._exit_message("closed its input")) from error

    def receive(self, deadline: float) -> dict[str, Any]:
        if self.process.stdout is None:
            raise RuntimeError("Codex stdio App Server stdout is unavailable")
        while True:
            if b"\n" in self.stdout_buffer:
                line, self.stdout_buffer = self.stdout_buffer.split(b"\n", 1)
                if line.strip():
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise RuntimeError(
                            "Codex stdio App Server returned invalid JSON"
                        ) from error
                    if not isinstance(value, dict):
                        raise RuntimeError(
                            "Codex stdio App Server returned a non-object frame"
                        )
                    return value

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Timed out waiting for Codex stdio App Server")
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                raise RuntimeError("Timed out waiting for Codex stdio App Server")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError(self._exit_message("closed its output"))
            self.stdout_bytes += len(chunk)
            if self.stdout_bytes > MAX_STDOUT_BYTES:
                raise RuntimeError("Codex stdio App Server exceeded its output bound")
            self.stdout_buffer += chunk

    def request(
        self, request_id: int, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + RESPONSE_TIMEOUT_SECONDS
        while True:
            message = self.receive(deadline)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"Codex stdio App Server rejected {method}")
            result = message.get("result", {})
            if not isinstance(result, dict):
                raise RuntimeError("Codex stdio App Server returned an invalid result")
            return result

    def _has_stderr(self) -> bool:
        self.stderr.flush()
        self.stderr.seek(0)
        return bool(self.stderr.read(1))

    def _exit_message(self, action: str) -> str:
        status = self.process.poll()
        detail = f"Codex stdio App Server {action}"
        if status is not None:
            detail += f" with status {status}"
        if self._has_stderr():
            detail += " (stderr suppressed)"
        return detail

    def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            with contextlib.suppress(BrokenPipeError):
                self.process.stdin.close()
        try:
            self.process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        finally:
            if self.process.stdout is not None:
                self.process.stdout.close()
            self.resources.close()

    def __enter__(self) -> StdioAppServer:
        return self

    def __exit__(
        self, _exc_type: object, _exc_value: object, _traceback: object
    ) -> None:
        self.close()


def normalized_title(value: str) -> str:
    title = unicodedata.normalize("NFC", value).strip()
    if not title:
        raise ValueError("thread title must not be empty")
    if len(title) > 100:
        raise ValueError("thread title must be at most 100 characters")
    if any(unicodedata.category(character).startswith("C") for character in title):
        raise ValueError("thread title must not contain control characters")
    return title


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rename the current Codex thread through isolated stdio"
    )
    parser.add_argument("--title", required=True)
    parser.add_argument("--codex", default=shutil.which("codex"))
    arguments = parser.parse_args()

    if not arguments.codex:
        parser.error("codex executable was not found")
    thread_id = os.environ.get("CODEX_THREAD_ID")
    if not thread_id:
        parser.error("CODEX_THREAD_ID is unavailable; run inside a Codex turn")
    try:
        title = normalized_title(arguments.title)
    except ValueError as error:
        parser.error(str(error))

    version = subprocess.run(
        [arguments.codex, "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=RESPONSE_TIMEOUT_SECONDS,
    ).stdout.strip()
    with StdioAppServer(arguments.codex) as server:
        server.request(
            1,
            "initialize",
            {
                "clientInfo": {
                    "name": "agent-switchboard-thread-name-probe",
                    "version": "1",
                },
                "capabilities": {},
            },
        )
        server.send({"method": "initialized", "params": {}})
        server.request(2, "thread/name/set", {"threadId": thread_id, "name": title})
        result = server.request(
            3, "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("name") != title:
            raise RuntimeError("persisted thread name did not match the request")

    print(
        json.dumps(
            {
                "providerVersion": version,
                "feature": "thread/name/set",
                "transport": "stdio",
                "verified": True,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from None
