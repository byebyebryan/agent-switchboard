from __future__ import annotations

from uuid import UUID

import pytest

from agent_switchboard._v3.domain import (
    CONTROL_EDGES,
    TRANSITION_EDGES,
    Activity,
    ActivityReason,
    BackgroundState,
    BriefId,
    CheckoutId,
    ClaimState,
    ControlState,
    FrameId,
    FrameLifecycleState,
    FrameRole,
    GenerationId,
    HostId,
    Project,
    ProjectId,
    ProviderId,
    ProviderSession,
    RequestId,
    Resumability,
    RuntimePresence,
    SessionKey,
    StateTransitionError,
    TaskPushPolicy,
    TransitionBrief,
    TransitionId,
    TransitionState,
    ValidationError,
    WorkContext,
    WorkContextId,
    content_hash,
    request_fingerprint,
    require_state_edge,
)

HOST = HostId("11111111-1111-4111-8111-111111111111")
PROJECT = ProjectId("22222222-2222-4222-8222-222222222222")
FRAME = FrameId("33333333-3333-4333-8333-333333333333")
SESSION_UUID = UUID("44444444-4444-4444-8444-444444444444")
SESSION = SessionKey(HOST, ProviderId.CODEX, SESSION_UUID)


def test_identifiers_and_session_keys_are_canonical_and_non_nil() -> None:
    assert str(GenerationId("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")) == (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    assert SessionKey.parse(str(SESSION)) == SESSION
    with pytest.raises(ValidationError, match="nil"):
        HostId("00000000-0000-0000-0000-000000000000")
    with pytest.raises(ValidationError, match="session key"):
        SessionKey.parse("not:a:session:key")


def test_project_aliases_and_semantic_fingerprint_are_normalized() -> None:
    project = Project(
        PROJECT,
        "Switchboard",
        (" Agent   Router ", "agent router", "Sessions"),
        task_push=TaskPushPolicy.CONSERVATIVE,
    )
    assert project.aliases == ("Agent Router", "Sessions")
    first = request_fingerprint(
        "view.open",
        {"hostId": str(HOST), "projectId": str(PROJECT)},
    )
    second = request_fingerprint(
        "view.open",
        {"projectId": str(PROJECT), "hostId": str(HOST)},
    )
    assert first == second
    with pytest.raises(ValidationError, match="presentation capability"):
        request_fingerprint(
            "view.open",
            {"projectId": str(PROJECT), "canFocusDesktop": True},
        )


def test_semantic_content_is_immutable_and_hash_checked() -> None:
    brief_text = "Implement the isolated registry.\nKeep 0.2 untouched."
    brief = TransitionBrief(
        BriefId("55555555-5555-4555-8555-555555555555"),
        TransitionId("66666666-6666-4666-8666-666666666666"),
        FRAME,
        SESSION,
        FrameId("77777777-7777-4777-8777-777777777777"),
        brief_text,
        content_hash(brief_text),
        10,
        None,
    )
    assert brief.brief == brief_text
    with pytest.raises(ValidationError, match="hash mismatch"):
        TransitionBrief(
            brief.brief_id,
            brief.transition_id,
            brief.source_frame_id,
            brief.source_session_key,
            brief.target_frame_id,
            brief.brief,
            "a" * 64,
            10,
            None,
        )


def test_provider_session_and_work_context_validate_cross_fields() -> None:
    with pytest.raises(ValidationError, match="identity"):
        ProviderSession(
            SESSION,
            HOST,
            ProviderId.CLAUDE,
            SESSION_UUID,
            PROJECT,
            None,
            None,
            None,
            False,
            RuntimePresence.UNKNOWN,
            Resumability.UNKNOWN,
            Activity.UNKNOWN,
            ActivityReason.UNKNOWN,
            None,
            None,
            10,
            10,
        )
    with pytest.raises(ValidationError, match="foreground"):
        WorkContext(
            WorkContextId("88888888-8888-4888-8888-888888888888"),
            HOST,
            PROJECT,
            CheckoutId("99999999-9999-4999-8999-999999999999"),
            ClaimState.HELD,
            1,
            None,
            BackgroundState.SAFE,
            10,
            None,
            10,
        )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TransitionState.PREPARED, TransitionState.EXECUTING),
        (TransitionState.EXECUTING, TransitionState.PRESENTED),
        (TransitionState.PRESENTED, TransitionState.AWAITING_CLAIM),
        (TransitionState.AWAITING_CLAIM, TransitionState.SETTLING),
        (TransitionState.SETTLING, TransitionState.COMPLETED),
    ],
)
def test_transition_state_edges_accept_only_normative_sequence(
    current: TransitionState,
    target: TransitionState,
) -> None:
    require_state_edge(current, target, TRANSITION_EDGES, "transition")


def test_terminal_and_skipped_state_edges_are_rejected() -> None:
    with pytest.raises(StateTransitionError, match="illegal transition"):
        require_state_edge(
            TransitionState.PREPARED,
            TransitionState.COMPLETED,
            TRANSITION_EDGES,
            "transition",
        )
    with pytest.raises(StateTransitionError, match="illegal control"):
        require_state_edge(
            ControlState.SUBMITTED,
            ControlState.SETTLED,
            CONTROL_EDGES,
            "control",
        )


def test_frame_role_and_lifecycle_contract_remains_explicit() -> None:
    assert FrameRole.WORKSPACE.value == "workspace"
    assert FrameLifecycleState.CLOSING.value == "closing"
    assert str(RequestId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
