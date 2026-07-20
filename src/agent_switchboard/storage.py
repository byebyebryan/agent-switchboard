"""SQLite-backed materialized registry for Switchboard.

This module accepts and returns primitive mappings while reusing canonical
domain identifiers and the validated snapshot protocol.  It remains independent
of provider adapters and frontend code so short-lived writers stay lightweight.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .domain import (
    Activity,
    ActivityReason,
    Attachment,
    Checkout,
    CheckoutId,
    CheckoutKind,
    HandoffId,
    HostId,
    LaunchId,
    NormalizedRuntimeObservation,
    ProjectId,
    ProjectRepository,
    ProviderId,
    RepositoryId,
    RuntimePresence,
    SessionKey,
    SurfaceId,
    TaskId,
    UUIDId,
    ValidationError,
    match_checkout,
    normalize_handoff_text,
)
from .domain import (
    handoff_content_hash as _domain_handoff_content_hash,
)
from .migrations import CURRENT_SCHEMA_VERSION, migrate
from .protocol import (
    PROTOCOL_VERSION as SNAPSHOT_PROTOCOL_VERSION,
)
from .protocol import (
    SCHEMA_VERSION as SNAPSHOT_SCHEMA_VERSION,
)
from .protocol import (
    SnapshotEnvelope,
)
from .state import HOOK_SOURCE_PRIORITY, HookEvent, HookTransition, hook_transition

DEFAULT_BUSY_TIMEOUT_MS: Final = 5_000
MAX_BUSY_TIMEOUT_MS: Final = 30_000
DEFAULT_EVENT_LIMIT: Final = 1_000
MAX_EVENT_LIMIT: Final = 100_000
DEFAULT_SNAPSHOT_TASK_LIMIT: Final = 1_000
DEFAULT_SNAPSHOT_SESSION_LIMIT: Final = 1_000
DEFAULT_SNAPSHOT_RUNTIME_LIMIT: Final = 10_000
DEFAULT_LIVENESS_OBSERVATION_LIMIT: Final = 16
DEFAULT_HANDOFF_LIMIT: Final = 20
MAX_HANDOFF_LIMIT: Final = 100
DEFAULT_AGENT_CONTEXT_SESSION_LIMIT: Final = 20
DEFAULT_AGENT_PROJECT_SESSION_LIMIT: Final = 50
DEFAULT_AGENT_SEARCH_LIMIT: Final = 20
MAX_AGENT_SEARCH_SESSION_CANDIDATES: Final = 512
MAX_AGENT_SEARCH_HANDOFF_CANDIDATES: Final = 2_048
_SQLITE_MAX_INTEGER: Final = 2**63 - 1
_MAX_EVIDENCE_PRIORITY: Final = 1_000_000

_ACTIVE_LAUNCH_STATES: Final = (
    "reserved",
    "surface_ready",
    "waiting_for_client",
    "provider_started",
)
_LEASED_LAUNCH_STATES: Final = (*_ACTIVE_LAUNCH_STATES, "manager_ready")
_TERMINAL_LAUNCH_STATES: Final = ("bound", "failed", "expired")
_SURFACE_REQUIRED_LAUNCH_STATES: Final = (
    "surface_ready",
    "waiting_for_client",
    "provider_started",
    "bound",
    "manager_ready",
)
_LAUNCH_REQUEST_FIELDS: Final = (
    "host_id",
    "provider",
    "action",
    "project_id",
    "task_id",
    "checkout_id",
    "cwd",
    "source_handoff_id",
    "target_session_key",
    "transport",
)
_SESSION_FIELDS: Final = {
    "project_id",
    "task_id",
    "checkout_id",
    "name",
    "provider_name",
    "name_source",
    "name_actor",
    "purpose",
    "cwd",
    "created_at",
    "provider_updated_at",
    "last_activity_at",
    "first_observed_at",
    "last_observed_at",
    "runtime_presence",
    "resumability",
    "activity",
    "activity_reason",
    "attachment",
    "runtime_pid",
    "provider_runtime_id",
    "tmux_session",
    "tmux_window",
    "tmux_pane",
    "runtime_observed_at",
    "metadata_source",
    "state_confidence",
    "state_observed_at",
    "latest_handoff_id",
    "wrapped_at",
    "continued_from_handoff_id",
    "pinned",
}
_PRIVATE_SESSION_FIELDS: Final = {
    "runtime_source_priority",
    "runtime_order_ns",
    "resumability_source_priority",
    "resumability_order_ns",
    "activity_source_priority",
    "activity_order_ns",
    "attachment_source_priority",
    "attachment_order_ns",
    "last_hook_turn_id",
    "last_hook_entry_ns",
    "last_hook_kind_priority",
    "runtime_process_birth_id",
    "tmux_socket",
}
_SESSION_RUNTIME_FIELDS: Final = {
    "runtime_presence",
    "attachment",
    "runtime_pid",
    "provider_runtime_id",
    "tmux_session",
    "tmux_window",
    "tmux_pane",
}
_SESSION_STATE_FIELDS: Final = {
    "resumability",
    "activity",
    "activity_reason",
    "state_confidence",
}
_PROVIDER_SESSION_FIELDS: Final = {
    "session_key",
    "host_id",
    "provider",
    "provider_session_id",
    "name",
    "cwd",
    "created_at",
    "provider_updated_at",
    "last_activity_at",
    "last_observed_at",
    "metadata_source",
}
_REQUIRED_PROVIDER_SESSION_FIELDS: Final = _PROVIDER_SESSION_FIELDS
_SNAPSHOT_RUNTIME_TAIL_QUERY: Final = """
    SELECT * FROM runtime_observations
    INDEXED BY runtime_observations_host_recent
    WHERE host_id = ?
    ORDER BY observed_at DESC, observation_id DESC
    LIMIT ?
"""
_RUNTIME_OBSERVATION_HASH_FIELDS: Final = (
    "observation_key",
    "host_id",
    "provider",
    "session_key",
    "launch_id",
    "source",
    "source_priority",
    "runtime_presence",
    "resumability",
    "activity",
    "activity_reason",
    "attachment",
    "pid",
    "provider_runtime_id",
    "tmux_session",
    "tmux_window",
    "tmux_pane",
    "observed_at",
)
_PRIVATE_RUNTIME_OBSERVATION_FIELDS: Final = (
    "entry_ns",
    "process_birth_id",
    "tmux_socket",
)
_EVENT_HASH_FIELDS: Final = (
    "idempotency_key",
    "host_id",
    "provider",
    "session_key",
    "launch_id",
    "surface_id",
    "event_kind",
    "provider_turn_id",
    "source_priority",
    "kind_priority",
    "diagnostic_code",
    "diagnostic_detail",
)
_HOOK_EVENT_HASH_FIELDS: Final = (
    *_EVENT_HASH_FIELDS,
    "provider_session_id",
    "cwd",
    "pid",
    "process_birth_id",
    "tmux_socket",
    "tmux_pane",
)


class StorageError(RuntimeError):
    """Base error for registry operations."""


class IdentityConflict(StorageError):
    """A stable ID was reused for different immutable identity fields."""


class RequestConflict(StorageError):
    """A launch request ID was reused for a different normalized request."""


class ContinuationError(StorageError):
    """A local handoff reference cannot safely seed a new launch."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TaskConflict(StorageError):
    """A task lifecycle or ownership invariant blocks an operation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RegistryClosed(StorageError):
    """An operation was attempted after the registry was closed."""


@dataclass(frozen=True, slots=True)
class ReservationResult:
    """Outcome of an atomic launch reservation."""

    kind: str
    launch: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LaunchBindingResult:
    """Outcome of atomically binding one provider session to a launch."""

    kind: str
    launch: dict[str, Any]
    session: dict[str, Any]
    surface: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class LaunchSurfaceResult:
    """Outcome of atomically publishing one launch-owned waiting surface."""

    launch: dict[str, Any]
    surface: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HookIngestionResult:
    """Outcome of one atomic, privacy-safe lifecycle event write."""

    kind: str
    event: dict[str, Any]
    session: dict[str, Any]
    runtime: dict[str, Any] | None
    launch: dict[str, Any] | None
    surface: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class RuntimeObservationApplyResult:
    """Outcome of one atomic batch of normalized live observations."""

    applied_count: int
    stale_count: int
    observations: tuple[dict[str, Any], ...]
    sessions: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _PreparedHookEvent:
    host_id: str
    host_display_name: str
    provider: str
    provider_session_id: str
    session_key: str
    cwd: str
    event_kind: HookEvent
    transition: HookTransition
    provider_turn_id: str | None
    idempotency_key: str
    source_priority: int
    entry_ns: int
    observed_at: int
    received_at: int
    launch_id: str | None
    surface_id: str | None
    pid: int | None
    process_birth_id: str | None
    tmux_socket: str | None
    tmux_pane: str | None
    payload_hash: str


@dataclass(frozen=True, slots=True)
class _HookLaunchContext:
    launch: sqlite3.Row | None
    surface: sqlite3.Row | None
    mismatch: bool


@dataclass(frozen=True, slots=True)
class ProviderSessionReconciliationResult:
    """Outcome of one complete host/provider discovery observation.

    ``updated_count`` counts previously known sessions present in the complete
    scan, not rows whose stored columns happened to change. ``missing_count``
    counts retained rows absent from it, including rows that were already
    missing. This makes the result describe the reconciled input set while
    repeated calls remain safe and deterministic.
    """

    observed_at: int
    inserted_count: int
    updated_count: int
    missing_count: int
    sessions: tuple[dict[str, Any], ...]

    @property
    def observed_count(self) -> int:
        return self.inserted_count + self.updated_count

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        """Alias emphasizing that the tuple is the reconciled record set."""

        return self.sessions


@dataclass(frozen=True, slots=True)
class HostSnapshotRows:
    """Coherent primitive rows read for one host in one transaction."""

    host: dict[str, Any]
    projects: tuple[dict[str, Any], ...]
    project_repositories: tuple[dict[str, Any], ...]
    repositories: tuple[dict[str, Any], ...]
    checkouts: tuple[dict[str, Any], ...]
    tasks: tuple[dict[str, Any], ...]
    retained_task_count: int
    sessions: tuple[dict[str, Any], ...]
    retained_session_count: int
    runtimes: tuple[dict[str, Any], ...]
    surfaces: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class SessionDetailRows:
    """One coherent local session and a bounded newest-first handoff page."""

    session: dict[str, Any]
    handoffs: tuple[dict[str, Any], ...]
    handoffs_truncated: bool


@dataclass(frozen=True, slots=True)
class SessionCurationResult:
    """Committed curation state and its optional immutable handoff."""

    session: dict[str, Any]
    handoff: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ProjectContextRows:
    """Coherent current-project metadata and newest explicit handoffs."""

    current_session: dict[str, Any]
    sessions: tuple[dict[str, Any], ...]
    latest_handoffs: tuple[dict[str, Any], ...]
    retained_session_count: int


@dataclass(frozen=True, slots=True)
class ProjectSearchRows:
    """Bounded same-project retained-state search results."""

    current_session: dict[str, Any]
    query: str
    results: tuple[dict[str, Any], ...]
    results_truncated: bool


@dataclass(frozen=True, slots=True)
class ContinuationSource:
    """A retained local source session and one exact immutable handoff."""

    session: dict[str, Any]
    handoff: dict[str, Any]
    from_session: bool


def now_ms() -> int:
    """Return the current Unix time in integer milliseconds."""

    return int(time.time() * 1_000)


def _nonnegative_integer(value: Any, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _SQLITE_MAX_INTEGER
    ):
        raise StorageError(f"{field} must be a non-negative SQLite integer")
    return value


def _evidence_priority(value: Any, field: str) -> int:
    priority = _nonnegative_integer(value, field)
    if priority > _MAX_EVIDENCE_PRIORITY:
        raise StorageError(f"{field} must be no greater than {_MAX_EVIDENCE_PRIORITY}")
    return priority


def _reject_invalid_unicode(value: str, field: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise StorageError(f"{field} contains an invalid Unicode scalar")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_hash(value: str, field: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise StorageError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _reject_controls(
    value: str | None,
    field: str,
    *,
    allow_multiline: bool = False,
) -> None:
    """Reject Unicode controls, except newline/tab in explicit diagnostic text."""

    if value is None:
        return
    if not isinstance(value, str):
        raise StorageError(f"{field} must be a string")
    allowed = "\n\t" if allow_multiline else ""
    if any(
        unicodedata.category(character) == "Cc" and character not in allowed
        for character in value
    ):
        raise StorageError(f"{field} contains terminal control characters")


def _reject_mapping_controls(value: Mapping[str, Any], fields: Sequence[str]) -> None:
    for field in fields:
        if field in value:
            _reject_controls(value[field], field)


def _normalize_curation_text(value: str, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise StorageError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise StorageError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise StorageError(f"{field} exceeds {maximum} characters")
    _reject_controls(normalized, field)
    _reject_invalid_unicode(normalized, field)
    return normalized


def _normalize_task_purpose(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StorageError("task purpose must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if len(normalized) > 4096:
        raise StorageError("task purpose exceeds 4096 characters")
    _reject_controls(normalized, "task purpose", allow_multiline=True)
    _reject_invalid_unicode(normalized, "task purpose")
    return normalized or None


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _database_path(path: str | os.PathLike[str]) -> str:
    return os.fspath(path)


def _canonical_host_id(value: Any) -> str:
    try:
        canonical = str(HostId(str(value)))
    except ValidationError as error:
        raise StorageError("host_id must be a non-nil UUID") from error
    if value != canonical:
        raise StorageError("host_id must use canonical lowercase UUID spelling")
    return canonical


def _canonical_uuid_id(value: Any, value_type: type[UUIDId], field: str) -> str:
    try:
        canonical = str(value_type(str(value)))
    except ValidationError as error:
        raise StorageError(f"{field} must be a non-nil UUID") from error
    if value != canonical:
        raise StorageError(f"{field} must use canonical lowercase UUID spelling")
    return canonical


def _canonical_plain_uuid(value: Any, field: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise StorageError(f"{field} must be a non-nil UUID") from error
    if parsed.int == 0:
        raise StorageError(f"{field} must be a non-nil UUID")
    canonical = str(parsed)
    if value != canonical:
        raise StorageError(f"{field} must use canonical lowercase UUID spelling")
    return canonical


def _canonical_provider(value: Any) -> str:
    try:
        provider = ProviderId(str(value))
    except (TypeError, ValueError) as error:
        raise StorageError(f"unknown provider: {value!r}") from error
    return provider.value


def _canonical_session_key(value: Any) -> SessionKey:
    try:
        key = SessionKey.parse(str(value))
    except ValidationError as error:
        raise StorageError(
            "session_key must be a canonical domain session key"
        ) from error
    if value != str(key):
        raise StorageError("session_key must use canonical lowercase UUID spelling")
    return key


def _source_allows(
    session: Mapping[str, Any], axis: str, priority: int, order_ns: int
) -> bool:
    """Merge evidence by causal order, using source priority only on a tie."""

    stored_priority = int(session[f"{axis}_source_priority"])
    stored_order = int(session[f"{axis}_order_ns"])
    return order_ns > stored_order or (
        order_ns == stored_order and priority >= stored_priority
    )


def _prepare_hook_event(
    event: Mapping[str, Any], host_display_name: str
) -> _PreparedHookEvent:
    required = {
        "idempotency_key",
        "host_id",
        "provider",
        "provider_session_id",
        "session_key",
        "cwd",
        "event_kind",
        "source_priority",
        "kind_priority",
        "entry_ns",
        "observed_at",
        "received_at",
    } - set(event)
    if required:
        raise StorageError(f"missing normalized hook fields: {sorted(required)}")

    host_id = _canonical_host_id(event["host_id"])
    provider = _canonical_provider(event["provider"])
    provider_session_id = _canonical_plain_uuid(
        event["provider_session_id"], "provider_session_id"
    )
    session_key = str(_canonical_session_key(event["session_key"]))
    if session_key != f"{host_id}:{provider}:{provider_session_id}":
        raise IdentityConflict("hook session identity is inconsistent")

    try:
        event_kind = HookEvent(event["event_kind"])
    except (TypeError, ValueError) as error:
        raise StorageError("unsupported normalized hook event") from error
    allowed_events = {
        "codex": {
            HookEvent.SESSION_START,
            HookEvent.USER_PROMPT_SUBMIT,
            HookEvent.PERMISSION_REQUEST,
            HookEvent.POST_TOOL_USE,
            HookEvent.STOP,
        },
        "claude": set(HookEvent),
    }
    if event_kind not in allowed_events[provider]:
        raise StorageError("unsupported normalized hook event")
    transition = hook_transition(event_kind)
    kind_priority = _evidence_priority(event["kind_priority"], "kind_priority")
    if kind_priority != transition.kind_priority:
        raise StorageError("hook kind priority does not match the lifecycle contract")
    source_priority = _evidence_priority(event["source_priority"], "source_priority")
    if source_priority != HOOK_SOURCE_PRIORITY:
        raise StorageError("hook source priority does not match the lifecycle contract")

    cwd = event["cwd"]
    if not isinstance(cwd, str) or not cwd or len(cwd) > 4096:
        raise StorageError("hook cwd must be a bounded string")
    if not Path(cwd).is_absolute():
        raise StorageError("hook cwd must be absolute")
    _reject_controls(cwd, "cwd")
    _reject_invalid_unicode(cwd, "cwd")
    if not isinstance(host_display_name, str) or not 1 <= len(host_display_name) <= 256:
        raise StorageError("host display name must be a bounded string")
    _reject_controls(host_display_name, "host display name")
    _reject_invalid_unicode(host_display_name, "host display name")

    idempotency_key = event["idempotency_key"]
    provider_turn_id = event.get("provider_turn_id")
    pid = event.get("pid")
    process_birth_id = event.get("process_birth_id")
    tmux_socket = event.get("tmux_socket")
    tmux_pane = event.get("tmux_pane")
    for field, value in (
        ("idempotency_key", idempotency_key),
        ("provider_turn_id", provider_turn_id),
        ("process_birth_id", process_birth_id),
        ("tmux_socket", tmux_socket),
        ("tmux_pane", tmux_pane),
    ):
        _reject_controls(value, field)
        if isinstance(value, str):
            _reject_invalid_unicode(value, field)
    if (
        not isinstance(idempotency_key, str)
        or len(idempotency_key) != 69
        or not idempotency_key.startswith("hook:")
        or any(character not in "0123456789abcdef" for character in idempotency_key[5:])
    ):
        raise StorageError("hook idempotency key must contain a lowercase digest")
    if event_kind is HookEvent.SESSION_START:
        if provider_turn_id is not None:
            raise StorageError("SessionStart must not carry a provider turn ID")
    elif event_kind is HookEvent.SESSION_END:
        if provider_turn_id is not None and (
            not isinstance(provider_turn_id, str)
            or not 1 <= len(provider_turn_id) <= 256
        ):
            raise StorageError(
                "SessionEnd provider turn ID must be null or a bounded string"
            )
    elif not isinstance(provider_turn_id, str) or not 1 <= len(provider_turn_id) <= 256:
        raise StorageError("turn hook events require a bounded provider turn ID")
    if process_birth_id is not None and (
        not isinstance(process_birth_id, str)
        or len(process_birth_id) != 64
        or any(character not in "0123456789abcdef" for character in process_birth_id)
    ):
        raise StorageError("process birth ID must be an opaque lowercase digest")
    if pid is not None and (
        isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
    ):
        raise StorageError("hook process PID must be a positive integer")
    if pid is not None and process_birth_id is None:
        raise StorageError("hook process PID requires a process birth ID")
    if (tmux_socket is None) != (tmux_pane is None):
        raise StorageError("tmux socket and pane must be supplied together")
    if tmux_socket is not None and (
        not isinstance(tmux_socket, str)
        or not 1 <= len(tmux_socket) <= 4096
        or not Path(tmux_socket).is_absolute()
    ):
        raise StorageError("tmux socket must be a bounded absolute path")
    if tmux_pane is not None and (
        not isinstance(tmux_pane, str)
        or len(tmux_pane) > 256
        or not tmux_pane.startswith("%")
        or not tmux_pane[1:].isdigit()
    ):
        raise StorageError("tmux pane must use canonical tmux pane syntax")

    launch_id = event.get("launch_id")
    surface_id = event.get("surface_id")
    if (launch_id is None) != (surface_id is None):
        raise StorageError("hook launch and surface IDs must be supplied together")
    if launch_id is not None:
        launch_id = _canonical_uuid_id(launch_id, LaunchId, "launch_id")
        surface_id = _canonical_uuid_id(surface_id, SurfaceId, "surface_id")

    entry_ns = _nonnegative_integer(event["entry_ns"], "entry_ns")
    observed_at = _nonnegative_integer(event["observed_at"], "observed_at")
    received_at = _nonnegative_integer(event["received_at"], "received_at")
    payload_hash = _sha256_text(
        _canonical_json({field: event.get(field) for field in _HOOK_EVENT_HASH_FIELDS})
    )
    supplied_hash = event.get("payload_hash")
    if supplied_hash is not None and (
        _require_hash(str(supplied_hash), "payload_hash") != payload_hash
    ):
        raise StorageError("payload_hash does not match the normalized hook event")

    return _PreparedHookEvent(
        host_id=host_id,
        host_display_name=host_display_name,
        provider=provider,
        provider_session_id=provider_session_id,
        session_key=session_key,
        cwd=cwd,
        event_kind=event_kind,
        transition=transition,
        provider_turn_id=provider_turn_id,
        idempotency_key=idempotency_key,
        source_priority=source_priority,
        entry_ns=entry_ns,
        observed_at=observed_at,
        received_at=received_at,
        launch_id=launch_id,
        surface_id=surface_id,
        pid=pid,
        process_birth_id=process_birth_id,
        tmux_socket=tmux_socket,
        tmux_pane=tmux_pane,
        payload_hash=payload_hash,
    )


def _secure_database_file(path: Path) -> None:
    """Create or tighten the main database before SQLite creates sidecars."""

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


def _secure_sqlite_sidecars(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.chmod(0o600)
        except FileNotFoundError:
            continue


def connect_database(
    path: str | os.PathLike[str],
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    target_version: int = CURRENT_SCHEMA_VERSION,
) -> sqlite3.Connection:
    """Open, configure, and migrate a Switchboard database.

    File databases use WAL mode and private main, WAL, and shared-memory files.
    ``:memory:`` remains supported for focused tests.
    """

    if not 1 <= busy_timeout_ms <= MAX_BUSY_TIMEOUT_MS:
        raise ValueError(f"busy_timeout_ms must be between 1 and {MAX_BUSY_TIMEOUT_MS}")

    database = _database_path(path)
    if database.startswith("file:"):
        raise StorageError("SQLite URI database paths are not supported")
    file_database = database != ":memory:"
    if file_database:
        database_path = Path(database)
        _secure_database_file(database_path)

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
        migrate(connection, target_version=target_version)
        if file_database:
            _secure_sqlite_sidecars(Path(database))
        return connection
    except BaseException:
        connection.close()
        raise


def launch_request_fingerprint(request: Mapping[str, Any]) -> str:
    """Hash only the normalized launch request, never presentation context."""

    unknown = set(request) - set(_LAUNCH_REQUEST_FIELDS)
    if unknown:
        raise StorageError(
            f"unknown normalized launch request fields: {sorted(unknown)}"
        )
    missing = {"host_id", "provider", "action", "transport"} - set(request)
    if missing:
        raise StorageError(
            f"missing normalized launch request fields: {sorted(missing)}"
        )
    normalized = {field: request.get(field) for field in _LAUNCH_REQUEST_FIELDS}
    return _sha256_text(_canonical_json(normalized))


def handoff_content_hash(summary: str, next_action: str) -> str:
    """Return the domain-canonical hash used for local and imported handoffs."""

    return _domain_handoff_content_hash(summary, next_action)


class Registry:
    """One configured SQLite registry connection.

    The object is intentionally synchronous: hooks perform one short local
    transaction and exit.  Use one Registry per thread/process.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        target_version: int = CURRENT_SCHEMA_VERSION,
    ) -> None:
        self._connection: sqlite3.Connection | None = connect_database(
            path,
            busy_timeout_ms=busy_timeout_ms,
            target_version=target_version,
        )

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the configured connection for composed core transactions."""

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
        """Run an explicit transaction and reliably roll it back on failure."""

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

    def metadata(self) -> dict[str, str]:
        rows = self.connection.execute(
            "SELECT key, value FROM registry_metadata ORDER BY key"
        ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def upsert_host(
        self,
        host_id: str,
        display_name: str,
        *,
        is_local: bool = False,
        observed_at: int | None = None,
    ) -> dict[str, Any]:
        host_id = _canonical_host_id(host_id)
        _reject_controls(display_name, "display_name")
        timestamp = now_ms() if observed_at is None else observed_at
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO hosts(
                    host_id, display_name, is_local, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    is_local = excluded.is_local,
                    updated_at = excluded.updated_at
                """,
                (host_id, display_name, int(is_local), timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM hosts WHERE host_id = ?", (host_id,)
            ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return result

    def get_host(self, host_id: str) -> dict[str, Any] | None:
        return _row_dict(
            self.connection.execute(
                "SELECT * FROM hosts WHERE host_id = ?", (host_id,)
            ).fetchone()
        )

    def materialize_projects(
        self,
        host_id: str,
        projects: Sequence[Mapping[str, Any]],
        *,
        observed_at: int | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically replace the local host's configured project declarations.

        Missing rows are marked undeclared; no project, checkout, session, or
        handoff history is deleted.  Stable checkout IDs cannot move between a
        project or host, while their configured paths and display fields can
        change authoritatively. Remote catalogs remain cached snapshots and are
        merged at the read layer rather than materialized here.
        """

        host_id = _canonical_host_id(host_id)
        timestamp = now_ms() if observed_at is None else observed_at
        normalized = self._normalize_project_catalog(projects)
        catalog_hash = _sha256_text(_canonical_json(normalized))

        with self.transaction(immediate=True) as connection:
            host = connection.execute(
                "SELECT * FROM hosts WHERE host_id = ?", (host_id,)
            ).fetchone()
            if host is None:
                raise StorageError(f"unknown host: {host_id}")
            if not bool(host["is_local"]):
                raise StorageError(
                    "project catalogs can only be materialized for the local host"
                )

            connection.execute(
                "UPDATE projects SET declared = 0, updated_at = ? WHERE declared = 1",
                (timestamp,),
            )
            connection.execute(
                "UPDATE repositories SET declared = 0, updated_at = ? "
                "WHERE declared = 1",
                (timestamp,),
            )
            connection.execute(
                """
                UPDATE checkouts
                SET declared = 0, is_default = 0, updated_at = ?
                WHERE host_id = ? AND declared = 1
                """,
                (timestamp, host_id),
            )

            for project in normalized:
                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, name, aliases_json, default_provider,
                        default_transport, declared, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        name = excluded.name,
                        aliases_json = excluded.aliases_json,
                        default_provider = excluded.default_provider,
                        default_transport = excluded.default_transport,
                        declared = 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        project["project_id"],
                        project["name"],
                        _canonical_json(project["aliases"]),
                        project["default_provider"],
                        project["default_transport"],
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    "DELETE FROM project_repositories WHERE project_id = ?",
                    (project["project_id"],),
                )
                for repository in project["repositories"]:
                    existing_repository = connection.execute(
                        """
                        SELECT kind, kind_provisional
                        FROM repositories WHERE repository_id = ?
                        """,
                        (repository["repository_id"],),
                    ).fetchone()
                    if (
                        existing_repository is not None
                        and existing_repository["kind"] != repository["kind"]
                        and not bool(existing_repository["kind_provisional"])
                    ):
                        raise IdentityConflict(
                            f"repository ID {repository['repository_id']!r} "
                            "changed kind"
                        )
                    connection.execute(
                        """
                        INSERT INTO repositories(
                            repository_id, name, kind, context_sources_json,
                            declared, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 1, ?, ?)
                        ON CONFLICT(repository_id) DO UPDATE SET
                            name = excluded.name,
                            kind = excluded.kind,
                            kind_provisional = 0,
                            context_sources_json = excluded.context_sources_json,
                            declared = 1,
                            updated_at = excluded.updated_at
                        """,
                        (
                            repository["repository_id"],
                            repository["name"],
                            repository["kind"],
                            _canonical_json(repository["context_sources"]),
                            timestamp,
                            timestamp,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO project_repositories(
                            project_id, repository_id, is_primary,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            project["project_id"],
                            repository["repository_id"],
                            int(repository["is_primary"]),
                            timestamp,
                            timestamp,
                        ),
                    )
                    for checkout in repository["checkouts"]:
                        existing = connection.execute(
                            """
                        SELECT repository_id, host_id
                        FROM checkouts WHERE checkout_id = ?
                        """,
                            (checkout["checkout_id"],),
                        ).fetchone()
                        if existing is not None and (
                            existing["repository_id"] != repository["repository_id"]
                            or existing["host_id"] != host_id
                        ):
                            raise IdentityConflict(
                                f"checkout ID {checkout['checkout_id']!r} already "
                                "belongs to another repository or host"
                            )
                        try:
                            connection.execute(
                                """
                                INSERT INTO checkouts(
                                    checkout_id, repository_id, host_id, path, kind,
                                    display_name, provider_override,
                                    transport_override, is_default, declared, present,
                                    last_observed_at, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                                ON CONFLICT(checkout_id) DO UPDATE SET
                                    path = excluded.path,
                                    kind = excluded.kind,
                                    display_name = excluded.display_name,
                                    provider_override = excluded.provider_override,
                                    transport_override = excluded.transport_override,
                                    is_default = excluded.is_default,
                                    declared = 1,
                                    present = excluded.present,
                                    last_observed_at = excluded.last_observed_at,
                                    updated_at = excluded.updated_at
                                """,
                                (
                                    checkout["checkout_id"],
                                    repository["repository_id"],
                                    host_id,
                                    checkout["path"],
                                    checkout["kind"],
                                    checkout["display_name"],
                                    checkout["provider_override"],
                                    checkout["transport_override"],
                                    int(checkout["is_default"]),
                                    int(checkout["present"]),
                                    checkout["last_observed_at"],
                                    timestamp,
                                    timestamp,
                                ),
                            )
                        except sqlite3.IntegrityError as error:
                            raise IdentityConflict(
                                "checkout identity/path conflict for "
                                f"{checkout['checkout_id']!r}"
                            ) from error

            connection.execute(
                """
                INSERT INTO registry_metadata(key, value, updated_at)
                VALUES ('project_catalog_hash', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (catalog_hash, timestamp),
            )

        return self.list_projects(include_undeclared=True)

    @staticmethod
    def _normalize_project_catalog(
        projects: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        project_ids: set[str] = set()
        repository_declarations: dict[str, dict[str, Any]] = {}
        checkout_ids: set[str] = set()
        paths: set[str] = set()

        for raw_project in projects:
            try:
                project_id = _canonical_uuid_id(
                    raw_project["project_id"], ProjectId, "project_id"
                )
                name = str(raw_project["name"])
            except KeyError as error:
                raise StorageError(f"missing project field: {error.args[0]}") from error
            if project_id in project_ids:
                raise IdentityConflict(f"duplicate project ID: {project_id}")
            _reject_controls(name, "project name")
            project_ids.add(project_id)

            raw_aliases = raw_project.get("aliases", ())
            raw_repositories = raw_project.get("repositories")
            if raw_repositories is None:
                # Private migration input used by copied v1 registries and
                # pre-v2 core callers. Public config/Snapshot parsing remains
                # strict v2 and never accepts this flattened shape.
                raw_repositories = (
                    {
                        "repository_id": project_id,
                        "name": name,
                        "kind": "git",
                        "is_primary": True,
                        "context_sources": raw_project.get("context_sources", ()),
                        "checkouts": raw_project.get("checkouts", ()),
                    },
                )
            if isinstance(raw_aliases, (str, bytes)):
                raise StorageError("aliases must be a sequence")
            if isinstance(raw_repositories, (str, bytes)):
                raise StorageError("repositories must be a sequence")

            aliases = sorted({str(alias) for alias in raw_aliases})
            for alias in aliases:
                _reject_controls(alias, "project alias")
            repositories: list[dict[str, Any]] = []
            primary_count = 0
            for raw_repository in raw_repositories:
                try:
                    repository_id = _canonical_uuid_id(
                        raw_repository["repository_id"],
                        RepositoryId,
                        "repository_id",
                    )
                    repository_name = str(raw_repository["name"])
                except KeyError as error:
                    raise StorageError(
                        f"missing project repository field: {error.args[0]}"
                    ) from error
                _reject_controls(repository_name, "repository name")
                kind = str(raw_repository.get("kind", "git"))
                if kind not in {"git", "directory"}:
                    raise StorageError("repository kind must be git or directory")
                raw_context = raw_repository.get("context_sources", ())
                raw_checkouts = raw_repository.get("checkouts", ())
                if isinstance(raw_context, (str, bytes)):
                    raise StorageError("context_sources must be a sequence")
                if isinstance(raw_checkouts, (str, bytes)):
                    raise StorageError("checkouts must be a sequence")
                context_sources = [str(source) for source in raw_context]
                for source in context_sources:
                    _reject_controls(source, "context source")
                is_primary = bool(raw_repository.get("is_primary", False))
                primary_count += int(is_primary)
                checkouts: list[dict[str, Any]] = []
                default_count = 0
                for raw_checkout in raw_checkouts:
                    try:
                        checkout_id = _canonical_uuid_id(
                            raw_checkout["checkout_id"], CheckoutId, "checkout_id"
                        )
                        path = str(raw_checkout["path"])
                    except KeyError as error:
                        raise StorageError(
                            f"missing repository checkout field: {error.args[0]}"
                        ) from error
                    if checkout_id in checkout_ids:
                        raise IdentityConflict(f"duplicate checkout ID: {checkout_id}")
                    if not Path(path).is_absolute():
                        raise StorageError(
                            f"configured checkout path must be absolute: {path!r}"
                        )
                    _reject_controls(path, "checkout path")
                    _reject_controls(
                        raw_checkout.get("display_name"), "checkout display_name"
                    )
                    if path in paths:
                        raise IdentityConflict(
                            f"duplicate configured checkout path: {path}"
                        )
                    checkout_kind = str(
                        raw_checkout.get(
                            "kind", "directory" if kind == "directory" else "main"
                        )
                    )
                    if checkout_kind not in {"main", "worktree", "directory"}:
                        raise StorageError("invalid checkout kind")
                    checkout_ids.add(checkout_id)
                    paths.add(path)
                    is_default = bool(raw_checkout.get("is_default", False))
                    default_count += int(is_default)
                    checkouts.append(
                        {
                            "checkout_id": checkout_id,
                            "path": path,
                            "kind": checkout_kind,
                            "display_name": raw_checkout.get("display_name"),
                            "provider_override": raw_checkout.get("provider_override"),
                            "transport_override": raw_checkout.get(
                                "transport_override"
                            ),
                            "is_default": is_default,
                            "present": bool(raw_checkout.get("present", True)),
                            "last_observed_at": raw_checkout.get("last_observed_at"),
                        }
                    )
                if default_count > 1:
                    raise StorageError(
                        f"repository {repository_id!r} has more than one default "
                        "checkout on this host"
                    )
                declaration = {
                    "repository_id": repository_id,
                    "name": repository_name,
                    "kind": kind,
                    "context_sources": context_sources,
                }
                prior = repository_declarations.get(repository_id)
                if prior is not None and prior != declaration:
                    raise IdentityConflict(
                        f"repository ID {repository_id!r} has conflicting declarations"
                    )
                repository_declarations[repository_id] = declaration
                repositories.append(
                    {
                        **declaration,
                        "is_primary": is_primary,
                        "checkouts": checkouts,
                    }
                )
            if repositories and primary_count != 1:
                raise StorageError(
                    f"project {project_id!r} must have exactly one primary repository"
                )
            normalized.append(
                {
                    "project_id": project_id,
                    "name": name,
                    "aliases": aliases,
                    "default_provider": raw_project.get("default_provider"),
                    "default_transport": raw_project.get("default_transport"),
                    "repositories": repositories,
                }
            )

        return sorted(normalized, key=lambda project: project["project_id"])

    def list_projects(
        self, *, include_undeclared: bool = False
    ) -> list[dict[str, Any]]:
        where = "" if include_undeclared else "WHERE project.declared = 1"
        rows = self.connection.execute(
            f"""
            SELECT project.*
            FROM projects AS project
            {where}
            ORDER BY project.name COLLATE NOCASE, project.project_id
            """
        ).fetchall()
        projects: list[dict[str, Any]] = []
        for row in rows:
            project = dict(row)
            project["aliases"] = json.loads(project.pop("aliases_json"))
            repository_rows = self.connection.execute(
                """
                SELECT repository.*, membership.is_primary
                FROM project_repositories AS membership
                JOIN repositories AS repository
                  ON repository.repository_id = membership.repository_id
                WHERE membership.project_id = ?
                ORDER BY membership.is_primary DESC, repository.repository_id
                """,
                (project["project_id"],),
            ).fetchall()
            project["repositories"] = []
            for repository_row in repository_rows:
                repository = dict(repository_row)
                repository["context_sources"] = json.loads(
                    repository.pop("context_sources_json")
                )
                repository["checkouts"] = [
                    dict(checkout)
                    for checkout in self.connection.execute(
                        """
                    SELECT * FROM checkouts
                    WHERE repository_id = ?
                    ORDER BY host_id, is_default DESC, path
                    """,
                        (repository["repository_id"],),
                    ).fetchall()
                ]
                project["repositories"].append(repository)
            projects.append(project)
        return projects

    def reconcile_repository_checkouts(
        self,
        *,
        host_id: str,
        repository_id: str,
        observations: Sequence[Mapping[str, Any]],
        observed_at: int | None = None,
    ) -> list[dict[str, Any]]:
        """Apply proven Git checkout evidence without mutating the repository."""

        host_id = _canonical_host_id(host_id)
        repository_id = _canonical_uuid_id(repository_id, RepositoryId, "repository_id")
        timestamp = now_ms() if observed_at is None else observed_at
        normalized: list[dict[str, Any]] = []
        paths: set[str] = set()
        git_dirs: set[str] = set()
        for observation in observations:
            path = str(Path(str(observation["path"])).resolve(strict=False))
            git_common_dir = str(
                Path(str(observation["git_common_dir"])).resolve(strict=False)
            )
            git_dir = str(Path(str(observation["git_dir"])).resolve(strict=False))
            kind = str(observation["kind"])
            if kind not in {"main", "worktree"}:
                raise StorageError("discovered Git checkout kind is invalid")
            branch = observation.get("branch")
            head_oid = observation.get("head_oid")
            for value, field in (
                (path, "checkout path"),
                (git_common_dir, "Git common directory"),
                (git_dir, "Git directory"),
                (branch, "branch"),
                (head_oid, "HEAD OID"),
            ):
                _reject_controls(value, field)
            if path in paths or git_dir in git_dirs:
                raise IdentityConflict("duplicate discovered Git checkout evidence")
            paths.add(path)
            git_dirs.add(git_dir)
            normalized.append(
                {
                    "path": path,
                    "kind": kind,
                    "branch": branch,
                    "head_oid": head_oid,
                    "git_common_dir": git_common_dir,
                    "git_dir": git_dir,
                }
            )
        with self.transaction(immediate=True) as connection:
            repository = connection.execute(
                "SELECT * FROM repositories WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
            if repository is None or repository["kind"] != "git":
                raise StorageError("unknown Git repository")
            retained = connection.execute(
                """
                SELECT * FROM checkouts
                WHERE repository_id = ? AND host_id = ?
                ORDER BY declared DESC, checkout_id
                """,
                (repository_id, host_id),
            ).fetchall()
            used_ids: set[str] = set()
            for observation in normalized:
                matches = [
                    row
                    for row in retained
                    if row["git_dir"] == observation["git_dir"]
                    or row["path"] == observation["path"]
                ]
                if len(matches) > 1:
                    raise IdentityConflict(
                        "discovered Git evidence matches multiple retained checkouts"
                    )
                existing = matches[0] if matches else None
                checkout_id = (
                    str(uuid.uuid4())
                    if existing is None
                    else str(existing["checkout_id"])
                )
                if existing is not None:
                    for field in ("git_common_dir", "git_dir"):
                        if (
                            existing[field] is not None
                            and existing[field] != observation[field]
                        ):
                            raise IdentityConflict(
                                "retained Git checkout evidence changed identity"
                            )
                    connection.execute(
                        """
                        UPDATE checkouts
                        SET path = ?, kind = ?, branch = ?, head_oid = ?,
                            present = 1, git_common_dir = ?, git_dir = ?,
                            last_observed_at = ?, updated_at = ?
                        WHERE checkout_id = ?
                        """,
                        (
                            observation["path"],
                            observation["kind"],
                            observation["branch"],
                            observation["head_oid"],
                            observation["git_common_dir"],
                            observation["git_dir"],
                            timestamp,
                            timestamp,
                            checkout_id,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO checkouts(
                            checkout_id, repository_id, host_id, path, kind,
                            branch, head_oid, is_default, declared, present,
                            git_common_dir, git_dir, last_observed_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 1, ?, ?, ?, ?, ?)
                        """,
                        (
                            checkout_id,
                            repository_id,
                            host_id,
                            observation["path"],
                            observation["kind"],
                            observation["branch"],
                            observation["head_oid"],
                            observation["git_common_dir"],
                            observation["git_dir"],
                            timestamp,
                            timestamp,
                            timestamp,
                        ),
                    )
                used_ids.add(checkout_id)
            for row in retained:
                if str(row["checkout_id"]) not in used_ids:
                    connection.execute(
                        """
                        UPDATE checkouts
                        SET present = 0, last_observed_at = ?, updated_at = ?
                        WHERE checkout_id = ?
                        """,
                        (timestamp, timestamp, row["checkout_id"]),
                    )
            rows = connection.execute(
                """
                SELECT * FROM checkouts
                WHERE repository_id = ? AND host_id = ?
                ORDER BY is_default DESC, path, checkout_id
                """,
                (repository_id, host_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_task(
        self,
        *,
        task_id: str,
        host_id: str,
        project_id: str,
        title: str,
        checkout_id: str | None = None,
        purpose: str | None = None,
        preferred_provider: str | None = None,
        observed_at: int | None = None,
    ) -> dict[str, Any]:
        """Create one explicit open task without starting a provider."""

        task_id = _canonical_uuid_id(task_id, TaskId, "task_id")
        host_id = _canonical_host_id(host_id)
        project_id = _canonical_uuid_id(project_id, ProjectId, "project_id")
        if checkout_id is not None:
            checkout_id = _canonical_uuid_id(checkout_id, CheckoutId, "checkout_id")
        title = _normalize_curation_text(title, "task title", maximum=256)
        purpose = _normalize_task_purpose(purpose)
        if preferred_provider not in {None, "codex", "claude"}:
            raise StorageError("preferred_provider must be codex or claude")
        timestamp = now_ms() if observed_at is None else observed_at
        try:
            with self.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id, host_id, project_id, checkout_id, title, purpose,
                        preferred_provider, status, pinned, current_session_key,
                        created_at, updated_at, closed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 0, NULL, ?, ?, NULL)
                    """,
                    (
                        task_id,
                        host_id,
                        project_id,
                        checkout_id,
                        title,
                        purpose,
                        preferred_provider,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
        except sqlite3.IntegrityError as error:
            message = str(error)
            if "worktree already belongs" in message:
                raise TaskConflict("worktree_claimed", message) from error
            raise TaskConflict("task_create_conflict", message) from error
        result = _row_dict(row)
        assert result is not None
        return result

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task_id = _canonical_uuid_id(task_id, TaskId, "task_id")
        return _row_dict(
            self.connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        )

    def list_tasks(
        self,
        *,
        host_id: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[str] = []
        if host_id is not None:
            clauses.append("host_id = ?")
            parameters.append(_canonical_host_id(host_id))
        if project_id is not None:
            clauses.append("project_id = ?")
            parameters.append(_canonical_uuid_id(project_id, ProjectId, "project_id"))
        if status is not None:
            if status not in {"open", "closed"}:
                raise StorageError("task status must be open or closed")
            clauses.append("status = ?")
            parameters.append(status)
        where = "" if not clauses else "WHERE " + " AND ".join(clauses)
        rows = self.connection.execute(
            f"""
            SELECT * FROM tasks
            {where}
            ORDER BY pinned DESC, updated_at DESC, task_id
            """,
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_task_sessions(self, task_id: str) -> list[dict[str, Any]]:
        task_id = _canonical_uuid_id(task_id, TaskId, "task_id")
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT * FROM sessions WHERE task_id = ?
                ORDER BY first_observed_at, session_key
                """,
                (task_id,),
            ).fetchall()
        ]

    def update_task(
        self,
        task_id: str,
        *,
        title: str | None = None,
        purpose: str | None = None,
        pinned: bool | None = None,
        observed_at: int | None = None,
    ) -> dict[str, Any]:
        task_id = _canonical_uuid_id(task_id, TaskId, "task_id")
        updates: dict[str, Any] = {}
        if title is not None:
            updates["title"] = _normalize_curation_text(
                title, "task title", maximum=256
            )
        if purpose is not None:
            updates["purpose"] = _normalize_task_purpose(purpose)
        if pinned is not None:
            if not isinstance(pinned, bool):
                raise StorageError("task pinned must be boolean")
            updates["pinned"] = int(pinned)
        if not updates:
            raise StorageError("task update requires at least one field")
        updates["updated_at"] = now_ms() if observed_at is None else observed_at
        with self.transaction(immediate=True) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                is None
            ):
                raise TaskConflict("task_not_found", f"unknown task: {task_id}")
            assignments = ", ".join(f"{field} = ?" for field in updates)
            connection.execute(
                f"UPDATE tasks SET {assignments} WHERE task_id = ?",
                (*updates.values(), task_id),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return result

    def route_task(
        self,
        task_id: str,
        *,
        host_id: str,
        checkout_id: str,
        observed_at: int | None = None,
    ) -> dict[str, Any]:
        """Assign a checkout before a task has acquired session history."""

        task_id = _canonical_uuid_id(task_id, TaskId, "task_id")
        host_id = _canonical_host_id(host_id)
        checkout_id = _canonical_uuid_id(checkout_id, CheckoutId, "checkout_id")
        timestamp = now_ms() if observed_at is None else observed_at
        try:
            with self.transaction(immediate=True) as connection:
                task = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if task is None or task["host_id"] != host_id:
                    raise TaskConflict(
                        "task_not_found", f"unknown local task: {task_id}"
                    )
                if task["status"] != "open":
                    raise TaskConflict("task_closed", "cannot route a closed task")
                if task["current_session_key"] is not None:
                    raise TaskConflict(
                        "task_has_history",
                        "a task checkout cannot change after its first session",
                    )
                connection.execute(
                    """
                    UPDATE tasks SET checkout_id = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (checkout_id, timestamp, task_id),
                )
                row = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
        except sqlite3.IntegrityError as error:
            code = (
                "worktree_claimed"
                if "worktree already belongs" in str(error)
                else "task_context_conflict"
            )
            raise TaskConflict(code, str(error)) from error
        result = _row_dict(row)
        assert result is not None
        return result

    def adopt_session(
        self,
        *,
        task_id: str,
        session_key: str,
        observed_at: int | None = None,
    ) -> dict[str, Any]:
        """Explicitly make an Inbox session the current session of a task."""

        task_id = _canonical_uuid_id(task_id, TaskId, "task_id")
        session_key = str(_canonical_session_key(session_key))
        timestamp = now_ms() if observed_at is None else observed_at
        with self.transaction(immediate=True) as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            session = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
            if task is None:
                raise TaskConflict("task_not_found", f"unknown task: {task_id}")
            if session is None:
                raise TaskConflict(
                    "session_not_found", f"unknown session: {session_key}"
                )
            if task["status"] != "open":
                raise TaskConflict("task_closed", "cannot adopt into a closed task")
            if task["current_session_key"] not in {None, session_key}:
                raise TaskConflict(
                    "task_has_current_session",
                    "task already has a different current session",
                )
            if session["task_id"] not in {None, task_id}:
                raise TaskConflict(
                    "session_already_adopted",
                    "session already belongs to another task",
                )
            if session["host_id"] != task["host_id"]:
                raise TaskConflict(
                    "task_host_conflict", "task and session belong to different hosts"
                )
            if task["checkout_id"] is None:
                if session["project_id"] != task["project_id"]:
                    raise TaskConflict(
                        "task_project_conflict",
                        "task and session belong to different projects",
                    )
                if session["checkout_id"] is None:
                    raise TaskConflict(
                        "session_checkout_missing",
                        "the selected session has no routable checkout",
                    )
                connection.execute(
                    """
                    UPDATE tasks SET checkout_id = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (session["checkout_id"], timestamp, task_id),
                )
                task = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                assert task is not None
            connection.execute(
                """
                UPDATE sessions
                SET task_id = ?, project_id = ?, checkout_id = ?,
                    metadata_source = 'task_adoption'
                WHERE session_key = ?
                """,
                (
                    task_id,
                    task["project_id"],
                    task["checkout_id"],
                    session_key,
                ),
            )
            connection.execute(
                """
                UPDATE tasks
                SET current_session_key = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (session_key, timestamp, task_id),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return result

    def close_task(
        self,
        task_id: str,
        *,
        host_id: str,
        summary: str | None = None,
        next_action: str | None = None,
        handoff_id: str | None = None,
        source: str = "user",
        observed_at: int | None = None,
    ) -> dict[str, Any]:
        """Close a task, wrapping its current session without stopping it."""

        task_id = _canonical_uuid_id(task_id, TaskId, "task_id")
        host_id = _canonical_host_id(host_id)
        timestamp = now_ms() if observed_at is None else observed_at
        with self.transaction(immediate=True) as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None or task["host_id"] != host_id:
                raise TaskConflict("task_not_found", f"unknown local task: {task_id}")
            if task["status"] == "closed":
                return dict(task)
            current_session_key = task["current_session_key"]
            if current_session_key is not None:
                if summary is None or next_action is None:
                    raise TaskConflict(
                        "handoff_required",
                        "closing a started task requires an explicit handoff",
                    )
                normalized_summary = normalize_handoff_text(summary, "summary")
                normalized_next = normalize_handoff_text(next_action, "next_action")
                parsed_handoff_id = _canonical_uuid_id(
                    handoff_id or str(uuid.uuid4()), HandoffId, "handoff_id"
                )
                handoff = self._append_handoff_row(
                    connection,
                    session_key=current_session_key,
                    summary=normalized_summary,
                    source=source,
                    source_host_id=host_id,
                    next_action=normalized_next,
                    handoff_id=parsed_handoff_id,
                    sequence=None,
                    created_at=timestamp,
                    content_hash=None,
                )
                connection.execute(
                    "UPDATE sessions SET wrapped_at = ? WHERE session_key = ?",
                    (handoff["created_at"], current_session_key),
                )
            elif any(value is not None for value in (summary, next_action, handoff_id)):
                raise TaskConflict(
                    "handoff_without_session",
                    "a never-started task cannot receive a close handoff",
                )
            connection.execute(
                """
                UPDATE tasks
                SET status = 'closed', closed_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (timestamp, timestamp, task_id),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return result

    def reopen_task(
        self,
        task_id: str,
        *,
        host_id: str,
        observed_at: int | None = None,
    ) -> dict[str, Any]:
        task_id = _canonical_uuid_id(task_id, TaskId, "task_id")
        host_id = _canonical_host_id(host_id)
        timestamp = now_ms() if observed_at is None else observed_at
        try:
            with self.transaction(immediate=True) as connection:
                task = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if task is None or task["host_id"] != host_id:
                    raise TaskConflict(
                        "task_not_found", f"unknown local task: {task_id}"
                    )
                if task["status"] == "open":
                    return dict(task)
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'open', closed_at = NULL, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (timestamp, task_id),
                )
                row = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
        except sqlite3.IntegrityError as error:
            if "worktree already belongs" in str(error):
                raise TaskConflict("worktree_claimed", str(error)) from error
            raise
        result = _row_dict(row)
        assert result is not None
        return result

    def reconcile_provider_sessions(
        self,
        host_id: str,
        provider: str,
        sessions: Sequence[Mapping[str, Any]],
        *,
        observed_at: int | None = None,
    ) -> ProviderSessionReconciliationResult:
        """Atomically apply one complete provider-session observation.

        The caller must withhold this operation when discovery is incomplete.
        Inputs are deliberately restricted to the privacy-safe provider
        metadata contract. Provider discovery changes resumability, but never
        advances the shared state clock or infers runtime presence, activity,
        or confidence. Those axes remain available to delayed hook evidence.
        """

        host_id = _canonical_host_id(host_id)
        provider = _canonical_provider(provider)
        timestamp = (
            now_ms()
            if observed_at is None
            else _nonnegative_integer(observed_at, "observed_at")
        )
        normalized = self._normalize_provider_session_scan(
            host_id,
            provider,
            sessions,
            observed_at=timestamp,
        )
        observed_keys = {session["session_key"] for session in normalized}

        with self.transaction(immediate=True) as connection:
            host = connection.execute(
                "SELECT host_id FROM hosts WHERE host_id = ?", (host_id,)
            ).fetchone()
            if host is None:
                raise StorageError(f"unknown host: {host_id}")

            existing_rows = connection.execute(
                """
                SELECT * FROM sessions
                WHERE host_id = ? AND provider = ?
                ORDER BY session_key
                """,
                (host_id, provider),
            ).fetchall()
            existing_by_key = {str(row["session_key"]): row for row in existing_rows}

            # Validate every database-dependent condition before the first
            # effective write. The enclosing transaction still protects the
            # entire operation if SQLite rejects a later row.
            for session in normalized:
                existing = existing_by_key.get(str(session["session_key"]))
                if existing is None:
                    continue
                self._validate_reconciliation_target(
                    existing,
                    session,
                    resumability="resumable",
                    observed_at=timestamp,
                )
            for existing in existing_rows:
                if existing["session_key"] in observed_keys:
                    continue
                self._validate_reconciliation_target(
                    existing,
                    None,
                    resumability="missing",
                    observed_at=timestamp,
                )

            declared_checkouts = tuple(
                Checkout(
                    checkout_id=CheckoutId(row["checkout_id"]),
                    repository_id=RepositoryId(row["repository_id"]),
                    host_id=HostId(row["host_id"]),
                    path=Path(row["path"]),
                    kind=CheckoutKind(row["kind"]),
                )
                for row in connection.execute(
                    """
                    SELECT checkout.checkout_id, checkout.repository_id,
                           checkout.host_id, checkout.path, checkout.kind
                    FROM checkouts AS checkout
                    JOIN repositories AS repository
                      ON repository.repository_id = checkout.repository_id
                    WHERE checkout.host_id = ?
                      AND checkout.declared = 1
                      AND checkout.present = 1
                      AND repository.declared = 1
                    ORDER BY checkout.checkout_id
                    """,
                    (host_id,),
                ).fetchall()
            )
            memberships = tuple(
                ProjectRepository(
                    project_id=ProjectId(row["project_id"]),
                    repository_id=RepositoryId(row["repository_id"]),
                    is_primary=bool(row["is_primary"]),
                )
                for row in connection.execute(
                    """
                    SELECT membership.*
                    FROM project_repositories AS membership
                    JOIN projects AS project
                      ON project.project_id = membership.project_id
                    WHERE project.declared = 1
                    ORDER BY membership.project_id, membership.repository_id
                    """
                ).fetchall()
            )
            assignments: dict[str, tuple[Checkout, ProjectId]] = {}
            for session in normalized:
                session_key = str(session["session_key"])
                existing = existing_by_key.get(session_key)
                if existing is not None and (
                    existing["project_id"] is not None
                    or existing["checkout_id"] is not None
                ):
                    continue
                checkout = match_checkout(
                    session["cwd"],
                    HostId(host_id),
                    declared_checkouts,
                )
                if checkout is not None:
                    matching_projects = {
                        membership.project_id
                        for membership in memberships
                        if membership.repository_id == checkout.repository_id
                    }
                    if len(matching_projects) == 1:
                        assignments[session_key] = (
                            checkout,
                            next(iter(matching_projects)),
                        )

            inserted_count = 0
            updated_count = 0
            for session in normalized:
                session_key = str(session["session_key"])
                existing = existing_by_key.get(session_key)
                update = self._provider_metadata_update(
                    session,
                    existing,
                )
                assignment = assignments.get(session_key)
                if assignment is not None:
                    checkout, project_id = assignment
                    update.update(
                        project_id=str(project_id),
                        checkout_id=str(checkout.checkout_id),
                        metadata_source="checkout_match",
                    )
                if existing is None:
                    # Initial provider history is resumability evidence, but
                    # it says nothing about activity freshness or confidence.
                    update["resumability"] = "resumable"
                self._upsert_session_row(connection, update)
                if existing is None:
                    inserted_count += 1
                else:
                    connection.execute(
                        "UPDATE sessions SET resumability = 'resumable' "
                        "WHERE session_key = ?",
                        (existing["session_key"],),
                    )
                    updated_count += 1

            missing_count = 0
            for existing in existing_rows:
                if existing["session_key"] in observed_keys:
                    continue
                update: dict[str, Any] = {
                    "session_key": existing["session_key"],
                    "last_observed_at": timestamp,
                }
                self._upsert_session_row(connection, update)
                connection.execute(
                    "UPDATE sessions SET resumability = 'missing' "
                    "WHERE session_key = ?",
                    (existing["session_key"],),
                )
                missing_count += 1

            reconciled = tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM sessions
                    WHERE host_id = ? AND provider = ?
                    ORDER BY session_key
                    """,
                    (host_id, provider),
                ).fetchall()
            )

        return ProviderSessionReconciliationResult(
            observed_at=timestamp,
            inserted_count=inserted_count,
            updated_count=updated_count,
            missing_count=missing_count,
            sessions=reconciled,
        )

    @staticmethod
    def _normalize_provider_session_scan(
        host_id: str,
        provider: str,
        sessions: Sequence[Mapping[str, Any]],
        *,
        observed_at: int,
    ) -> tuple[dict[str, Any], ...]:
        if isinstance(sessions, (str, bytes, bytearray)) or not isinstance(
            sessions, Sequence
        ):
            raise StorageError("provider sessions must be a complete sequence")

        normalized: list[dict[str, Any]] = []
        session_keys: set[str] = set()
        provider_session_ids: set[str] = set()
        for index, raw_session in enumerate(sessions):
            if not isinstance(raw_session, Mapping) or not all(
                isinstance(key, str) for key in raw_session
            ):
                raise StorageError(f"provider session {index} must be an object")
            unknown = set(raw_session) - _PROVIDER_SESSION_FIELDS
            if unknown:
                raise StorageError(
                    "provider session contains unsupported retained fields: "
                    f"{sorted(unknown)}"
                )
            missing = _REQUIRED_PROVIDER_SESSION_FIELDS - set(raw_session)
            if missing:
                raise StorageError(f"provider session is incomplete: {sorted(missing)}")

            session = dict(raw_session)
            row_host_id = _canonical_host_id(session["host_id"])
            row_provider = _canonical_provider(session["provider"])
            provider_session_id = _canonical_plain_uuid(
                session["provider_session_id"], "provider_session_id"
            )
            parsed_key = _canonical_session_key(session["session_key"])
            expected_identity = (
                host_id,
                provider,
                provider_session_id,
            )
            if (
                row_host_id,
                row_provider,
                provider_session_id,
            ) != expected_identity or (
                str(parsed_key.host_id),
                parsed_key.provider.value,
                str(parsed_key.provider_session_id),
            ) != expected_identity:
                raise IdentityConflict(
                    "provider session identity does not match the scan host/provider"
                )

            session_key = str(parsed_key)
            if session_key in session_keys:
                raise IdentityConflict(f"duplicate provider session key: {session_key}")
            if provider_session_id in provider_session_ids:
                raise IdentityConflict(
                    f"duplicate provider session ID: {provider_session_id}"
                )
            session_keys.add(session_key)
            provider_session_ids.add(provider_session_id)

            cwd = session["cwd"]
            if not isinstance(cwd, str) or not cwd or len(cwd) > 4096:
                raise StorageError("provider session cwd must be a bounded string")
            if not Path(cwd).is_absolute():
                raise StorageError("provider session cwd must be absolute")
            _reject_controls(cwd, "cwd")
            _reject_invalid_unicode(cwd, "cwd")
            name = session["name"]
            if name is not None:
                if not isinstance(name, str) or not name or len(name) > 256:
                    raise StorageError(
                        "provider session name must be null or a non-empty "
                        "bounded string"
                    )
                _reject_controls(name, "name")
                _reject_invalid_unicode(name, "name")
                if name != unicodedata.normalize("NFC", name).strip():
                    raise StorageError("provider session name must be normalized")
            if (
                not isinstance(session["metadata_source"], str)
                or session["metadata_source"] != "provider"
            ):
                raise StorageError(
                    "provider session metadata_source must be 'provider'"
                )

            created_at = _nonnegative_integer(session["created_at"], "created_at")
            provider_updated_at = _nonnegative_integer(
                session["provider_updated_at"], "provider_updated_at"
            )
            last_activity_at = _nonnegative_integer(
                session["last_activity_at"], "last_activity_at"
            )
            row_observed_at = _nonnegative_integer(
                session["last_observed_at"], "last_observed_at"
            )
            if row_observed_at != observed_at:
                raise StorageError(
                    "provider session observation time does not match its scan"
                )
            if provider_updated_at < created_at or last_activity_at < created_at:
                raise StorageError("provider session timestamps are reversed")

            normalized.append(session)

        return tuple(sorted(normalized, key=lambda session: session["session_key"]))

    @staticmethod
    def _provider_name_update(
        incoming: Mapping[str, Any],
        existing: sqlite3.Row | None,
    ) -> dict[str, Any]:
        update = dict(incoming)
        provider_name = update.pop("name")
        update["provider_name"] = provider_name
        update.pop("name_source", None)
        update.pop("name_actor", None)
        if existing is None or existing["name_source"] != "curated":
            update["name"] = provider_name
            update["name_source"] = "provider"
            update["name_actor"] = None
        return update

    @classmethod
    def _provider_metadata_update(
        cls,
        incoming: Mapping[str, Any],
        existing: sqlite3.Row | None,
    ) -> dict[str, Any]:
        update = cls._provider_name_update(incoming, existing)
        if existing is not None and existing["metadata_source"] != "provider":
            update.pop("metadata_source", None)
        return update

    @classmethod
    def _validate_reconciliation_target(
        cls,
        existing: sqlite3.Row,
        incoming: Mapping[str, Any] | None,
        *,
        resumability: str,
        observed_at: int,
    ) -> None:
        if observed_at < int(existing["last_observed_at"]):
            raise StorageError(f"stale provider scan for {existing['session_key']!r}")
        stored_resumability = str(existing["resumability"])
        if observed_at == int(
            existing["last_observed_at"]
        ) and stored_resumability not in {"unknown", resumability}:
            raise StorageError(
                "conflicting provider presence observation at the same timestamp"
            )
        if (
            incoming is not None
            and observed_at == int(existing["last_observed_at"])
            and stored_resumability == resumability
        ):
            effective = cls._provider_metadata_update(
                incoming,
                existing,
            )
            mutable_fields = {
                "cwd",
                "created_at",
                "provider_updated_at",
                "last_activity_at",
                "provider_name",
            }
            mutable_fields.update(
                {"name", "name_source", "metadata_source"}.intersection(effective)
            )
            if any(effective[field] != existing[field] for field in mutable_fields):
                raise StorageError(
                    "conflicting provider metadata observation at the same timestamp"
                )

    def upsert_session(self, session: Mapping[str, Any]) -> dict[str, Any]:
        """Insert a provider session or update only explicitly supplied fields."""

        with self.transaction(immediate=True) as connection:
            return self._upsert_session_row(connection, session)

    @staticmethod
    def _validate_effective_name_provenance(
        changes: Mapping[str, Any],
        existing: sqlite3.Row | None,
    ) -> None:
        name = changes.get("name", None if existing is None else existing["name"])
        provider_name = changes.get(
            "provider_name",
            None if existing is None else existing["provider_name"],
        )
        name_source = changes.get(
            "name_source",
            "unknown" if existing is None else existing["name_source"],
        )
        name_actor = changes.get(
            "name_actor",
            None if existing is None else existing["name_actor"],
        )
        if name_source == "provider" and name != provider_name:
            raise StorageError("provider-owned name and provider_name must agree")
        if name_source == "unknown" and name is not None:
            raise StorageError("unknown name_source requires name to be null")
        if name_actor not in {None, "user", "agent"}:
            raise StorageError("name_actor must be user, agent, or null")
        if name_source != "curated" and name_actor is not None:
            raise StorageError("only a curated name can retain name_actor")

    @staticmethod
    def _upsert_session_row(
        connection: sqlite3.Connection,
        session: Mapping[str, Any],
    ) -> dict[str, Any]:
        session = dict(session)
        try:
            session_key = str(session["session_key"])
        except KeyError as error:
            raise StorageError("missing session field: session_key") from error
        private_fields = _PRIVATE_SESSION_FIELDS.intersection(session)
        if private_fields:
            raise StorageError(
                "private session evidence fields require an atomic reconciler: "
                f"{sorted(private_fields)}"
            )
        if "surface_id" in session:
            raise StorageError(
                "session surface bindings must be changed with bind_surface"
            )
        _reject_mapping_controls(
            session,
            (
                "name",
                "provider_name",
                "name_source",
                "name_actor",
                "purpose",
                "cwd",
                "provider_runtime_id",
                "tmux_session",
                "tmux_window",
                "tmux_pane",
                "metadata_source",
            ),
        )
        if "name_source" in session and session["name_source"] not in {
            "unknown",
            "provider",
            "curated",
        }:
            raise StorageError("name_source must be unknown, provider, or curated")
        if "name_actor" in session and session["name_actor"] not in {
            None,
            "user",
            "agent",
        }:
            raise StorageError("name_actor must be user, agent, or null")
        if "name" in session:
            session.setdefault("name_source", "curated")
            if session["name_source"] == "provider":
                if (
                    "provider_name" in session
                    and session["provider_name"] != session["name"]
                ):
                    raise StorageError(
                        "provider-owned name and provider_name must agree"
                    )
                session["provider_name"] = session["name"]
        parsed_key = _canonical_session_key(session_key)
        timestamp = int(session.get("last_observed_at", now_ms()))

        existing = connection.execute(
            "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
        ).fetchone()
        if existing is None:
            required = {"host_id", "provider", "provider_session_id"} - set(session)
            if required:
                raise StorageError(f"missing session fields: {sorted(required)}")
            supplied_identity = (
                str(session["host_id"]),
                str(session["provider"]),
                str(session["provider_session_id"]),
            )
            expected_identity = (
                str(parsed_key.host_id),
                parsed_key.provider.value,
                str(parsed_key.provider_session_id),
            )
            if supplied_identity != expected_identity:
                raise IdentityConflict(
                    "session key does not match host, provider, and provider session ID"
                )
            values: dict[str, Any] = {
                "session_key": session_key,
                "host_id": expected_identity[0],
                "provider": expected_identity[1],
                "provider_session_id": expected_identity[2],
                "first_observed_at": int(session.get("first_observed_at", timestamp)),
                "last_observed_at": timestamp,
            }
            values.update(
                {field: session[field] for field in _SESSION_FIELDS if field in session}
            )
            Registry._validate_effective_name_provenance(values, None)
            columns = tuple(values)
            placeholders = ", ".join("?" for _ in columns)
            column_names = ", ".join(columns)
            connection.execute(
                f"INSERT INTO sessions({column_names}) VALUES ({placeholders})",
                tuple(values[column] for column in columns),
            )
        else:
            for identity_field in ("host_id", "provider", "provider_session_id"):
                if (
                    identity_field in session
                    and session[identity_field] != existing[identity_field]
                ):
                    raise IdentityConflict(
                        f"session {session_key!r} changed {identity_field}"
                    )
            expected_identity = (
                str(parsed_key.host_id),
                parsed_key.provider.value,
                str(parsed_key.provider_session_id),
            )
            if expected_identity != (
                existing["host_id"],
                existing["provider"],
                existing["provider_session_id"],
            ):
                raise IdentityConflict(
                    "session key does not match stored identity fields"
                )
            if timestamp < int(existing["last_observed_at"]):
                raise StorageError(
                    f"stale session observation for {session_key!r}: "
                    f"{timestamp} < {existing['last_observed_at']}"
                )
            for axis_timestamp, axis_name, axis_fields in (
                ("runtime_observed_at", "runtime", _SESSION_RUNTIME_FIELDS),
                ("state_observed_at", "state", _SESSION_STATE_FIELDS),
            ):
                supplied_axis_fields = axis_fields.intersection(session)
                if axis_timestamp not in session:
                    if supplied_axis_fields and existing[axis_timestamp] is not None:
                        raise StorageError(
                            f"session {axis_name} observation requires {axis_timestamp}"
                        )
                    continue
                incoming_axis_timestamp = session[axis_timestamp]
                stored_axis_timestamp = existing[axis_timestamp]
                if stored_axis_timestamp is not None and (
                    incoming_axis_timestamp is None
                    or int(incoming_axis_timestamp) < int(stored_axis_timestamp)
                ):
                    raise StorageError(f"stale session {axis_name} observation")
                if (
                    stored_axis_timestamp is not None
                    and incoming_axis_timestamp is not None
                    and int(incoming_axis_timestamp) == int(stored_axis_timestamp)
                    and any(
                        session[field] != existing[field]
                        for field in supplied_axis_fields
                    )
                ):
                    raise StorageError(
                        f"conflicting session {axis_name} observation "
                        "at the same timestamp"
                    )
            updates = {
                field: session[field] for field in _SESSION_FIELDS if field in session
            }
            updates.setdefault("last_observed_at", timestamp)
            updates.pop("first_observed_at", None)
            Registry._validate_effective_name_provenance(updates, existing)
            assignments = ", ".join(f"{field} = ?" for field in updates)
            connection.execute(
                f"UPDATE sessions SET {assignments} WHERE session_key = ?",
                (*updates.values(), session_key),
            )
        row = connection.execute(
            "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
        ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return result

    def get_session(self, session_key: str) -> dict[str, Any] | None:
        return _row_dict(
            self.connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
        )

    def list_sessions(self, *, host_id: str | None = None) -> list[dict[str, Any]]:
        if host_id is None:
            rows = self.connection.execute(
                "SELECT * FROM sessions ORDER BY last_observed_at DESC, session_key"
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM sessions
                WHERE host_id = ?
                ORDER BY last_observed_at DESC, session_key
                """,
                (host_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _local_session_row(
        connection: sqlite3.Connection,
        *,
        host_id: str,
        session_key: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
        ).fetchone()
        if row is None:
            raise StorageError(f"unknown session: {session_key}")
        host = connection.execute(
            "SELECT is_local FROM hosts WHERE host_id = ?", (host_id,)
        ).fetchone()
        if row["host_id"] != host_id or host is None or not bool(host["is_local"]):
            raise StorageError("session is not owned by the local host")
        return row

    @staticmethod
    def _validate_local_session_identity(host_id: str, session_key: str) -> None:
        canonical_host = _canonical_host_id(host_id)
        key = _canonical_session_key(session_key)
        if key.host_id != HostId(canonical_host):
            raise StorageError("session key belongs to a different host")

    def read_session_detail(
        self,
        session_key: str,
        *,
        host_id: str,
        handoff_limit: int = DEFAULT_HANDOFF_LIMIT,
    ) -> SessionDetailRows:
        """Read one local session and its newest bounded handoffs coherently."""

        self._validate_local_session_identity(host_id, session_key)
        if (
            isinstance(handoff_limit, bool)
            or not isinstance(handoff_limit, int)
            or not 1 <= handoff_limit <= MAX_HANDOFF_LIMIT
        ):
            raise StorageError(
                f"handoff_limit must be between 1 and {MAX_HANDOFF_LIMIT}"
            )
        with self.transaction() as connection:
            session = self._local_session_row(
                connection, host_id=host_id, session_key=session_key
            )
            rows = connection.execute(
                """
                SELECT * FROM handoffs
                WHERE session_key = ?
                ORDER BY sequence DESC, handoff_id
                LIMIT ?
                """,
                (session_key, handoff_limit + 1),
            ).fetchall()
        return SessionDetailRows(
            session=dict(session),
            handoffs=tuple(dict(row) for row in rows[:handoff_limit]),
            handoffs_truncated=len(rows) > handoff_limit,
        )

    def read_project_context(
        self,
        session_key: str,
        *,
        host_id: str,
        session_limit: int = DEFAULT_AGENT_CONTEXT_SESSION_LIMIT,
    ) -> ProjectContextRows:
        """Read bounded same-project local session context coherently."""

        self._validate_local_session_identity(host_id, session_key)
        if (
            isinstance(session_limit, bool)
            or not isinstance(session_limit, int)
            or not 1 <= session_limit <= DEFAULT_AGENT_PROJECT_SESSION_LIMIT
        ):
            raise StorageError(
                "session_limit must be between 1 and "
                f"{DEFAULT_AGENT_PROJECT_SESSION_LIMIT}"
            )
        with self.transaction() as connection:
            current = self._local_session_row(
                connection, host_id=host_id, session_key=session_key
            )
            project_id = current["project_id"]
            checkout_id = current["checkout_id"]
            if not isinstance(project_id, str) or not isinstance(checkout_id, str):
                raise StorageError(
                    "the current session has no complete project checkout"
                )
            retained_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM sessions
                    WHERE host_id = ? AND project_id = ?
                    """,
                    (host_id, project_id),
                ).fetchone()[0]
            )
            session_rows = connection.execute(
                """
                SELECT * FROM sessions
                WHERE host_id = ? AND project_id = ?
                ORDER BY
                    CASE WHEN session_key = ? THEN 0 ELSE 1 END,
                    pinned DESC,
                    COALESCE(last_activity_at, last_observed_at) DESC,
                    session_key
                LIMIT ?
                """,
                (host_id, project_id, session_key, session_limit),
            ).fetchall()
            handoff_ids = [
                str(row["latest_handoff_id"])
                for row in session_rows
                if row["latest_handoff_id"] is not None
            ]
            handoffs_by_id: dict[str, sqlite3.Row] = {}
            if handoff_ids:
                placeholders = ", ".join("?" for _ in handoff_ids)
                handoffs_by_id = {
                    str(row["handoff_id"]): row
                    for row in connection.execute(
                        f"SELECT * FROM handoffs WHERE handoff_id IN ({placeholders})",
                        handoff_ids,
                    ).fetchall()
                }
            latest_handoffs: list[dict[str, Any]] = []
            for row in session_rows:
                handoff_id = row["latest_handoff_id"]
                if handoff_id is None:
                    continue
                handoff = handoffs_by_id.get(str(handoff_id))
                if handoff is None or handoff["session_key"] != row["session_key"]:
                    raise StorageError(
                        "a projected session has an inconsistent latest handoff"
                    )
                latest_handoffs.append(dict(handoff))
        return ProjectContextRows(
            current_session=dict(current),
            sessions=tuple(dict(row) for row in session_rows),
            latest_handoffs=tuple(latest_handoffs),
            retained_session_count=retained_count,
        )

    def read_project_session_detail(
        self,
        caller_session_key: str,
        target_session_key: str,
        *,
        host_id: str,
        handoff_limit: int = DEFAULT_HANDOFF_LIMIT,
    ) -> SessionDetailRows:
        """Read a target only when it is in the caller's local project."""

        self._validate_local_session_identity(host_id, caller_session_key)
        target_key = _canonical_session_key(target_session_key)
        if target_key.host_id != HostId(host_id):
            raise StorageError("session is not in the current project")
        if (
            isinstance(handoff_limit, bool)
            or not isinstance(handoff_limit, int)
            or not 1 <= handoff_limit <= MAX_HANDOFF_LIMIT
        ):
            raise StorageError(
                f"handoff_limit must be between 1 and {MAX_HANDOFF_LIMIT}"
            )
        with self.transaction() as connection:
            caller = self._local_session_row(
                connection, host_id=host_id, session_key=caller_session_key
            )
            target = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (target_session_key,)
            ).fetchone()
            if (
                target is None
                or not isinstance(caller["project_id"], str)
                or target["host_id"] != host_id
                or caller["project_id"] != target["project_id"]
            ):
                raise StorageError("session is not in the current project")
            rows = connection.execute(
                """
                SELECT * FROM handoffs
                WHERE session_key = ?
                ORDER BY sequence DESC, handoff_id
                LIMIT ?
                """,
                (target_session_key, handoff_limit + 1),
            ).fetchall()
        return SessionDetailRows(
            session=dict(target),
            handoffs=tuple(dict(row) for row in rows[:handoff_limit]),
            handoffs_truncated=len(rows) > handoff_limit,
        )

    def read_project_handoff(
        self,
        caller_session_key: str,
        handoff_id: str,
        *,
        host_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Read one exact handoff scoped to the caller's local project."""

        self._validate_local_session_identity(host_id, caller_session_key)
        canonical_handoff = _canonical_uuid_id(handoff_id, HandoffId, "handoff_id")
        with self.transaction() as connection:
            caller = self._local_session_row(
                connection, host_id=host_id, session_key=caller_session_key
            )
            row = connection.execute(
                """
                SELECT h.*, s.host_id AS session_host_id,
                       s.project_id AS session_project_id
                FROM handoffs AS h
                JOIN sessions AS s ON s.session_key = h.session_key
                WHERE h.handoff_id = ?
                """,
                (canonical_handoff,),
            ).fetchone()
            if (
                row is None
                or not isinstance(caller["project_id"], str)
                or row["session_host_id"] != host_id
                or row["session_project_id"] != caller["project_id"]
            ):
                raise StorageError("handoff is not in the current project")
        handoff = dict(row)
        handoff.pop("session_host_id", None)
        handoff.pop("session_project_id", None)
        return dict(caller), handoff

    def search_project_context(
        self,
        caller_session_key: str,
        query: str,
        *,
        host_id: str,
        limit: int = DEFAULT_AGENT_SEARCH_LIMIT,
    ) -> ProjectSearchRows:
        """Search bounded curated metadata without reading provider transcripts."""

        self._validate_local_session_identity(host_id, caller_session_key)
        normalized_query = _normalize_curation_text(query, "query", maximum=256)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= DEFAULT_AGENT_SEARCH_LIMIT
        ):
            raise StorageError(
                f"limit must be between 1 and {DEFAULT_AGENT_SEARCH_LIMIT}"
            )
        with self.transaction() as connection:
            caller = self._local_session_row(
                connection, host_id=host_id, session_key=caller_session_key
            )
            project_id = caller["project_id"]
            if not isinstance(project_id, str):
                raise StorageError("the current session has no project")
            session_rows = connection.execute(
                """
                SELECT * FROM sessions
                WHERE host_id = ? AND project_id = ?
                ORDER BY COALESCE(last_activity_at, last_observed_at) DESC, session_key
                LIMIT ?
                """,
                (host_id, project_id, MAX_AGENT_SEARCH_SESSION_CANDIDATES + 1),
            ).fetchall()
            handoff_rows = connection.execute(
                """
                SELECT h.* FROM handoffs AS h
                JOIN sessions AS s ON s.session_key = h.session_key
                WHERE s.host_id = ? AND s.project_id = ?
                ORDER BY h.created_at DESC, h.handoff_id
                LIMIT ?
                """,
                (host_id, project_id, MAX_AGENT_SEARCH_HANDOFF_CANDIDATES + 1),
            ).fetchall()
        candidates_truncated = (
            len(session_rows) > MAX_AGENT_SEARCH_SESSION_CANDIDATES
            or len(handoff_rows) > MAX_AGENT_SEARCH_HANDOFF_CANDIDATES
        )
        needle = normalized_query.casefold()
        matches: list[dict[str, Any]] = []
        for row in session_rows[:MAX_AGENT_SEARCH_SESSION_CANDIDATES]:
            if any(
                needle in str(row[field]).casefold()
                for field in ("name", "purpose", "provider")
                if row[field] is not None
            ):
                record: dict[str, Any] = {
                    "kind": "session",
                    "session_key": row["session_key"],
                    "provider": row["provider"],
                    "observed_at": row["last_activity_at"] or row["last_observed_at"],
                }
                if row["name"] is not None:
                    record["name"] = row["name"]
                if row["purpose"] is not None:
                    record["purpose"] = row["purpose"]
                matches.append(record)
        for row in handoff_rows[:MAX_AGENT_SEARCH_HANDOFF_CANDIDATES]:
            if any(
                needle in str(row[field]).casefold()
                for field in ("summary", "next_action")
            ):
                matches.append(
                    {
                        "kind": "handoff",
                        "session_key": row["session_key"],
                        "handoff_id": row["handoff_id"],
                        "sequence": row["sequence"],
                        "summary": row["summary"],
                        "next_action": row["next_action"],
                        "source": row["source"],
                        "observed_at": row["created_at"],
                    }
                )
        matches.sort(
            key=lambda item: (
                -int(item["observed_at"]),
                str(item["kind"]),
                str(item.get("handoff_id", item["session_key"])),
            )
        )
        return ProjectSearchRows(
            current_session=dict(caller),
            query=normalized_query,
            results=tuple(matches[:limit]),
            results_truncated=candidates_truncated or len(matches) > limit,
        )

    def get_handoff(self, handoff_id: str) -> dict[str, Any] | None:
        handoff_id = _canonical_uuid_id(handoff_id, HandoffId, "handoff_id")
        return _row_dict(
            self.connection.execute(
                "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
            ).fetchone()
        )

    def export_task_handoff(
        self,
        task_id: str,
        handoff_id: str,
        *,
        host_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Read one exact local task handoff for a bounded export envelope."""

        task_id = _canonical_uuid_id(task_id, TaskId, "task_id")
        handoff_id = _canonical_uuid_id(handoff_id, HandoffId, "handoff_id")
        host_id = _canonical_host_id(host_id)
        with self.transaction() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ? AND host_id = ?",
                (task_id, host_id),
            ).fetchone()
            row = connection.execute(
                """
                SELECT h.*, s.task_id AS session_task_id, s.host_id AS session_host_id,
                       s.project_id AS session_project_id
                FROM handoffs AS h
                JOIN sessions AS s ON s.session_key = h.session_key
                WHERE h.handoff_id = ?
                """,
                (handoff_id,),
            ).fetchone()
            if (
                task is None
                or row is None
                or row["source"] == "imported"
                or row["session_task_id"] != task_id
                or row["session_host_id"] != host_id
                or row["session_project_id"] != task["project_id"]
            ):
                raise StorageError("handoff does not belong to the local task")
        handoff = dict(row)
        handoff.pop("session_task_id", None)
        handoff.pop("session_host_id", None)
        handoff.pop("session_project_id", None)
        session = self.get_session(str(handoff["session_key"]))
        assert session is not None
        return dict(task), session, handoff

    def list_task_imported_handoffs(
        self,
        task_id: str,
        *,
        limit: int = DEFAULT_HANDOFF_LIMIT,
    ) -> list[dict[str, Any]]:
        task_id = _canonical_uuid_id(task_id, TaskId, "task_id")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_HANDOFF_LIMIT
        ):
            raise StorageError(f"limit must be between 1 and {MAX_HANDOFF_LIMIT}")
        rows = self.connection.execute(
            """
            SELECT h.*, link.source_task_id, link.source_project_id,
                   link.imported_at
            FROM task_imported_handoffs AS link
            JOIN handoffs AS h ON h.handoff_id = link.handoff_id
            WHERE link.task_id = ?
            ORDER BY link.imported_at DESC, h.handoff_id
            LIMIT ?
            """,
            (task_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_task_imported_handoff(
        self,
        task_id: str,
        handoff_id: str,
    ) -> dict[str, Any] | None:
        task_id = _canonical_uuid_id(task_id, TaskId, "task_id")
        handoff_id = _canonical_uuid_id(handoff_id, HandoffId, "handoff_id")
        return _row_dict(
            self.connection.execute(
                """
                SELECT h.*, link.source_task_id, link.source_project_id,
                       link.imported_at
                FROM task_imported_handoffs AS link
                JOIN handoffs AS h ON h.handoff_id = link.handoff_id
                WHERE link.task_id = ? AND link.handoff_id = ?
                """,
                (task_id, handoff_id),
            ).fetchone()
        )

    def resolve_continuation_source(
        self,
        reference: str,
        *,
        host_id: str,
    ) -> ContinuationSource:
        """Resolve one retained local session or exact handoff for preparation."""

        host_id = _canonical_host_id(host_id)
        from_session = ":" in reference
        if from_session:
            session_key = str(_canonical_session_key(reference))
            handoff_id = None
        else:
            session_key = None
            handoff_id = _canonical_uuid_id(reference, HandoffId, "handoff_id")
        with self.transaction() as connection:
            if session_key is not None:
                session = self._local_session_row(
                    connection, host_id=host_id, session_key=session_key
                )
                handoff_id = session["latest_handoff_id"]
                if handoff_id is None:
                    raise ContinuationError(
                        "continuation_handoff_missing",
                        "The source session has no explicit handoff.",
                    )
                handoff = connection.execute(
                    "SELECT * FROM handoffs WHERE handoff_id = ?",
                    (handoff_id,),
                ).fetchone()
            else:
                handoff = connection.execute(
                    "SELECT * FROM handoffs WHERE handoff_id = ?",
                    (handoff_id,),
                ).fetchone()
                if handoff is None:
                    raise ContinuationError(
                        "continuation_handoff_not_found",
                        "The selected handoff is not retained.",
                    )
                session_key = str(handoff["session_key"])
                session = self._local_session_row(
                    connection, host_id=host_id, session_key=session_key
                )
            if handoff is None or handoff["session_key"] != session_key:
                raise ContinuationError(
                    "continuation_handoff_inconsistent",
                    "The selected handoff is not bound to its source session.",
                )
            if handoff["source"] == "imported":
                raise ContinuationError(
                    "continuation_remote_not_supported",
                    "Imported handoff continuation remains a remote-host feature.",
                )
            if not all(
                isinstance(session[field], str) and session[field]
                for field in ("project_id", "checkout_id", "cwd")
            ):
                raise ContinuationError(
                    "continuation_checkout_missing",
                    "The source session has no complete local project checkout.",
                )
        return ContinuationSource(dict(session), dict(handoff), from_session)

    def list_handoffs(
        self,
        session_key: str,
        *,
        limit: int = DEFAULT_HANDOFF_LIMIT,
        before_sequence: int | None = None,
    ) -> list[dict[str, Any]]:
        _canonical_session_key(session_key)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_HANDOFF_LIMIT
        ):
            raise StorageError(f"limit must be between 1 and {MAX_HANDOFF_LIMIT}")
        if before_sequence is not None and (
            isinstance(before_sequence, bool)
            or not isinstance(before_sequence, int)
            or before_sequence < 1
        ):
            raise StorageError("before_sequence must be a positive integer")
        if before_sequence is None:
            rows = self.connection.execute(
                """
                SELECT * FROM handoffs WHERE session_key = ?
                ORDER BY sequence DESC, handoff_id LIMIT ?
                """,
                (session_key, limit),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM handoffs
                WHERE session_key = ? AND sequence < ?
                ORDER BY sequence DESC, handoff_id LIMIT ?
                """,
                (session_key, before_sequence, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_session_name(
        self,
        session_key: str,
        *,
        host_id: str,
        name: str | None,
        actor: str = "user",
    ) -> dict[str, Any]:
        """Set or clear one curated name without advancing observation time."""

        self._validate_local_session_identity(host_id, session_key)
        normalized = (
            None
            if name is None
            else _normalize_curation_text(name, "session name", maximum=512)
        )
        if actor not in {"user", "agent"}:
            raise StorageError("name actor must be user or agent")
        with self.transaction(immediate=True) as connection:
            session = self._local_session_row(
                connection, host_id=host_id, session_key=session_key
            )
            if normalized is None:
                provider_name = session["provider_name"]
                connection.execute(
                    """
                    UPDATE sessions SET name = ?, name_source = ?, name_actor = NULL
                    WHERE session_key = ?
                    """,
                    (
                        provider_name,
                        "provider" if provider_name is not None else "unknown",
                        session_key,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE sessions
                    SET name = ?, name_source = 'curated', name_actor = ?
                    WHERE session_key = ?
                    """,
                    (normalized, actor, session_key),
                )
            updated = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
        result = _row_dict(updated)
        assert result is not None
        return result

    def set_session_purpose(
        self,
        session_key: str,
        *,
        host_id: str,
        purpose: str | None,
    ) -> dict[str, Any]:
        """Set or clear one explicit purpose without changing runtime truth."""

        self._validate_local_session_identity(host_id, session_key)
        normalized = (
            None
            if purpose is None
            else _normalize_curation_text(purpose, "session purpose", maximum=4096)
        )
        with self.transaction(immediate=True) as connection:
            self._local_session_row(
                connection, host_id=host_id, session_key=session_key
            )
            connection.execute(
                "UPDATE sessions SET purpose = ? WHERE session_key = ?",
                (normalized, session_key),
            )
            updated = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
        result = _row_dict(updated)
        assert result is not None
        return result

    def set_session_pinned(
        self,
        session_key: str,
        *,
        host_id: str,
        pinned: bool,
    ) -> dict[str, Any]:
        """Set one local pin without changing provider observation clocks."""

        self._validate_local_session_identity(host_id, session_key)
        if not isinstance(pinned, bool):
            raise StorageError("pinned must be boolean")
        with self.transaction(immediate=True) as connection:
            self._local_session_row(
                connection, host_id=host_id, session_key=session_key
            )
            connection.execute(
                "UPDATE sessions SET pinned = ? WHERE session_key = ?",
                (int(pinned), session_key),
            )
            updated = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
        result = _row_dict(updated)
        assert result is not None
        return result

    def clear_session_wrapped(
        self,
        session_key: str,
        *,
        host_id: str,
    ) -> dict[str, Any]:
        """Clear only the wrapping marker after successful re-entry."""

        self._validate_local_session_identity(host_id, session_key)
        with self.transaction(immediate=True) as connection:
            self._local_session_row(
                connection, host_id=host_id, session_key=session_key
            )
            connection.execute(
                "UPDATE sessions SET wrapped_at = NULL WHERE session_key = ?",
                (session_key,),
            )
            updated = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
        result = _row_dict(updated)
        assert result is not None
        return result

    def read_host_snapshot(
        self,
        host_id: str,
        *,
        task_limit: int = DEFAULT_SNAPSHOT_TASK_LIMIT,
        session_limit: int = DEFAULT_SNAPSHOT_SESSION_LIMIT,
        runtime_limit: int = DEFAULT_SNAPSHOT_RUNTIME_LIMIT,
    ) -> HostSnapshotRows:
        """Read host-local snapshot inputs from one coherent DB view.

        Task/session candidates and append-only runtime observations are bounded
        at read time. Coherent retained counts let snapshot assembly report
        explicit truncation without loading the entire registry.
        """

        host_id = _canonical_host_id(host_id)
        if (
            isinstance(task_limit, bool)
            or not isinstance(task_limit, int)
            or not 1 <= task_limit <= DEFAULT_SNAPSHOT_TASK_LIMIT
        ):
            raise StorageError(
                f"task_limit must be between 1 and {DEFAULT_SNAPSHOT_TASK_LIMIT}"
            )
        if (
            isinstance(session_limit, bool)
            or not isinstance(session_limit, int)
            or not 1 <= session_limit <= DEFAULT_SNAPSHOT_SESSION_LIMIT
        ):
            raise StorageError(
                f"session_limit must be between 1 and {DEFAULT_SNAPSHOT_SESSION_LIMIT}"
            )
        if (
            isinstance(runtime_limit, bool)
            or not isinstance(runtime_limit, int)
            or not 1 <= runtime_limit <= DEFAULT_SNAPSHOT_RUNTIME_LIMIT
        ):
            raise StorageError(
                f"runtime_limit must be between 1 and {DEFAULT_SNAPSHOT_RUNTIME_LIMIT}"
            )
        with self.transaction() as connection:
            host_row = connection.execute(
                "SELECT * FROM hosts WHERE host_id = ?", (host_id,)
            ).fetchone()
            if host_row is None:
                raise StorageError(f"unknown host: {host_id}")

            checkouts = tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM checkouts
                    WHERE host_id = ?
                    ORDER BY repository_id, checkout_id
                    """,
                    (host_id,),
                ).fetchall()
            )
            retained_task_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE host_id = ?",
                    (host_id,),
                ).fetchone()[0]
            )
            tasks = tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM tasks
                    WHERE host_id = ?
                    ORDER BY status, pinned DESC, updated_at DESC, task_id
                    LIMIT ?
                    """,
                    (host_id, task_limit),
                ).fetchall()
            )
            retained_session_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM sessions WHERE host_id = ?",
                    (host_id,),
                ).fetchone()[0]
            )
            sessions = tuple(
                dict(row)
                for row in connection.execute(
                    """
                    WITH selected_tasks AS (
                        SELECT current_session_key
                        FROM tasks
                        WHERE host_id = ? AND current_session_key IS NOT NULL
                        ORDER BY status, pinned DESC, updated_at DESC, task_id
                        LIMIT ?
                    )
                    SELECT * FROM sessions
                    WHERE host_id = ?
                    ORDER BY CASE WHEN session_key IN (
                                 SELECT current_session_key FROM selected_tasks
                             ) THEN 0 ELSE 1 END,
                             session_key
                    LIMIT ?
                    """,
                    (host_id, task_limit, host_id, session_limit),
                ).fetchall()
            )
            projects = tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM projects
                    WHERE (
                        ? = 1 AND declared = 1
                    ) OR project_id IN (
                            SELECT project_id FROM sessions
                            WHERE host_id = ? AND project_id IS NOT NULL
                            UNION
                            SELECT project_id FROM tasks
                            WHERE host_id = ?
                        )
                    ORDER BY project_id
                    """,
                    (int(bool(host_row["is_local"])), host_id, host_id),
                ).fetchall()
            )
            project_repositories = tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT membership.*
                    FROM project_repositories AS membership
                    WHERE membership.project_id IN (
                        SELECT project_id FROM projects
                        WHERE ? = 1 AND declared = 1
                        UNION
                        SELECT project_id FROM sessions
                        WHERE host_id = ? AND project_id IS NOT NULL
                        UNION
                        SELECT project_id FROM tasks
                        WHERE host_id = ?
                    )
                    ORDER BY membership.project_id, membership.repository_id
                    """,
                    (int(bool(host_row["is_local"])), host_id, host_id),
                ).fetchall()
            )
            repositories = tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM repositories
                    WHERE repository_id IN (
                        SELECT repository_id FROM project_repositories
                        WHERE project_id IN (
                            SELECT project_id FROM projects
                            WHERE ? = 1 AND declared = 1
                            UNION
                            SELECT project_id FROM sessions
                            WHERE host_id = ? AND project_id IS NOT NULL
                            UNION
                            SELECT project_id FROM tasks
                            WHERE host_id = ?
                        )
                        UNION
                        SELECT repository_id FROM checkouts WHERE host_id = ?
                    )
                    ORDER BY repository_id
                    """,
                    (int(bool(host_row["is_local"])), host_id, host_id, host_id),
                ).fetchall()
            )
            latest_runtime_rows = connection.execute(
                _SNAPSHOT_RUNTIME_TAIL_QUERY,
                (host_id, runtime_limit),
            ).fetchall()
            runtimes = tuple(dict(row) for row in reversed(latest_runtime_rows))
            surfaces = tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM surfaces
                    WHERE host_id = ?
                    ORDER BY surface_id
                    """,
                    (host_id,),
                ).fetchall()
            )

        return HostSnapshotRows(
            host=dict(host_row),
            projects=projects,
            project_repositories=project_repositories,
            repositories=repositories,
            checkouts=checkouts,
            tasks=tasks,
            retained_task_count=retained_task_count,
            sessions=sessions,
            retained_session_count=retained_session_count,
            runtimes=runtimes,
            surfaces=surfaces,
        )

    @staticmethod
    def _append_handoff_row(
        connection: sqlite3.Connection,
        *,
        session_key: str,
        summary: str,
        source: str,
        source_host_id: str,
        next_action: str,
        handoff_id: str,
        sequence: int | None,
        created_at: int | None,
        content_hash: str | None,
    ) -> dict[str, Any]:
        existing = connection.execute(
            "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        if existing is not None:
            timestamp = (
                int(existing["created_at"]) if created_at is None else created_at
            )
            requested_sequence = (
                int(existing["sequence"]) if sequence is None else sequence
            )
            calculated_hash = handoff_content_hash(summary, next_action)
            if content_hash is not None and content_hash != calculated_hash:
                raise IdentityConflict(
                    "handoff content hash does not match canonical content"
                )
            immutable = {
                "session_key": session_key,
                "sequence": requested_sequence,
                "summary": summary,
                "next_action": next_action,
                "source": source,
                "source_host_id": source_host_id,
                "created_at": timestamp,
                "content_hash": calculated_hash,
            }
            if any(existing[field] != value for field, value in immutable.items()):
                raise IdentityConflict(
                    f"handoff ID {handoff_id!r} was reused for different content"
                )
            return dict(existing)

        timestamp = now_ms() if created_at is None else created_at
        if sequence is None:
            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM handoffs WHERE session_key = ?
                    """,
                    (session_key,),
                ).fetchone()[0]
            )
        calculated_hash = handoff_content_hash(summary, next_action)
        if content_hash is not None and content_hash != calculated_hash:
            raise IdentityConflict(
                "handoff content hash does not match canonical content"
            )

        connection.execute(
            """
            INSERT INTO handoffs(
                handoff_id, session_key, sequence, summary, next_action,
                source, source_host_id, created_at, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                handoff_id,
                session_key,
                sequence,
                summary,
                next_action,
                source,
                source_host_id,
                timestamp,
                calculated_hash,
            ),
        )
        connection.execute(
            "UPDATE sessions SET latest_handoff_id = ? WHERE session_key = ?",
            (handoff_id, session_key),
        )
        row = connection.execute(
            "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return result

    def append_handoff(
        self,
        *,
        session_key: str,
        summary: str,
        source: str,
        source_host_id: str,
        next_action: str,
        handoff_id: str | None = None,
        sequence: int | None = None,
        created_at: int | None = None,
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        summary = normalize_handoff_text(summary, "summary")
        next_action = normalize_handoff_text(next_action, "next_action")
        handoff_id = _canonical_uuid_id(
            handoff_id or str(uuid.uuid4()), HandoffId, "handoff_id"
        )
        _canonical_session_key(session_key)
        _canonical_host_id(source_host_id)

        with self.transaction(immediate=True) as connection:
            return self._append_handoff_row(
                connection,
                session_key=session_key,
                summary=summary,
                source=source,
                source_host_id=source_host_id,
                next_action=next_action,
                handoff_id=handoff_id,
                sequence=sequence,
                created_at=created_at,
                content_hash=content_hash,
            )

    def curate_session_handoff(
        self,
        session_key: str,
        *,
        host_id: str,
        summary: str,
        next_action: str,
        handoff_id: str | None = None,
        wrap: bool = False,
        source: str = "user",
        observed_at: int | None = None,
    ) -> SessionCurationResult:
        """Append a local attributed handoff and optionally wrap atomically."""

        self._validate_local_session_identity(host_id, session_key)
        summary = normalize_handoff_text(summary, "summary")
        next_action = normalize_handoff_text(next_action, "next_action")
        handoff_id = _canonical_uuid_id(
            handoff_id or str(uuid.uuid4()), HandoffId, "handoff_id"
        )
        if not isinstance(wrap, bool):
            raise StorageError("wrap must be boolean")
        if source not in {"user", "agent"}:
            raise StorageError("curation handoff source must be user or agent")
        timestamp = (
            now_ms()
            if observed_at is None
            else _nonnegative_integer(observed_at, "observed_at")
        )
        with self.transaction(immediate=True) as connection:
            self._local_session_row(
                connection, host_id=host_id, session_key=session_key
            )
            existing = connection.execute(
                "SELECT created_at FROM handoffs WHERE handoff_id = ?",
                (handoff_id,),
            ).fetchone()
            handoff = self._append_handoff_row(
                connection,
                session_key=session_key,
                summary=summary,
                source=source,
                source_host_id=host_id,
                next_action=next_action,
                handoff_id=handoff_id,
                sequence=None,
                created_at=None if existing is not None else timestamp,
                content_hash=None,
            )
            if wrap and existing is None:
                connection.execute(
                    "UPDATE sessions SET wrapped_at = ? WHERE session_key = ?",
                    (handoff["created_at"], session_key),
                )
            session = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
        result = _row_dict(session)
        assert result is not None
        return SessionCurationResult(result, handoff)

    def reserve_launch(
        self,
        request: Mapping[str, Any],
        *,
        request_id: str,
        lease_owner: str,
        capability_hash: str,
        expires_at: int,
        agent_capability_hash: str | None = None,
        launch_id: str | None = None,
        created_at: int | None = None,
        source_session_key: str | None = None,
        task_create: Mapping[str, Any] | None = None,
        imported_handoff: Mapping[str, Any] | None = None,
    ) -> ReservationResult:
        """Reserve a launch, optionally creating its task in the same transaction."""

        request = dict(request)
        capability_hash = _require_hash(capability_hash, "capability_hash")
        if agent_capability_hash is not None:
            agent_capability_hash = _require_hash(
                agent_capability_hash, "agent_capability_hash"
            )
        if not isinstance(lease_owner, str) or not lease_owner.strip():
            raise StorageError("lease_owner must be a non-empty string")
        _reject_controls(lease_owner, "lease_owner")
        _canonical_host_id(request["host_id"])
        _canonical_provider(request["provider"])
        request_id = _canonical_plain_uuid(request_id, "request_id")
        launch_id = _canonical_uuid_id(
            launch_id or str(uuid.uuid4()), LaunchId, "launch_id"
        )
        for field, value_type in (
            ("project_id", ProjectId),
            ("task_id", TaskId),
            ("checkout_id", CheckoutId),
            ("source_handoff_id", HandoffId),
        ):
            value = request.get(field)
            if value is not None:
                _canonical_uuid_id(value, value_type, field)
        target_session_key = request.get("target_session_key")
        if target_session_key is not None:
            _canonical_session_key(target_session_key)
        if source_session_key is not None:
            source_session_key = str(_canonical_session_key(source_session_key))
            if request["action"] != "new":
                raise StorageError(
                    "source_session_key is valid only for a new continuation"
                )
        if request["action"] == "new" and request.get("task_id") is None:
            raise TaskConflict(
                "task_required", "a new provider launch must belong to a task"
            )
        _reject_controls(request.get("cwd"), "cwd")
        if request["action"] == "manage" and any(
            request.get(field) is not None
            for field in (
                "project_id",
                "task_id",
                "checkout_id",
                "cwd",
                "source_handoff_id",
                "target_session_key",
            )
        ):
            raise StorageError("manage launch cannot target project/session context")
        timestamp = now_ms() if created_at is None else created_at
        if expires_at <= timestamp:
            raise StorageError("launch lease must expire after creation")
        normalized_task_create: dict[str, Any] | None = None
        if task_create is not None:
            if request["action"] != "new":
                raise TaskConflict(
                    "task_action_conflict", "only a new launch can create a task"
                )
            try:
                create_task_id = _canonical_uuid_id(
                    task_create["task_id"], TaskId, "task_id"
                )
                create_title = _normalize_curation_text(
                    task_create["title"], "task title", maximum=256
                )
            except KeyError as error:
                raise StorageError(
                    f"missing task creation field: {error.args[0]}"
                ) from error
            if create_task_id != request.get("task_id"):
                raise TaskConflict(
                    "task_context_conflict",
                    "created task ID does not match the launch request",
                )
            preferred_provider = task_create.get("preferred_provider")
            if preferred_provider not in {None, "codex", "claude"}:
                raise StorageError("preferred_provider must be codex or claude")
            normalized_task_create = {
                "task_id": create_task_id,
                "host_id": request["host_id"],
                "project_id": request.get("project_id"),
                "checkout_id": request.get("checkout_id"),
                "title": create_title,
                "purpose": _normalize_task_purpose(task_create.get("purpose")),
                "preferred_provider": preferred_provider,
            }
        normalized_import: dict[str, Any] | None = None
        if imported_handoff is not None:
            if normalized_task_create is None or source_session_key is not None:
                raise ContinuationError(
                    "continuation_import_context_invalid",
                    "an imported handoff requires atomic destination task creation",
                )
            try:
                source_host_id = _canonical_host_id(imported_handoff["source_host_id"])
                source_project_id = _canonical_uuid_id(
                    imported_handoff["source_project_id"],
                    ProjectId,
                    "source_project_id",
                )
                source_task_id = _canonical_uuid_id(
                    imported_handoff["source_task_id"],
                    TaskId,
                    "source_task_id",
                )
                imported_session_key = str(
                    _canonical_session_key(imported_handoff["source_session_key"])
                )
                imported_handoff_id = _canonical_uuid_id(
                    imported_handoff["handoff_id"], HandoffId, "handoff_id"
                )
                imported_sequence = _nonnegative_integer(
                    imported_handoff["sequence"], "handoff sequence"
                )
                imported_created_at = _nonnegative_integer(
                    imported_handoff["created_at"], "handoff created_at"
                )
                imported_summary = normalize_handoff_text(
                    imported_handoff["summary"], "summary"
                )
                imported_next = normalize_handoff_text(
                    imported_handoff["next_action"], "next_action"
                )
                imported_hash = _require_hash(
                    imported_handoff["content_hash"], "content_hash"
                )
            except KeyError as error:
                raise ContinuationError(
                    "continuation_import_incomplete",
                    f"missing imported handoff field: {error.args[0]}",
                ) from error
            if imported_sequence == 0:
                raise ContinuationError(
                    "continuation_import_invalid",
                    "imported handoff sequence must be positive",
                )
            parsed_imported_session = SessionKey.parse(imported_session_key)
            if parsed_imported_session.host_id != HostId(source_host_id):
                raise ContinuationError(
                    "continuation_import_host_mismatch",
                    "imported handoff session belongs to another source host",
                )
            if (
                request.get("source_handoff_id") != imported_handoff_id
                or request.get("project_id") != source_project_id
                or normalized_task_create["project_id"] != source_project_id
            ):
                raise ContinuationError(
                    "continuation_import_context_mismatch",
                    "imported handoff does not match the destination project",
                )
            if handoff_content_hash(imported_summary, imported_next) != imported_hash:
                raise ContinuationError(
                    "continuation_import_hash_mismatch",
                    "imported handoff content hash is invalid",
                )
            normalized_import = {
                "source_host_id": source_host_id,
                "source_project_id": source_project_id,
                "source_task_id": source_task_id,
                "source_session_key": imported_session_key,
                "handoff_id": imported_handoff_id,
                "sequence": imported_sequence,
                "summary": imported_summary,
                "next_action": imported_next,
                "created_at": imported_created_at,
                "content_hash": imported_hash,
            }

        with self.transaction(immediate=True) as connection:
            placeholders = ", ".join("?" for _ in _LEASED_LAUNCH_STATES)
            connection.execute(
                f"""
                UPDATE launch_intents
                SET state = 'expired', lease_owner = NULL, updated_at = ?
                WHERE state IN ({placeholders})
                  AND expires_at <= ?
                """,
                (timestamp, *_LEASED_LAUNCH_STATES, timestamp),
            )
            existing_request = connection.execute(
                """
                SELECT * FROM launch_intents
                WHERE host_id = ? AND request_id = ?
                """,
                (request["host_id"], request_id),
            ).fetchone()
            if normalized_task_create is not None and existing_request is None:
                if (
                    connection.execute(
                        "SELECT 1 FROM tasks WHERE task_id = ?",
                        (normalized_task_create["task_id"],),
                    ).fetchone()
                    is not None
                ):
                    raise TaskConflict(
                        "task_create_conflict",
                        "the requested task ID already exists without this request",
                    )
                try:
                    connection.execute(
                        """
                        INSERT INTO tasks(
                            task_id, host_id, project_id, checkout_id, title,
                            purpose, preferred_provider, status, pinned,
                            current_session_key, created_at, updated_at, closed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 0, NULL, ?, ?, NULL)
                        """,
                        (
                            normalized_task_create["task_id"],
                            normalized_task_create["host_id"],
                            normalized_task_create["project_id"],
                            normalized_task_create["checkout_id"],
                            normalized_task_create["title"],
                            normalized_task_create["purpose"],
                            normalized_task_create["preferred_provider"],
                            timestamp,
                            timestamp,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    code = (
                        "worktree_claimed"
                        if "worktree already belongs" in str(error)
                        else "task_create_conflict"
                    )
                    raise TaskConflict(code, str(error)) from error
            elif normalized_task_create is not None:
                existing_task = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?",
                    (normalized_task_create["task_id"],),
                ).fetchone()
                compared_fields = (
                    "host_id",
                    "project_id",
                    "checkout_id",
                    "title",
                    "purpose",
                    "preferred_provider",
                )
                if existing_task is None or any(
                    existing_task[field] != normalized_task_create[field]
                    for field in compared_fields
                ):
                    raise RequestConflict(
                        "request ID task creation parameters do not match"
                    )
            if normalized_import is not None:
                assert normalized_task_create is not None
                if (
                    connection.execute(
                        "SELECT 1 FROM hosts WHERE host_id = ? AND is_local = 0",
                        (normalized_import["source_host_id"],),
                    ).fetchone()
                    is None
                ):
                    raise ContinuationError(
                        "continuation_source_host_unknown",
                        "the imported handoff source host is not configured",
                    )
                handoff = self._append_handoff_row(
                    connection,
                    session_key=normalized_import["source_session_key"],
                    summary=normalized_import["summary"],
                    source="imported",
                    source_host_id=normalized_import["source_host_id"],
                    next_action=normalized_import["next_action"],
                    handoff_id=normalized_import["handoff_id"],
                    sequence=normalized_import["sequence"],
                    created_at=normalized_import["created_at"],
                    content_hash=normalized_import["content_hash"],
                )
                existing_link = connection.execute(
                    """
                    SELECT * FROM task_imported_handoffs WHERE handoff_id = ?
                    """,
                    (normalized_import["handoff_id"],),
                ).fetchone()
                expected_link = {
                    "task_id": normalized_task_create["task_id"],
                    "handoff_id": normalized_import["handoff_id"],
                    "source_task_id": normalized_import["source_task_id"],
                    "source_project_id": normalized_import["source_project_id"],
                }
                if existing_link is None:
                    try:
                        connection.execute(
                            """
                            INSERT INTO task_imported_handoffs(
                                task_id, handoff_id, source_task_id,
                                source_project_id, imported_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                expected_link["task_id"],
                                expected_link["handoff_id"],
                                expected_link["source_task_id"],
                                expected_link["source_project_id"],
                                timestamp,
                            ),
                        )
                    except sqlite3.IntegrityError as error:
                        raise ContinuationError(
                            "continuation_import_conflict",
                            "the imported handoff conflicts with destination state",
                        ) from error
                elif any(
                    existing_link[field] != value
                    for field, value in expected_link.items()
                ):
                    raise RequestConflict(
                        "request ID imported handoff parameters do not match"
                    )
                request["source_handoff_id"] = str(handoff["handoff_id"])
            if source_session_key is not None:
                if existing_request is not None:
                    exact_handoff_id = existing_request["source_handoff_id"]
                    handoff = connection.execute(
                        """
                        SELECT * FROM handoffs
                        WHERE handoff_id = ? AND session_key = ?
                        """,
                        (exact_handoff_id, source_session_key),
                    ).fetchone()
                    if handoff is None:
                        raise RequestConflict(
                            "request ID continuation source does not match"
                        )
                    request["source_handoff_id"] = exact_handoff_id
                else:
                    source = self._local_session_row(
                        connection,
                        host_id=request["host_id"],
                        session_key=source_session_key,
                    )
                    exact_handoff_id = source["latest_handoff_id"]
                    if exact_handoff_id is None:
                        raise ContinuationError(
                            "continuation_handoff_missing",
                            "The source session has no explicit handoff.",
                        )
                    handoff = connection.execute(
                        """
                        SELECT * FROM handoffs
                        WHERE handoff_id = ? AND session_key = ?
                        """,
                        (exact_handoff_id, source_session_key),
                    ).fetchone()
                    if handoff is None:
                        raise ContinuationError(
                            "continuation_handoff_inconsistent",
                            "The source session's latest handoff is inconsistent.",
                        )
                    request["source_handoff_id"] = exact_handoff_id
            if (
                request.get("source_handoff_id") is not None
                and existing_request is None
            ):
                handoff = connection.execute(
                    "SELECT * FROM handoffs WHERE handoff_id = ?",
                    (request["source_handoff_id"],),
                ).fetchone()
                if handoff is None:
                    raise ContinuationError(
                        "continuation_handoff_not_found",
                        "The selected handoff is not retained.",
                    )
                if handoff["source"] == "imported":
                    link = connection.execute(
                        """
                        SELECT * FROM task_imported_handoffs
                        WHERE task_id = ? AND handoff_id = ?
                        """,
                        (request.get("task_id"), request["source_handoff_id"]),
                    ).fetchone()
                    if link is None or link["source_project_id"] != request.get(
                        "project_id"
                    ):
                        raise ContinuationError(
                            "continuation_source_changed",
                            "The imported handoff is not linked to this task.",
                        )
                else:
                    source = self._local_session_row(
                        connection,
                        host_id=request["host_id"],
                        session_key=str(handoff["session_key"]),
                    )
                    for field in ("project_id", "checkout_id", "cwd"):
                        if source[field] != request.get(field):
                            raise ContinuationError(
                                "continuation_source_changed",
                                "The source session's project checkout changed.",
                            )
            if request["action"] == "new" and existing_request is None:
                task_id = request.get("task_id")
                if task_id is None:
                    task = None
                else:
                    task = connection.execute(
                        "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                    ).fetchone()
                if task_id is not None and task is None:
                    raise TaskConflict("task_not_found", f"unknown task: {task_id}")
                if task is not None and task["status"] != "open":
                    raise TaskConflict("task_closed", "cannot launch a closed task")
                if task is not None and any(
                    task[field] != request.get(field)
                    for field in ("host_id", "project_id", "checkout_id")
                ):
                    raise TaskConflict(
                        "task_context_conflict",
                        "launch context does not match the selected task",
                    )
                current_session_key = (
                    None if task is None else task["current_session_key"]
                )
                if (
                    task is not None
                    and current_session_key is None
                    and request.get("source_handoff_id") is not None
                    and normalized_import is None
                ):
                    raise TaskConflict(
                        "task_continuation_conflict",
                        "a task without history cannot continue another session",
                    )
                if current_session_key is not None:
                    current = connection.execute(
                        "SELECT * FROM sessions WHERE session_key = ?",
                        (current_session_key,),
                    ).fetchone()
                    if (
                        current is None
                        or current["wrapped_at"] is None
                        or current["latest_handoff_id"]
                        != request.get("source_handoff_id")
                    ):
                        raise TaskConflict(
                            "task_current_session_active",
                            "task continuation requires the current session's exact "
                            "wrapped handoff",
                        )
            elif request.get("task_id") is not None and request["action"] != "new":
                raise TaskConflict(
                    "task_action_conflict", "only new launches carry task context"
                )
            fingerprint = launch_request_fingerprint(request)
            if existing_request is not None:
                if existing_request["request_fingerprint"] != fingerprint:
                    raise RequestConflict(
                        f"request ID {request_id!r} was reused for another "
                        "launch request"
                    )
                return ReservationResult("idempotent", dict(existing_request))

            target = request.get("target_session_key")
            existing_launch: sqlite3.Row | None = None
            if target is not None:
                active_placeholders = ", ".join("?" for _ in _ACTIVE_LAUNCH_STATES)
                existing_launch = connection.execute(
                    f"""
                    SELECT * FROM launch_intents
                    WHERE target_session_key = ?
                      AND state IN ({active_placeholders})
                    ORDER BY created_at, launch_id
                    LIMIT 1
                    """,
                    (target, *_ACTIVE_LAUNCH_STATES),
                ).fetchone()
            elif request["action"] == "manage":
                existing_launch = connection.execute(
                    f"""
                    SELECT * FROM launch_intents
                    WHERE host_id = ? AND provider = ? AND action = 'manage'
                      AND state IN ({placeholders})
                    ORDER BY created_at, launch_id
                    LIMIT 1
                    """,
                    (
                        request["host_id"],
                        request["provider"],
                        *_LEASED_LAUNCH_STATES,
                    ),
                ).fetchone()
            if existing_launch is not None:
                return ReservationResult("existing", dict(existing_launch))

            try:
                connection.execute(
                    """
                INSERT INTO launch_intents(
                    launch_id, request_id, request_fingerprint, host_id,
                    provider, action, project_id, task_id, checkout_id, cwd,
                    source_handoff_id, target_session_key, transport, state,
                    lease_owner, capability_hash, agent_capability_hash,
                    created_at, updated_at, expires_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved',
                    ?, ?, ?, ?, ?, ?
                )
                    """,
                    (
                        launch_id,
                        request_id,
                        fingerprint,
                        request["host_id"],
                        request["provider"],
                        request["action"],
                        request.get("project_id"),
                        request.get("task_id"),
                        request.get("checkout_id"),
                        request.get("cwd"),
                        request.get("source_handoff_id"),
                        request.get("target_session_key"),
                        request["transport"],
                        lease_owner,
                        capability_hash,
                        agent_capability_hash,
                        timestamp,
                        timestamp,
                        expires_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                if "launch_intents.task_id" in str(error):
                    raise TaskConflict(
                        "task_launch_pending",
                        "the task already has a pending launch",
                    ) from error
                raise
            row = connection.execute(
                "SELECT * FROM launch_intents WHERE launch_id = ?", (launch_id,)
            ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return ReservationResult("created", result)

    def get_launch(self, launch_id: str) -> dict[str, Any] | None:
        return _row_dict(
            self.connection.execute(
                "SELECT * FROM launch_intents WHERE launch_id = ?", (launch_id,)
            ).fetchone()
        )

    def list_launches(
        self, *, host_id: str | None = None, target_session_key: str | None = None
    ) -> list[dict[str, Any]]:
        if host_id is not None:
            _canonical_host_id(host_id)
        if target_session_key is not None:
            _canonical_session_key(target_session_key)
        clauses: list[str] = []
        values: list[str] = []
        if host_id is not None:
            clauses.append("host_id = ?")
            values.append(host_id)
        if target_session_key is not None:
            clauses.append("target_session_key = ?")
            values.append(target_session_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM launch_intents {where} ORDER BY created_at, launch_id",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    def transition_launch(
        self,
        launch_id: str,
        state: str,
        *,
        lease_owner: str | None = None,
        observed_at: int | None = None,
        surface_id: str | None = None,
        failure_code: str | None = None,
        failure_detail: str | None = None,
    ) -> dict[str, Any]:
        launch_id = _canonical_uuid_id(launch_id, LaunchId, "launch_id")
        if surface_id is not None:
            surface_id = _canonical_uuid_id(surface_id, SurfaceId, "surface_id")
        timestamp = now_ms() if observed_at is None else observed_at
        _reject_controls(failure_code, "failure_code")
        _reject_controls(failure_detail, "failure_detail", allow_multiline=True)
        with self.transaction(immediate=True) as connection:
            launch = connection.execute(
                "SELECT * FROM launch_intents WHERE launch_id = ?", (launch_id,)
            ).fetchone()
            if launch is None:
                raise StorageError(f"unknown launch: {launch_id}")
            if timestamp < int(launch["updated_at"]):
                raise StorageError("stale launch transition")
            if state == "bound":
                raise StorageError(
                    "bound transitions require atomic provider-session binding"
                )
            if (
                surface_id is not None
                and launch["surface_id"] is not None
                and surface_id != launch["surface_id"]
            ):
                raise IdentityConflict(f"launch {launch_id!r} changed surface_id")
            if (
                surface_id is not None
                and launch["surface_id"] is None
                and state != "surface_ready"
            ):
                raise StorageError(
                    "launch surface can only be assigned by surface_ready"
                )
            if state == "expired":
                if launch["expires_at"] is None or timestamp < int(
                    launch["expires_at"]
                ):
                    raise StorageError("cannot expire a live launch lease")
            else:
                self._assert_launch_lease(launch, lease_owner, timestamp)
            effective_surface_id = surface_id or launch["surface_id"]
            if (
                state in _SURFACE_REQUIRED_LAUNCH_STATES
                and effective_surface_id is None
            ):
                raise StorageError(f"{state} transition requires a surface")
            if effective_surface_id is not None and (
                surface_id is not None or state in _SURFACE_REQUIRED_LAUNCH_STATES
            ):
                surface = connection.execute(
                    "SELECT * FROM surfaces WHERE surface_id = ?",
                    (effective_surface_id,),
                ).fetchone()
                if surface is None:
                    raise StorageError(f"unknown surface: {effective_surface_id}")
                expected_role = (
                    "provider_manager" if launch["action"] == "manage" else "session"
                )
                if (
                    surface["host_id"] != launch["host_id"]
                    or surface["provider"] != launch["provider"]
                    or surface["role"] != expected_role
                    or surface["launch_id"] != launch_id
                    or surface["retired_at"] is not None
                ):
                    raise IdentityConflict(
                        "surface does not match launch host, provider, role, and lease"
                    )
            terminal = state in _TERMINAL_LAUNCH_STATES
            cursor = connection.execute(
                """
                UPDATE launch_intents
                SET state = ?, updated_at = ?,
                    surface_id = COALESCE(?, surface_id),
                    failure_code = ?, failure_detail = ?, lease_owner = ?
                WHERE launch_id = ?
                """,
                (
                    state,
                    timestamp,
                    surface_id,
                    failure_code,
                    failure_detail,
                    None if terminal else launch["lease_owner"],
                    launch_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StorageError(f"unknown launch: {launch_id}")
            row = connection.execute(
                "SELECT * FROM launch_intents WHERE launch_id = ?", (launch_id,)
            ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return result

    @staticmethod
    def _assert_launch_lease(
        launch: sqlite3.Row,
        lease_owner: str | None,
        observed_at: int,
    ) -> None:
        if lease_owner is None or lease_owner != launch["lease_owner"]:
            raise StorageError("launch lease is owned by a different worker")
        if launch["expires_at"] is None or observed_at >= int(launch["expires_at"]):
            raise StorageError("launch lease has expired")

    def renew_launch_lease(
        self,
        launch_id: str,
        *,
        lease_owner: str,
        expires_at: int,
        observed_at: int | None = None,
    ) -> dict[str, Any]:
        launch_id = _canonical_uuid_id(launch_id, LaunchId, "launch_id")
        timestamp = now_ms() if observed_at is None else observed_at
        if expires_at <= timestamp:
            raise StorageError("renewed launch lease must expire in the future")
        with self.transaction(immediate=True) as connection:
            launch = connection.execute(
                "SELECT * FROM launch_intents WHERE launch_id = ?", (launch_id,)
            ).fetchone()
            if launch is None:
                raise StorageError(f"unknown launch: {launch_id}")
            if launch["state"] not in _LEASED_LAUNCH_STATES:
                raise StorageError("terminal launch lease cannot be renewed")
            if timestamp < int(launch["updated_at"]):
                raise StorageError("stale launch lease renewal")
            self._assert_launch_lease(launch, lease_owner, timestamp)
            connection.execute(
                """
                UPDATE launch_intents
                SET expires_at = ?, updated_at = ?
                WHERE launch_id = ?
                """,
                (expires_at, timestamp, launch_id),
            )
            row = connection.execute(
                "SELECT * FROM launch_intents WHERE launch_id = ?", (launch_id,)
            ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return result

    def bind_provider_session(
        self,
        launch_id: str,
        session: Mapping[str, Any],
        *,
        lease_owner: str,
        observed_at: int | None = None,
    ) -> LaunchBindingResult:
        """Atomically persist provider identity, surface binding, and launch state."""

        launch_id = _canonical_uuid_id(launch_id, LaunchId, "launch_id")
        timestamp = now_ms() if observed_at is None else observed_at
        required = {"session_key", "host_id", "provider", "provider_session_id"} - set(
            session
        )
        if required:
            raise StorageError(f"missing provider session fields: {sorted(required)}")

        with self.transaction(immediate=True) as connection:
            launch = connection.execute(
                "SELECT * FROM launch_intents WHERE launch_id = ?", (launch_id,)
            ).fetchone()
            if launch is None:
                raise StorageError(f"unknown launch: {launch_id}")
            if launch["action"] == "manage":
                raise StorageError("manager launches do not bind provider sessions")
            if launch["state"] != "provider_started":
                raise StorageError(
                    "provider session binding requires a provider_started launch"
                )
            if timestamp < int(launch["updated_at"]):
                raise StorageError("stale provider session binding")
            self._assert_launch_lease(launch, lease_owner, timestamp)
            if (
                session["host_id"] != launch["host_id"]
                or session["provider"] != launch["provider"]
            ):
                raise IdentityConflict(
                    "provider session does not match launch host and provider"
                )

            observed_session = dict(session)
            observed_session.setdefault("first_observed_at", timestamp)
            observed_session.setdefault("last_observed_at", timestamp)
            if "name" in observed_session:
                existing_session = connection.execute(
                    "SELECT * FROM sessions WHERE session_key = ?",
                    (str(observed_session["session_key"]),),
                ).fetchone()
                observed_session = self._provider_name_update(
                    observed_session,
                    existing_session,
                )
            if launch["action"] == "new":
                observed_session.update(
                    project_id=launch["project_id"],
                    task_id=launch["task_id"],
                    checkout_id=launch["checkout_id"],
                    cwd=launch["cwd"],
                    metadata_source="launch",
                    continued_from_handoff_id=launch["source_handoff_id"],
                )
            elif launch["action"] in {"resume", "attach", "history"}:
                observed_session["wrapped_at"] = None
            stored_session = self._upsert_session_row(connection, observed_session)
            if launch["action"] == "new" and launch["task_id"] is not None:
                connection.execute(
                    """
                    UPDATE tasks
                    SET current_session_key = ?, updated_at = ?
                    WHERE task_id = ? AND status = 'open'
                    """,
                    (stored_session["session_key"], timestamp, launch["task_id"]),
                )

            expected_session_key = launch["target_session_key"]
            mismatch = (
                launch["action"] in {"resume", "attach"}
                and stored_session["session_key"] != expected_session_key
            )
            surface = (
                connection.execute(
                    "SELECT * FROM surfaces WHERE surface_id = ?",
                    (launch["surface_id"],),
                ).fetchone()
                if launch["surface_id"] is not None
                else None
            )

            if mismatch:
                if surface is not None and surface["launch_id"] == launch_id:
                    connection.execute(
                        "UPDATE sessions SET surface_id = NULL WHERE surface_id = ?",
                        (surface["surface_id"],),
                    )
                    connection.execute(
                        """
                        UPDATE surfaces
                        SET current_session_key = NULL,
                            binding_confidence = 'unknown',
                            last_observed_at = ?
                        WHERE surface_id = ?
                        """,
                        (timestamp, surface["surface_id"]),
                    )
                connection.execute(
                    """
                    UPDATE launch_intents
                    SET state = 'failed', lease_owner = NULL, updated_at = ?,
                        failure_code = 'provider_identity_mismatch',
                        failure_detail = ?
                    WHERE launch_id = ?
                    """,
                    (
                        timestamp,
                        "provider session did not match the requested target",
                        launch_id,
                    ),
                )
                failed_launch = connection.execute(
                    "SELECT * FROM launch_intents WHERE launch_id = ?", (launch_id,)
                ).fetchone()
                failed_surface = (
                    connection.execute(
                        "SELECT * FROM surfaces WHERE surface_id = ?",
                        (surface["surface_id"],),
                    ).fetchone()
                    if surface is not None
                    else None
                )
                assert failed_launch is not None
                return LaunchBindingResult(
                    "provider_identity_mismatch",
                    dict(failed_launch),
                    stored_session,
                    _row_dict(failed_surface),
                )

            if surface is None:
                raise StorageError("provider-started launch has no surface")
            expected_role = "session"
            if (
                surface["host_id"] != launch["host_id"]
                or surface["provider"] != launch["provider"]
                or surface["role"] != expected_role
                or surface["launch_id"] != launch_id
                or surface["retired_at"] is not None
            ):
                raise IdentityConflict(
                    "surface does not match launch host, provider, role, and lease"
                )
            stored_surface = self._bind_surface_row(
                connection,
                surface,
                stored_session,
                confidence="confirmed",
                observed_at=timestamp,
            )
            connection.execute(
                """
                UPDATE launch_intents
                SET state = 'bound', target_session_key = ?, lease_owner = NULL,
                    updated_at = ?, failure_code = NULL, failure_detail = NULL
                WHERE launch_id = ?
                """,
                (stored_session["session_key"], timestamp, launch_id),
            )
            bound_launch = connection.execute(
                "SELECT * FROM launch_intents WHERE launch_id = ?", (launch_id,)
            ).fetchone()
            bound_session = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?",
                (stored_session["session_key"],),
            ).fetchone()
            assert bound_launch is not None and bound_session is not None
            return LaunchBindingResult(
                "bound",
                dict(bound_launch),
                dict(bound_session),
                stored_surface,
            )

    def expire_launches(self, *, observed_at: int | None = None) -> int:
        timestamp = now_ms() if observed_at is None else observed_at
        placeholders = ", ".join("?" for _ in _LEASED_LAUNCH_STATES)
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                f"""
                UPDATE launch_intents
                SET state = 'expired', lease_owner = NULL, updated_at = ?
                WHERE state IN ({placeholders}) AND expires_at <= ?
                """,
                (timestamp, *_LEASED_LAUNCH_STATES, timestamp),
            )
            return cursor.rowcount

    def upsert_surface(self, surface: Mapping[str, Any]) -> dict[str, Any]:
        """Insert or refresh non-binding metadata for an active surface."""

        required = {
            "surface_id",
            "host_id",
            "provider",
            "transport",
            "transport_locator",
            "role",
        } - set(surface)
        if required:
            raise StorageError(f"missing surface fields: {sorted(required)}")
        _canonical_host_id(surface["host_id"])
        _canonical_provider(surface["provider"])
        surface_id = _canonical_uuid_id(surface["surface_id"], SurfaceId, "surface_id")
        launch_id = surface.get("launch_id")
        if launch_id is not None:
            _canonical_uuid_id(launch_id, LaunchId, "launch_id")
        current_session_key = surface.get("current_session_key")
        binding_confidence = surface.get("binding_confidence", "unknown")
        if current_session_key is not None or binding_confidence != "unknown":
            raise StorageError("surface bindings must be changed with bind_surface")
        if surface.get("retired_at") is not None:
            raise StorageError("surface retirement must use retire_surface")
        _reject_mapping_controls(surface, ("transport_locator", "workspace_id"))
        timestamp = int(surface.get("last_observed_at", now_ms()))
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
            ).fetchone()
            if existing is not None:
                for field in ("host_id", "provider", "role"):
                    if surface[field] != existing[field]:
                        raise IdentityConflict(
                            f"surface {surface_id!r} changed {field}"
                        )
                if (
                    launch_id is not None
                    and existing["launch_id"] is not None
                    and launch_id != existing["launch_id"]
                ):
                    raise IdentityConflict(f"surface {surface_id!r} changed launch_id")
                if existing["retired_at"] is not None:
                    raise StorageError("retired surfaces cannot be refreshed")
                if timestamp < int(existing["last_observed_at"]):
                    raise StorageError(
                        f"stale surface observation for {surface_id!r}: "
                        f"{timestamp} < {existing['last_observed_at']}"
                    )
                if (
                    "current_session_key" in surface
                    and surface["current_session_key"]
                    != existing["current_session_key"]
                ) or (
                    "binding_confidence" in surface
                    and surface["binding_confidence"] != existing["binding_confidence"]
                ):
                    raise StorageError(
                        "surface bindings must be changed with bind_surface"
                    )
                mutable_fields = {
                    "transport": surface["transport"],
                    "transport_locator": surface["transport_locator"],
                }
                if "workspace_id" in surface:
                    mutable_fields["workspace_id"] = surface["workspace_id"]
                if "client_attached" in surface:
                    mutable_fields["client_attached"] = int(
                        bool(surface["client_attached"])
                    )
                if launch_id is not None:
                    mutable_fields["launch_id"] = launch_id
                if timestamp == int(existing["last_observed_at"]) and any(
                    existing[field] != value for field, value in mutable_fields.items()
                ):
                    raise StorageError(
                        "conflicting surface observation at the same timestamp"
                    )

                updates = {
                    "transport": surface["transport"],
                    "transport_locator": surface["transport_locator"],
                    "last_observed_at": timestamp,
                }
                if "workspace_id" in surface:
                    updates["workspace_id"] = surface["workspace_id"]
                if "client_attached" in surface:
                    updates["client_attached"] = int(bool(surface["client_attached"]))
                if existing["launch_id"] is None and launch_id is not None:
                    updates["launch_id"] = launch_id
                assignments = ", ".join(f"{field} = ?" for field in updates)
                connection.execute(
                    f"UPDATE surfaces SET {assignments} WHERE surface_id = ?",
                    (*updates.values(), surface_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO surfaces(
                        surface_id, host_id, provider, transport, transport_locator,
                        workspace_id, role, current_session_key, binding_confidence,
                        launch_id, created_at, last_observed_at, client_attached,
                        retired_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'unknown', ?, ?, ?, ?, NULL)
                    """,
                    (
                        surface_id,
                        surface["host_id"],
                        surface["provider"],
                        surface["transport"],
                        surface["transport_locator"],
                        surface.get("workspace_id"),
                        surface["role"],
                        surface.get("launch_id"),
                        int(surface.get("created_at", timestamp)),
                        timestamp,
                        int(bool(surface.get("client_attached", False))),
                    ),
                )
            row = connection.execute(
                "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
            ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return result

    def activate_launch_surface(
        self,
        launch_id: str,
        surface: Mapping[str, Any],
        *,
        lease_owner: str,
        observed_at: int | None = None,
    ) -> LaunchSurfaceResult:
        """Atomically insert a launch surface and make its bootstrap waitable."""

        launch_id = _canonical_uuid_id(launch_id, LaunchId, "launch_id")
        required = {
            "surface_id",
            "host_id",
            "provider",
            "transport",
            "transport_locator",
            "role",
        } - set(surface)
        if required:
            raise StorageError(f"missing surface fields: {sorted(required)}")
        surface_id = _canonical_uuid_id(surface["surface_id"], SurfaceId, "surface_id")
        _canonical_host_id(surface["host_id"])
        _canonical_provider(surface["provider"])
        if surface["transport"] != "tmux":
            raise StorageError("launch surface transport must be tmux")
        if surface["role"] not in {"session", "provider_manager"}:
            raise StorageError("unsupported launch surface role")
        if surface.get("launch_id", launch_id) != launch_id:
            raise IdentityConflict("surface launch_id does not match launch")
        if (
            any(
                surface.get(field) is not None
                for field in ("current_session_key", "retired_at")
            )
            or surface.get("binding_confidence", "unknown") != "unknown"
        ):
            raise StorageError("waiting launch surface cannot be bound or retired")
        _reject_mapping_controls(surface, ("transport_locator", "workspace_id"))
        timestamp = now_ms() if observed_at is None else observed_at
        created_at = int(surface.get("created_at", timestamp))
        if created_at > timestamp:
            raise StorageError("surface creation time is after its observation")

        with self.transaction(immediate=True) as connection:
            launch = connection.execute(
                "SELECT * FROM launch_intents WHERE launch_id = ?", (launch_id,)
            ).fetchone()
            if launch is None:
                raise StorageError(f"unknown launch: {launch_id}")
            if launch["state"] != "reserved":
                raise StorageError("launch is not awaiting surface creation")
            if timestamp < int(launch["updated_at"]):
                raise StorageError("stale launch surface observation")
            self._assert_launch_lease(launch, lease_owner, timestamp)
            expected_role = (
                "provider_manager" if launch["action"] == "manage" else "session"
            )
            if (
                surface["host_id"] != launch["host_id"]
                or surface["provider"] != launch["provider"]
                or surface["transport"] != launch["transport"]
                or surface["role"] != expected_role
            ):
                raise IdentityConflict(
                    "surface does not match launch host, provider, transport, and role"
                )
            connection.execute(
                """
                INSERT INTO surfaces(
                    surface_id, host_id, provider, transport, transport_locator,
                    workspace_id, role, current_session_key, binding_confidence,
                    launch_id, created_at, last_observed_at, client_attached,
                    retired_at
                ) VALUES (?, ?, ?, 'tmux', ?, ?, ?, NULL, 'unknown', ?, ?, ?, ?, NULL)
                """,
                (
                    surface_id,
                    surface["host_id"],
                    surface["provider"],
                    surface["transport_locator"],
                    surface.get("workspace_id"),
                    surface["role"],
                    launch_id,
                    created_at,
                    timestamp,
                    int(bool(surface.get("client_attached", False))),
                ),
            )
            connection.execute(
                """
                UPDATE launch_intents
                SET state = 'surface_ready', surface_id = ?, updated_at = ?
                WHERE launch_id = ?
                """,
                (surface_id, timestamp, launch_id),
            )
            connection.execute(
                """
                UPDATE launch_intents
                SET state = 'waiting_for_client', updated_at = ?
                WHERE launch_id = ?
                """,
                (timestamp, launch_id),
            )
            stored_launch = connection.execute(
                "SELECT * FROM launch_intents WHERE launch_id = ?", (launch_id,)
            ).fetchone()
            stored_surface = connection.execute(
                "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
            ).fetchone()
        assert stored_launch is not None and stored_surface is not None
        return LaunchSurfaceResult(dict(stored_launch), dict(stored_surface))

    def adopt_bound_surface(
        self,
        surface: Mapping[str, Any],
        session_key: str,
        *,
        observed_at: int | None = None,
    ) -> dict[str, Any]:
        """Atomically register and bind one externally created live surface."""

        parsed_session_key = _canonical_session_key(session_key)
        required = {
            "surface_id",
            "host_id",
            "provider",
            "transport",
            "transport_locator",
            "role",
        } - set(surface)
        if required:
            raise StorageError(f"missing surface fields: {sorted(required)}")
        surface_id = _canonical_uuid_id(surface["surface_id"], SurfaceId, "surface_id")
        _canonical_host_id(surface["host_id"])
        _canonical_provider(surface["provider"])
        if surface["transport"] != "tmux" or surface["role"] != "session":
            raise StorageError("adopted surface must be a tmux session surface")
        if (
            any(
                surface.get(field) is not None
                for field in (
                    "launch_id",
                    "current_session_key",
                    "retired_at",
                )
            )
            or surface.get("binding_confidence", "unknown") != "unknown"
        ):
            raise StorageError("adopted surface must be unbound and launch-free")
        _reject_mapping_controls(surface, ("transport_locator", "workspace_id"))
        timestamp = now_ms() if observed_at is None else observed_at
        created_at = int(surface.get("created_at", timestamp))
        if created_at > timestamp:
            raise StorageError("surface creation time is after its observation")

        with self.transaction(immediate=True) as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?",
                (str(parsed_session_key),),
            ).fetchone()
            if session is None:
                raise StorageError(f"unknown session: {parsed_session_key}")
            if session["surface_id"] is not None:
                raise StorageError("session already has a managed surface")
            if (
                session["host_id"] != surface["host_id"]
                or session["provider"] != surface["provider"]
            ):
                raise IdentityConflict("surface and session host/provider do not match")
            connection.execute(
                """
                INSERT INTO surfaces(
                    surface_id, host_id, provider, transport, transport_locator,
                    workspace_id, role, current_session_key, binding_confidence,
                    launch_id, created_at, last_observed_at, client_attached,
                    retired_at
                ) VALUES (?, ?, ?, 'tmux', ?, ?, 'session', NULL, 'unknown',
                          NULL, ?, ?, ?, NULL)
                """,
                (
                    surface_id,
                    surface["host_id"],
                    surface["provider"],
                    surface["transport_locator"],
                    surface.get("workspace_id"),
                    created_at,
                    timestamp,
                    int(bool(surface.get("client_attached", False))),
                ),
            )
            stored = connection.execute(
                "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
            ).fetchone()
            assert stored is not None
            return self._bind_surface_row(
                connection,
                stored,
                session,
                confidence="confirmed",
                observed_at=timestamp,
            )

    def get_surface(self, surface_id: str) -> dict[str, Any] | None:
        surface_id = _canonical_uuid_id(surface_id, SurfaceId, "surface_id")
        return _row_dict(
            self.connection.execute(
                "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
            ).fetchone()
        )

    def list_surfaces(
        self, *, host_id: str | None = None, session_key: str | None = None
    ) -> list[dict[str, Any]]:
        if host_id is not None:
            _canonical_host_id(host_id)
        if session_key is not None:
            _canonical_session_key(session_key)
        clauses: list[str] = []
        values: list[str] = []
        if host_id is not None:
            clauses.append("host_id = ?")
            values.append(host_id)
        if session_key is not None:
            clauses.append("current_session_key = ?")
            values.append(session_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM surfaces {where} ORDER BY surface_id", values
        ).fetchall()
        return [dict(row) for row in rows]

    def bind_surface(
        self,
        surface_id: str,
        session_key: str,
        *,
        confidence: str,
        observed_at: int | None = None,
    ) -> dict[str, Any]:
        surface_id = _canonical_uuid_id(surface_id, SurfaceId, "surface_id")
        _canonical_session_key(session_key)
        if confidence != "confirmed":
            raise StorageError("only confirmed evidence can bind a session surface")
        timestamp = now_ms() if observed_at is None else observed_at
        with self.transaction(immediate=True) as connection:
            surface = connection.execute(
                "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
            ).fetchone()
            if surface is None:
                raise StorageError(f"unknown surface: {surface_id}")
            if surface["role"] == "provider_manager":
                raise StorageError("provider-manager surfaces cannot bind a session")
            if surface["retired_at"] is not None:
                raise StorageError("retired surfaces cannot bind a session")
            if timestamp < int(surface["last_observed_at"]):
                raise StorageError("stale surface binding observation")
            if surface["launch_id"] is not None:
                launch = connection.execute(
                    "SELECT state FROM launch_intents WHERE launch_id = ?",
                    (surface["launch_id"],),
                ).fetchone()
                if launch is None or launch["state"] != "bound":
                    raise StorageError(
                        "pending launch surfaces require atomic provider binding"
                    )
            session = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
            if session is None:
                raise StorageError(f"unknown session: {session_key}")
            if (
                session["host_id"] != surface["host_id"]
                or session["provider"] != surface["provider"]
            ):
                raise IdentityConflict("surface and session host/provider do not match")
            return self._bind_surface_row(
                connection,
                surface,
                session,
                confidence=confidence,
                observed_at=timestamp,
            )

    @staticmethod
    def _bind_surface_row(
        connection: sqlite3.Connection,
        surface: sqlite3.Row,
        session: sqlite3.Row | Mapping[str, Any],
        *,
        confidence: str,
        observed_at: int,
    ) -> dict[str, Any]:
        surface_id = str(surface["surface_id"])
        session_key = str(session["session_key"])
        surface_observed_at = int(surface["last_observed_at"])
        if observed_at < surface_observed_at:
            raise StorageError("stale surface binding observation")
        previous_surface_id = session["surface_id"]
        if observed_at == surface_observed_at:
            pointed_session = connection.execute(
                "SELECT session_key FROM sessions WHERE surface_id = ?",
                (surface_id,),
            ).fetchone()
            if (
                surface["current_session_key"] == session_key
                and surface["binding_confidence"] == confidence
                and previous_surface_id == surface_id
                and pointed_session is not None
                and pointed_session["session_key"] == session_key
            ):
                result = _row_dict(surface)
                assert result is not None
                return result
            pristine_binding = (
                surface["current_session_key"] is None
                and surface["binding_confidence"] == "unknown"
                and previous_surface_id is None
                and pointed_session is None
            )
            if not pristine_binding:
                raise StorageError(
                    "conflicting target-surface binding observation "
                    "at the same timestamp"
                )
        if previous_surface_id is not None and previous_surface_id != surface_id:
            previous_surface = connection.execute(
                "SELECT * FROM surfaces WHERE surface_id = ?",
                (previous_surface_id,),
            ).fetchone()
            if previous_surface is not None and observed_at < int(
                previous_surface["last_observed_at"]
            ):
                raise StorageError("stale previous-surface binding observation")
            if previous_surface is not None and observed_at == int(
                previous_surface["last_observed_at"]
            ):
                raise StorageError(
                    "conflicting previous-surface binding observation "
                    "at the same timestamp"
                )
            connection.execute(
                """
                UPDATE surfaces
                SET current_session_key = NULL, binding_confidence = 'unknown',
                    last_observed_at = ?
                WHERE surface_id = ? AND current_session_key = ?
                """,
                (observed_at, previous_surface_id, session_key),
            )
        connection.execute(
            "UPDATE sessions SET surface_id = NULL WHERE surface_id = ?",
            (surface_id,),
        )
        connection.execute(
            """
            UPDATE surfaces
            SET current_session_key = ?, binding_confidence = ?,
                last_observed_at = ?
            WHERE surface_id = ?
            """,
            (session_key, confidence, observed_at, surface_id),
        )
        connection.execute(
            "UPDATE sessions SET surface_id = ? WHERE session_key = ?",
            (surface_id, session_key),
        )
        row = connection.execute(
            "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
        ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return result

    def retire_surface(
        self, surface_id: str, *, observed_at: int | None = None
    ) -> dict[str, Any]:
        surface_id = _canonical_uuid_id(surface_id, SurfaceId, "surface_id")
        timestamp = now_ms() if observed_at is None else observed_at
        with self.transaction(immediate=True) as connection:
            surface = connection.execute(
                "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
            ).fetchone()
            if surface is None:
                raise StorageError(f"unknown surface: {surface_id}")
            if timestamp < int(surface["last_observed_at"]):
                raise StorageError("stale surface retirement observation")
            connection.execute(
                "UPDATE sessions SET surface_id = NULL WHERE surface_id = ?",
                (surface_id,),
            )
            connection.execute(
                """
                UPDATE surfaces
                SET current_session_key = NULL, binding_confidence = 'unknown',
                    client_attached = 0, retired_at = COALESCE(retired_at, ?),
                    last_observed_at = ?
                WHERE surface_id = ?
                """,
                (timestamp, timestamp, surface_id),
            )
            row = connection.execute(
                "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
            ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return result

    @staticmethod
    def _hook_duplicate_result(
        connection: sqlite3.Connection,
        event: _PreparedHookEvent,
    ) -> HookIngestionResult | None:
        duplicate = connection.execute(
            """
            SELECT * FROM events
            WHERE host_id = ? AND provider = ? AND idempotency_key = ?
            """,
            (event.host_id, event.provider, event.idempotency_key),
        ).fetchone()
        if duplicate is None:
            return None
        if duplicate["payload_hash"] != event.payload_hash:
            raise IdentityConflict(
                "hook idempotency key was reused for different content"
            )
        session = connection.execute(
            "SELECT * FROM sessions WHERE session_key = ?", (event.session_key,)
        ).fetchone()
        if session is None:
            raise IdentityConflict("hook replay references a missing session")
        runtime = connection.execute(
            """
            SELECT * FROM runtime_observations
            WHERE host_id = ? AND provider = ? AND observation_key = ?
            """,
            (
                event.host_id,
                event.provider,
                f"event:{event.idempotency_key}",
            ),
        ).fetchone()
        launch = (
            connection.execute(
                "SELECT * FROM launch_intents WHERE launch_id = ?",
                (event.launch_id,),
            ).fetchone()
            if event.launch_id is not None
            else None
        )
        surface = (
            connection.execute(
                "SELECT * FROM surfaces WHERE surface_id = ?", (event.surface_id,)
            ).fetchone()
            if event.surface_id is not None
            else None
        )
        return HookIngestionResult(
            "duplicate",
            dict(duplicate),
            dict(session),
            _row_dict(runtime),
            _row_dict(launch),
            _row_dict(surface),
        )

    @staticmethod
    def _ensure_hook_host(
        connection: sqlite3.Connection, event: _PreparedHookEvent
    ) -> None:
        host = connection.execute(
            "SELECT * FROM hosts WHERE host_id = ?", (event.host_id,)
        ).fetchone()
        if host is None:
            connection.execute(
                """
                INSERT INTO hosts(
                    host_id, display_name, is_local, created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?)
                """,
                (
                    event.host_id,
                    event.host_display_name,
                    event.observed_at,
                    event.observed_at,
                ),
            )
        elif not bool(host["is_local"]):
            connection.execute(
                """
                UPDATE hosts
                SET display_name = ?, is_local = 1, updated_at = ?
                WHERE host_id = ?
                """,
                (
                    event.host_display_name,
                    max(event.observed_at, int(host["updated_at"])),
                    event.host_id,
                ),
            )

    @staticmethod
    def _resolve_hook_launch(
        connection: sqlite3.Connection, event: _PreparedHookEvent
    ) -> _HookLaunchContext:
        if event.launch_id is None:
            return _HookLaunchContext(None, None, False)
        launch = connection.execute(
            "SELECT * FROM launch_intents WHERE launch_id = ?", (event.launch_id,)
        ).fetchone()
        if launch is None:
            raise StorageError("unknown hook launch")
        surface = connection.execute(
            "SELECT * FROM surfaces WHERE surface_id = ?", (event.surface_id,)
        ).fetchone()
        if surface is None:
            raise StorageError("unknown hook surface")
        if (
            launch["host_id"] != event.host_id
            or launch["provider"] != event.provider
            or launch["surface_id"] != event.surface_id
            or surface["host_id"] != event.host_id
            or surface["provider"] != event.provider
            or surface["role"] != "session"
            or surface["launch_id"] != event.launch_id
            or surface["retired_at"] is not None
        ):
            raise IdentityConflict("hook launch and surface identity do not match")
        if event.observed_at < int(launch["updated_at"]):
            raise StorageError("stale hook launch observation")
        mismatch = False
        if launch["state"] == "provider_started":
            if launch["lease_owner"] is None or event.observed_at >= int(
                launch["expires_at"]
            ):
                raise StorageError("hook launch lease has expired")
            mismatch = (
                launch["action"] in {"resume", "attach"}
                and launch["target_session_key"] != event.session_key
            )
        elif launch["state"] == "bound":
            if (
                launch["target_session_key"] != event.session_key
                or surface["current_session_key"] != event.session_key
                or surface["binding_confidence"] != "confirmed"
            ):
                raise IdentityConflict("bound hook launch changed session identity")
        elif (
            launch["state"] == "failed"
            and launch["failure_code"] == "provider_identity_mismatch"
        ):
            mismatch = launch["target_session_key"] != event.session_key
            if not mismatch:
                raise IdentityConflict("failed hook launch changed session identity")
        else:
            raise StorageError("hook launch is not ready for provider binding")
        return _HookLaunchContext(launch, surface, mismatch)

    @classmethod
    def _ensure_hook_session(
        cls,
        connection: sqlite3.Connection,
        event: _PreparedHookEvent,
        launch: sqlite3.Row | None,
    ) -> None:
        existing = connection.execute(
            "SELECT * FROM sessions WHERE session_key = ?", (event.session_key,)
        ).fetchone()
        if existing is not None:
            if (
                existing["host_id"] != event.host_id
                or existing["provider"] != event.provider
                or existing["provider_session_id"] != event.provider_session_id
            ):
                raise IdentityConflict("hook session changed immutable identity")
            return
        initial_cwd = (
            str(launch["cwd"])
            if launch is not None
            and launch["action"] == "new"
            and launch["cwd"] is not None
            else event.cwd
        )
        initial: dict[str, Any] = {
            "session_key": event.session_key,
            "host_id": event.host_id,
            "provider": event.provider,
            "provider_session_id": event.provider_session_id,
            "cwd": initial_cwd,
            "first_observed_at": event.observed_at,
            "last_observed_at": event.observed_at,
            "metadata_source": "hook",
        }
        if launch is not None and launch["action"] == "new":
            initial.update(
                project_id=launch["project_id"],
                task_id=launch["task_id"],
                checkout_id=launch["checkout_id"],
                metadata_source="launch",
                continued_from_handoff_id=launch["source_handoff_id"],
            )
        cls._upsert_session_row(connection, initial)
        if (
            launch is not None
            and launch["action"] == "new"
            and launch["task_id"] is not None
        ):
            connection.execute(
                """
                UPDATE tasks
                SET current_session_key = ?, updated_at = ?
                WHERE task_id = ? AND status = 'open'
                """,
                (event.session_key, event.observed_at, launch["task_id"]),
            )

    @classmethod
    def _apply_hook_launch_binding(
        cls,
        connection: sqlite3.Connection,
        event: _PreparedHookEvent,
        context: _HookLaunchContext,
    ) -> None:
        launch = context.launch
        surface = context.surface
        if launch is None or launch["state"] != "provider_started":
            return
        if context.mismatch:
            connection.execute(
                "UPDATE sessions SET surface_id = NULL WHERE surface_id = ?",
                (event.surface_id,),
            )
            connection.execute(
                """
                UPDATE surfaces
                SET current_session_key = NULL,
                    binding_confidence = 'unknown',
                    last_observed_at = ?
                WHERE surface_id = ?
                """,
                (event.observed_at, event.surface_id),
            )
            connection.execute(
                """
                UPDATE launch_intents
                SET state = 'failed', lease_owner = NULL, updated_at = ?,
                    failure_code = 'provider_identity_mismatch',
                    failure_detail = ?
                WHERE launch_id = ?
                """,
                (
                    event.observed_at,
                    "provider session did not match the requested target",
                    event.launch_id,
                ),
            )
            return
        session = connection.execute(
            "SELECT * FROM sessions WHERE session_key = ?", (event.session_key,)
        ).fetchone()
        assert surface is not None and session is not None
        cls._bind_surface_row(
            connection,
            surface,
            session,
            confidence="confirmed",
            observed_at=event.observed_at,
        )
        connection.execute(
            """
            UPDATE launch_intents
            SET state = 'bound', target_session_key = ?,
                lease_owner = NULL, updated_at = ?,
                failure_code = NULL, failure_detail = NULL
            WHERE launch_id = ?
            """,
            (event.session_key, event.observed_at, event.launch_id),
        )

    @staticmethod
    def _materialize_hook_state(
        connection: sqlite3.Connection,
        event: _PreparedHookEvent,
        launch: sqlite3.Row | None,
    ) -> tuple[sqlite3.Row, bool, bool]:
        session = connection.execute(
            "SELECT * FROM sessions WHERE session_key = ?", (event.session_key,)
        ).fetchone()
        assert session is not None
        runtime_allowed = _source_allows(
            session,
            "runtime",
            event.source_priority,
            event.entry_ns,
        )
        same_turn = (
            event.provider_turn_id is not None
            and session["last_hook_turn_id"] == event.provider_turn_id
        )
        if event.transition.activity is None:
            activity_allowed = False
        elif same_turn and event.source_priority == int(
            session["activity_source_priority"]
        ):
            stored_kind = session["last_hook_kind_priority"]
            activity_allowed = stored_kind is None or (
                event.transition.kind_priority >= int(stored_kind)
            )
        else:
            activity_allowed = _source_allows(
                session,
                "activity",
                event.source_priority,
                event.entry_ns,
            )

        updates: dict[str, Any] = {}
        if runtime_allowed or activity_allowed:
            updates.update(
                last_observed_at=max(
                    event.observed_at, int(session["last_observed_at"])
                ),
            )
        if activity_allowed:
            updates["last_activity_at"] = max(
                event.observed_at, int(session["last_activity_at"] or 0)
            )
        if runtime_allowed:
            # A hook proves this runtime is live, but only the evidence carried
            # by this newer hook remains trustworthy. Missing locator fields
            # therefore clear an older PID/tmux association.
            updates.update(
                cwd=(
                    launch["cwd"]
                    if launch is not None
                    and launch["action"] == "new"
                    and launch["cwd"] is not None
                    else event.cwd
                ),
                runtime_presence=event.transition.runtime_presence.value,
                runtime_observed_at=event.observed_at,
                runtime_source_priority=event.source_priority,
                runtime_order_ns=max(event.entry_ns, int(session["runtime_order_ns"])),
                runtime_pid=event.pid,
                provider_runtime_id=None,
                runtime_process_birth_id=event.process_birth_id,
                tmux_session=None,
                tmux_window=None,
                tmux_pane=event.tmux_pane,
                tmux_socket=event.tmux_socket,
            )
        if activity_allowed:
            assert event.transition.activity is not None
            assert event.transition.activity_reason is not None
            updates.update(
                activity=event.transition.activity.value,
                activity_reason=event.transition.activity_reason.value,
                state_confidence="confirmed",
                state_observed_at=max(
                    event.observed_at, int(session["state_observed_at"] or 0)
                ),
                activity_source_priority=event.source_priority,
                activity_order_ns=max(
                    event.entry_ns, int(session["activity_order_ns"])
                ),
                last_hook_turn_id=event.provider_turn_id,
                last_hook_entry_ns=max(
                    event.entry_ns, int(session["last_hook_entry_ns"] or 0)
                ),
                last_hook_kind_priority=event.transition.kind_priority,
            )
        if updates:
            assignments = ", ".join(f"{field} = ?" for field in updates)
            connection.execute(
                f"UPDATE sessions SET {assignments} WHERE session_key = ?",
                (*updates.values(), event.session_key),
            )
            session = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (event.session_key,)
            ).fetchone()
            assert session is not None
        return session, runtime_allowed, activity_allowed

    @staticmethod
    def _record_hook_evidence(
        connection: sqlite3.Connection,
        event: _PreparedHookEvent,
        context: _HookLaunchContext,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        retained_launch_id = None if context.mismatch else event.launch_id
        retained_surface_id = None if context.mismatch else event.surface_id
        event_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO events(
                event_id, idempotency_key, host_id, provider, session_key,
                launch_id, surface_id, event_kind, provider_turn_id,
                source_priority, kind_priority, observed_at, received_at,
                payload_hash, diagnostic_code, diagnostic_detail, entry_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                event_id,
                event.idempotency_key,
                event.host_id,
                event.provider,
                event.session_key,
                retained_launch_id,
                retained_surface_id,
                event.event_kind.value,
                event.provider_turn_id,
                event.source_priority,
                event.transition.kind_priority,
                event.observed_at,
                event.received_at,
                event.payload_hash,
                event.entry_ns,
            ),
        )

        activity = event.transition.activity or Activity.UNKNOWN
        activity_reason = event.transition.activity_reason or ActivityReason.UNKNOWN
        observation_key = f"event:{event.idempotency_key}"
        runtime_values = {
            "observation_key": observation_key,
            "host_id": event.host_id,
            "provider": event.provider,
            "session_key": event.session_key,
            "launch_id": retained_launch_id,
            "source": "hook",
            "source_priority": event.source_priority,
            "runtime_presence": event.transition.runtime_presence.value,
            "resumability": "unknown",
            "activity": activity.value,
            "activity_reason": activity_reason.value,
            "attachment": "unknown",
            "pid": event.pid,
            "provider_runtime_id": None,
            "tmux_session": None,
            "tmux_window": None,
            "tmux_pane": event.tmux_pane,
            "observed_at": event.observed_at,
        }
        runtime_hash = _sha256_text(
            _canonical_json(
                {
                    field: runtime_values.get(field)
                    for field in _RUNTIME_OBSERVATION_HASH_FIELDS
                }
            )
        )
        observation_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO runtime_observations(
                observation_id, observation_key, host_id, provider,
                session_key, launch_id, source, source_priority,
                runtime_presence, resumability, activity, activity_reason,
                attachment, pid, provider_runtime_id, tmux_session,
                tmux_window, tmux_pane, observed_at, received_at, payload_hash,
                entry_ns, process_birth_id, tmux_socket
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                NULL, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                observation_id,
                observation_key,
                event.host_id,
                event.provider,
                event.session_key,
                retained_launch_id,
                "hook",
                event.source_priority,
                event.transition.runtime_presence.value,
                "unknown",
                activity.value,
                activity_reason.value,
                "unknown",
                event.pid,
                event.tmux_pane,
                event.observed_at,
                event.received_at,
                runtime_hash,
                event.entry_ns,
                event.process_birth_id,
                event.tmux_socket,
            ),
        )
        stored_event = connection.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        stored_runtime = connection.execute(
            "SELECT * FROM runtime_observations WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        assert stored_event is not None and stored_runtime is not None
        return stored_event, stored_runtime

    @staticmethod
    def _prune_hook_evidence(connection: sqlite3.Connection, limit: int) -> None:
        # Hook events and their runtime observations are one replay witness. If
        # retention drops one, it must drop the other so a later exact replay
        # can be ingested without colliding with an orphan observation key.
        connection.execute(
            """
            DELETE FROM runtime_observations
            WHERE source = 'hook'
              AND (host_id, provider, observation_key) IN (
                  SELECT host_id, provider, 'event:' || idempotency_key
                  FROM events
                  ORDER BY received_at DESC, event_id DESC
                  LIMIT -1 OFFSET ?
              )
            """,
            (limit,),
        )
        connection.execute(
            """
            DELETE FROM events
            WHERE event_id IN (
                SELECT event_id FROM events
                ORDER BY received_at DESC, event_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (limit,),
        )

    def ingest_hook_event(
        self,
        event: Mapping[str, Any],
        *,
        host_display_name: str,
        limit: int = DEFAULT_EVENT_LIMIT,
    ) -> HookIngestionResult:
        """Atomically retain and materialize one normalized lifecycle event.

        The hook path intentionally consumes a correlated launch's stored lease
        without accepting the private lease owner from the provider environment.
        The launch and its surface must already be in ``provider_started`` and
        must match the normalized inherited IDs exactly.
        """

        if not 1 <= limit <= MAX_EVENT_LIMIT:
            raise ValueError(f"event limit must be between 1 and {MAX_EVENT_LIMIT}")
        prepared = _prepare_hook_event(event, host_display_name)

        with self.transaction(immediate=True) as connection:
            duplicate = self._hook_duplicate_result(connection, prepared)
            if duplicate is not None:
                return duplicate
            self._ensure_hook_host(connection, prepared)
            launch_context = self._resolve_hook_launch(connection, prepared)
            self._ensure_hook_session(connection, prepared, launch_context.launch)
            self._apply_hook_launch_binding(connection, prepared, launch_context)
            stored_session, runtime_allowed, activity_allowed = (
                self._materialize_hook_state(
                    connection,
                    prepared,
                    launch_context.launch,
                )
            )
            stored_event, stored_runtime = self._record_hook_evidence(
                connection, prepared, launch_context
            )
            self._prune_hook_evidence(connection, limit)
            stored_session = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?",
                (prepared.session_key,),
            ).fetchone()
            launch = (
                connection.execute(
                    "SELECT * FROM launch_intents WHERE launch_id = ?",
                    (prepared.launch_id,),
                ).fetchone()
                if prepared.launch_id is not None
                else None
            )
            surface = (
                connection.execute(
                    "SELECT * FROM surfaces WHERE surface_id = ?",
                    (prepared.surface_id,),
                ).fetchone()
                if prepared.surface_id is not None
                else None
            )
            assert stored_event is not None
            assert stored_runtime is not None
            assert stored_session is not None

        kind = (
            "provider_identity_mismatch"
            if launch_context.mismatch
            else ("applied" if runtime_allowed or activity_allowed else "stale")
        )
        return HookIngestionResult(
            kind,
            dict(stored_event),
            dict(stored_session),
            dict(stored_runtime),
            _row_dict(launch),
            _row_dict(surface),
        )

    @staticmethod
    def _retain_runtime_observation(
        connection: sqlite3.Connection,
        values: Mapping[str, Any],
    ) -> tuple[sqlite3.Row, bool]:
        """Insert new semantic evidence or reuse its stable retained row."""

        semantic_values = {
            key: value
            for key, value in values.items()
            if key not in {"entry_ns", "observed_at", "received_at"}
        }
        payload_hash = _sha256_text(_canonical_json(semantic_values))
        existing = connection.execute(
            """
            SELECT * FROM runtime_observations
            WHERE host_id = ? AND provider = ? AND observation_key = ?
            """,
            (values["host_id"], values["provider"], values["observation_key"]),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise IdentityConflict(
                    "runtime observation key was reused for different content"
                )
            return existing, False

        observation_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO runtime_observations(
                observation_id, observation_key, host_id, provider,
                session_key, launch_id, source, source_priority,
                runtime_presence, resumability, activity, activity_reason,
                attachment, pid, provider_runtime_id, tmux_session,
                tmux_window, tmux_pane, observed_at, received_at,
                payload_hash, entry_ns, process_birth_id, tmux_socket
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            """,
            (
                observation_id,
                values["observation_key"],
                values["host_id"],
                values["provider"],
                values["session_key"],
                values["launch_id"],
                values["source"],
                values["source_priority"],
                values["runtime_presence"],
                values["resumability"],
                values["activity"],
                values["activity_reason"],
                values["attachment"],
                values["pid"],
                values["provider_runtime_id"],
                values["tmux_session"],
                values["tmux_window"],
                values["tmux_pane"],
                values["observed_at"],
                values["received_at"],
                payload_hash,
                values["entry_ns"],
                values["process_birth_id"],
                values["tmux_socket"],
            ),
        )
        stored = connection.execute(
            "SELECT * FROM runtime_observations WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        assert stored is not None
        return stored, True

    @staticmethod
    def _runtime_axis_updates(
        session: Mapping[str, Any],
        values: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool, bool]:
        """Compute independent axis and locator changes from one observation."""

        priority = int(values["source_priority"])
        order_ns = int(values["entry_ns"])
        runtime_allowed = values["runtime_presence"] is not None and _source_allows(
            session, "runtime", priority, order_ns
        )
        resumability_allowed = values["resumability"] is not None and _source_allows(
            session, "resumability", priority, order_ns
        )
        activity_allowed = values["activity"] is not None and _source_allows(
            session, "activity", priority, order_ns
        )
        attachment_allowed = values["attachment"] is not None and _source_allows(
            session, "attachment", priority, order_ns
        )
        updates: dict[str, Any] = {}
        if runtime_allowed:
            stopped = values["runtime_presence"] == RuntimePresence.STOPPED.value
            updates.update(
                runtime_presence=values["runtime_presence"],
                runtime_observed_at=values["observed_at"],
                runtime_source_priority=priority,
                runtime_order_ns=order_ns,
                runtime_pid=None if stopped else values["pid"],
                provider_runtime_id=(
                    None if stopped else values["provider_runtime_id"]
                ),
                runtime_process_birth_id=(
                    None if stopped else values["process_birth_id"]
                ),
            )
            if stopped or values["tmux_observed"]:
                updates.update(
                    tmux_socket=None if stopped else values["tmux_socket"],
                    tmux_session=None if stopped else values["tmux_session"],
                    tmux_window=None if stopped else values["tmux_window"],
                    tmux_pane=None if stopped else values["tmux_pane"],
                )
        if resumability_allowed:
            updates.update(
                resumability=values["resumability"],
                resumability_source_priority=priority,
                resumability_order_ns=order_ns,
            )
        if activity_allowed:
            updates.update(
                activity=values["activity"],
                activity_reason=values["activity_reason"] or "unknown",
                activity_source_priority=priority,
                activity_order_ns=order_ns,
                state_observed_at=values["observed_at"],
                state_confidence="confirmed",
            )
        if attachment_allowed:
            updates.update(
                attachment=values["attachment"],
                attachment_source_priority=priority,
                attachment_order_ns=order_ns,
            )
        if updates:
            updates["last_observed_at"] = max(
                int(session["last_observed_at"]), int(values["observed_at"])
            )
        return updates, runtime_allowed, attachment_allowed

    @classmethod
    def _apply_runtime_launch_binding(
        cls,
        connection: sqlite3.Connection,
        session: Mapping[str, Any],
        values: Mapping[str, Any],
    ) -> None:
        """Consume exact live tmux evidence for a missed launch hook."""

        launch_id = values["launch_id"]
        if launch_id is None:
            return
        if (
            values["runtime_presence"] != RuntimePresence.LIVE.value
            or not values["tmux_observed"]
            or values["pid"] is None
            or values["process_birth_id"] is None
            or any(
                values[field] is None
                for field in (
                    "tmux_socket",
                    "tmux_session",
                    "tmux_window",
                    "tmux_pane",
                )
            )
        ):
            raise StorageError(
                "runtime launch binding requires complete live tmux evidence"
            )
        launch = connection.execute(
            "SELECT * FROM launch_intents WHERE launch_id = ?", (launch_id,)
        ).fetchone()
        if launch is None:
            raise StorageError(f"unknown launch: {launch_id}")
        surface = connection.execute(
            "SELECT * FROM surfaces WHERE surface_id = ?", (launch["surface_id"],)
        ).fetchone()
        if surface is None:
            raise StorageError("runtime launch binding has no surface")
        if (
            launch["host_id"] != values["host_id"]
            or launch["provider"] != values["provider"]
            or launch["action"] not in {"new", "resume", "attach", "history"}
            or (
                launch["action"] in {"resume", "attach"}
                and launch["target_session_key"] != session["session_key"]
            )
            or (
                launch["action"] in {"new", "history"}
                and launch["target_session_key"] not in {None, session["session_key"]}
            )
            or surface["host_id"] != values["host_id"]
            or surface["provider"] != values["provider"]
            or surface["transport"] != "tmux"
            or surface["role"] != "session"
            or surface["launch_id"] != launch_id
            or surface["retired_at"] is not None
        ):
            raise IdentityConflict(
                "runtime evidence does not match launch and surface identity"
            )
        try:
            locator = json.loads(surface["transport_locator"])
        except (json.JSONDecodeError, RecursionError, TypeError) as error:
            raise StorageError("runtime launch surface locator is invalid") from error
        expected_locator = {
            "socket": values["tmux_socket"],
            "session": values["tmux_session"],
            "window": values["tmux_window"],
            "pane": values["tmux_pane"],
        }
        if locator != expected_locator:
            raise IdentityConflict(
                "runtime evidence does not match launch surface locator"
            )
        if launch["state"] == "bound":
            if (
                surface["current_session_key"] != session["session_key"]
                or surface["binding_confidence"] != "confirmed"
                or session["surface_id"] != surface["surface_id"]
            ):
                raise IdentityConflict("bound runtime launch changed identity")
            return
        if launch["state"] != "provider_started":
            raise StorageError("runtime launch is not ready for provider binding")
        if int(values["observed_at"]) < int(launch["updated_at"]):
            raise StorageError("stale runtime launch observation")
        if launch["action"] == "new":
            connection.execute(
                """
                UPDATE sessions
                SET project_id = ?, task_id = ?, checkout_id = ?, cwd = ?,
                    metadata_source = 'launch', continued_from_handoff_id = ?
                WHERE session_key = ?
                """,
                (
                    launch["project_id"],
                    launch["task_id"],
                    launch["checkout_id"],
                    launch["cwd"],
                    launch["source_handoff_id"],
                    session["session_key"],
                ),
            )
            if launch["task_id"] is not None:
                connection.execute(
                    """
                    UPDATE tasks
                    SET current_session_key = ?, updated_at = ?
                    WHERE task_id = ? AND status = 'open'
                    """,
                    (
                        session["session_key"],
                        int(values["observed_at"]),
                        launch["task_id"],
                    ),
                )
            refreshed = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?",
                (session["session_key"],),
            ).fetchone()
            assert refreshed is not None
            session = refreshed
        elif launch["action"] in {"resume", "attach", "history"}:
            connection.execute(
                "UPDATE sessions SET wrapped_at = NULL WHERE session_key = ?",
                (session["session_key"],),
            )
            refreshed = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?",
                (session["session_key"],),
            ).fetchone()
            assert refreshed is not None
            session = refreshed
        cls._bind_surface_row(
            connection,
            surface,
            session,
            confidence="confirmed",
            observed_at=int(values["observed_at"]),
        )
        connection.execute(
            """
            UPDATE launch_intents
            SET state = 'bound', target_session_key = ?, lease_owner = NULL,
                updated_at = ?,
                failure_code = NULL, failure_detail = NULL
            WHERE launch_id = ?
            """,
            (session["session_key"], values["observed_at"], launch_id),
        )

    @staticmethod
    def _apply_runtime_surface(
        connection: sqlite3.Connection,
        session: Mapping[str, Any],
        values: Mapping[str, Any],
        *,
        runtime_allowed: bool,
        attachment_allowed: bool,
    ) -> None:
        """Apply authoritative tmux absence without touching other transports."""

        surface_id = session["surface_id"]
        if surface_id is None or not runtime_allowed:
            return
        surface = connection.execute(
            "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
        ).fetchone()
        if (
            surface is None
            or surface["transport"] != "tmux"
            or int(values["observed_at"]) < int(surface["last_observed_at"])
        ):
            return
        stopped = values["runtime_presence"] == RuntimePresence.STOPPED.value
        tmux_absent = values["tmux_observed"] and values["tmux_pane"] is None
        if stopped or tmux_absent:
            connection.execute(
                """
                UPDATE surfaces
                SET current_session_key = NULL,
                    binding_confidence = 'unknown',
                    client_attached = 0,
                    last_observed_at = MAX(last_observed_at, ?)
                WHERE surface_id = ? AND current_session_key = ?
                """,
                (values["observed_at"], surface_id, session["session_key"]),
            )
            connection.execute(
                "UPDATE sessions SET surface_id = NULL "
                "WHERE session_key = ? AND surface_id = ?",
                (session["session_key"], surface_id),
            )
        elif values["tmux_observed"] and attachment_allowed:
            connection.execute(
                """
                UPDATE surfaces
                SET client_attached = ?,
                    last_observed_at = MAX(last_observed_at, ?)
                WHERE surface_id = ? AND current_session_key = ?
                """,
                (
                    int(values["attachment"] == Attachment.ATTACHED.value),
                    values["observed_at"],
                    surface_id,
                    session["session_key"],
                ),
            )

    @staticmethod
    def _prune_liveness_observations(
        connection: sqlite3.Connection,
        session_key: str,
        limit: int = DEFAULT_LIVENESS_OBSERVATION_LIMIT,
    ) -> None:
        connection.execute(
            """
            DELETE FROM runtime_observations
            WHERE observation_id IN (
                SELECT observation_id FROM runtime_observations
                WHERE session_key = ? AND source = 'liveness'
                ORDER BY observed_at DESC, observation_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (session_key, limit),
        )

    def apply_runtime_observations(
        self,
        observations: Sequence[NormalizedRuntimeObservation],
    ) -> RuntimeObservationApplyResult:
        """Atomically retain and materialize one bounded live-probe batch.

        A missing optional axis means that the probe did not establish it.
        This is intentionally different from the public ``unknown`` enum: a
        failed tmux probe, for example, must not erase a retained attachment.
        """

        normalized = tuple(observations)
        if any(
            not isinstance(observation, NormalizedRuntimeObservation)
            for observation in normalized
        ):
            raise StorageError(
                "runtime reconciliation requires normalized observations"
            )
        keys = [observation.observation_key for observation in normalized]
        if len(keys) != len(set(keys)):
            raise IdentityConflict("duplicate runtime observation key in batch")

        applied_count = 0
        stale_count = 0
        retained: list[dict[str, Any]] = []
        session_keys: set[str] = set()
        with self.transaction(immediate=True) as connection:
            for observation in normalized:
                values = observation.storage_mapping()
                session_key = str(observation.session_key)
                session = connection.execute(
                    "SELECT * FROM sessions WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
                if session is None:
                    raise StorageError(f"unknown session: {session_key}")
                if (
                    session["host_id"] != values["host_id"]
                    or session["provider"] != values["provider"]
                ):
                    raise IdentityConflict(
                        "runtime observation session does not match host/provider"
                    )

                durable_values = {
                    **values,
                    "runtime_presence": values["runtime_presence"] or "unknown",
                    "resumability": values["resumability"] or "unknown",
                    "activity": values["activity"] or "unknown",
                    "activity_reason": values["activity_reason"] or "unknown",
                    "attachment": values["attachment"] or "unknown",
                }
                stored, _inserted = self._retain_runtime_observation(
                    connection, durable_values
                )
                updates, runtime_allowed, attachment_allowed = (
                    self._runtime_axis_updates(session, values)
                )
                if updates:
                    assignments = ", ".join(f"{field} = ?" for field in updates)
                    connection.execute(
                        f"UPDATE sessions SET {assignments} WHERE session_key = ?",
                        (*updates.values(), session_key),
                    )
                    applied_count += 1
                else:
                    stale_count += 1
                session = connection.execute(
                    "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
                ).fetchone()
                assert session is not None
                self._apply_runtime_launch_binding(connection, session, values)
                session = connection.execute(
                    "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
                ).fetchone()
                assert session is not None
                self._apply_runtime_surface(
                    connection,
                    session,
                    values,
                    runtime_allowed=runtime_allowed,
                    attachment_allowed=attachment_allowed,
                )
                self._prune_liveness_observations(connection, session_key)
                retained.append(dict(stored))
                session_keys.add(session_key)

            sessions = (
                tuple(
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM sessions WHERE session_key IN "
                        f"({', '.join('?' for _ in session_keys)}) "
                        "ORDER BY session_key",
                        tuple(sorted(session_keys)),
                    ).fetchall()
                )
                if session_keys
                else ()
            )

        return RuntimeObservationApplyResult(
            applied_count,
            stale_count,
            tuple(retained),
            sessions,
        )

    def record_runtime_observation(
        self, observation: Mapping[str, Any]
    ) -> dict[str, Any]:
        required = {
            "observation_key",
            "host_id",
            "provider",
            "source",
            "source_priority",
            "runtime_presence",
            "resumability",
            "activity",
            "activity_reason",
            "attachment",
            "observed_at",
            "received_at",
        } - set(observation)
        if required:
            raise StorageError(
                f"missing runtime observation fields: {sorted(required)}"
            )
        private_fields = set(_PRIVATE_RUNTIME_OBSERVATION_FIELDS).intersection(
            observation
        )
        if private_fields:
            raise StorageError(
                "private runtime evidence fields require an atomic reconciler: "
                f"{sorted(private_fields)}"
            )
        _canonical_host_id(observation["host_id"])
        _canonical_provider(observation["provider"])
        _evidence_priority(observation["source_priority"], "source_priority")
        _nonnegative_integer(observation["observed_at"], "observed_at")
        _nonnegative_integer(observation["received_at"], "received_at")
        session_key = observation.get("session_key")
        if session_key is not None:
            _canonical_session_key(session_key)
        launch_id = observation.get("launch_id")
        if launch_id is not None:
            launch_id = _canonical_uuid_id(launch_id, LaunchId, "launch_id")
        _reject_mapping_controls(
            observation,
            (
                "observation_id",
                "observation_key",
                "source",
                "provider_runtime_id",
                "tmux_session",
                "tmux_window",
                "tmux_pane",
            ),
        )
        observation_id = str(observation.get("observation_id", uuid.uuid4()))
        payload_hash = _sha256_text(
            _canonical_json(
                {
                    field: observation.get(field)
                    for field in _RUNTIME_OBSERVATION_HASH_FIELDS
                }
            )
        )
        supplied_hash = observation.get("payload_hash")
        if supplied_hash is not None and (
            _require_hash(str(supplied_hash), "payload_hash") != payload_hash
        ):
            raise StorageError(
                "payload_hash does not match the normalized runtime observation"
            )

        with self.transaction(immediate=True) as connection:
            if session_key is not None:
                stored_session = connection.execute(
                    "SELECT * FROM sessions WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
                if stored_session is None:
                    raise StorageError(f"unknown session: {session_key}")
                if (
                    stored_session["host_id"] != observation["host_id"]
                    or stored_session["provider"] != observation["provider"]
                ):
                    raise IdentityConflict(
                        "runtime observation session does not match host/provider"
                    )
            if launch_id is not None:
                launch = connection.execute(
                    "SELECT * FROM launch_intents WHERE launch_id = ?",
                    (launch_id,),
                ).fetchone()
                if launch is None:
                    raise StorageError(f"unknown launch: {launch_id}")
                if (
                    launch["host_id"] != observation["host_id"]
                    or launch["provider"] != observation["provider"]
                ):
                    raise IdentityConflict(
                        "runtime observation launch does not match host/provider"
                    )
                if (
                    session_key is not None
                    and launch["target_session_key"] is not None
                    and launch["target_session_key"] != session_key
                ):
                    raise IdentityConflict(
                        "runtime observation session does not match launch target"
                    )
            existing = connection.execute(
                """
                SELECT * FROM runtime_observations
                WHERE host_id = ? AND provider = ? AND observation_key = ?
                """,
                (
                    observation["host_id"],
                    observation["provider"],
                    observation["observation_key"],
                ),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != payload_hash and any(
                    existing[field] != observation.get(field)
                    for field in _RUNTIME_OBSERVATION_HASH_FIELDS
                ):
                    raise IdentityConflict(
                        "runtime observation key was reused for different content"
                    )
                return dict(existing)
            connection.execute(
                """
                INSERT INTO runtime_observations(
                    observation_id, observation_key, host_id, provider,
                    session_key, launch_id, source, source_priority,
                    runtime_presence, resumability, activity, activity_reason,
                    attachment, pid, provider_runtime_id, tmux_session,
                    tmux_window, tmux_pane, observed_at, received_at, payload_hash,
                    entry_ns, process_birth_id, tmux_socket
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?
                )
                """,
                (
                    observation_id,
                    observation["observation_key"],
                    observation["host_id"],
                    observation["provider"],
                    observation.get("session_key"),
                    launch_id,
                    observation["source"],
                    observation["source_priority"],
                    observation["runtime_presence"],
                    observation["resumability"],
                    observation["activity"],
                    observation["activity_reason"],
                    observation["attachment"],
                    observation.get("pid"),
                    observation.get("provider_runtime_id"),
                    observation.get("tmux_session"),
                    observation.get("tmux_window"),
                    observation.get("tmux_pane"),
                    observation["observed_at"],
                    observation["received_at"],
                    payload_hash,
                    observation.get("entry_ns"),
                    observation.get("process_birth_id"),
                    observation.get("tmux_socket"),
                ),
            )
            row = connection.execute(
                "SELECT * FROM runtime_observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return result

    def record_event(
        self,
        event: Mapping[str, Any],
        *,
        limit: int = DEFAULT_EVENT_LIMIT,
    ) -> dict[str, Any]:
        """Store bounded normalized hook metadata, never a raw hook payload."""

        if not 1 <= limit <= MAX_EVENT_LIMIT:
            raise ValueError(f"event limit must be between 1 and {MAX_EVENT_LIMIT}")
        required = {
            "idempotency_key",
            "host_id",
            "provider",
            "event_kind",
            "source_priority",
            "kind_priority",
            "observed_at",
            "received_at",
        } - set(event)
        if required:
            raise StorageError(f"missing event fields: {sorted(required)}")
        if "entry_ns" in event:
            raise StorageError(
                "private event evidence fields require an atomic reconciler: "
                "['entry_ns']"
            )
        _canonical_host_id(event["host_id"])
        _canonical_provider(event["provider"])
        _evidence_priority(event["source_priority"], "source_priority")
        _evidence_priority(event["kind_priority"], "kind_priority")
        _nonnegative_integer(event["observed_at"], "observed_at")
        _nonnegative_integer(event["received_at"], "received_at")
        session_key = event.get("session_key")
        if session_key is not None:
            _canonical_session_key(session_key)
        launch_id = event.get("launch_id")
        if launch_id is not None:
            launch_id = _canonical_uuid_id(launch_id, LaunchId, "launch_id")
        surface_id = event.get("surface_id")
        if surface_id is not None:
            surface_id = _canonical_uuid_id(surface_id, SurfaceId, "surface_id")
        _reject_mapping_controls(
            event,
            (
                "event_id",
                "idempotency_key",
                "event_kind",
                "provider_turn_id",
                "diagnostic_code",
            ),
        )
        payload_hash = _sha256_text(
            _canonical_json({field: event.get(field) for field in _EVENT_HASH_FIELDS})
        )
        supplied_hash = event.get("payload_hash")
        if supplied_hash is not None and (
            _require_hash(str(supplied_hash), "payload_hash") != payload_hash
        ):
            raise StorageError("payload_hash does not match the normalized event")
        _reject_controls(
            event.get("diagnostic_detail"),
            "diagnostic_detail",
            allow_multiline=True,
        )
        event_id = str(event.get("event_id", uuid.uuid4()))

        with self.transaction(immediate=True) as connection:
            stored_session: sqlite3.Row | None = None
            if session_key is not None:
                stored_session = connection.execute(
                    "SELECT * FROM sessions WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
                if stored_session is None:
                    raise StorageError(f"unknown session: {session_key}")
                if (
                    stored_session["host_id"] != event["host_id"]
                    or stored_session["provider"] != event["provider"]
                ):
                    raise IdentityConflict("event session does not match host/provider")
            launch: sqlite3.Row | None = None
            if launch_id is not None:
                launch = connection.execute(
                    "SELECT * FROM launch_intents WHERE launch_id = ?",
                    (launch_id,),
                ).fetchone()
                if launch is None:
                    raise StorageError(f"unknown launch: {launch_id}")
                if (
                    launch["host_id"] != event["host_id"]
                    or launch["provider"] != event["provider"]
                ):
                    raise IdentityConflict("event launch does not match host/provider")
                if (
                    session_key is not None
                    and launch["target_session_key"] is not None
                    and launch["target_session_key"] != session_key
                ):
                    raise IdentityConflict("event session does not match launch target")
            surface: sqlite3.Row | None = None
            if surface_id is not None:
                surface = connection.execute(
                    "SELECT * FROM surfaces WHERE surface_id = ?",
                    (surface_id,),
                ).fetchone()
                if surface is None:
                    raise StorageError(f"unknown surface: {surface_id}")
                if (
                    surface["host_id"] != event["host_id"]
                    or surface["provider"] != event["provider"]
                ):
                    raise IdentityConflict("event surface does not match host/provider")
                if (
                    session_key is not None
                    and surface["current_session_key"] is not None
                    and surface["current_session_key"] != session_key
                ):
                    raise IdentityConflict(
                        "event session does not match surface binding"
                    )
                if (
                    stored_session is not None
                    and stored_session["surface_id"] is not None
                    and stored_session["surface_id"] != surface_id
                ):
                    raise IdentityConflict(
                        "event surface does not match session binding"
                    )
            if (
                launch is not None
                and surface is not None
                and surface["launch_id"] != launch_id
            ):
                raise IdentityConflict("event surface does not match launch")
            existing = connection.execute(
                """
                SELECT * FROM events
                WHERE host_id = ? AND provider = ? AND idempotency_key = ?
                """,
                (event["host_id"], event["provider"], event["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != payload_hash and any(
                    existing[field] != event.get(field) for field in _EVENT_HASH_FIELDS
                ):
                    raise IdentityConflict(
                        "event idempotency key has different content"
                    )
                return dict(existing)
            connection.execute(
                """
                INSERT INTO events(
                    event_id, idempotency_key, host_id, provider, session_key,
                    launch_id, surface_id, event_kind, provider_turn_id,
                    source_priority, kind_priority, observed_at, received_at,
                    payload_hash, diagnostic_code, diagnostic_detail, entry_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event["idempotency_key"],
                    event["host_id"],
                    event["provider"],
                    event.get("session_key"),
                    launch_id,
                    surface_id,
                    event["event_kind"],
                    event.get("provider_turn_id"),
                    event["source_priority"],
                    event["kind_priority"],
                    event["observed_at"],
                    event["received_at"],
                    payload_hash,
                    event.get("diagnostic_code"),
                    event.get("diagnostic_detail"),
                    event.get("entry_ns"),
                ),
            )
            row = connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            assert row is not None
            result = dict(row)
            self._prune_hook_evidence(connection, limit)
        return result

    def upsert_remote(
        self,
        remote_name: str,
        ssh_target: str,
        display_name: str,
        *,
        declared: bool = True,
        observed_at: int | None = None,
    ) -> dict[str, Any]:
        _reject_controls(remote_name, "remote_name")
        _reject_controls(ssh_target, "ssh_target")
        _reject_controls(display_name, "display_name")
        timestamp = now_ms() if observed_at is None else observed_at
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO remote_snapshots(
                    remote_name, ssh_target, display_name, declared,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(remote_name) DO UPDATE SET
                    ssh_target = excluded.ssh_target,
                    display_name = excluded.display_name,
                    declared = excluded.declared,
                    updated_at = excluded.updated_at
                """,
                (
                    remote_name,
                    ssh_target,
                    display_name,
                    int(declared),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM remote_snapshots WHERE remote_name = ?", (remote_name,)
            ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return result

    def store_remote_snapshot(
        self,
        remote_name: str,
        snapshot: Mapping[str, Any],
        *,
        remote_host_id: str,
        schema_version: int,
        protocol_version: int,
        observed_at: int,
        received_at: int | None = None,
    ) -> dict[str, Any]:
        """Atomically replace a validated remote's last successful snapshot."""

        remote_host_id = _canonical_host_id(remote_host_id)
        envelope = SnapshotEnvelope.from_dict(snapshot)
        envelope_host_id = str(envelope.host.host_id)
        if remote_host_id != envelope_host_id:
            raise IdentityConflict(
                "remote snapshot host does not match the expected remote host"
            )
        if schema_version != SNAPSHOT_SCHEMA_VERSION:
            raise StorageError(
                "remote snapshot schema argument does not match the envelope"
            )
        if protocol_version != SNAPSHOT_PROTOCOL_VERSION:
            raise StorageError(
                "remote snapshot protocol argument does not match the envelope"
            )
        if observed_at != envelope.generated_at:
            raise StorageError(
                "remote snapshot observed_at does not match envelope.generatedAt"
            )
        received = now_ms() if received_at is None else received_at
        snapshot_json = envelope.to_json()
        snapshot_hash = _sha256_text(snapshot_json)
        with self.transaction(immediate=True) as connection:
            endpoint = connection.execute(
                "SELECT * FROM remote_snapshots WHERE remote_name = ?", (remote_name,)
            ).fetchone()
            if endpoint is None:
                raise StorageError(f"unknown remote: {remote_name}")
            pinned_host_id = endpoint["remote_host_id"]
            if pinned_host_id is not None and pinned_host_id != remote_host_id:
                raise IdentityConflict(
                    "remote endpoint changed its pinned host identity"
                )
            last_attempt_at = endpoint["last_attempt_at"]
            if last_attempt_at is not None and received < int(last_attempt_at):
                raise StorageError("stale remote snapshot completion")
            if last_attempt_at is not None and received == int(last_attempt_at):
                if (
                    endpoint["reachability"] == "online"
                    and endpoint["snapshot_hash"] == snapshot_hash
                    and endpoint["snapshot_observed_at"] == observed_at
                ):
                    result = dict(endpoint)
                    result["snapshot"] = json.loads(result["snapshot_json"])
                    return result
                raise StorageError("conflicting remote completion at the same time")
            previous_observed_at = endpoint["snapshot_observed_at"]
            if previous_observed_at is not None and observed_at < int(
                previous_observed_at
            ):
                raise StorageError("stale remote snapshot observation")
            if (
                previous_observed_at is not None
                and observed_at == int(previous_observed_at)
                and endpoint["snapshot_hash"] != snapshot_hash
            ):
                raise IdentityConflict(
                    "remote snapshot timestamp was reused for different content"
                )
            connection.execute(
                """
                INSERT INTO hosts(
                    host_id, display_name, is_local, created_at, updated_at
                ) VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                    updated_at = MAX(hosts.updated_at, excluded.updated_at)
                """,
                (remote_host_id, envelope.host.display_name, received, received),
            )
            connection.execute(
                """
                UPDATE remote_snapshots SET
                    remote_host_id = ?, reachability = 'online',
                    snapshot_schema_version = ?, snapshot_protocol_version = ?,
                    snapshot_json = ?, snapshot_hash = ?, snapshot_observed_at = ?,
                    snapshot_received_at = ?, last_attempt_at = ?, error_code = NULL,
                    error_detail = NULL, updated_at = ?
                WHERE remote_name = ?
                """,
                (
                    remote_host_id,
                    schema_version,
                    protocol_version,
                    snapshot_json,
                    snapshot_hash,
                    observed_at,
                    received,
                    received,
                    received,
                    remote_name,
                ),
            )
            row = connection.execute(
                "SELECT * FROM remote_snapshots WHERE remote_name = ?", (remote_name,)
            ).fetchone()
        result = _row_dict(row)
        assert result is not None
        result["snapshot"] = json.loads(result["snapshot_json"])
        return result

    def mark_remote_failure(
        self,
        remote_name: str,
        *,
        error_code: str,
        error_detail: str | None = None,
        attempted_at: int | None = None,
    ) -> dict[str, Any]:
        """Mark a remote offline without overwriting its last good snapshot."""

        timestamp = now_ms() if attempted_at is None else attempted_at
        _reject_controls(error_code, "error_code")
        _reject_controls(error_detail, "error_detail", allow_multiline=True)
        with self.transaction(immediate=True) as connection:
            endpoint = connection.execute(
                "SELECT * FROM remote_snapshots WHERE remote_name = ?", (remote_name,)
            ).fetchone()
            if endpoint is None:
                raise StorageError(f"unknown remote: {remote_name}")
            last_attempt_at = endpoint["last_attempt_at"]
            if last_attempt_at is not None and timestamp < int(last_attempt_at):
                raise StorageError("stale remote failure completion")
            if last_attempt_at is not None and timestamp == int(last_attempt_at):
                if (
                    endpoint["reachability"] == "offline"
                    and endpoint["error_code"] == error_code
                    and endpoint["error_detail"] == error_detail
                ):
                    result = dict(endpoint)
                    if result["snapshot_json"] is not None:
                        result["snapshot"] = json.loads(result["snapshot_json"])
                    return result
                raise StorageError("conflicting remote completion at the same time")
            connection.execute(
                """
                UPDATE remote_snapshots SET
                    reachability = 'offline', last_attempt_at = ?,
                    error_code = ?, error_detail = ?, updated_at = ?
                WHERE remote_name = ?
                """,
                (timestamp, error_code, error_detail, timestamp, remote_name),
            )
            row = connection.execute(
                "SELECT * FROM remote_snapshots WHERE remote_name = ?", (remote_name,)
            ).fetchone()
        result = _row_dict(row)
        assert result is not None
        if result["snapshot_json"] is not None:
            result["snapshot"] = json.loads(result["snapshot_json"])
        return result

    def get_remote(self, remote_name: str) -> dict[str, Any] | None:
        result = _row_dict(
            self.connection.execute(
                "SELECT * FROM remote_snapshots WHERE remote_name = ?", (remote_name,)
            ).fetchone()
        )
        if result is not None and result["snapshot_json"] is not None:
            result["snapshot"] = json.loads(result["snapshot_json"])
        return result

    def list_remotes(self, *, declared_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE declared = 1" if declared_only else ""
        rows = self.connection.execute(
            f"SELECT * FROM remote_snapshots {where} ORDER BY remote_name"
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            result = dict(row)
            if result["snapshot_json"] is not None:
                result["snapshot"] = json.loads(result["snapshot_json"])
            results.append(result)
        return results


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "DEFAULT_AGENT_CONTEXT_SESSION_LIMIT",
    "DEFAULT_AGENT_PROJECT_SESSION_LIMIT",
    "DEFAULT_AGENT_SEARCH_LIMIT",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_EVENT_LIMIT",
    "DEFAULT_HANDOFF_LIMIT",
    "DEFAULT_LIVENESS_OBSERVATION_LIMIT",
    "DEFAULT_SNAPSHOT_RUNTIME_LIMIT",
    "DEFAULT_SNAPSHOT_SESSION_LIMIT",
    "DEFAULT_SNAPSHOT_TASK_LIMIT",
    "ContinuationError",
    "ContinuationSource",
    "HookIngestionResult",
    "HostSnapshotRows",
    "IdentityConflict",
    "LaunchBindingResult",
    "ProjectContextRows",
    "ProjectSearchRows",
    "ProviderSessionReconciliationResult",
    "Registry",
    "RegistryClosed",
    "RequestConflict",
    "ReservationResult",
    "RuntimeObservationApplyResult",
    "SessionCurationResult",
    "SessionDetailRows",
    "StorageError",
    "connect_database",
    "handoff_content_hash",
    "launch_request_fingerprint",
    "now_ms",
]
