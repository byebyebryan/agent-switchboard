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
    v0004_runtime_truth_ordering,
    v0005_history_launch,
    v0006_agent_tools,
    v0007_repository_checkouts,
    v0008_tasks,
    v0009_imported_task_handoffs,
)

PROTOCOL_VERSION = 2


class MigrationError(RuntimeError):
    """Raised when a database cannot be migrated safely."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One monotonically numbered migration."""

    version: int
    name: str
    statements: tuple[str, ...]
    requires_foreign_keys_off: bool = False


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
    Migration(
        v0004_runtime_truth_ordering.VERSION,
        v0004_runtime_truth_ordering.NAME,
        v0004_runtime_truth_ordering.STATEMENTS,
    ),
    Migration(
        v0005_history_launch.VERSION,
        v0005_history_launch.NAME,
        v0005_history_launch.STATEMENTS,
        requires_foreign_keys_off=v0005_history_launch.REQUIRES_FOREIGN_KEYS_OFF,
    ),
    Migration(
        v0006_agent_tools.VERSION,
        v0006_agent_tools.NAME,
        v0006_agent_tools.STATEMENTS,
        requires_foreign_keys_off=v0006_agent_tools.REQUIRES_FOREIGN_KEYS_OFF,
    ),
    Migration(
        v0007_repository_checkouts.VERSION,
        v0007_repository_checkouts.NAME,
        v0007_repository_checkouts.STATEMENTS,
        requires_foreign_keys_off=(
            v0007_repository_checkouts.REQUIRES_FOREIGN_KEYS_OFF
        ),
    ),
    Migration(
        v0008_tasks.VERSION,
        v0008_tasks.NAME,
        v0008_tasks.STATEMENTS,
        requires_foreign_keys_off=v0008_tasks.REQUIRES_FOREIGN_KEYS_OFF,
    ),
    Migration(
        v0009_imported_task_handoffs.VERSION,
        v0009_imported_task_handoffs.NAME,
        v0009_imported_task_handoffs.STATEMENTS,
    ),
)
CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version


def _protocol_for_schema(schema_version: int) -> int:
    return 1 if schema_version <= 6 else 2


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


def _validated_current(
    connection: sqlite3.Connection,
    migrations: tuple[Migration, ...],
    target_version: int,
) -> int:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY CHECK (version > 0),
            name TEXT NOT NULL UNIQUE,
            applied_at INTEGER NOT NULL CHECK (applied_at >= 0)
        )
        """
    )
    applied = {
        int(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    }
    unexpected = sorted(set(applied) - {migration.version for migration in migrations})
    if unexpected:
        raise MigrationError(f"database has unknown migration versions: {unexpected}")
    applied_versions = tuple(applied)
    expected_versions = tuple(range(1, len(applied_versions) + 1))
    if applied_versions != expected_versions:
        raise MigrationError(
            "applied migration versions must be contiguous from 1: "
            f"expected {expected_versions}, got {applied_versions}"
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
            "protocol_version": str(_protocol_for_schema(current)),
            "schema_version": str(current),
        }
        if metadata != expected_metadata:
            raise MigrationError(
                "registry metadata disagrees with applied migrations: "
                f"expected {expected_metadata}, got {metadata}"
            )
    return current


def _apply_migration(
    connection: sqlite3.Connection, migration: Migration, applied_at: int
) -> None:
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
            (str(_protocol_for_schema(1)), applied_at),
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

    applied_at = int(time.time() * 1000) if now is None else now
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _validated_current(connection, migrations, target_version)
        for migration in migrations:
            if migration.version <= current:
                continue
            if migration.version > target_version:
                break
            if migration.requires_foreign_keys_off:
                break
            _apply_migration(connection, migration, applied_at)
            current = migration.version
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise

    original_foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    for migration in migrations:
        if (
            not migration.requires_foreign_keys_off
            or migration.version > target_version
        ):
            continue
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = _validated_current(connection, migrations, target_version)
            if migration.version > current:
                if migration.version != current + 1:
                    raise MigrationError(
                        "foreign-key migration is not the next schema version"
                    )
                _apply_migration(connection, migration, applied_at)
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise MigrationError(
                        f"migration {migration.version} created foreign-key violations"
                    )
                current = migration.version
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA legacy_alter_table = OFF")
            connection.execute(
                f"PRAGMA foreign_keys = {1 if original_foreign_keys else 0}"
            )

    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _validated_current(connection, migrations, target_version)
        for migration in migrations:
            if migration.version <= current:
                continue
            if migration.version > target_version:
                break
            if migration.requires_foreign_keys_off:
                raise MigrationError(
                    "foreign-key migration is not the next schema version"
                )
            if migration.version != current + 1:
                raise MigrationError("migration is not the next schema version")
            _apply_migration(connection, migration, applied_at)
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
