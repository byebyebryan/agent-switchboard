"""Fresh Phase 6 schema migrations, independent from the 0.2 chain."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from .. import PROTOCOL_VERSION, SCHEMA_VERSION
from ..domain import ActivationState, GenerationId, HostId
from . import v0001_baseline


class MigrationError(RuntimeError):
    """The Phase 6 registry cannot be initialized or verified safely."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        v0001_baseline.VERSION,
        v0001_baseline.NAME,
        v0001_baseline.STATEMENTS,
    ),
)
CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version


def _existing_objects(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type IN ('table', 'index', 'trigger')
              AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
    }


def _metadata(connection: sqlite3.Connection) -> dict[str, object]:
    row = connection.execute(
        "SELECT * FROM registry_metadata WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise MigrationError("Phase 6 registry metadata is missing")
    return dict(zip(row.keys(), row, strict=True))


def _validate_current(
    connection: sqlite3.Connection,
    generation_id: GenerationId,
    local_host_id: HostId,
) -> None:
    applied = connection.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall()
    expected = [(migration.version, migration.name) for migration in MIGRATIONS]
    if [tuple(row) for row in applied] != expected:
        raise MigrationError(
            "incompatible_registry_generation: Phase 6 migration history mismatch"
        )
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if user_version != CURRENT_SCHEMA_VERSION:
        raise MigrationError("incompatible_registry_generation: user_version mismatch")
    metadata = _metadata(connection)
    expected_values = {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "generation_id": str(generation_id),
        "local_host_id": str(local_host_id),
    }
    for key, expected_value in expected_values.items():
        if metadata[key] != expected_value:
            raise MigrationError(f"incompatible_registry_generation: {key} mismatch")
    try:
        ActivationState(str(metadata["activation_state"]))
    except ValueError as error:
        raise MigrationError(
            "incompatible_registry_generation: invalid activation state"
        ) from error
    host = connection.execute(
        "SELECT is_local FROM hosts WHERE host_id = ?",
        (str(local_host_id),),
    ).fetchone()
    if host is None or int(host[0]) != 1:
        raise MigrationError(
            "incompatible_registry_generation: local host row mismatch"
        )
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise MigrationError("Phase 6 registry has foreign-key violations")


def migrate(
    connection: sqlite3.Connection,
    *,
    generation_id: GenerationId,
    local_host_id: HostId,
    local_display_name: str,
    initial_activation_state: ActivationState = ActivationState.CUTOVER_STAGED,
    now: int | None = None,
) -> int:
    """Initialize an empty Phase 6 registry or validate the exact baseline."""

    if connection.in_transaction:
        raise MigrationError("migrations require an idle connection")
    timestamp = int(time.time() * 1_000) if now is None else now
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise MigrationError("migration timestamp must be a non-negative integer")
    connection.execute("BEGIN EXCLUSIVE")
    try:
        objects = _existing_objects(connection)
        if not objects:
            for statement in MIGRATIONS[0].statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (MIGRATIONS[0].version, MIGRATIONS[0].name, timestamp),
            )
            connection.execute(
                """
                INSERT INTO hosts(
                    host_id, display_name, is_local, created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?)
                """,
                (str(local_host_id), local_display_name, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO registry_metadata(
                    singleton, schema_version, protocol_version, generation_id,
                    local_host_id, activation_state, created_at, committed_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    SCHEMA_VERSION,
                    PROTOCOL_VERSION,
                    str(generation_id),
                    str(local_host_id),
                    initial_activation_state.value,
                    timestamp,
                    (
                        timestamp
                        if initial_activation_state is ActivationState.COMMITTED
                        else None
                    ),
                ),
            )
            connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        elif "schema_migrations" not in objects or "registry_metadata" not in objects:
            raise MigrationError(
                "incompatible_registry_generation: database is not an empty "
                "Phase 6 registry"
            )
        _validate_current(connection, generation_id, local_host_id)
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return CURRENT_SCHEMA_VERSION


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MIGRATIONS",
    "Migration",
    "MigrationError",
    "migrate",
]
