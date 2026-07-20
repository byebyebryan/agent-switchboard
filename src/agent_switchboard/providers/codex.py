"""Bounded, read-only discovery through the Codex app-server.

The adapter deliberately retains only durable session identity and safe display
metadata.  In particular, app-server thread status is validated as protocol
shape but is not interpreted as evidence about another CLI process.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
import unicodedata
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from agent_switchboard import __version__
from agent_switchboard.domain import HostId, ProviderId, SessionKey, canonical_path

CODEX_TESTED_CONTRACT_MIN: Final = "0.144.6"
CODEX_TESTED_CONTRACT_MAX: Final = "0.144.6"
CODEX_0144_SCHEMA_FINGERPRINT: Final = (
    "5d8251e1e2f713a3c567c927386f84f2f94692d4721b90d8ff36d0ff92877621"
)

_CLIENT_INFO: Final = {
    "name": "agent_switchboard",
    "title": "Switchboard",
    "version": __version__,
}
_THREAD_LIST_BASE_PARAMS: Final = {
    "limit": 100,
    "sourceKinds": ["cli"],
    "archived": False,
    "useStateDbOnly": False,
}
_SCHEMA_FILENAME: Final = "codex_app_server_protocol.v2.schemas.json"
_VERSION_RE: Final = re.compile(
    r"(?:^|\s)(?:codex(?:-cli)?\s+)?(?P<version>\d+\.\d+\.\d+(?:[-+][^\s]+)?)$"
)
_THREAD_REQUIRED_FIELDS: Final = frozenset(
    {
        "cliVersion",
        "createdAt",
        "cwd",
        "ephemeral",
        "id",
        "modelProvider",
        "preview",
        "sessionId",
        "source",
        "status",
        "turns",
        "updatedAt",
    }
)
_STATUS_TYPES: Final = frozenset({"notLoaded", "idle", "systemError", "active"})
_ACTIVE_FLAGS: Final = frozenset({"waitingOnApproval", "waitingOnUserInput"})
_MAX_TIMESTAMP_SECONDS: Final = (2**63 - 1) // 1000
_MAX_JSON_NODES: Final = 1_000_000
_INITIALIZE_STRING_FIELDS: Final = (
    "userAgent",
    "codexHome",
    "platformFamily",
    "platformOs",
)
_HOOK_EVENT_NAMES: Final = frozenset(
    {
        "preToolUse",
        "permissionRequest",
        "postToolUse",
        "preCompact",
        "postCompact",
        "sessionStart",
        "userPromptSubmit",
        "subagentStart",
        "subagentStop",
        "stop",
    }
)
_HOOK_HANDLER_TYPES: Final = frozenset({"command", "prompt", "agent"})
_HOOK_SOURCES: Final = frozenset(
    {
        "system",
        "user",
        "project",
        "mdm",
        "sessionFlags",
        "plugin",
        "cloudRequirements",
        "cloudManagedConfig",
        "legacyManagedConfigFile",
        "legacyManagedConfigMdm",
        "unknown",
    }
)
_HOOK_TRUST_STATUSES: Final = frozenset({"managed", "untrusted", "trusted", "modified"})
_MAX_HOOKS: Final = 10_000
_MAX_HOOK_DIAGNOSTICS: Final = 1_000


class _InvalidJsonValue(ValueError):
    """Parsed JSON contains a value outside Switchboard's safe JSON subset."""


def _reject_json_constant(_value: str) -> None:
    raise _InvalidJsonValue("non-finite JSON number")


def _valid_unicode_scalar(value: str) -> bool:
    return not any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _validate_json_value(value: object) -> None:
    """Reject lone surrogates, nonfinite numbers, and pathological trees."""

    stack = [value]
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > _MAX_JSON_NODES:
            raise _InvalidJsonValue("JSON value contains too many nodes")
        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise _InvalidJsonValue("non-finite JSON number")
            continue
        if isinstance(current, str):
            if not _valid_unicode_scalar(current):
                raise _InvalidJsonValue("invalid Unicode scalar")
            continue
        if isinstance(current, list):
            stack.extend(current)
            continue
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str) or not _valid_unicode_scalar(key):
                    raise _InvalidJsonValue("invalid JSON object key")
                stack.append(item)
            continue
        raise _InvalidJsonValue("value is not JSON-compatible")


def _load_json(raw: bytes) -> object:
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
        _validate_json_value(value)
        return value
    except _InvalidJsonValue:
        raise
    except (RecursionError, UnicodeDecodeError, ValueError) as exc:
        raise _InvalidJsonValue("invalid JSON encoding") from exc


@dataclass(frozen=True, slots=True)
class CodexProviderIssue:
    """A stable, payload-free degradation suitable for protocol conversion."""

    code: str
    message: str
    retryable: bool
    stage: str
    feature: str | None = None
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class CodexCapabilityReport:
    """Provider capability evidence observed during one discovery attempt."""

    available: bool
    provider_version: str | None
    tested_contract_min: str
    tested_contract_max: str
    features: tuple[str, ...]
    schema_fingerprint: str | None
    degraded_reasons: tuple[CodexProviderIssue, ...]


@dataclass(frozen=True, slots=True)
class NormalizedCodexSession:
    """The complete provider metadata Switchboard is allowed to retain."""

    provider_session_id: UUID
    cwd: Path
    name: str | None
    created_at: int
    provider_updated_at: int
    last_activity_at: int

    def storage_record(self, host_id: HostId, *, observed_at: int) -> dict[str, Any]:
        """Build Phase 2's storage input without provider-private fields."""

        key = SessionKey(host_id, ProviderId.CODEX, self.provider_session_id)
        record: dict[str, Any] = {
            "session_key": str(key),
            "host_id": str(host_id),
            "provider": ProviderId.CODEX.value,
            "provider_session_id": str(self.provider_session_id),
            "name": self.name,
            "cwd": str(self.cwd),
            "created_at": self.created_at,
            "provider_updated_at": self.provider_updated_at,
            "last_activity_at": self.last_activity_at,
            "last_observed_at": observed_at,
            "metadata_source": "provider",
        }
        return record


@dataclass(frozen=True, slots=True)
class CodexDiscoveryResult:
    """All-or-nothing session scan plus independently observed capabilities."""

    complete: bool
    sessions: tuple[NormalizedCodexSession, ...]
    capability: CodexCapabilityReport


@dataclass(frozen=True, slots=True)
class CodexHookMetadata:
    event_name: str
    handler_type: str
    command: str | None
    matcher: str | None
    source: str
    source_path: Path
    timeout_seconds: int
    status_message: str | None
    enabled: bool
    trust_status: str
    is_managed: bool
    current_hash: str


@dataclass(frozen=True, slots=True)
class CodexHookSourceEntry:
    cwd: Path
    hooks: tuple[CodexHookMetadata, ...]
    warnings: tuple[str, ...]
    errors: tuple[tuple[Path, str], ...]


@dataclass(frozen=True, slots=True)
class CodexHooksInspection:
    available: bool
    provider_version: str | None
    entries: tuple[CodexHookSourceEntry, ...]
    issues: tuple[CodexProviderIssue, ...]


class _ProviderFailure(Exception):
    def __init__(self, issue: CodexProviderIssue) -> None:
        self.issue = issue
        super().__init__(issue.message)


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int
    stdout: bytes


def _issue(
    code: str,
    message: str,
    *,
    retryable: bool,
    stage: str,
    feature: str | None = None,
    blocking: bool = True,
) -> CodexProviderIssue:
    return CodexProviderIssue(code, message, retryable, stage, feature, blocking)


def _failure(
    code: str,
    message: str,
    *,
    retryable: bool,
    stage: str,
    feature: str | None = None,
) -> _ProviderFailure:
    return _ProviderFailure(
        _issue(
            code,
            message,
            retryable=retryable,
            stage=stage,
            feature=feature,
        )
    )


def canonical_json_fingerprint(value: object) -> str:
    """Hash one parsed JSON value using the documented canonical byte stream."""

    _validate_json_value(value)
    try:
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, UnicodeEncodeError, ValueError) as exc:
        raise _InvalidJsonValue("JSON value cannot be canonicalized") from exc
    return hashlib.sha256(canonical).hexdigest()


def _stop_process(
    process: subprocess.Popen[bytes],
    timeout: float,
    *,
    graceful: bool = False,
) -> None:
    """Reap a child with bounded EOF, terminate, and kill waits."""

    if graceful and process.poll() is None:
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=timeout)

    if process.poll() is None:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=timeout)


def _run_bounded_command(
    argv: list[str],
    *,
    timeout: float,
    cleanup_timeout: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    environment: Mapping[str, str] | None = None,
) -> _CommandResult:
    """Run a small provider probe without unbounded communicate buffers."""

    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise _failure(
            "provider_not_found",
            "The configured Codex executable was not found.",
            retryable=False,
            stage="spawn",
        ) from exc
    except OSError as exc:
        raise _failure(
            "provider_start_failed",
            "The configured Codex executable could not be started.",
            retryable=False,
            stage="spawn",
        ) from exc

    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    stdout = bytearray()
    stderr_seen = 0
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _failure(
                    "provider_command_timeout",
                    "A Codex capability probe exceeded its deadline.",
                    retryable=True,
                    stage="command",
                )
            events = selector.select(remaining)
            if not events:
                continue
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    if len(stdout) + len(chunk) > max_stdout_bytes:
                        raise _failure(
                            "provider_output_too_large",
                            "A Codex capability probe exceeded its output limit.",
                            retryable=False,
                            stage="command",
                        )
                    stdout.extend(chunk)
                else:
                    # Drain continuously, but never retain provider stderr.
                    stderr_seen += len(chunk)
                    if stderr_seen > max_stderr_bytes:
                        raise _failure(
                            "provider_stderr_limit",
                            "A Codex capability probe exceeded its stderr limit.",
                            retryable=False,
                            stage="command",
                        )

        remaining = max(0.0, deadline - time.monotonic())
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise _failure(
                "provider_command_timeout",
                "A Codex capability probe exceeded its deadline.",
                retryable=True,
                stage="command",
            ) from exc
        return _CommandResult(returncode, bytes(stdout))
    finally:
        selector.close()
        _stop_process(process, cleanup_timeout)
        process.stdout.close()
        process.stderr.close()


def _read_schema_file(path: Path, *, maximum: int, timeout: float) -> bytes:
    """Read one private regular schema file without following special files."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise _failure(
            "schema_output_missing",
            "The generated Codex protocol schema is unavailable.",
            retryable=True,
            stage="schema",
            feature="schema_fingerprint",
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise _failure(
            "schema_output_unsafe",
            "The generated Codex protocol schema is not a regular file.",
            retryable=False,
            stage="schema",
            feature="schema_fingerprint",
        )
    if metadata.st_size > maximum:
        raise _failure(
            "schema_too_large",
            "The generated Codex protocol schema exceeds its size limit.",
            retryable=False,
            stage="schema",
            feature="schema_fingerprint",
        )

    flags = os.O_RDONLY | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _failure(
                "schema_output_unsafe",
                "The generated Codex protocol schema is not a regular file.",
                retryable=False,
                stage="schema",
                feature="schema_fingerprint",
            )
        deadline = time.monotonic() + timeout
        content = bytearray()
        while len(content) <= maximum:
            if time.monotonic() >= deadline:
                raise _failure(
                    "schema_read_timeout",
                    "Reading the generated Codex protocol schema timed out.",
                    retryable=True,
                    stage="schema",
                    feature="schema_fingerprint",
                )
            try:
                chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(content)))
            except BlockingIOError as exc:
                raise _failure(
                    "schema_read_blocked",
                    "The generated Codex protocol schema could not be read safely.",
                    retryable=True,
                    stage="schema",
                    feature="schema_fingerprint",
                ) from exc
            if not chunk:
                return bytes(content)
            content.extend(chunk)
        raise _failure(
            "schema_too_large",
            "The generated Codex protocol schema exceeds its size limit.",
            retryable=False,
            stage="schema",
            feature="schema_fingerprint",
        )
    except _ProviderFailure:
        raise
    except (OSError, ValueError) as exc:
        raise _failure(
            "schema_read_failed",
            "The generated Codex protocol schema could not be read safely.",
            retryable=True,
            stage="schema",
            feature="schema_fingerprint",
        ) from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


class _AppServer:
    """One bounded newline-delimited JSON-RPC app-server connection."""

    def __init__(
        self,
        executable: str,
        *,
        request_timeout: float,
        total_timeout: float,
        cleanup_timeout: float,
        max_line_bytes: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        max_messages: int,
        feature: str = "app_server_thread_list",
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.feature = feature
        try:
            self.process = subprocess.Popen(
                [executable, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise _failure(
                "provider_not_found",
                "The configured Codex executable was not found.",
                retryable=False,
                stage="spawn",
                feature=self.feature,
            ) from exc
        except OSError as exc:
            raise _failure(
                "app_server_start_failed",
                "The Codex app-server could not be started.",
                retryable=True,
                stage="discovery",
                feature=self.feature,
            ) from exc

        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self.request_timeout = request_timeout
        self.cleanup_timeout = cleanup_timeout
        self.max_line_bytes = max_line_bytes
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.max_messages = max_messages
        self.deadline = time.monotonic() + total_timeout
        self._selector = selectors.DefaultSelector()
        self._selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
        self._selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        for stream in (
            self.process.stdin,
            self.process.stdout,
            self.process.stderr,
        ):
            os.set_blocking(stream.fileno(), False)
        self._stdout = bytearray()
        self._lines: deque[bytes] = deque()
        self._stdout_seen = 0
        self._stderr_seen = 0
        self._seen_messages = 0
        self._next_id = 1
        self._stdout_eof = False

    def __enter__(self) -> _AppServer:
        try:
            initialize = self.request(
                "initialize",
                {"clientInfo": dict(_CLIENT_INFO)},
                request_id=0,
            )
            _validate_initialize_result(initialize)
            self.notify("initialized", {})
            return self
        except _ProviderFailure as failure:
            self.close()
            tagged = self._retag(failure)
            if tagged is failure:
                raise
            raise tagged from failure
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._selector.close()
        finally:
            if self.process.stdin is not None and not self.process.stdin.closed:
                with suppress(OSError):
                    self.process.stdin.close()
            _stop_process(self.process, self.cleanup_timeout, graceful=True)
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None and not stream.closed:
                    with suppress(OSError):
                        stream.close()

    def _send(self, message: Mapping[str, Any], deadline: float) -> None:
        try:
            _validate_json_value(message)
            payload = (
                json.dumps(
                    message,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
        except (RecursionError, UnicodeEncodeError, ValueError) as exc:
            raise _failure(
                "invalid_app_server_request",
                "A Codex app-server request could not be encoded safely.",
                retryable=False,
                stage="discovery",
                feature="app_server_thread_list",
            ) from exc
        if len(payload) > self.max_line_bytes:
            raise _failure(
                "request_too_large",
                "A Codex app-server request exceeded its size limit.",
                retryable=False,
                stage="discovery",
                feature="app_server_thread_list",
            )
        assert self.process.stdin is not None
        written = 0
        registered = False
        try:
            self._selector.register(
                self.process.stdin,
                selectors.EVENT_WRITE,
                "stdin",
            )
            registered = True
            while written < len(payload):
                events = self._select(deadline)
                for key, _ in events:
                    if key.data != "stdin":
                        self._read_stream(key)
                        continue
                    try:
                        count = os.write(
                            self.process.stdin.fileno(),
                            memoryview(payload)[written:],
                        )
                    except BlockingIOError:
                        continue
                    except (BrokenPipeError, OSError) as exc:
                        raise _failure(
                            "app_server_closed",
                            "The Codex app-server closed before discovery completed.",
                            retryable=True,
                            stage="discovery",
                            feature="app_server_thread_list",
                        ) from exc
                    if count <= 0:
                        raise _failure(
                            "app_server_closed",
                            "The Codex app-server closed before discovery completed.",
                            retryable=True,
                            stage="discovery",
                            feature="app_server_thread_list",
                        )
                    written += count
        finally:
            if registered:
                with suppress(KeyError):
                    self._selector.unregister(self.process.stdin)

    def _retag(self, failure: _ProviderFailure) -> _ProviderFailure:
        issue = failure.issue
        if issue.feature == self.feature:
            return failure
        return _ProviderFailure(
            CodexProviderIssue(
                issue.code,
                issue.message,
                issue.retryable,
                issue.stage,
                self.feature,
                issue.blocking,
            )
        )

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        deadline = min(self.deadline, time.monotonic() + self.request_timeout)
        try:
            self._send({"method": method, "params": dict(params)}, deadline)
        except _ProviderFailure as failure:
            tagged = self._retag(failure)
            if tagged is failure:
                raise
            raise tagged from failure

    def request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        request_id: int | None = None,
    ) -> dict[str, Any]:
        try:
            return self._request(method, params, request_id=request_id)
        except _ProviderFailure as failure:
            tagged = self._retag(failure)
            if tagged is failure:
                raise
            raise tagged from failure

    def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        request_id: int | None = None,
    ) -> dict[str, Any]:
        if request_id is None:
            request_id = self._next_id
            self._next_id += 1
        request_deadline = min(
            self.deadline,
            time.monotonic() + self.request_timeout,
        )
        self._send(
            {"method": method, "id": request_id, "params": dict(params)},
            request_deadline,
        )
        while True:
            raw = self._next_line(request_deadline)
            self._seen_messages += 1
            if self._seen_messages > self.max_messages:
                raise _failure(
                    "app_server_message_limit",
                    "The Codex app-server exceeded its response message limit.",
                    retryable=False,
                    stage="discovery",
                    feature="app_server_thread_list",
                )
            try:
                message = _load_json(raw)
            except _InvalidJsonValue as exc:
                raise _failure(
                    "app_server_malformed_json",
                    "The Codex app-server returned malformed JSON.",
                    retryable=False,
                    stage="discovery",
                    feature="app_server_thread_list",
                ) from exc
            if not isinstance(message, dict):
                raise _failure(
                    "app_server_invalid_message",
                    "The Codex app-server returned a non-object message.",
                    retryable=False,
                    stage="discovery",
                    feature="app_server_thread_list",
                )
            response_id = message.get("id")
            if type(response_id) is not int or response_id != request_id:
                # Notifications and responses to unrelated IDs may interleave.
                continue
            if "error" in message and message["error"] is not None:
                error = message["error"]
                if (
                    not isinstance(error, dict)
                    or type(error.get("code")) is not int
                    or not isinstance(error.get("message"), str)
                ):
                    raise _failure(
                        "app_server_invalid_error",
                        "The Codex app-server returned an invalid error object.",
                        retryable=False,
                        stage="discovery",
                        feature="app_server_thread_list",
                    )
                if error["code"] in {-32700, -32600, -32601, -32602}:
                    raise _failure(
                        "app_server_incompatible_rpc",
                        "The Codex app-server rejected the discovery contract.",
                        retryable=False,
                        stage="discovery",
                        feature="app_server_thread_list",
                    )
                raise _failure(
                    "app_server_rpc_error",
                    "The Codex app-server rejected a discovery request.",
                    retryable=True,
                    stage="discovery",
                    feature="app_server_thread_list",
                )
            if "result" not in message or not isinstance(message["result"], dict):
                raise _failure(
                    "app_server_invalid_result",
                    "The Codex app-server returned an invalid result object.",
                    retryable=False,
                    stage="discovery",
                    feature="app_server_thread_list",
                )
            return message["result"]

    def _next_line(self, deadline: float) -> bytes:
        while True:
            if self._lines:
                return self._lines.popleft()
            if self._stdout_eof:
                code = (
                    "app_server_incomplete_line"
                    if self._stdout
                    else "app_server_closed"
                )
                message = (
                    "The Codex app-server returned an incomplete response."
                    if self._stdout
                    else "The Codex app-server closed before discovery completed."
                )
                raise _failure(
                    code,
                    message,
                    retryable=True,
                    stage="discovery",
                    feature="app_server_thread_list",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _failure(
                    "app_server_timeout",
                    "The Codex app-server exceeded a discovery deadline.",
                    retryable=True,
                    stage="discovery",
                    feature="app_server_thread_list",
                )
            events = self._select(deadline)
            for key, _ in events:
                self._read_stream(key)

    def _select(self, deadline: float) -> list[tuple[selectors.SelectorKey, int]]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _failure(
                "app_server_timeout",
                "The Codex app-server exceeded a discovery deadline.",
                retryable=True,
                stage="discovery",
                feature="app_server_thread_list",
            )
        events = self._selector.select(remaining)
        if not events:
            raise _failure(
                "app_server_timeout",
                "The Codex app-server exceeded a discovery deadline.",
                retryable=True,
                stage="discovery",
                feature="app_server_thread_list",
            )
        return events

    def _read_stream(self, key: selectors.SelectorKey) -> None:
        try:
            chunk = os.read(key.fileobj.fileno(), 64 * 1024)
        except BlockingIOError:
            return
        if not chunk:
            self._selector.unregister(key.fileobj)
            if key.data == "stdout":
                self._stdout_eof = True
            return
        if key.data == "stderr":
            self._stderr_seen += len(chunk)
            if self._stderr_seen > self.max_stderr_bytes:
                raise _failure(
                    "app_server_stderr_limit",
                    "The Codex app-server exceeded its stderr limit.",
                    retryable=False,
                    stage="discovery",
                    feature="app_server_thread_list",
                )
            return
        self._stdout_seen += len(chunk)
        if self._stdout_seen > self.max_stdout_bytes:
            raise _failure(
                "app_server_stdout_limit",
                "The Codex app-server exceeded its cumulative stdout limit.",
                retryable=False,
                stage="discovery",
                feature="app_server_thread_list",
            )
        self._stdout.extend(chunk)
        self._extract_lines()

    def _extract_lines(self) -> None:
        while True:
            newline = self._stdout.find(b"\n")
            if newline < 0:
                if len(self._stdout) > self.max_line_bytes:
                    raise _failure(
                        "app_server_line_too_large",
                        "The Codex app-server exceeded its response line limit.",
                        retryable=False,
                        stage="discovery",
                        feature="app_server_thread_list",
                    )
                return
            if newline > self.max_line_bytes:
                raise _failure(
                    "app_server_line_too_large",
                    "The Codex app-server exceeded its response line limit.",
                    retryable=False,
                    stage="discovery",
                    feature="app_server_thread_list",
                )
            line = bytes(self._stdout[:newline])
            del self._stdout[: newline + 1]
            if line.strip():
                self._lines.append(line)
                if len(self._lines) > self.max_messages:
                    raise _failure(
                        "app_server_message_limit",
                        "The Codex app-server exceeded its response message limit.",
                        retryable=False,
                        stage="discovery",
                        feature="app_server_thread_list",
                    )


def _integer_seconds(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_TIMESTAMP_SECONDS
    ):
        raise _failure(
            "invalid_thread_shape",
            f"A Codex thread has an invalid {field} field.",
            retryable=False,
            stage="normalization",
            feature="app_server_thread_list",
        )
    return value * 1000


def _safe_name(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _failure(
            "invalid_thread_shape",
            "A Codex thread has an invalid name field.",
            retryable=False,
            stage="normalization",
            feature="app_server_thread_list",
        )
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        return None
    if (
        len(normalized) > 256
        or not _valid_unicode_scalar(normalized)
        or any(unicodedata.category(char) == "Cc" for char in normalized)
    ):
        # Name is optional and may originate in user content.  Omit an unsafe
        # display value without discarding otherwise sound durable metadata.
        return None
    return normalized


def _validate_status(value: object) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise _failure(
            "invalid_thread_shape",
            "A Codex thread has an invalid status field.",
            retryable=False,
            stage="normalization",
            feature="app_server_thread_list",
        )
    status_type = value["type"]
    if status_type not in _STATUS_TYPES:
        raise _failure(
            "invalid_thread_shape",
            "A Codex thread has an unsupported status shape.",
            retryable=False,
            stage="normalization",
            feature="app_server_thread_list",
        )
    if status_type != "active":
        return
    flags = value.get("activeFlags")
    if not isinstance(flags, list) or any(flag not in _ACTIVE_FLAGS for flag in flags):
        raise _failure(
            "invalid_thread_shape",
            "A Codex thread has an invalid active status shape.",
            retryable=False,
            stage="normalization",
            feature="app_server_thread_list",
        )


def _validate_initialize_result(value: Mapping[str, Any]) -> None:
    if any(
        not isinstance(value.get(field), str) for field in _INITIALIZE_STRING_FIELDS
    ):
        raise _failure(
            "invalid_initialize_result",
            "The Codex app-server returned an incompatible initialize result.",
            retryable=False,
            stage="discovery",
            feature="app_server_thread_list",
        )


def _hook_failure(message: str) -> _ProviderFailure:
    return _failure(
        "invalid_hooks_list",
        message,
        retryable=False,
        stage="normalization",
        feature="hooks_list",
    )


def _hook_text(
    value: object,
    field: str,
    *,
    optional: bool = False,
    maximum: int = 16_384,
) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise _hook_failure(f"Codex hooks/list returned an invalid {field} field.")
    normalized = unicodedata.normalize("NFC", value)
    if not _valid_unicode_scalar(normalized) or any(
        unicodedata.category(character) == "Cc" for character in normalized
    ):
        raise _hook_failure(f"Codex hooks/list returned an invalid {field} field.")
    return normalized


def _hook_path(value: object, field: str, *, absolute: bool) -> Path:
    text = _hook_text(value, field, maximum=4096)
    assert text is not None
    path = Path(text)
    if absolute and not path.is_absolute():
        raise _hook_failure(f"Codex hooks/list returned a non-absolute {field} field.")
    return path


def _normalize_hook_metadata(value: object) -> CodexHookMetadata:
    if not isinstance(value, dict):
        raise _hook_failure("Codex hooks/list returned a non-object hook.")
    event_name = _hook_text(value.get("eventName"), "eventName", maximum=64)
    handler_type = _hook_text(value.get("handlerType"), "handlerType", maximum=64)
    source = _hook_text(value.get("source"), "source", maximum=64)
    trust_status = _hook_text(value.get("trustStatus"), "trustStatus", maximum=64)
    if event_name not in _HOOK_EVENT_NAMES:
        raise _hook_failure("Codex hooks/list returned an unknown eventName.")
    if handler_type not in _HOOK_HANDLER_TYPES:
        raise _hook_failure("Codex hooks/list returned an unknown handlerType.")
    if source not in _HOOK_SOURCES:
        raise _hook_failure("Codex hooks/list returned an unknown source.")
    if trust_status not in _HOOK_TRUST_STATUSES:
        raise _hook_failure("Codex hooks/list returned an unknown trustStatus.")
    timeout = value.get("timeoutSec")
    if type(timeout) is not int or not 0 <= timeout <= 86_400:
        raise _hook_failure("Codex hooks/list returned an invalid timeoutSec field.")
    for field in ("enabled", "isManaged"):
        if type(value.get(field)) is not bool:
            raise _hook_failure(f"Codex hooks/list returned an invalid {field} field.")
    current_hash = _hook_text(value.get("currentHash"), "currentHash", maximum=4096)
    assert event_name is not None
    assert handler_type is not None
    assert source is not None
    assert trust_status is not None
    assert current_hash is not None
    return CodexHookMetadata(
        event_name=event_name,
        handler_type=handler_type,
        command=_hook_text(value.get("command"), "command", optional=True),
        matcher=_hook_text(
            value.get("matcher"), "matcher", optional=True, maximum=4096
        ),
        source=source,
        source_path=_hook_path(value.get("sourcePath"), "sourcePath", absolute=True),
        timeout_seconds=timeout,
        status_message=_hook_text(
            value.get("statusMessage"),
            "statusMessage",
            optional=True,
            maximum=4096,
        ),
        enabled=value["enabled"],
        trust_status=trust_status,
        is_managed=value["isManaged"],
        current_hash=current_hash,
    )


def _normalize_hooks_list(value: Mapping[str, Any]) -> tuple[CodexHookSourceEntry, ...]:
    data = value.get("data")
    if not isinstance(data, list) or len(data) > _MAX_HOOK_DIAGNOSTICS:
        raise _hook_failure("Codex hooks/list returned an invalid data field.")
    entries: list[CodexHookSourceEntry] = []
    hook_count = 0
    diagnostic_count = 0
    for raw_entry in data:
        if not isinstance(raw_entry, dict):
            raise _hook_failure("Codex hooks/list returned a non-object entry.")
        raw_hooks = raw_entry.get("hooks")
        raw_warnings = raw_entry.get("warnings")
        raw_errors = raw_entry.get("errors")
        if not all(
            isinstance(item, list) for item in (raw_hooks, raw_warnings, raw_errors)
        ):
            raise _hook_failure("Codex hooks/list returned an invalid entry shape.")
        hook_count += len(raw_hooks)
        diagnostic_count += len(raw_warnings) + len(raw_errors)
        if hook_count > _MAX_HOOKS or diagnostic_count > _MAX_HOOK_DIAGNOSTICS:
            raise _hook_failure("Codex hooks/list exceeded its bounded result limits.")
        warnings: list[str] = []
        for warning in raw_warnings:
            normalized = _hook_text(warning, "warning", maximum=4096)
            assert normalized is not None
            warnings.append(normalized)
        errors: list[tuple[Path, str]] = []
        for error in raw_errors:
            if not isinstance(error, dict):
                raise _hook_failure("Codex hooks/list returned an invalid error.")
            message = _hook_text(error.get("message"), "error message", maximum=4096)
            assert message is not None
            errors.append(
                (
                    _hook_path(error.get("path"), "error path", absolute=False),
                    message,
                )
            )
        entries.append(
            CodexHookSourceEntry(
                cwd=_hook_path(raw_entry.get("cwd"), "cwd", absolute=True),
                hooks=tuple(_normalize_hook_metadata(hook) for hook in raw_hooks),
                warnings=tuple(warnings),
                errors=tuple(errors),
            )
        )
    return tuple(entries)


def _normalize_thread(value: object) -> NormalizedCodexSession | None:
    if not isinstance(value, dict):
        raise _failure(
            "invalid_thread_shape",
            "The Codex app-server returned a non-object thread.",
            retryable=False,
            stage="normalization",
            feature="app_server_thread_list",
        )
    missing = _THREAD_REQUIRED_FIELDS - value.keys()
    if missing:
        raise _failure(
            "invalid_thread_shape",
            "A Codex thread is missing required metadata.",
            retryable=False,
            stage="normalization",
            feature="app_server_thread_list",
        )
    source = value["source"]
    ephemeral = value["ephemeral"]
    if not isinstance(source, str) or not isinstance(ephemeral, bool):
        raise _failure(
            "invalid_thread_shape",
            "A Codex thread has invalid durability metadata.",
            retryable=False,
            stage="normalization",
            feature="app_server_thread_list",
        )
    if source != "cli" or ephemeral:
        return None

    for field in ("cliVersion", "modelProvider", "preview", "sessionId"):
        if not isinstance(value[field], str):
            raise _failure(
                "invalid_thread_shape",
                "A Codex thread has invalid required metadata.",
                retryable=False,
                stage="normalization",
                feature="app_server_thread_list",
            )
    if not isinstance(value["turns"], list):
        raise _failure(
            "invalid_thread_shape",
            "A Codex thread has an invalid turns field.",
            retryable=False,
            stage="normalization",
            feature="app_server_thread_list",
        )
    _validate_status(value["status"])

    try:
        provider_session_id = UUID(value["id"])
    except (AttributeError, TypeError, ValueError) as exc:
        raise _failure(
            "invalid_thread_identity",
            "A Codex thread has an invalid session identifier.",
            retryable=False,
            stage="normalization",
            feature="app_server_thread_list",
        ) from exc
    if provider_session_id.int == 0:
        raise _failure(
            "invalid_thread_identity",
            "A Codex thread has an invalid session identifier.",
            retryable=False,
            stage="normalization",
            feature="app_server_thread_list",
        )

    cwd = value["cwd"]
    if (
        not isinstance(cwd, str)
        or len(cwd) > 4096
        or not _valid_unicode_scalar(cwd)
        or not Path(cwd).is_absolute()
        or any(unicodedata.category(char) == "Cc" for char in cwd)
    ):
        raise _failure(
            "invalid_thread_cwd",
            "A Codex thread has an invalid working directory.",
            retryable=False,
            stage="normalization",
            feature="app_server_thread_list",
        )
    try:
        normalized_cwd = canonical_path(cwd)
    except (OSError, ValueError) as exc:
        raise _failure(
            "invalid_thread_cwd",
            "A Codex thread has an invalid working directory.",
            retryable=False,
            stage="normalization",
            feature="app_server_thread_list",
        ) from exc

    created_at = _integer_seconds(value["createdAt"], "createdAt")
    provider_updated_at = _integer_seconds(value["updatedAt"], "updatedAt")
    recency = value.get("recencyAt")
    last_activity_at = (
        provider_updated_at
        if recency is None
        else _integer_seconds(recency, "recencyAt")
    )
    if provider_updated_at < created_at or last_activity_at < created_at:
        raise _failure(
            "invalid_thread_timestamps",
            "A Codex thread has impossible timestamp ordering.",
            retryable=False,
            stage="normalization",
            feature="app_server_thread_list",
        )
    return NormalizedCodexSession(
        provider_session_id=provider_session_id,
        cwd=normalized_cwd,
        name=_safe_name(value.get("name")),
        created_at=created_at,
        provider_updated_at=provider_updated_at,
        last_activity_at=last_activity_at,
    )


class CodexProvider:
    """Production read-only Codex discovery adapter."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        request_timeout: float = 5.0,
        total_timeout: float = 15.0,
        command_timeout: float = 5.0,
        cleanup_timeout: float = 1.0,
        max_line_bytes: int = 4 * 1024 * 1024,
        max_stdout_bytes: int = 64 * 1024 * 1024,
        max_schema_bytes: int = 8 * 1024 * 1024,
        max_stderr_bytes: int = 64 * 1024,
        max_pages: int = 1000,
        max_sessions: int = 10_000,
        max_cursor_bytes: int = 16 * 1024,
        max_messages: int = 10_000,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if executable is not None and (
            not isinstance(executable, str) or not executable or "\x00" in executable
        ):
            raise ValueError("Codex executable must be a non-empty string")
        self.executable = executable or "codex"
        if environment is not None and any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or "\x00" in key
            or "\x00" in value
            or "=" in key
            for key, value in environment.items()
        ):
            raise ValueError("Codex provider environment must contain safe strings")
        self.environment = None if environment is None else dict(environment)
        self.request_timeout = request_timeout
        self.total_timeout = total_timeout
        self.command_timeout = command_timeout
        self.cleanup_timeout = cleanup_timeout
        self.max_line_bytes = max_line_bytes
        self.max_stdout_bytes = max_stdout_bytes
        self.max_schema_bytes = max_schema_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.max_pages = max_pages
        self.max_sessions = max_sessions
        self.max_cursor_bytes = max_cursor_bytes
        self.max_messages = max_messages
        timeouts = {
            "request_timeout": request_timeout,
            "total_timeout": total_timeout,
            "command_timeout": command_timeout,
            "cleanup_timeout": cleanup_timeout,
        }
        counts = {
            "max_line_bytes": max_line_bytes,
            "max_stdout_bytes": max_stdout_bytes,
            "max_schema_bytes": max_schema_bytes,
            "max_stderr_bytes": max_stderr_bytes,
            "max_pages": max_pages,
            "max_sessions": max_sessions,
            "max_cursor_bytes": max_cursor_bytes,
            "max_messages": max_messages,
        }
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in timeouts.values()
        ):
            raise ValueError("Codex provider timeouts must be finite and positive")
        if any(type(value) is not int or value <= 0 for value in counts.values()):
            raise ValueError(
                "Codex provider count and byte bounds must be positive ints"
            )

    def discover_sessions(self) -> CodexDiscoveryResult:
        """Return a complete validated scan or an empty incomplete result."""

        try:
            provider_version = self._provider_version()
        except _ProviderFailure as failure:
            return self._failed_result(None, (failure.issue,))

        issues: list[CodexProviderIssue] = []
        schema_fingerprint: str | None = None
        try:
            schema_fingerprint = self._schema_fingerprint()
        except _ProviderFailure as failure:
            issues.append(failure.issue)

        if (
            provider_version == CODEX_TESTED_CONTRACT_MIN
            and schema_fingerprint is not None
            and schema_fingerprint != CODEX_0144_SCHEMA_FINGERPRINT
        ):
            issues.append(
                _issue(
                    "schema_contract_mismatch",
                    "The Codex schema differs from the tested contract fixture.",
                    retryable=False,
                    stage="schema",
                    feature="schema_fingerprint",
                )
            )

        if provider_version != CODEX_TESTED_CONTRACT_MIN:
            issues.append(
                _issue(
                    "untested_provider_version",
                    "The installed Codex version is outside the tested contract range.",
                    retryable=False,
                    stage="version",
                    feature="app_server_thread_list",
                    blocking=False,
                )
            )

        try:
            sessions = self._discover_all_pages()
        except _ProviderFailure as failure:
            return self._failed_result(
                provider_version,
                (*issues, failure.issue),
                schema_fingerprint=schema_fingerprint,
            )

        features = ["app_server_thread_list"]
        if schema_fingerprint is not None:
            features.append("schema_fingerprint")
        capability = CodexCapabilityReport(
            available=True,
            provider_version=provider_version,
            tested_contract_min=CODEX_TESTED_CONTRACT_MIN,
            tested_contract_max=CODEX_TESTED_CONTRACT_MAX,
            features=tuple(features),
            schema_fingerprint=schema_fingerprint,
            degraded_reasons=tuple(issues),
        )
        return CodexDiscoveryResult(True, sessions, capability)

    def inspect_hooks(
        self, *, cwds: tuple[str | Path, ...] = ()
    ) -> CodexHooksInspection:
        """Inspect effective Codex hooks through the supported app-server RPC."""

        try:
            provider_version = self._provider_version()
        except _ProviderFailure as failure:
            issue = failure.issue
            return CodexHooksInspection(
                False,
                None,
                (),
                (
                    CodexProviderIssue(
                        issue.code,
                        issue.message,
                        issue.retryable,
                        issue.stage,
                        "hooks_list",
                        issue.blocking,
                    ),
                ),
            )
        issues: list[CodexProviderIssue] = []
        if provider_version != CODEX_TESTED_CONTRACT_MIN:
            issues.append(
                _issue(
                    "untested_provider_version",
                    "The installed Codex version is outside the tested contract range.",
                    retryable=False,
                    stage="version",
                    feature="hooks_list",
                    blocking=False,
                )
            )
        try:
            entries = self._inspect_hooks(cwds)
        except _ProviderFailure as failure:
            return CodexHooksInspection(
                False,
                provider_version,
                (),
                (*issues, failure.issue),
            )
        return CodexHooksInspection(True, provider_version, entries, tuple(issues))

    def _failed_result(
        self,
        provider_version: str | None,
        issues: tuple[CodexProviderIssue, ...],
        *,
        schema_fingerprint: str | None = None,
    ) -> CodexDiscoveryResult:
        features = ("schema_fingerprint",) if schema_fingerprint is not None else ()
        capability = CodexCapabilityReport(
            available=False,
            provider_version=provider_version,
            tested_contract_min=CODEX_TESTED_CONTRACT_MIN,
            tested_contract_max=CODEX_TESTED_CONTRACT_MAX,
            features=features,
            schema_fingerprint=schema_fingerprint,
            degraded_reasons=issues,
        )
        return CodexDiscoveryResult(False, (), capability)

    def _provider_version(self) -> str:
        result = _run_bounded_command(
            [self.executable, "--version"],
            timeout=self.command_timeout,
            cleanup_timeout=self.cleanup_timeout,
            max_stdout_bytes=4096,
            max_stderr_bytes=self.max_stderr_bytes,
            environment=self.environment,
        )
        if result.returncode != 0:
            raise _failure(
                "provider_version_failed",
                "Codex version detection failed.",
                retryable=True,
                stage="version",
            )
        try:
            output = result.stdout.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise _failure(
                "provider_version_invalid",
                "Codex returned an invalid version string.",
                retryable=False,
                stage="version",
            ) from exc
        match = _VERSION_RE.fullmatch(output)
        if match is None:
            raise _failure(
                "provider_version_invalid",
                "Codex returned an invalid version string.",
                retryable=False,
                stage="version",
            )
        return match.group("version")

    def _schema_fingerprint(self) -> str:
        with tempfile.TemporaryDirectory(
            prefix="agent-switchboard-codex-schema-"
        ) as raw:
            directory = Path(raw)
            os.chmod(directory, 0o700)
            try:
                result = _run_bounded_command(
                    [
                        self.executable,
                        "app-server",
                        "generate-json-schema",
                        "--out",
                        str(directory),
                    ],
                    timeout=self.command_timeout,
                    cleanup_timeout=self.cleanup_timeout,
                    max_stdout_bytes=64 * 1024,
                    max_stderr_bytes=self.max_stderr_bytes,
                    environment=self.environment,
                )
            except _ProviderFailure as failure:
                issue = failure.issue
                raise _failure(
                    issue.code,
                    issue.message,
                    retryable=issue.retryable,
                    stage="schema",
                    feature="schema_fingerprint",
                ) from failure
            if result.returncode != 0:
                raise _failure(
                    "schema_generation_failed",
                    "Codex protocol schema generation failed.",
                    retryable=True,
                    stage="schema",
                    feature="schema_fingerprint",
                )
            schema_path = directory / _SCHEMA_FILENAME
            try:
                raw_schema = _read_schema_file(
                    schema_path,
                    maximum=self.max_schema_bytes,
                    timeout=self.command_timeout,
                )
                parsed = _load_json(raw_schema)
            except _ProviderFailure:
                raise
            except _InvalidJsonValue as exc:
                raise _failure(
                    "schema_invalid",
                    "The generated Codex protocol schema is invalid.",
                    retryable=False,
                    stage="schema",
                    feature="schema_fingerprint",
                ) from exc
            if (
                not isinstance(parsed, dict)
                or parsed.get("type") != "object"
                or not isinstance(parsed.get("title"), str)
                or not isinstance(parsed.get("$schema"), str)
                or not isinstance(parsed.get("definitions"), dict)
                or not {
                    "Thread",
                    "ThreadListParams",
                    "ThreadListResponse",
                }.issubset(parsed["definitions"])
            ):
                raise _failure(
                    "schema_invalid",
                    "The generated Codex protocol schema has an invalid shape.",
                    retryable=False,
                    stage="schema",
                    feature="schema_fingerprint",
                )
            try:
                return canonical_json_fingerprint(parsed)
            except _InvalidJsonValue as exc:
                raise _failure(
                    "schema_invalid",
                    "The generated Codex protocol schema cannot be canonicalized.",
                    retryable=False,
                    stage="schema",
                    feature="schema_fingerprint",
                ) from exc

    def _discover_all_pages(self) -> tuple[NormalizedCodexSession, ...]:
        discovered: dict[UUID, NormalizedCodexSession] = {}
        cursors: set[str] = set()
        cursor: str | None = None
        with _AppServer(
            self.executable,
            request_timeout=self.request_timeout,
            total_timeout=self.total_timeout,
            cleanup_timeout=self.cleanup_timeout,
            max_line_bytes=self.max_line_bytes,
            max_stdout_bytes=self.max_stdout_bytes,
            max_stderr_bytes=self.max_stderr_bytes,
            max_messages=self.max_messages,
            environment=self.environment,
        ) as server:
            for _page in range(self.max_pages):
                params = dict(_THREAD_LIST_BASE_PARAMS)
                if cursor is not None:
                    params["cursor"] = cursor
                result = server.request("thread/list", params)
                data = result.get("data")
                if not isinstance(data, list):
                    raise _failure(
                        "invalid_thread_list",
                        "The Codex app-server returned an invalid thread list.",
                        retryable=False,
                        stage="discovery",
                        feature="app_server_thread_list",
                    )
                if len(data) > _THREAD_LIST_BASE_PARAMS["limit"]:
                    raise _failure(
                        "thread_list_page_limit",
                        "The Codex app-server exceeded the requested page size.",
                        retryable=False,
                        stage="discovery",
                        feature="app_server_thread_list",
                    )
                backwards_cursor = result.get("backwardsCursor")
                if backwards_cursor is not None and not isinstance(
                    backwards_cursor, str
                ):
                    raise _failure(
                        "invalid_pagination_cursor",
                        "The Codex app-server returned an invalid pagination cursor.",
                        retryable=False,
                        stage="discovery",
                        feature="app_server_thread_list",
                    )
                next_cursor = result.get("nextCursor")
                if next_cursor is not None:
                    try:
                        cursor_size = len(next_cursor.encode("utf-8"))
                    except (AttributeError, UnicodeEncodeError) as exc:
                        raise _failure(
                            "invalid_pagination_cursor",
                            "The Codex app-server returned an invalid "
                            "pagination cursor.",
                            retryable=False,
                            stage="discovery",
                            feature="app_server_thread_list",
                        ) from exc
                    if (
                        not isinstance(next_cursor, str)
                        or not next_cursor
                        or cursor_size > self.max_cursor_bytes
                    ):
                        raise _failure(
                            "invalid_pagination_cursor",
                            "The Codex app-server returned an invalid "
                            "pagination cursor.",
                            retryable=False,
                            stage="discovery",
                            feature="app_server_thread_list",
                        )
                    if next_cursor in cursors:
                        raise _failure(
                            "repeated_pagination_cursor",
                            "The Codex app-server repeated a pagination cursor.",
                            retryable=True,
                            stage="discovery",
                            feature="app_server_thread_list",
                        )

                page_sessions = tuple(_normalize_thread(item) for item in data)
                for session in page_sessions:
                    if session is None:
                        continue
                    previous = discovered.get(session.provider_session_id)
                    if previous is not None and previous != session:
                        raise _failure(
                            "conflicting_thread_duplicate",
                            "Codex returned conflicting metadata for one session.",
                            retryable=True,
                            stage="normalization",
                            feature="app_server_thread_list",
                        )
                    discovered[session.provider_session_id] = session
                    if len(discovered) > self.max_sessions:
                        raise _failure(
                            "normalized_session_limit",
                            "Codex discovery exceeded its normalized session limit.",
                            retryable=False,
                            stage="normalization",
                            feature="app_server_thread_list",
                        )

                if next_cursor is None:
                    return tuple(discovered[key] for key in sorted(discovered, key=str))
                cursors.add(next_cursor)
                cursor = next_cursor

        raise _failure(
            "pagination_page_limit",
            "The Codex app-server exceeded its pagination page limit.",
            retryable=True,
            stage="discovery",
            feature="app_server_thread_list",
        )

    def _inspect_hooks(
        self, cwds: tuple[str | Path, ...]
    ) -> tuple[CodexHookSourceEntry, ...]:
        if len(cwds) > 32:
            raise _hook_failure("Codex hooks/list cwd count exceeds its bound.")
        normalized_cwds: list[str] = []
        for cwd in cwds:
            path = Path(cwd)
            if not path.is_absolute() or len(str(path)) > 4096:
                raise _hook_failure("Codex hooks/list requires absolute bounded cwds.")
            normalized_cwds.append(str(path))
        with _AppServer(
            self.executable,
            request_timeout=self.request_timeout,
            total_timeout=self.total_timeout,
            cleanup_timeout=self.cleanup_timeout,
            max_line_bytes=self.max_line_bytes,
            max_stdout_bytes=self.max_stdout_bytes,
            max_stderr_bytes=self.max_stderr_bytes,
            max_messages=self.max_messages,
            feature="hooks_list",
            environment=self.environment,
        ) as server:
            return _normalize_hooks_list(
                server.request("hooks/list", {"cwds": normalized_cwds})
            )
