"""Bounded pull-based SSH snapshot federation."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from .config import MAX_REMOTES, RemoteConfig, SwitchboardConfig
from .domain import HostId, ValidationError
from .protocol import (
    FLEET_VERSION,
    FleetEnvelope,
    FleetError,
    FleetHost,
    FleetReachability,
    FleetSource,
    PresentationPlanEnvelope,
    ProtocolError,
    SessionActionEnvelope,
    SnapshotEnvelope,
)
from .storage import IdentityConflict, Registry, StorageError
from .tui_gateway import CommandOutput, GatewayError, run_bounded_command

SSH_CONNECT_TIMEOUT_SECONDS = 5
REMOTE_SNAPSHOT_TIMEOUT_SECONDS = 20.0
MAX_CONCURRENT_SSH = 4
REMOTE_ACTION_TIMEOUT_SECONDS = 20.0
_REMOTE_COMMAND_TOKEN = re.compile(r"[A-Za-z0-9_./:@%+,=-]+\Z")
_REMOTE_COMMAND_OPTIONS = frozenset(
    {
        "--can-focus-desktop",
        "--can-launch-terminal",
        "--checkout",
        "--client",
        "--continue-json-stdin",
        "--create",
        "--has-current-terminal",
        "--json",
        "--json-stdin",
        "--project",
        "--provider",
        "--request-id",
    }
)

Clock = Callable[[], int]
AsyncRunner = Callable[[Sequence[str], float, bytes | None], Awaitable[CommandOutput]]
type RemoteParser[T] = Callable[[str | bytes | bytearray], T]


class RemoteError(RuntimeError):
    """One remote operation failed with a bounded public classification."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class RemoteSnapshotResult:
    remote: RemoteConfig
    completed_at: int
    snapshot: SnapshotEnvelope | None = None
    error: RemoteError | None = None


def _clock_ms() -> int:
    return time.time_ns() // 1_000_000


def snapshot_ssh_argv(
    remote: RemoteConfig,
    *,
    refresh: bool,
    ssh_executable: str = "ssh",
) -> tuple[str, ...]:
    reconcile = "full" if refresh else "none"
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
        "snapshot",
        "--reconcile",
        reconcile,
        "--json",
    )


def _remote_command_token(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or _REMOTE_COMMAND_TOKEN.fullmatch(value) is None
        or (value.startswith("-") and value not in _REMOTE_COMMAND_OPTIONS)
    ):
        raise RemoteError(
            "remote_argument_invalid",
            "A remote command argument is not a safe bounded token.",
            retryable=False,
        )
    return value


def action_ssh_argv(
    remote: RemoteConfig,
    arguments: Sequence[str],
    *,
    ssh_executable: str = "ssh",
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
        *(_remote_command_token(argument) for argument in arguments),
    )


def attach_ssh_argv(
    remote: RemoteConfig,
    surface_id: str,
    *,
    ssh_executable: str = "ssh",
) -> tuple[str, ...]:
    return (
        ssh_executable,
        "-tt",
        "--",
        remote.ssh_target,
        "swbctl",
        "attach-surface",
        _remote_command_token(surface_id),
    )


def _gateway_remote_error(error: GatewayError) -> RemoteError:
    codes = {
        "command_timeout": "ssh_timeout",
        "stdout_overflow": "remote_snapshot_overflow",
        "stderr_overflow": "ssh_stderr_overflow",
        "executable_not_found": "ssh_not_found",
        "executable_permission_denied": "ssh_permission_denied",
        "executable_start_failed": "ssh_start_failed",
    }
    code = codes.get(error.code, "ssh_failed")
    messages = {
        "ssh_timeout": "The remote snapshot request timed out.",
        "remote_snapshot_overflow": "The remote snapshot exceeded its byte limit.",
        "ssh_stderr_overflow": "The SSH client produced too much diagnostic output.",
        "ssh_not_found": "The SSH executable was not found.",
        "ssh_permission_denied": "The SSH executable is not runnable.",
        "ssh_start_failed": "The SSH client could not be started.",
        "ssh_failed": "The remote snapshot request failed.",
    }
    return RemoteError(code, messages[code], retryable=error.retryable)


def resolve_remote_host(
    registry: Registry,
    config: SwitchboardConfig,
    host_id: HostId,
) -> RemoteConfig:
    """Resolve one declared, pinned endpoint without exposing its target."""

    materialize_remote_endpoints(registry, config, observed_at=_clock_ms())
    configured = {remote.alias: remote for remote in config.remotes}
    matches = [
        row
        for row in registry.list_remotes(declared_only=True)
        if row.get("remote_host_id") == str(host_id)
        and str(row["remote_name"]) in configured
    ]
    if not matches:
        raise RemoteError(
            "remote_host_unknown",
            "The requested host has no declared pinned remote endpoint.",
            retryable=False,
        )
    if len(matches) != 1:
        raise RemoteError(
            "remote_host_ambiguous",
            "The requested host resolves to more than one remote endpoint.",
            retryable=False,
        )
    return configured[str(matches[0]["remote_name"])]


async def invoke_remote_json[T: PresentationPlanEnvelope | SessionActionEnvelope](
    remote: RemoteConfig,
    arguments: Sequence[str],
    parser: RemoteParser[T],
    *,
    stdin: bytes | None = None,
    runner: AsyncRunner = run_bounded_command,
    ssh_executable: str = "ssh",
) -> T:
    """Run one bounded noninteractive remote action and validate one envelope."""

    try:
        output = await runner(
            action_ssh_argv(
                remote,
                arguments,
                ssh_executable=ssh_executable,
            ),
            REMOTE_ACTION_TIMEOUT_SECONDS,
            stdin,
        )
    except GatewayError as error:
        raise _gateway_remote_error(error) from error
    if output.exit_code != 0:
        raise RemoteError(
            "remote_action_failed",
            "The remote action command exited unsuccessfully.",
            retryable=True,
        )
    if output.stderr:
        raise RemoteError(
            "remote_action_invalid",
            "The remote action emitted unexpected diagnostics.",
            retryable=False,
        )
    if (
        not output.stdout.endswith(b"\n")
        or b"\n" in output.stdout[:-1]
        or b"\r" in output.stdout
    ):
        raise RemoteError(
            "remote_action_invalid",
            "The remote action did not emit one JSON record.",
            retryable=False,
        )
    payload = output.stdout[:-1]
    if not payload or payload[:1].isspace() or payload[-1:].isspace():
        raise RemoteError(
            "remote_action_invalid",
            "The remote action emitted invalid JSON framing.",
            retryable=False,
        )
    try:
        return parser(payload)
    except (ProtocolError, ValidationError, ValueError) as error:
        raise RemoteError(
            "remote_action_invalid",
            "The remote action returned an incompatible response.",
            retryable=False,
        ) from error


async def invoke_remote_empty(
    remote: RemoteConfig,
    arguments: Sequence[str],
    *,
    runner: AsyncRunner = run_bounded_command,
    ssh_executable: str = "ssh",
) -> None:
    try:
        output = await runner(
            action_ssh_argv(
                remote,
                arguments,
                ssh_executable=ssh_executable,
            ),
            REMOTE_ACTION_TIMEOUT_SECONDS,
            None,
        )
    except GatewayError as error:
        raise _gateway_remote_error(error) from error
    if output.exit_code != 0:
        raise RemoteError(
            "remote_action_failed",
            "The remote action command exited unsuccessfully.",
            retryable=True,
        )
    if output.stdout or output.stderr:
        raise RemoteError(
            "remote_action_invalid",
            "The remote action emitted unexpected output.",
            retryable=False,
        )


async def fetch_remote_snapshot(
    remote: RemoteConfig,
    *,
    refresh: bool,
    runner: AsyncRunner = run_bounded_command,
    clock: Clock = _clock_ms,
    ssh_executable: str = "ssh",
) -> RemoteSnapshotResult:
    try:
        output = await runner(
            snapshot_ssh_argv(
                remote,
                refresh=refresh,
                ssh_executable=ssh_executable,
            ),
            REMOTE_SNAPSHOT_TIMEOUT_SECONDS,
            None,
        )
    except GatewayError as error:
        return RemoteSnapshotResult(
            remote,
            clock(),
            error=_gateway_remote_error(error),
        )
    if output.exit_code != 0:
        return RemoteSnapshotResult(
            remote,
            clock(),
            error=RemoteError(
                "ssh_failed",
                "The remote snapshot command exited unsuccessfully.",
                retryable=True,
            ),
        )
    try:
        snapshot = SnapshotEnvelope.from_json(output.stdout)
    except ProtocolError as error:
        return RemoteSnapshotResult(
            remote,
            clock(),
            error=RemoteError(
                error.code,
                "The remote returned an incompatible or invalid snapshot.",
                retryable=False,
            ),
        )
    return RemoteSnapshotResult(remote, clock(), snapshot=snapshot)


async def fetch_remote_snapshots(
    remotes: Sequence[RemoteConfig],
    *,
    refresh: bool,
    runner: AsyncRunner = run_bounded_command,
    clock: Clock = _clock_ms,
    ssh_executable: str = "ssh",
) -> tuple[RemoteSnapshotResult, ...]:
    if len(remotes) > MAX_REMOTES:
        raise RemoteError(
            "remote_count_exceeded",
            f"At most {MAX_REMOTES} remotes may be refreshed.",
            retryable=False,
        )
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SSH)

    async def fetch(remote: RemoteConfig) -> RemoteSnapshotResult:
        async with semaphore:
            return await fetch_remote_snapshot(
                remote,
                refresh=refresh,
                runner=runner,
                clock=clock,
                ssh_executable=ssh_executable,
            )

    return tuple(await asyncio.gather(*(fetch(remote) for remote in remotes)))


def materialize_remote_endpoints(
    registry: Registry,
    config: SwitchboardConfig,
    *,
    observed_at: int,
) -> None:
    configured = {remote.alias: remote for remote in config.remotes}
    for remote in config.remotes:
        registry.upsert_remote(
            remote.alias,
            remote.ssh_target,
            remote.display_name,
            declared=True,
            observed_at=observed_at,
        )
    for retained in registry.list_remotes(declared_only=True):
        alias = str(retained["remote_name"])
        if alias in configured:
            continue
        registry.upsert_remote(
            alias,
            str(retained["ssh_target"]),
            str(retained["display_name"]),
            declared=False,
            observed_at=observed_at,
        )


def refresh_remote_cache(
    registry: Registry,
    config: SwitchboardConfig,
    *,
    local_host_id: HostId,
    runner: AsyncRunner = run_bounded_command,
    clock: Clock = _clock_ms,
    ssh_executable: str = "ssh",
) -> None:
    started_at = clock()
    materialize_remote_endpoints(registry, config, observed_at=started_at)
    results = asyncio.run(
        fetch_remote_snapshots(
            config.remotes,
            refresh=True,
            runner=runner,
            clock=clock,
            ssh_executable=ssh_executable,
        )
    )
    for result in results:
        error = result.error
        snapshot = result.snapshot
        if snapshot is not None and snapshot.host.host_id == local_host_id:
            snapshot = None
            error = RemoteError(
                "remote_host_is_local",
                "The remote endpoint returned the local host identity.",
                retryable=False,
            )
        if snapshot is not None:
            try:
                registry.store_remote_snapshot(
                    result.remote.alias,
                    snapshot.to_dict(),
                    remote_host_id=str(snapshot.host.host_id),
                    schema_version=2,
                    protocol_version=2,
                    observed_at=snapshot.generated_at,
                    received_at=result.completed_at,
                )
                continue
            except (IdentityConflict, StorageError, ValidationError) as failure:
                error = RemoteError(
                    "remote_snapshot_rejected",
                    str(failure),
                    retryable=False,
                )
        assert error is not None
        try:
            registry.mark_remote_failure(
                result.remote.alias,
                error_code=error.code,
                error_detail=error.message,
                attempted_at=result.completed_at,
            )
        except StorageError:
            # Another frontend may already have committed a newer completion.
            # That state wins; an older result must not make the whole fleet fail.
            continue


def build_fleet_envelope(
    local_snapshot: SnapshotEnvelope,
    remote_rows: Sequence[dict[str, object]],
    *,
    generated_at: int,
    staleness_interval_seconds: int,
) -> FleetEnvelope:
    def optional_int(value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StorageError("remote cache contains an invalid timestamp")
        return value

    local = FleetHost(
        source=FleetSource.LOCAL,
        remote_name=None,
        host_id=local_snapshot.host.host_id,
        display_name=local_snapshot.host.display_name,
        reachability=FleetReachability.ONLINE,
        snapshot_observed_at=local_snapshot.generated_at,
        snapshot_received_at=generated_at,
        last_attempt_at=generated_at,
        stale=False,
        error=None,
        snapshot=local_snapshot,
    )
    hosts: list[FleetHost] = [local]
    known_host_ids = {local_snapshot.host.host_id}
    projects = {
        str(record["projectId"]): dict(record) for record in local_snapshot.projects
    }
    repositories = {
        str(record["repositoryId"]): dict(record)
        for record in local_snapshot.repositories
    }
    memberships = {
        (str(record["projectId"]), str(record["repositoryId"])): dict(record)
        for record in local_snapshot.project_repositories
    }
    checkout_ids = {str(record["checkoutId"]) for record in local_snapshot.checkouts}

    def catalog_conflict(snapshot: SnapshotEnvelope) -> str | None:
        for record in snapshot.projects:
            identity = str(record["projectId"])
            if identity in projects and projects[identity] != dict(record):
                return "A remote project identity conflicts with another host."
        for record in snapshot.repositories:
            identity = str(record["repositoryId"])
            if identity in repositories and repositories[identity] != dict(record):
                return "A remote repository identity conflicts with another host."
        for record in snapshot.project_repositories:
            identity = (str(record["projectId"]), str(record["repositoryId"]))
            if identity in memberships and memberships[identity] != dict(record):
                return "A remote project membership conflicts with another host."
        if checkout_ids.intersection(
            str(record["checkoutId"]) for record in snapshot.checkouts
        ):
            return "A remote checkout identity conflicts with another host."
        return None

    def accept_catalog(snapshot: SnapshotEnvelope) -> None:
        projects.update(
            (str(record["projectId"]), dict(record)) for record in snapshot.projects
        )
        repositories.update(
            (str(record["repositoryId"]), dict(record))
            for record in snapshot.repositories
        )
        memberships.update(
            (
                (str(record["projectId"]), str(record["repositoryId"])),
                dict(record),
            )
            for record in snapshot.project_repositories
        )
        checkout_ids.update(str(record["checkoutId"]) for record in snapshot.checkouts)

    for row in sorted(remote_rows, key=lambda value: str(value["remote_name"])):
        if not bool(row["declared"]):
            continue
        raw_snapshot = row.get("snapshot")
        snapshot = (
            None if raw_snapshot is None else SnapshotEnvelope.from_dict(raw_snapshot)
        )
        reachability = FleetReachability(str(row["reachability"]))
        raw_error_code = row.get("error_code")
        error = (
            None
            if raw_error_code is None
            else FleetError(
                str(raw_error_code),
                str(row.get("error_detail") or "The remote request failed."),
                reachability is not FleetReachability.ONLINE,
            )
        )
        received_at = optional_int(row.get("snapshot_received_at"))
        routed_host_id = (
            None
            if row.get("remote_host_id") is None
            else HostId(str(row["remote_host_id"]))
        )
        conflict = None
        if snapshot is not None:
            if snapshot.host.host_id in known_host_ids:
                conflict = "A remote endpoint duplicates another owning host."
            else:
                conflict = catalog_conflict(snapshot)
        if conflict is not None:
            snapshot = None
            routed_host_id = None
            received_at = None
            reachability = FleetReachability.OFFLINE
            error = FleetError("remote_catalog_conflict", conflict, False)
        elif snapshot is not None:
            known_host_ids.add(snapshot.host.host_id)
            accept_catalog(snapshot)
        stale = (
            received_at is not None
            and generated_at - received_at > staleness_interval_seconds * 1000
        )
        hosts.append(
            FleetHost(
                source=FleetSource.REMOTE,
                remote_name=str(row["remote_name"]),
                host_id=routed_host_id,
                display_name=(
                    snapshot.host.display_name
                    if snapshot is not None
                    else str(row["display_name"])
                ),
                reachability=reachability,
                snapshot_observed_at=(
                    None
                    if snapshot is None
                    else optional_int(row.get("snapshot_observed_at"))
                ),
                snapshot_received_at=received_at,
                last_attempt_at=optional_int(row.get("last_attempt_at")),
                stale=stale,
                error=error,
                snapshot=snapshot,
            )
        )
    envelope = FleetEnvelope(
        generated_at=generated_at,
        local_host_id=local_snapshot.host.host_id,
        hosts=tuple(hosts),
    )
    return FleetEnvelope.from_dict(envelope.to_dict())


__all__ = [
    "FLEET_VERSION",
    "MAX_CONCURRENT_SSH",
    "REMOTE_ACTION_TIMEOUT_SECONDS",
    "REMOTE_SNAPSHOT_TIMEOUT_SECONDS",
    "RemoteError",
    "RemoteSnapshotResult",
    "action_ssh_argv",
    "attach_ssh_argv",
    "build_fleet_envelope",
    "fetch_remote_snapshot",
    "fetch_remote_snapshots",
    "invoke_remote_empty",
    "invoke_remote_json",
    "materialize_remote_endpoints",
    "refresh_remote_cache",
    "resolve_remote_host",
    "snapshot_ssh_argv",
]
