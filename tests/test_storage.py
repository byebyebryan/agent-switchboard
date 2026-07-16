from __future__ import annotations

import hashlib
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agent_switchboard.domain import (
    Handoff,
    HandoffSource,
    HostId,
    ProviderId,
    SessionKey,
    Surface,
    SurfaceId,
    SurfaceRole,
    Transport,
)
from agent_switchboard.protocol import ProtocolError
from agent_switchboard.storage import (
    DEFAULT_BUSY_TIMEOUT_MS,
    IdentityConflict,
    Registry,
    RequestConflict,
    StorageError,
    launch_request_fingerprint,
)


def stable_uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"agent-switchboard-test:{label}"))


HOST_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = stable_uuid("project-switchboard")
LOCATION_ID = stable_uuid("location-main")
SESSION_ID = "22222222-2222-4222-8222-222222222222"
SECOND_SESSION_ID = "33333333-3333-4333-8333-333333333333"
SESSION_KEY = f"{HOST_ID}:codex:{SESSION_ID}"
SECOND_SESSION_KEY = f"{HOST_ID}:codex:{SECOND_SESSION_ID}"
REMOTE_HOST_ID = "44444444-4444-4444-8444-444444444444"
REMOTE_SESSION_ID = "55555555-5555-4555-8555-555555555555"
REMOTE_SESSION_KEY = f"{REMOTE_HOST_ID}:claude:{REMOTE_SESSION_ID}"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def project_catalog(path: str = "/work/switchboard") -> list[dict[str, object]]:
    return [
        {
            "project_id": PROJECT_ID,
            "name": "switchboard",
            "aliases": ["asb", "switchboard"],
            "default_provider": "codex",
            "default_transport": "tmux",
            "context_sources": ["AGENTS.md", "README.md"],
            "locations": [
                {
                    "location_id": LOCATION_ID,
                    "path": path,
                    "display_name": "main checkout",
                    "repository_identity": "example/agent-switchboard",
                    "is_default": True,
                }
            ],
        }
    ]


@pytest.fixture
def registry(tmp_path) -> Registry:
    value = Registry(tmp_path / "switchboard.db")
    value.upsert_host(HOST_ID, "starship", is_local=True, observed_at=10)
    value.materialize_projects(HOST_ID, project_catalog(), observed_at=20)
    yield value
    value.close()


def add_session(
    registry: Registry,
    *,
    session_key: str = SESSION_KEY,
    provider_session_id: str = SESSION_ID,
    provider: str = "codex",
) -> dict[str, object]:
    return registry.upsert_session(
        {
            "session_key": session_key,
            "host_id": HOST_ID,
            "provider": provider,
            "provider_session_id": provider_session_id,
            "project_id": PROJECT_ID,
            "location_id": LOCATION_ID,
            "cwd": "/work/switchboard",
            "runtime_presence": "live",
            "resumability": "resumable",
            "activity": "ready",
            "activity_reason": "turn_complete",
            "attachment": "detached",
            "metadata_source": "launch",
            "state_confidence": "confirmed",
            "first_observed_at": 30,
            "last_observed_at": 30,
        }
    )


def resume_request(session_key: str = SESSION_KEY) -> dict[str, object]:
    return {
        "host_id": HOST_ID,
        "provider": "codex",
        "action": "resume",
        "project_id": PROJECT_ID,
        "location_id": LOCATION_ID,
        "cwd": "/work/switchboard",
        "source_handoff_id": None,
        "target_session_key": session_key,
        "transport": "tmux",
    }


def new_request() -> dict[str, object]:
    return {
        "host_id": HOST_ID,
        "provider": "codex",
        "action": "new",
        "project_id": PROJECT_ID,
        "location_id": LOCATION_ID,
        "cwd": "/work/switchboard",
        "source_handoff_id": None,
        "target_session_key": None,
        "transport": "tmux",
    }


def remote_snapshot(
    generated_at: int,
    *,
    host_id: str = REMOTE_HOST_ID,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "protocolVersion": 1,
        "generatedAt": generated_at,
        "host": {"hostId": host_id, "displayName": "remote host"},
        "projects": [],
        "locations": [],
        "sessions": [],
        "runtimes": [],
        "surfaces": [],
        "capabilities": [],
        "errors": [],
    }


def add_launch_surface(
    registry: Registry,
    launch: dict[str, object],
    *,
    role: str = "session",
    observed_at: int = 105,
) -> str:
    launch_id = str(launch["launch_id"])
    surface_id = stable_uuid(f"surface:{launch_id}")
    registry.upsert_surface(
        {
            "surface_id": surface_id,
            "host_id": launch["host_id"],
            "provider": launch["provider"],
            "transport": launch["transport"],
            "transport_locator": f"tmux:{launch_id}",
            "role": role,
            "launch_id": launch_id,
            "created_at": observed_at,
            "last_observed_at": observed_at,
        }
    )
    return surface_id


def prepare_provider_started_launch(
    registry: Registry,
    request: dict[str, object],
    *,
    launch_id: str,
    request_id: str,
    lease_owner: str = "frontend",
) -> dict[str, object]:
    launch_id = stable_uuid(launch_id)
    request_id = stable_uuid(request_id)
    registry.reserve_launch(
        request,
        launch_id=launch_id,
        request_id=request_id,
        lease_owner=lease_owner,
        capability_hash=digest(f"capability:{launch_id}"),
        expires_at=1_000,
        created_at=100,
    )
    launch = registry.get_launch(launch_id)
    assert launch is not None
    surface_id = add_launch_surface(registry, launch)
    registry.transition_launch(
        launch_id,
        "surface_ready",
        lease_owner=lease_owner,
        surface_id=surface_id,
        observed_at=110,
    )
    registry.transition_launch(
        launch_id,
        "waiting_for_client",
        lease_owner=lease_owner,
        observed_at=120,
    )
    return registry.transition_launch(
        launch_id,
        "provider_started",
        lease_owner=lease_owner,
        observed_at=130,
    )


def test_connection_enables_wal_foreign_keys_timeout_and_private_mode(tmp_path) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir(mode=0o755)
    state_directory.chmod(0o755)
    database = state_directory / "switchboard.db"
    with Registry(database) as registry:
        assert registry.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert registry.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert (
            registry.connection.execute("PRAGMA busy_timeout").fetchone()[0]
            == DEFAULT_BUSY_TIMEOUT_MS
        )
        assert registry.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert stat.S_IMODE(database.stat().st_mode) == 0o600
        assert (
            stat.S_IMODE((state_directory / "switchboard.db-wal").stat().st_mode)
            == 0o600
        )
        assert (
            stat.S_IMODE((state_directory / "switchboard.db-shm").stat().st_mode)
            == 0o600
        )

    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_directory.stat().st_mode) == 0o755


def test_registry_rejects_sqlite_uri_database_paths() -> None:
    with pytest.raises(StorageError, match="URI database paths"):
        Registry("file::memory:?cache=shared")


def test_project_materialization_marks_removed_rows_undeclared_and_keeps_history(
    registry: Registry,
) -> None:
    second_project = {
        "project_id": stable_uuid("project-old"),
        "name": "old project",
        "locations": [
            {
                "location_id": stable_uuid("location-old"),
                "path": "/work/old",
                "is_default": True,
            }
        ],
    }
    registry.materialize_projects(
        HOST_ID, [*project_catalog(), second_project], observed_at=40
    )
    registry.upsert_session(
        {
            "session_key": f"{HOST_ID}:codex:66666666-6666-4666-8666-666666666666",
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": "66666666-6666-4666-8666-666666666666",
            "project_id": stable_uuid("project-old"),
            "location_id": stable_uuid("location-old"),
            "first_observed_at": 41,
            "last_observed_at": 41,
        }
    )

    materialized = registry.materialize_projects(
        HOST_ID,
        project_catalog("/work/moved-switchboard"),
        observed_at=50,
    )
    by_id = {project["project_id"]: project for project in materialized}
    assert by_id[PROJECT_ID]["declared"] == 1
    assert by_id[PROJECT_ID]["locations"][0]["location_id"] == LOCATION_ID
    assert by_id[PROJECT_ID]["locations"][0]["path"] == "/work/moved-switchboard"
    assert by_id[stable_uuid("project-old")]["declared"] == 0
    assert by_id[stable_uuid("project-old")]["locations"][0]["declared"] == 0
    assert (
        registry.get_session(f"{HOST_ID}:codex:66666666-6666-4666-8666-666666666666")
        is not None
    )

    registry.materialize_projects(HOST_ID, [], observed_at=60)
    assert registry.list_projects() == []
    assert len(registry.list_projects(include_undeclared=True)) == 2


def test_materialization_rejects_identity_conflicts_without_partial_changes(
    registry: Registry,
) -> None:
    registry.upsert_host(REMOTE_HOST_ID, "remote", observed_at=35)
    with pytest.raises(StorageError, match="local host"):
        registry.materialize_projects(
            REMOTE_HOST_ID, project_catalog("/work/remote"), observed_at=36
        )
    assert registry.list_projects()[0]["project_id"] == PROJECT_ID

    conflicting = [
        {
            "project_id": stable_uuid("another-project"),
            "name": "another",
            "locations": [
                {
                    "location_id": LOCATION_ID,
                    "path": "/work/another",
                    "is_default": True,
                }
            ],
        }
    ]
    with pytest.raises(IdentityConflict, match="already belongs"):
        registry.materialize_projects(HOST_ID, conflicting, observed_at=40)

    projects = registry.list_projects()
    assert [project["project_id"] for project in projects] == [PROJECT_ID]
    assert projects[0]["locations"][0]["path"] == "/work/switchboard"

    with pytest.raises(StorageError, match="must be absolute"):
        registry.materialize_projects(
            HOST_ID, project_catalog("relative/path"), observed_at=50
        )


def test_storage_rejects_unicode_controls_before_retaining_metadata(
    registry: Registry,
) -> None:
    controlled = "\u009b"
    new_host_id = stable_uuid("controlled-host")
    with pytest.raises(StorageError, match="display_name contains terminal control"):
        registry.upsert_host(
            new_host_id,
            f"host{controlled}name",
            observed_at=30,
        )
    assert registry.get_host(new_host_id) is None

    with pytest.raises(StorageError, match="location path contains terminal control"):
        registry.materialize_projects(
            HOST_ID,
            project_catalog(f"/work/{controlled}switchboard"),
            observed_at=30,
        )
    assert registry.list_projects()[0]["locations"][0]["path"] == "/work/switchboard"

    launch_id = stable_uuid("controlled-launch")
    with pytest.raises(StorageError, match="cwd contains terminal control"):
        registry.reserve_launch(
            {**new_request(), "cwd": f"/work/{controlled}switchboard"},
            launch_id=launch_id,
            request_id=stable_uuid("controlled-launch-request"),
            lease_owner="frontend",
            capability_hash=digest("controlled-launch-capability"),
            expires_at=1_000,
            created_at=100,
        )
    assert registry.get_launch(launch_id) is None

    add_session(registry)
    with pytest.raises(StorageError, match="name contains terminal control"):
        registry.upsert_session(
            {
                "session_key": SESSION_KEY,
                "name": f"unsafe{controlled}session",
                "last_observed_at": 40,
            }
        )
    session = registry.get_session(SESSION_KEY)
    assert session is not None
    assert session["name"] is None
    assert session["last_observed_at"] == 30

    registry.upsert_remote("remote", "remote.lan", "remote", observed_at=50)
    with pytest.raises(StorageError, match="error_detail contains terminal control"):
        registry.mark_remote_failure(
            "remote",
            error_code="ssh_unreachable",
            error_detail=f"connection{controlled}failed",
            attempted_at=60,
        )
    remote = registry.connection.execute(
        "SELECT * FROM remote_snapshots WHERE remote_name = 'remote'"
    ).fetchone()
    assert remote is not None
    assert remote["reachability"] == "unknown"
    assert remote["last_attempt_at"] is None

    failed = registry.mark_remote_failure(
        "remote",
        error_code="ssh_unreachable",
        error_detail="line one\nline two\tcontext",
        attempted_at=60,
    )
    assert failed["error_detail"] == "line one\nline two\tcontext"


def test_session_upsert_preserves_unsupplied_curation_and_checks_location(
    registry: Registry,
) -> None:
    first = add_session(registry)
    assert first["runtime_presence"] == "live"
    registry.upsert_session(
        {
            "session_key": SESSION_KEY,
            "purpose": "implement storage",
            "pinned": 1,
            "last_observed_at": 40,
        }
    )
    updated = registry.upsert_session(
        {
            "session_key": SESSION_KEY,
            "runtime_presence": "stopped",
            "last_observed_at": 50,
        }
    )
    assert updated["purpose"] == "implement storage"
    assert updated["pinned"] == 1
    assert updated["runtime_presence"] == "stopped"
    assert updated["first_observed_at"] == 30

    with pytest.raises(StorageError, match="stale session observation"):
        registry.upsert_session(
            {
                "session_key": SESSION_KEY,
                "activity": "working",
                "last_observed_at": 49,
            }
        )

    registry.upsert_session(
        {
            "session_key": SESSION_KEY,
            "runtime_presence": "live",
            "runtime_observed_at": 55,
            "activity": "ready",
            "state_observed_at": 55,
            "last_observed_at": 55,
        }
    )
    with pytest.raises(StorageError, match="stale session state observation"):
        registry.upsert_session(
            {
                "session_key": SESSION_KEY,
                "activity": "working",
                "state_observed_at": 54,
                "last_observed_at": 60,
            }
        )
    with pytest.raises(StorageError, match="stale session runtime observation"):
        registry.upsert_session(
            {
                "session_key": SESSION_KEY,
                "runtime_presence": "stopped",
                "runtime_observed_at": 54,
                "last_observed_at": 60,
            }
        )
    with pytest.raises(StorageError, match="requires state_observed_at"):
        registry.upsert_session(
            {
                "session_key": SESSION_KEY,
                "activity": "working",
                "last_observed_at": 60,
            }
        )
    with pytest.raises(StorageError, match="conflicting session state observation"):
        registry.upsert_session(
            {
                "session_key": SESSION_KEY,
                "activity": "working",
                "state_observed_at": 55,
                "last_observed_at": 60,
            }
        )
    unchanged = registry.get_session(SESSION_KEY)
    assert unchanged is not None
    assert unchanged["activity"] == "ready"
    assert unchanged["runtime_presence"] == "live"
    assert unchanged["last_observed_at"] == 55

    with pytest.raises(IdentityConflict, match="changed provider"):
        registry.upsert_session(
            {
                "session_key": SESSION_KEY,
                "provider": "claude",
                "last_observed_at": 60,
            }
        )

    with pytest.raises(StorageError, match="canonical domain session key"):
        registry.upsert_session(
            {
                "session_key": "not-a-domain-session-key",
                "host_id": HOST_ID,
                "provider": "codex",
                "provider_session_id": SESSION_ID,
                "last_observed_at": 60,
            }
        )

    mismatched_id = "99999999-9999-4999-8999-999999999999"
    mismatched_key = f"{HOST_ID}:codex:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    with pytest.raises(sqlite3.IntegrityError):
        registry.connection.execute(
            """
            INSERT INTO sessions(
                session_key, provider, provider_session_id, host_id,
                first_observed_at, last_observed_at
            ) VALUES (?, 'codex', ?, ?, 60, 60)
            """,
            (mismatched_key, mismatched_id, HOST_ID),
        )

    other_host_id = "77777777-7777-4777-8777-777777777777"
    other_session_id = "88888888-8888-4888-8888-888888888888"
    registry.upsert_host(other_host_id, "other", observed_at=60)
    with pytest.raises(sqlite3.IntegrityError, match="location does not match"):
        registry.connection.execute(
            """
            INSERT INTO sessions(
                session_key, project_id, location_id, provider,
                provider_session_id, host_id, first_observed_at, last_observed_at
            ) VALUES (?, ?, ?, 'claude', ?, ?, 60, 60)
            """,
            (
                f"{other_host_id}:claude:{other_session_id}",
                PROJECT_ID,
                LOCATION_ID,
                other_session_id,
                other_host_id,
            ),
        )


def test_session_name_provenance_defaults_curated_and_syncs_explicit_provider(
    registry: Registry,
) -> None:
    initial = add_session(registry)
    assert initial["name_source"] == "unknown"

    curated = registry.upsert_session(
        {
            "session_key": SESSION_KEY,
            "name": "user title",
            "last_observed_at": 40,
        }
    )
    assert curated["name"] == "user title"
    assert curated["provider_name"] is None
    assert curated["name_source"] == "curated"

    with pytest.raises(
        StorageError, match="provider-owned name and provider_name must agree"
    ):
        registry.upsert_session(
            {
                "session_key": SESSION_KEY,
                "name_source": "provider",
                "last_observed_at": 45,
            }
        )
    unchanged_curated = registry.get_session(SESSION_KEY)
    assert unchanged_curated is not None
    assert unchanged_curated["name"] == "user title"
    assert unchanged_curated["provider_name"] is None
    assert unchanged_curated["name_source"] == "curated"
    assert unchanged_curated["last_observed_at"] == 40

    provider_owned = registry.upsert_session(
        {
            "session_key": SESSION_KEY,
            "name": "provider title",
            "name_source": "provider",
            "last_observed_at": 50,
        }
    )
    assert provider_owned["name"] == "provider title"
    assert provider_owned["provider_name"] == "provider title"
    assert provider_owned["name_source"] == "provider"

    with pytest.raises(
        StorageError, match="provider-owned name and provider_name must agree"
    ):
        registry.upsert_session(
            {
                "session_key": SESSION_KEY,
                "provider_name": "divergent provider title",
                "last_observed_at": 60,
            }
        )
    unchanged_provider = registry.get_session(SESSION_KEY)
    assert unchanged_provider is not None
    assert unchanged_provider["name"] == "provider title"
    assert unchanged_provider["provider_name"] == "provider title"
    assert unchanged_provider["name_source"] == "provider"
    assert unchanged_provider["last_observed_at"] == 50


@pytest.mark.parametrize(
    ("name_fields", "message"),
    (
        (
            {"name": "display", "name_source": "unknown"},
            "unknown name_source requires name to be null",
        ),
        (
            {"provider_name": "shadow", "name_source": "provider"},
            "provider-owned name and provider_name must agree",
        ),
        (
            {
                "name": "display",
                "provider_name": "different",
                "name_source": "provider",
            },
            "provider-owned name and provider_name must agree",
        ),
    ),
)
def test_session_name_provenance_rejects_invalid_insert_combinations(
    registry: Registry,
    name_fields: dict[str, object],
    message: str,
) -> None:
    session = {
        "session_key": SECOND_SESSION_KEY,
        "host_id": HOST_ID,
        "provider": "codex",
        "provider_session_id": SECOND_SESSION_ID,
        "first_observed_at": 30,
        "last_observed_at": 30,
        **name_fields,
    }

    with pytest.raises(StorageError, match=message):
        registry.upsert_session(session)

    assert registry.get_session(SECOND_SESSION_KEY) is None


def test_handoffs_are_hashed_sequenced_idempotent_and_append_only(
    registry: Registry,
) -> None:
    add_session(registry)
    first = registry.append_handoff(
        handoff_id=stable_uuid("handoff-1"),
        session_key=SESSION_KEY,
        summary="Storage schema is ready.",
        next_action="Run migration tests.",
        source="agent",
        source_host_id=HOST_ID,
        created_at=100,
    )
    second = registry.append_handoff(
        handoff_id=stable_uuid("handoff-2"),
        session_key=SESSION_KEY,
        summary="Migration tests pass.",
        next_action="Start the implementation.",
        source="user",
        source_host_id=HOST_ID,
        created_at=110,
    )
    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert len(first["content_hash"]) == 64
    assert registry.get_session(SESSION_KEY)["latest_handoff_id"] == stable_uuid(
        "handoff-2"
    )

    replay = registry.append_handoff(
        handoff_id=stable_uuid("handoff-1"),
        session_key=SESSION_KEY,
        sequence=1,
        summary="Storage schema is ready.",
        next_action="Run migration tests.",
        source="agent",
        source_host_id=HOST_ID,
        created_at=100,
        content_hash=first["content_hash"],
    )
    assert replay == first

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        registry.connection.execute(
            "UPDATE handoffs SET summary = 'rewritten' WHERE handoff_id = ?",
            (stable_uuid("handoff-1"),),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        registry.connection.execute(
            "DELETE FROM handoffs WHERE handoff_id = ?",
            (stable_uuid("handoff-1"),),
        )
    with pytest.raises(IdentityConflict, match="hash does not match"):
        registry.append_handoff(
            handoff_id=stable_uuid("bad-hash"),
            session_key=REMOTE_SESSION_KEY,
            summary="Imported bounded handoff.",
            next_action="Continue remotely.",
            source="imported",
            source_host_id=HOST_ID,
            created_at=120,
            content_hash="0" * 64,
        )

    imported = registry.append_handoff(
        handoff_id=stable_uuid("imported-1"),
        session_key=REMOTE_SESSION_KEY,
        summary="Imported bounded handoff.",
        next_action="Continue remotely.",
        source="imported",
        source_host_id=HOST_ID,
        created_at=120,
    )
    assert imported["session_key"] == REMOTE_SESSION_KEY
    with pytest.raises(sqlite3.IntegrityError, match="requires a registry session"):
        registry.append_handoff(
            handoff_id=stable_uuid("orphan-local"),
            session_key=(
                "99999999-9999-4999-8999-999999999999:codex:"
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            ),
            summary="This is not a remote import.",
            next_action="This must fail.",
            source="agent",
            source_host_id=HOST_ID,
            created_at=130,
        )


def test_domain_handoff_hash_is_storage_canonical(registry: Registry) -> None:
    host_id = HostId("11111111-1111-4111-8111-111111111111")
    key = SessionKey(
        host_id,
        ProviderId.CODEX,
        UUID("22222222-2222-4222-8222-222222222222"),
    )
    registry.upsert_host(str(host_id), "domain host", observed_at=200)
    registry.upsert_session(
        {
            "session_key": str(key),
            "host_id": str(host_id),
            "provider": key.provider.value,
            "provider_session_id": str(key.provider_session_id),
            "first_observed_at": 200,
            "last_observed_at": 200,
        }
    )
    handoff = Handoff.create(
        session_key=key,
        sequence=1,
        summary="  Canonical summary.  ",
        next_action="  Continue implementation.  ",
        source=HandoffSource.AGENT,
        source_host_id=host_id,
        created_at=datetime.fromtimestamp(0.25, UTC),
    )

    stored = registry.append_handoff(
        handoff_id=str(handoff.handoff_id),
        session_key=str(handoff.session_key),
        sequence=handoff.sequence,
        summary=handoff.summary,
        next_action=handoff.next_action,
        source=handoff.source.value,
        source_host_id=str(handoff.source_host_id),
        created_at=250,
        content_hash=handoff.content_hash,
    )

    assert stored["summary"] == "Canonical summary."
    assert stored["next_action"] == "Continue implementation."
    assert stored["content_hash"] == handoff.content_hash


def test_launch_reservation_is_idempotent_unique_and_lease_bounded(
    registry: Registry,
) -> None:
    add_session(registry)
    add_session(
        registry,
        session_key=SECOND_SESSION_KEY,
        provider_session_id=SECOND_SESSION_ID,
    )
    capability_hash = digest("capability")
    created = registry.reserve_launch(
        resume_request(),
        launch_id=stable_uuid("launch-1"),
        request_id=stable_uuid("request-1"),
        lease_owner="frontend-1",
        capability_hash=capability_hash,
        expires_at=200,
        created_at=100,
    )
    retry = registry.reserve_launch(
        resume_request(),
        request_id=stable_uuid("request-1"),
        lease_owner="frontend-2",
        capability_hash=digest("different retry capability"),
        expires_at=220,
        created_at=110,
    )
    competing = registry.reserve_launch(
        resume_request(),
        request_id=stable_uuid("request-2"),
        lease_owner="frontend-2",
        capability_hash=capability_hash,
        expires_at=220,
        created_at=110,
    )
    assert created.kind == "created"
    assert retry.kind == "idempotent"
    assert competing.kind == "existing"
    assert {
        created.launch["launch_id"],
        retry.launch["launch_id"],
        competing.launch["launch_id"],
    } == {stable_uuid("launch-1")}

    with pytest.raises(RequestConflict):
        registry.reserve_launch(
            resume_request(SECOND_SESSION_KEY),
            request_id=stable_uuid("request-1"),
            lease_owner="frontend-1",
            capability_hash=capability_hash,
            expires_at=230,
            created_at=120,
        )

    replacement = registry.reserve_launch(
        resume_request(),
        request_id=stable_uuid("request-3"),
        lease_owner="frontend-3",
        capability_hash=capability_hash,
        expires_at=400,
        created_at=250,
    )
    assert replacement.kind == "created"
    assert replacement.launch["launch_id"] != stable_uuid("launch-1")
    assert registry.get_launch(stable_uuid("launch-1"))["state"] == "expired"

    assert launch_request_fingerprint(resume_request()) == launch_request_fingerprint(
        dict(reversed(list(resume_request().items())))
    )
    with pytest.raises(StorageError, match="unknown normalized"):
        launch_request_fingerprint({**resume_request(), "can_focus_desktop": True})


def test_launch_state_machine_and_manager_uniqueness(registry: Registry) -> None:
    add_session(registry)
    capability_hash = digest("capability")
    launch = registry.reserve_launch(
        resume_request(),
        request_id=stable_uuid("request-state"),
        lease_owner="frontend",
        capability_hash=capability_hash,
        expires_at=1_000,
        created_at=100,
    ).launch
    with pytest.raises(StorageError, match="requires a surface"):
        registry.transition_launch(
            launch["launch_id"],
            "surface_ready",
            lease_owner="frontend",
            observed_at=105,
        )
    surface_id = add_launch_surface(registry, launch)
    registry.transition_launch(
        launch["launch_id"],
        "surface_ready",
        lease_owner="frontend",
        surface_id=surface_id,
        observed_at=110,
    )
    with pytest.raises(StorageError, match="atomic provider-session"):
        registry.transition_launch(
            launch["launch_id"],
            "bound",
            lease_owner="frontend",
            observed_at=120,
        )
    registry.transition_launch(
        launch["launch_id"],
        "failed",
        lease_owner="frontend",
        observed_at=120,
        failure_code="surface_create_failed",
    )
    with pytest.raises(sqlite3.IntegrityError, match="invalid launch state"):
        registry.transition_launch(launch["launch_id"], "expired", observed_at=1_000)

    manager_request = {
        "host_id": HOST_ID,
        "provider": "claude",
        "action": "manage",
        "project_id": None,
        "location_id": None,
        "cwd": None,
        "source_handoff_id": None,
        "target_session_key": None,
        "transport": "tmux",
    }
    with pytest.raises(StorageError, match="cannot target project/session context"):
        registry.reserve_launch(
            {**manager_request, "cwd": "/work/switchboard"},
            request_id=stable_uuid("manager-with-cwd"),
            lease_owner="frontend",
            capability_hash=capability_hash,
            expires_at=1_000,
            created_at=100,
        )
    manager = registry.reserve_launch(
        manager_request,
        request_id=stable_uuid("manager-1"),
        lease_owner="frontend",
        capability_hash=capability_hash,
        expires_at=1_000,
        created_at=100,
    )
    duplicate = registry.reserve_launch(
        manager_request,
        request_id=stable_uuid("manager-2"),
        lease_owner="frontend",
        capability_hash=capability_hash,
        expires_at=1_000,
        created_at=100,
    )
    assert manager.kind == "created"
    assert duplicate.kind == "existing"
    assert duplicate.launch["launch_id"] == manager.launch["launch_id"]

    manager_id = manager.launch["launch_id"]
    manager_surface_id = add_launch_surface(
        registry, manager.launch, role="provider_manager"
    )
    registry.transition_launch(
        manager_id,
        "surface_ready",
        lease_owner="frontend",
        surface_id=manager_surface_id,
        observed_at=110,
    )
    registry.transition_launch(
        manager_id, "waiting_for_client", lease_owner="frontend", observed_at=120
    )
    registry.transition_launch(
        manager_id, "provider_started", lease_owner="frontend", observed_at=130
    )
    ready = registry.transition_launch(
        manager_id, "manager_ready", lease_owner="frontend", observed_at=140
    )
    assert ready["state"] == "manager_ready"
    assert registry.expire_launches(observed_at=1_000) == 1
    assert registry.get_launch(manager_id)["state"] == "expired"
    registry.retire_surface(manager_surface_id, observed_at=1_000)

    replacement = registry.reserve_launch(
        manager_request,
        request_id=stable_uuid("manager-3"),
        lease_owner="frontend",
        capability_hash=capability_hash,
        expires_at=2_000,
        created_at=1_100,
    )
    replacement_id = replacement.launch["launch_id"]
    replacement_surface_id = add_launch_surface(
        registry,
        replacement.launch,
        role="provider_manager",
        observed_at=1_105,
    )
    registry.transition_launch(
        replacement_id,
        "surface_ready",
        lease_owner="frontend",
        surface_id=replacement_surface_id,
        observed_at=1_110,
    )
    registry.transition_launch(
        replacement_id,
        "waiting_for_client",
        lease_owner="frontend",
        observed_at=1_120,
    )
    registry.transition_launch(
        replacement_id,
        "provider_started",
        lease_owner="frontend",
        observed_at=1_130,
    )
    registry.transition_launch(
        replacement_id,
        "manager_ready",
        lease_owner="frontend",
        observed_at=1_140,
    )
    failed = registry.transition_launch(
        replacement_id,
        "failed",
        lease_owner="frontend",
        observed_at=1_150,
        failure_code="manager_process_exited",
    )
    assert failed["state"] == "failed"
    assert failed["lease_owner"] is None


def test_launch_transitions_enforce_and_renew_the_active_lease(
    registry: Registry,
) -> None:
    add_session(registry)
    launch_id = registry.reserve_launch(
        resume_request(),
        request_id=stable_uuid("request-lease"),
        lease_owner="owner-a",
        capability_hash=digest("lease capability"),
        expires_at=200,
        created_at=100,
    ).launch["launch_id"]

    with pytest.raises(StorageError, match="different worker"):
        registry.transition_launch(
            launch_id,
            "surface_ready",
            lease_owner="owner-b",
            observed_at=110,
        )
    with pytest.raises(StorageError, match="live launch lease"):
        registry.transition_launch(launch_id, "expired", observed_at=150)

    renewed = registry.renew_launch_lease(
        launch_id,
        lease_owner="owner-a",
        expires_at=300,
        observed_at=120,
    )
    assert renewed["expires_at"] == 300
    launch = registry.get_launch(launch_id)
    assert launch is not None
    surface_id = add_launch_surface(registry, launch, observed_at=125)
    registry.transition_launch(
        launch_id,
        "surface_ready",
        lease_owner="owner-a",
        surface_id=surface_id,
        observed_at=130,
    )
    with pytest.raises(StorageError, match="expired"):
        registry.transition_launch(
            launch_id,
            "failed",
            lease_owner="owner-a",
            observed_at=300,
            failure_code="too_late",
        )
    failed = registry.transition_launch(
        launch_id,
        "failed",
        lease_owner="owner-a",
        observed_at=140,
        failure_code="surface_failed",
    )
    assert failed["lease_owner"] is None


def test_new_launch_atomically_binds_provider_session_and_surface(
    registry: Registry,
) -> None:
    launch = prepare_provider_started_launch(
        registry,
        new_request(),
        launch_id="launch-new",
        request_id="request-new",
    )
    assert launch["target_session_key"] is None

    result = registry.bind_provider_session(
        str(launch["launch_id"]),
        {
            "session_key": (f"{HOST_ID}:codex:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "name": "Provider launch title",
            "runtime_presence": "live",
            "last_observed_at": 140,
        },
        lease_owner="frontend",
        observed_at=140,
    )

    assert result.kind == "bound"
    assert result.launch["state"] == "bound"
    assert result.launch["target_session_key"] == (
        f"{HOST_ID}:codex:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    )
    assert result.launch["lease_owner"] is None
    assert result.session["project_id"] == PROJECT_ID
    assert result.session["location_id"] == LOCATION_ID
    assert result.session["cwd"] == "/work/switchboard"
    assert result.session["metadata_source"] == "launch"
    assert result.session["name"] == "Provider launch title"
    assert result.session["provider_name"] == "Provider launch title"
    assert result.session["name_source"] == "provider"
    assert result.surface is not None
    assert result.surface["binding_confidence"] == "confirmed"
    assert result.surface["current_session_key"] == result.session["session_key"]
    assert result.session["surface_id"] == result.surface["surface_id"]


def test_resume_identity_mismatch_fails_launch_without_losing_observation(
    registry: Registry,
) -> None:
    add_session(registry)
    launch = prepare_provider_started_launch(
        registry,
        resume_request(),
        launch_id="launch-resume",
        request_id="request-resume",
    )

    result = registry.bind_provider_session(
        str(launch["launch_id"]),
        {
            "session_key": SECOND_SESSION_KEY,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": SECOND_SESSION_ID,
            "last_observed_at": 140,
        },
        lease_owner="frontend",
        observed_at=140,
    )

    assert result.kind == "provider_identity_mismatch"
    assert result.launch["state"] == "failed"
    assert result.launch["failure_code"] == "provider_identity_mismatch"
    assert result.launch["target_session_key"] == SESSION_KEY
    assert result.launch["lease_owner"] is None
    assert registry.get_session(SESSION_KEY) is not None
    assert registry.get_session(SECOND_SESSION_KEY) is not None
    assert result.surface is not None
    assert result.surface["current_session_key"] is None
    assert result.surface["binding_confidence"] == "unknown"


def test_provider_binding_rejects_wrong_or_expired_lease_atomically(
    registry: Registry,
) -> None:
    launch = prepare_provider_started_launch(
        registry,
        new_request(),
        launch_id="launch-lease-bind",
        request_id="request-lease-bind",
    )
    launch_id = str(launch["launch_id"])
    session_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    session = {
        "session_key": f"{HOST_ID}:codex:{session_id}",
        "host_id": HOST_ID,
        "provider": "codex",
        "provider_session_id": session_id,
        "last_observed_at": 140,
    }

    with pytest.raises(StorageError, match="different worker"):
        registry.bind_provider_session(
            launch_id,
            session,
            lease_owner="another-worker",
            observed_at=140,
        )
    assert registry.get_session(str(session["session_key"])) is None
    assert registry.get_launch(launch_id)["state"] == "provider_started"

    with pytest.raises(StorageError, match="expired"):
        registry.bind_provider_session(
            launch_id,
            session,
            lease_owner="frontend",
            observed_at=1_000,
        )
    assert registry.get_session(str(session["session_key"])) is None
    assert registry.get_launch(launch_id)["state"] == "provider_started"


def test_concurrent_launch_reservations_select_one_winner(tmp_path) -> None:
    database = tmp_path / "switchboard.db"
    with Registry(database) as registry:
        registry.upsert_host(HOST_ID, "starship", is_local=True, observed_at=10)
        registry.materialize_projects(HOST_ID, project_catalog(), observed_at=20)
        add_session(registry)

    def reserve(index: int) -> tuple[str, str]:
        with Registry(database) as worker:
            result = worker.reserve_launch(
                resume_request(),
                request_id=stable_uuid(f"request-{index}"),
                lease_owner=f"worker-{index}",
                capability_hash=digest(f"capability-{index}"),
                expires_at=1_000,
                created_at=100,
            )
            return result.kind, result.launch["launch_id"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(reserve, range(16)))
    assert [kind for kind, _ in results].count("created") == 1
    assert len({launch_id for _, launch_id in results}) == 1


def test_surface_binding_and_manager_invariants(registry: Registry) -> None:
    add_session(registry)
    surface_one = stable_uuid("surface-1")
    surface_two = stable_uuid("surface-2")
    manager_one = stable_uuid("manager-1-surface")
    manager_two = stable_uuid("manager-2-surface")
    registry.upsert_surface(
        {
            "surface_id": surface_one,
            "host_id": HOST_ID,
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": "as-codex:session-1",
            "role": "session",
            "created_at": 100,
            "last_observed_at": 100,
        }
    )
    bound = registry.bind_surface(
        surface_one, SESSION_KEY, confidence="confirmed", observed_at=110
    )
    assert bound["current_session_key"] == SESSION_KEY
    assert registry.get_session(SESSION_KEY)["surface_id"] == surface_one

    registry.upsert_surface(
        {
            "surface_id": surface_two,
            "host_id": HOST_ID,
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": "as-codex:session-2",
            "role": "session",
            "created_at": 100,
            "last_observed_at": 100,
        }
    )
    with pytest.raises(sqlite3.IntegrityError):
        registry.connection.execute(
            """
            UPDATE surfaces
            SET current_session_key = ?, binding_confidence = 'confirmed'
            WHERE surface_id = ?
            """,
            (SESSION_KEY, surface_two),
        )

    registry.upsert_surface(
        {
            "surface_id": manager_one,
            "host_id": HOST_ID,
            "provider": "claude",
            "transport": "tmux",
            "transport_locator": "as-claude:manager",
            "role": "provider_manager",
            "created_at": 100,
            "last_observed_at": 100,
        }
    )
    with pytest.raises(StorageError, match="cannot bind"):
        registry.bind_surface(
            manager_one, SESSION_KEY, confidence="confirmed", observed_at=120
        )
    with pytest.raises(sqlite3.IntegrityError):
        registry.upsert_surface(
            {
                "surface_id": manager_two,
                "host_id": HOST_ID,
                "provider": "claude",
                "transport": "tmux",
                "transport_locator": "as-claude:manager-2",
                "role": "provider_manager",
                "created_at": 100,
                "last_observed_at": 100,
            }
        )

    retired = registry.retire_surface(surface_one, observed_at=130)
    assert retired["retired_at"] == 130
    assert retired["current_session_key"] is None
    assert registry.get_session(SESSION_KEY)["surface_id"] is None
    with pytest.raises(StorageError, match="retired"):
        registry.bind_surface(
            surface_one, SESSION_KEY, confidence="confirmed", observed_at=140
        )
    with pytest.raises(StorageError, match="unknown surface"):
        registry.bind_surface(
            stable_uuid("missing-surface"),
            SESSION_KEY,
            confidence="confirmed",
            observed_at=140,
        )


def test_session_upsert_cannot_establish_or_clear_surface_binding(
    registry: Registry,
) -> None:
    add_session(registry)
    surface_id = stable_uuid("session-upsert-binding-boundary")
    registry.upsert_surface(
        {
            "surface_id": surface_id,
            "host_id": HOST_ID,
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": "as-codex:session-upsert-boundary",
            "role": "session",
            "created_at": 100,
            "last_observed_at": 100,
        }
    )

    with pytest.raises(StorageError, match="bindings must be changed"):
        registry.upsert_session(
            {
                "session_key": SESSION_KEY,
                "surface_id": surface_id,
                "last_observed_at": 110,
            }
        )
    assert registry.get_session(SESSION_KEY)["surface_id"] is None
    surface = registry.connection.execute(
        "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
    ).fetchone()
    assert surface is not None and surface["current_session_key"] is None

    registry.bind_surface(
        surface_id, SESSION_KEY, confidence="confirmed", observed_at=120
    )
    with pytest.raises(StorageError, match="bindings must be changed"):
        registry.upsert_session(
            {
                "session_key": SESSION_KEY,
                "surface_id": None,
                "last_observed_at": 130,
            }
        )
    assert registry.get_session(SESSION_KEY)["surface_id"] == surface_id
    surface = registry.connection.execute(
        "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
    ).fetchone()
    assert surface is not None and surface["current_session_key"] == SESSION_KEY


def test_surface_upsert_preserves_binding_and_rejects_direct_or_stale_changes(
    registry: Registry,
) -> None:
    add_session(registry)
    surface_id = stable_uuid("surface-upsert-boundary")
    surface = {
        "surface_id": surface_id,
        "host_id": HOST_ID,
        "provider": "codex",
        "transport": "tmux",
        "transport_locator": "as-codex:boundary",
        "role": "session",
        "created_at": 100,
        "last_observed_at": 100,
    }

    with pytest.raises(StorageError, match="bindings must be changed"):
        registry.upsert_surface(
            {
                **surface,
                "current_session_key": SESSION_KEY,
                "binding_confidence": "confirmed",
            }
        )
    assert (
        registry.connection.execute(
            "SELECT 1 FROM surfaces WHERE surface_id = ?", (surface_id,)
        ).fetchone()
        is None
    )
    assert registry.get_session(SESSION_KEY)["surface_id"] is None

    registry.upsert_surface(
        {**surface, "workspace_id": "workspace-1", "client_attached": True}
    )
    registry.bind_surface(
        surface_id, SESSION_KEY, confidence="confirmed", observed_at=200
    )
    refreshed = registry.upsert_surface(
        {
            **surface,
            "transport_locator": "as-codex:boundary-refreshed",
            "last_observed_at": 210,
        }
    )
    assert refreshed["current_session_key"] == SESSION_KEY
    assert refreshed["binding_confidence"] == "confirmed"
    assert refreshed["workspace_id"] == "workspace-1"
    assert refreshed["client_attached"] == 1
    assert registry.get_session(SESSION_KEY)["surface_id"] == surface_id

    idempotent = registry.upsert_surface(
        {
            **surface,
            "transport_locator": "as-codex:boundary-refreshed",
            "last_observed_at": 210,
        }
    )
    assert idempotent["last_observed_at"] == 210
    with pytest.raises(StorageError, match="conflicting surface observation"):
        registry.upsert_surface(
            {
                **surface,
                "transport_locator": "as-codex:same-time-conflict",
                "last_observed_at": 210,
            }
        )

    with pytest.raises(StorageError, match="stale surface observation"):
        registry.upsert_surface(
            {
                **surface,
                "transport_locator": "as-codex:stale",
                "last_observed_at": 100,
                "retired_at": None,
            }
        )
    with pytest.raises(StorageError, match="bindings must be changed"):
        registry.upsert_surface(
            {
                **surface,
                "transport_locator": "as-codex:attempted-clear",
                "current_session_key": None,
                "binding_confidence": "unknown",
                "last_observed_at": 220,
            }
        )

    stored = registry.connection.execute(
        "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
    ).fetchone()
    assert stored is not None
    assert stored["transport_locator"] == "as-codex:boundary-refreshed"
    assert stored["last_observed_at"] == 210
    assert stored["retired_at"] is None
    assert stored["current_session_key"] == SESSION_KEY
    assert registry.get_session(SESSION_KEY)["surface_id"] == surface_id


def test_surface_retirement_is_monotonic_and_cannot_be_resurrected(
    registry: Registry,
) -> None:
    surface_id = stable_uuid("surface-retirement-boundary")
    surface = {
        "surface_id": surface_id,
        "host_id": HOST_ID,
        "provider": "codex",
        "transport": "tmux",
        "transport_locator": "as-codex:retire",
        "role": "session",
        "created_at": 100,
        "last_observed_at": 200,
    }
    registry.upsert_surface(surface)
    retired = registry.retire_surface(surface_id, observed_at=220)
    assert retired["retired_at"] == 220
    assert retired["last_observed_at"] == 220

    with pytest.raises(StorageError, match="stale surface retirement"):
        registry.retire_surface(surface_id, observed_at=210)
    with pytest.raises(StorageError, match="retired surfaces cannot be refreshed"):
        registry.upsert_surface(
            {
                **surface,
                "transport_locator": "as-codex:resurrected",
                "last_observed_at": 230,
                "retired_at": None,
            }
        )

    observed_again = registry.retire_surface(surface_id, observed_at=240)
    assert observed_again["retired_at"] == 220
    assert observed_again["last_observed_at"] == 240
    assert observed_again["transport_locator"] == "as-codex:retire"


def test_surface_launch_binding_requires_atomic_provider_path(
    registry: Registry,
) -> None:
    add_session(registry)
    launch_id = stable_uuid("pending-surface-launch")
    registry.reserve_launch(
        new_request(),
        launch_id=launch_id,
        request_id=stable_uuid("pending-surface-request"),
        lease_owner="frontend",
        capability_hash=digest("pending-surface-capability"),
        expires_at=1_000,
        created_at=100,
    )
    surface_id = stable_uuid("pending-launch-surface")
    surface = {
        "surface_id": surface_id,
        "host_id": HOST_ID,
        "provider": "codex",
        "transport": "tmux",
        "transport_locator": "as-codex:pending-launch",
        "role": "session",
        "launch_id": launch_id,
        "created_at": 105,
        "last_observed_at": 105,
    }
    registry.upsert_surface(surface)

    with pytest.raises(StorageError, match="pending launch surfaces"):
        registry.bind_surface(
            surface_id, SESSION_KEY, confidence="confirmed", observed_at=110
        )
    stored = registry.connection.execute(
        "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
    ).fetchone()
    assert stored is not None
    assert stored["current_session_key"] is None
    assert stored["binding_confidence"] == "unknown"
    assert registry.get_session(SESSION_KEY)["surface_id"] is None

    with pytest.raises(IdentityConflict, match="changed launch_id"):
        registry.upsert_surface(
            {
                **surface,
                "launch_id": stable_uuid("conflicting-surface-launch"),
                "last_observed_at": 110,
            }
        )
    assert (
        registry.connection.execute(
            "SELECT launch_id FROM surfaces WHERE surface_id = ?", (surface_id,)
        ).fetchone()["launch_id"]
        == launch_id
    )


def test_surface_rebind_rejects_observation_older_than_previous_surface(
    registry: Registry,
) -> None:
    add_session(registry)
    previous_surface_id = stable_uuid("newer-previous-surface")
    next_surface_id = stable_uuid("older-next-surface")
    for surface_id, locator, last_observed_at in (
        (previous_surface_id, "as-codex:previous", 200),
        (next_surface_id, "as-codex:next", 100),
    ):
        registry.upsert_surface(
            {
                "surface_id": surface_id,
                "host_id": HOST_ID,
                "provider": "codex",
                "transport": "tmux",
                "transport_locator": locator,
                "role": "session",
                "created_at": 100,
                "last_observed_at": last_observed_at,
            }
        )
    registry.bind_surface(
        previous_surface_id,
        SESSION_KEY,
        confidence="confirmed",
        observed_at=200,
    )

    replay = registry.bind_surface(
        previous_surface_id,
        SESSION_KEY,
        confidence="confirmed",
        observed_at=200,
    )
    assert replay["current_session_key"] == SESSION_KEY

    add_session(
        registry,
        session_key=SECOND_SESSION_KEY,
        provider_session_id=SECOND_SESSION_ID,
    )
    with pytest.raises(StorageError, match="conflicting target-surface binding"):
        registry.bind_surface(
            previous_surface_id,
            SECOND_SESSION_KEY,
            confidence="confirmed",
            observed_at=200,
        )

    with pytest.raises(StorageError, match="conflicting previous-surface binding"):
        registry.bind_surface(
            next_surface_id,
            SESSION_KEY,
            confidence="confirmed",
            observed_at=200,
        )

    with pytest.raises(StorageError, match="stale previous-surface binding"):
        registry.bind_surface(
            next_surface_id,
            SESSION_KEY,
            confidence="confirmed",
            observed_at=150,
        )
    previous = registry.connection.execute(
        "SELECT * FROM surfaces WHERE surface_id = ?", (previous_surface_id,)
    ).fetchone()
    next_surface = registry.connection.execute(
        "SELECT * FROM surfaces WHERE surface_id = ?", (next_surface_id,)
    ).fetchone()
    assert previous is not None and next_surface is not None
    assert previous["current_session_key"] == SESSION_KEY
    assert previous["last_observed_at"] == 200
    assert next_surface["current_session_key"] is None
    assert registry.get_session(SESSION_KEY)["surface_id"] == previous_surface_id


def test_domain_surface_provider_round_trips_through_storage(
    registry: Registry,
) -> None:
    host_id = HostId("33333333-3333-4333-8333-333333333333")
    session_key = SessionKey(
        host_id,
        ProviderId.CLAUDE,
        UUID("44444444-4444-4444-8444-444444444444"),
    )
    surface = Surface(
        surface_id=SurfaceId("55555555-5555-4555-8555-555555555555"),
        host_id=host_id,
        provider=ProviderId.CLAUDE,
        transport=Transport.TMUX,
        transport_locator="tmux:domain-surface",
        role=SurfaceRole.SESSION,
        created_at=datetime.fromtimestamp(0.2, UTC),
        last_observed_at=datetime.fromtimestamp(0.2, UTC),
    )
    registry.upsert_host(str(host_id), "surface host", observed_at=200)
    registry.upsert_session(
        {
            "session_key": str(session_key),
            "host_id": str(host_id),
            "provider": session_key.provider.value,
            "provider_session_id": str(session_key.provider_session_id),
            "first_observed_at": 200,
            "last_observed_at": 200,
        }
    )
    stored = registry.upsert_surface(
        {
            "surface_id": str(surface.surface_id),
            "host_id": str(surface.host_id),
            "provider": surface.provider.value,
            "transport": surface.transport.value,
            "transport_locator": surface.transport_locator,
            "role": surface.role.value,
            "created_at": 200,
            "last_observed_at": 200,
        }
    )

    assert stored["provider"] == surface.provider.value
    bound = registry.bind_surface(
        str(surface.surface_id),
        str(session_key),
        confidence="confirmed",
        observed_at=210,
    )
    assert bound["current_session_key"] == str(session_key)


def test_runtime_observations_are_idempotent_and_events_are_bounded(
    registry: Registry,
) -> None:
    add_session(registry)
    observation = {
        "observation_id": "observation-1",
        "observation_key": "process:123:100",
        "host_id": HOST_ID,
        "provider": "codex",
        "session_key": SESSION_KEY,
        "source": "process",
        "source_priority": 20,
        "runtime_presence": "live",
        "resumability": "resumable",
        "activity": "working",
        "activity_reason": "unknown",
        "attachment": "detached",
        "pid": 123,
        "observed_at": 100,
        "received_at": 101,
    }
    first = registry.record_runtime_observation(observation)
    replay = registry.record_runtime_observation(observation)
    assert replay == first
    assert len(first["payload_hash"]) == 64
    assert (
        registry.record_runtime_observation(
            {**observation, "payload_hash": first["payload_hash"]}
        )
        == first
    )
    with pytest.raises(StorageError, match="does not match"):
        registry.record_runtime_observation(
            {**observation, "payload_hash": digest("forged")}
        )
    with pytest.raises(IdentityConflict, match="different content"):
        registry.record_runtime_observation({**observation, "activity": "ready"})

    hashless = {
        **observation,
        "observation_id": "observation-hashless",
        "observation_key": "process:456:100",
        "pid": 456,
    }
    stored_hashless = registry.record_runtime_observation(hashless)
    assert len(stored_hashless["payload_hash"]) == 64
    assert registry.record_runtime_observation(hashless) == stored_hashless
    with pytest.raises(IdentityConflict, match="different content"):
        registry.record_runtime_observation({**hashless, "activity": "ready"})

    for index in range(5):
        registry.record_event(
            {
                "event_id": f"event-{index}",
                "idempotency_key": f"hook-{index}",
                "host_id": HOST_ID,
                "provider": "codex",
                "session_key": SESSION_KEY,
                "event_kind": "PostToolUse",
                "provider_turn_id": "turn-1",
                "source_priority": 10,
                "kind_priority": index,
                "observed_at": 200 + index,
                "received_at": 200 + index,
            },
            limit=3,
        )
    rows = registry.connection.execute(
        "SELECT event_id FROM events ORDER BY received_at"
    ).fetchall()
    assert [row[0] for row in rows] == ["event-2", "event-3", "event-4"]

    old_event = registry.record_event(
        {
            "event_id": "event-old",
            "idempotency_key": "hook-old",
            "host_id": HOST_ID,
            "provider": "codex",
            "session_key": SESSION_KEY,
            "event_kind": "SessionStart",
            "source_priority": 10,
            "kind_priority": 0,
            "observed_at": 1,
            "received_at": 1,
        },
        limit=3,
    )
    assert old_event["event_id"] == "event-old"
    retained = registry.connection.execute(
        "SELECT event_id FROM events ORDER BY received_at"
    ).fetchall()
    assert [row[0] for row in retained] == ["event-2", "event-3", "event-4"]

    event = {
        "event_id": "event-verified-hash",
        "idempotency_key": "hook-verified-hash",
        "host_id": HOST_ID,
        "provider": "codex",
        "session_key": SESSION_KEY,
        "event_kind": "Stop",
        "source_priority": 10,
        "kind_priority": 50,
        "observed_at": 300,
        "received_at": 300,
    }
    stored_event = registry.record_event(event)
    assert (
        registry.record_event({**event, "payload_hash": stored_event["payload_hash"]})
        == stored_event
    )
    with pytest.raises(StorageError, match="does not match"):
        registry.record_event({**event, "payload_hash": digest("forged-event")})
    with pytest.raises(IdentityConflict, match="different content"):
        registry.record_event({**event, "event_kind": "SessionEnd"})


def test_observation_and_event_links_require_coherent_identity(
    registry: Registry,
) -> None:
    add_session(registry)
    add_session(
        registry,
        session_key=SECOND_SESSION_KEY,
        provider_session_id=SECOND_SESSION_ID,
    )
    registry.upsert_host(REMOTE_HOST_ID, "remote", observed_at=40)

    def reserve_manage(host_id: str, provider: str, label: str) -> str:
        return str(
            registry.reserve_launch(
                {
                    "host_id": host_id,
                    "provider": provider,
                    "action": "manage",
                    "project_id": None,
                    "location_id": None,
                    "cwd": None,
                    "source_handoff_id": None,
                    "target_session_key": None,
                    "transport": "tmux",
                },
                launch_id=stable_uuid(f"{label}-launch"),
                request_id=stable_uuid(f"{label}-request"),
                lease_owner="frontend",
                capability_hash=digest(f"{label}-capability"),
                expires_at=1_000,
                created_at=100,
            ).launch["launch_id"]
        )

    remote_launch_id = reserve_manage(REMOTE_HOST_ID, "codex", "remote-manage")
    other_provider_launch_id = reserve_manage(HOST_ID, "claude", "claude-manage")
    local_manager_launch_id = reserve_manage(HOST_ID, "codex", "codex-manage")
    targeted_launch_id = str(
        registry.reserve_launch(
            resume_request(),
            launch_id=stable_uuid("targeted-launch"),
            request_id=stable_uuid("targeted-request"),
            lease_owner="frontend",
            capability_hash=digest("targeted-capability"),
            expires_at=1_000,
            created_at=100,
        ).launch["launch_id"]
    )

    remote_surface_id = stable_uuid("remote-observation-surface")
    registry.upsert_surface(
        {
            "surface_id": remote_surface_id,
            "host_id": REMOTE_HOST_ID,
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": "as-codex:remote",
            "role": "session",
            "created_at": 200,
            "last_observed_at": 200,
        }
    )
    bound_surface_id = stable_uuid("bound-observation-surface")
    registry.upsert_surface(
        {
            "surface_id": bound_surface_id,
            "host_id": HOST_ID,
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": "as-codex:bound",
            "role": "session",
            "created_at": 200,
            "last_observed_at": 200,
        }
    )
    registry.bind_surface(
        bound_surface_id,
        SESSION_KEY,
        confidence="confirmed",
        observed_at=210,
    )
    launch_surface_id = stable_uuid("linked-observation-surface")
    registry.upsert_surface(
        {
            "surface_id": launch_surface_id,
            "host_id": HOST_ID,
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": "as-codex:linked",
            "role": "session",
            "launch_id": targeted_launch_id,
            "created_at": 200,
            "last_observed_at": 200,
        }
    )

    observation = {
        "observation_key": "identity:runtime",
        "host_id": HOST_ID,
        "provider": "codex",
        "source": "process",
        "source_priority": 20,
        "runtime_presence": "live",
        "resumability": "resumable",
        "activity": "working",
        "activity_reason": "unknown",
        "attachment": "detached",
        "observed_at": 300,
        "received_at": 300,
    }
    with pytest.raises(IdentityConflict, match="launch does not match host/provider"):
        registry.record_runtime_observation(
            {**observation, "launch_id": remote_launch_id}
        )
    with pytest.raises(IdentityConflict, match="launch does not match host/provider"):
        registry.record_runtime_observation(
            {
                **observation,
                "observation_key": "identity:provider-runtime",
                "launch_id": other_provider_launch_id,
            }
        )
    with pytest.raises(IdentityConflict, match="does not match launch target"):
        registry.record_runtime_observation(
            {
                **observation,
                "observation_key": "identity:target-runtime",
                "session_key": SECOND_SESSION_KEY,
                "launch_id": targeted_launch_id,
            }
        )

    event = {
        "idempotency_key": "identity:event",
        "host_id": HOST_ID,
        "provider": "codex",
        "event_kind": "SessionStart",
        "source_priority": 10,
        "kind_priority": 10,
        "observed_at": 300,
        "received_at": 300,
    }
    with pytest.raises(IdentityConflict, match="launch does not match host/provider"):
        registry.record_event({**event, "launch_id": remote_launch_id})
    with pytest.raises(IdentityConflict, match="surface does not match host/provider"):
        registry.record_event(
            {
                **event,
                "idempotency_key": "identity:remote-surface-event",
                "surface_id": remote_surface_id,
            }
        )
    with pytest.raises(IdentityConflict, match="does not match surface binding"):
        registry.record_event(
            {
                **event,
                "idempotency_key": "identity:bound-surface-event",
                "session_key": SECOND_SESSION_KEY,
                "surface_id": bound_surface_id,
            }
        )
    with pytest.raises(IdentityConflict, match="surface does not match launch"):
        registry.record_event(
            {
                **event,
                "idempotency_key": "identity:linked-surface-event",
                "launch_id": local_manager_launch_id,
                "surface_id": launch_surface_id,
            }
        )

    def insert_runtime(
        label: str,
        *,
        launch_id: str,
        session_key: str | None = None,
    ) -> None:
        registry.connection.execute(
            """
            INSERT INTO runtime_observations(
                observation_id, observation_key, host_id, provider,
                session_key, launch_id, source, source_priority,
                runtime_presence, resumability, activity, activity_reason,
                attachment, observed_at, received_at, payload_hash
            ) VALUES (?, ?, ?, 'codex', ?, ?, 'process', 20,
                      'live', 'resumable', 'working', 'unknown', 'detached',
                      300, 300, ?)
            """,
            (label, label, HOST_ID, session_key, launch_id, digest(label)),
        )

    with pytest.raises(sqlite3.IntegrityError, match="observation launch"):
        insert_runtime("direct-remote-launch", launch_id=remote_launch_id)
    with pytest.raises(sqlite3.IntegrityError, match="observation launch"):
        insert_runtime(
            "direct-target-launch",
            launch_id=targeted_launch_id,
            session_key=SECOND_SESSION_KEY,
        )

    def insert_event(
        label: str,
        *,
        launch_id: str | None = None,
        surface_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        registry.connection.execute(
            """
            INSERT INTO events(
                event_id, idempotency_key, host_id, provider, session_key,
                launch_id, surface_id, event_kind, source_priority,
                kind_priority, observed_at, received_at, payload_hash
            ) VALUES (?, ?, ?, 'codex', ?, ?, ?, 'SessionStart', 10, 10,
                      300, 300, ?)
            """,
            (
                label,
                label,
                HOST_ID,
                session_key,
                launch_id,
                surface_id,
                digest(label),
            ),
        )

    with pytest.raises(sqlite3.IntegrityError, match="event launch"):
        insert_event("direct-provider-launch", launch_id=other_provider_launch_id)
    with pytest.raises(sqlite3.IntegrityError, match="event surface"):
        insert_event("direct-remote-surface", surface_id=remote_surface_id)
    with pytest.raises(sqlite3.IntegrityError, match="event surface"):
        insert_event(
            "direct-bound-surface",
            surface_id=bound_surface_id,
            session_key=SECOND_SESSION_KEY,
        )
    with pytest.raises(sqlite3.IntegrityError, match="event surface"):
        insert_event(
            "direct-linked-surface",
            launch_id=local_manager_launch_id,
            surface_id=launch_surface_id,
        )

    assert (
        registry.connection.execute(
            "SELECT COUNT(*) FROM runtime_observations"
        ).fetchone()[0]
        == 0
    )
    assert registry.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_remote_failure_retains_last_successful_snapshot(registry: Registry) -> None:
    registry.upsert_remote(
        "snap",
        "snap.lan",
        "snap",
        observed_at=100,
    )
    snapshot = remote_snapshot(110)
    stored = registry.store_remote_snapshot(
        "snap",
        snapshot,
        remote_host_id=REMOTE_HOST_ID,
        schema_version=1,
        protocol_version=1,
        observed_at=110,
        received_at=120,
    )
    assert stored["reachability"] == "online"
    assert stored["snapshot"] == snapshot
    original_hash = stored["snapshot_hash"]
    original_json = stored["snapshot_json"]

    failed = registry.mark_remote_failure(
        "snap",
        error_code="ssh_unreachable",
        error_detail="connection timed out",
        attempted_at=200,
    )
    assert failed["reachability"] == "offline"
    assert failed["snapshot"] == snapshot
    assert failed["snapshot_hash"] == original_hash
    assert failed["snapshot_json"] == original_json
    assert failed["snapshot_received_at"] == 120

    undeclared = registry.upsert_remote(
        "snap",
        "new-target.lan",
        "snap renamed",
        declared=False,
        observed_at=220,
    )
    assert undeclared["declared"] == 0
    assert undeclared["snapshot_json"] == original_json
    assert registry.get_host(REMOTE_HOST_ID)["is_local"] == 0


def test_remote_snapshot_is_canonical_and_atomically_replaced(
    registry: Registry,
) -> None:
    registry.upsert_remote("remote", "remote.lan", "remote", observed_at=100)
    first_snapshot = {**remote_snapshot(110), "futureSafeField": {"safe": True}}
    first = registry.store_remote_snapshot(
        "remote",
        first_snapshot,
        remote_host_id=REMOTE_HOST_ID,
        schema_version=1,
        protocol_version=1,
        observed_at=110,
        received_at=120,
    )
    assert "futureSafeField" not in first["snapshot_json"]
    assert first["snapshot"] == remote_snapshot(110)
    second = registry.store_remote_snapshot(
        "remote",
        remote_snapshot(130),
        remote_host_id=REMOTE_HOST_ID,
        schema_version=1,
        protocol_version=1,
        observed_at=130,
        received_at=140,
    )
    assert second["snapshot"] == remote_snapshot(130)
    assert second["snapshot_hash"] != first["snapshot_hash"]
    assert second["error_code"] is None


def test_remote_completions_and_snapshot_observations_are_monotonic(
    registry: Registry,
) -> None:
    registry.upsert_remote("ordered", "ordered.lan", "ordered", observed_at=100)
    first = registry.store_remote_snapshot(
        "ordered",
        remote_snapshot(110),
        remote_host_id=REMOTE_HOST_ID,
        schema_version=1,
        protocol_version=1,
        observed_at=110,
        received_at=120,
    )

    with pytest.raises(StorageError, match="stale remote failure completion"):
        registry.mark_remote_failure(
            "ordered", error_code="old_failure", attempted_at=119
        )
    assert registry.get_remote("ordered")["reachability"] == "online"

    second = registry.store_remote_snapshot(
        "ordered",
        remote_snapshot(130),
        remote_host_id=REMOTE_HOST_ID,
        schema_version=1,
        protocol_version=1,
        observed_at=130,
        received_at=140,
    )
    assert second["snapshot_observed_at"] == 130
    with pytest.raises(StorageError, match="stale remote snapshot observation"):
        registry.store_remote_snapshot(
            "ordered",
            remote_snapshot(125),
            remote_host_id=REMOTE_HOST_ID,
            schema_version=1,
            protocol_version=1,
            observed_at=125,
            received_at=150,
        )
    assert registry.get_remote("ordered")["snapshot_hash"] == second["snapshot_hash"]

    conflicting = remote_snapshot(130)
    conflicting["host"]["displayName"] = "different"
    with pytest.raises(IdentityConflict, match="reused for different content"):
        registry.store_remote_snapshot(
            "ordered",
            conflicting,
            remote_host_id=REMOTE_HOST_ID,
            schema_version=1,
            protocol_version=1,
            observed_at=130,
            received_at=160,
        )

    failed = registry.mark_remote_failure(
        "ordered", error_code="ssh_unreachable", attempted_at=170
    )
    assert failed["reachability"] == "offline"
    with pytest.raises(StorageError, match="stale remote snapshot completion"):
        registry.store_remote_snapshot(
            "ordered",
            remote_snapshot(180),
            remote_host_id=REMOTE_HOST_ID,
            schema_version=1,
            protocol_version=1,
            observed_at=180,
            received_at=169,
        )
    assert registry.get_remote("ordered")["reachability"] == "offline"
    assert registry.get_remote("ordered")["snapshot_hash"] == second["snapshot_hash"]
    assert first["snapshot_hash"] != second["snapshot_hash"]


def test_remote_snapshot_rejects_unsafe_or_mismatched_envelopes(
    registry: Registry,
) -> None:
    registry.upsert_remote("remote", "remote.lan", "remote", observed_at=100)
    unsafe = {**remote_snapshot(110), "prompt": "do not cache me"}
    with pytest.raises(ProtocolError, match="forbidden"):
        registry.store_remote_snapshot(
            "remote",
            unsafe,
            remote_host_id=REMOTE_HOST_ID,
            schema_version=1,
            protocol_version=1,
            observed_at=110,
        )
    controlled = remote_snapshot(110)
    controlled["host"] = {
        "hostId": REMOTE_HOST_ID,
        "displayName": "bad\x1bname",
    }
    with pytest.raises(ProtocolError, match="control"):
        registry.store_remote_snapshot(
            "remote",
            controlled,
            remote_host_id=REMOTE_HOST_ID,
            schema_version=1,
            protocol_version=1,
            observed_at=110,
        )
    incompatible = {**remote_snapshot(110), "protocolVersion": 2}
    with pytest.raises(ProtocolError, match="not supported"):
        registry.store_remote_snapshot(
            "remote",
            incompatible,
            remote_host_id=REMOTE_HOST_ID,
            schema_version=1,
            protocol_version=2,
            observed_at=110,
        )
    with pytest.raises(IdentityConflict, match="expected remote host"):
        registry.store_remote_snapshot(
            "remote",
            remote_snapshot(110),
            remote_host_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            schema_version=1,
            protocol_version=1,
            observed_at=110,
        )
    with pytest.raises(StorageError, match="schema argument"):
        registry.store_remote_snapshot(
            "remote",
            remote_snapshot(110),
            remote_host_id=REMOTE_HOST_ID,
            schema_version=2,
            protocol_version=1,
            observed_at=110,
        )
    with pytest.raises(StorageError, match="observed_at"):
        registry.store_remote_snapshot(
            "remote",
            remote_snapshot(110),
            remote_host_id=REMOTE_HOST_ID,
            schema_version=1,
            protocol_version=1,
            observed_at=111,
        )


def test_registry_rejects_nested_transactions_and_use_after_close(tmp_path) -> None:
    registry = Registry(tmp_path / "switchboard.db")
    with registry.transaction():
        nested = registry.transaction()
        with pytest.raises(StorageError, match="nested"):
            nested.__enter__()
    registry.close()
    with pytest.raises(StorageError, match="closed"):
        registry.metadata()
