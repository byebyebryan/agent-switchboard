from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_switchboard.domain import (
    Activity,
    ActivityReason,
    Attachment,
    Resumability,
    RuntimePresence,
    ValidationError,
)
from agent_switchboard.state import (
    DisplayStatus,
    HookEvent,
    HostReachability,
    SessionState,
    apply_hook_transition,
    derive_display_status,
)


@pytest.mark.parametrize(
    ("reachability", "state", "expected"),
    [
        (
            HostReachability.OFFLINE,
            SessionState(activity=Activity.NEEDS_INPUT),
            DisplayStatus.OFFLINE,
        ),
        (
            HostReachability.ONLINE,
            SessionState(activity=Activity.NEEDS_INPUT),
            DisplayStatus.NEEDS_INPUT,
        ),
        (
            HostReachability.ONLINE,
            SessionState(activity=Activity.WORKING),
            DisplayStatus.WORKING,
        ),
        (
            HostReachability.ONLINE,
            SessionState(activity=Activity.COMPLETED),
            DisplayStatus.COMPLETED,
        ),
        (
            HostReachability.ONLINE,
            SessionState(activity=Activity.READY),
            DisplayStatus.READY,
        ),
        (
            HostReachability.ONLINE,
            SessionState(
                runtime_presence=RuntimePresence.STOPPED,
                resumability=Resumability.RESUMABLE,
            ),
            DisplayStatus.PARKED,
        ),
        (
            HostReachability.ONLINE,
            SessionState(
                runtime_presence=RuntimePresence.STOPPED,
                resumability=Resumability.MISSING,
            ),
            DisplayStatus.UNAVAILABLE,
        ),
        (
            HostReachability.UNKNOWN,
            SessionState(),
            DisplayStatus.UNKNOWN,
        ),
    ],
)
def test_display_precedence(
    reachability: HostReachability,
    state: SessionState,
    expected: DisplayStatus,
) -> None:
    assert derive_display_status(reachability, state) is expected


def test_attachment_remains_independent_from_primary_status() -> None:
    attached = SessionState(activity=Activity.WORKING, attachment=Attachment.ATTACHED)
    detached = SessionState(activity=Activity.WORKING, attachment=Attachment.DETACHED)
    assert (
        derive_display_status(HostReachability.ONLINE, attached)
        is DisplayStatus.WORKING
    )
    assert (
        derive_display_status(HostReachability.ONLINE, detached)
        is DisplayStatus.WORKING
    )
    assert attached.attachment != detached.attachment


def test_hook_transitions_follow_normalized_contract() -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    state = apply_hook_transition(
        SessionState(), HookEvent.SESSION_START, observed_at=now
    )
    assert (state.runtime_presence, state.activity) == (
        RuntimePresence.LIVE,
        Activity.READY,
    )
    state = apply_hook_transition(
        state, HookEvent.USER_PROMPT_SUBMIT, observed_at=now + timedelta(seconds=1)
    )
    assert state.activity is Activity.WORKING
    state = apply_hook_transition(
        state, HookEvent.PERMISSION_REQUEST, observed_at=now + timedelta(seconds=2)
    )
    assert (state.activity, state.activity_reason) == (
        Activity.NEEDS_INPUT,
        ActivityReason.PERMISSION,
    )
    state = apply_hook_transition(
        state, HookEvent.STOP, observed_at=now + timedelta(seconds=3)
    )
    assert (state.activity, state.activity_reason) == (
        Activity.READY,
        ActivityReason.TURN_COMPLETE,
    )
    state = apply_hook_transition(
        state, HookEvent.SESSION_END, observed_at=now + timedelta(seconds=4)
    )
    assert state.runtime_presence is RuntimePresence.STOPPED


def test_stale_hook_observation_is_rejected() -> None:
    now = datetime.now(UTC)
    state = SessionState(observed_at=now)
    with pytest.raises(ValidationError, match="stale"):
        apply_hook_transition(
            state, HookEvent.POST_TOOL_USE, observed_at=now - timedelta(seconds=1)
        )


def test_equal_time_hook_precedence_is_order_independent() -> None:
    now = datetime.now(UTC)
    permission_then_tool = apply_hook_transition(
        apply_hook_transition(
            SessionState(), HookEvent.PERMISSION_REQUEST, observed_at=now
        ),
        HookEvent.POST_TOOL_USE,
        observed_at=now,
    )
    tool_then_permission = apply_hook_transition(
        apply_hook_transition(SessionState(), HookEvent.POST_TOOL_USE, observed_at=now),
        HookEvent.PERMISSION_REQUEST,
        observed_at=now,
    )
    assert permission_then_tool == tool_then_permission
    assert permission_then_tool.activity is Activity.NEEDS_INPUT
    assert permission_then_tool.activity_reason is ActivityReason.PERMISSION
