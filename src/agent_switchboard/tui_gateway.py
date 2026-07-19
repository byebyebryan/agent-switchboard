"""Bounded public-command gateway for the terminal frontend."""

from __future__ import annotations

import asyncio
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

from .domain import PresentationContext, ProviderId, SessionKey, ValidationError
from .protocol import (
    MAX_JSON_BYTES,
    PresentationPlanEnvelope,
    ProtocolError,
    SessionActionEnvelope,
    SnapshotEnvelope,
)
from .tmux import TmuxController

MAX_STDOUT_BYTES = MAX_JSON_BYTES + 1
MAX_STDERR_BYTES = 64 * 1024
READ_CHUNK_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 15.0
PROCESS_REAP_SECONDS = 1.0

Envelope = TypeVar(
    "Envelope", SnapshotEnvelope, PresentationPlanEnvelope, SessionActionEnvelope
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


AsyncCommandRunner = Callable[[Sequence[str], float], Awaitable[CommandOutput]]


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


async def run_bounded_command(
    argv: Sequence[str],
    timeout_seconds: float,
) -> CommandOutput:
    """Run one shell-free argv with bounded streams and group cleanup."""

    command = tuple(argv)
    if not command:
        raise GatewayError(
            "command_invalid",
            "The Switchboard command is empty.",
            retryable=False,
        )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.DEVNULL,
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
    tasks = (stdout_task, stderr_task, wait_task)
    try:
        async with asyncio.timeout(timeout_seconds):
            stdout, stderr, exit_code = await asyncio.gather(*tasks)
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
    ) -> Envelope:
        output = await self._runner(
            (self.executable, *arguments),
            self.timeout_seconds,
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

    async def prepare_open(
        self,
        session_key: str,
        *,
        request_id: str,
        context: PresentationContext,
    ) -> PresentationPlanEnvelope:
        try:
            canonical_key = str(SessionKey.parse(session_key))
        except ValidationError as error:
            raise GatewayError(
                "argument_invalid",
                "Session key is invalid.",
                retryable=False,
            ) from error
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

    async def prepare_new(
        self,
        project_id: str,
        *,
        location_id: str | None,
        provider: str,
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
            "prepare-new",
            "--project",
            _uuid_argument(project_id, "project ID"),
        ]
        if location_id is not None:
            arguments.extend(("--location", _uuid_argument(location_id, "location ID")))
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
        location_id: str | None,
        request_id: str,
        context: PresentationContext,
    ) -> PresentationPlanEnvelope:
        arguments = [
            "prepare-history",
            "--project",
            _uuid_argument(project_id, "project ID"),
        ]
        if location_id is not None:
            arguments.extend(("--location", _uuid_argument(location_id, "location ID")))
        arguments.extend(("--request-id", _uuid_argument(request_id, "request ID")))
        arguments.extend(self._context_arguments(context))
        arguments.append("--json")
        envelope = await self._json(arguments, PresentationPlanEnvelope.from_json)
        self._validate_plan(envelope, context)
        return envelope

    async def stop_session(self, session_key: str) -> SessionActionEnvelope:
        try:
            canonical_key = str(SessionKey.parse(session_key))
        except ValidationError as error:
            raise GatewayError(
                "argument_invalid",
                "Session key is invalid.",
                retryable=False,
            ) from error
        return await self._json(
            ("stop-session", canonical_key, "--json"),
            SessionActionEnvelope.from_json,
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
