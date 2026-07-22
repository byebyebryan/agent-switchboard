from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from agent_switchboard._v3.domain import (
    Activity,
    ActivityReason,
    AgentCapability,
    BackgroundState,
    CapabilityId,
    Checkout,
    CheckoutId,
    CheckoutKind,
    ClaimState,
    FrameId,
    FramePlacement,
    FrameSession,
    FrameSessionId,
    GenerationId,
    HandoffId,
    HostId,
    LaunchAction,
    LaunchId,
    LaunchIntent,
    LaunchState,
    MembershipReason,
    PlacementId,
    PlacementState,
    Project,
    ProjectId,
    ProjectRepository,
    ProviderId,
    ProviderSession,
    Repository,
    RepositoryId,
    RepositoryKind,
    RequestId,
    Resumability,
    RuntimePresence,
    SessionHandoff,
    SessionHandoffSource,
    SessionKey,
    StateTransitionError,
    Surface,
    SurfaceId,
    SurfaceState,
    TmuxServer,
    TmuxServerId,
    UserView,
    ViewId,
    ViewMode,
    ViewState,
    WorkContextId,
    content_hash,
)
from agent_switchboard._v3.storage import ConflictError, Registry

GENERATION = GenerationId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
HOST = HostId("11111111-1111-4111-8111-111111111111")
PROJECT = ProjectId("22222222-2222-4222-8222-222222222222")
REPOSITORY = RepositoryId("33333333-3333-4333-8333-333333333333")
OTHER_REPOSITORY = RepositoryId("33333333-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CHECKOUT = CheckoutId("44444444-4444-4444-8444-444444444444")
CONTEXT = WorkContextId("55555555-5555-4555-8555-555555555555")
WORKSPACE = FrameId("66666666-6666-4666-8666-666666666666")
SESSION_UUID = UUID("77777777-7777-4777-8777-777777777777")
SESSION = SessionKey(HOST, ProviderId.CODEX, SESSION_UUID)
FRAME_SESSION = FrameSessionId("88888888-8888-4888-8888-888888888888")
HANDOFF = HandoffId("99999999-9999-4999-8999-999999999999")
VIEW = ViewId("aaaaaaaa-1111-4111-8111-111111111111")
PLACEMENT = PlacementId("aaaaaaaa-2222-4222-8222-222222222222")
LAUNCH = LaunchId("aaaaaaaa-3333-4333-8333-333333333333")
REQUEST = RequestId("aaaaaaaa-4444-4444-8444-444444444444")
SURFACE = SurfaceId("aaaaaaaa-5555-4555-8555-555555555555")
TMUX = TmuxServerId("aaaaaaaa-6666-4666-8666-666666666666")
CAPABILITY = CapabilityId("aaaaaaaa-7777-4777-8777-777777777777")


def registry(path: str | Path = ":memory:") -> Registry:
    return Registry(
        path,
        generation_id=GENERATION,
        local_host_id=HOST,
        local_display_name="starship",
        now=10,
    )


def seed_catalog(opened: Registry) -> None:
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


def seed_workspace(opened: Registry) -> None:
    seed_catalog(opened)
    result = opened.ensure_workspace(
        CONTEXT, WORKSPACE, HOST, PROJECT, CHECKOUT, "Switchboard", now=30
    )
    assert result.kind == "created"


def provider_session(*, observed_at: int = 40) -> ProviderSession:
    return ProviderSession(
        SESSION,
        HOST,
        ProviderId.CODEX,
        SESSION_UUID,
        PROJECT,
        CHECKOUT,
        "switchboard",
        "Phase 6",
        False,
        RuntimePresence.LIVE,
        Resumability.RESUMABLE,
        Activity.WORKING,
        ActivityReason.UNKNOWN,
        35,
        observed_at,
        observed_at,
        observed_at,
    )


def test_catalog_materialization_is_atomic_and_checkout_identity_is_stable() -> None:
    with registry() as opened:
        seed_catalog(opened)
        seed_catalog(opened)
        assert (
            opened.connection.execute(
                "SELECT count(*) FROM projects WHERE declared = 1"
            ).fetchone()[0]
            == 1
        )
        with pytest.raises(ConflictError) as caught:
            opened.materialize_catalog(
                HOST,
                [Project(PROJECT, "Switchboard")],
                [
                    Repository(REPOSITORY, "switchboard", RepositoryKind.GIT),
                    Repository(OTHER_REPOSITORY, "other", RepositoryKind.GIT),
                ],
                [ProjectRepository(PROJECT, OTHER_REPOSITORY, True)],
                [
                    Checkout(
                        CHECKOUT,
                        OTHER_REPOSITORY,
                        HOST,
                        Path("/home/bryan/code/other"),
                        CheckoutKind.MAIN,
                        is_default=True,
                    )
                ],
                now=21,
            )
        assert caught.value.code == "checkout_identity"
        assert opened.connection.execute(
            "SELECT repository_id FROM checkouts WHERE checkout_id = ?",
            (str(CHECKOUT),),
        ).fetchone()[0] == str(REPOSITORY)


def test_lazy_workspace_converges_and_claim_operations_are_fenced() -> None:
    with registry() as opened:
        seed_catalog(opened)
        created = opened.ensure_workspace(
            CONTEXT, WORKSPACE, HOST, PROJECT, CHECKOUT, "Switchboard", now=30
        )
        existing = opened.ensure_workspace(
            WorkContextId("55555555-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            FrameId("66666666-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            HOST,
            PROJECT,
            CHECKOUT,
            "Ignored",
            now=31,
        )
        assert created.kind == "created"
        assert existing.kind == "existing"
        assert existing.frame.frame_id == WORKSPACE

        held = opened.acquire_work_context(CONTEXT, 0, WORKSPACE, now=40)
        assert held.claim_state is ClaimState.HELD
        assert held.claim_generation == 1
        with pytest.raises(ConflictError) as caught:
            opened.acquire_work_context(CONTEXT, 0, WORKSPACE, now=41)
        assert caught.value.code == "stale_generation"

        blocked = opened.block_work_context(
            CONTEXT, 1, BackgroundState.UNCERTAIN, now=42
        )
        assert blocked.claim_state is ClaimState.BLOCKED
        held = opened.acquire_work_context(CONTEXT, 2, WORKSPACE, now=43)
        with pytest.raises(ConflictError) as caught:
            opened.release_work_context(CONTEXT, held.claim_generation, now=44)
        assert caught.value.code == "background_unsafe"
        released = opened.release_work_context(
            CONTEXT, held.claim_generation, human_override=True, now=45
        )
        assert released.claim_state is ClaimState.RELEASED


def test_provider_session_rollover_and_handoff_are_idempotent_and_immutable() -> None:
    with registry() as opened:
        seed_workspace(opened)
        assert opened.upsert_provider_session(provider_session()).session_key == SESSION
        updated = opened.upsert_provider_session(provider_session(observed_at=41))
        assert updated.last_observed_at == 41
        membership = FrameSession(
            FRAME_SESSION,
            WORKSPACE,
            SESSION,
            1,
            MembershipReason.STARTED,
            42,
        )
        opened.append_frame_session(membership)
        opened.append_frame_session(membership)
        assert opened.get_frame(WORKSPACE).current_session_key == SESSION

        summary = "Ownership storage is implemented."
        next_action = "Build transition state machines."
        handoff = SessionHandoff(
            HANDOFF,
            SESSION,
            1,
            summary,
            next_action,
            SessionHandoffSource.AGENT,
            HOST,
            content_hash(summary, next_action),
            43,
        )
        opened.append_session_handoff(handoff)
        opened.append_session_handoff(handoff)
        changed = SessionHandoff(
            HANDOFF,
            SESSION,
            1,
            summary,
            "Different",
            SessionHandoffSource.AGENT,
            HOST,
            content_hash(summary, "Different"),
            43,
        )
        with pytest.raises(ConflictError) as caught:
            opened.append_session_handoff(changed)
        assert caught.value.code == "handoff_conflict"


def test_view_and_placement_lifecycle_use_revision_and_generation_cas() -> None:
    with registry() as opened:
        seed_workspace(opened)
        view = UserView(
            VIEW,
            HOST,
            ViewMode.NAVIGATOR,
            WORKSPACE,
            ViewState.READY,
            0,
            "desktop-1",
            None,
            40,
            None,
            40,
        )
        placement = FramePlacement(
            PLACEMENT,
            HOST,
            VIEW,
            WORKSPACE,
            None,
            PlacementState.ACTIVE,
            0,
            40,
            40,
        )
        opened.create_view(view, placement)
        changed = opened.set_view_mode(VIEW, 0, ViewMode.DIRECT, now=41)
        assert changed.mode is ViewMode.DIRECT
        assert changed.revision == 1
        with pytest.raises(ConflictError) as caught:
            opened.set_view_mode(VIEW, 0, ViewMode.NAVIGATOR, now=42)
        assert caught.value.code == "stale_revision"
        parked = opened.advance_placement(PLACEMENT, 0, PlacementState.PARKED, now=43)
        assert parked.generation == 1
        with pytest.raises(StateTransitionError):
            opened.advance_placement(PLACEMENT, 1, PlacementState.STAGED, now=44)
        retired = opened.retire_view(VIEW, 1, now=45)
        assert retired.state is ViewState.RETIRED


def test_launch_surface_binding_and_capability_match_exact_runtime() -> None:
    with registry() as opened:
        seed_workspace(opened)
        opened.upsert_provider_session(provider_session())
        opened.append_frame_session(
            FrameSession(
                FRAME_SESSION,
                WORKSPACE,
                SESSION,
                1,
                MembershipReason.STARTED,
                41,
            )
        )
        launch = LaunchIntent(
            LAUNCH,
            REQUEST,
            HOST,
            WORKSPACE,
            ProviderId.CODEX,
            LaunchAction.NEW,
            None,
            LaunchState.PLANNED,
            None,
            42,
            42,
        )
        surface = Surface(
            SURFACE,
            HOST,
            ProviderId.CODEX,
            None,
            LAUNCH,
            SurfaceState.PLANNED,
            None,
            None,
            None,
            None,
            0,
            42,
            42,
            None,
        )
        opened.plan_launch(launch, surface)
        opened.advance_launch(
            LAUNCH, LaunchState.PLANNED, LaunchState.AUTHORIZED, now=43
        )
        opened.advance_launch(
            LAUNCH, LaunchState.AUTHORIZED, LaunchState.STARTED, now=44
        )
        opened.record_tmux_server(
            TmuxServer(TMUX, HOST, "/tmp/tmux-1000/default", 1234, 40, 45)
        )
        live = opened.publish_surface(SURFACE, 0, TMUX, "%1", now=46)
        assert live.lifecycle_state is SurfaceState.LIVE
        bound, bound_launch = opened.bind_surface_session(SURFACE, 1, SESSION, now=47)
        assert bound.session_key == SESSION
        assert bound_launch.state is LaunchState.BOUND

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
                48,
                None,
                48,
            ),
            FramePlacement(
                PLACEMENT,
                HOST,
                VIEW,
                WORKSPACE,
                SURFACE,
                PlacementState.ACTIVE,
                0,
                48,
                48,
            ),
        )
        capability = AgentCapability(
            CAPABILITY,
            "a" * 64,
            HOST,
            VIEW,
            WORKSPACE,
            SESSION,
            SURFACE,
            LAUNCH,
            TMUX,
            "%1",
            0,
            49,
            149,
            None,
        )
        assert opened.issue_capability(capability) == capability
        with pytest.raises(ConflictError) as caught:
            opened.release_work_context(
                CONTEXT,
                opened.acquire_work_context(
                    CONTEXT, 0, WORKSPACE, now=50
                ).claim_generation,
                now=51,
            )
        assert caught.value.code == "live_surface"


def test_illegal_launch_and_surface_edges_fail_closed() -> None:
    with registry() as opened:
        seed_workspace(opened)
        opened.plan_launch(
            LaunchIntent(
                LAUNCH,
                REQUEST,
                HOST,
                WORKSPACE,
                ProviderId.CODEX,
                LaunchAction.NEW,
                None,
                LaunchState.PLANNED,
                None,
                40,
                40,
            ),
            Surface(
                SURFACE,
                HOST,
                ProviderId.CODEX,
                None,
                LAUNCH,
                SurfaceState.PLANNED,
                None,
                None,
                None,
                None,
                0,
                40,
                40,
                None,
            ),
        )
        with pytest.raises(StateTransitionError):
            opened.advance_launch(
                LAUNCH, LaunchState.PLANNED, LaunchState.BOUND, now=41
            )
        with pytest.raises(ConflictError) as caught:
            opened.publish_surface(SURFACE, 0, TMUX, "%1", now=42)
        assert caught.value.code == "tmux_identity"
