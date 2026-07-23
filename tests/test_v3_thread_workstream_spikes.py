from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from spikes.thread_workstream import IsolationError, IsolationLayout
from spikes.thread_workstream.adoption import (
    AdoptionError,
    AdoptionMachine,
    AtomicBindingStore,
    BindingRecord,
    ClearObservation,
    InputObservation,
    TransitionClassification,
)
from spikes.thread_workstream.codex_app_server import latest_plan
from spikes.thread_workstream.evidence import (
    EvidenceError,
    StudyResult,
    StudyStatus,
    assert_private_file,
    audit_sanitized_evidence,
    sanitize_hook_order,
    write_private_json,
)
from spikes.thread_workstream.isolation import reject_repository
from spikes.thread_workstream.memory_continuity import (
    ContinuityProfile,
    FailingMemoryAdapter,
    FixedMemoryAdapter,
    MemoryObservation,
    TransitionCapsule,
    evaluate_continuity,
)
from spikes.thread_workstream.memory_study import run_study as run_memory_study
from spikes.thread_workstream.navigator import (
    NavigatorError,
    NavigatorState,
    TransitionVisibility,
)
from spikes.thread_workstream.worktree_study import run_study as run_worktree_study

FINGERPRINT = "a" * 64
ROOT = Path(__file__).parents[1]


def initial_binding() -> BindingRecord:
    return BindingRecord(
        version=1,
        provider_identity="thread-a-private",
        capability="capability-a",
        launch="launch",
        surface="surface",
        server_generation="generation",
        pane="pane",
        process_birth=100,
        working_directory="disposable-repository",
    )


def clear_event(**overrides: object) -> ClearObservation:
    values: dict[str, object] = {
        "nonce": "clear-1",
        "order": 1,
        "predecessor_identity": "thread-a-private",
        "destination_identity": "thread-b-private",
        "source": "clear",
        "launch": "launch",
        "surface": "surface",
        "server_generation": "generation",
        "pane": "pane",
        "process_birth": 100,
        "working_directory": "disposable-repository",
        "provider_ancestor": True,
        "provider_thread_exists": True,
        "accepted_plan_digest": "plan-a",
    }
    values.update(overrides)
    return ClearObservation(**values)  # type: ignore[arg-type]


def input_event(**overrides: object) -> InputObservation:
    values: dict[str, object] = {
        "nonce": "input-1",
        "order": 2,
        "provider_identity": "thread-b-private",
        "launch": "launch",
        "surface": "surface",
        "server_generation": "generation",
        "pane": "pane",
        "process_birth": 100,
        "working_directory": "disposable-repository",
        "provider_ancestor": True,
        "provider_thread_exists": True,
        "carried_plan_digest": "plan-a",
    }
    values.update(overrides)
    return InputObservation(**values)  # type: ignore[arg-type]


def adoption_machine() -> AdoptionMachine:
    capabilities = iter(("capability-b", "capability-c", "capability-d"))
    return AdoptionMachine(
        AtomicBindingStore(initial_binding()),
        capability_factory=lambda: next(capabilities),
    )


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
        passing_result(assertions={"provider_session_id_matches": True}).as_dict()

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


def test_retained_trust_history_fixture_is_sanitized_and_passing() -> None:
    fixture = (
        ROOT
        / "spikes"
        / "fixtures"
        / "thread-workstream"
        / "codex"
        / "0.145.0"
        / "trust-history.json"
    )
    retained = json.loads(fixture.read_text())
    assert retained["status"] == "pass"
    assert retained["assisted"] is False
    assert all(retained["assertions"].values())
    assert all(retained["isolation"].values())
    assert all(retained["cleanup"].values())
    assert retained["limitations"] == ["historical resume hook was not observed"]
    encoded = fixture.read_text()
    assert "/home/" not in encoded
    assert "/tmp/" not in encoded
    assert "provider_identity" not in encoded
    assert "process_birth" not in encoded


def test_repeated_adoption_rotates_identity_and_capability_atomically() -> None:
    machine = adoption_machine()
    machine.begin_clear(clear_event())
    first = machine.confirm_input(input_event())
    assert first.classification is TransitionClassification.TASK_TRANSITION
    assert first.capability_rotated is True
    assert machine.current.provider_identity == "thread-b-private"
    assert machine.current.capability == "capability-b"
    assert machine.current.version == 2

    machine.begin_clear(
        clear_event(
            nonce="clear-2",
            order=3,
            predecessor_identity="thread-b-private",
            destination_identity="thread-c-private",
            accepted_plan_digest="plan-b",
        )
    )
    second = machine.confirm_input(
        input_event(
            nonce="input-2",
            order=4,
            provider_identity="thread-c-private",
            carried_plan_digest="plan-b",
        )
    )
    assert second.binding_version == 3
    assert second.capability_rotated is True
    assert machine.current.provider_identity == "thread-c-private"
    assert machine.current.capability == "capability-c"
    assert machine.current.launch == "launch"
    assert machine.current.surface == "surface"


def test_generic_clear_rotates_provider_without_fabricating_task_boundary() -> None:
    machine = adoption_machine()
    machine.begin_clear(clear_event(accepted_plan_digest=None))
    receipt = machine.confirm_input(input_event(carried_plan_digest=None))
    assert receipt.classification is TransitionClassification.PROVIDER_CLEAR
    assert machine.current.provider_identity == "thread-b-private"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("launch", "forged", "authority_launch_mismatch"),
        ("surface", "forged", "authority_surface_mismatch"),
        ("server_generation", "forged", "authority_server_generation_mismatch"),
        ("pane", "forged", "authority_pane_mismatch"),
        ("process_birth", 999, "authority_process_birth_mismatch"),
        (
            "working_directory",
            "wrong-repository",
            "authority_working_directory_mismatch",
        ),
        ("provider_ancestor", False, "authority_provider_ancestor_mismatch"),
        (
            "provider_thread_exists",
            False,
            "authority_provider_thread_exists_mismatch",
        ),
    ],
)
def test_adoption_rejects_forged_or_mismatched_clear(
    field: str,
    value: object,
    code: str,
) -> None:
    machine = adoption_machine()
    with pytest.raises(AdoptionError) as caught:
        machine.begin_clear(replace(clear_event(), **{field: value}))
    assert caught.value.code == code
    assert machine.current == initial_binding()


def test_adoption_rejects_replay_stale_source_resume_unknown_and_concurrency() -> None:
    machine = adoption_machine()
    with pytest.raises(AdoptionError, match="clear_source_invalid"):
        machine.begin_clear(clear_event(source="resume"))
    with pytest.raises(AdoptionError, match="clear_predecessor_unknown"):
        machine.begin_clear(clear_event(predecessor_identity="unknown"))

    machine.begin_clear(clear_event())
    with pytest.raises(AdoptionError, match="event_replayed"):
        machine.begin_clear(clear_event())
    with pytest.raises(AdoptionError, match="clear_concurrent"):
        machine.begin_clear(clear_event(nonce="clear-2", order=2))
    with pytest.raises(AdoptionError, match="clear_conflict_unresolved"):
        machine.confirm_input(input_event(order=3))
    assert machine.current == initial_binding()

    fresh = adoption_machine()
    fresh.begin_clear(clear_event())
    with pytest.raises(AdoptionError, match="event_stale"):
        fresh.confirm_input(input_event(order=1))


@pytest.mark.parametrize("fault", ["before", "partial"])
def test_atomic_binding_rejects_partial_commit_without_mutation(fault: str) -> None:
    machine = adoption_machine()
    machine.begin_clear(clear_event())
    with pytest.raises(AdoptionError, match=f"binding_{fault}_commit_rejected"):
        machine.confirm_input(input_event(), fail_at=fault)
    assert machine.current == initial_binding()


def test_atomic_binding_recovers_complete_record_after_lost_commit_response() -> None:
    machine = adoption_machine()
    machine.begin_clear(clear_event())
    with pytest.raises(AdoptionError, match="binding_commit_outcome_uncertain"):
        machine.confirm_input(input_event(), fail_at="after")
    assert machine.current.provider_identity == "thread-b-private"
    assert machine.current.capability == "capability-b"
    assert machine.current.version == 2


def test_navigator_shows_pending_and_confirmed_aliases_without_moving_tip() -> None:
    pending = NavigatorState(
        previous="thread-a",
        current="thread-b",
        transition=TransitionVisibility.PENDING,
        active_tip="thread-b",
    )
    assert "Transition: pending" in pending.render()
    confirmed = replace(pending, transition=TransitionVisibility.CONFIRMED)
    assert confirmed.render() == (
        "Previous: thread-a\n"
        "Current: thread-b\n"
        "Transition: confirmed\n"
        "Active: thread-b\n"
    )
    assert confirmed.activate_current() == ("thread-b", 1)
    with pytest.raises(NavigatorError):
        replace(confirmed, active_tip="thread-a")
    with pytest.raises(NavigatorError):
        replace(confirmed, previous="11111111-1111-4111-8111-111111111111")


def test_input_fence_drops_attempted_historical_input(tmp_path: Path) -> None:
    status = tmp_path / "fence.json"
    reached_child = tmp_path / "child-received-input"
    child = (
        "import pathlib,select,sys,time;"
        "ready=select.select([sys.stdin],[],[],3)[0];"
        f"pathlib.Path({str(reached_child)!r}).write_text('bad') if ready else None;"
        "time.sleep(30)"
    )
    environment = dict(os.environ)
    environment["ASB_SPIKE_DISPOSABLE_ROOT"] = str(tmp_path)
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "spikes" / "thread_workstream" / "input_fence.py"),
            "--status",
            str(status),
            "--",
            sys.executable,
            "-c",
            child,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=environment,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not status.exists():
            time.sleep(0.02)
        assert status.exists()
        assert process.stdin is not None
        process.stdin.write(b"forbidden historical input\n")
        process.stdin.flush()
        while time.monotonic() < deadline:
            if json.loads(status.read_text())["droppedBytes"] > 0:
                break
            time.sleep(0.02)
        retained = json.loads(status.read_text())
        assert retained["droppedBytes"] == len(b"forbidden historical input\n")
        assert not reached_child.exists()
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert json.loads(status.read_text())["childStarted"] is False
    assert not reached_child.exists()


def test_latest_plan_uses_completed_structured_item() -> None:
    assert (
        latest_plan(
            {
                "turns": [
                    {"items": [{"type": "plan", "text": "first"}]},
                    {"items": [{"type": "plan", "text": "authoritative"}]},
                ]
            }
        )
        == "authoritative"
    )


def test_managed_worktree_study_passes_all_ownership_gates() -> None:
    _version, _fingerprint, status, assertions, cleanup = run_worktree_study()
    assert status is StudyStatus.PASS
    assert all(assertions.values())
    assert all(cleanup.values())


def test_retained_managed_worktree_fixture_is_sanitized_and_passing() -> None:
    fixture = (
        ROOT
        / "spikes"
        / "fixtures"
        / "thread-workstream"
        / "git"
        / "managed-worktree.json"
    )
    retained = json.loads(fixture.read_text())
    assert retained["status"] == "pass"
    assert all(retained["assertions"].values())
    assert all(retained["isolation"].values())
    assert all(retained["cleanup"].values())
    encoded = fixture.read_text()
    assert "/home/" not in encoded
    assert "/tmp/" not in encoded
    assert "recorded_commit" not in encoded
    assert "repository_identity" not in encoded


def memory_capsule() -> TransitionCapsule:
    return TransitionCapsule(
        accepted_plan="bounded accepted plan",
        triggering_input="continue",
        project_scope="disposable-project",
    )


def healthy_memory(**overrides: object) -> MemoryObservation:
    values: dict[str, object] = {
        "available": True,
        "healthy": True,
        "scope_exact": True,
        "recent_context": True,
        "planning_context": True,
        "latency_ms": 1,
    }
    values.update(overrides)
    return MemoryObservation(**values)  # type: ignore[arg-type]


def test_memory_full_continuity_requires_two_timely_exact_observations() -> None:
    outcome = evaluate_continuity(
        memory_capsule(),
        FixedMemoryAdapter(healthy_memory()),
    )
    assert outcome.profile is ContinuityProfile.FULL
    assert outcome.plan_preserved is True
    assert outcome.source == healthy_memory()
    assert outcome.destination == healthy_memory()
    assert outcome.memory_authorized_transition is False


@pytest.mark.parametrize(
    "adapter",
    [
        FailingMemoryAdapter(),
        FixedMemoryAdapter(healthy_memory(latency_ms=2_001)),
        FixedMemoryAdapter(healthy_memory(recent_context=False)),
        FixedMemoryAdapter(healthy_memory(scope_exact=False)),
    ],
)
def test_memory_degradation_is_immediate_only_and_plan_sufficient(
    adapter: object,
) -> None:
    outcome = evaluate_continuity(
        memory_capsule(),
        adapter,  # type: ignore[arg-type]
    )
    assert outcome.profile is ContinuityProfile.IMMEDIATE_ONLY
    assert outcome.plan_preserved is True
    assert outcome.memory_authorized_transition is False


def test_incomplete_immediate_capsule_blocks_without_memory_authority() -> None:
    outcome = evaluate_continuity(
        replace(memory_capsule(), accepted_plan=""),
        FixedMemoryAdapter(healthy_memory()),
    )
    assert outcome.profile is ContinuityProfile.BLOCKED
    assert outcome.plan_preserved is False
    assert outcome.source is None
    assert outcome.destination is None
    assert outcome.memory_authorized_transition is False


def test_live_memory_study_passes_reference_and_degradation_gates() -> None:
    _version, _fingerprint, status, assertions, timings = run_memory_study()
    assert status is StudyStatus.PASS
    assert all(assertions.values())
    assert all(value >= 0 for value in timings.values())


def test_retained_memory_fixture_is_sanitized_and_passing() -> None:
    fixture = (
        ROOT
        / "spikes"
        / "fixtures"
        / "thread-workstream"
        / "memory"
        / "external-continuity.json"
    )
    retained = json.loads(fixture.read_text())
    assert retained["status"] == "pass"
    assert all(retained["assertions"].values())
    assert all(retained["isolation"].values())
    assert all(retained["cleanup"].values())
    encoded = fixture.read_text()
    assert "/home/" not in encoded
    assert "/tmp/" not in encoded
    assert "bounded accepted plan" not in encoded
    assert "memory_content" not in encoded


def test_all_retained_thread_workstream_evidence_passes_privacy_audit() -> None:
    fixture_root = ROOT / "spikes" / "fixtures" / "thread-workstream"
    fixtures = sorted(fixture_root.rglob("*.json"))
    assert len(fixtures) == 4
    for fixture in fixtures:
        retained = json.loads(fixture.read_text())
        audit_sanitized_evidence(retained)
        assert retained["status"] == "pass"
        assert retained["assisted"] is False
        assert all(retained["privacyAudit"].values())
