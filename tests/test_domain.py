from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_switchboard.domain import (
    Activity,
    AgentSession,
    AmbiguousCheckoutError,
    Attachment,
    Checkout,
    CheckoutConflictError,
    CheckoutId,
    Handoff,
    HandoffSource,
    HostId,
    Project,
    ProjectConflictError,
    ProjectId,
    ProjectRepository,
    ProviderId,
    RepositoryId,
    Resumability,
    RuntimeLocator,
    RuntimePresence,
    SessionKey,
    StateConfidence,
    Surface,
    SurfaceId,
    SurfaceRole,
    Transport,
    ValidationError,
    assign_checkout,
    match_checkout,
    merge_checkouts,
    merge_projects,
)

HOST_A = HostId("11111111-1111-4111-8111-111111111111")
HOST_B = HostId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PROJECT_ID = ProjectId("22222222-2222-4222-8222-222222222222")
REPOSITORY_ID = RepositoryId("66666666-6666-4666-8666-666666666666")
LOCATION_A = CheckoutId("33333333-3333-4333-8333-333333333333")
LOCATION_B = CheckoutId("44444444-4444-4444-8444-444444444444")
SESSION_ID = UUID("55555555-5555-4555-8555-555555555555")


def test_uuid_ids_are_validated_and_type_distinct() -> None:
    assert str(HOST_A) == "11111111-1111-4111-8111-111111111111"
    assert ProjectId(str(HOST_A)) != HOST_A
    with pytest.raises(ValidationError, match="invalid UUID"):
        HostId("not-a-uuid")
    with pytest.raises(ValidationError, match="nil UUID"):
        HostId("00000000-0000-0000-0000-000000000000")


def test_session_key_round_trip_and_namespace() -> None:
    key = SessionKey(HOST_A, ProviderId.CODEX, SESSION_ID)
    assert SessionKey.parse(str(key)) == key
    assert key != SessionKey(HOST_A, ProviderId.CLAUDE, SESSION_ID)
    assert key != SessionKey(HOST_B, ProviderId.CODEX, SESSION_ID)
    with pytest.raises(ValidationError):
        SessionKey.parse("codex:missing")


def test_agent_session_validates_independent_axes(tmp_path: Path) -> None:
    key = SessionKey(HOST_A, ProviderId.CODEX, SESSION_ID)
    session = AgentSession(
        key,
        tmp_path,
        runtime_presence="live",
        resumability="resumable",
        activity="working",
        attachment="detached",
        state_confidence="confirmed",
    )
    assert session.runtime_presence is RuntimePresence.LIVE
    assert session.resumability is Resumability.RESUMABLE
    assert session.activity is Activity.WORKING
    assert session.attachment is Attachment.DETACHED
    assert session.state_confidence is StateConfidence.CONFIRMED
    assert session.host_id == HOST_A
    assert session.provider is ProviderId.CODEX
    with pytest.raises(ValidationError, match="runtime_presence"):
        AgentSession(key, tmp_path, runtime_presence="parked")
    with pytest.raises(ValidationError, match="activity"):
        AgentSession(key, tmp_path, activity="offline")
    controlled_path = Path(f"{tmp_path}\u009bchild")
    with pytest.raises(ValidationError, match="path contains control"):
        AgentSession(key, controlled_path)
    with pytest.raises(ValidationError, match="path contains control"):
        Checkout(LOCATION_A, REPOSITORY_ID, HOST_A, controlled_path)
    with pytest.raises(ValidationError, match="control"):
        Checkout(
            LOCATION_A,
            REPOSITORY_ID,
            HOST_A,
            tmp_path,
            display_name="bad\u009bdisplay",
        )


def test_runtime_locator_and_surface_invariants() -> None:
    now = datetime.now(UTC)
    locator = RuntimeLocator(pid=42, tmux_pane="%7", observed_at=now)
    assert locator.observed_at == now
    with pytest.raises(ValidationError, match="positive"):
        RuntimeLocator(pid=0)
    key = SessionKey(HOST_A, ProviderId.CODEX, SESSION_ID)
    with pytest.raises(ValidationError, match="manager"):
        Surface(
            surface_id=SurfaceId("66666666-6666-4666-8666-666666666666"),
            host_id=HOST_A,
            provider=ProviderId.CODEX,
            transport=Transport.TMUX,
            transport_locator="tmux:@1.%1",
            role=SurfaceRole.PROVIDER_MANAGER,
            current_session_key=key,
        )
    with pytest.raises(ValidationError, match="provider"):
        Surface(
            surface_id=SurfaceId("66666666-6666-4666-8666-666666666666"),
            host_id=HOST_A,
            provider=ProviderId.CLAUDE,
            transport=Transport.TMUX,
            transport_locator="tmux:@1.%1",
            role=SurfaceRole.SESSION,
            current_session_key=key,
        )
    with pytest.raises(ValidationError, match="requires a session"):
        Surface(
            surface_id=SurfaceId("66666666-6666-4666-8666-666666666666"),
            host_id=HOST_A,
            provider=ProviderId.CODEX,
            transport=Transport.TMUX,
            transport_locator="tmux:@1.%1",
            role=SurfaceRole.SESSION,
            binding_confidence="confirmed",
        )


def test_handoff_is_immutable_hashed_and_bounded() -> None:
    now = datetime.now(UTC)
    key = SessionKey(HOST_A, ProviderId.CLAUDE, SESSION_ID)
    handoff = Handoff.create(
        session_key=key,
        sequence=1,
        summary="Implemented the parser.",
        next_action="Run integration tests.",
        source=HandoffSource.AGENT,
        source_host_id=HOST_A,
        created_at=now,
    )
    assert len(handoff.content_hash) == 64
    with pytest.raises(FrozenInstanceError):
        handoff.summary = "rewritten"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="does not match"):
        Handoff(
            handoff.handoff_id,
            key,
            1,
            "changed",
            handoff.next_action,
            handoff.source,
            HOST_A,
            now,
            handoff.content_hash,
        )
    with pytest.raises(ValidationError, match="control"):
        Handoff.create(
            session_key=key,
            sequence=1,
            summary="bad\x00text",
            next_action="continue",
            source=HandoffSource.USER,
            source_host_id=HOST_A,
            created_at=now,
        )


def test_project_merge_unions_sanitized_aliases_and_detects_conflict() -> None:
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = older + timedelta(days=1)
    first = Project(
        PROJECT_ID,
        "Switchboard",
        aliases=(" agent router ", "Sessions"),
        default_provider=ProviderId.CODEX,
        created_at=newer,
        updated_at=older,
    )
    second = Project(
        PROJECT_ID,
        "Switchboard",
        aliases=("Agent   Router", "remote"),
        default_provider=ProviderId.CODEX,
        created_at=older,
        updated_at=newer,
    )
    merged = merge_projects([first, second])[0]
    assert merged.aliases == ("agent router", "remote", "Sessions")
    assert merged.created_at == older
    assert merged.updated_at == newer
    with pytest.raises(ProjectConflictError) as error:
        merge_projects([first, Project(PROJECT_ID, "Different")])
    assert {"name", "default_provider"} <= set(error.value.fields)


def test_checkout_merge_rejects_conflicts_and_multiple_defaults(tmp_path: Path) -> None:
    first = Checkout(LOCATION_A, REPOSITORY_ID, HOST_A, tmp_path, is_default=True)
    duplicate = Checkout(LOCATION_A, REPOSITORY_ID, HOST_A, tmp_path, is_default=True)
    assert merge_checkouts([first, duplicate]) == (first,)
    with pytest.raises(CheckoutConflictError, match="conflicting"):
        merge_checkouts(
            [
                first,
                Checkout(LOCATION_A, REPOSITORY_ID, HOST_B, tmp_path),
            ]
        )
    with pytest.raises(CheckoutConflictError, match="multiple defaults"):
        merge_checkouts(
            [
                first,
                Checkout(
                    LOCATION_B,
                    REPOSITORY_ID,
                    HOST_A,
                    tmp_path / "worktree",
                    is_default=True,
                ),
            ]
        )


def test_checkout_merge_keeps_latest_observation(tmp_path: Path) -> None:
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = older + timedelta(seconds=1)
    first = Checkout(
        LOCATION_A, REPOSITORY_ID, HOST_A, tmp_path, last_observed_at=older
    )
    second = Checkout(
        LOCATION_A, REPOSITORY_ID, HOST_A, tmp_path, last_observed_at=newer
    )
    assert merge_checkouts([first, second])[0].last_observed_at == newer


def test_canonical_longest_path_matching_and_assignment(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    worktree = repository / "worktrees" / "feature"
    cwd = worktree / "src"
    cwd.mkdir(parents=True)
    root_checkout = Checkout(LOCATION_A, REPOSITORY_ID, HOST_A, repository)
    worktree_checkout = Checkout(LOCATION_B, REPOSITORY_ID, HOST_A, worktree)
    assert (
        match_checkout(cwd, HOST_A, [root_checkout, worktree_checkout])
        == worktree_checkout
    )
    assert match_checkout(cwd, HOST_B, [root_checkout]) is None
    session = AgentSession(SessionKey(HOST_A, ProviderId.CODEX, SESSION_ID), cwd)
    memberships = [ProjectRepository(PROJECT_ID, REPOSITORY_ID, True)]
    assigned = assign_checkout(session, [root_checkout, worktree_checkout], memberships)
    assert assigned.project_id == PROJECT_ID
    assert assigned.checkout_id == LOCATION_B
    assert assigned.metadata_source == "checkout_match"
    curated = AgentSession(
        SessionKey(HOST_A, ProviderId.CODEX, SESSION_ID),
        cwd,
        project_id=ProjectId("77777777-7777-4777-8777-777777777777"),
    )
    assert (
        assign_checkout(curated, [root_checkout, worktree_checkout], memberships)
        is curated
    )


def test_matching_resolves_symlinks_and_reports_equal_ambiguity(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    first = Checkout(LOCATION_A, REPOSITORY_ID, HOST_A, real)
    assert match_checkout(link, HOST_A, [first]) == first
    second = Checkout(
        LOCATION_B,
        RepositoryId("77777777-7777-4777-8777-777777777777"),
        HOST_A,
        real,
    )
    with pytest.raises(AmbiguousCheckoutError) as error:
        match_checkout(real / "src", HOST_A, [first, second])
    assert set(error.value.checkouts) == {first, second}
