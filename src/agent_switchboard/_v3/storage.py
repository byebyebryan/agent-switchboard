"""Secure SQLite connection gate for the private Phase 6 registry."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .domain import (
    LAUNCH_EDGES,
    PLACEMENT_EDGES,
    SURFACE_EDGES,
    VIEW_EDGES,
    WORK_CONTEXT_EDGES,
    ActivationState,
    Activity,
    ActivityReason,
    AgentCapability,
    BackgroundState,
    Checkout,
    CheckoutId,
    ClaimState,
    CloseReason,
    CreatedBy,
    FailureRecord,
    Frame,
    FrameId,
    FrameLifecycleState,
    FramePlacement,
    FrameRole,
    FrameSession,
    GenerationId,
    HostId,
    LaunchAction,
    LaunchId,
    LaunchIntent,
    LaunchState,
    PlacementId,
    PlacementState,
    Project,
    ProjectId,
    ProjectRepository,
    ProviderId,
    ProviderSession,
    Repository,
    RepositoryId,
    RequestId,
    Resumability,
    RuntimePresence,
    SessionHandoff,
    SessionKey,
    Surface,
    SurfaceId,
    SurfaceState,
    TmuxServer,
    TmuxServerId,
    UserView,
    ViewId,
    ViewMode,
    ViewState,
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
        connection.execute("PRAGMA journal_mode = WAL")
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
                    "AND state IN ('prepared', 'executing', 'awaiting_claim')",
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
                    JOIN surfaces AS surface
                      ON surface.surface_id = placement.surface_id
                    WHERE placement.view_id = ? AND placement.frame_id = ?
                      AND placement.surface_id = ? AND placement.state = 'active'
                      AND placement.generation = ? AND view.host_id = ?
                      AND view.state != 'retired' AND surface.launch_id = ?
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


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "MAX_BUSY_TIMEOUT_MS",
    "ConflictError",
    "Registry",
    "RegistryClosed",
    "StorageError",
    "WorkspaceResult",
    "connect_database",
    "now_ms",
]
