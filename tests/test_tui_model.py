from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agent_switchboard.domain import (
    Activity,
    Attachment,
    ProviderId,
    RuntimePresence,
    ValidationError,
)
from agent_switchboard.protocol import MAX_JSON_BYTES, SnapshotEnvelope
from agent_switchboard.state import DisplayStatus
from agent_switchboard.tui_model import (
    AttentionRank,
    CapabilityStatus,
    FrontendModel,
    IssueSource,
    ViewFilters,
)

ROOT = Path(__file__).parents[1]
SNAPSHOT_FIXTURE = ROOT / "tests/fixtures/protocol/v1/snapshot.json"


def _value() -> dict[str, object]:
    return json.loads(SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))


def _snapshot(value: dict[str, object]) -> SnapshotEnvelope:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(raw) <= MAX_JSON_BYTES
    return SnapshotEnvelope.from_json(raw)


def _session(
    template: dict[str, object],
    index: int,
    *,
    activity: str,
    runtime_presence: str = "live",
    resumability: str = "resumable",
    attachment: str = "detached",
    provider: str = "codex",
    last_observed_at: int = 1_000,
) -> dict[str, object]:
    session = copy.deepcopy(template)
    provider_session_id = f"55555555-5555-4555-8555-{index:012d}"
    session.update(
        {
            "sessionKey": (
                f"11111111-1111-4111-8111-111111111111:{provider}:{provider_session_id}"
            ),
            "provider": provider,
            "providerSessionId": provider_session_id,
            "name": f"session-{index}",
            "firstObservedAt": 1,
            "lastObservedAt": last_observed_at,
            "lastActivityAt": last_observed_at,
            "runtimePresence": runtime_presence,
            "resumability": resumability,
            "activity": activity,
            "activityReason": "unknown",
            "attachment": attachment,
        }
    )
    session.pop("surfaceId", None)
    return session


def _attention_value() -> dict[str, object]:
    value = _value()
    template = value["sessions"][0]  # type: ignore[index]
    assert isinstance(template, dict)
    sessions = [
        _session(
            template,
            1,
            activity="needs_input",
            provider="claude",
            last_observed_at=100,
        ),
        _session(template, 2, activity="working", last_observed_at=600),
        _session(
            template,
            3,
            activity="completed",
            runtime_presence="stopped",
            last_observed_at=200,
        ),
        _session(template, 4, activity="ready", last_observed_at=300),
        _session(
            template,
            5,
            activity="unknown",
            runtime_presence="stopped",
            last_observed_at=400,
        ),
        _session(
            template,
            6,
            activity="unknown",
            runtime_presence="unknown",
            resumability="unknown",
            attachment="unknown",
            last_observed_at=500,
        ),
    ]
    sessions[-1].pop("projectId", None)
    sessions[-1].pop("locationId", None)
    value["sessions"] = sessions
    value["runtimes"] = []
    value["surfaces"] = []
    return value


def test_empty_snapshot_has_neutral_capabilities_and_no_selection() -> None:
    value = _value()
    for collection in (
        "projects",
        "locations",
        "sessions",
        "runtimes",
        "surfaces",
        "capabilities",
        "errors",
    ):
        value[collection] = []

    model = FrontendModel.from_snapshot(
        _snapshot(value), now_ms=int(value["generatedAt"])
    )

    assert model.rows == model.visible_rows == ()
    assert model.launch_targets == ()
    assert model.issues == ()
    assert model.selected_session_key is None
    assert [item.status for item in model.capabilities] == [
        CapabilityStatus.NEUTRAL,
        CapabilityStatus.NEUTRAL,
    ]


def test_snapshot_projects_rows_launch_targets_and_capabilities() -> None:
    snapshot = _snapshot(_value())
    model = FrontendModel.from_snapshot(snapshot, now_ms=snapshot.generated_at)

    assert len(model.rows) == 1
    row = model.rows[0]
    assert row.label == "example"
    assert row.project_name == "example"
    assert row.location_path == "/work/example"
    assert row.status is DisplayStatus.WORKING
    assert row.attention_rank is AttentionRank.WORKING
    assert row.can_stop is False
    assert model.selected_row is row
    assert [target.provider for target in model.launch_targets] == [
        ProviderId.CODEX,
        ProviderId.CLAUDE,
    ]
    assert model.launch_targets[0].is_default is True
    assert model.launch_targets[0].is_preferred_provider is True
    assert model.launch_targets[1].is_preferred_provider is False
    assert model.capability("codex").status is CapabilityStatus.AVAILABLE
    assert model.capability("claude").status is CapabilityStatus.NEUTRAL

    undeclared_value = _value()
    undeclared_value["locations"][0]["declared"] = False  # type: ignore[index]
    undeclared = FrontendModel.from_snapshot(
        _snapshot(undeclared_value), now_ms=snapshot.generated_at
    )
    assert undeclared.launch_targets == ()


def test_attention_order_filters_and_selection_are_widget_independent() -> None:
    snapshot = _snapshot(_attention_value())
    model = FrontendModel.from_snapshot(snapshot, now_ms=snapshot.generated_at)

    assert [row.status for row in model.rows] == [
        DisplayStatus.NEEDS_INPUT,
        DisplayStatus.WORKING,
        DisplayStatus.COMPLETED,
        DisplayStatus.READY,
        DisplayStatus.PARKED,
        DisplayStatus.UNKNOWN,
    ]

    ready = model.with_filters(
        ViewFilters(
            providers=frozenset({"codex"}),
            activities=frozenset({Activity.READY}),
            runtime_presences=frozenset({RuntimePresence.LIVE}),
            attachments=frozenset({Attachment.DETACHED}),
        )
    )
    assert [row.name for row in ready.visible_rows] == ["session-4"]
    assert ready.selected_row is ready.visible_rows[0]

    unassigned = model.with_filters(ViewFilters(project_ids=frozenset({None})))
    assert [row.name for row in unassigned.visible_rows] == ["session-6"]

    selected = model.with_selection(model.rows[3].session_key)
    assert selected.selected_row is model.rows[3]
    hidden = selected.with_filters(ViewFilters(providers=frozenset({"claude"})))
    assert hidden.selected_row is hidden.visible_rows[0]
    with pytest.raises(ValidationError, match="not visible"):
        hidden.with_selection(model.rows[2].session_key)


def test_selection_survives_refresh_and_falls_back_to_the_same_index() -> None:
    value = _attention_value()
    first = _snapshot(value)
    model = FrontendModel.from_snapshot(first, now_ms=first.generated_at)
    selected_key = model.rows[3].session_key
    model = model.with_selection(selected_key)

    refreshed_value = copy.deepcopy(value)
    refreshed_value["generatedAt"] = int(value["generatedAt"]) + 1
    refreshed_value["sessions"][3]["lastActivityAt"] = 999  # type: ignore[index]
    refreshed = model.apply_snapshot(
        _snapshot(refreshed_value), now_ms=int(refreshed_value["generatedAt"])
    )
    assert refreshed.selected_session_key == selected_key

    removed_value = copy.deepcopy(refreshed_value)
    removed_value["generatedAt"] = int(refreshed_value["generatedAt"]) + 1
    removed_value["sessions"] = [  # type: ignore[index]
        session
        for session in removed_value["sessions"]  # type: ignore[union-attr]
        if session["sessionKey"] != selected_key
    ]
    removed = refreshed.apply_snapshot(
        _snapshot(removed_value), now_ms=int(removed_value["generatedAt"])
    )
    assert removed.selected_session_key == removed.visible_rows[3].session_key


def test_degraded_capability_errors_and_safe_stop_are_inspectable() -> None:
    value = _value()
    session = value["sessions"][0]  # type: ignore[index]
    surface = value["surfaces"][0]  # type: ignore[index]
    assert isinstance(session, dict) and isinstance(surface, dict)
    provider_session_id = "77777777-7777-4777-8777-777777777777"
    session_key = "11111111-1111-4111-8111-111111111111:claude:" + provider_session_id
    session.update(
        {
            "provider": "claude",
            "providerSessionId": provider_session_id,
            "sessionKey": session_key,
            "runtimePresence": "live",
        }
    )
    surface.update(
        {
            "provider": "claude",
            "currentSessionKey": session_key,
        }
    )
    value["runtimes"] = []
    value["capabilities"] = [
        {
            "provider": "claude",
            "available": False,
            "providerVersion": "2.1.210",
            "testedContractRange": {
                "minimum": "2.1.210",
                "maximum": "2.1.210",
            },
            "features": ["hooks", "native_resume", "tmux_runtime"],
            "degradedReasons": [
                {
                    "code": "agent_view_enabled",
                    "message": "Agent View must be disabled.",
                    "feature": "tmux_runtime",
                    "retryable": False,
                }
            ],
        }
    ]
    value["errors"] = [
        {
            "code": "provider_probe_failed",
            "message": "Claude probe failed.",
            "scope": "provider",
            "provider": "claude",
            "retryable": True,
            "observedAt": value["generatedAt"],
        }
    ]

    model = FrontendModel.from_snapshot(
        _snapshot(value), now_ms=int(value["generatedAt"])
    )
    row = model.rows[0]
    assert model.capability("claude").status is CapabilityStatus.DEGRADED
    assert model.capability("claude").available is False
    assert row.can_stop is True
    assert len(row.issue_ids) == 2
    assert {model.issue(issue_id).source for issue_id in row.issue_ids} == {
        IssueSource.CAPABILITY,
        IssueSource.SNAPSHOT,
    }

    surface.pop("launchId")
    blocked = FrontendModel.from_snapshot(
        _snapshot(value), now_ms=int(value["generatedAt"])
    )
    assert blocked.rows[0].can_stop is False


def test_unicode_token_search_matches_across_public_display_fields() -> None:
    value = _value()
    value["projects"][0]["name"] = "Café"  # type: ignore[index]
    value["projects"][0]["aliases"] = ["Résumé"]  # type: ignore[index]
    value["locations"][0]["displayName"] = "東京"  # type: ignore[index]
    value["sessions"][0]["name"] = "Straße Δ"  # type: ignore[index]
    value["sessions"][0]["purpose"] = "naïve router"  # type: ignore[index]
    snapshot = _snapshot(value)
    model = FrontendModel.from_snapshot(snapshot, now_ms=snapshot.generated_at)

    filtered = model.with_filters(ViewFilters(query="CAFE\u0301 東京 STRASSE naïve"))
    assert filtered.visible_rows == model.rows
    assert model.with_filters(ViewFilters(query="missing")).visible_rows == ()


def test_stale_snapshot_keeps_source_status_and_frontend_errors_are_replaceable() -> (
    None
):
    snapshot = _snapshot(_value())
    model = FrontendModel.from_snapshot(
        snapshot,
        now_ms=snapshot.generated_at + 120_001,
    )

    assert model.is_stale is True
    assert model.snapshot_age_ms == 120_001
    assert model.rows[0].status is DisplayStatus.WORKING

    failed = model.with_frontend_error(
        "command_timeout",
        "Refresh timed out.",
        retryable=True,
        observed_at=snapshot.generated_at + 120_001,
    ).with_frontend_error(
        "command_timeout",
        "Refresh timed out again.",
        retryable=True,
        observed_at=snapshot.generated_at + 120_002,
    )
    frontend_issues = [
        issue for issue in failed.issues if issue.source is IssueSource.FRONTEND
    ]
    assert len(frontend_issues) == 1
    assert frontend_issues[0].message == "Refresh timed out again."
    assert failed.clear_frontend_errors().issues == model.issues


def test_older_refresh_cannot_replace_a_newer_model() -> None:
    new_value = _value()
    generated_at = int(new_value["generatedAt"])
    new_value["sessions"][0]["name"] = "new result"  # type: ignore[index]
    model = FrontendModel.from_snapshot(_snapshot(new_value), now_ms=generated_at)

    old_value = copy.deepcopy(new_value)
    old_value["generatedAt"] = generated_at - 1_000
    old_value["sessions"][0]["name"] = "old result"  # type: ignore[index]
    retained = model.apply_snapshot(_snapshot(old_value), now_ms=generated_at + 1)

    assert retained.rows[0].name == "new result"
    assert retained.generated_at == generated_at
    assert retained.ignored_snapshot_count == 1
    assert retained.issue("frontend:stale_snapshot_ignored").retryable is True

    fresh_value = copy.deepcopy(new_value)
    fresh_value["generatedAt"] = generated_at + 1
    fresh_value["sessions"][0]["name"] = "fresh result"  # type: ignore[index]
    fresh = retained.apply_snapshot(_snapshot(fresh_value), now_ms=generated_at + 1)
    assert fresh.rows[0].name == "fresh result"
    assert all(issue.source is not IssueSource.FRONTEND for issue in fresh.issues)


def test_large_bounded_snapshot_builds_stable_rows_and_searches_locally() -> None:
    value = _value()
    template = value["sessions"][0]  # type: ignore[index]
    assert isinstance(template, dict)
    sessions = [
        _session(template, index, activity="working", last_observed_at=1_000)
        for index in range(2_000)
    ]
    sessions[1_337]["name"] = "Needle 東京"
    value["sessions"] = sessions
    value["runtimes"] = []
    value["surfaces"] = []
    snapshot = _snapshot(value)

    model = FrontendModel.from_snapshot(snapshot, now_ms=snapshot.generated_at)

    assert len(model.rows) == 2_000
    assert [row.session_key for row in model.rows] == sorted(
        row.session_key for row in model.rows
    )
    matching = model.with_filters(ViewFilters(query="needle 東京"))
    assert [row.name for row in matching.visible_rows] == ["Needle 東京"]
