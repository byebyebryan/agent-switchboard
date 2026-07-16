from __future__ import annotations

import json
from uuid import UUID

import pytest

import agent_switchboard.storage as storage_module
from agent_switchboard.domain import HostId, ProviderId
from agent_switchboard.protocol import (
    MAX_JSON_BYTES,
    Capability,
    ErrorRecord,
    ErrorScope,
    ProtocolError,
    SnapshotEnvelope,
)
from agent_switchboard.snapshot import build_host_snapshot, build_host_snapshot_json
from agent_switchboard.storage import Registry

HOST_ID = "11111111-1111-4111-8111-111111111111"
REMOTE_HOST_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
EMPTY_PROJECT_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"
LOCATION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
FIRST_ID = "22222222-2222-4222-8222-222222222222"
SECOND_ID = "33333333-3333-4333-8333-333333333333"
REMOTE_ID = "44444444-4444-4444-8444-444444444444"
FIRST_KEY = f"{HOST_ID}:codex:{FIRST_ID}"
SECOND_KEY = f"{HOST_ID}:codex:{SECOND_ID}"
REMOTE_KEY = f"{REMOTE_HOST_ID}:claude:{REMOTE_ID}"
SURFACE_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
REMOTE_SURFACE_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


def _provider_record(index: int, *, cwd: str) -> dict[str, object]:
    provider_session_id = str(UUID(int=index + 1))
    return {
        "session_key": f"{HOST_ID}:codex:{provider_session_id}",
        "host_id": HOST_ID,
        "provider": "codex",
        "provider_session_id": provider_session_id,
        "name": None,
        "cwd": cwd,
        "created_at": 1,
        "provider_updated_at": 2,
        "last_activity_at": 2,
        "last_observed_at": 3,
        "metadata_source": "provider",
    }


@pytest.fixture
def registry(tmp_path) -> Registry:
    value = Registry(tmp_path / "switchboard.db")
    value.upsert_host(HOST_ID, "local", is_local=True, observed_at=1)
    value.upsert_host(REMOTE_HOST_ID, "remote", observed_at=2)
    value.materialize_projects(
        HOST_ID,
        (
            {
                "project_id": PROJECT_ID,
                "name": "switchboard",
                "aliases": ("sb",),
                "default_provider": "codex",
                "default_transport": "tmux",
                "context_sources": ("AGENTS.md",),
                "locations": (
                    {
                        "location_id": LOCATION_ID,
                        "path": "/work/project",
                        "display_name": "main checkout",
                        "repository_identity": "example/switchboard",
                        "is_default": True,
                    },
                ),
            },
            {
                "project_id": EMPTY_PROJECT_ID,
                "name": "project without a checkout",
                "locations": (),
            },
        ),
        observed_at=10,
    )
    value.upsert_session(
        {
            "session_key": FIRST_KEY,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": FIRST_ID,
            "project_id": PROJECT_ID,
            "location_id": LOCATION_ID,
            "name": "first",
            "purpose": "snapshot test",
            "cwd": "/work/project",
            "runtime_presence": "live",
            "resumability": "resumable",
            "activity": "needs_input",
            "activity_reason": "question",
            "attachment": "detached",
            "runtime_pid": 1234,
            "provider_runtime_id": "runtime-1",
            "tmux_session": "work",
            "tmux_window": "1",
            "tmux_pane": "%2",
            "runtime_observed_at": 30,
            "metadata_source": "provider",
            "state_confidence": "confirmed",
            "state_observed_at": 30,
            "pinned": True,
            "first_observed_at": 30,
            "last_observed_at": 30,
        }
    )
    value.upsert_session(
        {
            "session_key": SECOND_KEY,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": SECOND_ID,
            "metadata_source": "provider",
            "first_observed_at": 31,
            "last_observed_at": 31,
        }
    )
    value.upsert_session(
        {
            "session_key": REMOTE_KEY,
            "host_id": REMOTE_HOST_ID,
            "provider": "claude",
            "provider_session_id": REMOTE_ID,
            "metadata_source": "remote",
            "first_observed_at": 32,
            "last_observed_at": 32,
        }
    )
    value.upsert_surface(
        {
            "surface_id": SURFACE_ID,
            "host_id": HOST_ID,
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": "tmux:work:1.2",
            "workspace_id": "workspace-1",
            "role": "session",
            "client_attached": True,
            "created_at": 40,
            "last_observed_at": 40,
        }
    )
    value.bind_surface(SURFACE_ID, FIRST_KEY, confidence="confirmed", observed_at=41)
    value.upsert_surface(
        {
            "surface_id": REMOTE_SURFACE_ID,
            "host_id": REMOTE_HOST_ID,
            "provider": "claude",
            "transport": "tmux",
            "transport_locator": "tmux:remote:0.0",
            "role": "session",
            "created_at": 40,
            "last_observed_at": 40,
        }
    )
    value.record_runtime_observation(
        {
            "observation_id": "runtime-observation-1",
            "observation_key": "runtime-key-1",
            "host_id": HOST_ID,
            "provider": "codex",
            "session_key": FIRST_KEY,
            "source": "hook",
            "source_priority": 100,
            "runtime_presence": "live",
            "resumability": "resumable",
            "activity": "needs_input",
            "activity_reason": "question",
            "attachment": "detached",
            "pid": 1234,
            "provider_runtime_id": "runtime-1",
            "tmux_session": "work",
            "tmux_window": "1",
            "tmux_pane": "%2",
            "observed_at": 42,
            "received_at": 43,
        }
    )
    yield value
    value.close()


def test_snapshot_is_host_local_camel_case_and_protocol_valid(
    registry: Registry,
) -> None:
    capability = Capability(
        provider=ProviderId.CODEX,
        available=True,
        provider_version="0.144.4",
        tested_contract_min="0.144.4",
        tested_contract_max="0.144.4",
        features=("app_server_thread_list",),
    )
    error = ErrorRecord(
        code="provider_schema_degraded",
        message="Provider schema support is degraded.",
        scope=ErrorScope.PROVIDER,
        retryable=False,
        observed_at=90,
        host_id=HostId(HOST_ID),
        provider=ProviderId.CODEX,
    )

    raw = build_host_snapshot_json(
        registry,
        HOST_ID,
        generated_at=100,
        capabilities=(capability,),
        errors=(error,),
    )
    parsed = SnapshotEnvelope.from_json(raw)
    data = json.loads(raw)

    assert parsed.generated_at == 100
    assert data["host"] == {"hostId": HOST_ID, "displayName": "local"}
    assert [item["projectId"] for item in data["projects"]] == [
        PROJECT_ID,
        EMPTY_PROJECT_ID,
    ]
    assert [item["locationId"] for item in data["locations"]] == [LOCATION_ID]
    assert [item["sessionKey"] for item in data["sessions"]] == [
        FIRST_KEY,
        SECOND_KEY,
    ]
    assert REMOTE_KEY not in raw
    assert REMOTE_SURFACE_ID not in raw
    assert "session_key" not in raw
    assert "providerName" not in raw
    assert "nameSource" not in raw
    assert "raw_payload" not in raw

    first = data["sessions"][0]
    assert first["projectId"] == PROJECT_ID
    assert first["locationId"] == LOCATION_ID
    assert first["runtimeLocator"] == {
        "observedAt": 30,
        "pid": 1234,
        "providerRuntimeId": "runtime-1",
        "tmuxPane": "%2",
        "tmuxSession": "work",
        "tmuxWindow": "1",
    }
    assert first["surfaceId"] == SURFACE_ID
    assert first["pinned"] is True

    assert len(data["runtimes"]) == 1
    runtime = data["runtimes"][0]
    assert runtime["sessionKey"] == FIRST_KEY
    assert runtime["observationId"] == "runtime-observation-1"
    assert len(runtime["payloadHash"]) == 64
    assert data["surfaces"][0]["currentSessionKey"] == FIRST_KEY
    assert data["surfaces"][0]["bindingConfidence"] == "confirmed"
    assert data["capabilities"][0]["provider"] == "codex"
    assert data["errors"][0]["scope"] == "provider"


def test_snapshot_read_is_one_transaction_and_filters_relevance(
    registry: Registry,
) -> None:
    statements: list[str] = []
    registry.connection.set_trace_callback(statements.append)
    try:
        rows = registry.read_host_snapshot(REMOTE_HOST_ID)
    finally:
        registry.connection.set_trace_callback(None)

    transaction_statements = [
        statement.strip().upper()
        for statement in statements
        if statement.strip().upper() in {"BEGIN", "COMMIT", "ROLLBACK"}
    ]
    assert transaction_statements == ["BEGIN", "COMMIT"]
    assert rows.projects == ()
    assert rows.locations == ()
    assert [row["session_key"] for row in rows.sessions] == [REMOTE_KEY]
    assert rows.runtimes == ()
    assert [row["surface_id"] for row in rows.surfaces] == [REMOTE_SURFACE_ID]

    snapshot = build_host_snapshot(registry, REMOTE_HOST_ID, generated_at=100)
    assert snapshot.projects == ()
    assert snapshot.locations == ()
    assert [item["sessionKey"] for item in snapshot.sessions] == [REMOTE_KEY]


def test_snapshot_final_protocol_gate_rejects_invalid_generated_time(
    registry: Registry,
) -> None:
    with pytest.raises(ProtocolError, match="non-negative integer"):
        build_host_snapshot(registry, HOST_ID, generated_at=True)


def test_snapshot_error_order_is_canonical(registry: Registry) -> None:
    first = ErrorRecord(
        code="provider_degraded",
        message="Alpha diagnostic.",
        scope=ErrorScope.PROVIDER,
        retryable=False,
        observed_at=90,
        host_id=HostId(HOST_ID),
        provider=ProviderId.CODEX,
    )
    second = ErrorRecord(
        code="provider_degraded",
        message="Beta diagnostic.",
        scope=ErrorScope.PROVIDER,
        retryable=True,
        observed_at=90,
        host_id=HostId(HOST_ID),
        provider=ProviderId.CODEX,
    )

    forward = build_host_snapshot_json(
        registry,
        HOST_ID,
        generated_at=100,
        errors=(first, second),
    )
    reversed_input = build_host_snapshot_json(
        registry,
        HOST_ID,
        generated_at=100,
        errors=(second, first),
    )
    assert forward == reversed_input


def test_snapshot_runtime_history_is_a_bounded_latest_tail(
    registry: Registry,
) -> None:
    for index, observed_at in ((2, 44), (3, 46)):
        registry.record_runtime_observation(
            {
                "observation_id": f"runtime-observation-{index}",
                "observation_key": f"runtime-key-{index}",
                "host_id": HOST_ID,
                "provider": "codex",
                "session_key": FIRST_KEY,
                "source": "hook",
                "source_priority": 100,
                "runtime_presence": "live",
                "resumability": "resumable",
                "activity": "ready",
                "activity_reason": "turn_complete",
                "attachment": "detached",
                "observed_at": observed_at,
                "received_at": observed_at + 1,
            }
        )

    plan = registry.connection.execute(
        "EXPLAIN QUERY PLAN " + storage_module._SNAPSHOT_RUNTIME_TAIL_QUERY,
        (HOST_ID, 2),
    ).fetchall()
    details = [str(row["detail"]).upper() for row in plan]
    assert any("RUNTIME_OBSERVATIONS_HOST_RECENT" in detail for detail in details)
    assert not any(
        "USE TEMP B-TREE" in detail and "ORDER BY" in detail for detail in details
    )

    rows = registry.read_host_snapshot(HOST_ID, runtime_limit=2)
    assert [row["observation_id"] for row in rows.runtimes] == [
        "runtime-observation-2",
        "runtime-observation-3",
    ]


def test_large_legal_reconciliation_yields_bounded_truncated_snapshot(
    tmp_path,
) -> None:
    database = tmp_path / "large-registry.db"
    value = Registry(database)
    value.upsert_host(HOST_ID, "local", is_local=True, observed_at=1)
    cwd_prefix = "/missing-snapshot-root/" + "/".join(["x" * 90] * 10)
    records = tuple(
        _provider_record(index, cwd=f"{cwd_prefix}/{index}") for index in range(10_000)
    )
    value.reconcile_provider_sessions(HOST_ID, "codex", records, observed_at=3)

    rows = value.read_host_snapshot(HOST_ID)
    assert rows.retained_session_count == 10_000
    assert len(rows.sessions) == storage_module.DEFAULT_SNAPSHOT_SESSION_LIMIT

    raw = build_host_snapshot_json(value, HOST_ID, generated_at=4)
    parsed = SnapshotEnvelope.from_json(raw)
    error = next(
        item for item in parsed.errors if item.code == "snapshot_sessions_truncated"
    )

    assert len(raw.encode("utf-8")) <= MAX_JSON_BYTES
    assert len(parsed.sessions) == storage_module.DEFAULT_SNAPSHOT_SESSION_LIMIT
    assert error.scope is ErrorScope.HOST
    assert error.details == {"emittedCount": 1_000, "retainedCount": 10_000}
    assert (
        value.connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE host_id = ?", (HOST_ID,)
        ).fetchone()[0]
        == 10_000
    )
    value.close()


def test_snapshot_session_budget_uses_actual_utf8_size_and_filters_references(
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_switchboard.snapshot._SNAPSHOT_SESSION_BYTE_BUDGET",
        2,
    )

    snapshot = build_host_snapshot(registry, HOST_ID, generated_at=100)
    error = next(
        item for item in snapshot.errors if item.code == "snapshot_sessions_truncated"
    )

    assert snapshot.sessions == ()
    assert snapshot.runtimes == ()
    assert snapshot.surfaces == ()
    assert error.details == {"emittedCount": 0, "retainedCount": 2}


def test_worst_case_utf8_sessions_are_selected_by_encoded_byte_budget(tmp_path) -> None:
    value = Registry(tmp_path / "wide-registry.db")
    value.upsert_host(HOST_ID, "local", is_local=True, observed_at=1)
    wide_cwd = "/missing-wide-root/" + "/".join(["💡" * 150] * 20)
    records = tuple(_provider_record(index, cwd=wide_cwd) for index in range(600))
    value.reconcile_provider_sessions(HOST_ID, "codex", records, observed_at=3)

    raw = build_host_snapshot_json(value, HOST_ID, generated_at=4)
    snapshot = SnapshotEnvelope.from_json(raw)
    error = next(
        item for item in snapshot.errors if item.code == "snapshot_sessions_truncated"
    )

    assert len(raw.encode("utf-8")) <= MAX_JSON_BYTES
    assert 0 < len(snapshot.sessions) < 600
    assert error.details == {
        "emittedCount": len(snapshot.sessions),
        "retainedCount": 600,
    }
    assert (
        value.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 600
    )
    value.close()


def test_snapshot_catches_corrupt_stored_project_json(registry: Registry) -> None:
    registry.connection.execute("PRAGMA ignore_check_constraints = ON")
    registry.connection.execute(
        "UPDATE projects SET aliases_json = ? WHERE project_id = ?",
        ("{not-json", PROJECT_ID),
    )

    with pytest.raises(ProtocolError, match="stored project aliases_json is invalid"):
        build_host_snapshot(registry, HOST_ID, generated_at=100)


def test_snapshot_catches_project_json_integer_digit_limit(registry: Registry) -> None:
    registry.connection.execute(
        "UPDATE projects SET aliases_json = ? WHERE project_id = ?",
        (f"[{'1' * 5_000}]", PROJECT_ID),
    )

    with pytest.raises(ProtocolError, match="stored project aliases_json is invalid"):
        build_host_snapshot(registry, HOST_ID, generated_at=100)
