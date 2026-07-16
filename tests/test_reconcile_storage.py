from __future__ import annotations

from copy import deepcopy

import pytest

from agent_switchboard.storage import IdentityConflict, Registry, StorageError

HOST_ID = "11111111-1111-4111-8111-111111111111"
FIRST_ID = "22222222-2222-4222-8222-222222222222"
SECOND_ID = "33333333-3333-4333-8333-333333333333"
THIRD_ID = "44444444-4444-4444-8444-444444444444"
PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
LOCATION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SURFACE_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
FIRST_KEY = f"{HOST_ID}:codex:{FIRST_ID}"
SECOND_KEY = f"{HOST_ID}:codex:{SECOND_ID}"
THIRD_KEY = f"{HOST_ID}:codex:{THIRD_ID}"


@pytest.fixture
def registry(tmp_path) -> Registry:
    value = Registry(tmp_path / "switchboard.db")
    value.upsert_host(HOST_ID, "local", is_local=True, observed_at=1)
    yield value
    value.close()


def provider_record(
    provider_session_id: str,
    observed_at: int,
    *,
    name: str | None = None,
    cwd: str = "/work/project",
) -> dict[str, object]:
    record: dict[str, object] = {
        "session_key": f"{HOST_ID}:codex:{provider_session_id}",
        "host_id": HOST_ID,
        "provider": "codex",
        "provider_session_id": provider_session_id,
        "name": name,
        "cwd": cwd,
        "created_at": 10,
        "provider_updated_at": 20,
        "last_activity_at": 20,
        "last_observed_at": observed_at,
        "metadata_source": "provider",
    }
    return record


def curated_session(registry: Registry, *, observed_at: int = 50) -> None:
    registry.upsert_session(
        {
            "session_key": FIRST_KEY,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": FIRST_ID,
            "name": "curated name",
            "purpose": "keep this purpose",
            "cwd": "/old/path",
            "runtime_presence": "live",
            "resumability": "unknown",
            "activity": "working",
            "activity_reason": "permission",
            "attachment": "attached",
            "runtime_pid": 123,
            "runtime_observed_at": observed_at,
            "metadata_source": "launch",
            "state_confidence": "inferred",
            "state_observed_at": observed_at,
            "pinned": True,
            "first_observed_at": observed_at,
            "last_observed_at": observed_at,
        }
    )


def test_reconcile_inserts_updates_and_preserves_other_axes(registry: Registry) -> None:
    curated_session(registry)

    result = registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (
            provider_record(FIRST_ID, 100, cwd="/new/path"),
            provider_record(SECOND_ID, 100, name="new session"),
        ),
        observed_at=100,
    )

    assert result.inserted_count == 1
    assert result.updated_count == 1
    assert result.observed_count == 2
    assert result.missing_count == 0
    assert result.records == result.sessions
    assert [row["session_key"] for row in result.sessions] == [FIRST_KEY, SECOND_KEY]

    updated = registry.get_session(FIRST_KEY)
    assert updated is not None
    assert updated["name"] == "curated name"
    assert updated["provider_name"] is None
    assert updated["name_source"] == "curated"
    assert updated["purpose"] == "keep this purpose"
    assert updated["cwd"] == "/new/path"
    assert updated["metadata_source"] == "launch"
    assert updated["runtime_presence"] == "live"
    assert updated["runtime_pid"] == 123
    assert updated["activity"] == "working"
    assert updated["activity_reason"] == "permission"
    assert updated["attachment"] == "attached"
    assert updated["state_confidence"] == "inferred"
    assert updated["resumability"] == "resumable"
    assert updated["state_observed_at"] == 50
    assert updated["pinned"] == 1

    inserted = registry.get_session(SECOND_KEY)
    assert inserted is not None
    assert inserted["name"] == "new session"
    assert inserted["provider_name"] == "new session"
    assert inserted["name_source"] == "provider"
    assert inserted["runtime_presence"] == "unknown"
    assert inserted["activity"] == "unknown"
    assert inserted["activity_reason"] == "unknown"
    assert inserted["resumability"] == "resumable"
    assert inserted["state_confidence"] == "unknown"
    assert inserted["state_observed_at"] is None
    assert inserted["first_observed_at"] == 100

    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (
            provider_record(FIRST_ID, 200, name="provider replacement"),
            provider_record(SECOND_ID, 200, name="updated provider name"),
        ),
        observed_at=200,
    )
    curated = registry.get_session(FIRST_KEY)
    assert curated is not None
    assert curated["name"] == "curated name"
    assert curated["provider_name"] == "provider replacement"
    assert curated["name_source"] == "curated"
    assert curated["metadata_source"] == "launch"
    provider_owned = registry.get_session(SECOND_KEY)
    assert provider_owned is not None
    assert provider_owned["name"] == "updated provider name"
    assert provider_owned["provider_name"] == "updated provider name"
    assert provider_owned["name_source"] == "provider"
    assert provider_owned["metadata_source"] == "provider"


def test_reconcile_preserves_project_handoff_and_surface_links(
    registry: Registry,
) -> None:
    registry.materialize_projects(
        HOST_ID,
        (
            {
                "project_id": PROJECT_ID,
                "name": "project",
                "locations": (
                    {
                        "location_id": LOCATION_ID,
                        "path": "/work/project",
                        "is_default": True,
                    },
                ),
            },
        ),
        observed_at=10,
    )
    curated_session(registry)
    registry.upsert_session(
        {
            "session_key": FIRST_KEY,
            "project_id": PROJECT_ID,
            "location_id": LOCATION_ID,
            "last_observed_at": 51,
        }
    )
    handoff = registry.append_handoff(
        session_key=FIRST_KEY,
        summary="Preserve the handoff.",
        source="user",
        source_host_id=HOST_ID,
        next_action="Continue reconciliation.",
        created_at=52,
    )
    registry.upsert_surface(
        {
            "surface_id": SURFACE_ID,
            "host_id": HOST_ID,
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": "tmux:work:0.0",
            "role": "session",
            "created_at": 53,
            "last_observed_at": 53,
        }
    )
    registry.bind_surface(SURFACE_ID, FIRST_KEY, confidence="confirmed", observed_at=54)

    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 100),),
        observed_at=100,
    )
    observed = registry.get_session(FIRST_KEY)
    assert observed is not None
    assert observed["project_id"] == PROJECT_ID
    assert observed["location_id"] == LOCATION_ID
    assert observed["latest_handoff_id"] == handoff["handoff_id"]
    assert observed["surface_id"] == SURFACE_ID
    assert observed["purpose"] == "keep this purpose"

    registry.reconcile_provider_sessions(HOST_ID, "codex", (), observed_at=200)
    missing = registry.get_session(FIRST_KEY)
    assert missing is not None
    assert missing["project_id"] == PROJECT_ID
    assert missing["location_id"] == LOCATION_ID
    assert missing["latest_handoff_id"] == handoff["handoff_id"]
    assert missing["surface_id"] == SURFACE_ID
    assert missing["activity"] == "working"
    assert missing["state_confidence"] == "inferred"
    assert missing["state_observed_at"] == 50


def test_provider_scan_preserves_state_clock_and_allows_delayed_hook(
    registry: Registry,
) -> None:
    registry.upsert_session(
        {
            "session_key": FIRST_KEY,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": FIRST_ID,
            "activity": "working",
            "activity_reason": "unknown",
            "state_confidence": "confirmed",
            "state_observed_at": 50,
            "metadata_source": "hook",
            "first_observed_at": 50,
            "last_observed_at": 50,
        }
    )

    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 100),),
        observed_at=100,
    )

    stored = registry.get_session(FIRST_KEY)
    assert stored is not None
    assert stored["activity"] == "working"
    assert stored["activity_reason"] == "unknown"
    assert stored["state_confidence"] == "confirmed"
    assert stored["resumability"] == "resumable"
    assert stored["state_observed_at"] == 50

    delayed = registry.upsert_session(
        {
            "session_key": FIRST_KEY,
            "activity": "ready",
            "activity_reason": "turn_complete",
            "state_confidence": "confirmed",
            "state_observed_at": 75,
            "last_observed_at": 101,
        }
    )
    assert delayed["activity"] == "ready"
    assert delayed["activity_reason"] == "turn_complete"
    assert delayed["state_observed_at"] == 75
    assert delayed["state_confidence"] == "confirmed"


def test_provider_owned_name_can_update_and_clear(registry: Registry) -> None:
    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 100, name="first name"),),
        observed_at=100,
    )
    registry.upsert_session(
        {
            "session_key": FIRST_KEY,
            "metadata_source": "location_match",
            "last_observed_at": 150,
        }
    )
    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 200, name="second name"),),
        observed_at=200,
    )
    updated = registry.get_session(FIRST_KEY)
    assert updated is not None
    assert updated["name"] == "second name"
    assert updated["provider_name"] == "second name"
    assert updated["name_source"] == "provider"
    assert updated["metadata_source"] == "location_match"

    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 300, name=None),),
        observed_at=300,
    )
    cleared = registry.get_session(FIRST_KEY)
    assert cleared is not None
    assert cleared["name"] is None
    assert cleared["provider_name"] is None
    assert cleared["name_source"] == "provider"
    assert cleared["metadata_source"] == "location_match"


def test_source_only_curated_claim_protects_consistent_provider_name(
    registry: Registry,
) -> None:
    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 100, name="provider one"),),
        observed_at=100,
    )

    curated = registry.upsert_session(
        {
            "session_key": FIRST_KEY,
            "name_source": "curated",
            "last_observed_at": 150,
        }
    )
    assert curated["name"] == "provider one"
    assert curated["provider_name"] == "provider one"
    assert curated["name_source"] == "curated"

    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 200, name="provider two"),),
        observed_at=200,
    )
    protected = registry.get_session(FIRST_KEY)
    assert protected is not None
    assert protected["name"] == "provider one"
    assert protected["provider_name"] == "provider two"
    assert protected["name_source"] == "curated"


def test_user_rename_survives_while_provider_name_tracks_scans(
    registry: Registry,
) -> None:
    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 100, name="provider one"),),
        observed_at=100,
    )
    renamed = registry.upsert_session(
        {
            "session_key": FIRST_KEY,
            "name": "user title",
            "last_observed_at": 150,
        }
    )
    assert renamed["name_source"] == "curated"
    assert renamed["provider_name"] == "provider one"

    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 200, name="user title"),),
        observed_at=200,
    )
    collision = registry.get_session(FIRST_KEY)
    assert collision is not None
    assert collision["name"] == "user title"
    assert collision["provider_name"] == "user title"
    assert collision["name_source"] == "curated"

    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 300, name="provider three"),),
        observed_at=300,
    )

    stored = registry.get_session(FIRST_KEY)
    assert stored is not None
    assert stored["name"] == "user title"
    assert stored["provider_name"] == "provider three"
    assert stored["name_source"] == "curated"
    assert stored["metadata_source"] == "provider"

    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 400, name=None),),
        observed_at=400,
    )
    cleared_provider = registry.get_session(FIRST_KEY)
    assert cleared_provider is not None
    assert cleared_provider["name"] == "user title"
    assert cleared_provider["provider_name"] is None
    assert cleared_provider["name_source"] == "curated"
    assert cleared_provider["metadata_source"] == "provider"


def test_empty_nonprovider_name_can_accept_provider_metadata(
    registry: Registry,
) -> None:
    registry.upsert_session(
        {
            "session_key": FIRST_KEY,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": FIRST_ID,
            "metadata_source": "launch",
            "first_observed_at": 50,
            "last_observed_at": 50,
        }
    )

    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 100, name="provider name"),),
        observed_at=100,
    )
    stored = registry.get_session(FIRST_KEY)
    assert stored is not None
    assert stored["name"] == "provider name"
    assert stored["provider_name"] == "provider name"
    assert stored["name_source"] == "provider"
    assert stored["metadata_source"] == "launch"


def test_reconcile_marks_absent_and_empty_scan_missing_without_delete(
    registry: Registry,
) -> None:
    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (
            provider_record(FIRST_ID, 100),
            provider_record(SECOND_ID, 100, name="retained"),
        ),
        observed_at=100,
    )

    partial = registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 200),),
        observed_at=200,
    )
    assert partial.missing_count == 1
    absent = registry.get_session(SECOND_KEY)
    assert absent is not None
    assert absent["name"] == "retained"
    assert absent["resumability"] == "missing"
    assert absent["last_observed_at"] == 200
    assert absent["state_observed_at"] is None

    empty = registry.reconcile_provider_sessions(HOST_ID, "codex", (), observed_at=300)
    assert empty.inserted_count == 0
    assert empty.updated_count == 0
    assert empty.missing_count == 2
    assert len(registry.list_sessions(host_id=HOST_ID)) == 2
    assert {row["resumability"] for row in empty.sessions} == {"missing"}


def test_reconcile_is_idempotent_at_the_same_timestamp(registry: Registry) -> None:
    records = (provider_record(FIRST_ID, 100), provider_record(SECOND_ID, 100))
    first = registry.reconcile_provider_sessions(
        HOST_ID, "codex", records, observed_at=100
    )
    second = registry.reconcile_provider_sessions(
        HOST_ID, "codex", records, observed_at=100
    )

    assert first.sessions == second.sessions
    assert second.inserted_count == 0
    assert second.updated_count == 2


def test_reconcile_rejects_duplicates_and_private_fields_before_mutation(
    registry: Registry,
) -> None:
    curated_session(registry)
    before = deepcopy(registry.list_sessions(host_id=HOST_ID))
    duplicate = provider_record(SECOND_ID, 100)

    with pytest.raises(IdentityConflict, match="duplicate provider session"):
        registry.reconcile_provider_sessions(
            HOST_ID,
            "codex",
            (duplicate, duplicate),
            observed_at=100,
        )
    assert registry.list_sessions(host_id=HOST_ID) == before

    private = provider_record(SECOND_ID, 100)
    private["raw_payload"] = {"prompt": "do not retain"}
    with pytest.raises(StorageError, match="unsupported retained fields"):
        registry.reconcile_provider_sessions(
            HOST_ID,
            "codex",
            (provider_record(THIRD_ID, 100), private),
            observed_at=100,
        )
    assert registry.list_sessions(host_id=HOST_ID) == before
    assert registry.get_session(THIRD_KEY) is None


def test_stale_row_rolls_back_the_entire_complete_scan(registry: Registry) -> None:
    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 200), provider_record(SECOND_ID, 200)),
        observed_at=200,
    )
    before = deepcopy(registry.list_sessions(host_id=HOST_ID))

    with pytest.raises(StorageError, match="stale provider scan"):
        registry.reconcile_provider_sessions(
            HOST_ID,
            "codex",
            (provider_record(SECOND_ID, 150), provider_record(THIRD_ID, 150)),
            observed_at=150,
        )

    assert registry.list_sessions(host_id=HOST_ID) == before
    assert registry.get_session(THIRD_KEY) is None


def test_conflicting_same_timestamp_scan_rolls_back(registry: Registry) -> None:
    registry.reconcile_provider_sessions(
        HOST_ID,
        "codex",
        (provider_record(FIRST_ID, 100, cwd="/first"),),
        observed_at=100,
    )

    with pytest.raises(StorageError, match="conflicting provider metadata"):
        registry.reconcile_provider_sessions(
            HOST_ID,
            "codex",
            (
                provider_record(FIRST_ID, 100, cwd="/conflict"),
                provider_record(SECOND_ID, 100),
            ),
            observed_at=100,
        )

    assert registry.get_session(SECOND_KEY) is None
    assert registry.get_session(FIRST_KEY)["cwd"] == "/first"  # type: ignore[index]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("last_observed_at", 99, "observation time"),
        ("metadata_source", "app-server", "metadata_source"),
        ("host_id", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "identity"),
    ),
)
def test_reconcile_validates_complete_provider_records(
    registry: Registry, field: str, value: object, message: str
) -> None:
    record = provider_record(FIRST_ID, 100)
    record[field] = value

    with pytest.raises(StorageError, match=message):
        registry.reconcile_provider_sessions(
            HOST_ID, "codex", (record,), observed_at=100
        )
    assert registry.list_sessions(host_id=HOST_ID) == []


def test_reconcile_requires_explicit_nullable_name(registry: Registry) -> None:
    record = provider_record(FIRST_ID, 100)
    del record["name"]

    with pytest.raises(StorageError, match="provider session is incomplete"):
        registry.reconcile_provider_sessions(
            HOST_ID,
            "codex",
            (record,),
            observed_at=100,
        )
    assert registry.list_sessions(host_id=HOST_ID) == []
