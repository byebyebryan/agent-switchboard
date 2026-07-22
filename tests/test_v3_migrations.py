from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest

from agent_switchboard._v3.domain import GenerationId, HostId
from agent_switchboard._v3.migrations import MigrationError
from agent_switchboard._v3.storage import Registry, connect_database
from agent_switchboard.storage import Registry as V2Registry

GENERATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_GENERATION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
HOST = "11111111-1111-4111-8111-111111111111"
OTHER_HOST = "22222222-2222-4222-8222-222222222222"
PROJECT = "33333333-3333-4333-8333-333333333333"
OTHER_PROJECT = "44444444-4444-4444-8444-444444444444"
REPOSITORY = "55555555-5555-4555-8555-555555555555"
CHECKOUT = "66666666-6666-4666-8666-666666666666"
CONTEXT = "77777777-7777-4777-8777-777777777777"
OTHER_CONTEXT = "88888888-8888-4888-8888-888888888888"
WORKSPACE = "99999999-9999-4999-8999-999999999999"
OTHER_WORKSPACE = "99999999-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TASK_ONE = "aaaaaaaa-1111-4111-8111-111111111111"
TASK_TWO = "aaaaaaaa-2222-4222-8222-222222222222"

EXPECTED_TABLES = {
    "agent_capabilities",
    "checkouts",
    "completion_handoffs",
    "control_turns",
    "desktop_attachment_leases",
    "frame_placements",
    "frame_sessions",
    "frames",
    "host_state_cache",
    "hosts",
    "launch_intents",
    "project_repositories",
    "projects",
    "provider_sessions",
    "recoveries",
    "registry_metadata",
    "repositories",
    "request_records",
    "schema_migrations",
    "session_handoffs",
    "surfaces",
    "tmux_servers",
    "transition_briefs",
    "user_views",
    "view_transitions",
    "work_contexts",
}


def open_worker(path: str) -> tuple[int, str, str]:
    connection = connect_database(
        path,
        generation_id=GenerationId(GENERATION),
        local_host_id=HostId(HOST),
        local_display_name="starship",
        busy_timeout_ms=30_000,
        now=10,
    )
    try:
        metadata = connection.execute(
            "SELECT schema_version, generation_id, local_host_id FROM registry_metadata"
        ).fetchone()
        return int(metadata[0]), str(metadata[1]), str(metadata[2])
    finally:
        connection.close()


def registry(path: Path | str = ":memory:") -> Registry:
    return Registry(
        path,
        generation_id=GenerationId(GENERATION),
        local_host_id=HostId(HOST),
        local_display_name="starship",
        now=10,
    )


def test_fresh_baseline_has_exact_private_schema_and_metadata() -> None:
    with registry() as opened:
        tables = {
            str(row[0])
            for row in opened.connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == EXPECTED_TABLES
        assert not {"tasks", "remote_snapshots", "events"} & tables
        assert opened.metadata() == {
            "singleton": 1,
            "schema_version": 1,
            "protocol_version": 1,
            "generation_id": GENERATION,
            "local_host_id": HOST,
            "activation_state": "cutover_staged",
            "created_at": 10,
            "committed_at": None,
        }
        assert opened.connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert opened.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_file_registry_is_private_and_reopen_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "generation" / "switchboard.db"
    with registry(database):
        pass
    with registry(database) as reopened:
        assert reopened.metadata()["generation_id"] == GENERATION
    assert os.stat(database).st_mode & 0o777 == 0o600


def test_concurrent_first_open_converges_on_one_baseline(tmp_path: Path) -> None:
    database = str(tmp_path / "switchboard.db")
    context = get_context("spawn")
    with ProcessPoolExecutor(max_workers=8, mp_context=context) as executor:
        results = list(executor.map(open_worker, [database] * 8))
    assert results == [(1, GENERATION, HOST)] * 8


@pytest.mark.parametrize(
    ("generation", "host", "message"),
    [
        (OTHER_GENERATION, HOST, "generation_id mismatch"),
        (GENERATION, OTHER_HOST, "local_host_id mismatch"),
    ],
)
def test_reopen_rejects_generation_and_host_mismatch(
    tmp_path: Path,
    generation: str,
    host: str,
    message: str,
) -> None:
    database = tmp_path / "switchboard.db"
    with registry(database):
        pass
    with pytest.raises(MigrationError, match=message):
        connect_database(
            database,
            generation_id=GenerationId(generation),
            local_host_id=HostId(host),
            local_display_name="starship",
        )


def test_old_v10_and_partial_databases_fail_closed(tmp_path: Path) -> None:
    old_database = tmp_path / "old.db"
    with V2Registry(old_database):
        pass
    with pytest.raises(MigrationError, match="migration history mismatch"):
        registry(old_database)

    partial = tmp_path / "partial.db"
    connection = sqlite3.connect(partial)
    connection.execute("CREATE TABLE unrelated(value TEXT)")
    connection.close()
    with pytest.raises(MigrationError, match="not an empty Phase 6 registry"):
        registry(partial)


def seed_catalog(connection: sqlite3.Connection) -> None:
    connection.executemany(
        """
        INSERT INTO projects(
            project_id, name, created_at, updated_at
        ) VALUES (?, ?, 10, 10)
        """,
        ((PROJECT, "one"), (OTHER_PROJECT, "two")),
    )
    connection.execute(
        """
        INSERT INTO repositories(
            repository_id, name, kind, created_at, updated_at
        ) VALUES (?, 'repo', 'git', 10, 10)
        """,
        (REPOSITORY,),
    )
    connection.executemany(
        """
        INSERT INTO project_repositories(
            project_id, repository_id, is_primary, created_at, updated_at
        ) VALUES (?, ?, 1, 10, 10)
        """,
        ((PROJECT, REPOSITORY), (OTHER_PROJECT, REPOSITORY)),
    )
    connection.execute(
        """
        INSERT INTO checkouts(
            checkout_id, repository_id, host_id, path, kind,
            is_default, created_at, updated_at
        ) VALUES (?, ?, ?, '/tmp/phase6', 'main', 1, 10, 10)
        """,
        (CHECKOUT, REPOSITORY, HOST),
    )


def test_schema_enforces_workspace_claim_and_acyclic_parent_invariants() -> None:
    with registry() as opened:
        connection = opened.connection
        seed_catalog(connection)
        connection.executemany(
            """
            INSERT INTO work_contexts(
                work_context_id, host_id, project_id, checkout_id,
                claim_state, claim_generation, foreground_frame_id,
                background_state, updated_at
            ) VALUES (?, ?, ?, ?, 'released', 0, NULL, 'safe', 10)
            """,
            (
                (CONTEXT, HOST, PROJECT, CHECKOUT),
                (OTHER_CONTEXT, HOST, OTHER_PROJECT, CHECKOUT),
            ),
        )
        connection.execute(
            """
            INSERT INTO frames(
                frame_id, host_id, project_id, role, parent_frame_id,
                work_context_id, title, lifecycle_state, created_by,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'workspace', NULL, ?, 'workspace', 'open',
                      'user', 10, 10)
            """,
            (WORKSPACE, HOST, PROJECT, CONTEXT),
        )
        connection.execute(
            """
            INSERT INTO frames(
                frame_id, host_id, project_id, role, parent_frame_id,
                work_context_id, title, lifecycle_state, created_by,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'workspace', NULL, ?, 'other', 'open',
                      'user', 10, 10)
            """,
            (OTHER_WORKSPACE, HOST, OTHER_PROJECT, OTHER_CONTEXT),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                """
                INSERT INTO frames(
                    frame_id, host_id, project_id, role, parent_frame_id,
                    work_context_id, title, lifecycle_state, created_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'workspace', NULL, ?, 'duplicate', 'open',
                          'user', 10, 10)
                """,
                (TASK_ONE, HOST, PROJECT, CONTEXT),
            )
        connection.execute(
            """
            UPDATE work_contexts
            SET claim_state = 'held', claim_generation = 1,
                foreground_frame_id = ?, acquired_at = 11, updated_at = 11
            WHERE work_context_id = ?
            """,
            (WORKSPACE, CONTEXT),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                """
                UPDATE work_contexts
                SET claim_state = 'held', claim_generation = 1,
                    foreground_frame_id = ?, acquired_at = 11, updated_at = 11
                WHERE work_context_id = ?
                """,
                (OTHER_WORKSPACE, OTHER_CONTEXT),
            )
        connection.executemany(
            """
            INSERT INTO frames(
                frame_id, host_id, project_id, role, parent_frame_id,
                work_context_id, title, lifecycle_state, created_by,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'task', ?, ?, ?, 'open', 'user', 12, 12)
            """,
            (
                (TASK_ONE, HOST, PROJECT, WORKSPACE, CONTEXT, "one"),
                (TASK_TWO, HOST, PROJECT, TASK_ONE, CONTEXT, "two"),
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="cycle"):
            connection.execute(
                "UPDATE frames SET parent_frame_id = ? WHERE frame_id = ?",
                (TASK_TWO, TASK_ONE),
            )
