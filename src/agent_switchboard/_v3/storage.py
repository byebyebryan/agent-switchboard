"""Secure SQLite connection gate for the private Phase 6 registry."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final

from .domain import (
    CONTROL_EDGES,
    FRAME_EDGES,
    LAUNCH_EDGES,
    LEASE_EDGES,
    PLACEMENT_EDGES,
    RECOVERY_EDGES,
    REQUEST_EDGES,
    SURFACE_EDGES,
    TRANSITION_EDGES,
    TRANSPORT_EDGES,
    VIEW_EDGES,
    WORK_CONTEXT_EDGES,
    ActivationState,
    Activity,
    ActivityReason,
    AgentCapability,
    BackgroundState,
    BriefId,
    CapabilityId,
    Checkout,
    CheckoutId,
    ClaimState,
    CloseReason,
    CompletionHandoff,
    ControlKind,
    ControlState,
    ControlTransport,
    ControlTurn,
    ControlTurnId,
    CreatedBy,
    DesktopAttachmentLease,
    FailureRecord,
    Frame,
    FrameId,
    FrameLifecycleState,
    FramePlacement,
    FrameRole,
    FrameSession,
    GenerationId,
    HandoffId,
    HostId,
    HostStateCache,
    LaunchAction,
    LaunchId,
    LaunchIntent,
    LaunchState,
    LeaseId,
    LeaseState,
    PlacementId,
    PlacementState,
    Project,
    ProjectId,
    ProjectRepository,
    ProviderId,
    ProviderSession,
    Reachability,
    Recovery,
    RecoveryActionability,
    RecoveryId,
    RecoveryState,
    Repository,
    RepositoryId,
    RequestId,
    RequestRecord,
    RequestState,
    Resumability,
    RuntimePresence,
    SessionHandoff,
    SessionKey,
    Surface,
    SurfaceId,
    SurfaceState,
    TmuxServer,
    TmuxServerId,
    TransitionBrief,
    TransitionId,
    TransitionKind,
    TransitionState,
    TransportPhase,
    UserView,
    ViewId,
    ViewMode,
    ViewState,
    ViewTransition,
    WorkContext,
    WorkContextId,
    bounded_text,
    require_state_edge,
)
from .migrations import migrate

DEFAULT_BUSY_TIMEOUT_MS: Final = 5_000
MAX_BUSY_TIMEOUT_MS: Final = 30_000


class StorageError(RuntimeError):
    """Base Phase 6 registry error."""


class RegistryClosed(StorageError):
    """An operation was attempted after closing the registry."""


class ConflictError(StorageError):
    """A stable identity, state, or compare-and-swap precondition conflicts."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class WorkspaceResult:
    kind: str
    work_context: WorkContext
    frame: Frame


@dataclass(frozen=True, slots=True)
class TransitionClaim:
    kind: str
    transition_id: TransitionId
    target_frame_id: FrameId
    brief: str | None = None
    summary: str | None = None
    next_action: str | None = None


def now_ms() -> int:
    return int(time.time() * 1_000)


def _timestamp(value: int | None) -> int:
    timestamp = now_ms() if value is None else value
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise ValueError("timestamp must be a non-negative integer")
    return timestamp


def _failure_from_row(row: sqlite3.Row) -> FailureRecord | None:
    code = row["failure_code"]
    if code is None:
        return None
    return FailureRecord(
        str(code),
        str(row["failure_message"]),
        bool(row["failure_retryable"]),
    )


def _work_context(row: sqlite3.Row) -> WorkContext:
    return WorkContext(
        WorkContextId(row["work_context_id"]),
        HostId(row["host_id"]),
        ProjectId(row["project_id"]),
        CheckoutId(row["checkout_id"]),
        ClaimState(row["claim_state"]),
        int(row["claim_generation"]),
        None
        if row["foreground_frame_id"] is None
        else FrameId(row["foreground_frame_id"]),
        BackgroundState(row["background_state"]),
        None if row["acquired_at"] is None else int(row["acquired_at"]),
        None if row["released_at"] is None else int(row["released_at"]),
        int(row["updated_at"]),
    )


def _frame(row: sqlite3.Row) -> Frame:
    return Frame(
        FrameId(row["frame_id"]),
        HostId(row["host_id"]),
        ProjectId(row["project_id"]),
        FrameRole(row["role"]),
        None if row["parent_frame_id"] is None else FrameId(row["parent_frame_id"]),
        WorkContextId(row["work_context_id"]),
        str(row["title"]),
        None if row["purpose"] is None else str(row["purpose"]),
        None
        if row["preferred_provider"] is None
        else ProviderId(row["preferred_provider"]),
        FrameLifecycleState(row["lifecycle_state"]),
        None if row["close_reason"] is None else CloseReason(row["close_reason"]),
        None
        if row["current_session_key"] is None
        else SessionKey.parse(row["current_session_key"]),
        CreatedBy(row["created_by"]),
        int(row["created_at"]),
        int(row["updated_at"]),
    )


def _provider_session(row: sqlite3.Row) -> ProviderSession:
    return ProviderSession(
        SessionKey.parse(row["session_key"]),
        HostId(row["host_id"]),
        ProviderId(row["provider"]),
        SessionKey.parse(row["session_key"]).provider_session_id,
        None if row["project_id"] is None else ProjectId(row["project_id"]),
        None if row["checkout_id"] is None else CheckoutId(row["checkout_id"]),
        None if row["name"] is None else str(row["name"]),
        None if row["purpose"] is None else str(row["purpose"]),
        bool(row["pinned"]),
        RuntimePresence(row["runtime_presence"]),
        Resumability(row["resumability"]),
        Activity(row["activity"]),
        ActivityReason(row["activity_reason"]),
        None if row["created_at"] is None else int(row["created_at"]),
        None if row["provider_updated_at"] is None else int(row["provider_updated_at"]),
        int(row["last_observed_at"]),
        int(row["updated_at"]),
    )


def _view(row: sqlite3.Row) -> UserView:
    return UserView(
        ViewId(row["view_id"]),
        HostId(row["host_id"]),
        ViewMode(row["mode"]),
        None if row["active_frame_id"] is None else FrameId(row["active_frame_id"]),
        ViewState(row["state"]),
        int(row["revision"]),
        str(row["desktop_token"]),
        None if row["tmux_server_id"] is None else TmuxServerId(row["tmux_server_id"]),
        int(row["created_at"]),
        None if row["last_attached_at"] is None else int(row["last_attached_at"]),
        int(row["updated_at"]),
    )


def _placement(row: sqlite3.Row) -> FramePlacement:
    return FramePlacement(
        PlacementId(row["placement_id"]),
        HostId(row["host_id"]),
        ViewId(row["view_id"]),
        FrameId(row["frame_id"]),
        None if row["surface_id"] is None else SurfaceId(row["surface_id"]),
        PlacementState(row["state"]),
        int(row["generation"]),
        None if row["last_focused_at"] is None else int(row["last_focused_at"]),
        int(row["updated_at"]),
    )


def _launch(row: sqlite3.Row) -> LaunchIntent:
    return LaunchIntent(
        LaunchId(row["launch_id"]),
        RequestId(row["request_id"]),
        HostId(row["host_id"]),
        FrameId(row["frame_id"]),
        ProviderId(row["provider"]),
        LaunchAction(row["action"]),
        None
        if row["target_session_key"] is None
        else SessionKey.parse(row["target_session_key"]),
        LaunchState(row["state"]),
        _failure_from_row(row),
        int(row["created_at"]),
        int(row["updated_at"]),
    )


def _surface(row: sqlite3.Row) -> Surface:
    return Surface(
        SurfaceId(row["surface_id"]),
        HostId(row["host_id"]),
        ProviderId(row["provider"]),
        None if row["session_key"] is None else SessionKey.parse(row["session_key"]),
        LaunchId(row["launch_id"]),
        SurfaceState(row["lifecycle_state"]),
        None if row["tmux_server_id"] is None else TmuxServerId(row["tmux_server_id"]),
        None if row["pane_id"] is None else str(row["pane_id"]),
        None if row["process_id"] is None else int(row["process_id"]),
        None if row["process_birth_id"] is None else str(row["process_birth_id"]),
        int(row["metadata_generation"]),
        int(row["created_at"]),
        int(row["updated_at"]),
        None if row["retired_at"] is None else int(row["retired_at"]),
    )


def _transition(row: sqlite3.Row) -> ViewTransition:
    return ViewTransition(
        TransitionId(row["transition_id"]),
        RequestId(row["request_id"]),
        str(row["request_fingerprint"]),
        HostId(row["host_id"]),
        ViewId(row["view_id"]),
        TransitionKind(row["kind"]),
        None if row["source_frame_id"] is None else FrameId(row["source_frame_id"]),
        FrameId(row["target_frame_id"]),
        None
        if row["work_context_id"] is None
        else WorkContextId(row["work_context_id"]),
        int(row["expected_view_revision"]),
        None
        if row["expected_claim_generation"] is None
        else int(row["expected_claim_generation"]),
        TransitionState(row["state"]),
        None if row["execution_owner"] is None else str(row["execution_owner"]),
        None if row["lease_expires_at"] is None else int(row["lease_expires_at"]),
        TransportPhase(row["transport_phase"]),
        _failure_from_row(row),
        int(row["created_at"]),
        int(row["updated_at"]),
    )


def _brief(row: sqlite3.Row) -> TransitionBrief:
    return TransitionBrief(
        BriefId(row["brief_id"]),
        TransitionId(row["transition_id"]),
        FrameId(row["source_frame_id"]),
        SessionKey.parse(row["source_session_key"]),
        FrameId(row["target_frame_id"]),
        str(row["brief"]),
        str(row["content_hash"]),
        int(row["created_at"]),
        None if row["first_claimed_at"] is None else int(row["first_claimed_at"]),
    )


def _completion_handoff(row: sqlite3.Row) -> CompletionHandoff:
    return CompletionHandoff(
        HandoffId(row["handoff_id"]),
        TransitionId(row["transition_id"]),
        FrameId(row["source_frame_id"]),
        SessionKey.parse(row["source_session_key"]),
        FrameId(row["target_frame_id"]),
        str(row["summary"]),
        str(row["next_action"]),
        str(row["content_hash"]),
        int(row["created_at"]),
        None if row["first_claimed_at"] is None else int(row["first_claimed_at"]),
    )


def _control_turn(row: sqlite3.Row) -> ControlTurn:
    return ControlTurn(
        ControlTurnId(row["control_turn_id"]),
        TransitionId(row["transition_id"]),
        FrameId(row["target_frame_id"]),
        SessionKey.parse(row["target_session_key"]),
        ControlKind(row["kind"]),
        str(row["template_version"]),
        ControlTransport(row["transport"]),
        ControlState(row["state"]),
        int(row["submission_count"]),
        None if row["submitted_at"] is None else int(row["submitted_at"]),
        None if row["observed_prompt_id"] is None else str(row["observed_prompt_id"]),
        None if row["claimed_at"] is None else int(row["claimed_at"]),
        None if row["settled_at"] is None else int(row["settled_at"]),
        _failure_from_row(row),
    )


def _recovery(row: sqlite3.Row) -> Recovery:
    return Recovery(
        RecoveryId(row["recovery_id"]),
        HostId(row["host_id"]),
        str(row["kind"]),
        str(row["subject_type"]),
        str(row["subject_id"]),
        RecoveryActionability(row["actionability"]),
        RecoveryState(row["state"]),
        str(row["bounded_explanation"]),
        int(row["created_at"]),
        int(row["updated_at"]),
    )


def _lease(row: sqlite3.Row) -> DesktopAttachmentLease:
    return DesktopAttachmentLease(
        LeaseId(row["lease_id"]),
        ViewId(row["view_id"]),
        RequestId(row["request_id"]),
        LeaseState(row["state"]),
        int(row["expires_at"]),
    )


def _request(row: sqlite3.Row) -> RequestRecord:
    return RequestRecord(
        HostId(row["host_id"]),
        RequestId(row["request_id"]),
        str(row["operation"]),
        str(row["semantic_fingerprint"]),
        RequestState(row["state"]),
        None if row["result_type"] is None else str(row["result_type"]),
        None if row["result_id"] is None else str(row["result_id"]),
        int(row["created_at"]),
        None if row["completed_at"] is None else int(row["completed_at"]),
    )


def _host_state_cache(row: sqlite3.Row) -> HostStateCache:
    return HostStateCache(
        str(row["remote_name"]),
        HostId(row["host_id"]),
        str(row["state_json"]),
        str(row["content_hash"]),
        int(row["observed_at"]),
        int(row["received_at"]),
        int(row["last_attempt_at"]),
        Reachability(row["reachability"]),
        None
        if row["error_code"] is None
        else FailureRecord(
            str(row["error_code"]),
            str(row["error_message"]),
            bool(row["error_retryable"]),
        ),
    )


def _database_path(path: str | os.PathLike[str]) -> str:
    value = os.fspath(path)
    if value == ":memory:":
        return value
    if value.startswith("file:"):
        raise StorageError("SQLite URI database paths are not supported")
    return str(Path(value))


def _secure_database_file(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _secure_sidecars(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.chmod(0o600)
        except FileNotFoundError:
            continue


def _enable_wal_with_retry(
    connection: sqlite3.Connection, *, busy_timeout_ms: int
) -> None:
    deadline = time.monotonic() + busy_timeout_ms / 1_000
    while True:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).casefold() or time.monotonic() >= deadline:
                raise
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def connect_database(
    path: str | os.PathLike[str],
    *,
    generation_id: GenerationId,
    local_host_id: HostId,
    local_display_name: str,
    initial_activation_state: ActivationState = ActivationState.CUTOVER_STAGED,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    now: int | None = None,
) -> sqlite3.Connection:
    """Open an exact Phase 6 generation or initialize an empty database."""

    if not 1 <= busy_timeout_ms <= MAX_BUSY_TIMEOUT_MS:
        raise ValueError(f"busy_timeout_ms must be between 1 and {MAX_BUSY_TIMEOUT_MS}")
    if not isinstance(generation_id, GenerationId):
        generation_id = GenerationId(generation_id)
    if not isinstance(local_host_id, HostId):
        local_host_id = HostId(local_host_id)
    local_display_name = bounded_text(
        local_display_name,
        "local_display_name",
        maximum=256,
    )
    database = _database_path(path)
    file_database = database != ":memory:"
    if file_database:
        _secure_database_file(Path(database))
    connection = sqlite3.connect(
        database,
        timeout=busy_timeout_ms / 1_000,
        isolation_level=None,
        uri=False,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        _enable_wal_with_retry(connection, busy_timeout_ms=busy_timeout_ms)
        connection.execute("PRAGMA synchronous = NORMAL")
        migrate(
            connection,
            generation_id=generation_id,
            local_host_id=local_host_id,
            local_display_name=local_display_name,
            initial_activation_state=initial_activation_state,
            now=now,
        )
        if file_database:
            _secure_sidecars(Path(database))
        return connection
    except BaseException:
        connection.close()
        raise


class Registry:
    """One synchronous Phase 6 registry connection."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        generation_id: GenerationId,
        local_host_id: HostId,
        local_display_name: str,
        initial_activation_state: ActivationState = ActivationState.CUTOVER_STAGED,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        now: int | None = None,
    ) -> None:
        self._generation_id = GenerationId(generation_id)
        self._local_host_id = HostId(local_host_id)
        self._connection: sqlite3.Connection | None = connect_database(
            path,
            generation_id=self._generation_id,
            local_host_id=self._local_host_id,
            local_display_name=local_display_name,
            initial_activation_state=initial_activation_state,
            busy_timeout_ms=busy_timeout_ms,
            now=now,
        )

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RegistryClosed("registry is closed")
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Registry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connection
        if connection.in_transaction:
            raise StorageError("nested Registry transactions are not supported")
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def metadata(self) -> dict[str, object]:
        row = self.connection.execute(
            "SELECT * FROM registry_metadata WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise StorageError("registry metadata is missing")
        return dict(zip(row.keys(), row, strict=True))

    def _require_local_host(self, host_id: HostId) -> None:
        if host_id != self._local_host_id:
            raise ConflictError("host_mismatch", "operation targets a non-local host")

    def _one(self, table: str, column: str, value: object) -> sqlite3.Row:
        row = self.connection.execute(
            f"SELECT * FROM {table} WHERE {column} = ?", (str(value),)
        ).fetchone()
        if row is None:
            raise ConflictError("not_found", f"{table} record does not exist")
        return row

    def get_work_context(self, work_context_id: WorkContextId) -> WorkContext:
        return _work_context(
            self._one("work_contexts", "work_context_id", work_context_id)
        )

    def get_frame(self, frame_id: FrameId) -> Frame:
        return _frame(self._one("frames", "frame_id", frame_id))

    def get_provider_session(self, session_key: SessionKey) -> ProviderSession:
        return _provider_session(
            self._one("provider_sessions", "session_key", session_key)
        )

    def get_view(self, view_id: ViewId) -> UserView:
        return _view(self._one("user_views", "view_id", view_id))

    def get_placement(self, placement_id: PlacementId) -> FramePlacement:
        return _placement(self._one("frame_placements", "placement_id", placement_id))

    def get_launch(self, launch_id: LaunchId) -> LaunchIntent:
        return _launch(self._one("launch_intents", "launch_id", launch_id))

    def get_surface(self, surface_id: SurfaceId) -> Surface:
        return _surface(self._one("surfaces", "surface_id", surface_id))

    def get_transition(self, transition_id: TransitionId) -> ViewTransition:
        return _transition(
            self._one("view_transitions", "transition_id", transition_id)
        )

    def find_transition_by_request(
        self, host_id: HostId, request_id: RequestId
    ) -> ViewTransition | None:
        row = self.connection.execute(
            "SELECT * FROM view_transitions WHERE host_id = ? AND request_id = ?",
            (str(host_id), str(request_id)),
        ).fetchone()
        return None if row is None else _transition(row)

    def get_control_turn(self, control_turn_id: ControlTurnId) -> ControlTurn:
        return _control_turn(
            self._one("control_turns", "control_turn_id", control_turn_id)
        )

    def get_recovery(self, recovery_id: RecoveryId) -> Recovery:
        return _recovery(self._one("recoveries", "recovery_id", recovery_id))

    def get_lease(self, lease_id: LeaseId) -> DesktopAttachmentLease:
        return _lease(self._one("desktop_attachment_leases", "lease_id", lease_id))

    def get_tmux_server(self, tmux_server_id: TmuxServerId) -> TmuxServer:
        row = self._one("tmux_servers", "tmux_server_id", tmux_server_id)
        return TmuxServer(
            TmuxServerId(row["tmux_server_id"]),
            HostId(row["host_id"]),
            row["socket_path"],
            int(row["server_pid"]),
            int(row["server_start_time"]),
            int(row["observed_at"]),
        )

    def list_views(self, *, include_retired: bool = False) -> tuple[UserView, ...]:
        rows = self.connection.execute(
            "SELECT * FROM user_views "
            + ("" if include_retired else "WHERE state != 'retired' ")
            + "ORDER BY updated_at DESC, view_id"
        ).fetchall()
        return tuple(_view(row) for row in rows)

    def list_placements(
        self, *, view_id: ViewId | None = None
    ) -> tuple[FramePlacement, ...]:
        if view_id is None:
            rows = self.connection.execute(
                "SELECT * FROM frame_placements ORDER BY view_id, placement_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM frame_placements WHERE view_id = ? "
                "ORDER BY placement_id",
                (str(view_id),),
            ).fetchall()
        return tuple(_placement(row) for row in rows)

    def list_surfaces(self, *, live_only: bool = False) -> tuple[Surface, ...]:
        rows = self.connection.execute(
            "SELECT * FROM surfaces "
            + ("WHERE lifecycle_state = 'live' " if live_only else "")
            + "ORDER BY surface_id"
        ).fetchall()
        return tuple(_surface(row) for row in rows)

    def get_request(self, host_id: HostId, request_id: RequestId) -> RequestRecord:
        row = self.connection.execute(
            "SELECT * FROM request_records WHERE host_id = ? AND request_id = ?",
            (str(host_id), str(request_id)),
        ).fetchone()
        if row is None:
            raise ConflictError("not_found", "request record does not exist")
        return _request(row)

    def materialize_catalog(
        self,
        host_id: HostId,
        projects: Sequence[Project],
        repositories: Sequence[Repository],
        memberships: Sequence[ProjectRepository],
        checkouts: Sequence[Checkout],
        *,
        now: int | None = None,
    ) -> None:
        """Atomically materialize the complete local declared catalog."""

        self._require_local_host(host_id)
        timestamp = _timestamp(now)
        project_ids = {project.project_id for project in projects}
        repository_ids = {repository.repository_id for repository in repositories}
        if any(
            membership.project_id not in project_ids
            or membership.repository_id not in repository_ids
            for membership in memberships
        ):
            raise ConflictError(
                "catalog_membership", "membership is outside the declared catalog"
            )
        if any(
            checkout.host_id != host_id or checkout.repository_id not in repository_ids
            for checkout in checkouts
        ):
            raise ConflictError(
                "catalog_checkout", "checkout is outside the local declared catalog"
            )
        primary_counts: dict[ProjectId, int] = {}
        default_counts: dict[RepositoryId, int] = {}
        for membership in memberships:
            if membership.is_primary:
                primary_counts[membership.project_id] = (
                    primary_counts.get(membership.project_id, 0) + 1
                )
        for checkout in checkouts:
            if checkout.is_default:
                default_counts[checkout.repository_id] = (
                    default_counts.get(checkout.repository_id, 0) + 1
                )
        if any(count > 1 for count in primary_counts.values()):
            raise ConflictError("catalog_primary", "project has multiple primaries")
        if any(count > 1 for count in default_counts.values()):
            raise ConflictError("catalog_default", "repository has multiple defaults")

        try:
            with self.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE projects SET declared = 0, updated_at = ?", (timestamp,)
                )
                connection.execute(
                    "UPDATE repositories SET declared = 0, updated_at = ?", (timestamp,)
                )
                connection.execute(
                    "UPDATE checkouts SET declared = 0, is_default = 0, updated_at = ? "
                    "WHERE host_id = ?",
                    (timestamp, str(host_id)),
                )
                connection.execute("UPDATE project_repositories SET is_primary = 0")
                for project in projects:
                    connection.execute(
                        """
                        INSERT INTO projects(
                            project_id, name, aliases_json, default_provider,
                            default_transport, task_push, complete_return, declared,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        ON CONFLICT(project_id) DO UPDATE SET
                            name = excluded.name,
                            aliases_json = excluded.aliases_json,
                            default_provider = excluded.default_provider,
                            default_transport = excluded.default_transport,
                            task_push = excluded.task_push,
                            complete_return = excluded.complete_return,
                            declared = 1,
                            updated_at = excluded.updated_at
                        """,
                        (
                            str(project.project_id),
                            project.name,
                            json.dumps(project.aliases, separators=(",", ":")),
                            None
                            if project.default_provider is None
                            else project.default_provider.value,
                            project.default_transport.value,
                            None
                            if project.task_push is None
                            else project.task_push.value,
                            None
                            if project.complete_return is None
                            else project.complete_return.value,
                            timestamp,
                            timestamp,
                        ),
                    )
                for repository in repositories:
                    connection.execute(
                        """
                        INSERT INTO repositories(
                            repository_id, name, kind, context_sources_json,
                            declared, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 1, ?, ?)
                        ON CONFLICT(repository_id) DO UPDATE SET
                            name = excluded.name,
                            kind = excluded.kind,
                            context_sources_json = excluded.context_sources_json,
                            declared = 1,
                            updated_at = excluded.updated_at
                        """,
                        (
                            str(repository.repository_id),
                            repository.name,
                            repository.kind.value,
                            json.dumps(
                                repository.context_sources, separators=(",", ":")
                            ),
                            timestamp,
                            timestamp,
                        ),
                    )
                declared_projects = tuple(str(value) for value in project_ids)
                if declared_projects:
                    placeholders = ",".join("?" for _ in declared_projects)
                    connection.execute(
                        "DELETE FROM project_repositories "
                        f"WHERE project_id IN ({placeholders})",
                        declared_projects,
                    )
                for membership in memberships:
                    connection.execute(
                        "INSERT INTO project_repositories VALUES (?, ?, ?, ?, ?)",
                        (
                            str(membership.project_id),
                            str(membership.repository_id),
                            membership.is_primary,
                            timestamp,
                            timestamp,
                        ),
                    )
                for checkout in checkouts:
                    stable = connection.execute(
                        "SELECT repository_id, host_id FROM checkouts "
                        "WHERE checkout_id = ?",
                        (str(checkout.checkout_id),),
                    ).fetchone()
                    if stable is not None and (
                        stable["repository_id"] != str(checkout.repository_id)
                        or stable["host_id"] != str(checkout.host_id)
                    ):
                        raise ConflictError(
                            "checkout_identity",
                            "checkout cannot move between repository or host",
                        )
                    connection.execute(
                        """
                        INSERT INTO checkouts(
                            checkout_id, repository_id, host_id, path, kind,
                            display_name, provider_override, is_default, declared,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        ON CONFLICT(checkout_id) DO UPDATE SET
                            path = excluded.path,
                            kind = excluded.kind,
                            display_name = excluded.display_name,
                            provider_override = excluded.provider_override,
                            is_default = excluded.is_default,
                            declared = 1,
                            updated_at = excluded.updated_at
                        """,
                        (
                            str(checkout.checkout_id),
                            str(checkout.repository_id),
                            str(checkout.host_id),
                            str(checkout.path),
                            checkout.kind.value,
                            checkout.display_name,
                            None
                            if checkout.provider_override is None
                            else checkout.provider_override.value,
                            checkout.is_default,
                            timestamp,
                            timestamp,
                        ),
                    )
        except sqlite3.IntegrityError as error:
            raise ConflictError("catalog_conflict", str(error)) from error

    def upsert_provider_session(self, session: ProviderSession) -> ProviderSession:
        self._require_local_host(session.host_id)
        try:
            with self.transaction(immediate=True) as connection:
                existing = connection.execute(
                    "SELECT host_id, provider, provider_session_id "
                    "FROM provider_sessions WHERE session_key = ?",
                    (str(session.session_key),),
                ).fetchone()
                if existing is not None and tuple(existing) != (
                    str(session.host_id),
                    session.provider.value,
                    str(session.provider_session_id),
                ):
                    raise ConflictError(
                        "session_identity", "provider session identity changed"
                    )
                connection.execute(
                    """
                    INSERT INTO provider_sessions VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    ) ON CONFLICT(session_key) DO UPDATE SET
                        project_id = excluded.project_id,
                        checkout_id = excluded.checkout_id,
                        name = excluded.name,
                        purpose = excluded.purpose,
                        pinned = excluded.pinned,
                        runtime_presence = excluded.runtime_presence,
                        resumability = excluded.resumability,
                        activity = excluded.activity,
                        activity_reason = excluded.activity_reason,
                        created_at = COALESCE(
                            provider_sessions.created_at, excluded.created_at
                        ),
                        provider_updated_at = excluded.provider_updated_at,
                        last_observed_at = excluded.last_observed_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(session.session_key),
                        str(session.host_id),
                        session.provider.value,
                        str(session.provider_session_id),
                        None if session.project_id is None else str(session.project_id),
                        None
                        if session.checkout_id is None
                        else str(session.checkout_id),
                        session.name,
                        session.purpose,
                        session.pinned,
                        session.runtime_presence.value,
                        session.resumability.value,
                        session.activity.value,
                        session.activity_reason.value,
                        session.created_at,
                        session.provider_updated_at,
                        session.last_observed_at,
                        session.updated_at,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("session_conflict", str(error)) from error
        return self.get_provider_session(session.session_key)

    def append_session_handoff(self, handoff: SessionHandoff) -> SessionHandoff:
        self._require_local_host(handoff.source_host_id)
        values = (
            str(handoff.handoff_id),
            str(handoff.session_key),
            handoff.sequence,
            handoff.summary,
            handoff.next_action,
            handoff.source.value,
            str(handoff.source_host_id),
            handoff.content_hash,
            handoff.created_at,
        )
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM session_handoffs WHERE handoff_id = ? "
                "OR (session_key = ? AND sequence = ?)",
                (
                    str(handoff.handoff_id),
                    str(handoff.session_key),
                    handoff.sequence,
                ),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise ConflictError(
                        "handoff_conflict",
                        "handoff identity already has different content",
                    )
            else:
                connection.execute(
                    "INSERT INTO session_handoffs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
        return handoff

    def ensure_workspace(
        self,
        work_context_id: WorkContextId,
        frame_id: FrameId,
        host_id: HostId,
        project_id: ProjectId,
        checkout_id: CheckoutId,
        title: str,
        *,
        preferred_provider: ProviderId | None = None,
        created_by: CreatedBy = CreatedBy.USER,
        now: int | None = None,
    ) -> WorkspaceResult:
        self._require_local_host(host_id)
        title = bounded_text(title, "workspace.title")
        timestamp = _timestamp(now)
        try:
            with self.transaction(immediate=True) as connection:
                existing = connection.execute(
                    """
                    SELECT frame.*, context.checkout_id AS context_checkout_id
                    FROM frames AS frame
                    JOIN work_contexts AS context
                      ON context.work_context_id = frame.work_context_id
                    WHERE frame.host_id = ? AND frame.project_id = ?
                      AND frame.role = 'workspace'
                    """,
                    (str(host_id), str(project_id)),
                ).fetchone()
                if existing is not None:
                    if existing["context_checkout_id"] != str(checkout_id):
                        raise ConflictError(
                            "workspace_checkout",
                            "workspace already exists on a different checkout",
                        )
                    context_row = connection.execute(
                        "SELECT * FROM work_contexts WHERE work_context_id = ?",
                        (existing["work_context_id"],),
                    ).fetchone()
                    assert context_row is not None
                    return WorkspaceResult(
                        "existing", _work_context(context_row), _frame(existing)
                    )
                connection.execute(
                    """
                    INSERT INTO work_contexts VALUES (
                        ?, ?, ?, ?, 'released', 0, NULL, 'safe', NULL, ?, ?
                    )
                    """,
                    (
                        str(work_context_id),
                        str(host_id),
                        str(project_id),
                        str(checkout_id),
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO frames VALUES (
                        ?, ?, ?, 'workspace', NULL, ?, ?, NULL, ?, 'open',
                        NULL, NULL, ?, ?, ?
                    )
                    """,
                    (
                        str(frame_id),
                        str(host_id),
                        str(project_id),
                        str(work_context_id),
                        title,
                        None
                        if preferred_provider is None
                        else preferred_provider.value,
                        created_by.value,
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("workspace_conflict", str(error)) from error
        return WorkspaceResult(
            "created", self.get_work_context(work_context_id), self.get_frame(frame_id)
        )

    def append_frame_session(
        self, membership: FrameSession, *, make_current: bool = True
    ) -> FrameSession:
        values = (
            str(membership.frame_session_id),
            str(membership.frame_id),
            str(membership.session_key),
            membership.ordinal,
            membership.membership_reason.value,
            membership.joined_at,
        )
        try:
            with self.transaction(immediate=True) as connection:
                existing = connection.execute(
                    "SELECT * FROM frame_sessions WHERE frame_session_id = ? "
                    "OR session_key = ? OR (frame_id = ? AND ordinal = ?)",
                    (
                        str(membership.frame_session_id),
                        str(membership.session_key),
                        str(membership.frame_id),
                        membership.ordinal,
                    ),
                ).fetchone()
                if existing is not None and tuple(existing) != values:
                    raise ConflictError(
                        "frame_session_conflict", "frame session membership conflicts"
                    )
                if existing is None:
                    connection.execute(
                        "INSERT INTO frame_sessions VALUES (?, ?, ?, ?, ?, ?)",
                        values,
                    )
                if make_current:
                    connection.execute(
                        "UPDATE frames SET current_session_key = ?, updated_at = ? "
                        "WHERE frame_id = ?",
                        (
                            str(membership.session_key),
                            membership.joined_at,
                            str(membership.frame_id),
                        ),
                    )
        except sqlite3.IntegrityError as error:
            raise ConflictError("frame_session_conflict", str(error)) from error
        return membership

    def acquire_work_context(
        self,
        work_context_id: WorkContextId,
        expected_generation: int,
        foreground_frame_id: FrameId,
        *,
        now: int | None = None,
    ) -> WorkContext:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM work_contexts WHERE work_context_id = ?",
                (str(work_context_id),),
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "WorkContext does not exist")
            current = _work_context(row)
            if current.claim_generation != expected_generation:
                raise ConflictError(
                    "stale_generation", "WorkContext generation changed"
                )
            require_state_edge(
                current.claim_state, ClaimState.HELD, WORK_CONTEXT_EDGES, "claim state"
            )
            changed = connection.execute(
                """
                UPDATE work_contexts SET claim_state = 'held', claim_generation = ?,
                    foreground_frame_id = ?, acquired_at = ?, released_at = NULL,
                    updated_at = ?
                WHERE work_context_id = ? AND claim_generation = ?
                """,
                (
                    expected_generation + 1,
                    str(foreground_frame_id),
                    timestamp,
                    timestamp,
                    str(work_context_id),
                    expected_generation,
                ),
            ).rowcount
            if changed != 1:
                raise ConflictError(
                    "stale_generation", "WorkContext generation changed"
                )
        return self.get_work_context(work_context_id)

    def transfer_foreground(
        self,
        work_context_id: WorkContextId,
        expected_generation: int,
        foreground_frame_id: FrameId,
        *,
        now: int | None = None,
    ) -> WorkContext:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT claim_state, claim_generation FROM work_contexts "
                "WHERE work_context_id = ?",
                (str(work_context_id),),
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "WorkContext does not exist")
            if row["claim_state"] != ClaimState.HELD.value:
                raise ConflictError("claim_state", "WorkContext is not held")
            if row["claim_generation"] != expected_generation:
                raise ConflictError(
                    "stale_generation", "WorkContext generation changed"
                )
            connection.execute(
                "UPDATE work_contexts SET foreground_frame_id = ?, "
                "claim_generation = ?, updated_at = ? WHERE work_context_id = ?",
                (
                    str(foreground_frame_id),
                    expected_generation + 1,
                    timestamp,
                    str(work_context_id),
                ),
            )
        return self.get_work_context(work_context_id)

    def _release_blocker(
        self, connection: sqlite3.Connection, context_id: WorkContextId
    ) -> str | None:
        live = connection.execute(
            """
            SELECT 1 FROM surfaces AS surface
            JOIN launch_intents AS launch ON launch.launch_id = surface.launch_id
            JOIN frames AS frame ON frame.frame_id = launch.frame_id
            WHERE frame.work_context_id = ? AND surface.lifecycle_state = 'live'
            LIMIT 1
            """,
            (str(context_id),),
        ).fetchone()
        if live is not None:
            return "live_surface"
        transition = connection.execute(
            """
            SELECT 1 FROM view_transitions AS transition_record
            JOIN frames AS target
              ON target.frame_id = transition_record.target_frame_id
            LEFT JOIN frames AS source
              ON source.frame_id = transition_record.source_frame_id
            WHERE (target.work_context_id = ? OR source.work_context_id = ?)
              AND transition_record.state IN (
                  'prepared', 'executing', 'awaiting_claim'
              )
            LIMIT 1
            """,
            (str(context_id), str(context_id)),
        ).fetchone()
        return "active_transition" if transition is not None else None

    def release_work_context(
        self,
        work_context_id: WorkContextId,
        expected_generation: int,
        *,
        human_override: bool = False,
        now: int | None = None,
    ) -> WorkContext:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM work_contexts WHERE work_context_id = ?",
                (str(work_context_id),),
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "WorkContext does not exist")
            current = _work_context(row)
            if current.claim_generation != expected_generation:
                raise ConflictError(
                    "stale_generation", "WorkContext generation changed"
                )
            require_state_edge(
                current.claim_state,
                ClaimState.RELEASED,
                WORK_CONTEXT_EDGES,
                "claim state",
            )
            if (
                not human_override
                and current.background_state is not BackgroundState.SAFE
            ):
                raise ConflictError(
                    "background_unsafe", "background safety is not confirmed"
                )
            blocker = self._release_blocker(connection, work_context_id)
            if blocker is not None:
                raise ConflictError(
                    blocker, "WorkContext still owns mutation-capable runtime"
                )
            connection.execute(
                """
                UPDATE work_contexts SET claim_state = 'released',
                    claim_generation = ?, foreground_frame_id = NULL,
                    released_at = ?, updated_at = ?
                WHERE work_context_id = ?
                """,
                (
                    expected_generation + 1,
                    timestamp,
                    timestamp,
                    str(work_context_id),
                ),
            )
        return self.get_work_context(work_context_id)

    def block_work_context(
        self,
        work_context_id: WorkContextId,
        expected_generation: int,
        background_state: BackgroundState,
        *,
        now: int | None = None,
    ) -> WorkContext:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM work_contexts WHERE work_context_id = ?",
                (str(work_context_id),),
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "WorkContext does not exist")
            current = _work_context(row)
            if current.claim_generation != expected_generation:
                raise ConflictError(
                    "stale_generation", "WorkContext generation changed"
                )
            require_state_edge(
                current.claim_state,
                ClaimState.BLOCKED,
                WORK_CONTEXT_EDGES,
                "claim state",
            )
            connection.execute(
                """
                UPDATE work_contexts SET claim_state = 'blocked',
                    claim_generation = ?, foreground_frame_id = NULL,
                    background_state = ?, updated_at = ?
                WHERE work_context_id = ?
                """,
                (
                    expected_generation + 1,
                    background_state.value,
                    timestamp,
                    str(work_context_id),
                ),
            )
        return self.get_work_context(work_context_id)

    def create_task(
        self, frame: Frame, placement: FramePlacement
    ) -> tuple[Frame, FramePlacement]:
        self._require_local_host(frame.host_id)
        if (
            frame.role is not FrameRole.TASK
            or frame.lifecycle_state is not FrameLifecycleState.OPEN
            or placement.host_id != frame.host_id
            or placement.frame_id != frame.frame_id
            or placement.state is not PlacementState.STAGED
        ):
            raise ConflictError("task_identity", "task and staged placement disagree")
        try:
            with self.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO frames VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        str(frame.frame_id),
                        str(frame.host_id),
                        str(frame.project_id),
                        frame.role.value,
                        None
                        if frame.parent_frame_id is None
                        else str(frame.parent_frame_id),
                        str(frame.work_context_id),
                        frame.title,
                        frame.purpose,
                        None
                        if frame.preferred_provider is None
                        else frame.preferred_provider.value,
                        frame.lifecycle_state.value,
                        None,
                        None,
                        frame.created_by.value,
                        frame.created_at,
                        frame.updated_at,
                    ),
                )
                self._insert_placement(connection, placement)
        except sqlite3.IntegrityError as error:
            raise ConflictError("task_conflict", str(error)) from error
        return self.get_frame(frame.frame_id), self.get_placement(
            placement.placement_id
        )

    def advance_frame_state(
        self,
        frame_id: FrameId,
        expected_state: FrameLifecycleState,
        target: FrameLifecycleState,
        *,
        close_reason: CloseReason | None = None,
        now: int | None = None,
    ) -> Frame:
        timestamp = _timestamp(now)
        require_state_edge(expected_state, target, FRAME_EDGES, "frame state")
        if (target is FrameLifecycleState.CLOSED) != (close_reason is not None):
            raise ValueError(
                "closed frame requires and only closed frame accepts reason"
            )
        with self.transaction(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE frames SET lifecycle_state = ?, close_reason = ?, "
                "updated_at = ? WHERE frame_id = ? AND lifecycle_state = ?",
                (
                    target.value,
                    None if close_reason is None else close_reason.value,
                    timestamp,
                    str(frame_id),
                    expected_state.value,
                ),
            ).rowcount
            if changed != 1:
                raise ConflictError("stale_state", "frame state changed")
        return self.get_frame(frame_id)

    def record_tmux_server(self, server: TmuxServer) -> TmuxServer:
        self._require_local_host(server.host_id)
        try:
            with self.transaction(immediate=True) as connection:
                existing = connection.execute(
                    "SELECT host_id, socket_path, server_pid, server_start_time "
                    "FROM tmux_servers WHERE tmux_server_id = ?",
                    (str(server.tmux_server_id),),
                ).fetchone()
                identity = (
                    str(server.host_id),
                    server.socket_path,
                    server.server_pid,
                    server.server_start_time,
                )
                if existing is not None and tuple(existing) != identity:
                    raise ConflictError("tmux_identity", "tmux server identity changed")
                connection.execute(
                    """
                    INSERT INTO tmux_servers VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tmux_server_id) DO UPDATE SET
                        observed_at = excluded.observed_at
                    """,
                    (str(server.tmux_server_id), *identity, server.observed_at),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("tmux_conflict", str(error)) from error
        return server

    def rebind_view_tmux_server(
        self,
        view_id: ViewId,
        expected_revision: int,
        tmux_server_id: TmuxServerId,
        target_state: ViewState,
        *,
        now: int | None = None,
    ) -> UserView:
        """Fence a repaired view to newly observed exact tmux generation evidence."""

        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM user_views WHERE view_id = ?", (str(view_id),)
            ).fetchone()
            server = connection.execute(
                "SELECT host_id FROM tmux_servers WHERE tmux_server_id = ?",
                (str(tmux_server_id),),
            ).fetchone()
            if row is None or server is None:
                raise ConflictError("not_found", "view or tmux server does not exist")
            current = _view(row)
            if current.revision != expected_revision:
                raise ConflictError("stale_revision", "view revision changed")
            if server["host_id"] != str(current.host_id):
                raise ConflictError("tmux_identity", "tmux server host differs")
            if current.state is not target_state:
                require_state_edge(
                    current.state, target_state, VIEW_EDGES, "view state"
                )
            connection.execute(
                "UPDATE user_views SET tmux_server_id = ?, state = ?, "
                "revision = revision + 1, updated_at = ? WHERE view_id = ?",
                (
                    str(tmux_server_id),
                    target_state.value,
                    timestamp,
                    str(view_id),
                ),
            )
        return self.get_view(view_id)

    def mark_view_attached(
        self,
        view_id: ViewId,
        expected_revision: int,
        *,
        now: int | None = None,
    ) -> UserView:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE user_views SET last_attached_at = ?, "
                "revision = revision + 1, updated_at = ? "
                "WHERE view_id = ? AND revision = ? AND state != 'retired'",
                (timestamp, timestamp, str(view_id), expected_revision),
            ).rowcount
            if changed != 1:
                raise ConflictError("stale_revision", "view revision or state changed")
        return self.get_view(view_id)

    def invalidate_view_server_surfaces(
        self,
        view_id: ViewId,
        tmux_server_id: TmuxServerId,
        *,
        now: int | None = None,
    ) -> int:
        """Revoke locators and capabilities after exact server-generation loss."""

        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            view = connection.execute(
                "SELECT host_id FROM user_views WHERE view_id = ?",
                (str(view_id),),
            ).fetchone()
            server = connection.execute(
                "SELECT host_id FROM tmux_servers WHERE tmux_server_id = ?",
                (str(tmux_server_id),),
            ).fetchone()
            if view is None or server is None:
                raise ConflictError("not_found", "view or tmux server does not exist")
            if view["host_id"] != server["host_id"]:
                raise ConflictError("tmux_identity", "tmux server host differs")
            rows = connection.execute(
                "SELECT surface.surface_id FROM surfaces AS surface "
                "JOIN frame_placements AS placement "
                "ON placement.surface_id = surface.surface_id "
                "WHERE placement.view_id = ? AND surface.tmux_server_id = ?",
                (str(view_id), str(tmux_server_id)),
            ).fetchall()
            surface_ids = tuple(row["surface_id"] for row in rows)
            if not surface_ids:
                return 0
            placeholders = ",".join("?" for _ in surface_ids)
            connection.execute(
                "UPDATE agent_capabilities SET revoked_at = ? "
                f"WHERE surface_id IN ({placeholders}) AND revoked_at IS NULL",
                (timestamp, *surface_ids),
            )
            connection.execute(
                "UPDATE frame_placements SET surface_id = NULL, "
                "generation = generation + 1, updated_at = ? "
                f"WHERE surface_id IN ({placeholders})",
                (timestamp, *surface_ids),
            )
            connection.execute(
                "UPDATE surfaces SET lifecycle_state = CASE "
                "WHEN lifecycle_state = 'live' THEN 'orphaned' "
                "ELSE lifecycle_state END, tmux_server_id = NULL, pane_id = NULL, "
                "process_id = NULL, process_birth_id = NULL, "
                "metadata_generation = metadata_generation + 1, updated_at = ? "
                f"WHERE surface_id IN ({placeholders})",
                (timestamp, *surface_ids),
            )
        return len(surface_ids)

    def create_view(
        self, view: UserView, placement: FramePlacement
    ) -> tuple[UserView, FramePlacement]:
        self._require_local_host(view.host_id)
        if (
            placement.host_id != view.host_id
            or placement.view_id != view.view_id
            or placement.frame_id != view.active_frame_id
            or placement.state is not PlacementState.ACTIVE
        ):
            raise ConflictError(
                "view_placement", "initial view and active placement disagree"
            )
        try:
            with self.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO user_views VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(view.view_id),
                        str(view.host_id),
                        view.mode.value,
                        None
                        if view.active_frame_id is None
                        else str(view.active_frame_id),
                        view.state.value,
                        view.revision,
                        view.desktop_token,
                        None
                        if view.tmux_server_id is None
                        else str(view.tmux_server_id),
                        view.created_at,
                        view.last_attached_at,
                        view.updated_at,
                    ),
                )
                self._insert_placement(connection, placement)
        except sqlite3.IntegrityError as error:
            raise ConflictError("view_conflict", str(error)) from error
        return self.get_view(view.view_id), self.get_placement(placement.placement_id)

    @staticmethod
    def _insert_placement(
        connection: sqlite3.Connection, placement: FramePlacement
    ) -> None:
        connection.execute(
            "INSERT INTO frame_placements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(placement.placement_id),
                str(placement.host_id),
                str(placement.view_id),
                str(placement.frame_id),
                None if placement.surface_id is None else str(placement.surface_id),
                placement.state.value,
                placement.generation,
                placement.last_focused_at,
                placement.updated_at,
            ),
        )

    def add_placement(self, placement: FramePlacement) -> FramePlacement:
        self._require_local_host(placement.host_id)
        if placement.state not in {
            PlacementState.STAGED,
            PlacementState.STOPPED_AFFINITY,
        }:
            raise ConflictError(
                "placement_state", "new placement must be staged or stopped affinity"
            )
        try:
            with self.transaction(immediate=True) as connection:
                self._insert_placement(connection, placement)
        except sqlite3.IntegrityError as error:
            raise ConflictError("placement_conflict", str(error)) from error
        return self.get_placement(placement.placement_id)

    def set_view_mode(
        self,
        view_id: ViewId,
        expected_revision: int,
        mode: ViewMode,
        *,
        now: int | None = None,
    ) -> UserView:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE user_views SET mode = ?, revision = revision + 1, "
                "updated_at = ? WHERE view_id = ? AND revision = ? "
                "AND state != 'retired'",
                (mode.value, timestamp, str(view_id), expected_revision),
            ).rowcount
            if changed != 1:
                raise ConflictError("stale_revision", "view revision or state changed")
        return self.get_view(view_id)

    def advance_view_state(
        self,
        view_id: ViewId,
        expected_revision: int,
        target: ViewState,
        *,
        active_frame_id: FrameId | None,
        now: int | None = None,
    ) -> UserView:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM user_views WHERE view_id = ?", (str(view_id),)
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "view does not exist")
            current = _view(row)
            if current.revision != expected_revision:
                raise ConflictError("stale_revision", "view revision changed")
            require_state_edge(current.state, target, VIEW_EDGES, "view state")
            connection.execute(
                "UPDATE user_views SET state = ?, active_frame_id = ?, "
                "revision = ?, updated_at = ? WHERE view_id = ?",
                (
                    target.value,
                    None if active_frame_id is None else str(active_frame_id),
                    expected_revision + 1,
                    timestamp,
                    str(view_id),
                ),
            )
        return self.get_view(view_id)

    def advance_placement(
        self,
        placement_id: PlacementId,
        expected_generation: int,
        target: PlacementState,
        *,
        surface_id: SurfaceId | None = None,
        now: int | None = None,
    ) -> FramePlacement:
        timestamp = _timestamp(now)
        try:
            with self.transaction(immediate=True) as connection:
                row = connection.execute(
                    "SELECT * FROM frame_placements WHERE placement_id = ?",
                    (str(placement_id),),
                ).fetchone()
                if row is None:
                    raise ConflictError("not_found", "placement does not exist")
                current = _placement(row)
                if current.generation != expected_generation:
                    raise ConflictError(
                        "stale_generation", "placement generation changed"
                    )
                require_state_edge(
                    current.state, target, PLACEMENT_EDGES, "placement state"
                )
                effective_surface = (
                    current.surface_id if surface_id is None else surface_id
                )
                connection.execute(
                    "UPDATE frame_placements SET state = ?, surface_id = ?, "
                    "generation = ?, last_focused_at = CASE WHEN ? = 'active' "
                    "THEN ? ELSE last_focused_at END, updated_at = ? "
                    "WHERE placement_id = ?",
                    (
                        target.value,
                        None if effective_surface is None else str(effective_surface),
                        expected_generation + 1,
                        target.value,
                        timestamp,
                        timestamp,
                        str(placement_id),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("placement_conflict", str(error)) from error
        return self.get_placement(placement_id)

    def attach_surface_to_placement(
        self,
        placement_id: PlacementId,
        expected_generation: int,
        surface_id: SurfaceId,
        *,
        now: int | None = None,
    ) -> FramePlacement:
        """Bind a launched surface without changing staged/parked ownership state."""

        timestamp = _timestamp(now)
        try:
            with self.transaction(immediate=True) as connection:
                placement = connection.execute(
                    "SELECT * FROM frame_placements WHERE placement_id = ?",
                    (str(placement_id),),
                ).fetchone()
                surface = connection.execute(
                    """
                    SELECT surface.host_id, launch.frame_id
                    FROM surfaces AS surface
                    JOIN launch_intents AS launch
                      ON launch.launch_id = surface.launch_id
                    WHERE surface.surface_id = ?
                    """,
                    (str(surface_id),),
                ).fetchone()
                if placement is None or surface is None:
                    raise ConflictError(
                        "not_found", "placement or surface does not exist"
                    )
                record = _placement(placement)
                if record.generation != expected_generation:
                    raise ConflictError(
                        "stale_generation", "placement generation changed"
                    )
                if record.surface_id is not None:
                    raise ConflictError(
                        "surface_bound", "placement already owns a surface"
                    )
                if surface["host_id"] != str(record.host_id) or surface[
                    "frame_id"
                ] != str(record.frame_id):
                    raise ConflictError(
                        "surface_identity", "surface launch does not match placement"
                    )
                connection.execute(
                    "UPDATE frame_placements SET surface_id = ?, generation = ?, "
                    "updated_at = ? WHERE placement_id = ?",
                    (
                        str(surface_id),
                        expected_generation + 1,
                        timestamp,
                        str(placement_id),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("placement_conflict", str(error)) from error
        return self.get_placement(placement_id)

    def activate_placement(
        self,
        view_id: ViewId,
        expected_revision: int,
        target_placement_id: PlacementId,
        expected_target_generation: int,
        *,
        source_placement_id: PlacementId | None = None,
        expected_source_generation: int | None = None,
        now: int | None = None,
    ) -> tuple[UserView, FramePlacement]:
        timestamp = _timestamp(now)
        try:
            with self.transaction(immediate=True) as connection:
                view_row = connection.execute(
                    "SELECT * FROM user_views WHERE view_id = ?", (str(view_id),)
                ).fetchone()
                target_row = connection.execute(
                    "SELECT * FROM frame_placements WHERE placement_id = ?",
                    (str(target_placement_id),),
                ).fetchone()
                if view_row is None or target_row is None:
                    raise ConflictError("not_found", "view or placement does not exist")
                view = _view(view_row)
                target = _placement(target_row)
                if view.revision != expected_revision:
                    raise ConflictError("stale_revision", "view revision changed")
                if (
                    target.view_id != view_id
                    or target.generation != expected_target_generation
                ):
                    raise ConflictError(
                        "stale_generation", "target placement identity changed"
                    )
                require_state_edge(
                    target.state,
                    PlacementState.ACTIVE,
                    PLACEMENT_EDGES,
                    "placement state",
                )
                if source_placement_id is not None:
                    if expected_source_generation is None:
                        raise ValueError("source generation is required")
                    source_row = connection.execute(
                        "SELECT * FROM frame_placements WHERE placement_id = ?",
                        (str(source_placement_id),),
                    ).fetchone()
                    if source_row is None:
                        raise ConflictError(
                            "not_found", "source placement does not exist"
                        )
                    source = _placement(source_row)
                    if (
                        source.view_id != view_id
                        or source.generation != expected_source_generation
                    ):
                        raise ConflictError(
                            "stale_generation", "source placement identity changed"
                        )
                    require_state_edge(
                        source.state,
                        PlacementState.PARKED,
                        PLACEMENT_EDGES,
                        "placement state",
                    )
                    connection.execute(
                        "UPDATE frame_placements SET state = 'parked', "
                        "generation = generation + 1, updated_at = ? "
                        "WHERE placement_id = ?",
                        (timestamp, str(source_placement_id)),
                    )
                connection.execute(
                    "UPDATE frame_placements SET state = 'active', "
                    "generation = generation + 1, last_focused_at = ?, updated_at = ? "
                    "WHERE placement_id = ?",
                    (timestamp, timestamp, str(target_placement_id)),
                )
                connection.execute(
                    "UPDATE user_views SET active_frame_id = ?, "
                    "revision = revision + 1, updated_at = ? WHERE view_id = ?",
                    (str(target.frame_id), timestamp, str(view_id)),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("placement_conflict", str(error)) from error
        return self.get_view(view_id), self.get_placement(target_placement_id)

    def retire_view(
        self,
        view_id: ViewId,
        expected_revision: int,
        *,
        now: int | None = None,
    ) -> UserView:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM user_views WHERE view_id = ?", (str(view_id),)
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "view does not exist")
            view = _view(row)
            if view.revision != expected_revision:
                raise ConflictError("stale_revision", "view revision changed")
            require_state_edge(view.state, ViewState.RETIRED, VIEW_EDGES, "view state")
            if (
                connection.execute(
                    "SELECT 1 FROM view_transitions WHERE view_id = ? "
                    "AND state IN ("
                    "'prepared', 'executing', 'presented', 'awaiting_claim', 'settling'"
                    ")",
                    (str(view_id),),
                ).fetchone()
                is not None
            ):
                raise ConflictError(
                    "active_transition", "view has an active transition"
                )
            if (
                connection.execute(
                    "SELECT 1 FROM desktop_attachment_leases WHERE view_id = ? "
                    "AND state = 'offered'",
                    (str(view_id),),
                ).fetchone()
                is not None
            ):
                raise ConflictError(
                    "offered_lease", "view has an offered desktop lease"
                )
            if (
                connection.execute(
                    """
                SELECT 1 FROM frame_placements AS placement
                JOIN surfaces AS surface ON surface.surface_id = placement.surface_id
                WHERE placement.view_id = ? AND surface.lifecycle_state = 'live'
                """,
                    (str(view_id),),
                ).fetchone()
                is not None
            ):
                raise ConflictError("live_surface", "view still owns a live surface")
            connection.execute(
                "UPDATE user_views SET state = 'retired', active_frame_id = NULL, "
                "revision = revision + 1, updated_at = ? WHERE view_id = ?",
                (timestamp, str(view_id)),
            )
            connection.execute(
                "UPDATE frame_placements SET state = 'orphaned', "
                "generation = generation + 1, updated_at = ? "
                "WHERE view_id = ? AND state != 'orphaned'",
                (timestamp, str(view_id)),
            )
        return self.get_view(view_id)

    def plan_launch(
        self, launch: LaunchIntent, surface: Surface
    ) -> tuple[LaunchIntent, Surface]:
        self._require_local_host(launch.host_id)
        if (
            launch.state is not LaunchState.PLANNED
            or surface.lifecycle_state is not SurfaceState.PLANNED
            or surface.launch_id != launch.launch_id
            or surface.host_id != launch.host_id
            or surface.provider != launch.provider
            or surface.session_key is not None
            or surface.tmux_server_id is not None
        ):
            raise ConflictError(
                "launch_surface", "planned launch and surface identities disagree"
            )
        try:
            with self.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO launch_intents VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        str(launch.launch_id),
                        str(launch.request_id),
                        str(launch.host_id),
                        str(launch.frame_id),
                        launch.provider.value,
                        launch.action.value,
                        None
                        if launch.target_session_key is None
                        else str(launch.target_session_key),
                        launch.state.value,
                        None,
                        None,
                        None,
                        launch.created_at,
                        launch.updated_at,
                    ),
                )
                connection.execute(
                    "INSERT INTO surfaces VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(surface.surface_id),
                        str(surface.host_id),
                        surface.provider.value,
                        None,
                        str(surface.launch_id),
                        surface.lifecycle_state.value,
                        None,
                        None,
                        surface.process_id,
                        surface.process_birth_id,
                        surface.metadata_generation,
                        surface.created_at,
                        surface.updated_at,
                        surface.retired_at,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("launch_conflict", str(error)) from error
        return self.get_launch(launch.launch_id), self.get_surface(surface.surface_id)

    def advance_launch(
        self,
        launch_id: LaunchId,
        expected_state: LaunchState,
        target: LaunchState,
        *,
        failure: FailureRecord | None = None,
        now: int | None = None,
    ) -> LaunchIntent:
        timestamp = _timestamp(now)
        require_state_edge(expected_state, target, LAUNCH_EDGES, "launch state")
        if (target is LaunchState.FAILED) != (failure is not None):
            raise ValueError(
                "failed launch requires and only failed launch accepts failure"
            )
        with self.transaction(immediate=True) as connection:
            changed = connection.execute(
                """
                UPDATE launch_intents SET state = ?, failure_code = ?,
                    failure_message = ?, failure_retryable = ?, updated_at = ?
                WHERE launch_id = ? AND state = ?
                """,
                (
                    target.value,
                    None if failure is None else failure.code,
                    None if failure is None else failure.message,
                    None if failure is None else failure.retryable,
                    timestamp,
                    str(launch_id),
                    expected_state.value,
                ),
            ).rowcount
            if changed != 1:
                raise ConflictError("stale_state", "launch state changed")
        return self.get_launch(launch_id)

    def publish_surface(
        self,
        surface_id: SurfaceId,
        expected_metadata_generation: int,
        tmux_server_id: TmuxServerId,
        pane_id: str,
        *,
        process_id: int | None = None,
        process_birth_id: str | None = None,
        now: int | None = None,
    ) -> Surface:
        timestamp = _timestamp(now)
        pane_id = bounded_text(pane_id, "pane_id", maximum=64)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM surfaces WHERE surface_id = ?", (str(surface_id),)
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "surface does not exist")
            surface = _surface(row)
            if surface.metadata_generation != expected_metadata_generation:
                raise ConflictError(
                    "stale_generation", "surface metadata generation changed"
                )
            require_state_edge(
                surface.lifecycle_state,
                SurfaceState.LIVE,
                SURFACE_EDGES,
                "surface state",
            )
            server = connection.execute(
                "SELECT host_id FROM tmux_servers WHERE tmux_server_id = ?",
                (str(tmux_server_id),),
            ).fetchone()
            if server is None or server["host_id"] != str(surface.host_id):
                raise ConflictError(
                    "tmux_identity", "tmux server does not match surface"
                )
            connection.execute(
                """
                UPDATE surfaces SET lifecycle_state = 'live', tmux_server_id = ?,
                    pane_id = ?, process_id = ?, process_birth_id = ?,
                    metadata_generation = ?, updated_at = ?
                WHERE surface_id = ?
                """,
                (
                    str(tmux_server_id),
                    pane_id,
                    process_id,
                    process_birth_id,
                    expected_metadata_generation + 1,
                    timestamp,
                    str(surface_id),
                ),
            )
        return self.get_surface(surface_id)

    def advance_surface_state(
        self,
        surface_id: SurfaceId,
        expected_metadata_generation: int,
        target: SurfaceState,
        *,
        now: int | None = None,
    ) -> Surface:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM surfaces WHERE surface_id = ?", (str(surface_id),)
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "surface does not exist")
            surface = _surface(row)
            if surface.metadata_generation != expected_metadata_generation:
                raise ConflictError(
                    "stale_generation", "surface metadata generation changed"
                )
            require_state_edge(
                surface.lifecycle_state, target, SURFACE_EDGES, "surface state"
            )
            connection.execute(
                "UPDATE surfaces SET lifecycle_state = ?, metadata_generation = ?, "
                "updated_at = ?, retired_at = ? WHERE surface_id = ?",
                (
                    target.value,
                    expected_metadata_generation + 1,
                    timestamp,
                    timestamp if target is SurfaceState.RETIRED else None,
                    str(surface_id),
                ),
            )
        return self.get_surface(surface_id)

    def bind_surface_session(
        self,
        surface_id: SurfaceId,
        expected_metadata_generation: int,
        session_key: SessionKey,
        *,
        now: int | None = None,
    ) -> tuple[Surface, LaunchIntent]:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            surface_row = connection.execute(
                "SELECT * FROM surfaces WHERE surface_id = ?", (str(surface_id),)
            ).fetchone()
            if surface_row is None:
                raise ConflictError("not_found", "surface does not exist")
            surface = _surface(surface_row)
            launch_row = connection.execute(
                "SELECT * FROM launch_intents WHERE launch_id = ?",
                (str(surface.launch_id),),
            ).fetchone()
            session_row = connection.execute(
                "SELECT * FROM provider_sessions WHERE session_key = ?",
                (str(session_key),),
            ).fetchone()
            assert launch_row is not None
            if session_row is None:
                raise ConflictError("not_found", "provider session does not exist")
            launch = _launch(launch_row)
            session = _provider_session(session_row)
            if surface.metadata_generation != expected_metadata_generation:
                raise ConflictError(
                    "stale_generation", "surface metadata generation changed"
                )
            if surface.lifecycle_state is not SurfaceState.LIVE:
                raise ConflictError("surface_state", "surface is not live")
            if launch.state is not LaunchState.STARTED:
                raise ConflictError("launch_state", "launch is not started")
            if (
                session.host_id != surface.host_id
                or session.provider != surface.provider
                or (
                    launch.target_session_key is not None
                    and launch.target_session_key != session_key
                )
            ):
                raise ConflictError(
                    "session_identity", "session does not match launch and surface"
                )
            connection.execute(
                "UPDATE surfaces SET session_key = ?, metadata_generation = ?, "
                "updated_at = ? WHERE surface_id = ?",
                (
                    str(session_key),
                    expected_metadata_generation + 1,
                    timestamp,
                    str(surface_id),
                ),
            )
            connection.execute(
                "UPDATE launch_intents SET state = 'bound', updated_at = ? "
                "WHERE launch_id = ?",
                (timestamp, str(launch.launch_id)),
            )
        return self.get_surface(surface_id), self.get_launch(surface.launch_id)

    def issue_capability(self, capability: AgentCapability) -> AgentCapability:
        self._require_local_host(capability.host_id)
        try:
            with self.transaction(immediate=True) as connection:
                match = connection.execute(
                    """
                    SELECT 1 FROM frame_placements AS placement
                    JOIN user_views AS view ON view.view_id = placement.view_id
                    JOIN frames AS frame ON frame.frame_id = placement.frame_id
                    JOIN surfaces AS surface
                      ON surface.surface_id = placement.surface_id
                    JOIN launch_intents AS launch
                      ON launch.launch_id = surface.launch_id
                    WHERE placement.view_id = ? AND placement.frame_id = ?
                      AND placement.surface_id = ? AND placement.state = 'active'
                      AND placement.generation = ? AND view.host_id = ?
                      AND view.state = 'ready' AND view.active_frame_id = ?
                      AND frame.current_session_key IS ?
                      AND surface.launch_id = ? AND launch.state = 'bound'
                      AND surface.lifecycle_state = 'live'
                      AND surface.session_key IS ?
                      AND surface.tmux_server_id IS ? AND surface.pane_id IS ?
                    """,
                    (
                        str(capability.view_id),
                        str(capability.frame_id),
                        str(capability.surface_id),
                        capability.placement_generation,
                        str(capability.host_id),
                        str(capability.frame_id),
                        None
                        if capability.session_key is None
                        else str(capability.session_key),
                        str(capability.launch_id),
                        None
                        if capability.session_key is None
                        else str(capability.session_key),
                        None
                        if capability.tmux_server_id is None
                        else str(capability.tmux_server_id),
                        capability.pane_id,
                    ),
                ).fetchone()
                if match is None:
                    raise ConflictError(
                        "capability_identity",
                        "capability evidence does not match active runtime ownership",
                    )
                connection.execute(
                    "INSERT INTO agent_capabilities VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(capability.capability_id),
                        capability.capability_digest,
                        str(capability.host_id),
                        str(capability.view_id),
                        str(capability.frame_id),
                        None
                        if capability.session_key is None
                        else str(capability.session_key),
                        str(capability.surface_id),
                        str(capability.launch_id),
                        None
                        if capability.tmux_server_id is None
                        else str(capability.tmux_server_id),
                        capability.pane_id,
                        capability.placement_generation,
                        capability.issued_at,
                        capability.expires_at,
                        capability.revoked_at,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("capability_conflict", str(error)) from error
        return capability

    def revoke_capability(
        self, capability_id: CapabilityId, *, now: int | None = None
    ) -> None:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE agent_capabilities SET revoked_at = COALESCE(revoked_at, ?) "
                "WHERE capability_id = ?",
                (timestamp, str(capability_id)),
            ).rowcount
            if changed != 1:
                raise ConflictError("not_found", "capability does not exist")

    @staticmethod
    def _begin_request_tx(
        connection: sqlite3.Connection,
        host_id: HostId,
        request_id: RequestId,
        operation: str,
        semantic_fingerprint: str,
        timestamp: int,
    ) -> RequestRecord:
        operation = bounded_text(operation, "request.operation", maximum=64)
        existing = connection.execute(
            "SELECT * FROM request_records WHERE host_id = ? AND request_id = ?",
            (str(host_id), str(request_id)),
        ).fetchone()
        if existing is not None:
            record = _request(existing)
            if (
                record.operation != operation
                or record.semantic_fingerprint != semantic_fingerprint
            ):
                raise ConflictError(
                    "request_reuse",
                    "request UUID was reused for different semantic input",
                )
            return record
        connection.execute(
            "INSERT INTO request_records VALUES "
            "(?, ?, ?, ?, 'prepared', NULL, NULL, ?, NULL)",
            (
                str(host_id),
                str(request_id),
                operation,
                semantic_fingerprint,
                timestamp,
            ),
        )
        row = connection.execute(
            "SELECT * FROM request_records WHERE host_id = ? AND request_id = ?",
            (str(host_id), str(request_id)),
        ).fetchone()
        assert row is not None
        return _request(row)

    def begin_request(
        self,
        host_id: HostId,
        request_id: RequestId,
        operation: str,
        semantic_fingerprint: str,
        *,
        now: int | None = None,
    ) -> RequestRecord:
        self._require_local_host(host_id)
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            return self._begin_request_tx(
                connection,
                host_id,
                request_id,
                operation,
                semantic_fingerprint,
                timestamp,
            )

    def settle_request(
        self,
        host_id: HostId,
        request_id: RequestId,
        target: RequestState,
        *,
        result_type: str | None = None,
        result_id: str | None = None,
        now: int | None = None,
    ) -> RequestRecord:
        self._require_local_host(host_id)
        if target not in {RequestState.COMPLETED, RequestState.FAILED}:
            raise ValueError("request may settle only as completed or failed")
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM request_records WHERE host_id = ? AND request_id = ?",
                (str(host_id), str(request_id)),
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "request does not exist")
            current = _request(row)
            if current.state is target:
                if current.result_type != result_type or current.result_id != result_id:
                    raise ConflictError(
                        "request_result", "settled request result is immutable"
                    )
                return current
            require_state_edge(current.state, target, REQUEST_EDGES, "request state")
            connection.execute(
                "UPDATE request_records SET state = ?, result_type = ?, "
                "result_id = ?, completed_at = ? WHERE host_id = ? AND request_id = ?",
                (
                    target.value,
                    result_type,
                    result_id,
                    timestamp,
                    str(host_id),
                    str(request_id),
                ),
            )
        return self.get_request(host_id, request_id)

    def prepare_transition(
        self,
        transition: ViewTransition,
        *,
        desired_mode: ViewMode | None = None,
    ) -> ViewTransition:
        self._require_local_host(transition.host_id)
        if (transition.kind is TransitionKind.MODE) != (desired_mode is not None):
            raise ConflictError(
                "transition_mode", "only a mode transition accepts desired mode"
            )
        if (
            transition.state is not TransitionState.PREPARED
            or transition.transport_phase is not TransportPhase.INTENT
            or transition.execution_owner is not None
            or transition.lease_expires_at is not None
            or transition.failure is not None
        ):
            raise ConflictError(
                "transition_initial", "transition is not a clean prepared intent"
            )
        try:
            with self.transaction(immediate=True) as connection:
                existing = connection.execute(
                    "SELECT * FROM view_transitions "
                    "WHERE host_id = ? AND request_id = ?",
                    (str(transition.host_id), str(transition.request_id)),
                ).fetchone()
                request = self._begin_request_tx(
                    connection,
                    transition.host_id,
                    transition.request_id,
                    f"transition.{transition.kind.value}",
                    transition.request_fingerprint,
                    transition.created_at,
                )
                if existing is not None:
                    record = _transition(existing)
                    if record.transition_id != transition.transition_id:
                        raise ConflictError(
                            "request_result",
                            "request already identifies another transition",
                        )
                    return record
                if request.state is not RequestState.PREPARED:
                    raise ConflictError(
                        "request_settled", "settled request has no matching transition"
                    )
                view = connection.execute(
                    "SELECT * FROM user_views WHERE view_id = ?",
                    (str(transition.view_id),),
                ).fetchone()
                target = connection.execute(
                    "SELECT host_id, work_context_id, lifecycle_state "
                    "FROM frames WHERE frame_id = ?",
                    (str(transition.target_frame_id),),
                ).fetchone()
                if view is None or target is None:
                    raise ConflictError(
                        "not_found", "view or target frame does not exist"
                    )
                expected_current_revision = transition.expected_view_revision
                if desired_mode is not None:
                    expected_current_revision -= 1
                if (
                    view["host_id"] != str(transition.host_id)
                    or view["revision"] != expected_current_revision
                    or view["state"]
                    not in {ViewState.READY.value, ViewState.DEGRADED.value}
                    or target["host_id"] != str(transition.host_id)
                    or target["lifecycle_state"] != FrameLifecycleState.OPEN.value
                ):
                    raise ConflictError(
                        "transition_precondition", "view revision or host changed"
                    )
                if desired_mode is not None:
                    if (
                        transition.source_frame_id != transition.target_frame_id
                        or transition.source_frame_id
                        != FrameId(view["active_frame_id"])
                        or view["mode"] == desired_mode.value
                    ):
                        raise ConflictError(
                            "transition_mode", "mode intent does not change this view"
                        )
                    connection.execute(
                        "UPDATE user_views SET mode = ?, revision = revision + 1, "
                        "updated_at = ? WHERE view_id = ?",
                        (
                            desired_mode.value,
                            transition.created_at,
                            str(transition.view_id),
                        ),
                    )
                if transition.source_frame_id is not None:
                    source = connection.execute(
                        "SELECT host_id FROM frames WHERE frame_id = ?",
                        (str(transition.source_frame_id),),
                    ).fetchone()
                    if source is None or source["host_id"] != str(transition.host_id):
                        raise ConflictError(
                            "transition_source", "source frame does not match host"
                        )
                if transition.work_context_id is not None:
                    context = connection.execute(
                        "SELECT claim_generation FROM work_contexts "
                        "WHERE work_context_id = ?",
                        (str(transition.work_context_id),),
                    ).fetchone()
                    if (
                        context is None
                        or transition.expected_claim_generation is None
                        or context["claim_generation"]
                        != transition.expected_claim_generation
                        or target["work_context_id"] != str(transition.work_context_id)
                    ):
                        raise ConflictError(
                            "transition_claim", "WorkContext generation changed"
                        )
                connection.execute(
                    """
                    INSERT INTO view_transitions VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        str(transition.transition_id),
                        str(transition.request_id),
                        transition.request_fingerprint,
                        str(transition.host_id),
                        str(transition.view_id),
                        transition.kind.value,
                        None
                        if transition.source_frame_id is None
                        else str(transition.source_frame_id),
                        str(transition.target_frame_id),
                        None
                        if transition.work_context_id is None
                        else str(transition.work_context_id),
                        transition.expected_view_revision,
                        transition.expected_claim_generation,
                        transition.state.value,
                        None,
                        None,
                        transition.transport_phase.value,
                        None,
                        None,
                        None,
                        transition.created_at,
                        transition.updated_at,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("transition_conflict", str(error)) from error
        return self.get_transition(transition.transition_id)

    def claim_transition_execution(
        self,
        transition_id: TransitionId,
        execution_owner: str,
        lease_expires_at: int,
        *,
        now: int | None = None,
    ) -> ViewTransition:
        timestamp = _timestamp(now)
        execution_owner = bounded_text(execution_owner, "execution_owner", maximum=128)
        if lease_expires_at <= timestamp:
            raise ValueError("transition execution lease must be in the future")
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM view_transitions WHERE transition_id = ?",
                (str(transition_id),),
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "transition does not exist")
            transition = _transition(row)
            require_state_edge(
                transition.state,
                TransitionState.EXECUTING,
                TRANSITION_EDGES,
                "transition state",
            )
            view = connection.execute(
                "SELECT * FROM user_views WHERE view_id = ?",
                (str(transition.view_id),),
            ).fetchone()
            assert view is not None
            if view["revision"] != transition.expected_view_revision or view[
                "state"
            ] not in {ViewState.READY.value, ViewState.DEGRADED.value}:
                raise ConflictError("stale_revision", "view revision or state changed")
            if transition.source_frame_id is not None:
                source = connection.execute(
                    "SELECT state FROM frame_placements WHERE view_id = ? "
                    "AND frame_id = ?",
                    (str(transition.view_id), str(transition.source_frame_id)),
                ).fetchone()
                if source is None or source["state"] != PlacementState.ACTIVE.value:
                    raise ConflictError(
                        "source_placement", "source is not the active placement"
                    )
            target = connection.execute(
                "SELECT state FROM frame_placements WHERE view_id = ? AND frame_id = ?",
                (str(transition.view_id), str(transition.target_frame_id)),
            ).fetchone()
            if target is None or target["state"] not in {
                PlacementState.ACTIVE.value,
                PlacementState.PARKED.value,
                PlacementState.STAGED.value,
            }:
                raise ConflictError(
                    "target_placement", "target placement is unavailable"
                )
            if transition.work_context_id is not None:
                context = connection.execute(
                    "SELECT claim_generation FROM work_contexts "
                    "WHERE work_context_id = ?",
                    (str(transition.work_context_id),),
                ).fetchone()
                if (
                    context is None
                    or context["claim_generation"]
                    != transition.expected_claim_generation
                ):
                    raise ConflictError(
                        "stale_generation", "WorkContext generation changed"
                    )
            connection.execute(
                "UPDATE view_transitions SET state = 'executing', "
                "execution_owner = ?, lease_expires_at = ?, updated_at = ? "
                "WHERE transition_id = ?",
                (execution_owner, lease_expires_at, timestamp, str(transition_id)),
            )
            connection.execute(
                "UPDATE user_views SET state = 'transitioning', "
                "revision = revision + 1, updated_at = ? WHERE view_id = ?",
                (timestamp, str(transition.view_id)),
            )
        return self.get_transition(transition_id)

    def reclaim_transition_execution(
        self,
        transition_id: TransitionId,
        execution_owner: str,
        lease_expires_at: int,
        *,
        now: int | None = None,
    ) -> ViewTransition:
        timestamp = _timestamp(now)
        execution_owner = bounded_text(execution_owner, "execution_owner", maximum=128)
        if lease_expires_at <= timestamp:
            raise ValueError("transition execution lease must be in the future")
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM view_transitions WHERE transition_id = ?",
                (str(transition_id),),
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "transition does not exist")
            transition = _transition(row)
            if transition.state is not TransitionState.EXECUTING:
                raise ConflictError("transition_state", "transition is not executing")
            if (
                transition.lease_expires_at is None
                or transition.lease_expires_at > timestamp
            ):
                raise ConflictError("lease_active", "transition lease is still active")
            connection.execute(
                "UPDATE view_transitions SET execution_owner = ?, "
                "lease_expires_at = ?, updated_at = ? WHERE transition_id = ?",
                (execution_owner, lease_expires_at, timestamp, str(transition_id)),
            )
        return self.get_transition(transition_id)

    @staticmethod
    def _require_execution_owner(
        transition: ViewTransition, execution_owner: str, timestamp: int
    ) -> None:
        if transition.execution_owner != execution_owner:
            raise ConflictError("execution_owner", "transition execution owner changed")
        if (
            transition.lease_expires_at is None
            or transition.lease_expires_at <= timestamp
        ):
            raise ConflictError("lease_expired", "transition execution lease expired")

    def advance_transport_phase(
        self,
        transition_id: TransitionId,
        execution_owner: str,
        expected_phase: TransportPhase,
        target: TransportPhase,
        *,
        now: int | None = None,
    ) -> ViewTransition:
        timestamp = _timestamp(now)
        require_state_edge(expected_phase, target, TRANSPORT_EDGES, "transport phase")
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM view_transitions WHERE transition_id = ?",
                (str(transition_id),),
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "transition does not exist")
            transition = _transition(row)
            self._require_execution_owner(transition, execution_owner, timestamp)
            if (
                transition.state is not TransitionState.EXECUTING
                or transition.transport_phase is not expected_phase
            ):
                raise ConflictError(
                    "stale_state", "transition or transport state changed"
                )
            connection.execute(
                "UPDATE view_transitions SET transport_phase = ?, updated_at = ? "
                "WHERE transition_id = ?",
                (target.value, timestamp, str(transition_id)),
            )
        return self.get_transition(transition_id)

    def commit_transition_presentation(
        self,
        transition_id: TransitionId,
        execution_owner: str,
        *,
        now: int | None = None,
    ) -> ViewTransition:
        """Commit inspected presentation, placement, view, and foreground atomically."""

        timestamp = _timestamp(now)
        try:
            with self.transaction(immediate=True) as connection:
                row = connection.execute(
                    "SELECT * FROM view_transitions WHERE transition_id = ?",
                    (str(transition_id),),
                ).fetchone()
                if row is None:
                    raise ConflictError("not_found", "transition does not exist")
                transition = _transition(row)
                self._require_execution_owner(transition, execution_owner, timestamp)
                if (
                    transition.state is not TransitionState.EXECUTING
                    or transition.transport_phase is not TransportPhase.INSPECTED
                ):
                    raise ConflictError(
                        "transition_state", "presentation has not been inspected"
                    )
                view = connection.execute(
                    "SELECT * FROM user_views WHERE view_id = ?",
                    (str(transition.view_id),),
                ).fetchone()
                assert view is not None
                if (
                    view["state"] != ViewState.TRANSITIONING.value
                    or view["revision"] != transition.expected_view_revision + 1
                ):
                    raise ConflictError(
                        "stale_revision", "transitioning view revision changed"
                    )
                target = connection.execute(
                    "SELECT * FROM frame_placements WHERE view_id = ? AND frame_id = ?",
                    (str(transition.view_id), str(transition.target_frame_id)),
                ).fetchone()
                if target is None:
                    raise ConflictError(
                        "target_placement", "target placement is missing"
                    )
                target_placement = _placement(target)
                same_frame = transition.source_frame_id == transition.target_frame_id
                if same_frame:
                    if target_placement.state is not PlacementState.ACTIVE:
                        raise ConflictError(
                            "target_placement", "same-frame target is not active"
                        )
                else:
                    source = connection.execute(
                        "SELECT * FROM frame_placements WHERE view_id = ? "
                        "AND frame_id = ?",
                        (
                            str(transition.view_id),
                            None
                            if transition.source_frame_id is None
                            else str(transition.source_frame_id),
                        ),
                    ).fetchone()
                    if source is None or source["state"] != PlacementState.ACTIVE.value:
                        raise ConflictError(
                            "source_placement", "source placement is not active"
                        )
                    require_state_edge(
                        PlacementState(source["state"]),
                        PlacementState.PARKED,
                        PLACEMENT_EDGES,
                        "placement state",
                    )
                    require_state_edge(
                        target_placement.state,
                        PlacementState.ACTIVE,
                        PLACEMENT_EDGES,
                        "placement state",
                    )
                    connection.execute(
                        "UPDATE frame_placements SET state = 'parked', "
                        "generation = generation + 1, updated_at = ? "
                        "WHERE placement_id = ?",
                        (timestamp, source["placement_id"]),
                    )
                    connection.execute(
                        "UPDATE frame_placements SET state = 'active', "
                        "generation = generation + 1, last_focused_at = ?, "
                        "updated_at = ? WHERE placement_id = ?",
                        (
                            timestamp,
                            timestamp,
                            str(target_placement.placement_id),
                        ),
                    )
                    if target_placement.surface_id is not None:
                        connection.execute(
                            "UPDATE agent_capabilities SET placement_generation = ? "
                            "WHERE surface_id = ? AND revoked_at IS NULL",
                            (
                                target_placement.generation + 1,
                                str(target_placement.surface_id),
                            ),
                        )
                if transition.work_context_id is not None:
                    context = connection.execute(
                        "SELECT * FROM work_contexts WHERE work_context_id = ?",
                        (str(transition.work_context_id),),
                    ).fetchone()
                    if (
                        context is None
                        or context["claim_state"] != ClaimState.HELD.value
                        or context["claim_generation"]
                        != transition.expected_claim_generation
                    ):
                        raise ConflictError(
                            "stale_generation", "WorkContext claim changed"
                        )
                    connection.execute(
                        "UPDATE work_contexts SET foreground_frame_id = ?, "
                        "claim_generation = claim_generation + 1, updated_at = ? "
                        "WHERE work_context_id = ?",
                        (
                            str(transition.target_frame_id),
                            timestamp,
                            str(transition.work_context_id),
                        ),
                    )
                connection.execute(
                    "UPDATE user_views SET state = 'ready', active_frame_id = ?, "
                    "revision = revision + 1, updated_at = ? WHERE view_id = ?",
                    (
                        str(transition.target_frame_id),
                        timestamp,
                        str(transition.view_id),
                    ),
                )
                connection.execute(
                    "UPDATE view_transitions SET state = 'presented', "
                    "transport_phase = 'committed', updated_at = ? "
                    "WHERE transition_id = ?",
                    (timestamp, str(transition_id)),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("presentation_conflict", str(error)) from error
        return self.get_transition(transition_id)

    def advance_transition_state(
        self,
        transition_id: TransitionId,
        expected_state: TransitionState,
        target: TransitionState,
        *,
        execution_owner: str | None = None,
        failure: FailureRecord | None = None,
        now: int | None = None,
    ) -> ViewTransition:
        if target is TransitionState.PRESENTED:
            raise ValueError("use commit_transition_presentation for presentation")
        if (target is TransitionState.FAILED) != (failure is not None):
            raise ValueError(
                "failed transition requires and only failed transition accepts failure"
            )
        timestamp = _timestamp(now)
        require_state_edge(expected_state, target, TRANSITION_EDGES, "transition state")
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM view_transitions WHERE transition_id = ?",
                (str(transition_id),),
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "transition does not exist")
            transition = _transition(row)
            if transition.state is not expected_state:
                raise ConflictError("stale_state", "transition state changed")
            if (
                transition.execution_owner is not None
                and execution_owner != transition.execution_owner
            ):
                raise ConflictError(
                    "execution_owner", "transition execution owner changed"
                )
            if target is TransitionState.COMPLETED and (
                transition.transport_phase is not TransportPhase.COMMITTED
            ):
                raise ConflictError(
                    "transport_uncommitted", "transition transport is not committed"
                )
            connection.execute(
                "UPDATE view_transitions SET state = ?, failure_code = ?, "
                "failure_message = ?, failure_retryable = ?, updated_at = ? "
                "WHERE transition_id = ?",
                (
                    target.value,
                    None if failure is None else failure.code,
                    None if failure is None else failure.message,
                    None if failure is None else failure.retryable,
                    timestamp,
                    str(transition_id),
                ),
            )
            if target in {
                TransitionState.COMPLETED,
                TransitionState.CANCELLED,
                TransitionState.SUPERSEDED,
                TransitionState.FAILED,
            }:
                request_target = (
                    RequestState.COMPLETED
                    if target is TransitionState.COMPLETED
                    else RequestState.FAILED
                )
                connection.execute(
                    "UPDATE request_records SET state = ?, result_type = 'transition', "
                    "result_id = ?, completed_at = ? WHERE host_id = ? "
                    "AND request_id = ? AND state = 'prepared'",
                    (
                        request_target.value,
                        str(transition_id),
                        timestamp,
                        str(transition.host_id),
                        str(transition.request_id),
                    ),
                )
                if target is not TransitionState.COMPLETED:
                    connection.execute(
                        "UPDATE user_views SET state = 'degraded', "
                        "revision = revision + 1, updated_at = ? "
                        "WHERE view_id = ? AND state = 'transitioning'",
                        (timestamp, str(transition.view_id)),
                    )
        return self.get_transition(transition_id)

    def store_transition_brief(self, brief: TransitionBrief) -> TransitionBrief:
        values = (
            str(brief.brief_id),
            str(brief.transition_id),
            str(brief.source_frame_id),
            str(brief.source_session_key),
            str(brief.target_frame_id),
            brief.brief,
            brief.content_hash,
            brief.created_at,
            brief.first_claimed_at,
        )
        with self.transaction(immediate=True) as connection:
            transition_row = connection.execute(
                "SELECT * FROM view_transitions WHERE transition_id = ?",
                (str(brief.transition_id),),
            ).fetchone()
            if transition_row is None:
                raise ConflictError("not_found", "transition does not exist")
            transition = _transition(transition_row)
            if (
                transition.kind is not TransitionKind.PUSH
                or transition.source_frame_id != brief.source_frame_id
                or transition.target_frame_id != brief.target_frame_id
            ):
                raise ConflictError(
                    "brief_identity", "brief does not match push transition"
                )
            source_session = connection.execute(
                "SELECT 1 FROM frames AS frame "
                "JOIN frame_sessions AS membership "
                "ON membership.frame_id = frame.frame_id "
                "WHERE frame.frame_id = ? AND membership.session_key = ? "
                "AND frame.current_session_key = ?",
                (
                    str(brief.source_frame_id),
                    str(brief.source_session_key),
                    str(brief.source_session_key),
                ),
            ).fetchone()
            if source_session is None:
                raise ConflictError(
                    "brief_session", "brief source session is not current for frame"
                )
            existing = connection.execute(
                "SELECT * FROM transition_briefs WHERE brief_id = ? "
                "OR transition_id = ?",
                (str(brief.brief_id), str(brief.transition_id)),
            ).fetchone()
            if existing is not None:
                if tuple(existing)[:-1] != values[:-1]:
                    raise ConflictError("brief_conflict", "brief content is immutable")
                return _brief(existing)
            connection.execute(
                "INSERT INTO transition_briefs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return brief

    def store_completion_handoff(self, handoff: CompletionHandoff) -> CompletionHandoff:
        values = (
            str(handoff.handoff_id),
            str(handoff.transition_id),
            str(handoff.source_frame_id),
            str(handoff.source_session_key),
            str(handoff.target_frame_id),
            handoff.summary,
            handoff.next_action,
            handoff.content_hash,
            handoff.created_at,
            handoff.first_claimed_at,
        )
        with self.transaction(immediate=True) as connection:
            transition_row = connection.execute(
                "SELECT * FROM view_transitions WHERE transition_id = ?",
                (str(handoff.transition_id),),
            ).fetchone()
            if transition_row is None:
                raise ConflictError("not_found", "transition does not exist")
            transition = _transition(transition_row)
            if (
                transition.kind is not TransitionKind.COMPLETE_RETURN
                or transition.source_frame_id != handoff.source_frame_id
                or transition.target_frame_id != handoff.target_frame_id
            ):
                raise ConflictError(
                    "handoff_identity",
                    "completion handoff does not match transition",
                )
            source_session = connection.execute(
                "SELECT 1 FROM frames AS frame "
                "JOIN frame_sessions AS membership "
                "ON membership.frame_id = frame.frame_id "
                "WHERE frame.frame_id = ? AND membership.session_key = ? "
                "AND frame.current_session_key = ?",
                (
                    str(handoff.source_frame_id),
                    str(handoff.source_session_key),
                    str(handoff.source_session_key),
                ),
            ).fetchone()
            if source_session is None:
                raise ConflictError(
                    "handoff_session",
                    "completion source session is not current for frame",
                )
            existing = connection.execute(
                "SELECT * FROM completion_handoffs WHERE handoff_id = ? "
                "OR transition_id = ?",
                (str(handoff.handoff_id), str(handoff.transition_id)),
            ).fetchone()
            if existing is not None:
                if tuple(existing)[:-1] != values[:-1]:
                    raise ConflictError(
                        "handoff_conflict", "completion handoff content is immutable"
                    )
                return _completion_handoff(existing)
            frame = connection.execute(
                "SELECT lifecycle_state FROM frames WHERE frame_id = ?",
                (str(handoff.source_frame_id),),
            ).fetchone()
            if (
                frame is None
                or frame["lifecycle_state"] != FrameLifecycleState.OPEN.value
            ):
                raise ConflictError(
                    "frame_state", "completion source frame is not open"
                )
            connection.execute(
                "INSERT INTO completion_handoffs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            connection.execute(
                "UPDATE frames SET lifecycle_state = 'closing', updated_at = ? "
                "WHERE frame_id = ?",
                (handoff.created_at, str(handoff.source_frame_id)),
            )
        return handoff

    def prepare_control_turn(self, control: ControlTurn) -> ControlTurn:
        if (
            control.state is not ControlState.PREPARED
            or control.submission_count != 0
            or any(
                value is not None
                for value in (
                    control.submitted_at,
                    control.observed_prompt_id,
                    control.claimed_at,
                    control.settled_at,
                    control.failure,
                )
            )
        ):
            raise ConflictError(
                "control_initial", "control turn is not a clean prepared record"
            )
        try:
            with self.transaction(immediate=True) as connection:
                existing = connection.execute(
                    "SELECT * FROM control_turns WHERE control_turn_id = ? "
                    "OR transition_id = ?",
                    (str(control.control_turn_id), str(control.transition_id)),
                ).fetchone()
                if existing is not None:
                    persisted = _control_turn(existing)
                    if (
                        persisted.control_turn_id != control.control_turn_id
                        or persisted.transition_id != control.transition_id
                        or persisted.target_frame_id != control.target_frame_id
                        or persisted.target_session_key != control.target_session_key
                        or persisted.kind != control.kind
                        or persisted.template_version != control.template_version
                        or persisted.transport != control.transport
                    ):
                        raise ConflictError(
                            "control_conflict", "control turn identity is immutable"
                        )
                    return persisted
                transition_row = connection.execute(
                    "SELECT * FROM view_transitions WHERE transition_id = ?",
                    (str(control.transition_id),),
                ).fetchone()
                frame = connection.execute(
                    "SELECT current_session_key FROM frames WHERE frame_id = ?",
                    (str(control.target_frame_id),),
                ).fetchone()
                if transition_row is None or frame is None:
                    raise ConflictError(
                        "not_found", "transition or frame does not exist"
                    )
                transition = _transition(transition_row)
                expected_kind = (
                    ControlKind.CLAIM_BRIEF
                    if transition.kind is TransitionKind.PUSH
                    else ControlKind.CLAIM_HANDOFF
                )
                semantic_table = (
                    "transition_briefs"
                    if control.kind is ControlKind.CLAIM_BRIEF
                    else "completion_handoffs"
                )
                if (
                    control.target_frame_id != transition.target_frame_id
                    or control.kind is not expected_kind
                    or frame["current_session_key"] != str(control.target_session_key)
                    or connection.execute(
                        f"SELECT 1 FROM {semantic_table} WHERE transition_id = ?",
                        (str(control.transition_id),),
                    ).fetchone()
                    is None
                ):
                    raise ConflictError(
                        "control_identity",
                        "control turn target or semantic record differs",
                    )
                connection.execute(
                    """
                    INSERT INTO control_turns VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        str(control.control_turn_id),
                        str(control.transition_id),
                        str(control.target_frame_id),
                        str(control.target_session_key),
                        control.kind.value,
                        control.template_version,
                        control.transport.value,
                        control.state.value,
                        0,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("control_conflict", str(error)) from error
        return self.get_control_turn(control.control_turn_id)

    def advance_control_turn(
        self,
        control_turn_id: ControlTurnId,
        expected_state: ControlState,
        target: ControlState,
        *,
        observed_prompt_id: str | None = None,
        failure: FailureRecord | None = None,
        now: int | None = None,
    ) -> ControlTurn:
        timestamp = _timestamp(now)
        require_state_edge(expected_state, target, CONTROL_EDGES, "control state")
        if (target is ControlState.FAILED) != (failure is not None):
            raise ValueError(
                "failed control requires and only failed control accepts failure"
            )
        if target is ControlState.OBSERVED and observed_prompt_id is None:
            raise ValueError("observed control requires prompt identity")
        if observed_prompt_id is not None:
            observed_prompt_id = bounded_text(
                observed_prompt_id, "observed_prompt_id", maximum=256
            )
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM control_turns WHERE control_turn_id = ?",
                (str(control_turn_id),),
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "control turn does not exist")
            control = _control_turn(row)
            if control.state is not expected_state:
                raise ConflictError("stale_state", "control state changed")
            submission_count = control.submission_count
            submitted_at = control.submitted_at
            claimed_at = control.claimed_at
            settled_at = control.settled_at
            if target is ControlState.SUBMITTED:
                if submission_count != 0:
                    raise ConflictError(
                        "already_submitted", "control turn cannot be submitted twice"
                    )
                submission_count = 1
                submitted_at = timestamp
            if target is ControlState.CLAIMED:
                claimed_at = timestamp
            if target is ControlState.SETTLED:
                settled_at = timestamp
            connection.execute(
                """
                UPDATE control_turns SET state = ?, submission_count = ?,
                    submitted_at = ?,
                    observed_prompt_id = COALESCE(?, observed_prompt_id),
                    claimed_at = ?, settled_at = ?, failure_code = ?,
                    failure_message = ?, failure_retryable = ?
                WHERE control_turn_id = ?
                """,
                (
                    target.value,
                    submission_count,
                    submitted_at,
                    observed_prompt_id,
                    claimed_at,
                    settled_at,
                    None if failure is None else failure.code,
                    None if failure is None else failure.message,
                    None if failure is None else failure.retryable,
                    str(control_turn_id),
                ),
            )
        return self.get_control_turn(control_turn_id)

    @staticmethod
    def _validate_capability_tx(
        connection: sqlite3.Connection,
        raw_capability: str,
        timestamp: int,
    ) -> sqlite3.Row:
        digest = sha256(raw_capability.encode("utf-8")).hexdigest()
        row = connection.execute(
            """
            SELECT capability.*, placement.state AS placement_state,
                   placement.generation AS current_placement_generation,
                   view.state AS current_view_state,
                   view.active_frame_id AS current_active_frame_id,
                   frame.current_session_key AS frame_session_key,
                   surface.lifecycle_state AS current_surface_state,
                   surface.session_key AS current_session_key,
                   surface.launch_id AS current_launch_id,
                   surface.tmux_server_id AS current_tmux_server_id,
                   surface.pane_id AS current_pane_id,
                   launch.state AS current_launch_state
            FROM agent_capabilities AS capability
            JOIN frame_placements AS placement
              ON placement.view_id = capability.view_id
             AND placement.frame_id = capability.frame_id
             AND placement.surface_id = capability.surface_id
            JOIN user_views AS view ON view.view_id = capability.view_id
            JOIN frames AS frame ON frame.frame_id = capability.frame_id
            JOIN surfaces AS surface ON surface.surface_id = capability.surface_id
            JOIN launch_intents AS launch ON launch.launch_id = capability.launch_id
            WHERE capability.capability_digest = ?
            """,
            (digest,),
        ).fetchone()
        if row is None:
            raise ConflictError("capability_invalid", "capability is unknown")
        if row["revoked_at"] is not None or row["expires_at"] <= timestamp:
            raise ConflictError("capability_expired", "capability expired or revoked")
        if (
            row["placement_state"] != PlacementState.ACTIVE.value
            or row["current_placement_generation"] != row["placement_generation"]
            or row["current_view_state"] != ViewState.READY.value
            or row["current_active_frame_id"] != row["frame_id"]
            or row["frame_session_key"] != row["session_key"]
            or row["current_surface_state"] != SurfaceState.LIVE.value
            or row["current_session_key"] != row["session_key"]
            or row["current_launch_id"] != row["launch_id"]
            or row["current_tmux_server_id"] != row["tmux_server_id"]
            or row["current_pane_id"] != row["pane_id"]
            or row["current_launch_state"] != LaunchState.BOUND.value
        ):
            raise ConflictError(
                "capability_stale", "capability no longer matches active ownership"
            )
        return row

    def validate_capability(
        self, raw_capability: str, *, now: int | None = None
    ) -> AgentCapability:
        timestamp = _timestamp(now)
        row = self._validate_capability_tx(self.connection, raw_capability, timestamp)
        return AgentCapability(
            CapabilityId(row["capability_id"]),
            str(row["capability_digest"]),
            HostId(row["host_id"]),
            ViewId(row["view_id"]),
            FrameId(row["frame_id"]),
            None
            if row["session_key"] is None
            else SessionKey.parse(row["session_key"]),
            SurfaceId(row["surface_id"]),
            LaunchId(row["launch_id"]),
            None
            if row["tmux_server_id"] is None
            else TmuxServerId(row["tmux_server_id"]),
            None if row["pane_id"] is None else str(row["pane_id"]),
            int(row["placement_generation"]),
            int(row["issued_at"]),
            int(row["expires_at"]),
            None if row["revoked_at"] is None else int(row["revoked_at"]),
        )

    def transition_claim(
        self,
        transition_id: TransitionId,
        target_session_key: SessionKey,
        raw_capability: str,
        *,
        now: int | None = None,
    ) -> TransitionClaim:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            capability = self._validate_capability_tx(
                connection, raw_capability, timestamp
            )
            transition_row = connection.execute(
                "SELECT * FROM view_transitions WHERE transition_id = ?",
                (str(transition_id),),
            ).fetchone()
            if transition_row is None:
                raise ConflictError("not_found", "transition does not exist")
            transition = _transition(transition_row)
            if transition.state not in {
                TransitionState.AWAITING_CLAIM,
                TransitionState.SETTLING,
                TransitionState.COMPLETED,
            }:
                raise ConflictError(
                    "transition_state", "transition is not awaiting a claim"
                )
            if capability["frame_id"] != str(transition.target_frame_id) or capability[
                "session_key"
            ] != str(target_session_key):
                raise ConflictError(
                    "claim_identity", "capability does not identify transition target"
                )
            frame = connection.execute(
                "SELECT current_session_key FROM frames WHERE frame_id = ?",
                (str(transition.target_frame_id),),
            ).fetchone()
            if frame is None or frame["current_session_key"] != str(target_session_key):
                raise ConflictError(
                    "claim_session", "target session is not current for frame"
                )
            if transition.work_context_id is not None:
                context = connection.execute(
                    "SELECT claim_state, claim_generation, foreground_frame_id "
                    "FROM work_contexts WHERE work_context_id = ?",
                    (str(transition.work_context_id),),
                ).fetchone()
                expected_generation = transition.expected_claim_generation
                assert expected_generation is not None
                if (
                    context is None
                    or context["claim_state"] != ClaimState.HELD.value
                    or context["claim_generation"] != expected_generation + 1
                    or context["foreground_frame_id"] != str(transition.target_frame_id)
                ):
                    raise ConflictError(
                        "claim_generation", "foreground claim transfer is not exact"
                    )
            brief_row = connection.execute(
                "SELECT * FROM transition_briefs WHERE transition_id = ?",
                (str(transition_id),),
            ).fetchone()
            handoff_row = connection.execute(
                "SELECT * FROM completion_handoffs WHERE transition_id = ?",
                (str(transition_id),),
            ).fetchone()
            if (brief_row is None) == (handoff_row is None):
                raise ConflictError(
                    "claim_semantic", "transition must have exactly one semantic record"
                )
            control_row = connection.execute(
                "SELECT * FROM control_turns WHERE transition_id = ?",
                (str(transition_id),),
            ).fetchone()
            if control_row is not None:
                control = _control_turn(control_row)
                if (
                    control.target_session_key != target_session_key
                    or control.target_frame_id != transition.target_frame_id
                ):
                    raise ConflictError(
                        "control_identity", "control turn target differs from claim"
                    )
                if control.state not in {
                    ControlState.OBSERVED,
                    ControlState.UNCERTAIN,
                    ControlState.CLAIMED,
                    ControlState.SETTLED,
                }:
                    raise ConflictError(
                        "control_state", "control turn is not claimable"
                    )
                if control.state in {ControlState.OBSERVED, ControlState.UNCERTAIN}:
                    connection.execute(
                        "UPDATE control_turns SET state = 'claimed', "
                        "claimed_at = COALESCE(claimed_at, ?) "
                        "WHERE control_turn_id = ?",
                        (timestamp, str(control.control_turn_id)),
                    )
            if brief_row is not None:
                connection.execute(
                    "UPDATE transition_briefs SET first_claimed_at = "
                    "COALESCE(first_claimed_at, ?) WHERE transition_id = ?",
                    (timestamp, str(transition_id)),
                )
                brief = _brief(brief_row)
                claim = TransitionClaim(
                    "brief",
                    transition_id,
                    transition.target_frame_id,
                    brief=brief.brief,
                )
            else:
                assert handoff_row is not None
                connection.execute(
                    "UPDATE completion_handoffs SET first_claimed_at = "
                    "COALESCE(first_claimed_at, ?) WHERE transition_id = ?",
                    (timestamp, str(transition_id)),
                )
                handoff = _completion_handoff(handoff_row)
                source = connection.execute(
                    "SELECT lifecycle_state FROM frames WHERE frame_id = ?",
                    (str(handoff.source_frame_id),),
                ).fetchone()
                if source is None:
                    raise ConflictError("not_found", "completion source is missing")
                if source["lifecycle_state"] == FrameLifecycleState.CLOSING.value:
                    connection.execute(
                        "UPDATE frames SET lifecycle_state = 'closed', "
                        "close_reason = 'completed', updated_at = ? WHERE frame_id = ?",
                        (timestamp, str(handoff.source_frame_id)),
                    )
                elif source["lifecycle_state"] != FrameLifecycleState.CLOSED.value:
                    raise ConflictError(
                        "frame_state", "completion source is not closing"
                    )
                claim = TransitionClaim(
                    "handoff",
                    transition_id,
                    transition.target_frame_id,
                    summary=handoff.summary,
                    next_action=handoff.next_action,
                )
            if transition.state is TransitionState.AWAITING_CLAIM:
                connection.execute(
                    "UPDATE view_transitions SET state = 'settling', updated_at = ? "
                    "WHERE transition_id = ?",
                    (timestamp, str(transition_id)),
                )
        return claim

    def settle_transition_claim(
        self,
        transition_id: TransitionId,
        *,
        now: int | None = None,
    ) -> ViewTransition:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM view_transitions WHERE transition_id = ?",
                (str(transition_id),),
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "transition does not exist")
            transition = _transition(row)
            if transition.state is TransitionState.COMPLETED:
                return transition
            require_state_edge(
                transition.state,
                TransitionState.COMPLETED,
                TRANSITION_EDGES,
                "transition state",
            )
            if transition.transport_phase is not TransportPhase.COMMITTED:
                raise ConflictError(
                    "transport_uncommitted", "transition transport is not committed"
                )
            semantic_claimed = connection.execute(
                "SELECT first_claimed_at FROM transition_briefs "
                "WHERE transition_id = ? "
                "UNION ALL SELECT first_claimed_at FROM completion_handoffs "
                "WHERE transition_id = ?",
                (str(transition_id), str(transition_id)),
            ).fetchone()
            if semantic_claimed is None or semantic_claimed[0] is None:
                raise ConflictError("claim_missing", "semantic record was not claimed")
            control = connection.execute(
                "SELECT * FROM control_turns WHERE transition_id = ?",
                (str(transition_id),),
            ).fetchone()
            if control is not None:
                control_record = _control_turn(control)
                if control_record.state is ControlState.CLAIMED:
                    connection.execute(
                        "UPDATE control_turns SET state = 'settled', settled_at = ? "
                        "WHERE control_turn_id = ?",
                        (timestamp, str(control_record.control_turn_id)),
                    )
                elif control_record.state is not ControlState.SETTLED:
                    raise ConflictError(
                        "control_state", "control turn has not been claimed"
                    )
            connection.execute(
                "UPDATE view_transitions SET state = 'completed', updated_at = ? "
                "WHERE transition_id = ?",
                (timestamp, str(transition_id)),
            )
            connection.execute(
                "UPDATE request_records SET state = 'completed', "
                "result_type = 'transition', result_id = ?, completed_at = ? "
                "WHERE host_id = ? AND request_id = ? AND state = 'prepared'",
                (
                    str(transition_id),
                    timestamp,
                    str(transition.host_id),
                    str(transition.request_id),
                ),
            )
        return self.get_transition(transition_id)

    def open_recovery(self, recovery: Recovery) -> Recovery:
        self._require_local_host(recovery.host_id)
        if recovery.state is not RecoveryState.OPEN:
            raise ValueError("new recovery must be open")
        try:
            with self.transaction(immediate=True) as connection:
                existing = connection.execute(
                    """
                    SELECT * FROM recoveries
                    WHERE host_id = ? AND kind = ? AND subject_type = ?
                      AND subject_id = ? AND state = 'open'
                    """,
                    (
                        str(recovery.host_id),
                        recovery.kind,
                        recovery.subject_type,
                        recovery.subject_id,
                    ),
                ).fetchone()
                if existing is not None:
                    return _recovery(existing)
                connection.execute(
                    "INSERT INTO recoveries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(recovery.recovery_id),
                        str(recovery.host_id),
                        recovery.kind,
                        recovery.subject_type,
                        recovery.subject_id,
                        recovery.actionability.value,
                        recovery.state.value,
                        recovery.bounded_explanation,
                        recovery.created_at,
                        recovery.updated_at,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("recovery_conflict", str(error)) from error
        return self.get_recovery(recovery.recovery_id)

    def settle_recovery(
        self,
        recovery_id: RecoveryId,
        target: RecoveryState,
        *,
        now: int | None = None,
    ) -> Recovery:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM recoveries WHERE recovery_id = ?",
                (str(recovery_id),),
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "recovery does not exist")
            recovery = _recovery(row)
            require_state_edge(recovery.state, target, RECOVERY_EDGES, "recovery state")
            connection.execute(
                "UPDATE recoveries SET state = ?, updated_at = ? WHERE recovery_id = ?",
                (target.value, timestamp, str(recovery_id)),
            )
        return self.get_recovery(recovery_id)

    @staticmethod
    def _expire_leases_tx(connection: sqlite3.Connection, timestamp: int) -> int:
        return connection.execute(
            "UPDATE desktop_attachment_leases SET state = 'expired' "
            "WHERE state = 'offered' AND expires_at <= ?",
            (timestamp,),
        ).rowcount

    def expire_desktop_leases(self, *, now: int | None = None) -> int:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            return self._expire_leases_tx(connection, timestamp)

    def offer_desktop_lease(
        self, lease: DesktopAttachmentLease, *, now: int | None = None
    ) -> DesktopAttachmentLease:
        timestamp = _timestamp(now)
        if lease.state is not LeaseState.OFFERED or lease.expires_at <= timestamp:
            raise ValueError("new desktop lease must be an unexpired offer")
        try:
            with self.transaction(immediate=True) as connection:
                self._expire_leases_tx(connection, timestamp)
                exact = connection.execute(
                    "SELECT * FROM desktop_attachment_leases "
                    "WHERE view_id = ? AND request_id = ?",
                    (str(lease.view_id), str(lease.request_id)),
                ).fetchone()
                if exact is not None:
                    record = _lease(exact)
                    if record.state is LeaseState.OFFERED:
                        return record
                    raise ConflictError(
                        "lease_settled", "desktop request lease is already settled"
                    )
                offered = connection.execute(
                    "SELECT 1 FROM desktop_attachment_leases "
                    "WHERE view_id = ? AND state = 'offered'",
                    (str(lease.view_id),),
                ).fetchone()
                if offered is not None:
                    raise ConflictError(
                        "desktop_launch_in_progress",
                        "another desktop attachment request owns the view",
                    )
                connection.execute(
                    "INSERT INTO desktop_attachment_leases VALUES (?, ?, ?, ?, ?)",
                    (
                        str(lease.lease_id),
                        str(lease.view_id),
                        str(lease.request_id),
                        lease.state.value,
                        lease.expires_at,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("lease_conflict", str(error)) from error
        return self.get_lease(lease.lease_id)

    def claim_desktop_lease(
        self,
        view_id: ViewId,
        request_id: RequestId,
        *,
        now: int | None = None,
    ) -> DesktopAttachmentLease:
        timestamp = _timestamp(now)
        with self.transaction(immediate=True) as connection:
            self._expire_leases_tx(connection, timestamp)
            row = connection.execute(
                "SELECT * FROM desktop_attachment_leases "
                "WHERE view_id = ? AND request_id = ?",
                (str(view_id), str(request_id)),
            ).fetchone()
            if row is None:
                raise ConflictError("not_found", "desktop attachment lease is missing")
            lease = _lease(row)
            if lease.state is LeaseState.CLAIMED:
                return lease
            require_state_edge(
                lease.state, LeaseState.CLAIMED, LEASE_EDGES, "lease state"
            )
            connection.execute(
                "UPDATE desktop_attachment_leases SET state = 'claimed' "
                "WHERE lease_id = ?",
                (str(lease.lease_id),),
            )
        return self.get_lease(lease.lease_id)

    def cache_host_state(self, cached: HostStateCache) -> HostStateCache:
        if cached.host_id == self._local_host_id:
            raise ConflictError(
                "cache_local_host", "local owner state cannot be stored as remote cache"
            )
        try:
            with self.transaction(immediate=True) as connection:
                existing = connection.execute(
                    "SELECT * FROM host_state_cache WHERE remote_name = ? "
                    "OR host_id = ?",
                    (cached.remote_name, str(cached.host_id)),
                ).fetchone()
                if existing is not None:
                    current = _host_state_cache(existing)
                    if (
                        current.remote_name != cached.remote_name
                        or current.host_id != cached.host_id
                    ):
                        raise ConflictError(
                            "remote_identity", "remote name and host identity disagree"
                        )
                    if cached.observed_at < current.observed_at:
                        raise ConflictError(
                            "stale_observation", "remote HostState moved backwards"
                        )
                    if (
                        cached.observed_at == current.observed_at
                        and cached.content_hash != current.content_hash
                    ):
                        raise ConflictError(
                            "observation_conflict",
                            "equal remote observation time has different content",
                        )
                connection.execute(
                    """
                    INSERT INTO host_state_cache VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    ) ON CONFLICT(remote_name) DO UPDATE SET
                        state_json = excluded.state_json,
                        content_hash = excluded.content_hash,
                        observed_at = excluded.observed_at,
                        received_at = excluded.received_at,
                        last_attempt_at = excluded.last_attempt_at,
                        reachability = excluded.reachability,
                        error_code = excluded.error_code,
                        error_message = excluded.error_message,
                        error_retryable = excluded.error_retryable
                    """,
                    (
                        cached.remote_name,
                        str(cached.host_id),
                        cached.state_json,
                        cached.content_hash,
                        cached.observed_at,
                        cached.received_at,
                        cached.last_attempt_at,
                        cached.reachability.value,
                        None
                        if cached.bounded_error is None
                        else cached.bounded_error.code,
                        None
                        if cached.bounded_error is None
                        else cached.bounded_error.message,
                        None
                        if cached.bounded_error is None
                        else cached.bounded_error.retryable,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("cache_conflict", str(error)) from error
        row = self.connection.execute(
            "SELECT * FROM host_state_cache WHERE remote_name = ?",
            (cached.remote_name,),
        ).fetchone()
        assert row is not None
        return _host_state_cache(row)

    def cached_host_states(self) -> tuple[HostStateCache, ...]:
        return tuple(
            _host_state_cache(row)
            for row in self.connection.execute(
                "SELECT * FROM host_state_cache ORDER BY host_id, remote_name"
            )
        )


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "MAX_BUSY_TIMEOUT_MS",
    "ConflictError",
    "Registry",
    "RegistryClosed",
    "StorageError",
    "TransitionClaim",
    "WorkspaceResult",
    "connect_database",
    "now_ms",
]
