from __future__ import annotations

import json
from pathlib import Path

import pytest

from spikes.thread_workstream import IsolationError, IsolationLayout
from spikes.thread_workstream.evidence import (
    EvidenceError,
    StudyResult,
    StudyStatus,
    assert_private_file,
    sanitize_hook_order,
    write_private_json,
)
from spikes.thread_workstream.isolation import reject_repository

FINGERPRINT = "a" * 64
ROOT = Path(__file__).parents[1]


def passing_result(**overrides: object) -> StudyResult:
    values: dict[str, object] = {
        "study": "native-rollover",
        "provider": "codex",
        "installed_version": "codex-cli 0.145.0",
        "contract_fingerprint": FINGERPRINT,
        "status": StudyStatus.PASS,
        "assertions": {"different_provider_identities": True},
        "event_order": [
            "thread-a:SessionStart:startup",
            "thread-b:SessionStart:clear",
        ],
        "isolation": {"private_tmux": True},
        "cleanup": {"private_capture_deleted": True},
        "timings_ms": {"total": 1},
    }
    values.update(overrides)
    return StudyResult(**values)  # type: ignore[arg-type]


def test_pass_requires_unassisted_assertions_isolation_and_cleanup() -> None:
    assert passing_result().as_dict()["status"] == "pass"
    for override in (
        {"assisted": True},
        {"assertions": {"different_provider_identities": False}},
        {"isolation": {"private_tmux": False}},
        {"cleanup": {"private_capture_deleted": False}},
    ):
        with pytest.raises(EvidenceError):
            passing_result(**override).as_dict()


def test_sanitized_result_rejects_provider_private_data(tmp_path: Path) -> None:
    forbidden = (
        "11111111-1111-4111-8111-111111111111",
        "/tmp/provider-state",
        "line one\nline two",
    )
    for value in forbidden:
        with pytest.raises(EvidenceError):
            passing_result(limitations=[value]).as_dict()
    with pytest.raises(EvidenceError):
        passing_result(
            assertions={"provider_session_id_matches": True}
        ).as_dict()

    destination = tmp_path / "result.json"
    passing_result().write(destination)
    retained = json.loads(destination.read_text())
    assert retained["privacyAudit"] == {
        "credentialsExcluded": True,
        "providerIdentifiersExcluded": True,
        "providerInputExcluded": True,
        "providerOutputExcluded": True,
        "runtimeLocationsExcluded": True,
        "runtimeProcessIdentifiersExcluded": True,
    }


def test_private_capture_is_0600_and_sanitizer_aliases_identities(
    tmp_path: Path,
) -> None:
    private = tmp_path / "raw.json"
    write_private_json(private, {"secret": "not retained"})
    assert_private_file(private)
    events = [
        {
            "provider_identity": "11111111-1111-4111-8111-111111111111",
            "event": "SessionStart",
            "source": "startup",
        },
        {
            "provider_identity": "22222222-2222-4222-8222-222222222222",
            "event": "SessionStart",
            "source": "clear",
        },
        {
            "provider_identity": "22222222-2222-4222-8222-222222222222",
            "event": "UserPromptSubmit",
        },
    ]
    assert sanitize_hook_order(events) == [
        "thread-a:SessionStart:startup",
        "thread-b:SessionStart:clear",
        "thread-b:UserPromptSubmit",
    ]


def test_isolation_layout_owns_every_mutable_target_and_cleans_up() -> None:
    layout = IsolationLayout.create()
    root = layout.root
    try:
        layout.validate()
        reject_repository(
            layout.repository,
            expected_root=layout.root,
            expected_token=layout.marker_token,
        )
        environment = layout.provider_environment()
        assert Path(environment["CODEX_HOME"]).is_relative_to(root)
        assert Path(environment["SWB_V3_STATE_ROOT"]).is_relative_to(root)
        write_private_json(layout.private_events, {"event": "private"})
        assert layout.erase_private_events() is True
    finally:
        layout.cleanup()
    assert not root.exists()


def test_isolation_rejects_wrong_marker_external_or_remote_repository(
    tmp_path: Path,
) -> None:
    layout = IsolationLayout.create()
    try:
        with pytest.raises(IsolationError):
            reject_repository(
                layout.repository,
                expected_root=layout.root,
                expected_token="wrong",
            )
        with pytest.raises(IsolationError):
            reject_repository(
                tmp_path,
                expected_root=layout.root,
                expected_token=layout.marker_token,
            )
        marker = layout.repository / ".agent-switchboard-disposable-spike"
        marker.write_text("forged\n")
        with pytest.raises(IsolationError):
            layout.validate()
    finally:
        layout.cleanup()


def test_retained_native_rollover_fixture_is_sanitized_and_passing() -> None:
    fixture = (
        ROOT
        / "spikes"
        / "fixtures"
        / "thread-workstream"
        / "codex"
        / "0.145.0"
        / "native-rollover.json"
    )
    retained = json.loads(fixture.read_text())
    assert retained["status"] == "pass"
    assert retained["assisted"] is False
    assert all(retained["assertions"].values())
    assert all(retained["isolation"].values())
    assert all(retained["cleanup"].values())
    encoded = fixture.read_text()
    assert "/home/" not in encoded
    assert "/tmp/" not in encoded
    assert "11111111-1111-4111-8111-111111111111" not in encoded
    assert "provider_input" not in encoded
    assert "provider_cwd" not in encoded
