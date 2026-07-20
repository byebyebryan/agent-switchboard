from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_switchboard.domain import (
    HandoffId,
    HostId,
    LaunchAction,
    LaunchId,
    LaunchIntent,
    LaunchRequest,
    LaunchRequestKind,
    LaunchState,
    LaunchTransitionError,
    LeaseError,
    LocationId,
    ProjectId,
    ProviderId,
    RequestConflictError,
    SessionKey,
    SurfaceId,
    Transport,
    ValidationError,
    ensure_same_launch_request,
)

HOST = HostId("11111111-1111-4111-8111-111111111111")
PROJECT = ProjectId("22222222-2222-4222-8222-222222222222")
LOCATION = LocationId("33333333-3333-4333-8333-333333333333")
LAUNCH = LaunchId("44444444-4444-4444-8444-444444444444")
REQUEST = UUID("55555555-5555-4555-8555-555555555555")
SESSION = SessionKey(
    HOST, ProviderId.CODEX, UUID("66666666-6666-4666-8666-666666666666")
)
CAPABILITY_HASH = "a" * 64
SURFACE = SurfaceId("77777777-7777-4777-8777-777777777777")
OTHER_SURFACE = SurfaceId("88888888-8888-4888-8888-888888888888")


def make_intent(
    tmp_path: Path,
    *,
    action: LaunchAction = LaunchAction.NEW,
    target: SessionKey | None = None,
) -> tuple[LaunchIntent, datetime]:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    return (
        LaunchIntent(
            launch_id=LAUNCH,
            request_id=REQUEST,
            host_id=HOST,
            provider=ProviderId.CODEX,
            action=action,
            project_id=PROJECT if action is LaunchAction.NEW else None,
            location_id=LOCATION if action is LaunchAction.NEW else None,
            cwd=tmp_path if action is LaunchAction.NEW else None,
            source_handoff_id=None,
            target_session_key=target,
            surface_id=None,
            transport=Transport.TMUX,
            state=LaunchState.RESERVED,
            lease_owner="worker-1",
            capability_hash=CAPABILITY_HASH,
            created_at=now,
            expires_at=now + timedelta(seconds=30),
        ),
        now,
    )


def test_legal_new_launch_transition_path(tmp_path: Path) -> None:
    intent, now = make_intent(tmp_path)
    intent = intent.transition(
        LaunchState.SURFACE_READY,
        now=now,
        owner="worker-1",
        surface_id=SURFACE,
    )
    assert intent.surface_id == SURFACE
    with pytest.raises(LaunchTransitionError, match="cannot be replaced"):
        intent.transition(
            LaunchState.WAITING_FOR_CLIENT,
            now=now,
            owner="worker-1",
            surface_id=OTHER_SURFACE,
        )
    for state in (LaunchState.WAITING_FOR_CLIENT, LaunchState.PROVIDER_STARTED):
        intent = intent.transition(state, now=now, owner="worker-1")
    with pytest.raises(LaunchTransitionError, match="provider session identity"):
        intent.transition(LaunchState.BOUND, now=now, owner="worker-1")
    intent = intent.bind_target(SESSION, now=now, owner="worker-1")
    assert intent.state is LaunchState.BOUND
    assert intent.target_session_key == SESSION
    assert intent.lease_owner is None
    with pytest.raises(LaunchTransitionError):
        intent.transition(LaunchState.FAILED, now=now, owner="worker-1")


def test_transition_rejects_skips_and_wrong_or_expired_lease(tmp_path: Path) -> None:
    intent, now = make_intent(tmp_path)
    with pytest.raises(LaunchTransitionError):
        intent.transition(LaunchState.PROVIDER_STARTED, now=now, owner="worker-1")
    with pytest.raises(LeaseError, match="different"):
        intent.transition(LaunchState.SURFACE_READY, now=now, owner="worker-2")
    with pytest.raises(LeaseError, match="expired"):
        intent.transition(
            LaunchState.SURFACE_READY,
            now=now + timedelta(seconds=30),
            owner="worker-1",
        )
    with pytest.raises(LaunchTransitionError, match="requires a surface"):
        intent.transition(
            LaunchState.SURFACE_READY,
            now=now,
            owner="worker-1",
        )


def test_expiry_and_failed_transition_require_conditions(tmp_path: Path) -> None:
    intent, now = make_intent(tmp_path)
    with pytest.raises(LeaseError, match="live lease"):
        intent.transition(LaunchState.EXPIRED, now=now)
    expired = intent.transition(LaunchState.EXPIRED, now=now + timedelta(seconds=30))
    assert expired.state is LaunchState.EXPIRED
    failed = intent.transition(
        LaunchState.FAILED,
        now=now,
        owner="worker-1",
        failure_code="surface_creation_failed",
    )
    assert failed.failure_code == "surface_creation_failed"
    with pytest.raises(ValidationError, match="failure_code"):
        intent.transition(LaunchState.FAILED, now=now, owner="worker-1")


def test_lease_renewal_is_bounded_and_owned(tmp_path: Path) -> None:
    intent, now = make_intent(tmp_path)
    renewed = intent.renew_lease(
        "worker-1", now + timedelta(minutes=1), now + timedelta(seconds=1)
    )
    assert renewed.expires_at == now + timedelta(minutes=1)
    with pytest.raises(LeaseError):
        intent.renew_lease("worker-2", now + timedelta(minutes=1), now)


def test_manage_launch_reaches_only_manager_ready(tmp_path: Path) -> None:
    intent, now = make_intent(tmp_path, action=LaunchAction.MANAGE)
    intent = intent.transition(
        LaunchState.SURFACE_READY,
        now=now,
        owner="worker-1",
        surface_id=SURFACE,
    )
    for state in (LaunchState.WAITING_FOR_CLIENT, LaunchState.PROVIDER_STARTED):
        intent = intent.transition(state, now=now, owner="worker-1")
    with pytest.raises(LaunchTransitionError):
        intent.transition(LaunchState.BOUND, now=now, owner="worker-1")
    ready = intent.transition(LaunchState.MANAGER_READY, now=now, owner="worker-1")
    assert ready.state is LaunchState.MANAGER_READY
    assert ready.lease_owner == "worker-1"
    failed = ready.transition(
        LaunchState.FAILED,
        now=now,
        owner="worker-1",
        failure_code="manager_stopped",
    )
    assert failed.lease_owner is None


def test_launch_request_normalization_and_retry_semantics(tmp_path: Path) -> None:
    handoff = HandoffId("77777777-7777-4777-8777-777777777777")
    request = LaunchRequest(
        LaunchRequestKind.NEW,
        HOST,
        ProviderId.CODEX,
        project_id=PROJECT,
        location_id=LOCATION,
        cwd=tmp_path,
        source_handoff_id=handoff,
    )
    same = LaunchRequest(
        "new",
        str(HOST),
        "codex",
        project_id=str(PROJECT),
        location_id=str(LOCATION),
        cwd=tmp_path / ".",
        source_handoff_id=str(handoff),
    )
    assert request.normalized() == same.normalized()
    assert request.fingerprint() == same.fingerprint()
    ensure_same_launch_request(request, same)
    different = LaunchRequest(
        LaunchRequestKind.NEW,
        HOST,
        ProviderId.CLAUDE,
        project_id=PROJECT,
        location_id=LOCATION,
        cwd=tmp_path,
    )
    with pytest.raises(RequestConflictError, match="request_conflict"):
        ensure_same_launch_request(request, different)


def test_request_variant_validation(tmp_path: Path) -> None:
    intent, _ = make_intent(tmp_path)
    assert replace(intent, agent_capability_hash="b" * 64).agent_capability_hash == (
        "b" * 64
    )
    with pytest.raises(ValidationError, match="agent_capability_hash"):
        replace(intent, agent_capability_hash="not-a-hash")
    with pytest.raises(ValidationError, match="requires target"):
        LaunchRequest(LaunchRequestKind.OPEN, HOST, ProviderId.CODEX)
    with pytest.raises(ValidationError, match="requires project"):
        LaunchRequest(LaunchRequestKind.NEW, HOST, ProviderId.CODEX, cwd=tmp_path)
    with pytest.raises(ValidationError, match="cannot include"):
        LaunchRequest(
            LaunchRequestKind.MANAGE,
            HOST,
            ProviderId.CODEX,
            cwd=tmp_path,
        )
    manager_intent, _ = make_intent(tmp_path, action=LaunchAction.MANAGE)
    with pytest.raises(ValidationError, match="project/session context"):
        replace(manager_intent, cwd=tmp_path)
    with pytest.raises(ValidationError, match="terminal launch"):
        replace(
            manager_intent,
            state=LaunchState.FAILED,
            failure_code="manager_failed",
        )
    with pytest.raises(ValidationError, match="different host"):
        LaunchIntent(
            launch_id=LAUNCH,
            request_id=REQUEST,
            host_id=HostId("88888888-8888-4888-8888-888888888888"),
            provider=ProviderId.CODEX,
            action=LaunchAction.RESUME,
            project_id=None,
            location_id=None,
            cwd=None,
            source_handoff_id=None,
            target_session_key=SESSION,
            surface_id=None,
            transport=Transport.TMUX,
            state=LaunchState.RESERVED,
            lease_owner="worker",
            capability_hash=CAPABILITY_HASH,
            created_at=datetime(2026, 7, 15, tzinfo=UTC),
            expires_at=datetime(2026, 7, 15, tzinfo=UTC) + timedelta(seconds=1),
        )
