"""Validated values and state machines for the private Phase 6 baseline."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

MAX_NAME_LENGTH = 256
MAX_PURPOSE_LENGTH = 4_096
MAX_SEMANTIC_TEXT_BYTES = 64 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ValidationError(ValueError):
    """A Phase 6 value violates its stable contract."""


class StateTransitionError(ValidationError):
    """A requested durable state edge is not legal."""


@dataclass(frozen=True, slots=True, order=True)
class UUIDId:
    """Base for type-distinct canonical non-nil UUID identifiers."""

    value: UUID

    def __init__(self, value: UUID | str) -> None:
        try:
            parsed = value if isinstance(value, UUID) else UUID(str(value))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValidationError(f"invalid UUID: {value!r}") from error
        if parsed.int == 0:
            raise ValidationError("nil UUID is not a valid identifier")
        object.__setattr__(self, "value", parsed)

    @classmethod
    def new(cls) -> Self:
        return cls(uuid4())

    def __str__(self) -> str:
        return str(self.value)


class GenerationId(UUIDId):
    __slots__ = ()


class HostId(UUIDId):
    __slots__ = ()


class ProjectId(UUIDId):
    __slots__ = ()


class RepositoryId(UUIDId):
    __slots__ = ()


class CheckoutId(UUIDId):
    __slots__ = ()


class FrameId(UUIDId):
    __slots__ = ()


class FrameSessionId(UUIDId):
    __slots__ = ()


class WorkContextId(UUIDId):
    __slots__ = ()


class ViewId(UUIDId):
    __slots__ = ()


class PlacementId(UUIDId):
    __slots__ = ()


class TmuxServerId(UUIDId):
    __slots__ = ()


class SurfaceId(UUIDId):
    __slots__ = ()


class LaunchId(UUIDId):
    __slots__ = ()


class TransitionId(UUIDId):
    __slots__ = ()


class BriefId(UUIDId):
    __slots__ = ()


class HandoffId(UUIDId):
    __slots__ = ()


class ControlTurnId(UUIDId):
    __slots__ = ()


class RecoveryId(UUIDId):
    __slots__ = ()


class LeaseId(UUIDId):
    __slots__ = ()


class CapabilityId(UUIDId):
    __slots__ = ()


class RequestId(UUIDId):
    __slots__ = ()


class ProviderId(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"


class Transport(StrEnum):
    TMUX = "tmux"


class RepositoryKind(StrEnum):
    GIT = "git"
    DIRECTORY = "directory"


class CheckoutKind(StrEnum):
    MAIN = "main"
    WORKTREE = "worktree"
    DIRECTORY = "directory"


class FrameRole(StrEnum):
    WORKSPACE = "workspace"
    TASK = "task"


class FrameLifecycleState(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class CloseReason(StrEnum):
    COMPLETED = "completed"
    DISMISSED = "dismissed"


class CreatedBy(StrEnum):
    USER = "user"
    AGENT = "agent"
    CUTOVER = "cutover"


class MembershipReason(StrEnum):
    STARTED = "started"
    RESUMED = "resumed"
    ROLLOVER = "rollover"
    RECOVERY = "recovery"
    CUTOVER = "cutover"


class ClaimState(StrEnum):
    RELEASED = "released"
    HELD = "held"
    BLOCKED = "blocked"


class BackgroundState(StrEnum):
    SAFE = "safe"
    KNOWN = "known"
    UNCERTAIN = "uncertain"


class ViewMode(StrEnum):
    NAVIGATOR = "navigator"
    DIRECT = "direct"


class ViewState(StrEnum):
    READY = "ready"
    TRANSITIONING = "transitioning"
    DEGRADED = "degraded"
    RETIRED = "retired"


class PlacementState(StrEnum):
    ACTIVE = "active"
    PARKED = "parked"
    STAGED = "staged"
    STOPPED_AFFINITY = "stopped_affinity"
    ORPHANED = "orphaned"


class SurfaceState(StrEnum):
    PLANNED = "planned"
    LIVE = "live"
    DEAD = "dead"
    ORPHANED = "orphaned"
    RETIRED = "retired"


class LaunchAction(StrEnum):
    NEW = "new"
    RESUME = "resume"


class LaunchState(StrEnum):
    PLANNED = "planned"
    AUTHORIZED = "authorized"
    STARTED = "started"
    BOUND = "bound"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class TransitionKind(StrEnum):
    FOCUS = "focus"
    PUSH = "push"
    BACK = "back"
    COMPLETE_RETURN = "complete_return"
    HUMAN_CLOSE = "human_close"
    MODE = "mode"
    RECOVER = "recover"


class TransitionState(StrEnum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    PRESENTED = "presented"
    AWAITING_CLAIM = "awaiting_claim"
    SETTLING = "settling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class TransportPhase(StrEnum):
    INTENT = "intent"
    MOVED = "moved"
    INSPECTED = "inspected"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


class ControlKind(StrEnum):
    CLAIM_BRIEF = "claim_brief"
    CLAIM_HANDOFF = "claim_handoff"


class ControlTransport(StrEnum):
    LIVE_INPUT = "live_input"
    RESUME_INITIAL = "resume_initial"


class ControlState(StrEnum):
    PREPARED = "prepared"
    SUBMITTED = "submitted"
    OBSERVED = "observed"
    CLAIMED = "claimed"
    SETTLED = "settled"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class RecoveryActionability(StrEnum):
    SAFE_AUTO = "safe_auto"
    OPEN_VIEW = "open_view"
    MANUAL = "manual"


class RecoveryState(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class LeaseState(StrEnum):
    OFFERED = "offered"
    CLAIMED = "claimed"
    EXPIRED = "expired"


class RequestState(StrEnum):
    PREPARED = "prepared"
    COMPLETED = "completed"
    FAILED = "failed"


class ActivationState(StrEnum):
    CUTOVER_STAGED = "cutover_staged"
    COMMITTED = "committed"


class RuntimePresence(StrEnum):
    LIVE = "live"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


class Resumability(StrEnum):
    RESUMABLE = "resumable"
    MISSING = "missing"
    UNKNOWN = "unknown"


class Activity(StrEnum):
    WORKING = "working"
    NEEDS_INPUT = "needs_input"
    READY = "ready"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class ActivityReason(StrEnum):
    PERMISSION = "permission"
    QUESTION = "question"
    ELICITATION = "elicitation"
    TURN_COMPLETE = "turn_complete"
    PROVIDER_COMPLETE = "provider_complete"
    ERROR = "error"
    UNKNOWN = "unknown"


class SessionHandoffSource(StrEnum):
    USER = "user"
    AGENT = "agent"
    IMPORTED = "imported"


class TaskPushPolicy(StrEnum):
    CONSERVATIVE = "conservative"
    OFF = "off"


class CompleteReturnPolicy(StrEnum):
    SYNTHESIZE = "synthesize"
    HANDOFF = "handoff"


class ControlTurnPolicy(StrEnum):
    LIVE_FIRST = "live_first"
    RESUME_ONLY = "resume_only"


class Reachability(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, order=True)
class SessionKey:
    host_id: HostId
    provider: ProviderId
    provider_session_id: UUID

    @classmethod
    def parse(cls, value: str) -> Self:
        if not isinstance(value, str):
            raise ValidationError("session key must be a string")
        parts = value.split(":")
        if len(parts) != 3:
            raise ValidationError("session key must contain host, provider, and UUID")
        try:
            provider = ProviderId(parts[1])
            provider_session_id = UUID(parts[2])
        except ValueError as error:
            raise ValidationError(f"invalid session key: {value!r}") from error
        if provider_session_id.int == 0:
            raise ValidationError("session key contains a nil provider UUID")
        return cls(HostId(parts[0]), provider, provider_session_id)

    def __str__(self) -> str:
        return f"{self.host_id}:{self.provider.value}:{self.provider_session_id}"


def bounded_text(
    value: str,
    field: str,
    *,
    maximum: int = MAX_NAME_LENGTH,
    multiline: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise ValidationError(f"{field} must not be empty")
    if len(normalized) > maximum or len(normalized.encode("utf-8")) > maximum:
        raise ValidationError(f"{field} exceeds its {maximum}-byte limit")
    allowed_controls = {"\n", "\t"} if multiline else set()
    if any(
        unicodedata.category(character) == "Cc" and character not in allowed_controls
        for character in normalized
    ):
        raise ValidationError(f"{field} contains a control character")
    return normalized


def optional_text(
    value: str | None,
    field: str,
    *,
    maximum: int = MAX_NAME_LENGTH,
    multiline: bool = False,
) -> str | None:
    if value is None:
        return None
    return bounded_text(value, field, maximum=maximum, multiline=multiline)


def require_timestamp(value: int | None, field: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{field} must be a non-negative integer timestamp")


def require_hash(value: str, field: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValidationError(f"{field} must be a lower-case SHA-256 digest")


def canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValidationError("value is not canonical JSON data") from error


def semantic_fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


_PRESENTATION_CAPABILITY_FIELDS = {
    "canFocusDesktop",
    "canLaunchTerminal",
    "can_focus_desktop",
    "can_launch_terminal",
}


def request_fingerprint(operation: str, semantic_fields: Mapping[str, Any]) -> str:
    """Hash semantic target fields while rejecting presentation capability."""

    operation = bounded_text(operation, "operation", maximum=64)
    forbidden = sorted(set(semantic_fields) & _PRESENTATION_CAPABILITY_FIELDS)
    if forbidden:
        raise ValidationError(
            f"presentation capability is not a semantic request field: {forbidden}"
        )
    return semantic_fingerprint(
        {"operation": operation, "semantic": dict(semantic_fields)}
    )


def content_hash(*parts: str) -> str:
    normalized = [
        bounded_text(
            part,
            f"content[{index}]",
            maximum=MAX_SEMANTIC_TEXT_BYTES,
            multiline=True,
        )
        for index, part in enumerate(parts)
    ]
    encoded = canonical_json({"content": normalized}).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class FailureRecord:
    code: str
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", bounded_text(self.code, "code", maximum=64))
        object.__setattr__(
            self,
            "message",
            bounded_text(self.message, "message", maximum=1_024),
        )


@dataclass(frozen=True, slots=True)
class Host:
    host_id: HostId
    display_name: str
    is_local: bool
    created_at: int
    updated_at: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "display_name", bounded_text(self.display_name, "display_name")
        )
        require_timestamp(self.created_at, "created_at")
        require_timestamp(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValidationError("updated_at precedes created_at")


@dataclass(frozen=True, slots=True)
class Project:
    project_id: ProjectId
    name: str
    aliases: tuple[str, ...] = ()
    default_provider: ProviderId | None = None
    default_transport: Transport = Transport.TMUX
    task_push: TaskPushPolicy | None = None
    complete_return: CompleteReturnPolicy | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", bounded_text(self.name, "project.name"))
        aliases_by_key: dict[str, str] = {}
        for alias in self.aliases:
            normalized = " ".join(
                bounded_text(alias, "project.alias", maximum=128).split()
            )
            aliases_by_key.setdefault(normalized.casefold(), normalized)
        aliases = tuple(aliases_by_key.values())
        object.__setattr__(self, "aliases", aliases)


@dataclass(frozen=True, slots=True)
class Repository:
    repository_id: RepositoryId
    name: str
    kind: RepositoryKind
    context_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", bounded_text(self.name, "repository.name"))
        sources: list[str] = []
        for source in self.context_sources:
            normalized = bounded_text(source, "context_source", maximum=1_024)
            candidate = Path(normalized)
            if candidate.is_absolute() or ".." in candidate.parts or normalized == ".":
                raise ValidationError("context_source must be project-relative")
            if candidate.as_posix() not in sources:
                sources.append(candidate.as_posix())
        object.__setattr__(self, "context_sources", tuple(sources))


@dataclass(frozen=True, slots=True)
class ProjectRepository:
    project_id: ProjectId
    repository_id: RepositoryId
    is_primary: bool


@dataclass(frozen=True, slots=True)
class Checkout:
    checkout_id: CheckoutId
    repository_id: RepositoryId
    host_id: HostId
    path: Path
    kind: CheckoutKind
    display_name: str | None = None
    provider_override: ProviderId | None = None
    is_default: bool = False

    def __post_init__(self) -> None:
        path = Path(self.path).expanduser()
        if not path.is_absolute() or len(str(path)) > 4_096:
            raise ValidationError("checkout.path must be a bounded absolute path")
        object.__setattr__(self, "path", path)
        object.__setattr__(
            self,
            "display_name",
            optional_text(self.display_name, "checkout.display_name"),
        )


@dataclass(frozen=True, slots=True)
class ProviderSession:
    session_key: SessionKey
    host_id: HostId
    provider: ProviderId
    provider_session_id: UUID
    project_id: ProjectId | None
    checkout_id: CheckoutId | None
    name: str | None
    purpose: str | None
    pinned: bool
    runtime_presence: RuntimePresence
    resumability: Resumability
    activity: Activity
    activity_reason: ActivityReason
    created_at: int | None
    provider_updated_at: int | None
    last_observed_at: int
    updated_at: int

    def __post_init__(self) -> None:
        if (
            self.session_key.host_id != self.host_id
            or self.session_key.provider != self.provider
            or self.session_key.provider_session_id != self.provider_session_id
        ):
            raise ValidationError(
                "provider session identity disagrees with session key"
            )
        if self.checkout_id is not None and self.project_id is None:
            raise ValidationError("checkout association requires a project")
        object.__setattr__(
            self, "name", optional_text(self.name, "session.name", maximum=512)
        )
        object.__setattr__(
            self,
            "purpose",
            optional_text(self.purpose, "session.purpose", maximum=MAX_PURPOSE_LENGTH),
        )
        require_timestamp(self.created_at, "created_at", optional=True)
        require_timestamp(
            self.provider_updated_at, "provider_updated_at", optional=True
        )
        require_timestamp(self.last_observed_at, "last_observed_at")
        require_timestamp(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class SessionHandoff:
    handoff_id: HandoffId
    session_key: SessionKey
    sequence: int
    summary: str
    next_action: str
    source: SessionHandoffSource
    source_host_id: HostId
    content_hash: str
    created_at: int

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or self.sequence < 1:
            raise ValidationError("handoff sequence must be positive")
        summary = bounded_text(
            self.summary,
            "summary",
            maximum=MAX_SEMANTIC_TEXT_BYTES,
            multiline=True,
        )
        next_action = bounded_text(
            self.next_action,
            "next_action",
            maximum=MAX_SEMANTIC_TEXT_BYTES,
            multiline=True,
        )
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "next_action", next_action)
        require_hash(self.content_hash, "content_hash")
        if self.content_hash != content_hash(summary, next_action):
            raise ValidationError("handoff content hash mismatch")
        require_timestamp(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class WorkContext:
    work_context_id: WorkContextId
    host_id: HostId
    project_id: ProjectId
    checkout_id: CheckoutId
    claim_state: ClaimState
    claim_generation: int
    foreground_frame_id: FrameId | None
    background_state: BackgroundState
    acquired_at: int | None
    released_at: int | None
    updated_at: int

    def __post_init__(self) -> None:
        if isinstance(self.claim_generation, bool) or self.claim_generation < 0:
            raise ValidationError("claim_generation must be non-negative")
        if self.claim_state is ClaimState.HELD and self.foreground_frame_id is None:
            raise ValidationError("held WorkContext requires a foreground frame")
        if (
            self.claim_state is not ClaimState.HELD
            and self.foreground_frame_id is not None
        ):
            raise ValidationError("only a held WorkContext may name a foreground frame")
        require_timestamp(self.acquired_at, "acquired_at", optional=True)
        require_timestamp(self.released_at, "released_at", optional=True)
        require_timestamp(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class Frame:
    frame_id: FrameId
    host_id: HostId
    project_id: ProjectId
    role: FrameRole
    parent_frame_id: FrameId | None
    work_context_id: WorkContextId
    title: str
    purpose: str | None
    preferred_provider: ProviderId | None
    lifecycle_state: FrameLifecycleState
    close_reason: CloseReason | None
    current_session_key: SessionKey | None
    created_by: CreatedBy
    created_at: int
    updated_at: int

    def __post_init__(self) -> None:
        if self.role is FrameRole.WORKSPACE and self.parent_frame_id is not None:
            raise ValidationError("workspace frame cannot have a parent")
        if self.role is FrameRole.TASK and self.parent_frame_id is None:
            raise ValidationError("task frame requires a parent")
        if (
            self.lifecycle_state is FrameLifecycleState.CLOSED
            and self.close_reason is None
        ):
            raise ValidationError("closed frame requires a close reason")
        if (
            self.lifecycle_state is not FrameLifecycleState.CLOSED
            and self.close_reason is not None
        ):
            raise ValidationError("only a closed frame may have a close reason")
        object.__setattr__(self, "title", bounded_text(self.title, "frame.title"))
        object.__setattr__(
            self,
            "purpose",
            optional_text(self.purpose, "frame.purpose", maximum=MAX_PURPOSE_LENGTH),
        )
        require_timestamp(self.created_at, "created_at")
        require_timestamp(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class FrameSession:
    frame_session_id: FrameSessionId
    frame_id: FrameId
    session_key: SessionKey
    ordinal: int
    membership_reason: MembershipReason
    joined_at: int

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or self.ordinal < 1:
            raise ValidationError("frame session ordinal must be positive")
        require_timestamp(self.joined_at, "joined_at")


@dataclass(frozen=True, slots=True)
class UserView:
    view_id: ViewId
    host_id: HostId
    mode: ViewMode
    active_frame_id: FrameId | None
    state: ViewState
    revision: int
    desktop_token: str
    tmux_server_id: TmuxServerId | None
    created_at: int
    last_attached_at: int | None
    updated_at: int

    def __post_init__(self) -> None:
        if isinstance(self.revision, bool) or self.revision < 0:
            raise ValidationError("view revision must be non-negative")
        if (
            self.state in {ViewState.READY, ViewState.TRANSITIONING}
            and self.active_frame_id is None
        ):
            raise ValidationError("ready/transitioning view requires an active frame")
        object.__setattr__(
            self,
            "desktop_token",
            bounded_text(self.desktop_token, "desktop_token", maximum=256),
        )
        require_timestamp(self.created_at, "created_at")
        require_timestamp(self.last_attached_at, "last_attached_at", optional=True)
        require_timestamp(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class FramePlacement:
    placement_id: PlacementId
    host_id: HostId
    view_id: ViewId
    frame_id: FrameId
    surface_id: SurfaceId | None
    state: PlacementState
    generation: int
    last_focused_at: int | None
    updated_at: int

    def __post_init__(self) -> None:
        if isinstance(self.generation, bool) or self.generation < 0:
            raise ValidationError("placement generation must be non-negative")
        require_timestamp(self.last_focused_at, "last_focused_at", optional=True)
        require_timestamp(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class TmuxServer:
    tmux_server_id: TmuxServerId
    host_id: HostId
    socket_path: str
    server_pid: int
    server_start_time: int
    observed_at: int

    def __post_init__(self) -> None:
        socket_path = bounded_text(self.socket_path, "socket_path", maximum=4_096)
        if not Path(socket_path).is_absolute():
            raise ValidationError("tmux socket path must be absolute")
        object.__setattr__(self, "socket_path", socket_path)
        if isinstance(self.server_pid, bool) or self.server_pid <= 0:
            raise ValidationError("server_pid must be positive")
        require_timestamp(self.server_start_time, "server_start_time")
        require_timestamp(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class Surface:
    surface_id: SurfaceId
    host_id: HostId
    provider: ProviderId
    session_key: SessionKey | None
    launch_id: LaunchId
    lifecycle_state: SurfaceState
    tmux_server_id: TmuxServerId | None
    pane_id: str | None
    process_id: int | None
    process_birth_id: str | None
    metadata_generation: int
    created_at: int
    updated_at: int
    retired_at: int | None

    def __post_init__(self) -> None:
        if isinstance(self.metadata_generation, bool) or self.metadata_generation < 0:
            raise ValidationError("metadata_generation must be non-negative")
        object.__setattr__(
            self, "pane_id", optional_text(self.pane_id, "pane_id", maximum=64)
        )
        object.__setattr__(
            self,
            "process_birth_id",
            optional_text(self.process_birth_id, "process_birth_id", maximum=256),
        )
        if self.process_id is not None and (
            isinstance(self.process_id, bool) or self.process_id <= 0
        ):
            raise ValidationError("process_id must be positive")
        physical = (self.tmux_server_id, self.pane_id)
        if (physical[0] is None) != (physical[1] is None):
            raise ValidationError("tmux server and pane evidence must appear together")
        require_timestamp(self.created_at, "created_at")
        require_timestamp(self.updated_at, "updated_at")
        require_timestamp(self.retired_at, "retired_at", optional=True)


@dataclass(frozen=True, slots=True)
class LaunchIntent:
    launch_id: LaunchId
    request_id: RequestId
    host_id: HostId
    frame_id: FrameId
    provider: ProviderId
    action: LaunchAction
    target_session_key: SessionKey | None
    state: LaunchState
    failure: FailureRecord | None
    created_at: int
    updated_at: int

    def __post_init__(self) -> None:
        if (self.action is LaunchAction.RESUME) != (
            self.target_session_key is not None
        ):
            raise ValidationError("resume requires and new forbids target_session_key")
        if (self.state is LaunchState.FAILED) != (self.failure is not None):
            raise ValidationError("only failed launch intent requires failure")
        require_timestamp(self.created_at, "created_at")
        require_timestamp(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class AgentCapability:
    capability_id: CapabilityId
    capability_digest: str
    host_id: HostId
    view_id: ViewId
    frame_id: FrameId
    session_key: SessionKey | None
    surface_id: SurfaceId
    launch_id: LaunchId
    tmux_server_id: TmuxServerId | None
    pane_id: str | None
    placement_generation: int
    issued_at: int
    expires_at: int
    revoked_at: int | None

    def __post_init__(self) -> None:
        require_hash(self.capability_digest, "capability_digest")
        object.__setattr__(
            self, "pane_id", optional_text(self.pane_id, "pane_id", maximum=64)
        )
        if (self.tmux_server_id is None) != (self.pane_id is None):
            raise ValidationError("capability tmux evidence must appear together")
        if isinstance(self.placement_generation, bool) or self.placement_generation < 0:
            raise ValidationError("placement_generation must be non-negative")
        require_timestamp(self.issued_at, "issued_at")
        require_timestamp(self.expires_at, "expires_at")
        require_timestamp(self.revoked_at, "revoked_at", optional=True)
        if self.expires_at <= self.issued_at:
            raise ValidationError("capability must expire after issuance")


@dataclass(frozen=True, slots=True)
class ViewTransition:
    transition_id: TransitionId
    request_id: RequestId
    request_fingerprint: str
    host_id: HostId
    view_id: ViewId
    kind: TransitionKind
    source_frame_id: FrameId | None
    target_frame_id: FrameId
    work_context_id: WorkContextId | None
    expected_view_revision: int
    expected_claim_generation: int | None
    state: TransitionState
    execution_owner: str | None
    lease_expires_at: int | None
    transport_phase: TransportPhase
    failure: FailureRecord | None
    created_at: int
    updated_at: int

    def __post_init__(self) -> None:
        require_hash(self.request_fingerprint, "request_fingerprint")
        if (
            isinstance(self.expected_view_revision, bool)
            or self.expected_view_revision < 0
        ):
            raise ValidationError("expected_view_revision must be non-negative")
        if self.expected_claim_generation is not None and (
            isinstance(self.expected_claim_generation, bool)
            or self.expected_claim_generation < 0
        ):
            raise ValidationError("expected_claim_generation must be non-negative")
        object.__setattr__(
            self,
            "execution_owner",
            optional_text(self.execution_owner, "execution_owner", maximum=128),
        )
        require_timestamp(self.lease_expires_at, "lease_expires_at", optional=True)
        require_timestamp(self.created_at, "created_at")
        require_timestamp(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class TransitionBrief:
    brief_id: BriefId
    transition_id: TransitionId
    source_frame_id: FrameId
    source_session_key: SessionKey
    target_frame_id: FrameId
    brief: str
    content_hash: str
    created_at: int
    first_claimed_at: int | None

    def __post_init__(self) -> None:
        brief = bounded_text(
            self.brief,
            "brief",
            maximum=MAX_SEMANTIC_TEXT_BYTES,
            multiline=True,
        )
        object.__setattr__(self, "brief", brief)
        require_hash(self.content_hash, "content_hash")
        if self.content_hash != content_hash(brief):
            raise ValidationError("brief content hash mismatch")
        require_timestamp(self.created_at, "created_at")
        require_timestamp(self.first_claimed_at, "first_claimed_at", optional=True)


@dataclass(frozen=True, slots=True)
class CompletionHandoff:
    handoff_id: HandoffId
    transition_id: TransitionId
    source_frame_id: FrameId
    source_session_key: SessionKey
    target_frame_id: FrameId
    summary: str
    next_action: str
    content_hash: str
    created_at: int
    first_claimed_at: int | None

    def __post_init__(self) -> None:
        summary = bounded_text(
            self.summary,
            "summary",
            maximum=MAX_SEMANTIC_TEXT_BYTES,
            multiline=True,
        )
        next_action = bounded_text(
            self.next_action,
            "next_action",
            maximum=MAX_SEMANTIC_TEXT_BYTES,
            multiline=True,
        )
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "next_action", next_action)
        require_hash(self.content_hash, "content_hash")
        if self.content_hash != content_hash(summary, next_action):
            raise ValidationError("completion handoff content hash mismatch")
        require_timestamp(self.created_at, "created_at")
        require_timestamp(self.first_claimed_at, "first_claimed_at", optional=True)


@dataclass(frozen=True, slots=True)
class ControlTurn:
    control_turn_id: ControlTurnId
    transition_id: TransitionId
    target_frame_id: FrameId
    target_session_key: SessionKey
    kind: ControlKind
    template_version: str
    transport: ControlTransport
    state: ControlState
    submission_count: int
    submitted_at: int | None
    observed_prompt_id: str | None
    claimed_at: int | None
    settled_at: int | None
    failure: FailureRecord | None

    def __post_init__(self) -> None:
        if self.template_version != "control.claim.v1":
            raise ValidationError("unsupported control-turn template")
        if self.submission_count not in {0, 1}:
            raise ValidationError("control-turn submission_count must be zero or one")
        object.__setattr__(
            self,
            "observed_prompt_id",
            optional_text(self.observed_prompt_id, "observed_prompt_id", maximum=256),
        )
        require_timestamp(self.submitted_at, "submitted_at", optional=True)
        require_timestamp(self.claimed_at, "claimed_at", optional=True)
        require_timestamp(self.settled_at, "settled_at", optional=True)


@dataclass(frozen=True, slots=True)
class Recovery:
    recovery_id: RecoveryId
    host_id: HostId
    kind: str
    subject_type: str
    subject_id: str
    actionability: RecoveryActionability
    state: RecoveryState
    bounded_explanation: str
    created_at: int
    updated_at: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "kind", bounded_text(self.kind, "recovery.kind", maximum=64)
        )
        object.__setattr__(
            self,
            "subject_type",
            bounded_text(self.subject_type, "recovery.subject_type", maximum=64),
        )
        object.__setattr__(
            self,
            "subject_id",
            bounded_text(self.subject_id, "recovery.subject_id", maximum=512),
        )
        object.__setattr__(
            self,
            "bounded_explanation",
            bounded_text(
                self.bounded_explanation, "recovery.explanation", maximum=1_024
            ),
        )
        require_timestamp(self.created_at, "created_at")
        require_timestamp(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class DesktopAttachmentLease:
    lease_id: LeaseId
    view_id: ViewId
    request_id: RequestId
    state: LeaseState
    expires_at: int

    def __post_init__(self) -> None:
        require_timestamp(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True)
class RequestRecord:
    host_id: HostId
    request_id: RequestId
    operation: str
    semantic_fingerprint: str
    state: RequestState
    result_type: str | None
    result_id: str | None
    created_at: int
    completed_at: int | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation",
            bounded_text(self.operation, "request.operation", maximum=64),
        )
        require_hash(self.semantic_fingerprint, "semantic_fingerprint")
        object.__setattr__(
            self,
            "result_type",
            optional_text(self.result_type, "request.result_type", maximum=64),
        )
        object.__setattr__(
            self,
            "result_id",
            optional_text(self.result_id, "request.result_id", maximum=512),
        )
        require_timestamp(self.created_at, "created_at")
        require_timestamp(self.completed_at, "completed_at", optional=True)


@dataclass(frozen=True, slots=True)
class HostStateCache:
    remote_name: str
    host_id: HostId
    state_json: str
    content_hash: str
    observed_at: int
    received_at: int
    last_attempt_at: int
    reachability: Reachability
    bounded_error: FailureRecord | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "remote_name",
            bounded_text(self.remote_name, "remote_name", maximum=64),
        )
        if (
            not isinstance(self.state_json, str)
            or len(self.state_json.encode("utf-8")) > 8 * 1024 * 1024
        ):
            raise ValidationError("state_json must be bounded text")
        require_hash(self.content_hash, "content_hash")
        if (
            hashlib.sha256(self.state_json.encode("utf-8")).hexdigest()
            != self.content_hash
        ):
            raise ValidationError("cached HostState hash mismatch")
        require_timestamp(self.observed_at, "observed_at")
        require_timestamp(self.received_at, "received_at")
        require_timestamp(self.last_attempt_at, "last_attempt_at")


FRAME_EDGES = {
    FrameLifecycleState.OPEN: {FrameLifecycleState.CLOSING},
    FrameLifecycleState.CLOSING: {FrameLifecycleState.CLOSED},
    FrameLifecycleState.CLOSED: {FrameLifecycleState.OPEN},
}
WORK_CONTEXT_EDGES = {
    ClaimState.RELEASED: {ClaimState.HELD, ClaimState.BLOCKED},
    ClaimState.HELD: {ClaimState.RELEASED, ClaimState.BLOCKED},
    ClaimState.BLOCKED: {ClaimState.RELEASED, ClaimState.HELD},
}
VIEW_EDGES = {
    ViewState.READY: {ViewState.TRANSITIONING, ViewState.DEGRADED, ViewState.RETIRED},
    ViewState.TRANSITIONING: {ViewState.READY, ViewState.DEGRADED},
    ViewState.DEGRADED: {ViewState.READY, ViewState.RETIRED},
    ViewState.RETIRED: set(),
}
PLACEMENT_EDGES = {
    PlacementState.STAGED: {PlacementState.ACTIVE, PlacementState.ORPHANED},
    PlacementState.ACTIVE: {
        PlacementState.PARKED,
        PlacementState.STOPPED_AFFINITY,
        PlacementState.ORPHANED,
    },
    PlacementState.PARKED: {
        PlacementState.ACTIVE,
        PlacementState.STOPPED_AFFINITY,
        PlacementState.ORPHANED,
    },
    PlacementState.STOPPED_AFFINITY: {
        PlacementState.STAGED,
        PlacementState.ORPHANED,
    },
    PlacementState.ORPHANED: set(),
}
SURFACE_EDGES = {
    SurfaceState.PLANNED: {
        SurfaceState.LIVE,
        SurfaceState.ORPHANED,
        SurfaceState.RETIRED,
    },
    SurfaceState.LIVE: {SurfaceState.DEAD, SurfaceState.ORPHANED},
    SurfaceState.DEAD: {SurfaceState.RETIRED},
    SurfaceState.ORPHANED: {SurfaceState.LIVE, SurfaceState.DEAD, SurfaceState.RETIRED},
    SurfaceState.RETIRED: set(),
}
LAUNCH_EDGES = {
    LaunchState.PLANNED: {
        LaunchState.AUTHORIZED,
        LaunchState.FAILED,
        LaunchState.SUPERSEDED,
    },
    LaunchState.AUTHORIZED: {
        LaunchState.STARTED,
        LaunchState.FAILED,
        LaunchState.SUPERSEDED,
    },
    LaunchState.STARTED: {LaunchState.BOUND, LaunchState.FAILED},
    LaunchState.BOUND: set(),
    LaunchState.FAILED: set(),
    LaunchState.SUPERSEDED: set(),
}
TRANSITION_EDGES = {
    TransitionState.PREPARED: {
        TransitionState.EXECUTING,
        TransitionState.CANCELLED,
        TransitionState.SUPERSEDED,
        TransitionState.FAILED,
    },
    TransitionState.EXECUTING: {TransitionState.PRESENTED, TransitionState.FAILED},
    TransitionState.PRESENTED: {
        TransitionState.AWAITING_CLAIM,
        TransitionState.SETTLING,
        TransitionState.FAILED,
    },
    TransitionState.AWAITING_CLAIM: {TransitionState.SETTLING, TransitionState.FAILED},
    TransitionState.SETTLING: {TransitionState.COMPLETED, TransitionState.FAILED},
    TransitionState.COMPLETED: set(),
    TransitionState.CANCELLED: set(),
    TransitionState.SUPERSEDED: set(),
    TransitionState.FAILED: set(),
}
TRANSPORT_EDGES = {
    TransportPhase.INTENT: {TransportPhase.MOVED, TransportPhase.INSPECTED},
    TransportPhase.MOVED: {TransportPhase.INSPECTED, TransportPhase.ROLLED_BACK},
    TransportPhase.INSPECTED: {TransportPhase.COMMITTED, TransportPhase.ROLLED_BACK},
    TransportPhase.COMMITTED: set(),
    TransportPhase.ROLLED_BACK: set(),
}
CONTROL_EDGES = {
    ControlState.PREPARED: {
        ControlState.SUBMITTED,
        ControlState.FAILED,
        ControlState.SUPERSEDED,
    },
    ControlState.SUBMITTED: {ControlState.OBSERVED, ControlState.UNCERTAIN},
    ControlState.OBSERVED: {
        ControlState.CLAIMED,
        ControlState.UNCERTAIN,
        ControlState.FAILED,
    },
    ControlState.UNCERTAIN: {
        ControlState.OBSERVED,
        ControlState.CLAIMED,
        ControlState.FAILED,
    },
    ControlState.CLAIMED: {ControlState.SETTLED, ControlState.FAILED},
    ControlState.SETTLED: set(),
    ControlState.FAILED: set(),
    ControlState.SUPERSEDED: set(),
}
RECOVERY_EDGES = {
    RecoveryState.OPEN: {RecoveryState.RESOLVED, RecoveryState.DISMISSED},
    RecoveryState.RESOLVED: set(),
    RecoveryState.DISMISSED: set(),
}
LEASE_EDGES = {
    LeaseState.OFFERED: {LeaseState.CLAIMED, LeaseState.EXPIRED},
    LeaseState.CLAIMED: set(),
    LeaseState.EXPIRED: set(),
}
REQUEST_EDGES = {
    RequestState.PREPARED: {RequestState.COMPLETED, RequestState.FAILED},
    RequestState.COMPLETED: set(),
    RequestState.FAILED: set(),
}


def require_state_edge[T: StrEnum](
    current: T,
    target: T,
    edges: Mapping[T, set[T]],
    field: str,
) -> None:
    if target not in edges.get(current, set()):
        raise StateTransitionError(f"illegal {field} edge: {current} -> {target}")


__all__ = [name for name in globals() if not name.startswith("_")]
