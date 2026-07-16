"""Explicit, transactional SQLite schema migrations."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass

from . import (
    v0001_initial,
    v0002_remote_cache,
    v0003_name_provenance_runtime_index,
)

PROTOCOL_VERSION = 1


class MigrationError(RuntimeError):
    """Raised when a database cannot be migrated safely."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One monotonically numbered migration."""

    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(v0001_initial.VERSION, v0001_initial.NAME, v0001_initial.STATEMENTS),
    Migration(
        v0002_remote_cache.VERSION,
        v0002_remote_cache.NAME,
        v0002_remote_cache.STATEMENTS,
    ),
    Migration(
        v0003_name_provenance_runtime_index.VERSION,
        v0003_name_provenance_runtime_index.NAME,
        v0003_name_provenance_runtime_index.STATEMENTS,
    ),
)
CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version


def _validate_migrations(migrations: Iterable[Migration]) -> tuple[Migration, ...]:
    ordered = tuple(migrations)
    expected = tuple(range(1, len(ordered) + 1))
    actual = tuple(migration.version for migration in ordered)
    if actual != expected:
        raise MigrationError(
            "migration versions must be contiguous from 1: "
            f"expected {expected}, got {actual}"
        )
    if len({migration.name for migration in ordered}) != len(ordered):
        raise MigrationError("migration names must be unique")
    return ordered


def migrate(
    connection: sqlite3.Connection,
    *,
    target_version: int = CURRENT_SCHEMA_VERSION,
    now: int | None = None,
) -> int:
    """Upgrade *connection* to ``target_version`` using atomic migrations.

    Downgrades and databases created by a newer Switchboard are rejected.  The
    caller must enable foreign keys before invoking this function.
    """

    migrations = _validate_migrations(MIGRATIONS)
    if target_version < 0 or target_version > CURRENT_SCHEMA_VERSION:
        raise MigrationError(f"unsupported target schema version: {target_version}")

    if connection.in_transaction:
        raise MigrationError("migrations require an idle connection")
    connection.execute("BEGIN IMMEDIATE")
    try:
        # Acquire the writer lock before reading migration metadata. Another
        # process may be opening the same fresh registry at the same time.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                name TEXT NOT NULL UNIQUE,
                applied_at INTEGER NOT NULL CHECK (applied_at >= 0)
            )
            """
        )
        applied_rows = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied = {int(row[0]): str(row[1]) for row in applied_rows}

        unexpected = sorted(
            set(applied) - {migration.version for migration in migrations}
        )
        if unexpected:
            raise MigrationError(
                f"database has unknown migration versions: {unexpected}"
            )
        applied_versions = tuple(applied)
        expected_applied_versions = tuple(range(1, len(applied_versions) + 1))
        if applied_versions != expected_applied_versions:
            raise MigrationError(
                "applied migration versions must be contiguous from 1: "
                f"expected {expected_applied_versions}, got {applied_versions}"
            )
        for migration in migrations:
            recorded_name = applied.get(migration.version)
            if recorded_name is not None and recorded_name != migration.name:
                raise MigrationError(
                    f"migration {migration.version} name mismatch: "
                    f"expected {migration.name!r}, got {recorded_name!r}"
                )

        current = max(applied, default=0)
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version != current:
            raise MigrationError(
                "schema metadata disagrees: "
                f"user_version={user_version}, migrations={current}"
            )
        if current > target_version:
            raise MigrationError(
                f"database schema {current} is newer than requested {target_version}"
            )
        if current > 0:
            metadata = dict(
                connection.execute(
                    """
                    SELECT key, value FROM registry_metadata
                    WHERE key IN ('protocol_version', 'schema_version')
                    """
                ).fetchall()
            )
            expected_metadata = {
                "protocol_version": str(PROTOCOL_VERSION),
                "schema_version": str(current),
            }
            if metadata != expected_metadata:
                raise MigrationError(
                    "registry metadata disagrees with applied migrations: "
                    f"expected {expected_metadata}, got {metadata}"
                )

        applied_at = int(time.time() * 1000) if now is None else now
        for migration in migrations:
            if migration.version <= current or migration.version > target_version:
                continue
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (migration.version, migration.name, applied_at),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")
            if migration.version == 1:
                connection.execute(
                    """
                    INSERT INTO registry_metadata(key, value, updated_at)
                    VALUES ('protocol_version', ?, ?)
                    """,
                    (str(PROTOCOL_VERSION), applied_at),
                )
            connection.execute(
                """
                INSERT INTO registry_metadata(key, value, updated_at)
                VALUES ('schema_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (str(migration.version), applied_at),
            )
            current = migration.version
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise

    return current


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MIGRATIONS",
    "PROTOCOL_VERSION",
    "Migration",
    "MigrationError",
    "migrate",
]
