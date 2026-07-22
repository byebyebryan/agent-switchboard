"""Bounded HostState federation and fixed owner-host command routing."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

from .config import RemoteConfig, SwitchboardConfig
from .domain import FailureRecord, HostId, HostStateCache, Reachability
from .process import CommandOutput, ProcessError, run_bounded_command
from .protocol import HostState, PresentationDirective, ProtocolError
from .storage import ConflictError, Registry

SSH_CONNECT_TIMEOUT_SECONDS: Final = 5
REMOTE_TIMEOUT_SECONDS: Final = 12.0
MAX_CONCURRENT_SSH: Final = 4

Runner = Callable[[Sequence[str]], Awaitable[CommandOutput]]
Clock = Callable[[], int]


def _clock_ms() -> int:
    return int(time.time() * 1_000)


class RemoteError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RefreshResult:
    alias: str
    host_id: HostId | None
    reachability: Reachability
    error: FailureRecord | None


async def _default_runner(argv: Sequence[str]) -> CommandOutput:
    return await run_bounded_command(argv, timeout_seconds=REMOTE_TIMEOUT_SECONDS)


def host_state_argv(
    remote: RemoteConfig, *, ssh_executable: str = "ssh"
) -> tuple[str, ...]:
    return (
        ssh_executable,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
        "--",
        remote.ssh_target,
        "swbctl",
        "state",
        "host",
        "--json",
    )


def action_argv(
    remote: RemoteConfig,
    arguments: Sequence[str],
    *,
    ssh_executable: str = "ssh",
) -> tuple[str, ...]:
    allowed_literals = {
        "view",
        "open",
        "recover",
        "attach",
        "--host",
        "--view",
        "--project",
        "--recovery",
        "--request-id",
        "--can-focus-desktop",
        "--no-focus-desktop",
        "--can-launch-terminal",
        "--json",
    }
    command = tuple(arguments)
    if not command or any(
        not isinstance(item, str)
        or not item
        or len(item) > 512
        or any(character.isspace() for character in item)
        or (item.startswith("-") and item not in allowed_literals)
        for item in command
    ):
        raise RemoteError(
            "remote_argument_invalid",
            "remote action contains an unsafe argument",
            retryable=False,
        )
    return (
        ssh_executable,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
        "--",
        remote.ssh_target,
        "swbctl",
        *command,
    )


def attach_argv(
    remote: RemoteConfig,
    *,
    host_id: HostId,
    view_id: str,
    request_id: str,
    ssh_executable: str = "ssh",
) -> tuple[str, ...]:
    arguments = (
        "view",
        "attach",
        "--host",
        str(host_id),
        "--view",
        view_id,
        "--request-id",
        request_id,
    )
    validated = action_argv(remote, arguments, ssh_executable=ssh_executable)
    return (validated[0], "-tt", *validated[2:])


def _one_json(output: CommandOutput, *, operation: str) -> bytes:
    if output.exit_code != 0:
        raise RemoteError(
            "remote_command_failed", f"remote {operation} failed", retryable=True
        )
    if output.stderr:
        raise RemoteError(
            "remote_output_invalid",
            f"remote {operation} emitted diagnostics",
            retryable=False,
        )
    if (
        not output.stdout.endswith(b"\n")
        or b"\n" in output.stdout[:-1]
        or b"\r" in output.stdout
        or output.stdout[:1].isspace()
    ):
        raise RemoteError(
            "remote_output_invalid",
            f"remote {operation} did not emit one JSON record",
            retryable=False,
        )
    return output.stdout[:-1]


class RemoteRuntime:
    def __init__(
        self,
        config: SwitchboardConfig,
        registry: Registry,
        *,
        runner: Runner = _default_runner,
        clock: Clock = _clock_ms,
    ) -> None:
        self.config = config
        self.registry = registry
        self.runner = runner
        self.clock = clock

    def _remote_for_host(self, host_id: HostId) -> RemoteConfig:
        aliases = {
            cached.remote_name
            for cached in self.registry.cached_host_states()
            if cached.host_id == host_id
        }
        matches = [remote for remote in self.config.remotes if remote.alias in aliases]
        if len(matches) != 1:
            raise RemoteError(
                "remote_host_unknown",
                "host has no single pinned remote endpoint",
                retryable=False,
            )
        return matches[0]

    def attach_command(
        self, host_id: HostId, *, view_id: str, request_id: str
    ) -> tuple[str, ...]:
        return attach_argv(
            self._remote_for_host(host_id),
            host_id=host_id,
            view_id=view_id,
            request_id=request_id,
        )

    async def refresh(self, *, now: int) -> tuple[RefreshResult, ...]:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_SSH)

        async def collect(remote: RemoteConfig) -> RefreshResult:
            async with semaphore:
                try:
                    output = await self.runner(host_state_argv(remote))
                    state = HostState.from_json(
                        _one_json(output, operation="HostState read")
                    )
                    completed_at = max(now, self.clock())
                    if state.host_id == self.config.host.host_id:
                        raise RemoteError(
                            "remote_identity_invalid",
                            "remote returned the local host identity",
                            retryable=False,
                        )
                    encoded = state.to_json()
                    cached = self.registry.cache_host_state(
                        HostStateCache(
                            remote.alias,
                            state.host_id,
                            encoded,
                            hashlib.sha256(encoded.encode()).hexdigest(),
                            int(state.data["generatedAt"]),
                            completed_at,
                            completed_at,
                            Reachability.ONLINE,
                            None,
                        )
                    )
                    return RefreshResult(
                        remote.alias, cached.host_id, cached.reachability, None
                    )
                except (
                    ProcessError,
                    RemoteError,
                    ProtocolError,
                    ConflictError,
                ) as error:
                    completed_at = max(now, self.clock())
                    code = getattr(error, "code", "remote_state_invalid")
                    retryable = getattr(error, "retryable", False)
                    failure = FailureRecord(code, str(error)[:1024], retryable)
                    existing = next(
                        (
                            item
                            for item in self.registry.cached_host_states()
                            if item.remote_name == remote.alias
                        ),
                        None,
                    )
                    if existing is not None:
                        self.registry.cache_host_state(
                            HostStateCache(
                                existing.remote_name,
                                existing.host_id,
                                existing.state_json,
                                existing.content_hash,
                                existing.observed_at,
                                existing.received_at,
                                completed_at,
                                Reachability.OFFLINE,
                                failure,
                            )
                        )
                    return RefreshResult(
                        remote.alias,
                        None if existing is None else existing.host_id,
                        Reachability.OFFLINE,
                        failure,
                    )

        return tuple(
            await asyncio.gather(*(collect(remote) for remote in self.config.remotes))
        )

    async def directive(
        self, host_id: HostId, arguments: Sequence[str]
    ) -> PresentationDirective:
        remote = self._remote_for_host(host_id)
        try:
            output = await self.runner(action_argv(remote, arguments))
            directive = PresentationDirective.from_json(
                _one_json(output, operation="view action")
            )
        except (ProcessError, ProtocolError) as error:
            raise RemoteError(
                getattr(error, "code", "remote_action_invalid"),
                str(error)[:1024],
                retryable=getattr(error, "retryable", False),
            ) from error
        if directive.host_id != str(host_id):
            raise RemoteError(
                "remote_identity_invalid",
                "remote directive host does not match the routed owner",
                retryable=False,
            )
        return directive


__all__ = [
    "MAX_CONCURRENT_SSH",
    "REMOTE_TIMEOUT_SECONDS",
    "RemoteError",
    "RemoteRuntime",
    "action_argv",
    "attach_argv",
    "host_state_argv",
]
