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
    HandoffId,
    HostId,
    LaunchId,
    LocationId,
    ProjectId,
    ProviderId,
    SessionKey,
    SurfaceId,
    UUIDId,
    ValidationError,
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

DEFAULT_BUSY_TIMEOUT_MS: Final = 5_000
MAX_BUSY_TIMEOUT_MS: Final = 30_000
DEFAULT_EVENT_LIMIT: Final = 1_000
MAX_EVENT_LIMIT: Final = 100_000

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
    "location_id",
    "cwd",
    "source_handoff_id",
    "target_session_key",
    "transport",
)
_SESSION_FIELDS: Final = {
    "project_id",
    "location_id",
    "name",
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
    "received_at",
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
    "observed_at",
    "received_at",
    "diagnostic_code",
    "diagnostic_detail",
)


class StorageError(RuntimeError):
    """Base error for registry operations."""


class IdentityConflict(StorageError):
    """A stable ID was reused for different immutable identity fields."""


class RequestConflict(StorageError):
    """A launch request ID was reused for a different normalized request."""


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


def now_ms() -> int:
    """Return the current Unix time in integer milliseconds."""

    return int(time.time() * 1_000)


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

        Missing rows are marked undeclared; no project, location, session, or
        handoff history is deleted.  Stable location IDs cannot move between a
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
                """
                UPDATE project_locations
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
                        default_transport, context_sources_json, declared,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        name = excluded.name,
                        aliases_json = excluded.aliases_json,
                        default_provider = excluded.default_provider,
                        default_transport = excluded.default_transport,
                        context_sources_json = excluded.context_sources_json,
                        declared = 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        project["project_id"],
                        project["name"],
                        _canonical_json(project["aliases"]),
                        project["default_provider"],
                        project["default_transport"],
                        _canonical_json(project["context_sources"]),
                        timestamp,
                        timestamp,
                    ),
                )
                for location in project["locations"]:
                    existing = connection.execute(
                        """
                        SELECT project_id, host_id
                        FROM project_locations WHERE location_id = ?
                        """,
                        (location["location_id"],),
                    ).fetchone()
                    if existing is not None and (
                        existing["project_id"] != project["project_id"]
                        or existing["host_id"] != host_id
                    ):
                        raise IdentityConflict(
                            f"location ID {location['location_id']!r} already belongs "
                            "to another project or host"
                        )
                    try:
                        connection.execute(
                            """
                            INSERT INTO project_locations(
                                location_id, project_id, host_id, path, display_name,
                                repository_identity, provider_override,
                                transport_override, is_default, declared,
                                last_observed_at, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                            ON CONFLICT(location_id) DO UPDATE SET
                                path = excluded.path,
                                display_name = excluded.display_name,
                                repository_identity = excluded.repository_identity,
                                provider_override = excluded.provider_override,
                                transport_override = excluded.transport_override,
                                is_default = excluded.is_default,
                                declared = 1,
                                last_observed_at = excluded.last_observed_at,
                                updated_at = excluded.updated_at
                            """,
                            (
                                location["location_id"],
                                project["project_id"],
                                host_id,
                                location["path"],
                                location["display_name"],
                                location["repository_identity"],
                                location["provider_override"],
                                location["transport_override"],
                                int(location["is_default"]),
                                location["last_observed_at"],
                                timestamp,
                                timestamp,
                            ),
                        )
                    except sqlite3.IntegrityError as error:
                        raise IdentityConflict(
                            "location identity/path conflict for "
                            f"{location['location_id']!r}"
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
        location_ids: set[str] = set()
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
            raw_context = raw_project.get("context_sources", ())
            raw_locations = raw_project.get("locations", ())
            if isinstance(raw_aliases, (str, bytes)) or isinstance(
                raw_context, (str, bytes)
            ):
                raise StorageError("aliases and context_sources must be sequences")
            if isinstance(raw_locations, (str, bytes)):
                raise StorageError("locations must be a sequence")

            aliases = sorted({str(alias) for alias in raw_aliases})
            context_sources = [str(source) for source in raw_context]
            for alias in aliases:
                _reject_controls(alias, "project alias")
            for source in context_sources:
                _reject_controls(source, "context source")
            locations: list[dict[str, Any]] = []
            default_count = 0
            for raw_location in raw_locations:
                try:
                    location_id = _canonical_uuid_id(
                        raw_location["location_id"], LocationId, "location_id"
                    )
                    path = str(raw_location["path"])
                except KeyError as error:
                    raise StorageError(
                        f"missing project location field: {error.args[0]}"
                    ) from error
                if location_id in location_ids:
                    raise IdentityConflict(f"duplicate location ID: {location_id}")
                if not Path(path).is_absolute():
                    raise StorageError(
                        f"configured location path must be absolute: {path!r}"
                    )
                _reject_controls(path, "location path")
                _reject_controls(
                    raw_location.get("display_name"), "location display_name"
                )
                _reject_controls(
                    raw_location.get("repository_identity"),
                    "location repository_identity",
                )
                if path in paths:
                    raise IdentityConflict(
                        f"duplicate configured location path: {path}"
                    )
                location_ids.add(location_id)
                paths.add(path)
                is_default = bool(raw_location.get("is_default", False))
                default_count += int(is_default)
                locations.append(
                    {
                        "location_id": location_id,
                        "path": path,
                        "display_name": raw_location.get("display_name"),
                        "repository_identity": raw_location.get("repository_identity"),
                        "provider_override": raw_location.get("provider_override"),
                        "transport_override": raw_location.get("transport_override"),
                        "is_default": is_default,
                        "last_observed_at": raw_location.get("last_observed_at"),
                    }
                )
            if default_count > 1:
                raise StorageError(
                    f"project {project_id!r} has more than one default "
                    "location on this host"
                )
            normalized.append(
                {
                    "project_id": project_id,
                    "name": name,
                    "aliases": aliases,
                    "default_provider": raw_project.get("default_provider"),
                    "default_transport": raw_project.get("default_transport"),
                    "context_sources": context_sources,
                    "locations": locations,
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
            project["context_sources"] = json.loads(project.pop("context_sources_json"))
            project["locations"] = [
                dict(location)
                for location in self.connection.execute(
                    """
                    SELECT * FROM project_locations
                    WHERE project_id = ?
                    ORDER BY host_id, is_default DESC, path
                    """,
                    (project["project_id"],),
                ).fetchall()
            ]
            projects.append(project)
        return projects

    def upsert_session(self, session: Mapping[str, Any]) -> dict[str, Any]:
        """Insert a provider session or update only explicitly supplied fields."""

        with self.transaction(immediate=True) as connection:
            return self._upsert_session_row(connection, session)

    @staticmethod
    def _upsert_session_row(
        connection: sqlite3.Connection,
        session: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            session_key = str(session["session_key"])
        except KeyError as error:
            raise StorageError("missing session field: session_key") from error
        if "surface_id" in session:
            raise StorageError(
                "session surface bindings must be changed with bind_surface"
            )
        _reject_mapping_controls(
            session,
            (
                "name",
                "purpose",
                "cwd",
                "provider_runtime_id",
                "tmux_session",
                "tmux_window",
                "tmux_pane",
                "metadata_source",
            ),
        )
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
        handoff_id = handoff_id or str(uuid.uuid4())
        handoff_id = _canonical_uuid_id(handoff_id, HandoffId, "handoff_id")
        _canonical_session_key(session_key)
        _canonical_host_id(source_host_id)

        with self.transaction(immediate=True) as connection:
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
                """
                UPDATE sessions SET latest_handoff_id = ? WHERE session_key = ?
                """,
                (handoff_id, session_key),
            )
            row = connection.execute(
                "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
            ).fetchone()
        result = _row_dict(row)
        assert result is not None
        return result

    def reserve_launch(
        self,
        request: Mapping[str, Any],
        *,
        request_id: str,
        lease_owner: str,
        capability_hash: str,
        expires_at: int,
        launch_id: str | None = None,
        created_at: int | None = None,
    ) -> ReservationResult:
        """Reserve a launch under ``BEGIN IMMEDIATE`` with retry idempotency."""

        capability_hash = _require_hash(capability_hash, "capability_hash")
        if not isinstance(lease_owner, str) or not lease_owner.strip():
            raise StorageError("lease_owner must be a non-empty string")
        _reject_controls(lease_owner, "lease_owner")
        fingerprint = launch_request_fingerprint(request)
        _canonical_host_id(request["host_id"])
        _canonical_provider(request["provider"])
        request_id = _canonical_plain_uuid(request_id, "request_id")
        launch_id = _canonical_uuid_id(
            launch_id or str(uuid.uuid4()), LaunchId, "launch_id"
        )
        for field, value_type in (
            ("project_id", ProjectId),
            ("location_id", LocationId),
            ("source_handoff_id", HandoffId),
        ):
            value = request.get(field)
            if value is not None:
                _canonical_uuid_id(value, value_type, field)
        target_session_key = request.get("target_session_key")
        if target_session_key is not None:
            _canonical_session_key(target_session_key)
        _reject_controls(request.get("cwd"), "cwd")
        if request["action"] == "manage" and any(
            request.get(field) is not None
            for field in (
                "project_id",
                "location_id",
                "cwd",
                "source_handoff_id",
                "target_session_key",
            )
        ):
            raise StorageError("manage launch cannot target project/session context")
        timestamp = now_ms() if created_at is None else created_at
        if expires_at <= timestamp:
            raise StorageError("launch lease must expire after creation")

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

            connection.execute(
                """
                INSERT INTO launch_intents(
                    launch_id, request_id, request_fingerprint, host_id,
                    provider, action, project_id, location_id, cwd,
                    source_handoff_id, target_session_key, transport, state,
                    lease_owner, capability_hash, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?)
                """,
                (
                    launch_id,
                    request_id,
                    fingerprint,
                    request["host_id"],
                    request["provider"],
                    request["action"],
                    request.get("project_id"),
                    request.get("location_id"),
                    request.get("cwd"),
                    request.get("source_handoff_id"),
                    request.get("target_session_key"),
                    request["transport"],
                    lease_owner,
                    capability_hash,
                    timestamp,
                    timestamp,
                    expires_at,
                ),
            )
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
            if launch["action"] == "new":
                observed_session.update(
                    project_id=launch["project_id"],
                    location_id=launch["location_id"],
                    cwd=launch["cwd"],
                    metadata_source="launch",
                    continued_from_handoff_id=launch["source_handoff_id"],
                )
            stored_session = self._upsert_session_row(connection, observed_session)

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
        _canonical_host_id(observation["host_id"])
        _canonical_provider(observation["provider"])
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
                if existing["payload_hash"] != payload_hash:
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
                    tmux_window, tmux_pane, observed_at, received_at, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        _canonical_host_id(event["host_id"])
        _canonical_provider(event["provider"])
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
                if existing["payload_hash"] != payload_hash:
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
                    payload_hash, diagnostic_code, diagnostic_detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            row = connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            assert row is not None
            result = dict(row)
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


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_EVENT_LIMIT",
    "IdentityConflict",
    "LaunchBindingResult",
    "Registry",
    "RegistryClosed",
    "RequestConflict",
    "ReservationResult",
    "StorageError",
    "connect_database",
    "handoff_content_hash",
    "launch_request_fingerprint",
    "now_ms",
]
