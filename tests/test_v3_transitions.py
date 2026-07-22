from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from agent_switchboard._v3.domain import (
    Activity,
    ActivityReason,
    AgentCapability,
    BriefId,
    CapabilityId,
    Checkout,
    CheckoutId,
    CheckoutKind,
    CloseReason,
    CompletionHandoff,
    ControlKind,
    ControlState,
    ControlTransport,
    ControlTurn,
    ControlTurnId,
    CreatedBy,
    DesktopAttachmentLease,
    Frame,
    FrameId,
    FrameLifecycleState,
    FramePlacement,
    FrameRole,
    FrameSession,
    FrameSessionId,
    GenerationId,
    HandoffId,
    HostId,
    LaunchAction,
    LaunchId,
    LaunchIntent,
    LaunchState,
    LeaseId,
    LeaseState,
    MembershipReason,
    PlacementId,
    PlacementState,
    Project,
    ProjectId,
    ProjectRepository,
    ProviderId,
    ProviderSession,
    Recovery,
    RecoveryActionability,
    RecoveryId,
    RecoveryState,
    Repository,
    RepositoryId,
    RepositoryKind,
    RequestId,
    RequestState,
    Resumability,
    RuntimePresence,
    SessionKey,
    StateTransitionError,
    Surface,
    SurfaceId,
    SurfaceState,
    TmuxServer,
    TmuxServerId,
    TransitionBrief,
    TransitionId,
    TransitionKind,
    TransitionState,
    TransportPhase,
    UserView,
    ViewId,
    ViewMode,
    ViewState,
    ViewTransition,
    WorkContextId,
    content_hash,
    request_fingerprint,
)
from agent_switchboard._v3.protocol import build_host_state
from agent_switchboard._v3.storage import ConflictError, Registry


def ident[IdT](cls: type[IdT], number: int) -> IdT:
    return cls(UUID(f"00000000-0000-4000-8000-{number:012x}"))


GENERATION = ident(GenerationId, 1)
HOST = ident(HostId, 2)
PROJECT = ident(ProjectId, 3)
REPOSITORY = ident(RepositoryId, 4)
CHECKOUT = ident(CheckoutId, 5)
CONTEXT = ident(WorkContextId, 6)
WORKSPACE = ident(FrameId, 7)
TASK = ident(FrameId, 8)
VIEW = ident(ViewId, 9)
WORKSPACE_PLACEMENT = ident(PlacementId, 10)
TASK_PLACEMENT = ident(PlacementId, 11)
TMUX = ident(TmuxServerId, 12)
PARENT_SESSION_UUID = UUID("00000000-0000-4000-8000-000000000013")
CHILD_SESSION_UUID = UUID("00000000-0000-4000-8000-000000000014")
PARENT_SESSION = SessionKey(HOST, ProviderId.CODEX, PARENT_SESSION_UUID)
CHILD_SESSION = SessionKey(HOST, ProviderId.CODEX, CHILD_SESSION_UUID)
PARENT_LAUNCH = ident(LaunchId, 15)
PARENT_SURFACE = ident(SurfaceId, 16)
CHILD_LAUNCH = ident(LaunchId, 17)
CHILD_SURFACE = ident(SurfaceId, 18)
PUSH = ident(TransitionId, 19)
PUSH_REQUEST = ident(RequestId, 20)
PUSH_BRIEF = ident(BriefId, 21)
PUSH_CONTROL = ident(ControlTurnId, 22)
COMPLETE = ident(TransitionId, 23)
COMPLETE_REQUEST = ident(RequestId, 24)
COMPLETE_HANDOFF = ident(HandoffId, 25)
COMPLETE_CONTROL = ident(ControlTurnId, 26)
PARENT_CAPABILITY = ident(CapabilityId, 27)
CHILD_CAPABILITY = ident(CapabilityId, 28)
PARENT_TOKEN = "parent-secret"
CHILD_TOKEN = "child-secret"


def registry() -> Registry:
    return Registry(
        ":memory:",
        generation_id=GENERATION,
        local_host_id=HOST,
        local_display_name="starship",
        now=10,
    )


def seed_catalog_workspace(opened: Registry) -> None:
    opened.materialize_catalog(
        HOST,
        [Project(PROJECT, "Switchboard")],
        [Repository(REPOSITORY, "switchboard", RepositoryKind.GIT)],
        [ProjectRepository(PROJECT, REPOSITORY, True)],
        [
            Checkout(
                CHECKOUT,
                REPOSITORY,
                HOST,
                Path("/home/bryan/code/agent-switchboard"),
                CheckoutKind.MAIN,
                is_default=True,
            )
        ],
        now=20,
    )
    opened.ensure_workspace(
        CONTEXT, WORKSPACE, HOST, PROJECT, CHECKOUT, "Switchboard", now=21
    )
    opened.acquire_work_context(CONTEXT, 0, WORKSPACE, now=22)
    opened.record_tmux_server(
        TmuxServer(TMUX, HOST, "/tmp/tmux-1000/default", 1234, 20, 23)
    )


def build_runtime(
    opened: Registry,
    *,
    frame_id: FrameId,
    session_uuid: UUID,
    launch_id: LaunchId,
    surface_id: SurfaceId,
    pane_id: str,
    base: int,
) -> SessionKey:
    session_key = SessionKey(HOST, ProviderId.CODEX, session_uuid)
    opened.upsert_provider_session(
        ProviderSession(
            session_key,
            HOST,
            ProviderId.CODEX,
            session_uuid,
            PROJECT,
            CHECKOUT,
            None,
            None,
            False,
            RuntimePresence.LIVE,
            Resumability.RESUMABLE,
            Activity.READY,
            ActivityReason.TURN_COMPLETE,
            base,
            base,
            base,
            base,
        )
    )
    opened.append_frame_session(
        FrameSession(
            ident(FrameSessionId, base),
            frame_id,
            session_key,
            1,
            MembershipReason.STARTED,
            base,
        )
    )
    opened.plan_launch(
        LaunchIntent(
            launch_id,
            ident(RequestId, base + 1),
            HOST,
            frame_id,
            ProviderId.CODEX,
            LaunchAction.NEW,
            None,
            LaunchState.PLANNED,
            None,
            base,
            base,
        ),
        Surface(
            surface_id,
            HOST,
            ProviderId.CODEX,
            None,
            launch_id,
            SurfaceState.PLANNED,
            None,
            None,
            None,
            None,
            0,
            base,
            base,
            None,
        ),
    )
    opened.advance_launch(
        launch_id, LaunchState.PLANNED, LaunchState.AUTHORIZED, now=base + 1
    )
    opened.advance_launch(
        launch_id, LaunchState.AUTHORIZED, LaunchState.STARTED, now=base + 2
    )
    opened.publish_surface(surface_id, 0, TMUX, pane_id, process_id=base, now=base + 3)
    opened.bind_surface_session(surface_id, 1, session_key, now=base + 4)
    return session_key


def seed_two_frame_runtime(opened: Registry) -> None:
    seed_catalog_workspace(opened)
    build_runtime(
        opened,
        frame_id=WORKSPACE,
        session_uuid=PARENT_SESSION_UUID,
        launch_id=PARENT_LAUNCH,
        surface_id=PARENT_SURFACE,
        pane_id="%1",
        base=100,
    )
    opened.create_view(
        UserView(
            VIEW,
            HOST,
            ViewMode.NAVIGATOR,
            WORKSPACE,
            ViewState.READY,
            0,
            "desktop-1",
            TMUX,
            110,
            None,
            110,
        ),
        FramePlacement(
            WORKSPACE_PLACEMENT,
            HOST,
            VIEW,
            WORKSPACE,
            PARENT_SURFACE,
            PlacementState.ACTIVE,
            0,
            110,
            110,
        ),
    )
    opened.issue_capability(
        AgentCapability(
            PARENT_CAPABILITY,
            sha256(PARENT_TOKEN.encode()).hexdigest(),
            HOST,
            VIEW,
            WORKSPACE,
            PARENT_SESSION,
            PARENT_SURFACE,
            PARENT_LAUNCH,
            TMUX,
            "%1",
            0,
            111,
            1_000,
            None,
        )
    )
    opened.create_task(
        Frame(
            TASK,
            HOST,
            PROJECT,
            FrameRole.TASK,
            WORKSPACE,
            CONTEXT,
            "Phase 6 storage",
            "Implement the transition core",
            ProviderId.CODEX,
            FrameLifecycleState.OPEN,
            None,
            None,
            CreatedBy.USER,
            112,
            112,
        ),
        FramePlacement(
            TASK_PLACEMENT,
            HOST,
            VIEW,
            TASK,
            None,
            PlacementState.STAGED,
            0,
            None,
            112,
        ),
    )
    build_runtime(
        opened,
        frame_id=TASK,
        session_uuid=CHILD_SESSION_UUID,
        launch_id=CHILD_LAUNCH,
        surface_id=CHILD_SURFACE,
        pane_id="%2",
        base=120,
    )
    opened.attach_surface_to_placement(TASK_PLACEMENT, 0, CHILD_SURFACE, now=125)


def transition(
    *,
    transition_id: TransitionId,
    request_id: RequestId,
    kind: TransitionKind,
    source: FrameId,
    target: FrameId,
    view_revision: int,
    claim_generation: int,
    now: int,
) -> ViewTransition:
    fingerprint = request_fingerprint(
        f"transition.{kind.value}",
        {
            "viewId": str(VIEW),
            "sourceFrameId": str(source),
            "targetFrameId": str(target),
        },
    )
    return ViewTransition(
        transition_id,
        request_id,
        fingerprint,
        HOST,
        VIEW,
        kind,
        source,
        target,
        CONTEXT,
        view_revision,
        claim_generation,
        TransitionState.PREPARED,
        None,
        None,
        TransportPhase.INTENT,
        None,
        now,
        now,
    )


def execute_presentation(
    opened: Registry, transition_id: TransitionId, owner: str, base: int
) -> None:
    opened.claim_transition_execution(transition_id, owner, base + 20, now=base)
    opened.advance_transport_phase(
        transition_id,
        owner,
        TransportPhase.INTENT,
        TransportPhase.MOVED,
        now=base + 1,
    )
    opened.advance_transport_phase(
        transition_id,
        owner,
        TransportPhase.MOVED,
        TransportPhase.INSPECTED,
        now=base + 2,
    )
    opened.commit_transition_presentation(transition_id, owner, now=base + 3)
    opened.advance_transition_state(
        transition_id,
        TransitionState.PRESENTED,
        TransitionState.AWAITING_CLAIM,
        execution_owner=owner,
        now=base + 4,
    )


def prepare_push(opened: Registry) -> None:
    record = transition(
        transition_id=PUSH,
        request_id=PUSH_REQUEST,
        kind=TransitionKind.PUSH,
        source=WORKSPACE,
        target=TASK,
        view_revision=0,
        claim_generation=1,
        now=130,
    )
    assert opened.prepare_transition(record) == record
    assert opened.prepare_transition(record) == record
    brief_text = "Continue the Phase 6 storage implementation."
    brief = TransitionBrief(
        PUSH_BRIEF,
        PUSH,
        WORKSPACE,
        PARENT_SESSION,
        TASK,
        brief_text,
        content_hash(brief_text),
        131,
        None,
    )
    opened.store_transition_brief(brief)
    opened.store_transition_brief(brief)
    opened.prepare_control_turn(
        ControlTurn(
            PUSH_CONTROL,
            PUSH,
            TASK,
            CHILD_SESSION,
            ControlKind.CLAIM_BRIEF,
            "control.claim.v1",
            ControlTransport.LIVE_INPUT,
            ControlState.PREPARED,
            0,
            None,
            None,
            None,
            None,
            None,
        )
    )


def finish_push(opened: Registry) -> None:
    prepare_push(opened)
    execute_presentation(opened, PUSH, "worker-1", 140)
    opened.advance_control_turn(
        PUSH_CONTROL,
        ControlState.PREPARED,
        ControlState.SUBMITTED,
        now=145,
    )
    opened.advance_control_turn(
        PUSH_CONTROL,
        ControlState.SUBMITTED,
        ControlState.OBSERVED,
        observed_prompt_id="prompt-1",
        now=146,
    )
    child_generation = opened.get_placement(TASK_PLACEMENT).generation
    opened.issue_capability(
        AgentCapability(
            CHILD_CAPABILITY,
            sha256(CHILD_TOKEN.encode()).hexdigest(),
            HOST,
            VIEW,
            TASK,
            CHILD_SESSION,
            CHILD_SURFACE,
            CHILD_LAUNCH,
            TMUX,
            "%2",
            child_generation,
            147,
            1_000,
            None,
        )
    )
    claimed = opened.transition_claim(PUSH, CHILD_SESSION, CHILD_TOKEN, now=148)
    assert claimed.kind == "brief"
    assert claimed.brief == "Continue the Phase 6 storage implementation."
    assert opened.transition_claim(PUSH, CHILD_SESSION, CHILD_TOKEN, now=149) == claimed
    opened.settle_transition_claim(PUSH, now=150)


def test_request_idempotency_rejects_semantic_uuid_reuse() -> None:
    with registry() as opened:
        fingerprint = request_fingerprint("view.open", {"projectId": str(PROJECT)})
        first = opened.begin_request(
            HOST, PUSH_REQUEST, "view.open", fingerprint, now=20
        )
        assert (
            opened.begin_request(HOST, PUSH_REQUEST, "view.open", fingerprint, now=21)
            == first
        )
        with pytest.raises(ConflictError) as caught:
            opened.begin_request(
                HOST,
                PUSH_REQUEST,
                "view.open",
                request_fingerprint("view.open", {"projectId": "different"}),
                now=22,
            )
        assert caught.value.code == "request_reuse"
        completed = opened.settle_request(
            HOST,
            PUSH_REQUEST,
            RequestState.COMPLETED,
            result_type="view",
            result_id=str(VIEW),
            now=23,
        )
        assert completed.state is RequestState.COMPLETED
        assert (
            opened.settle_request(
                HOST,
                PUSH_REQUEST,
                RequestState.COMPLETED,
                result_type="view",
                result_id=str(VIEW),
                now=24,
            )
            == completed
        )


def test_push_transition_claim_is_exact_atomic_and_idempotent() -> None:
    with registry() as opened:
        seed_two_frame_runtime(opened)
        finish_push(opened)
        assert opened.get_transition(PUSH).state is TransitionState.COMPLETED
        assert opened.get_request(HOST, PUSH_REQUEST).state is RequestState.COMPLETED
        assert opened.get_work_context(CONTEXT).foreground_frame_id == TASK
        assert opened.get_view(VIEW).active_frame_id == TASK
        assert opened.get_placement(WORKSPACE_PLACEMENT).state is PlacementState.PARKED
        assert opened.get_control_turn(PUSH_CONTROL).state is ControlState.SETTLED
        projected = build_host_state(opened, generated_at=151)
        assert projected.data["transitions"][0]["state"] == "completed"  # type: ignore[index]
        assert projected.data["controlTurns"][0]["state"] == "settled"  # type: ignore[index]
        with pytest.raises(StateTransitionError):
            opened.advance_control_turn(
                PUSH_CONTROL,
                ControlState.SETTLED,
                ControlState.SUBMITTED,
                now=151,
            )


def test_complete_return_refreshes_parent_authority_and_closes_child_on_claim() -> None:
    with registry() as opened:
        seed_two_frame_runtime(opened)
        finish_push(opened)
        with pytest.raises(ConflictError) as caught:
            opened.validate_capability(PARENT_TOKEN, now=155)
        assert caught.value.code == "capability_stale"
        complete = transition(
            transition_id=COMPLETE,
            request_id=COMPLETE_REQUEST,
            kind=TransitionKind.COMPLETE_RETURN,
            source=TASK,
            target=WORKSPACE,
            view_revision=2,
            claim_generation=2,
            now=160,
        )
        opened.prepare_transition(complete)
        summary = "Transition storage and tests are complete."
        next_action = "Build the v1 projections."
        opened.store_completion_handoff(
            CompletionHandoff(
                COMPLETE_HANDOFF,
                COMPLETE,
                TASK,
                CHILD_SESSION,
                WORKSPACE,
                summary,
                next_action,
                content_hash(summary, next_action),
                161,
                None,
            )
        )
        assert opened.get_frame(TASK).lifecycle_state is FrameLifecycleState.CLOSING
        opened.prepare_control_turn(
            ControlTurn(
                COMPLETE_CONTROL,
                COMPLETE,
                WORKSPACE,
                PARENT_SESSION,
                ControlKind.CLAIM_HANDOFF,
                "control.claim.v1",
                ControlTransport.LIVE_INPUT,
                ControlState.PREPARED,
                0,
                None,
                None,
                None,
                None,
                None,
            )
        )
        execute_presentation(opened, COMPLETE, "worker-2", 170)
        opened.advance_control_turn(
            COMPLETE_CONTROL,
            ControlState.PREPARED,
            ControlState.SUBMITTED,
            now=175,
        )
        opened.advance_control_turn(
            COMPLETE_CONTROL,
            ControlState.SUBMITTED,
            ControlState.OBSERVED,
            observed_prompt_id="prompt-2",
            now=176,
        )
        assert opened.validate_capability(PARENT_TOKEN, now=177).frame_id == WORKSPACE
        claim = opened.transition_claim(COMPLETE, PARENT_SESSION, PARENT_TOKEN, now=178)
        assert claim.summary == summary
        assert claim.next_action == next_action
        assert opened.get_frame(TASK).lifecycle_state is FrameLifecycleState.CLOSED
        assert opened.get_frame(TASK).close_reason is CloseReason.COMPLETED
        opened.settle_transition_claim(COMPLETE, now=179)
        assert opened.get_work_context(CONTEXT).foreground_frame_id == WORKSPACE
        opened.revoke_capability(PARENT_CAPABILITY, now=180)
        with pytest.raises(ConflictError) as caught:
            opened.validate_capability(PARENT_TOKEN, now=181)
        assert caught.value.code == "capability_expired"


def test_transport_recovery_never_repeats_an_active_or_expired_lease() -> None:
    with registry() as opened:
        seed_two_frame_runtime(opened)
        prepare_push(opened)
        opened.claim_transition_execution(PUSH, "worker-1", 150, now=140)
        with pytest.raises(ConflictError) as caught:
            opened.reclaim_transition_execution(PUSH, "worker-2", 160, now=141)
        assert caught.value.code == "lease_active"
        reclaimed = opened.reclaim_transition_execution(PUSH, "worker-2", 180, now=151)
        assert reclaimed.execution_owner == "worker-2"
        opened.advance_transport_phase(
            PUSH,
            "worker-2",
            TransportPhase.INTENT,
            TransportPhase.MOVED,
            now=152,
        )
        rolled_back = opened.advance_transport_phase(
            PUSH,
            "worker-2",
            TransportPhase.MOVED,
            TransportPhase.ROLLED_BACK,
            now=153,
        )
        assert rolled_back.transport_phase is TransportPhase.ROLLED_BACK
        with pytest.raises(StateTransitionError):
            opened.advance_transport_phase(
                PUSH,
                "worker-2",
                TransportPhase.ROLLED_BACK,
                TransportPhase.MOVED,
                now=154,
            )


def test_recovery_and_desktop_lease_lifecycles_converge_and_fence() -> None:
    with registry() as opened:
        seed_catalog_workspace(opened)
        opened.create_view(
            UserView(
                VIEW,
                HOST,
                ViewMode.DIRECT,
                WORKSPACE,
                ViewState.READY,
                0,
                "desktop-1",
                None,
                30,
                None,
                30,
            ),
            FramePlacement(
                WORKSPACE_PLACEMENT,
                HOST,
                VIEW,
                WORKSPACE,
                None,
                PlacementState.ACTIVE,
                0,
                30,
                30,
            ),
        )
        recovery = Recovery(
            ident(RecoveryId, 40),
            HOST,
            "tmux_outcome_uncertain",
            "transition",
            str(PUSH),
            RecoveryActionability.OPEN_VIEW,
            RecoveryState.OPEN,
            "Inspect the exact pane before continuing.",
            31,
            31,
        )
        assert opened.open_recovery(recovery) == recovery
        converged = opened.open_recovery(
            Recovery(
                ident(RecoveryId, 41),
                HOST,
                recovery.kind,
                recovery.subject_type,
                recovery.subject_id,
                RecoveryActionability.MANUAL,
                RecoveryState.OPEN,
                "Different observer detail.",
                32,
                32,
            )
        )
        assert converged.recovery_id == recovery.recovery_id
        assert (
            opened.settle_recovery(
                recovery.recovery_id, RecoveryState.RESOLVED, now=33
            ).state
            is RecoveryState.RESOLVED
        )

        first = DesktopAttachmentLease(
            ident(LeaseId, 42), VIEW, ident(RequestId, 43), LeaseState.OFFERED, 50
        )
        assert opened.offer_desktop_lease(first, now=40) == first
        assert opened.offer_desktop_lease(first, now=41) == first
        with pytest.raises(ConflictError) as caught:
            opened.offer_desktop_lease(
                DesktopAttachmentLease(
                    ident(LeaseId, 44),
                    VIEW,
                    ident(RequestId, 45),
                    LeaseState.OFFERED,
                    60,
                ),
                now=42,
            )
        assert caught.value.code == "desktop_launch_in_progress"
        assert opened.expire_desktop_leases(now=50) == 1
        second = opened.offer_desktop_lease(
            DesktopAttachmentLease(
                ident(LeaseId, 44),
                VIEW,
                ident(RequestId, 45),
                LeaseState.OFFERED,
                70,
            ),
            now=51,
        )
        assert (
            opened.claim_desktop_lease(VIEW, second.request_id, now=52).state
            is LeaseState.CLAIMED
        )
