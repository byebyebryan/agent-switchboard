"""Bounded public-command gateway for the terminal frontend."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar
from uuid import UUID

from .domain import (
    MAX_HANDOFF_FIELD_BYTES,
    PresentationContext,
    ProviderId,
    SessionKey,
    ValidationError,
    normalize_handoff_text,
)
from .protocol import (
    MAX_JSON_BYTES,
    FleetEnvelope,
    PresentationPlanEnvelope,
    ProtocolError,
    SessionActionEnvelope,
    SessionDetailEnvelope,
    SnapshotEnvelope,
)
from .tmux import TmuxController

MAX_STDOUT_BYTES = MAX_JSON_BYTES + 1
MAX_STDERR_BYTES = 64 * 1024
MAX_STDIN_BYTES = 2 * MAX_HANDOFF_FIELD_BYTES + 8 * 1024
READ_CHUNK_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 15.0
PROCESS_REAP_SECONDS = 1.0

Envelope = TypeVar(
    "Envelope",
    SnapshotEnvelope,
    PresentationPlanEnvelope,
    SessionActionEnvelope,
    SessionDetailEnvelope,
    FleetEnvelope,
)
EnvelopeParser = Callable[[str | bytes | bytearray], Envelope]


class GatewayError(RuntimeError):
    """One bounded frontend command failed without exposing private output."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class _OutputOverflow(RuntimeError):
    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.stream = stream


@dataclass(frozen=True, slots=True)
class CommandOutput:
    stdout: bytes
    stderr: bytes
    exit_code: int


AsyncCommandRunner = Callable[
    [Sequence[str], float, bytes | None], Awaitable[CommandOutput]
]


class TmuxClientResolver(Protocol):
    def current_client(self, environment: Mapping[str, str]) -> str | None: ...


async def _read_bounded(
    stream: asyncio.StreamReader,
    *,
    limit: int,
    name: str,
) -> bytes:
    output = bytearray()
    while True:
        remaining = limit - len(output)
        chunk = await stream.read(min(READ_CHUNK_BYTES, remaining + 1))
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > limit:
            raise _OutputOverflow(name)


async def _kill_process_group(
    process: asyncio.subprocess.Process,
) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        await asyncio.wait_for(process.wait(), timeout=PROCESS_REAP_SECONDS)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=PROCESS_REAP_SECONDS)


async def _write_bounded_input(
    stream: asyncio.StreamWriter,
    payload: bytes,
) -> None:
    try:
        stream.write(payload)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stream.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await stream.wait_closed()


async def run_bounded_command(
    argv: Sequence[str],
    timeout_seconds: float,
    stdin: bytes | None = None,
) -> CommandOutput:
    """Run one shell-free argv with bounded streams and group cleanup."""

    command = tuple(argv)
    if not command:
        raise GatewayError(
            "command_invalid",
            "The Switchboard command is empty.",
            retryable=False,
        )
    if stdin is not None and len(stdin) > MAX_STDIN_BYTES:
        raise GatewayError(
            "stdin_overflow",
            "The Switchboard command input is too large.",
            retryable=False,
        )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as error:
        raise GatewayError(
            "executable_not_found",
            "The Switchboard executable was not found.",
            retryable=False,
        ) from error
    except PermissionError as error:
        raise GatewayError(
            "executable_permission_denied",
            "The Switchboard executable is not executable.",
            retryable=False,
        ) from error
    except OSError as error:
        raise GatewayError(
            "executable_start_failed",
            "The Switchboard executable could not be started.",
            retryable=True,
        ) from error

    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(
        _read_bounded(process.stdout, limit=MAX_STDOUT_BYTES, name="stdout")
    )
    stderr_task = asyncio.create_task(
        _read_bounded(process.stderr, limit=MAX_STDERR_BYTES, name="stderr")
    )
    wait_task = asyncio.create_task(process.wait())
    if stdin is not None:
        assert process.stdin is not None
    input_task = (
        None
        if stdin is None
        else asyncio.create_task(
            _write_bounded_input(
                process.stdin,
                stdin,
            )
        )
    )
    tasks = tuple(
        task
        for task in (stdout_task, stderr_task, wait_task, input_task)
        if task is not None
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            results = await asyncio.gather(*tasks)
    except BaseException as error:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _kill_process_group(process)
        if isinstance(error, TimeoutError):
            raise GatewayError(
                "command_timeout",
                "The Switchboard command exceeded its deadline.",
                retryable=True,
            ) from error
        if isinstance(error, _OutputOverflow):
            raise GatewayError(
                f"{error.stream}_overflow",
                f"The Switchboard command produced too much {error.stream}.",
                retryable=False,
            ) from error
        raise
    stdout, stderr, exit_code = results[:3]
    assert isinstance(stdout, bytes)
    assert isinstance(stderr, bytes)
    assert isinstance(exit_code, int)
    return CommandOutput(stdout, stderr, exit_code)


def resolve_terminal_context(
    *,
    environment: Mapping[str, str] | None = None,
    tmux: TmuxClientResolver | None = None,
) -> PresentationContext:
    """Resolve a plain terminal or the exact inherited tmux client."""

    current_environment = os.environ if environment is None else environment
    controller = TmuxController() if tmux is None else tmux
    client = controller.current_client(current_environment)
    return PresentationContext(True, client, False, False)


def _bounded_argument(value: object, field: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise GatewayError(
            "argument_invalid",
            f"{field} must be bounded text.",
            retryable=False,
        )
    if "\x00" in value or any(
        unicodedata.category(character) == "Cc" for character in value
    ):
        raise GatewayError(
            "argument_invalid",
            f"{field} contains control characters.",
            retryable=False,
        )
    return value


def _uuid_argument(value: object, field: str) -> str:
    text = _bounded_argument(value, field, maximum=36)
    try:
        return str(UUID(text))
    except ValueError as error:
        raise GatewayError(
            "argument_invalid",
            f"{field} must be a UUID.",
            retryable=False,
        ) from error


def _session_argument(value: object) -> str:
    try:
        return str(SessionKey.parse(value))
    except ValidationError as error:
        raise GatewayError(
            "argument_invalid",
            "Session key is invalid.",
            retryable=False,
        ) from error


def _curation_value(value: object, field: str, *, maximum: int) -> str:
    text = _bounded_argument(value, field, maximum=maximum).strip()
    if not text:
        raise GatewayError(
            "argument_invalid",
            f"{field} must not be empty.",
            retryable=False,
        )
    return text


class SwbctlGateway:
    """Invoke only the installed, versioned public Switchboard commands."""

    def __init__(
        self,
        executable: str | Path,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        runner: AsyncCommandRunner = run_bounded_command,
    ) -> None:
        path = Path(executable)
        if not path.is_absolute():
            raise GatewayError(
                "executable_invalid",
                "The Switchboard executable must be an absolute path.",
                retryable=False,
            )
        if not 0.1 <= timeout_seconds <= 60:
            raise GatewayError(
                "timeout_invalid",
                "The Switchboard timeout must be between 0.1 and 60 seconds.",
                retryable=False,
            )
        self.executable = str(path)
        self.timeout_seconds = float(timeout_seconds)
        self._runner = runner

    async def _json(
        self,
        arguments: Sequence[str],
        parser: EnvelopeParser[Envelope],
        *,
        stdin: bytes | None = None,
    ) -> Envelope:
        output = await self._runner(
            (self.executable, *arguments),
            self.timeout_seconds,
            stdin,
        )
        if output.exit_code != 0:
            raise GatewayError(
                "command_failed",
                "The Switchboard command failed.",
                retryable=True,
            )
        if output.stderr:
            raise GatewayError(
                "response_invalid",
                "The Switchboard command emitted unexpected diagnostics.",
                retryable=False,
            )
        if (
            not output.stdout.endswith(b"\n")
            or b"\n" in output.stdout[:-1]
            or b"\r" in output.stdout
        ):
            raise GatewayError(
                "response_invalid",
                "The Switchboard command did not emit one JSON record.",
                retryable=False,
            )
        payload = output.stdout[:-1]
        if not payload or payload[:1].isspace() or payload[-1:].isspace():
            raise GatewayError(
                "response_invalid",
                "The Switchboard command emitted invalid JSON framing.",
                retryable=False,
            )
        try:
            return parser(payload)
        except (ProtocolError, ValidationError, ValueError) as error:
            raise GatewayError(
                "response_invalid",
                "The Switchboard command emitted an incompatible response.",
                retryable=False,
            ) from error

    async def _empty(self, arguments: Sequence[str]) -> None:
        output = await self._runner(
            (self.executable, *arguments),
            self.timeout_seconds,
            None,
        )
        if output.exit_code != 0:
            raise GatewayError(
                "command_failed",
                "The Switchboard command failed.",
                retryable=True,
            )
        if output.stdout or output.stderr:
            raise GatewayError(
                "response_invalid",
                "The Switchboard command emitted unexpected output.",
                retryable=False,
            )

    async def _task_action(
        self,
        arguments: Sequence[str],
        *,
        task_id: str,
        stdin: bytes | None = None,
    ) -> Mapping[str, object]:
        output = await self._runner(
            (self.executable, *arguments), self.timeout_seconds, stdin
        )
        if output.exit_code != 0:
            raise GatewayError(
                "command_failed",
                "The Switchboard command failed.",
                retryable=True,
            )
        if output.stderr:
            raise GatewayError(
                "response_invalid",
                "The Switchboard command emitted unexpected diagnostics.",
                retryable=False,
            )
        if (
            not output.stdout.endswith(b"\n")
            or b"\n" in output.stdout[:-1]
            or b"\r" in output.stdout
        ):
            raise GatewayError(
                "response_invalid",
                "The Switchboard command did not emit one JSON record.",
                retryable=False,
            )
        try:
            payload = json.loads(output.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GatewayError(
                "response_invalid",
                "The Switchboard command emitted invalid JSON.",
                retryable=False,
            ) from error
        if (
            not isinstance(payload, dict)
            or payload.get("schemaVersion") != 2
            or payload.get("protocolVersion") != 2
            or not isinstance(payload.get("task"), dict)
            or payload["task"].get("taskId") != task_id
        ):
            raise GatewayError(
                "response_invalid",
                "The Switchboard command emitted an incompatible task response.",
                retryable=False,
            )
        return payload

    @staticmethod
    def _context_arguments(context: PresentationContext) -> tuple[str, ...]:
        if (
            not context.has_current_terminal
            or context.can_focus_desktop
            or context.can_launch_terminal
        ):
            raise GatewayError(
                "terminal_context_invalid",
                "The terminal frontend requires one terminal-local context.",
                retryable=False,
            )
        arguments = ["--has-current-terminal"]
        if context.current_tmux_client is not None:
            arguments.extend(
                (
                    "--current-tmux-client",
                    _bounded_argument(
                        context.current_tmux_client,
                        "tmux client",
                        maximum=1024,
                    ),
                )
            )
        return tuple(arguments)

    async def snapshot(self, *, reconcile: str) -> SnapshotEnvelope:
        if reconcile not in {"none", "live", "full"}:
            raise GatewayError(
                "argument_invalid",
                "Snapshot reconciliation mode is invalid.",
                retryable=False,
            )
        return await self._json(
            ("snapshot", "--reconcile", reconcile, "--json"),
            SnapshotEnvelope.from_json,
        )

    async def fleet(self, *, refresh: bool) -> FleetEnvelope:
        arguments = ["fleet"]
        if refresh:
            arguments.append("--refresh")
        arguments.append("--json")
        return await self._json(tuple(arguments), FleetEnvelope.from_json)

    async def prepare_open(
        self,
        session_key: str,
        *,
        request_id: str,
        context: PresentationContext,
    ) -> PresentationPlanEnvelope:
        canonical_key = _session_argument(session_key)
        envelope = await self._json(
            (
                "prepare-open",
                canonical_key,
                "--request-id",
                _uuid_argument(request_id, "request ID"),
                *self._context_arguments(context),
                "--json",
            ),
            PresentationPlanEnvelope.from_json,
        )
        self._validate_plan(envelope, context)
        return envelope

    async def prepare_task(
        self,
        task_id: str,
        *,
        provider: str | None,
        request_id: str,
        context: PresentationContext,
    ) -> PresentationPlanEnvelope:
        arguments = [
            "prepare-task",
            _uuid_argument(task_id, "task ID"),
        ]
        if provider is not None:
            try:
                provider_id = ProviderId(provider).value
            except ValueError as error:
                raise GatewayError(
                    "argument_invalid",
                    "Provider is invalid.",
                    retryable=False,
                ) from error
            arguments.extend(("--provider", provider_id))
        arguments.extend(("--request-id", _uuid_argument(request_id, "request ID")))
        arguments.extend(self._context_arguments(context))
        arguments.append("--json")
        envelope = await self._json(arguments, PresentationPlanEnvelope.from_json)
        self._validate_plan(envelope, context)
        return envelope

    async def prepare_task_create(
        self,
        task_id: str,
        *,
        project_id: str,
        title: str,
        checkout_id: str | None,
        provider: str,
        purpose: str | None = None,
        request_id: str,
        context: PresentationContext,
    ) -> PresentationPlanEnvelope:
        try:
            provider_id = ProviderId(provider).value
        except ValueError as error:
            raise GatewayError(
                "argument_invalid",
                "Provider is invalid.",
                retryable=False,
            ) from error
        arguments = [
            "prepare-task",
            _uuid_argument(task_id, "task ID"),
            "--create",
            "--project",
            _uuid_argument(project_id, "project ID"),
            "--title",
            _curation_value(title, "Task title", maximum=256),
        ]
        if purpose is not None:
            arguments.extend(
                ("--purpose", _curation_value(purpose, "Task purpose", maximum=4096))
            )
        if checkout_id is not None:
            arguments.extend(("--checkout", _uuid_argument(checkout_id, "checkout ID")))
        arguments.extend(
            (
                "--provider",
                provider_id,
                "--request-id",
                _uuid_argument(request_id, "request ID"),
            )
        )
        arguments.extend(self._context_arguments(context))
        arguments.append("--json")
        envelope = await self._json(arguments, PresentationPlanEnvelope.from_json)
        self._validate_plan(envelope, context)
        return envelope

    async def prepare_history(
        self,
        project_id: str,
        *,
        checkout_id: str | None,
        request_id: str,
        context: PresentationContext,
    ) -> PresentationPlanEnvelope:
        arguments = [
            "prepare-history",
            "--project",
            _uuid_argument(project_id, "project ID"),
        ]
        if checkout_id is not None:
            arguments.extend(("--checkout", _uuid_argument(checkout_id, "checkout ID")))
        arguments.extend(("--request-id", _uuid_argument(request_id, "request ID")))
        arguments.extend(self._context_arguments(context))
        arguments.append("--json")
        envelope = await self._json(arguments, PresentationPlanEnvelope.from_json)
        self._validate_plan(envelope, context)
        return envelope

    async def adopt_session(self, session_key: str, *, task_id: str) -> None:
        canonical_task_id = _uuid_argument(task_id, "task ID")
        await self._task_action(
            (
                "task",
                "adopt",
                _session_argument(session_key),
                "--task",
                canonical_task_id,
                "--json",
            ),
            task_id=canonical_task_id,
        )

    async def set_task_title(self, task_id: str, value: str) -> None:
        canonical_task_id = _uuid_argument(task_id, "task ID")
        await self._task_action(
            (
                "task",
                "title",
                canonical_task_id,
                _curation_value(value, "Task title", maximum=256),
                "--json",
            ),
            task_id=canonical_task_id,
        )

    async def set_task_purpose(self, task_id: str, value: str | None) -> None:
        canonical_task_id = _uuid_argument(task_id, "task ID")
        arguments = ["task", "purpose", canonical_task_id]
        if value is None:
            arguments.append("--clear")
        else:
            arguments.append(_curation_value(value, "Task purpose", maximum=4096))
        arguments.append("--json")
        await self._task_action(arguments, task_id=canonical_task_id)

    async def set_task_pinned(self, task_id: str, *, pinned: bool) -> None:
        if type(pinned) is not bool:
            raise GatewayError(
                "argument_invalid",
                "Pinned state must be boolean.",
                retryable=False,
            )
        canonical_task_id = _uuid_argument(task_id, "task ID")
        arguments = ["task", "pin", canonical_task_id]
        if not pinned:
            arguments.append("--off")
        arguments.append("--json")
        await self._task_action(arguments, task_id=canonical_task_id)

    async def reopen_task(self, task_id: str) -> None:
        canonical_task_id = _uuid_argument(task_id, "task ID")
        await self._task_action(
            ("task", "reopen", canonical_task_id, "--json"),
            task_id=canonical_task_id,
        )

    async def close_task(
        self,
        task_id: str,
        *,
        handoff_id: str | None,
        summary: str | None,
        next_action: str | None,
    ) -> None:
        canonical_task_id = _uuid_argument(task_id, "task ID")
        if handoff_id is None and summary is None and next_action is None:
            payload = b"{}"
        elif handoff_id is not None and summary is not None and next_action is not None:
            try:
                normalized_summary = normalize_handoff_text(summary, "summary")
                normalized_next_action = normalize_handoff_text(
                    next_action, "next action"
                )
            except ValidationError as error:
                raise GatewayError(
                    "argument_invalid", str(error), retryable=False
                ) from error
            payload = json.dumps(
                {
                    "handoffId": _uuid_argument(handoff_id, "handoff ID"),
                    "summary": normalized_summary,
                    "nextAction": normalized_next_action,
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        else:
            raise GatewayError(
                "argument_invalid",
                "Task close requires a complete handoff or no handoff fields.",
                retryable=False,
            )
        await self._task_action(
            ("task", "close", canonical_task_id, "--json-stdin", "--json"),
            task_id=canonical_task_id,
            stdin=payload,
        )

    async def stop_session(self, session_key: str) -> SessionActionEnvelope:
        canonical_key = _session_argument(session_key)
        envelope = await self._json(
            ("stop-session", canonical_key, "--json"),
            SessionActionEnvelope.from_json,
        )
        if str(envelope.action.session_key) != canonical_key:
            raise GatewayError(
                "response_invalid",
                "The Switchboard command emitted an incompatible response.",
                retryable=False,
            )
        return envelope

    async def session_detail(
        self,
        session_key: str,
        *,
        handoff_limit: int = 20,
    ) -> SessionDetailEnvelope:
        canonical_key = _session_argument(session_key)
        if (
            isinstance(handoff_limit, bool)
            or not isinstance(handoff_limit, int)
            or not 1 <= handoff_limit <= 100
        ):
            raise GatewayError(
                "argument_invalid",
                "Handoff limit must be between 1 and 100.",
                retryable=False,
            )
        envelope = await self._json(
            (
                "show",
                canonical_key,
                "--handoff-limit",
                str(handoff_limit),
                "--json",
            ),
            SessionDetailEnvelope.from_json,
        )
        self._validate_detail(envelope, canonical_key)
        return envelope

    async def set_session_name(
        self,
        session_key: str,
        value: str | None,
    ) -> SessionDetailEnvelope:
        return await self._edit_session(
            "name",
            session_key,
            None
            if value is None
            else _curation_value(value, "Session name", maximum=512),
        )

    async def set_session_purpose(
        self,
        session_key: str,
        value: str | None,
    ) -> SessionDetailEnvelope:
        return await self._edit_session(
            "purpose",
            session_key,
            None
            if value is None
            else _curation_value(value, "Session purpose", maximum=4096),
        )

    async def _edit_session(
        self,
        action: str,
        session_key: str,
        value: str | None,
    ) -> SessionDetailEnvelope:
        canonical_key = _session_argument(session_key)
        arguments = ["session", action, canonical_key]
        arguments.extend(("--clear",) if value is None else (value,))
        arguments.append("--json")
        envelope = await self._json(arguments, SessionDetailEnvelope.from_json)
        self._validate_detail(envelope, canonical_key)
        return envelope

    async def set_session_pinned(
        self,
        session_key: str,
        *,
        pinned: bool,
    ) -> SessionDetailEnvelope:
        if type(pinned) is not bool:
            raise GatewayError(
                "argument_invalid",
                "Pinned state must be boolean.",
                retryable=False,
            )
        canonical_key = _session_argument(session_key)
        arguments = ["session", "pin", canonical_key]
        if not pinned:
            arguments.append("--off")
        arguments.append("--json")
        envelope = await self._json(arguments, SessionDetailEnvelope.from_json)
        self._validate_detail(envelope, canonical_key)
        return envelope

    async def append_session_handoff(
        self,
        session_key: str,
        *,
        handoff_id: str,
        summary: str,
        next_action: str,
        wrap: bool,
    ) -> SessionDetailEnvelope:
        if type(wrap) is not bool:
            raise GatewayError(
                "argument_invalid",
                "Wrap state must be boolean.",
                retryable=False,
            )
        canonical_key = _session_argument(session_key)
        try:
            normalized_summary = normalize_handoff_text(summary, "summary")
            normalized_next_action = normalize_handoff_text(next_action, "next action")
        except ValidationError as error:
            raise GatewayError(
                "argument_invalid",
                str(error),
                retryable=False,
            ) from error
        payload = json.dumps(
            {
                "handoffId": _uuid_argument(handoff_id, "handoff ID"),
                "summary": normalized_summary,
                "nextAction": normalized_next_action,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > MAX_STDIN_BYTES:
            raise GatewayError(
                "stdin_overflow",
                "The Switchboard command input is too large.",
                retryable=False,
            )
        envelope = await self._json(
            (
                "session",
                "wrap" if wrap else "handoff",
                canonical_key,
                "--json-stdin",
                "--json",
            ),
            SessionDetailEnvelope.from_json,
            stdin=payload,
        )
        self._validate_detail(envelope, canonical_key)
        return envelope

    async def select_surface(self, surface_id: str, *, client: str) -> None:
        """Select one validated surface on the exact inherited tmux client."""

        await self._empty(
            (
                "select-surface",
                _uuid_argument(surface_id, "surface ID"),
                "--client",
                _bounded_argument(client, "tmux client", maximum=1024),
            )
        )

    def attach_surface_command(self, surface_id: str) -> tuple[str, ...]:
        """Build the public attachment command for post-TUI process replacement."""

        return (
            self.executable,
            "attach-surface",
            _uuid_argument(surface_id, "surface ID"),
        )

    @staticmethod
    def _validate_plan(
        envelope: PresentationPlanEnvelope,
        context: PresentationContext,
    ) -> None:
        try:
            envelope.plan.validate_for_context(context)
        except ProtocolError as error:
            raise GatewayError(
                "response_invalid",
                "The Switchboard plan is incompatible with this terminal.",
                retryable=False,
            ) from error

    @staticmethod
    def _validate_detail(
        envelope: SessionDetailEnvelope,
        session_key: str,
    ) -> None:
        if envelope.session["sessionKey"] != session_key:
            raise GatewayError(
                "response_invalid",
                "The Switchboard command emitted an incompatible response.",
                retryable=False,
            )


class SnapshotSource:
    """Coalesce full refreshes while preserving the last valid snapshot."""

    def __init__(self, gateway: SwbctlGateway) -> None:
        self.gateway = gateway
        self.last_good: SnapshotEnvelope | None = None
        self.last_error: GatewayError | None = None
        self._refresh_task: asyncio.Task[SnapshotEnvelope] | None = None

    def _finish_refresh(self, task: asyncio.Task[SnapshotEnvelope]) -> None:
        if self._refresh_task is task:
            self._refresh_task = None
        if task.cancelled():
            return
        try:
            snapshot = task.result()
        except GatewayError as error:
            self.last_error = error
        except BaseException:
            return
        else:
            self.last_good = snapshot
            self.last_error = None

    async def retained(self) -> SnapshotEnvelope:
        """Load retained state without hiding a prior valid result on failure."""

        try:
            snapshot = await self.gateway.snapshot(reconcile="none")
        except GatewayError as error:
            self.last_error = error
            if self.last_good is not None:
                return self.last_good
            raise
        self.last_good = snapshot
        self.last_error = None
        return snapshot

    async def refresh(self) -> SnapshotEnvelope:
        """Join or start one full refresh and retain explicit failure state."""

        task = self._refresh_task
        if task is None:
            task = asyncio.create_task(self.gateway.snapshot(reconcile="full"))
            self._refresh_task = task
            task.add_done_callback(self._finish_refresh)
        try:
            snapshot = await asyncio.shield(task)
        except GatewayError as error:
            self.last_error = error
            if self.last_good is not None:
                return self.last_good
            raise
        self.last_good = snapshot
        self.last_error = None
        return snapshot


class FleetSnapshotSource:
    """Coalesce fleet refreshes while retaining the last valid fleet."""

    def __init__(self, gateway: SwbctlGateway) -> None:
        self.gateway = gateway
        self.last_good: FleetEnvelope | None = None
        self.last_error: GatewayError | None = None
        self._refresh_task: asyncio.Task[FleetEnvelope] | None = None

    def _finish_refresh(self, task: asyncio.Task[FleetEnvelope]) -> None:
        if self._refresh_task is task:
            self._refresh_task = None
        if task.cancelled():
            return
        try:
            fleet = task.result()
        except GatewayError as error:
            self.last_error = error
        except BaseException:
            return
        else:
            self.last_good = fleet
            self.last_error = None

    async def retained(self) -> FleetEnvelope:
        try:
            fleet = await self.gateway.fleet(refresh=False)
        except GatewayError as error:
            self.last_error = error
            if self.last_good is not None:
                return self.last_good
            raise
        self.last_good = fleet
        self.last_error = None
        return fleet

    async def refresh(self) -> FleetEnvelope:
        task = self._refresh_task
        if task is None:
            task = asyncio.create_task(self.gateway.fleet(refresh=True))
            self._refresh_task = task
            task.add_done_callback(self._finish_refresh)
        try:
            fleet = await asyncio.shield(task)
        except GatewayError as error:
            self.last_error = error
            if self.last_good is not None:
                return self.last_good
            raise
        self.last_good = fleet
        self.last_error = None
        return fleet
