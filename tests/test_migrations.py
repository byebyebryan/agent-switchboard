from __future__ import annotations

import sqlite3
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from uuid import NAMESPACE_URL, uuid5

import pytest

from agent_switchboard.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    MigrationError,
    migrate,
)
from agent_switchboard.storage import connect_database


def configured_connection(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def stable_uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"agent-switchboard-migration-test:{label}"))


def open_database_worker(path: str) -> tuple[int, dict[str, str]]:
    connection = connect_database(path, busy_timeout_ms=30_000)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        metadata = dict(
            connection.execute("SELECT key, value FROM registry_metadata").fetchall()
        )
        return version, metadata
    finally:
        connection.close()


def test_migrations_are_explicit_contiguous_and_idempotent(tmp_path) -> None:
    database = tmp_path / "switchboard.db"
    connection = configured_connection(str(database))

    assert [migration.version for migration in MIGRATIONS] == [1, 2, 3, 4]
    assert [migration.name for migration in MIGRATIONS] == [
        "initial_registry",
        "remote_snapshot_cache",
        "name_provenance_runtime_index",
        "runtime_truth_ordering",
    ]
    assert migrate(connection, now=100) == CURRENT_SCHEMA_VERSION
    assert migrate(connection, now=200) == CURRENT_SCHEMA_VERSION

    applied = connection.execute(
        "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [tuple(row) for row in applied] == [
        (1, "initial_registry", 100),
        (2, "remote_snapshot_cache", 100),
        (3, "name_provenance_runtime_index", 100),
        (4, "runtime_truth_ordering", 100),
    ]
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    assert dict(
        connection.execute("SELECT key, value FROM registry_metadata").fetchall()
    ) == {"protocol_version": "1", "schema_version": "4"}
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_concurrent_first_open_is_serialized_across_32_processes(tmp_path) -> None:
    database = tmp_path / "switchboard.db"
    context = get_context("spawn")
    with ProcessPoolExecutor(max_workers=32, mp_context=context) as executor:
        results = list(executor.map(open_database_worker, [str(database)] * 32))

    assert (
        results
        == [
            (
                CURRENT_SCHEMA_VERSION,
                {
                    "protocol_version": "1",
                    "schema_version": str(CURRENT_SCHEMA_VERSION),
                },
            )
        ]
        * 32
    )


def test_upgrade_from_v1_preserves_registry_rows(tmp_path) -> None:
    database = tmp_path / "switchboard.db"
    connection = configured_connection(str(database))
    assert migrate(connection, target_version=1, now=10) == 1
    connection.execute(
        """
        INSERT INTO hosts(host_id, display_name, is_local, created_at, updated_at)
        VALUES ('11111111-1111-4111-8111-111111111111', 'starship', 1, 11, 11)
        """
    )
    connection.execute(
        """
        INSERT INTO projects(
            project_id, name, declared, created_at, updated_at
        ) VALUES (?, 'switchboard', 1, 12, 12)
        """,
        (stable_uuid("project-1"),),
    )
    connection.close()

    upgraded = connect_database(database)
    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 4
    assert (
        upgraded.execute(
            """
            SELECT display_name FROM hosts
            WHERE host_id = '11111111-1111-4111-8111-111111111111'
            """
        ).fetchone()[0]
        == "starship"
    )
    assert (
        upgraded.execute(
            "SELECT name FROM projects WHERE project_id = ?",
            (stable_uuid("project-1"),),
        ).fetchone()[0]
        == "switchboard"
    )
    assert (
        upgraded.execute(
            "SELECT name FROM sqlite_master WHERE name = 'remote_snapshots'"
        ).fetchone()[0]
        == "remote_snapshots"
    )
    assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
    upgraded.close()


def test_upgrade_from_v2_backfills_name_provenance_and_adds_runtime_index(
    tmp_path,
) -> None:
    database = tmp_path / "switchboard.db"
    connection = configured_connection(str(database))
    assert migrate(connection, target_version=2, now=10) == 2
    host_id = "11111111-1111-4111-8111-111111111111"
    connection.execute(
        """
        INSERT INTO hosts(host_id, display_name, is_local, created_at, updated_at)
        VALUES (?, 'host', 1, 10, 10)
        """,
        (host_id,),
    )
    historical_provider_name = "p" * 300
    rows = (
        (
            f"{host_id}:codex:22222222-2222-4222-8222-222222222222",
            "22222222-2222-4222-8222-222222222222",
            historical_provider_name,
            "provider",
        ),
        (
            f"{host_id}:codex:33333333-3333-4333-8333-333333333333",
            "33333333-3333-4333-8333-333333333333",
            "curated title",
            "launch",
        ),
        (
            f"{host_id}:codex:44444444-4444-4444-8444-444444444444",
            "44444444-4444-4444-8444-444444444444",
            None,
            "provider",
        ),
    )
    connection.executemany(
        """
        INSERT INTO sessions(
            session_key, provider, provider_session_id, name, host_id,
            first_observed_at, last_observed_at, metadata_source
        ) VALUES (?, 'codex', ?, ?, ?, 10, 10, ?)
        """,
        tuple((*row[:3], host_id, row[3]) for row in rows),
    )

    assert migrate(connection, now=20) == CURRENT_SCHEMA_VERSION
    names = connection.execute(
        """
        SELECT name, provider_name, name_source, metadata_source
        FROM sessions ORDER BY provider_session_id
        """
    ).fetchall()
    assert [tuple(row) for row in names] == [
        (
            historical_provider_name,
            historical_provider_name,
            "curated",
            "provider",
        ),
        ("curated title", None, "curated", "launch"),
        (None, None, "unknown", "provider"),
    ]
    assert (
        connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name = 'runtime_observations_host_recent'
            """
        ).fetchone()[0]
        == "runtime_observations_host_recent"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE sessions SET provider_name = ? WHERE provider_session_id = ?",
            ("x" * 513, "22222222-2222-4222-8222-222222222222"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE sessions SET name_source = 'guessed' WHERE provider_session_id = ?",
            ("22222222-2222-4222-8222-222222222222",),
        )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_failed_v2_to_v3_migration_rolls_back_provenance_and_index(
    tmp_path, monkeypatch
) -> None:
    import agent_switchboard.migrations as migration_module

    database = tmp_path / "switchboard.db"
    connection = configured_connection(str(database))
    migrate(connection, target_version=2, now=10)
    migration = migration_module.MIGRATIONS[2]
    broken = migration_module.Migration(
        3,
        "broken_name_provenance_runtime_index",
        (
            *migration.statements[:-1],
            "CREATE TABLE hosts(value TEXT)",
            migration.statements[-1],
        ),
    )
    monkeypatch.setattr(
        migration_module,
        "MIGRATIONS",
        (*migration_module.MIGRATIONS[:2], broken),
    )

    with pytest.raises(sqlite3.OperationalError):
        migration_module.migrate(connection, target_version=3, now=20)

    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
    }
    assert "provider_name" not in columns
    assert "name_source" not in columns
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND name = 'runtime_observations_host_recent'"
        ).fetchone()
        is None
    )
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert (
        connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2
    )
    connection.close()


def test_migration_rejects_inconsistent_or_newer_metadata(tmp_path) -> None:
    database = tmp_path / "switchboard.db"
    connection = configured_connection(str(database))
    migrate(connection, now=10)
    connection.execute("PRAGMA user_version = 99")

    with pytest.raises(MigrationError, match="metadata disagrees"):
        migrate(connection)

    connection.execute("PRAGMA user_version = 0")
    with pytest.raises(MigrationError, match="metadata disagrees"):
        migrate(connection)

    connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
    connection.execute(
        """
        INSERT INTO schema_migrations(version, name, applied_at)
        VALUES (99, 'future', 20)
        """
    )
    with pytest.raises(MigrationError, match="unknown migration versions"):
        migrate(connection)
    connection.close()


def test_migration_rejects_gapped_applied_versions(tmp_path) -> None:
    database = tmp_path / "switchboard.db"
    connection = configured_connection(str(database))
    migrate(connection, now=10)
    connection.execute("DELETE FROM schema_migrations WHERE version = 1")

    with pytest.raises(MigrationError, match="contiguous"):
        migrate(connection)
    connection.close()


@pytest.mark.parametrize(
    ("key", "value"),
    [("schema_version", "99"), ("protocol_version", "99")],
)
def test_migration_rejects_registry_metadata_disagreement(
    tmp_path, key: str, value: str
) -> None:
    database = tmp_path / f"{key}.db"
    connection = configured_connection(str(database))
    migrate(connection, now=10)
    connection.execute(
        "UPDATE registry_metadata SET value = ? WHERE key = ?", (value, key)
    )

    with pytest.raises(MigrationError, match="registry metadata disagrees"):
        migrate(connection)
    connection.close()


def test_schema_rejects_malformed_uuid_and_open_ended_enum_values(tmp_path) -> None:
    connection = configured_connection(str(tmp_path / "switchboard.db"))
    migrate(connection, now=10)
    host_id = "11111111-1111-4111-8111-111111111111"
    connection.execute(
        """
        INSERT INTO hosts(host_id, display_name, is_local, created_at, updated_at)
        VALUES (?, 'host', 1, 10, 10)
        """,
        (host_id,),
    )

    malformed = "1111111--1111-4111-8111-111111111111"
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO projects(project_id, name, created_at, updated_at)
            VALUES (?, 'bad id', 10, 10)
            """,
            (malformed,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO projects(
                project_id, name, default_transport, created_at, updated_at
            ) VALUES (?, 'bad transport', 'screen', 10, 10)
            """,
            (stable_uuid("bad-transport"),),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO sessions(
                session_key, provider, provider_session_id, host_id,
                first_observed_at, last_observed_at, state_confidence
            ) VALUES (?, 'codex', ?, ?, 10, 10, 'certain')
            """,
            (
                f"{host_id}:codex:22222222-2222-4222-8222-222222222222",
                "22222222-2222-4222-8222-222222222222",
                host_id,
            ),
        )
    connection.close()


def test_launch_schema_enforces_atomic_surface_and_lease_boundaries(tmp_path) -> None:
    connection = configured_connection(str(tmp_path / "switchboard.db"))
    migrate(connection, now=10)
    host_id = "11111111-1111-4111-8111-111111111111"
    connection.execute(
        """
        INSERT INTO hosts(host_id, display_name, is_local, created_at, updated_at)
        VALUES (?, 'host', 1, 10, 10)
        """,
        (host_id,),
    )

    def insert_manager_launch(
        label: str,
        *,
        state: str = "reserved",
        cwd: str | None = None,
        lease_owner: str | None = "worker",
        failure_code: str | None = None,
    ) -> str:
        launch_id = stable_uuid(f"launch-{label}")
        connection.execute(
            """
            INSERT INTO launch_intents(
                launch_id, request_id, request_fingerprint, host_id, provider,
                action, cwd, transport, state, lease_owner, capability_hash,
                created_at, updated_at, expires_at, failure_code
            ) VALUES (?, ?, ?, ?, 'claude', 'manage', ?, 'tmux', ?, ?, ?,
                      10, 10, 100, ?)
            """,
            (
                launch_id,
                stable_uuid(f"request-{label}"),
                "a" * 64,
                host_id,
                cwd,
                state,
                lease_owner,
                "b" * 64,
                failure_code,
            ),
        )
        return launch_id

    with pytest.raises(sqlite3.IntegrityError):
        insert_manager_launch("cwd", cwd="/work/not-allowed")
    with pytest.raises(sqlite3.IntegrityError, match="inserted as reserved"):
        insert_manager_launch(
            "terminal-insert",
            state="failed",
            lease_owner=None,
            failure_code="failed_before_reservation",
        )

    launch_id = insert_manager_launch("valid")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            UPDATE launch_intents
            SET state = 'surface_ready', updated_at = 11
            WHERE launch_id = ?
            """,
            (launch_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            UPDATE launch_intents
            SET state = 'failed', failure_code = 'failed', updated_at = 11
            WHERE launch_id = ?
            """,
            (launch_id,),
        )

    surface_id = stable_uuid("manager-surface")
    connection.execute(
        """
        INSERT INTO surfaces(
            surface_id, host_id, provider, transport, transport_locator, role,
            launch_id, created_at, last_observed_at
        ) VALUES (?, ?, 'claude', 'tmux', 'tmux:manager', 'provider_manager',
                  ?, 10, 10)
        """,
        (surface_id, host_id, launch_id),
    )
    connection.execute(
        """
        UPDATE launch_intents
        SET state = 'surface_ready', surface_id = ?, updated_at = 11
        WHERE launch_id = ?
        """,
        (surface_id, launch_id),
    )
    with pytest.raises(sqlite3.IntegrityError, match="surface is immutable"):
        connection.execute(
            "UPDATE launch_intents SET surface_id = NULL WHERE launch_id = ?",
            (launch_id,),
        )
    connection.close()


def test_failed_migration_rolls_back_all_statements(tmp_path, monkeypatch) -> None:
    import agent_switchboard.migrations as migration_module

    database = tmp_path / "switchboard.db"
    connection = configured_connection(str(database))
    migrate(connection, target_version=1, now=10)
    broken = migration_module.Migration(
        2,
        "broken",
        (
            "CREATE TABLE should_roll_back(value TEXT)",
            "CREATE TABLE hosts(value TEXT)",
        ),
    )
    monkeypatch.setattr(
        migration_module,
        "MIGRATIONS",
        (migration_module.MIGRATIONS[0], broken),
    )

    with pytest.raises(sqlite3.OperationalError):
        migration_module.migrate(connection, target_version=2, now=20)

    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'should_roll_back'"
        ).fetchone()
        is None
    )
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert (
        connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    )
    connection.close()
