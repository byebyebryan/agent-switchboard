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


_HOOK_KIND_PRIORITY = {
    HookEvent.SESSION_START: 10,
    HookEvent.USER_PROMPT_SUBMIT: 20,
    HookEvent.POST_TOOL_USE: 30,
    HookEvent.PERMISSION_REQUEST: 40,
    HookEvent.STOP: 50,
    HookEvent.SESSION_END: 60,
}


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
    kind_priority = _HOOK_KIND_PRIORITY[event]
    if (
        state.observed_at is not None
        and observed_at == state.observed_at
        and kind_priority < state.kind_priority
    ):
        return state
    changes: dict[str, object] = {
        "runtime_presence": RuntimePresence.LIVE,
        "observed_at": observed_at,
        "kind_priority": kind_priority,
    }
    if event is HookEvent.SESSION_START:
        changes.update(activity=Activity.READY, activity_reason=ActivityReason.UNKNOWN)
    elif event in {HookEvent.USER_PROMPT_SUBMIT, HookEvent.POST_TOOL_USE}:
        changes.update(
            activity=Activity.WORKING,
            activity_reason=ActivityReason.UNKNOWN,
        )
    elif event is HookEvent.PERMISSION_REQUEST:
        changes.update(
            activity=Activity.NEEDS_INPUT,
            activity_reason=ActivityReason.PERMISSION,
        )
    elif event is HookEvent.STOP:
        changes.update(
            activity=Activity.READY,
            activity_reason=ActivityReason.TURN_COMPLETE,
        )
    elif event is HookEvent.SESSION_END:
        changes["runtime_presence"] = RuntimePresence.STOPPED
    return replace(state, **changes)
