"""Independent normalized session-state axes and display derivation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from .domain import (
    Activity,
    ActivityReason,
    Attachment,
    Resumability,
    RuntimePresence,
    ValidationError,
)


class HostReachability(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class DisplayStatus(StrEnum):
    OFFLINE = "offline"
    NEEDS_INPUT = "needs_input"
    WORKING = "working"
    COMPLETED = "completed"
    READY = "ready"
    PARKED = "parked"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class HookEvent(StrEnum):
    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PERMISSION_REQUEST = "PermissionRequest"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"
    SESSION_END = "SessionEnd"


HOOK_SOURCE_PRIORITY = 100


@dataclass(frozen=True, slots=True)
class HookTransition:
    """Canonical priority and axes established by one hook event."""

    kind_priority: int
    runtime_presence: RuntimePresence
    activity: Activity | None
    activity_reason: ActivityReason | None


_HOOK_TRANSITIONS = {
    HookEvent.SESSION_START: HookTransition(
        10,
        RuntimePresence.LIVE,
        Activity.READY,
        ActivityReason.UNKNOWN,
    ),
    HookEvent.USER_PROMPT_SUBMIT: HookTransition(
        20,
        RuntimePresence.LIVE,
        Activity.WORKING,
        ActivityReason.UNKNOWN,
    ),
    HookEvent.POST_TOOL_USE: HookTransition(
        30,
        RuntimePresence.LIVE,
        Activity.WORKING,
        ActivityReason.UNKNOWN,
    ),
    HookEvent.PERMISSION_REQUEST: HookTransition(
        40,
        RuntimePresence.LIVE,
        Activity.NEEDS_INPUT,
        ActivityReason.PERMISSION,
    ),
    HookEvent.STOP: HookTransition(
        50,
        RuntimePresence.LIVE,
        Activity.READY,
        ActivityReason.TURN_COMPLETE,
    ),
    HookEvent.SESSION_END: HookTransition(
        60,
        RuntimePresence.STOPPED,
        None,
        None,
    ),
}


def hook_transition(event: HookEvent | str) -> HookTransition:
    """Return the one authoritative lifecycle mapping used by all layers."""

    return _HOOK_TRANSITIONS[_coerce(HookEvent, event, "hook event")]


def _coerce[T: StrEnum](enum_type: type[T], value: T | str, field: str) -> T:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"invalid {field}: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class SessionState:
    runtime_presence: RuntimePresence = RuntimePresence.UNKNOWN
    resumability: Resumability = Resumability.UNKNOWN
    activity: Activity = Activity.UNKNOWN
    activity_reason: ActivityReason = ActivityReason.UNKNOWN
    attachment: Attachment = Attachment.UNKNOWN
    observed_at: datetime | None = None
    kind_priority: int = -1

    def __post_init__(self) -> None:
        for field_name, enum_type in (
            ("runtime_presence", RuntimePresence),
            ("resumability", Resumability),
            ("activity", Activity),
            ("activity_reason", ActivityReason),
            ("attachment", Attachment),
        ):
            object.__setattr__(
                self,
                field_name,
                _coerce(enum_type, getattr(self, field_name), field_name),
            )
        if self.observed_at is not None:
            if (
                not isinstance(self.observed_at, datetime)
                or self.observed_at.tzinfo is None
            ):
                raise ValidationError("observed_at must be timezone-aware")
            object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))
        if (
            isinstance(self.kind_priority, bool)
            or not isinstance(self.kind_priority, int)
            or self.kind_priority < -1
        ):
            raise ValidationError("kind_priority must be an integer of at least -1")


def derive_display_status(
    reachability: HostReachability,
    state: SessionState,
) -> DisplayStatus:
    """Apply the documented primary-label precedence without merging axes."""

    reachability = _coerce(HostReachability, reachability, "host reachability")
    if reachability is HostReachability.OFFLINE:
        return DisplayStatus.OFFLINE
    if state.activity is Activity.NEEDS_INPUT:
        return DisplayStatus.NEEDS_INPUT
    if state.activity is Activity.WORKING:
        return DisplayStatus.WORKING
    if state.activity is Activity.COMPLETED:
        return DisplayStatus.COMPLETED
    if state.activity is Activity.READY:
        return DisplayStatus.READY
    if (
        state.runtime_presence is RuntimePresence.STOPPED
        and state.resumability is Resumability.RESUMABLE
    ):
        return DisplayStatus.PARKED
    if (
        state.runtime_presence is RuntimePresence.STOPPED
        and state.resumability is Resumability.MISSING
    ):
        return DisplayStatus.UNAVAILABLE
    return DisplayStatus.UNKNOWN


def apply_hook_transition(
    state: SessionState,
    event: HookEvent,
    *,
    observed_at: datetime,
) -> SessionState:
    """Apply one normalized hook observation, rejecting stale observations."""

    event = _coerce(HookEvent, event, "hook event")
    if observed_at.tzinfo is None:
        raise ValidationError("observed_at must be timezone-aware")
    observed_at = observed_at.astimezone(UTC)
    if state.observed_at is not None and observed_at < state.observed_at:
        raise ValidationError("stale hook observation")
    transition = hook_transition(event)
    kind_priority = transition.kind_priority
    if (
        state.observed_at is not None
        and observed_at == state.observed_at
        and kind_priority < state.kind_priority
    ):
        return state
    changes: dict[str, object] = {
        "runtime_presence": transition.runtime_presence,
        "observed_at": observed_at,
        "kind_priority": kind_priority,
    }
    if transition.activity is not None and transition.activity_reason is not None:
        changes.update(
            activity=transition.activity,
            activity_reason=transition.activity_reason,
        )
    return replace(state, **changes)


__all__ = [
    "HOOK_SOURCE_PRIORITY",
    "DisplayStatus",
    "HookEvent",
    "HookTransition",
    "HostReachability",
    "SessionState",
    "apply_hook_transition",
    "derive_display_status",
    "hook_transition",
]
