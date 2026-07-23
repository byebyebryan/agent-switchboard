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
from spikes.thread_workstream.codex_app_server import latest_agent_message, latest_plan
from spikes.thread_workstream.evidence import (
    EvidenceError,
    StudyResult,
    StudyStatus,
    assert_private_file,
    audit_sanitized_evidence,
    sanitize_hook_order,
    write_private_json,
)
from spikes.thread_workstream.execution_trigger import (
    AtomicCutoverStore,
    CutoverBinding,
    CutoverState,
    CutoverTransaction,
    DeliveryLedger,
    ExecutionSignal,
    ExecutionTriggerGate,
    PlanCandidate,
    PlanProvenance,
    TriggerDecision,
    TriggerError,
    TriggerObservation,
    classify_execution_trigger,
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
from spikes.thread_workstream.thread_actions import (
    ActionObservation,
    ActionRoute,
    ActionSurface,
    ThreadAction,
    ThreadActionError,
    conversational_action,
    decide_thread_action,
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


def test_explicit_thread_action_policy_preserves_user_choice() -> None:
    implement_here = decide_thread_action(
        ActionObservation(
            surface=ActionSurface.NATIVE_PLAN,
            action=ThreadAction.IMPLEMENT_HERE,
            active_turn=False,
            has_completed_turn=True,
            has_exact_filesystem_checkpoint=True,
            has_transfer_artifact=True,
        )
    )
    clear_and_implement = decide_thread_action(
        ActionObservation(
            surface=ActionSurface.NATIVE_PLAN,
            action=ThreadAction.CLEAR_AND_IMPLEMENT,
            active_turn=False,
            has_completed_turn=True,
            has_exact_filesystem_checkpoint=True,
            has_transfer_artifact=True,
        )
    )
    assert implement_here.route is ActionRoute.STAY
    assert clear_and_implement.route is ActionRoute.THREAD_CUTOVER
    assert not clear_and_implement.creates_worktree


def test_navigator_can_start_or_fork_work_while_source_turn_is_active() -> None:
    new_workstream = decide_thread_action(
        ActionObservation(
            surface=ActionSurface.NAVIGATOR,
            action=ThreadAction.START_WORKSTREAM,
            active_turn=True,
            has_completed_turn=True,
            has_exact_filesystem_checkpoint=True,
            has_transfer_artifact=False,
        )
    )
    fork = decide_thread_action(
        ActionObservation(
            surface=ActionSurface.NAVIGATOR,
            action=ThreadAction.FORK_WORKSTREAM,
            active_turn=True,
            has_completed_turn=True,
            has_exact_filesystem_checkpoint=True,
            has_transfer_artifact=False,
        )
    )
    assert new_workstream.route is ActionRoute.NEW_WORKSTREAM
    assert new_workstream.preserves_source_turn
    assert new_workstream.creates_worktree
    assert fork.route is ActionRoute.FORK_WORKSTREAM
    assert fork.preserves_source_turn
    assert fork.branches_from_last_completed_turn
    assert fork.creates_worktree
    assert not fork.requires_prompt_boundary


def test_interrupt_is_distinct_from_fork_and_does_not_require_prompt_boundary() -> None:
    decision = decide_thread_action(
        ActionObservation(
            surface=ActionSurface.NAVIGATOR,
            action=ThreadAction.INTERRUPT,
            active_turn=True,
            has_completed_turn=True,
            has_exact_filesystem_checkpoint=True,
            has_transfer_artifact=False,
        )
    )
    assert decision.route is ActionRoute.INTERRUPT
    assert not decision.preserves_source_turn
    assert not decision.requires_prompt_boundary
    with pytest.raises(ThreadActionError):
        decide_thread_action(
            ActionObservation(
                surface=ActionSurface.NAVIGATOR,
                action=ThreadAction.INTERRUPT,
                active_turn=False,
                has_completed_turn=True,
                has_exact_filesystem_checkpoint=True,
                has_transfer_artifact=False,
            )
        )

    with pytest.raises(ThreadActionError):
        decide_thread_action(
            ActionObservation(
                surface=ActionSurface.NAVIGATOR,
                action=ThreadAction.START_THREAD,
                active_turn=True,
                has_completed_turn=True,
                has_exact_filesystem_checkpoint=True,
                has_transfer_artifact=False,
            )
        )


def test_conversational_thread_actions_are_exact_and_prompt_bound() -> None:
    assert (
        conversational_action("  Go ahead   in a new thread.  ")
        is ThreadAction.CLEAR_AND_IMPLEMENT
    )
    assert conversational_action("fork this task") is ThreadAction.FORK_WORKSTREAM
    assert conversational_action("start a new thread") is ThreadAction.START_THREAD
    assert (
        conversational_action("start a new workstream") is ThreadAction.START_WORKSTREAM
    )
    assert conversational_action("maybe fork this task please") is None
    assert conversational_action("sounds good") is None
    with pytest.raises(ThreadActionError):
        decide_thread_action(
            ActionObservation(
                surface=ActionSurface.CONVERSATION,
                action=ThreadAction.FORK_WORKSTREAM,
                active_turn=True,
                has_completed_turn=True,
                has_exact_filesystem_checkpoint=True,
                has_transfer_artifact=True,
            )
        )


def test_fork_requires_settled_provider_and_filesystem_boundaries() -> None:
    for overrides in (
        {"has_completed_turn": False},
        {"has_exact_filesystem_checkpoint": False},
    ):
        values = {
            "surface": ActionSurface.NAVIGATOR,
            "action": ThreadAction.FORK_WORKSTREAM,
            "active_turn": True,
            "has_completed_turn": True,
            "has_exact_filesystem_checkpoint": True,
            "has_transfer_artifact": False,
        }
        values.update(overrides)
        with pytest.raises(ThreadActionError):
            decide_thread_action(ActionObservation(**values))

    with pytest.raises(ThreadActionError):
        decide_thread_action(
            ActionObservation(
                surface=ActionSurface.NATIVE_PLAN,
                action=ThreadAction.FORK_WORKSTREAM,
                active_turn=False,
                has_completed_turn=True,
                has_exact_filesystem_checkpoint=True,
                has_transfer_artifact=False,
            )
        )

    with pytest.raises(ThreadActionError):
        decide_thread_action(
            ActionObservation(
                surface=ActionSurface.CONVERSATION,
                action=ThreadAction.CLEAR_AND_IMPLEMENT,
                active_turn=False,
                has_completed_turn=True,
                has_exact_filesystem_checkpoint=True,
                has_transfer_artifact=False,
            )
        )


def test_retained_running_fork_fixture_is_sanitized_and_passing() -> None:
    fixture = (
        ROOT
        / "spikes"
        / "fixtures"
        / "thread-workstream"
        / "codex"
        / "0.145.0"
        / "running-source-fork.json"
    )
    retained = json.loads(fixture.read_text())
    audit_sanitized_evidence(retained)
    assert retained["status"] == "pass"
    assert retained["assisted"] is False
    assert all(retained["assertions"].values())
    assert all(retained["isolation"].values())
    assert all(retained["cleanup"].values())
    encoded = fixture.read_text()
    assert "/home/" not in encoded
    assert "/tmp/" not in encoded
    assert "threadId" not in encoded
    assert "provider_input" not in encoded


def test_retained_navigator_fork_fixture_is_sanitized_and_passing() -> None:
    fixture = (
        ROOT
        / "spikes"
        / "fixtures"
        / "thread-workstream"
        / "codex"
        / "0.145.0"
        / "navigator-running-fork.json"
    )
    retained = json.loads(fixture.read_text())
    audit_sanitized_evidence(retained)
    assert retained["status"] == "pass"
    assert retained["assisted"] is False
    assert all(retained["assertions"].values())
    assert all(retained["isolation"].values())
    assert all(retained["cleanup"].values())
    encoded = fixture.read_text()
    assert "/home/" not in encoded
    assert "/tmp/" not in encoded
    assert "threadId" not in encoded
    assert "provider_input" not in encoded


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


def test_latest_agent_message_uses_last_completed_message() -> None:
    assert (
        latest_agent_message(
            {
                "turns": [
                    {"items": [{"type": "agentMessage", "text": "first"}]},
                    {"items": [{"type": "plan", "text": "not a message"}]},
                    {"items": [{"type": "agentMessage", "text": "authoritative"}]},
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
    assert len(fixtures) >= 4
    for fixture in fixtures:
        retained = json.loads(fixture.read_text())
        audit_sanitized_evidence(retained)
        assert retained["status"] in {"pass", "falsified", "blocked"}
        assert retained["assisted"] is False
        assert all(retained["privacyAudit"].values())


def test_trigger_hook_holds_matching_input_until_private_block_decision(
    tmp_path: Path,
) -> None:
    events = tmp_path / "private" / "events.jsonl"
    decision = tmp_path / "private" / "decision.json"
    write_private_json(decision, {"decision": "block"})
    environment = dict(os.environ)
    environment["ASB_SPIKE_DISPOSABLE_ROOT"] = str(tmp_path)
    payload = {
        "session_id": "11111111-1111-4111-8111-111111111111",
        "hook_event_name": "UserPromptSubmit",
        "turn_id": "private-turn",
        "prompt": "Implement the plan.",
        "cwd": str(tmp_path),
        "permission_mode": "bypassPermissions",
    }
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "spikes" / "thread_workstream" / "trigger_hook.py"),
            str(events),
            str(decision),
            "ordinary",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
        env=environment,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "block"
    assert_private_file(events)
    retained = json.loads(events.read_text())
    assert retained["trigger_match"] is True
    assert retained["provider_input"] == "Implement the plan."


def test_retained_execution_trigger_fixture_is_sanitized_and_passing() -> None:
    fixture = (
        ROOT
        / "spikes"
        / "fixtures"
        / "thread-workstream"
        / "codex"
        / "0.145.0"
        / "execution-trigger.json"
    )
    retained = json.loads(fixture.read_text())
    assert retained["status"] == "pass"
    assert retained["assisted"] is False
    assert all(retained["assertions"].values())
    assert all(retained["isolation"].values())
    assert all(retained["cleanup"].values())
    assert retained["assertions"]["ordinary_trigger_authoritative"] is True
    assert retained["assertions"]["natural_language_is_advisory_only"] is True
    assert retained["assertions"]["explicit_selection_authorizes_cutover"] is True
    encoded = fixture.read_text()
    assert "/home/" not in encoded
    assert "/tmp/" not in encoded
    assert "provider_input" not in encoded
    assert "provider_output" not in encoded


PLAN_DIGEST = "b" * 64


def structured_candidate(**overrides: object) -> PlanCandidate:
    values: dict[str, object] = {
        "source_identity": "source-private",
        "source_revision": 4,
        "digest": PLAN_DIGEST,
        "provenance": PlanProvenance.CODEX_PLAN_ITEM,
        "accepted": True,
    }
    values.update(overrides)
    return PlanCandidate(**values)  # type: ignore[arg-type]


def ordinary_observation(**overrides: object) -> TriggerObservation:
    values: dict[str, object] = {
        "nonce": "trigger-1",
        "order": 1,
        "source_identity": "source-private",
        "source_revision": 4,
        "signal": ExecutionSignal.CODEX_ORDINARY_IMPLEMENT,
        "before_source_commit": True,
        "source_sampled": False,
        "referenced_plan_digest": PLAN_DIGEST,
        "ordinary_coding_input": True,
        "mode_before": "plan",
        "mode_at_submit": "default",
    }
    values.update(overrides)
    return TriggerObservation(**values)  # type: ignore[arg-type]


def test_structured_ordinary_implementation_authorizes_pre_submit_cutover() -> None:
    receipt = classify_execution_trigger(
        structured_candidate(),
        ordinary_observation(),
    )
    assert receipt.decision is TriggerDecision.CUTOVER
    assert receipt.authoritative is True
    assert receipt.reason == "structured-plan-implementation"


@pytest.mark.parametrize(
    ("candidate", "observation"),
    [
        (None, ordinary_observation()),
        (
            structured_candidate(provenance=PlanProvenance.EXPLICIT_SELECTION),
            ordinary_observation(),
        ),
        (structured_candidate(accepted=False), ordinary_observation()),
        (structured_candidate(consumed=True), ordinary_observation()),
        (
            structured_candidate(source_identity="other"),
            ordinary_observation(),
        ),
        (structured_candidate(source_revision=3), ordinary_observation()),
        (
            structured_candidate(),
            ordinary_observation(referenced_plan_digest="c" * 64),
        ),
        (
            structured_candidate(),
            ordinary_observation(ordinary_coding_input=False),
        ),
        (
            structured_candidate(),
            ordinary_observation(before_source_commit=False),
        ),
        (structured_candidate(), ordinary_observation(source_sampled=True)),
    ],
)
def test_ordinary_implementation_fails_closed_without_compound_authority(
    candidate: PlanCandidate | None,
    observation: TriggerObservation,
) -> None:
    receipt = classify_execution_trigger(candidate, observation)
    assert receipt.decision is TriggerDecision.STAY
    assert receipt.authoritative is False


def test_permission_mode_is_not_required_as_collaboration_mode_evidence() -> None:
    receipt = classify_execution_trigger(
        structured_candidate(),
        ordinary_observation(
            mode_before=None,
            mode_at_submit="bypassPermissions",
        ),
    )
    assert receipt.decision is TriggerDecision.CUTOVER
    assert receipt.authoritative is True


def test_selected_conversational_plan_requires_explicit_fresh_action() -> None:
    selected = structured_candidate(
        provenance=PlanProvenance.EXPLICIT_SELECTION,
    )
    natural = ordinary_observation(
        signal=ExecutionSignal.NATURAL_LANGUAGE_ACCEPTANCE,
        ordinary_coding_input=False,
        mode_before="default",
        mode_at_submit="default",
    )
    advisory = classify_execution_trigger(selected, natural)
    assert advisory.decision is TriggerDecision.ADVISORY
    assert advisory.authoritative is False

    explicit = replace(
        natural,
        signal=ExecutionSignal.EXPLICIT_FRESH_IMPLEMENT,
    )
    receipt = classify_execution_trigger(selected, explicit)
    assert receipt.decision is TriggerDecision.CUTOVER
    assert receipt.authoritative is True
    assert receipt.reason == "explicit-plan-selection"


def test_discussion_revision_and_generic_clear_do_not_create_task_boundary() -> None:
    for signal in (ExecutionSignal.DISCUSSION, ExecutionSignal.PLAN_REVISION):
        receipt = classify_execution_trigger(
            structured_candidate(),
            ordinary_observation(signal=signal),
        )
        assert receipt.decision is TriggerDecision.STAY
        assert receipt.authoritative is False

    generic = classify_execution_trigger(
        structured_candidate(),
        ordinary_observation(signal=ExecutionSignal.GENERIC_CLEAR),
    )
    assert generic.decision is TriggerDecision.PROVIDER_ONLY
    assert generic.authoritative is False


def test_native_clear_requires_exact_structured_plan_for_task_adoption() -> None:
    observation = ordinary_observation(
        signal=ExecutionSignal.NATIVE_CLEAR_IMPLEMENT,
    )
    accepted = classify_execution_trigger(structured_candidate(), observation)
    assert accepted.decision is TriggerDecision.NATIVE_ADOPT
    assert accepted.authoritative is True

    unproven = classify_execution_trigger(None, observation)
    assert unproven.decision is TriggerDecision.PROVIDER_ONLY
    assert unproven.authoritative is False


def test_trigger_gate_rejects_replay_stale_and_concurrent_authority() -> None:
    gate = ExecutionTriggerGate()
    first = ordinary_observation()
    assert gate.observe(structured_candidate(), first).authoritative is True
    with pytest.raises(TriggerError, match="trigger-replayed"):
        gate.observe(structured_candidate(), first)
    with pytest.raises(TriggerError, match="trigger-stale"):
        gate.observe(
            structured_candidate(),
            ordinary_observation(nonce="stale", order=0),
        )
    with pytest.raises(TriggerError, match="trigger-concurrent"):
        gate.observe(
            structured_candidate(),
            ordinary_observation(nonce="concurrent", order=2),
        )
    with pytest.raises(TriggerError, match="trigger-settlement-mismatch"):
        gate.settle("wrong")
    gate.settle(first.nonce)
    assert (
        gate.observe(
            structured_candidate(),
            ordinary_observation(nonce="next", order=3),
        ).authoritative
        is True
    )


def prepared_transaction() -> tuple[
    PlanCandidate,
    TriggerObservation,
    AtomicCutoverStore,
    CutoverTransaction,
]:
    candidate = structured_candidate()
    observation = ordinary_observation()
    receipt = classify_execution_trigger(candidate, observation)
    store = AtomicCutoverStore(
        CutoverBinding(version=7, active_identity=observation.source_identity)
    )
    transaction = CutoverTransaction.prepare(
        candidate,
        observation,
        receipt,
        store,
    )
    return candidate, observation, store, transaction


def test_cutover_transaction_delivers_once_then_commits_and_consumes_plan() -> None:
    candidate, _observation, store, transaction = prepared_transaction()
    ledger = DeliveryLedger()
    transaction.set_destination("destination-private")
    transaction.deliver(ledger)
    transaction.commit(store)
    consumed = transaction.consume_candidate(candidate)

    assert transaction.state is CutoverState.COMMITTED
    assert ledger.attempts == ledger.deliveries == 1
    assert store.binding == CutoverBinding(
        version=8,
        active_identity="destination-private",
        plan_consumed=True,
    )
    assert consumed.consumed is True
    assert transaction.source_input_restored is False


def test_pre_delivery_failure_restores_source_input_without_delivery() -> None:
    _candidate, _observation, store, transaction = prepared_transaction()
    ledger = DeliveryLedger()
    transaction.set_destination("destination-private")
    with pytest.raises(TriggerError, match="delivery-rejected"):
        transaction.deliver(ledger, fail_at="before")
    transaction.restore_source()

    assert transaction.state is CutoverState.RESTORED
    assert transaction.source_input_restored is True
    assert ledger.deliveries == 0
    assert store.binding.active_identity == "source-private"


def test_uncertain_delivery_recovers_without_duplicate_source_or_destination() -> None:
    _candidate, _observation, store, transaction = prepared_transaction()
    ledger = DeliveryLedger()
    transaction.set_destination("destination-private")
    with pytest.raises(TriggerError, match="delivery-outcome-uncertain"):
        transaction.deliver(ledger, fail_at="after")
    with pytest.raises(
        TriggerError,
        match="delivered-input-cannot-return-to-source",
    ):
        transaction.restore_source()

    transaction.recover_delivery(ledger)
    transaction.commit(store)

    assert transaction.state is CutoverState.COMMITTED
    assert ledger.attempts == 1
    assert ledger.deliveries == 1
    assert transaction.source_input_restored is False


def test_uncertain_binding_commit_recovers_complete_destination_record() -> None:
    _candidate, _observation, store, transaction = prepared_transaction()
    ledger = DeliveryLedger()
    transaction.set_destination("destination-private")
    transaction.deliver(ledger)
    with pytest.raises(TriggerError, match="binding-commit-outcome-uncertain"):
        transaction.commit(store, fail_at="after")
    assert transaction.state is CutoverState.COMMIT_UNCERTAIN

    transaction.recover_commit(store)
    assert transaction.state is CutoverState.COMMITTED
    assert store.binding.active_identity == "destination-private"
    assert ledger.deliveries == 1
