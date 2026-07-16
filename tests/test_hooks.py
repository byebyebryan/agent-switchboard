from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace

import pytest

import agent_switchboard.local_events as local_events_module
from agent_switchboard.domain import HostId
from agent_switchboard.hooks import (
    HookInputError,
    normalize_codex_event,
    read_hook_json,
)
from agent_switchboard.local_events import ingest_local_event
from agent_switchboard.snapshot import build_host_snapshot_json
from agent_switchboard.storage import IdentityConflict, Registry, StorageError

HOST_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
SECOND_ID = "33333333-3333-4333-8333-333333333333"
SESSION_KEY = f"{HOST_ID}:codex:{SESSION_ID}"
SECOND_KEY = f"{HOST_ID}:codex:{SECOND_ID}"
LAUNCH_ID = "44444444-4444-4444-8444-444444444444"
SURFACE_ID = "55555555-5555-4555-8555-555555555555"
REQUEST_ID = "66666666-6666-4666-8666-666666666666"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def payload(event: str, **extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "session_id": SESSION_ID,
        "transcript_path": "/private/SECRET-transcript.jsonl",
        "cwd": "/work/switchboard",
        "hook_event_name": event,
        "model": "SECRET-model",
        "permission_mode": "default",
        "prompt": "SECRET prompt",
        "last_assistant_message": "SECRET assistant message",
        "tool_input": {"command": "SECRET command"},
        "tool_response": "SECRET response",
        "unknown": {"nestedSecret": "SECRET nested"},
    }
    value.update(extra)
    return value


def normalized(
    event: str,
    *,
    entry_ns: int,
    environment: dict[str, str] | None = None,
    **extra: object,
):
    return normalize_codex_event(
        payload(event, **extra),
        environment or {},
        entry_ns=entry_ns,
        process_birth_id="a" * 64,
    )


def test_codex_normalization_allowlists_lifecycle_and_stable_identity() -> None:
    first = normalized(
        "UserPromptSubmit",
        entry_ns=2_000_000_000,
        turn_id="turn-1",
    )
    changed_private = normalize_codex_event(
        payload(
            "UserPromptSubmit",
            turn_id="turn-1",
            prompt="A different secret",
            tool_input={"command": "another secret"},
        ),
        {},
        entry_ns=2_500_000_000,
        process_birth_id="a" * 64,
    )

    assert changed_private.idempotency_key == first.idempotency_key
    assert changed_private.provider_session_id == SESSION_ID
    assert changed_private.cwd == "/work/switchboard"
    assert changed_private.provider_turn_id == "turn-1"
    retained = json.dumps(changed_private.storage_mapping(HostId(HOST_ID)))
    assert "SECRET" not in retained
    assert "prompt" not in retained
    assert "transcript" not in retained
    assert "tool_input" not in retained

    same_bucket = normalized(
        "SessionStart",
        entry_ns=4_500_000_000,
        source="resume",
    )
    retry = normalized(
        "SessionStart",
        entry_ns=4_900_000_000,
        source="resume",
    )
    later_resume = normalized(
        "SessionStart",
        entry_ns=5_000_000_000,
        source="resume",
    )
    assert same_bucket.idempotency_key == retry.idempotency_key
    assert later_resume.idempotency_key != retry.idempotency_key


def test_hook_json_is_bounded_deep_and_requires_supported_shapes() -> None:
    assert read_hook_json(io.BytesIO(b'{"hook_event_name":"Stop"}')) == {
        "hook_event_name": "Stop"
    }
    with pytest.raises(HookInputError, match="8 MiB"):
        read_hook_json(io.BytesIO(b" " * (8 * 1024 * 1024 + 1)))
    deep: object = "leaf"
    for _ in range(33):
        deep = [deep]
    with pytest.raises(HookInputError, match="depth"):
        read_hook_json(io.BytesIO(json.dumps({"value": deep}).encode()))
    with pytest.raises(HookInputError, match="unsupported"):
        normalized("PreToolUse", entry_ns=1, turn_id="turn-1")
    with pytest.raises(HookInputError, match="turn_id"):
        normalized("Stop", entry_ns=1)


@pytest.mark.parametrize("birth_id", ("a" * 63, "a" * 65, "A" * 64))
def test_normalization_requires_storage_sized_process_birth_digest(
    birth_id: str,
) -> None:
    with pytest.raises(HookInputError, match="opaque lowercase digest"):
        normalize_codex_event(
            payload("SessionStart", source="startup"),
            {},
            entry_ns=1_000_000_000,
            process_birth_id=birth_id,
        )


def test_tmux_normalization_matches_storage_locator_bounds() -> None:
    socket = "/" + "s" * 4095
    pane = "%" + "1" * 255
    retained = normalized(
        "SessionStart",
        entry_ns=1_000_000_000,
        environment={"TMUX": f"{socket},123,0", "TMUX_PANE": pane},
        source="startup",
    )
    assert retained.tmux_socket == socket
    assert retained.tmux_pane == pane

    socket_too_long = normalized(
        "SessionStart",
        entry_ns=2_000_000_000,
        environment={"TMUX": f"/{'s' * 4096},123,0", "TMUX_PANE": "%1"},
        source="startup",
    )
    assert socket_too_long.tmux_socket is None
    assert socket_too_long.tmux_pane is None

    pane_too_long = normalized(
        "SessionStart",
        entry_ns=3_000_000_000,
        environment={
            "TMUX": "/tmp/tmux,123,0",
            "TMUX_PANE": "%" + "1" * 256,
        },
        source="startup",
    )
    assert pane_too_long.tmux_socket is None
    assert pane_too_long.tmux_pane is None


def test_same_turn_kind_overrides_order_without_reducing_watermarks(
    tmp_path,
) -> None:
    with Registry(tmp_path / "same-turn-order.db") as registry:
        start = normalized("SessionStart", entry_ns=1_000_000_000, source="startup")
        registry.ingest_hook_event(
            start.storage_mapping(HostId(HOST_ID)), host_display_name="starship"
        )
        permission = normalized(
            "PermissionRequest",
            entry_ns=3_000_000_000,
            turn_id="t",
            tool_name="Bash",
            cwd="/newer",
        )
        registry.ingest_hook_event(
            permission.storage_mapping(HostId(HOST_ID)), host_display_name="starship"
        )
        delayed_stop = normalized(
            "Stop",
            entry_ns=2_000_000_000,
            turn_id="t",
            cwd="/older",
        )
        applied = registry.ingest_hook_event(
            delayed_stop.storage_mapping(HostId(HOST_ID)),
            host_display_name="starship",
        )

        assert applied.kind == "applied"
        assert applied.session["activity"] == "ready"
        assert applied.session["activity_reason"] == "turn_complete"
        assert applied.session["cwd"] == "/newer"
        assert applied.session["activity_order_ns"] == 3_000_000_000
        assert applied.session["runtime_order_ns"] == 3_000_000_000
        assert applied.session["last_hook_entry_ns"] == 3_000_000_000

        intervening_other_turn = normalized(
            "UserPromptSubmit",
            entry_ns=2_500_000_000,
            turn_id="different-turn",
            cwd="/must-stay-stale",
        )
        stale = registry.ingest_hook_event(
            intervening_other_turn.storage_mapping(HostId(HOST_ID)),
            host_display_name="starship",
        )
        assert stale.kind == "stale"
        assert stale.session["activity"] == "ready"
        assert stale.session["activity_reason"] == "turn_complete"
        assert stale.session["cwd"] == "/newer"
        assert stale.session["activity_order_ns"] == 3_000_000_000
        assert stale.session["runtime_order_ns"] == 3_000_000_000
        assert stale.session["last_hook_entry_ns"] == 3_000_000_000


def test_atomic_ingestion_materializes_ordering_and_never_retains_private_input(
    tmp_path,
) -> None:
    database = tmp_path / "switchboard.db"
    with Registry(database) as registry:
        start = normalized("SessionStart", entry_ns=1_000_000_000, source="startup")
        first = registry.ingest_hook_event(
            start.storage_mapping(HostId(HOST_ID)), host_display_name="starship"
        )
        assert first.kind == "applied"
        assert first.session["runtime_presence"] == "live"
        assert first.session["activity"] == "ready"
        assert first.session["resumability"] == "unknown"

        permission = normalized(
            "PermissionRequest",
            entry_ns=2_000_000_000,
            turn_id="turn-1",
            tool_name="Bash",
        )
        registry.ingest_hook_event(
            permission.storage_mapping(HostId(HOST_ID)), host_display_name="starship"
        )
        delayed_tool = normalized(
            "PostToolUse",
            entry_ns=3_000_000_000,
            turn_id="turn-1",
            tool_name="Bash",
            tool_use_id="call-1",
        )
        registry.ingest_hook_event(
            delayed_tool.storage_mapping(HostId(HOST_ID)), host_display_name="starship"
        )
        session = registry.get_session(SESSION_KEY)
        assert session is not None
        assert session["activity"] == "needs_input"
        assert session["activity_reason"] == "permission"
        assert session["last_hook_kind_priority"] == 40

        stale_other_turn = normalized(
            "Stop",
            entry_ns=1_500_000_000,
            turn_id="turn-old",
            cwd="/work/stale-should-not-win",
        )
        stale = registry.ingest_hook_event(
            stale_other_turn.storage_mapping(HostId(HOST_ID)),
            host_display_name="starship",
        )
        assert stale.kind == "stale"
        assert stale.session["activity"] == "needs_input"
        assert stale.session["cwd"] == "/work/switchboard"

        replay_mapping = replace(
            permission,
            entry_ns=9_000_000_000,
            observed_at=9_000,
            received_at=9_000,
        ).storage_mapping(HostId(HOST_ID))
        replay = registry.ingest_hook_event(
            replay_mapping, host_display_name="starship"
        )
        assert replay.kind == "duplicate"
        assert replay.event["received_at"] == permission.received_at

        changed = dict(permission.storage_mapping(HostId(HOST_ID)))
        changed["cwd"] = "/different"
        with pytest.raises(IdentityConflict, match="different content"):
            registry.ingest_hook_event(changed, host_display_name="starship")

    raw_database = database.read_bytes()
    assert b"SECRET" not in raw_database
    assert b"transcript" not in raw_database
    assert b"tool_input" not in raw_database


def test_hook_retention_prunes_event_and_runtime_as_one_replay_witness(
    tmp_path,
) -> None:
    with Registry(tmp_path / "retention.db") as registry:
        start = normalized("SessionStart", entry_ns=1_000_000_000, source="startup")
        start_mapping = start.storage_mapping(HostId(HOST_ID))
        registry.ingest_hook_event(
            start_mapping,
            host_display_name="starship",
            limit=1,
        )
        stop = normalized(
            "Stop",
            entry_ns=2_000_000_000,
            turn_id="turn-1",
        )
        registry.ingest_hook_event(
            stop.storage_mapping(HostId(HOST_ID)),
            host_display_name="starship",
            limit=1,
        )

        assert (
            registry.connection.execute(
                "SELECT COUNT(*) FROM events WHERE idempotency_key = ?",
                (start.idempotency_key,),
            ).fetchone()[0]
            == 0
        )
        assert (
            registry.connection.execute(
                "SELECT COUNT(*) FROM runtime_observations WHERE observation_key = ?",
                (f"event:{start.idempotency_key}",),
            ).fetchone()[0]
            == 0
        )

        replay = registry.ingest_hook_event(
            start_mapping,
            host_display_name="starship",
            limit=1,
        )
        assert replay.kind == "stale"
        assert replay.session["activity"] == "ready"
        assert (
            registry.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            == 1
        )
        assert (
            registry.connection.execute(
                "SELECT COUNT(*) FROM runtime_observations"
            ).fetchone()[0]
            == 1
        )


def test_newer_hook_without_locator_evidence_clears_stale_runtime_identity(
    tmp_path,
) -> None:
    tmux_environment = {
        "TMUX": "/tmp/private-switchboard-tmux,123,0",
        "TMUX_PANE": "%8",
    }
    with Registry(tmp_path / "locators.db") as registry:
        first = normalized(
            "UserPromptSubmit",
            entry_ns=1_000_000_000,
            environment=tmux_environment,
            turn_id="turn-1",
        )
        registry.ingest_hook_event(
            first.storage_mapping(HostId(HOST_ID)), host_display_name="starship"
        )
        registry.connection.execute(
            """
            UPDATE sessions
            SET runtime_pid = 123, provider_runtime_id = 'runtime-old',
                tmux_session = 'old', tmux_window = '1'
            WHERE session_key = ?
            """,
            (SESSION_KEY,),
        )

        stop = replace(
            normalized(
                "Stop",
                entry_ns=2_000_000_000,
                turn_id="turn-2",
            ),
            process_birth_id=None,
            tmux_socket=None,
            tmux_pane=None,
        )
        result = registry.ingest_hook_event(
            stop.storage_mapping(HostId(HOST_ID)), host_display_name="starship"
        )

        for field in (
            "runtime_pid",
            "provider_runtime_id",
            "runtime_process_birth_id",
            "tmux_session",
            "tmux_window",
            "tmux_pane",
            "tmux_socket",
        ):
            assert result.session[field] is None


def test_private_hook_evidence_is_not_publicly_hashed_but_detects_replay_conflict(
    tmp_path,
) -> None:
    environment = {
        "TMUX": "/tmp/private-switchboard-tmux,123,0",
        "TMUX_PANE": "%8",
    }
    event = normalized(
        "UserPromptSubmit",
        entry_ns=1_000_000_000,
        environment=environment,
        turn_id="turn-1",
    )
    mapping = event.storage_mapping(HostId(HOST_ID))
    with Registry(tmp_path / "private-hash.db") as registry:
        result = registry.ingest_hook_event(mapping, host_display_name="starship")
        assert result.runtime is not None
        expected_public_runtime = {
            "observation_key": f"event:{event.idempotency_key}",
            "host_id": HOST_ID,
            "provider": "codex",
            "session_key": SESSION_KEY,
            "launch_id": None,
            "source": "hook",
            "source_priority": 100,
            "runtime_presence": "live",
            "resumability": "unknown",
            "activity": "working",
            "activity_reason": "unknown",
            "attachment": "unknown",
            "pid": None,
            "provider_runtime_id": None,
            "tmux_session": None,
            "tmux_window": None,
            "tmux_pane": "%8",
            "observed_at": 1_000,
        }
        expected_hash = digest(
            json.dumps(
                expected_public_runtime,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        assert result.runtime["payload_hash"] == expected_hash

        snapshot = build_host_snapshot_json(
            registry,
            HOST_ID,
            generated_at=2_000,
        )
        public_runtime = json.loads(snapshot)["runtimes"][0]
        assert public_runtime["payloadHash"] == expected_hash
        assert "a" * 64 not in snapshot
        assert "/tmp/private-switchboard-tmux" not in snapshot

        changed_private_evidence = {**mapping, "process_birth_id": "b" * 64}
        with pytest.raises(IdentityConflict, match="different content"):
            registry.ingest_hook_event(
                changed_private_evidence,
                host_display_name="starship",
            )


def test_private_ordering_fields_require_validated_atomic_ingestion(tmp_path) -> None:
    with Registry(tmp_path / "private-fields.db") as registry:
        start = normalized("SessionStart", entry_ns=1_000_000_000, source="startup")
        registry.ingest_hook_event(
            start.storage_mapping(HostId(HOST_ID)), host_display_name="starship"
        )
        with pytest.raises(StorageError, match="private session evidence"):
            registry.upsert_session(
                {"session_key": SESSION_KEY, "runtime_order_ns": 2_000_000_000}
            )
        with pytest.raises(StorageError, match="private runtime evidence"):
            registry.record_runtime_observation(
                {
                    "observation_key": "private-write",
                    "host_id": HOST_ID,
                    "provider": "codex",
                    "session_key": SESSION_KEY,
                    "source": "hook",
                    "source_priority": 100,
                    "runtime_presence": "live",
                    "resumability": "unknown",
                    "activity": "ready",
                    "activity_reason": "unknown",
                    "attachment": "unknown",
                    "observed_at": 2_000,
                    "received_at": 2_000,
                    "entry_ns": 2_000_000_000,
                }
            )
        with pytest.raises(StorageError, match="private event evidence"):
            registry.record_event(
                {
                    "idempotency_key": "generic-event",
                    "host_id": HOST_ID,
                    "provider": "codex",
                    "session_key": SESSION_KEY,
                    "event_kind": "Stop",
                    "source_priority": 100,
                    "kind_priority": 50,
                    "observed_at": 2_000,
                    "received_at": 2_000,
                    "entry_ns": 2_000_000_000,
                }
            )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    (
        ("source_priority", 1_000_001, "no greater"),
        ("cwd", "/work/\x1bunsafe", "control"),
        ("process_birth_id", "A" * 64, "opaque lowercase"),
        ("tmux_socket", "/tmp/incoherent", "supplied together"),
    ),
)
def test_hook_storage_revalidates_untrusted_normalized_evidence(
    tmp_path,
    field: str,
    value: object,
    match: str,
) -> None:
    mapping = normalized(
        "Stop", entry_ns=1_000_000_000, turn_id="turn-1"
    ).storage_mapping(HostId(HOST_ID))
    mapping[field] = value
    with Registry(tmp_path / f"invalid-{field}.db") as registry:
        with pytest.raises(StorageError, match=match):
            registry.ingest_hook_event(mapping, host_display_name="starship")
        assert (
            registry.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            == 0
        )


def test_raw_hook_mapping_is_destroyed_before_local_state_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = payload("SessionStart", source="startup")
    safe = normalized("SessionStart", entry_ns=1_000_000_000, source="startup")
    captured: dict[str, object] = {}

    def fake_read(_stream: object) -> dict[str, object]:
        return raw

    def fake_normalize(
        value: dict[str, object],
        _environment: object,
        *,
        entry_ns: int | None,
    ):
        captured["payload"] = value
        assert entry_ns == 1_000_000_000
        return safe

    def stop_before_filesystem() -> HostId:
        assert captured["payload"] == {}
        raise RuntimeError("stop after raw payload destruction")

    monkeypatch.setattr(local_events_module, "read_hook_json", fake_read)
    monkeypatch.setattr(local_events_module, "normalize_codex_event", fake_normalize)
    monkeypatch.setattr(
        local_events_module,
        "load_or_create_host_id",
        stop_before_filesystem,
    )

    with pytest.raises(RuntimeError, match="raw payload destruction"):
        ingest_local_event(
            "codex",
            io.BytesIO(b"unused"),
            environment={},
            entry_ns=1_000_000_000,
        )
    assert raw == {}


def prepare_resume_launch(registry: Registry) -> None:
    registry.upsert_host(HOST_ID, "starship", is_local=True, observed_at=10)
    registry.upsert_session(
        {
            "session_key": SESSION_KEY,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": SESSION_ID,
            "cwd": "/work/switchboard",
            "first_observed_at": 20,
            "last_observed_at": 20,
        }
    )
    registry.reserve_launch(
        {
            "host_id": HOST_ID,
            "provider": "codex",
            "action": "resume",
            "project_id": None,
            "location_id": None,
            "cwd": None,
            "source_handoff_id": None,
            "target_session_key": SESSION_KEY,
            "transport": "tmux",
        },
        request_id=REQUEST_ID,
        lease_owner="private-worker-token",
        capability_hash=digest("capability"),
        expires_at=10_000,
        launch_id=LAUNCH_ID,
        created_at=100,
    )
    registry.upsert_surface(
        {
            "surface_id": SURFACE_ID,
            "host_id": HOST_ID,
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": "tmux:test:0.0",
            "role": "session",
            "launch_id": LAUNCH_ID,
            "created_at": 110,
            "last_observed_at": 110,
        }
    )
    for state, observed_at, surface_id in (
        ("surface_ready", 120, SURFACE_ID),
        ("waiting_for_client", 130, None),
        ("provider_started", 140, None),
    ):
        registry.transition_launch(
            LAUNCH_ID,
            state,
            lease_owner="private-worker-token",
            observed_at=observed_at,
            surface_id=surface_id,
        )


def test_hook_binding_consumes_stored_lease_and_mismatch_preserves_actual_session(
    tmp_path,
) -> None:
    environment = {
        "AGENT_SWITCHBOARD_LAUNCH_ID": LAUNCH_ID,
        "AGENT_SWITCHBOARD_SURFACE_ID": SURFACE_ID,
    }
    with Registry(tmp_path / "bound.db") as registry:
        prepare_resume_launch(registry)
        event = normalized(
            "SessionStart",
            entry_ns=200_000_000,
            environment=environment,
            source="resume",
        )
        result = registry.ingest_hook_event(
            event.storage_mapping(HostId(HOST_ID)), host_display_name="starship"
        )
        assert result.kind == "applied"
        assert result.launch is not None and result.launch["state"] == "bound"
        assert result.launch["lease_owner"] is None
        assert result.surface is not None
        assert result.surface["current_session_key"] == SESSION_KEY

    with Registry(tmp_path / "mismatch.db") as registry:
        prepare_resume_launch(registry)
        mismatch_payload = payload(
            "SessionStart",
            session_id=SECOND_ID,
            source="resume",
        )
        event = normalize_codex_event(
            mismatch_payload,
            environment,
            entry_ns=200_000_000,
            process_birth_id="a" * 64,
        )
        result = registry.ingest_hook_event(
            event.storage_mapping(HostId(HOST_ID)), host_display_name="starship"
        )
        assert result.kind == "provider_identity_mismatch"
        assert result.launch is not None and result.launch["state"] == "failed"
        assert result.launch["failure_code"] == "provider_identity_mismatch"
        assert result.launch["lease_owner"] is None
        assert result.session["session_key"] == SECOND_KEY
        assert result.session["surface_id"] is None
        assert result.surface is not None
        assert result.surface["current_session_key"] is None
        assert registry.get_session(SESSION_KEY) is not None


def test_hook_identity_failure_rolls_back_session_event_and_runtime(tmp_path) -> None:
    environment = {
        "AGENT_SWITCHBOARD_LAUNCH_ID": LAUNCH_ID,
        "AGENT_SWITCHBOARD_SURFACE_ID": "77777777-7777-4777-8777-777777777777",
    }
    with Registry(tmp_path / "rollback.db") as registry:
        prepare_resume_launch(registry)
        invalid = normalize_codex_event(
            payload("SessionStart", session_id=SECOND_ID, source="resume"),
            environment,
            entry_ns=200_000_000,
            process_birth_id="a" * 64,
        )
        with pytest.raises(StorageError, match="unknown hook surface"):
            registry.ingest_hook_event(
                invalid.storage_mapping(HostId(HOST_ID)),
                host_display_name="starship",
            )

        assert registry.get_session(SECOND_KEY) is None
        assert (
            registry.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            == 0
        )
        assert (
            registry.connection.execute(
                "SELECT COUNT(*) FROM runtime_observations"
            ).fetchone()[0]
            == 0
        )
        launch = registry.get_launch(LAUNCH_ID)
        assert launch is not None and launch["state"] == "provider_started"
