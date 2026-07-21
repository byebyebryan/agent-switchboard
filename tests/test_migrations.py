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
from agent_switchboard.storage import Registry, connect_database


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

    assert [migration.version for migration in MIGRATIONS] == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
    ]
    assert [migration.name for migration in MIGRATIONS] == [
        "initial_registry",
        "remote_snapshot_cache",
        "name_provenance_runtime_index",
        "runtime_truth_ordering",
        "history_launch",
        "agent_tools",
        "repository_checkouts",
        "tasks",
        "imported_task_handoffs",
        "runtime_worktree_claims",
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
        (5, "history_launch", 100),
        (6, "agent_tools", 100),
        (7, "repository_checkouts", 100),
        (8, "tasks", 100),
        (9, "imported_task_handoffs", 100),
        (10, "runtime_worktree_claims", 100),
    ]
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
    assert dict(
        connection.execute("SELECT key, value FROM registry_metadata").fetchall()
    ) == {"protocol_version": "2", "schema_version": "10"}
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
                    "protocol_version": "2",
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
    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 10
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


def test_upgrade_from_v8_links_imported_handoffs_append_only(tmp_path) -> None:
    connection = configured_connection(str(tmp_path / "switchboard.db"))
    assert migrate(connection, target_version=8, now=10) == 8
    local_host_id = stable_uuid("v9-local-host")
    remote_host_id = stable_uuid("v9-remote-host")
    project_id = stable_uuid("v9-project")
    task_id = stable_uuid("v9-task")
    source_task_id = stable_uuid("v9-source-task")
    handoff_id = stable_uuid("v9-handoff")
    source_session_key = f"{remote_host_id}:codex:{stable_uuid('v9-session')}"
    connection.executemany(
        """
        INSERT INTO hosts(host_id, display_name, is_local, created_at, updated_at)
        VALUES (?, ?, ?, 10, 10)
        """,
        ((local_host_id, "local", 1), (remote_host_id, "remote", 0)),
    )
    connection.execute(
        """
        INSERT INTO projects(project_id, name, declared, created_at, updated_at)
        VALUES (?, 'project', 1, 10, 10)
        """,
        (project_id,),
    )
    connection.execute(
        """
        INSERT INTO tasks(
            task_id, host_id, project_id, checkout_id, title, purpose,
            preferred_provider, status, pinned, current_session_key,
            created_at, updated_at, closed_at
        ) VALUES (?, ?, ?, NULL, 'destination', NULL, 'codex', 'open', 0,
                  NULL, 10, 10, NULL)
        """,
        (task_id, local_host_id, project_id),
    )
    connection.execute(
        """
        INSERT INTO handoffs(
            handoff_id, session_key, sequence, summary, next_action, source,
            source_host_id, created_at, content_hash
        ) VALUES (?, ?, 1, 'summary', 'next', 'imported', ?, 10, ?)
        """,
        (handoff_id, source_session_key, remote_host_id, "a" * 64),
    )

    assert migrate(connection, now=20) == CURRENT_SCHEMA_VERSION
    connection.execute(
        """
        INSERT INTO task_imported_handoffs(
            task_id, handoff_id, source_task_id, source_project_id, imported_at
        ) VALUES (?, ?, ?, ?, 20)
        """,
        (task_id, handoff_id, source_task_id, project_id),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "UPDATE task_imported_handoffs SET imported_at = 21 WHERE handoff_id = ?",
            (handoff_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "DELETE FROM task_imported_handoffs WHERE handoff_id = ?",
            (handoff_id,),
        )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_v6_to_v8_preserves_identity_and_leaves_sessions_in_inbox(tmp_path) -> None:
    database = tmp_path / "switchboard.db"
    connection = configured_connection(str(database))
    assert migrate(connection, target_version=6, now=10) == 6
    host_id = "11111111-1111-4111-8111-111111111111"
    project_id = stable_uuid("v6-project")
    location_id = stable_uuid("v6-location")
    session_id = stable_uuid("v6-session")
    session_key = f"{host_id}:codex:{session_id}"
    connection.execute(
        """
        INSERT INTO hosts(host_id, display_name, is_local, created_at, updated_at)
        VALUES (?, 'host', 1, 10, 10)
        """,
        (host_id,),
    )
    connection.execute(
        """
        INSERT INTO projects(
            project_id, name, aliases_json, default_provider,
            default_transport, context_sources_json, declared,
            created_at, updated_at
        ) VALUES (?, 'Switchboard', '[]', 'codex', 'tmux',
                  '["README.md"]', 1, 11, 11)
        """,
        (project_id,),
    )
    connection.execute(
        """
        INSERT INTO project_locations(
            location_id, project_id, host_id, path, display_name,
            is_default, declared, last_observed_at, created_at, updated_at
        ) VALUES (?, ?, ?, '/work/switchboard', 'main', 1, 1, 12, 12, 12)
        """,
        (location_id, project_id, host_id),
    )
    connection.execute(
        """
        INSERT INTO sessions(
            session_key, project_id, location_id, provider,
            provider_session_id, cwd, host_id, first_observed_at,
            last_observed_at
        ) VALUES (?, ?, ?, 'codex', ?, '/work/switchboard', ?, 13, 13)
        """,
        (session_key, project_id, location_id, session_id, host_id),
    )

    assert migrate(connection, target_version=8, now=20) == 8

    repository = connection.execute(
        "SELECT * FROM repositories WHERE repository_id = ?", (project_id,)
    ).fetchone()
    membership = connection.execute(
        "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
    ).fetchone()
    checkout = connection.execute(
        "SELECT * FROM checkouts WHERE checkout_id = ?", (location_id,)
    ).fetchone()
    session = connection.execute(
        "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
    ).fetchone()
    assert repository["context_sources_json"] == '["README.md"]'
    assert repository["kind"] == "git"
    assert repository["kind_provisional"] == 1
    assert membership["repository_id"] == project_id
    assert membership["is_primary"] == 1
    assert checkout["repository_id"] == project_id
    assert checkout["kind"] == "main"
    assert session["checkout_id"] == location_id
    assert session["task_id"] is None
    assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()

    with Registry(database) as registry:
        registry.materialize_projects(
            host_id,
            [
                {
                    "project_id": project_id,
                    "name": "Switchboard",
                    "default_provider": "codex",
                    "default_transport": "tmux",
                    "repositories": [
                        {
                            "repository_id": project_id,
                            "name": "Switchboard",
                            "kind": "directory",
                            "is_primary": True,
                            "context_sources": ["README.md"],
                            "checkouts": [
                                {
                                    "checkout_id": location_id,
                                    "path": "/work/switchboard",
                                    "kind": "directory",
                                    "is_default": True,
                                }
                            ],
                        }
                    ],
                }
            ],
            observed_at=30,
        )
    verified = configured_connection(str(database))
    corrected = verified.execute(
        "SELECT kind, kind_provisional FROM repositories WHERE repository_id = ?",
        (project_id,),
    ).fetchone()
    assert (corrected["kind"], corrected["kind_provisional"]) == ("directory", 0)
    assert verified.execute("PRAGMA foreign_key_check").fetchall() == []
    verified.close()


def test_upgrade_from_v4_preserves_launches_and_adds_claude_history(tmp_path) -> None:
    connection = configured_connection(str(tmp_path / "switchboard.db"))
    assert migrate(connection, target_version=4, now=10) == 4
    host_id = "11111111-1111-4111-8111-111111111111"
    project_id = stable_uuid("history-project")
    location_id = stable_uuid("history-location")
    connection.execute(
        """
        INSERT INTO hosts(host_id, display_name, is_local, created_at, updated_at)
        VALUES (?, 'host', 1, 10, 10)
        """,
        (host_id,),
    )
    connection.execute(
        """
        INSERT INTO projects(project_id, name, created_at, updated_at)
        VALUES (?, 'project', 10, 10)
        """,
        (project_id,),
    )
    connection.execute(
        """
        INSERT INTO project_locations(
            location_id, project_id, host_id, path, is_default,
            created_at, updated_at
        ) VALUES (?, ?, ?, '/work/project', 1, 10, 10)
        """,
        (location_id, project_id, host_id),
    )
    old_launch_id = stable_uuid("pre-history-launch")
    launch_values = (
        old_launch_id,
        stable_uuid("pre-history-request"),
        host_id,
        project_id,
        location_id,
    )
    connection.execute(
        """
        INSERT INTO launch_intents(
            launch_id, request_id, request_fingerprint, host_id, provider,
            action, project_id, location_id, cwd, transport, state,
            lease_owner, capability_hash, created_at, updated_at, expires_at
        ) VALUES (?, ?, ?, ?, 'claude', 'new', ?, ?, '/work/project', 'tmux',
                  'reserved', 'worker', ?, 10, 10, 100)
        """,
        (*launch_values[:2], "a" * 64, *launch_values[2:], "b" * 64),
    )

    assert migrate(connection, now=20) == CURRENT_SCHEMA_VERSION
    assert (
        connection.execute(
            "SELECT action FROM launch_intents WHERE launch_id = ?", (old_launch_id,)
        ).fetchone()[0]
        == "new"
    )

    history_values = (
        stable_uuid("history-launch"),
        stable_uuid("history-request"),
        "c" * 64,
        host_id,
        project_id,
        location_id,
        "d" * 64,
    )
    connection.execute(
        """
        INSERT INTO launch_intents(
            launch_id, request_id, request_fingerprint, host_id, provider,
            action, project_id, checkout_id, cwd, transport, state,
            lease_owner, capability_hash, created_at, updated_at, expires_at
        ) VALUES (?, ?, ?, ?, 'claude', 'history', ?, ?, '/work/project',
                  'tmux', 'reserved', 'worker', ?, 20, 20, 100)
        """,
        history_values,
    )
    old_launch = connection.execute(
        "SELECT * FROM launch_intents WHERE launch_id = ?", (old_launch_id,)
    ).fetchone()
    assert old_launch["agent_capability_hash"] is None
    connection.execute(
        "UPDATE launch_intents SET agent_capability_hash = ? WHERE launch_id = ?",
        ("e" * 64, old_launch_id),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE launch_intents SET agent_capability_hash = ? WHERE launch_id = ?",
            ("e" * 64, history_values[0]),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE launch_intents SET provider = 'codex' WHERE launch_id = ?",
            (history_values[0],),
        )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


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
