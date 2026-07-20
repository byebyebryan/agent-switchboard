"""Validated, provider-neutral domain objects.

This module deliberately contains no subprocess or persistence behavior.  The
objects are safe values shared by storage, provider adapters, and frontends.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Self
from uuid import UUID, uuid4

MAX_NAME_LENGTH = 256
MAX_ALIAS_LENGTH = 128
MAX_HANDOFF_FIELD_BYTES = 64 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ValidationError(ValueError):
    """A domain value violates a stable core invariant."""


class ProjectConflictError(ValidationError):
    """Project declarations with one stable ID disagree."""

    def __init__(self, project_id: ProjectId, fields: tuple[str, ...]) -> None:
        self.project_id = project_id
        self.fields = fields
        super().__init__(
            f"project {project_id} has conflicting fields: {', '.join(fields)}"
        )


class LocationConflictError(ValidationError):
    """Location declarations with one stable ID disagree."""


class AmbiguousLocationError(ValidationError):
    """A path has more than one equally specific configured location."""

    def __init__(self, path: Path, locations: tuple[ProjectLocation, ...]) -> None:
        self.path = path
        self.locations = locations
        ids = ", ".join(str(location.location_id) for location in locations)
        super().__init__(f"path {path} matches equally specific locations: {ids}")


class LaunchTransitionError(ValidationError):
    """A launch intent transition is not legal."""


class LeaseError(ValidationError):
    """A launch intent lease is missing, stale, or owned by someone else."""


class RequestConflictError(ValidationError):
    """One request ID was reused for a different normalized launch request."""


@dataclass(frozen=True, slots=True, order=True)
class UUIDId:
    """Base for type-distinct, non-nil UUID identifiers."""

    value: UUID

    def __init__(self, value: UUID | str) -> None:
        try:
            parsed = value if isinstance(value, UUID) else UUID(str(value))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValidationError(f"invalid UUID: {value!r}") from exc
        if parsed.int == 0:
            raise ValidationError("nil UUID is not a valid stable identifier")
        object.__setattr__(self, "value", parsed)

    @classmethod
    def new(cls) -> Self:
        return cls(uuid4())

    def __str__(self) -> str:
        return str(self.value)


class HostId(UUIDId):
    __slots__ = ()


class ProjectId(UUIDId):
    __slots__ = ()


class LocationId(UUIDId):
    __slots__ = ()


class LaunchId(UUIDId):
    __slots__ = ()


class HandoffId(UUIDId):
    __slots__ = ()


class SurfaceId(UUIDId):
    __slots__ = ()


class ProviderId(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"


class Transport(StrEnum):
    TMUX = "tmux"


class LaunchAction(StrEnum):
    NEW = "new"
    RESUME = "resume"
    ATTACH = "attach"
    HISTORY = "history"
    MANAGE = "manage"


class LaunchState(StrEnum):
    RESERVED = "reserved"
    SURFACE_READY = "surface_ready"
    WAITING_FOR_CLIENT = "waiting_for_client"
    PROVIDER_STARTED = "provider_started"
    BOUND = "bound"
    MANAGER_READY = "manager_ready"
    FAILED = "failed"
    EXPIRED = "expired"


class HandoffSource(StrEnum):
    USER = "user"
    AGENT = "agent"
    IMPORTED = "imported"


class SurfaceRole(StrEnum):
    SESSION = "session"
    PROVIDER_MANAGER = "provider_manager"


class BindingConfidence(StrEnum):
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


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


class Attachment(StrEnum):
    ATTACHED = "attached"
    DETACHED = "detached"
    NONE = "none"
    UNKNOWN = "unknown"


class StateConfidence(StrEnum):
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class LaunchRequestKind(StrEnum):
    OPEN = "open"
    NEW = "new"
    HISTORY = "history"
    MANAGE = "manage"


def _enum_value[T: StrEnum](enum_type: type[T], value: T | str, field: str) -> T:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"invalid {field}: {value!r}") from exc


def _text(value: str, field: str, *, maximum: int = MAX_NAME_LENGTH) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise ValidationError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise ValidationError(f"{field} exceeds {maximum} characters")
    if any(unicodedata.category(char) == "Cc" for char in normalized):
        raise ValidationError(f"{field} contains control characters")
    return normalized


def sanitize_alias(value: str) -> str:
    """Normalize a user-facing alias without turning it into an identifier."""

    value = _text(value, "alias", maximum=MAX_ALIAS_LENGTH)
    return " ".join(value.split())


def sanitize_aliases(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    by_key: dict[str, str] = {}
    for value in values:
        alias = sanitize_alias(value)
        by_key.setdefault(alias.casefold(), alias)
    return tuple(by_key[key] for key in sorted(by_key))


def canonical_path(value: str | Path) -> Path:
    """Return an absolute path, resolving existing symlinks and lexical tails."""

    if not isinstance(value, (str, Path)):
        raise ValidationError("path must be a string or pathlib.Path")
    text = str(value)
    if any(unicodedata.category(char) == "Cc" for char in text):
        raise ValidationError("path contains control characters")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValidationError(f"path must be absolute: {value!r}")
    return path.resolve(strict=False)


def validate_context_source(value: str) -> str:
    """Validate one stable, project-relative context file or directory."""

    value = _text(value, "context source", maximum=1024)
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value in {".", ""}:
        raise ValidationError(
            f"context source must be a project-relative path: {value!r}"
        )
    return path.as_posix()


def _aware(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Project:
    project_id: ProjectId
    name: str
    aliases: tuple[str, ...] = ()
    default_provider: ProviderId | None = None
    default_transport: Transport = Transport.TMUX
    context_sources: tuple[str, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, ProjectId):
            object.__setattr__(self, "project_id", ProjectId(self.project_id))
        object.__setattr__(self, "name", _text(self.name, "project name"))
        object.__setattr__(self, "aliases", sanitize_aliases(list(self.aliases)))
        if self.default_provider is not None:
            object.__setattr__(
                self,
                "default_provider",
                _enum_value(ProviderId, self.default_provider, "default provider"),
            )
        object.__setattr__(
            self,
            "default_transport",
            _enum_value(Transport, self.default_transport, "default transport"),
        )
        context_sources = tuple(
            dict.fromkeys(
                validate_context_source(value) for value in self.context_sources
            )
        )
        object.__setattr__(self, "context_sources", context_sources)
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _aware(self.updated_at, "updated_at"))


@dataclass(frozen=True, slots=True)
class ProjectLocation:
    location_id: LocationId
    project_id: ProjectId
    host_id: HostId
    path: Path
    display_name: str | None = None
    repository_identity: str | None = None
    provider_override: ProviderId | None = None
    transport_override: Transport | None = None
    is_default: bool = False
    last_observed_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name, field_type in (
            ("location_id", LocationId),
            ("project_id", ProjectId),
            ("host_id", HostId),
        ):
            value = getattr(self, field_name)
            if not isinstance(value, field_type):
                object.__setattr__(self, field_name, field_type(value))
        object.__setattr__(self, "path", canonical_path(self.path))
        if self.display_name is not None:
            object.__setattr__(
                self, "display_name", _text(self.display_name, "location display name")
            )
        if self.repository_identity is not None:
            object.__setattr__(
                self,
                "repository_identity",
                _text(self.repository_identity, "repository identity", maximum=1024),
            )
        if self.provider_override is not None:
            object.__setattr__(
                self,
                "provider_override",
                _enum_value(ProviderId, self.provider_override, "provider override"),
            )
        if self.transport_override is not None:
            object.__setattr__(
                self,
                "transport_override",
                _enum_value(Transport, self.transport_override, "transport override"),
            )
        if not isinstance(self.is_default, bool):
            raise ValidationError("is_default must be boolean")
        object.__setattr__(
            self,
            "last_observed_at",
            _aware(self.last_observed_at, "last_observed_at"),
        )


@dataclass(frozen=True, slots=True)
class SessionKey:
    host_id: HostId
    provider: ProviderId
    provider_session_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.host_id, HostId):
            object.__setattr__(self, "host_id", HostId(self.host_id))
        object.__setattr__(
            self, "provider", _enum_value(ProviderId, self.provider, "provider")
        )
        try:
            session_id = (
                self.provider_session_id
                if isinstance(self.provider_session_id, UUID)
                else UUID(str(self.provider_session_id))
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError("provider_session_id must be a UUID") from exc
        if session_id.int == 0:
            raise ValidationError("provider_session_id must not be nil")
        object.__setattr__(self, "provider_session_id", session_id)

    def __str__(self) -> str:
        return f"{self.host_id}:{self.provider}:{self.provider_session_id}"

    @classmethod
    def parse(cls, value: str) -> Self:
        if not isinstance(value, str):
            raise ValidationError("session key must be a string")
        parts = value.split(":")
        if len(parts) != 3:
            raise ValidationError("session key must contain host, provider, and UUID")
        return cls(HostId(parts[0]), ProviderId(parts[1]), UUID(parts[2]))


@dataclass(frozen=True, slots=True)
class RuntimeLocator:
    pid: int | None = None
    provider_runtime_id: str | None = None
    tmux_session: str | None = None
    tmux_window: str | None = None
    tmux_pane: str | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.pid is not None and (isinstance(self.pid, bool) or self.pid <= 0):
            raise ValidationError("pid must be a positive integer")
        for field_name in (
            "provider_runtime_id",
            "tmux_session",
            "tmux_window",
            "tmux_pane",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self, field_name, _text(value, field_name, maximum=1024)
                )
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))


@dataclass(frozen=True, slots=True)
class NormalizedRuntimeObservation:
    """Provider-neutral, privacy-safe evidence from a bounded live probe.

    Optional state axes mean "not observed" rather than ``unknown``.  The
    ``tmux_observed`` bit makes that distinction explicit for locator fields:
    a successful probe may authoritatively clear a stale pane, while a failed
    probe must leave the retained pane association untouched.
    """

    observation_key: str
    host_id: HostId
    provider: ProviderId
    session_key: SessionKey
    source: str
    source_priority: int
    entry_ns: int
    observed_at: int
    runtime_presence: RuntimePresence | None = None
    resumability: Resumability | None = None
    activity: Activity | None = None
    activity_reason: ActivityReason | None = None
    attachment: Attachment | None = None
    pid: int | None = None
    process_birth_id: str | None = None
    provider_runtime_id: str | None = None
    tmux_observed: bool = False
    tmux_socket: str | None = None
    tmux_session: str | None = None
    tmux_window: str | None = None
    tmux_pane: str | None = None
    launch_id: LaunchId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.host_id, HostId):
            object.__setattr__(self, "host_id", HostId(self.host_id))
        object.__setattr__(
            self, "provider", _enum_value(ProviderId, self.provider, "provider")
        )
        if not isinstance(self.session_key, SessionKey):
            object.__setattr__(self, "session_key", SessionKey.parse(self.session_key))
        if (
            self.session_key.host_id != self.host_id
            or self.session_key.provider is not self.provider
        ):
            raise ValidationError("runtime observation routing fields disagree")
        object.__setattr__(
            self,
            "observation_key",
            _text(self.observation_key, "observation_key", maximum=256),
        )
        object.__setattr__(
            self,
            "source",
            _text(self.source, "source", maximum=64),
        )
        for field_name in ("source_priority", "entry_ns", "observed_at"):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > 2**63 - 1
            ):
                raise ValidationError(f"{field_name} must be a non-negative integer")
        if self.source_priority > 1_000_000:
            raise ValidationError("source_priority exceeds the supported range")
        for field_name, enum_type in (
            ("runtime_presence", RuntimePresence),
            ("resumability", Resumability),
            ("activity", Activity),
            ("activity_reason", ActivityReason),
            ("attachment", Attachment),
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self, field_name, _enum_value(enum_type, value, field_name)
                )
        if self.pid is not None and (
            isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0
        ):
            raise ValidationError("pid must be a positive integer")
        if self.process_birth_id is not None and (
            len(self.process_birth_id) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.process_birth_id
            )
        ):
            raise ValidationError(
                "process_birth_id must be an opaque lowercase SHA-256 digest"
            )
        for field_name in (
            "provider_runtime_id",
            "tmux_session",
            "tmux_window",
            "tmux_pane",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self, field_name, _text(value, field_name, maximum=256)
                )
        if self.tmux_socket is not None:
            socket = _text(self.tmux_socket, "tmux_socket", maximum=4096)
            if not Path(socket).is_absolute():
                raise ValidationError("tmux_socket must be an absolute path")
            object.__setattr__(self, "tmux_socket", socket)
        if not isinstance(self.tmux_observed, bool):
            raise ValidationError("tmux_observed must be boolean")
        if not self.tmux_observed and any(
            value is not None
            for value in (
                self.attachment,
                self.tmux_socket,
                self.tmux_session,
                self.tmux_window,
                self.tmux_pane,
            )
        ):
            raise ValidationError("tmux evidence requires tmux_observed")
        if self.activity is None and self.activity_reason is not None:
            raise ValidationError("activity_reason requires activity evidence")
        if self.launch_id is not None and not isinstance(self.launch_id, LaunchId):
            object.__setattr__(self, "launch_id", LaunchId(self.launch_id))

    def storage_mapping(self) -> dict[str, Any]:
        """Return the private normalized mapping accepted by the registry."""

        return {
            "observation_key": self.observation_key,
            "host_id": str(self.host_id),
            "provider": self.provider.value,
            "session_key": str(self.session_key),
            "source": self.source,
            "source_priority": self.source_priority,
            "entry_ns": self.entry_ns,
            "observed_at": self.observed_at,
            "received_at": self.observed_at,
            "runtime_presence": (
                None if self.runtime_presence is None else self.runtime_presence.value
            ),
            "resumability": (
                None if self.resumability is None else self.resumability.value
            ),
            "activity": None if self.activity is None else self.activity.value,
            "activity_reason": (
                None if self.activity_reason is None else self.activity_reason.value
            ),
            "attachment": (None if self.attachment is None else self.attachment.value),
            "pid": self.pid,
            "process_birth_id": self.process_birth_id,
            "provider_runtime_id": self.provider_runtime_id,
            "tmux_observed": self.tmux_observed,
            "tmux_socket": self.tmux_socket,
            "tmux_session": self.tmux_session,
            "tmux_window": self.tmux_window,
            "tmux_pane": self.tmux_pane,
            "launch_id": None if self.launch_id is None else str(self.launch_id),
        }


@dataclass(frozen=True, slots=True)
class AgentSession:
    key: SessionKey
    cwd: Path
    project_id: ProjectId | None = None
    location_id: LocationId | None = None
    name: str | None = None
    purpose: str | None = None
    created_at: datetime | None = None
    provider_updated_at: datetime | None = None
    last_activity_at: datetime | None = None
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    runtime_presence: RuntimePresence = RuntimePresence.UNKNOWN
    resumability: Resumability = Resumability.UNKNOWN
    activity: Activity = Activity.UNKNOWN
    activity_reason: ActivityReason = ActivityReason.UNKNOWN
    attachment: Attachment = Attachment.UNKNOWN
    runtime_locator: RuntimeLocator | None = None
    surface_id: SurfaceId | None = None
    metadata_source: str | None = None
    state_confidence: StateConfidence = StateConfidence.UNKNOWN
    state_observed_at: datetime | None = None
    latest_handoff_id: HandoffId | None = None
    wrapped_at: datetime | None = None
    continued_from_handoff_id: HandoffId | None = None
    pinned: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.key, SessionKey):
            raise ValidationError("key must be a SessionKey")
        object.__setattr__(self, "cwd", canonical_path(self.cwd))
        for field_name, field_type in (
            ("project_id", ProjectId),
            ("location_id", LocationId),
            ("surface_id", SurfaceId),
            ("latest_handoff_id", HandoffId),
            ("continued_from_handoff_id", HandoffId),
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, field_type):
                object.__setattr__(self, field_name, field_type(value))
        for field_name in ("name", "purpose", "metadata_source"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self, field_name, _text(value, field_name, maximum=4096)
                )
        for field_name, enum_type in (
            ("runtime_presence", RuntimePresence),
            ("resumability", Resumability),
            ("activity", Activity),
            ("activity_reason", ActivityReason),
            ("attachment", Attachment),
            ("state_confidence", StateConfidence),
        ):
            object.__setattr__(
                self,
                field_name,
                _enum_value(enum_type, getattr(self, field_name), field_name),
            )
        for field_name in (
            "created_at",
            "provider_updated_at",
            "last_activity_at",
            "first_observed_at",
            "last_observed_at",
            "state_observed_at",
            "wrapped_at",
        ):
            object.__setattr__(
                self, field_name, _aware(getattr(self, field_name), field_name)
            )
        if self.runtime_locator is not None and not isinstance(
            self.runtime_locator, RuntimeLocator
        ):
            raise ValidationError("runtime_locator must be a RuntimeLocator")
        if not isinstance(self.pinned, bool):
            raise ValidationError("pinned must be boolean")

    @property
    def host_id(self) -> HostId:
        return self.key.host_id

    @property
    def provider(self) -> ProviderId:
        return self.key.provider

    @property
    def provider_session_id(self) -> UUID:
        return self.key.provider_session_id


def normalize_handoff_text(value: str, field: str) -> str:
    """Normalize one bounded explicit handoff field for storage and transport."""

    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise ValidationError(f"{field} must not be empty")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValidationError(f"{field} contains invalid Unicode") from error
    if len(encoded) > MAX_HANDOFF_FIELD_BYTES:
        raise ValidationError(f"{field} exceeds {MAX_HANDOFF_FIELD_BYTES} bytes")
    if any(
        unicodedata.category(char) == "Cc" and char not in "\n\t" for char in normalized
    ):
        raise ValidationError(f"{field} contains disallowed control characters")
    return normalized


def handoff_content_hash(summary: str, next_action: str) -> str:
    summary = normalize_handoff_text(summary, "summary")
    next_action = normalize_handoff_text(next_action, "next_action")
    payload = json.dumps(
        {"nextAction": next_action, "summary": summary},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class Handoff:
    handoff_id: HandoffId
    session_key: SessionKey
    sequence: int
    summary: str
    next_action: str
    source: HandoffSource
    source_host_id: HostId
    created_at: datetime
    content_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.handoff_id, HandoffId):
            object.__setattr__(self, "handoff_id", HandoffId(self.handoff_id))
        if not isinstance(self.session_key, SessionKey):
            raise ValidationError("session_key must be a SessionKey")
        if isinstance(self.sequence, bool) or self.sequence < 1:
            raise ValidationError("handoff sequence must be a positive integer")
        summary = normalize_handoff_text(self.summary, "summary")
        next_action = normalize_handoff_text(self.next_action, "next_action")
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "next_action", next_action)
        object.__setattr__(
            self, "source", _enum_value(HandoffSource, self.source, "handoff source")
        )
        if not isinstance(self.source_host_id, HostId):
            object.__setattr__(self, "source_host_id", HostId(self.source_host_id))
        created_at = _aware(self.created_at, "created_at")
        assert created_at is not None
        object.__setattr__(self, "created_at", created_at)
        if not isinstance(self.content_hash, str) or not _SHA256_RE.fullmatch(
            self.content_hash
        ):
            raise ValidationError("content_hash must be a lowercase SHA-256 digest")
        if self.content_hash != handoff_content_hash(summary, next_action):
            raise ValidationError("content_hash does not match handoff content")

    @classmethod
    def create(
        cls,
        *,
        session_key: SessionKey,
        sequence: int,
        summary: str,
        next_action: str,
        source: HandoffSource,
        source_host_id: HostId,
        created_at: datetime,
        handoff_id: HandoffId | None = None,
    ) -> Self:
        summary = normalize_handoff_text(summary, "summary")
        next_action = normalize_handoff_text(next_action, "next_action")
        return cls(
            handoff_id=handoff_id or HandoffId.new(),
            session_key=session_key,
            sequence=sequence,
            summary=summary,
            next_action=next_action,
            source=source,
            source_host_id=source_host_id,
            created_at=created_at,
            content_hash=handoff_content_hash(summary, next_action),
        )


@dataclass(frozen=True, slots=True)
class Surface:
    surface_id: SurfaceId
    host_id: HostId
    provider: ProviderId
    transport: Transport
    transport_locator: str
    role: SurfaceRole
    current_session_key: SessionKey | None = None
    binding_confidence: BindingConfidence = BindingConfidence.UNKNOWN
    launch_id: LaunchId | None = None
    created_at: datetime | None = None
    last_observed_at: datetime | None = None
    client_attached: bool | None = None

    def __post_init__(self) -> None:
        for field_name, field_type in (
            ("surface_id", SurfaceId),
            ("host_id", HostId),
            ("launch_id", LaunchId),
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, field_type):
                object.__setattr__(self, field_name, field_type(value))
        object.__setattr__(
            self, "provider", _enum_value(ProviderId, self.provider, "provider")
        )
        object.__setattr__(
            self, "transport", _enum_value(Transport, self.transport, "transport")
        )
        object.__setattr__(
            self, "role", _enum_value(SurfaceRole, self.role, "surface role")
        )
        object.__setattr__(
            self,
            "binding_confidence",
            _enum_value(
                BindingConfidence, self.binding_confidence, "binding confidence"
            ),
        )
        object.__setattr__(
            self,
            "transport_locator",
            _text(self.transport_locator, "transport locator", maximum=2048),
        )
        if self.current_session_key is not None and not isinstance(
            self.current_session_key, SessionKey
        ):
            raise ValidationError("current_session_key must be a SessionKey")
        if self.role is SurfaceRole.PROVIDER_MANAGER and (
            self.current_session_key
            or self.binding_confidence is not BindingConfidence.UNKNOWN
        ):
            raise ValidationError("a provider manager surface cannot bind a session")
        if (
            self.current_session_key
            and self.current_session_key.host_id != self.host_id
        ):
            raise ValidationError("surface and current session must belong to one host")
        if (
            self.current_session_key
            and self.current_session_key.provider is not self.provider
        ):
            raise ValidationError(
                "surface and current session must belong to one provider"
            )
        if (
            self.binding_confidence is BindingConfidence.CONFIRMED
            and self.current_session_key is None
        ):
            raise ValidationError("confirmed surface binding requires a session")
        for field_name in ("created_at", "last_observed_at"):
            object.__setattr__(
                self, field_name, _aware(getattr(self, field_name), field_name)
            )
        if self.client_attached is not None and not isinstance(
            self.client_attached, bool
        ):
            raise ValidationError("client_attached must be boolean or null")


_TRANSITIONS: dict[LaunchState, frozenset[LaunchState]] = {
    LaunchState.RESERVED: frozenset(
        {LaunchState.SURFACE_READY, LaunchState.FAILED, LaunchState.EXPIRED}
    ),
    LaunchState.SURFACE_READY: frozenset(
        {LaunchState.WAITING_FOR_CLIENT, LaunchState.FAILED, LaunchState.EXPIRED}
    ),
    LaunchState.WAITING_FOR_CLIENT: frozenset(
        {LaunchState.PROVIDER_STARTED, LaunchState.FAILED, LaunchState.EXPIRED}
    ),
    LaunchState.PROVIDER_STARTED: frozenset(
        {
            LaunchState.BOUND,
            LaunchState.MANAGER_READY,
            LaunchState.FAILED,
            LaunchState.EXPIRED,
        }
    ),
    LaunchState.BOUND: frozenset(),
    LaunchState.MANAGER_READY: frozenset({LaunchState.FAILED, LaunchState.EXPIRED}),
    LaunchState.FAILED: frozenset(),
    LaunchState.EXPIRED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class LaunchIntent:
    launch_id: LaunchId
    request_id: UUID
    host_id: HostId
    provider: ProviderId
    action: LaunchAction
    project_id: ProjectId | None
    location_id: LocationId | None
    cwd: Path | None
    source_handoff_id: HandoffId | None
    target_session_key: SessionKey | None
    surface_id: SurfaceId | None
    transport: Transport
    state: LaunchState
    lease_owner: str | None
    capability_hash: str
    created_at: datetime
    expires_at: datetime
    agent_capability_hash: str | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        for field_name, field_type in (
            ("launch_id", LaunchId),
            ("host_id", HostId),
            ("project_id", ProjectId),
            ("location_id", LocationId),
            ("source_handoff_id", HandoffId),
            ("surface_id", SurfaceId),
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, field_type):
                object.__setattr__(self, field_name, field_type(value))
        try:
            request_id = (
                self.request_id
                if isinstance(self.request_id, UUID)
                else UUID(str(self.request_id))
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError("request_id must be a UUID") from exc
        if request_id.int == 0:
            raise ValidationError("request_id must not be nil")
        object.__setattr__(self, "request_id", request_id)
        for field_name, enum_type in (
            ("provider", ProviderId),
            ("action", LaunchAction),
            ("transport", Transport),
            ("state", LaunchState),
        ):
            object.__setattr__(
                self,
                field_name,
                _enum_value(enum_type, getattr(self, field_name), field_name),
            )
        if self.cwd is not None:
            object.__setattr__(self, "cwd", canonical_path(self.cwd))
        if self.target_session_key is not None:
            if not isinstance(self.target_session_key, SessionKey):
                raise ValidationError("target_session_key must be a SessionKey")
            if self.target_session_key.host_id != self.host_id:
                raise ValidationError("target session belongs to a different host")
            if self.target_session_key.provider is not self.provider:
                raise ValidationError("target session belongs to a different provider")
        if self.action in {LaunchAction.NEW, LaunchAction.HISTORY}:
            if not all((self.project_id, self.location_id, self.cwd)):
                raise ValidationError(
                    f"{self.action} launch requires project, location, and cwd"
                )
            if self.action is LaunchAction.HISTORY and (
                self.provider is not ProviderId.CLAUDE
                or self.source_handoff_id is not None
            ):
                raise ValidationError(
                    "history launch requires Claude without a source handoff"
                )
            if (
                self.target_session_key is not None
                and self.state is not LaunchState.BOUND
            ):
                raise ValidationError(
                    f"{self.action} launch receives a target session only when bound"
                )
        elif self.action in {LaunchAction.RESUME, LaunchAction.ATTACH}:
            if self.target_session_key is None:
                raise ValidationError(f"{self.action} launch requires a target session")
        elif self.action is LaunchAction.MANAGE:
            if any(
                (
                    self.project_id,
                    self.location_id,
                    self.cwd,
                    self.source_handoff_id,
                    self.target_session_key,
                )
            ):
                raise ValidationError(
                    "manage launch cannot target project/session context"
                )
        if self.state is LaunchState.BOUND and self.action is LaunchAction.MANAGE:
            raise ValidationError("manage launch cannot enter bound state")
        if (
            self.state is LaunchState.MANAGER_READY
            and self.action is not LaunchAction.MANAGE
        ):
            raise ValidationError("only manage launch can enter manager_ready")
        if self.state is LaunchState.RESERVED and self.surface_id is not None:
            raise ValidationError("reserved launch cannot already have a surface")
        if (
            self.state
            in {
                LaunchState.SURFACE_READY,
                LaunchState.WAITING_FOR_CLIENT,
                LaunchState.PROVIDER_STARTED,
                LaunchState.BOUND,
                LaunchState.MANAGER_READY,
            }
            and self.surface_id is None
        ):
            raise ValidationError(f"{self.state} launch requires a surface")
        if self.state is LaunchState.FAILED:
            if self.failure_code is None:
                raise ValidationError("failed launch requires failure_code")
            object.__setattr__(
                self,
                "failure_code",
                _text(self.failure_code, "failure_code", maximum=128),
            )
        elif self.failure_code is not None:
            raise ValidationError("failure_code is only valid for failed launch")
        if (
            self.state
            not in {
                LaunchState.BOUND,
                LaunchState.FAILED,
                LaunchState.EXPIRED,
            }
            and not self.lease_owner
        ):
            raise ValidationError("active launch requires lease_owner")
        if (
            self.state in {LaunchState.BOUND, LaunchState.FAILED, LaunchState.EXPIRED}
            and self.lease_owner is not None
        ):
            raise ValidationError("terminal launch cannot retain a lease owner")
        if self.lease_owner is not None:
            object.__setattr__(
                self, "lease_owner", _text(self.lease_owner, "lease_owner", maximum=256)
            )
        if not isinstance(self.capability_hash, str) or not _SHA256_RE.fullmatch(
            self.capability_hash
        ):
            raise ValidationError("capability_hash must be a lowercase SHA-256 digest")
        if self.agent_capability_hash is not None and (
            not isinstance(self.agent_capability_hash, str)
            or not _SHA256_RE.fullmatch(self.agent_capability_hash)
        ):
            raise ValidationError(
                "agent_capability_hash must be a lowercase SHA-256 digest"
            )
        created_at = _aware(self.created_at, "created_at")
        expires_at = _aware(self.expires_at, "expires_at")
        assert created_at is not None and expires_at is not None
        if expires_at <= created_at:
            raise ValidationError("expires_at must be after created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)

    def lease_expired(self, now: datetime) -> bool:
        normalized = _aware(now, "now")
        assert normalized is not None
        return normalized >= self.expires_at

    def assert_lease(self, owner: str, now: datetime) -> None:
        owner = _text(owner, "lease owner", maximum=256)
        if self.lease_owner != owner:
            raise LeaseError("launch lease is owned by a different worker")
        if self.lease_expired(now):
            raise LeaseError("launch lease has expired")

    def renew_lease(self, owner: str, expires_at: datetime, now: datetime) -> Self:
        self.assert_lease(owner, now)
        normalized = _aware(expires_at, "expires_at")
        current = _aware(now, "now")
        assert normalized is not None and current is not None
        if normalized <= current:
            raise LeaseError("renewed lease must expire in the future")
        return replace(self, expires_at=normalized)

    def transition(
        self,
        state: LaunchState,
        *,
        now: datetime,
        owner: str | None = None,
        surface_id: SurfaceId | str | None = None,
        failure_code: str | None = None,
    ) -> Self:
        state = _enum_value(LaunchState, state, "launch state")
        if state not in _TRANSITIONS[self.state]:
            raise LaunchTransitionError(f"cannot transition {self.state} to {state}")
        if state is LaunchState.EXPIRED:
            if not self.lease_expired(now):
                raise LeaseError("cannot expire a live lease")
        else:
            if owner is None:
                raise LeaseError("transition requires the lease owner")
            self.assert_lease(owner, now)
        if state is LaunchState.BOUND and self.action is LaunchAction.MANAGE:
            raise LaunchTransitionError("manage launch cannot bind a session")
        if state is LaunchState.BOUND and self.action in {
            LaunchAction.NEW,
            LaunchAction.HISTORY,
        }:
            raise LaunchTransitionError(
                "unbound launch binding requires the provider session identity"
            )
        if (
            state is LaunchState.MANAGER_READY
            and self.action is not LaunchAction.MANAGE
        ):
            raise LaunchTransitionError("only manage launch reaches manager_ready")
        next_surface_id = self.surface_id
        if surface_id is not None:
            supplied_surface_id = (
                surface_id
                if isinstance(surface_id, SurfaceId)
                else SurfaceId(surface_id)
            )
            if self.surface_id is not None and supplied_surface_id != self.surface_id:
                raise LaunchTransitionError("launch surface cannot be replaced")
            if self.surface_id is None and state is not LaunchState.SURFACE_READY:
                raise LaunchTransitionError(
                    "launch surface can only be assigned by surface_ready"
                )
            next_surface_id = supplied_surface_id
        if (
            state
            in {
                LaunchState.SURFACE_READY,
                LaunchState.WAITING_FOR_CLIENT,
                LaunchState.PROVIDER_STARTED,
                LaunchState.BOUND,
                LaunchState.MANAGER_READY,
            }
            and next_surface_id is None
        ):
            raise LaunchTransitionError(f"{state} transition requires a surface")
        terminal = state in {
            LaunchState.BOUND,
            LaunchState.FAILED,
            LaunchState.EXPIRED,
        }
        return replace(
            self,
            state=state,
            surface_id=next_surface_id,
            lease_owner=None if terminal else self.lease_owner,
            failure_code=failure_code if state is LaunchState.FAILED else None,
        )

    def bind_target(
        self,
        target_session_key: SessionKey,
        *,
        now: datetime,
        owner: str,
    ) -> Self:
        """Atomically model the provider identity supplied for a new launch."""

        if self.action not in {LaunchAction.NEW, LaunchAction.HISTORY}:
            raise LaunchTransitionError(
                "only a new or history launch receives a bound target"
            )
        if self.state is not LaunchState.PROVIDER_STARTED:
            raise LaunchTransitionError(
                "launch must be provider_started before binding"
            )
        if not isinstance(target_session_key, SessionKey):
            raise ValidationError("target_session_key must be a SessionKey")
        if (
            target_session_key.host_id != self.host_id
            or target_session_key.provider is not self.provider
        ):
            raise ValidationError("target session does not match host/provider")
        self.assert_lease(owner, now)
        return replace(
            self,
            target_session_key=target_session_key,
            state=LaunchState.BOUND,
            lease_owner=None,
        )


@dataclass(frozen=True, slots=True)
class LaunchRequest:
    kind: LaunchRequestKind
    host_id: HostId
    provider: ProviderId
    target_session_key: SessionKey | None = None
    project_id: ProjectId | None = None
    location_id: LocationId | None = None
    cwd: Path | None = None
    source_handoff_id: HandoffId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "kind", _enum_value(LaunchRequestKind, self.kind, "request kind")
        )
        if not isinstance(self.host_id, HostId):
            object.__setattr__(self, "host_id", HostId(self.host_id))
        object.__setattr__(
            self, "provider", _enum_value(ProviderId, self.provider, "provider")
        )
        for field_name, field_type in (
            ("project_id", ProjectId),
            ("location_id", LocationId),
            ("source_handoff_id", HandoffId),
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, field_type):
                object.__setattr__(self, field_name, field_type(value))
        if self.cwd is not None:
            object.__setattr__(self, "cwd", canonical_path(self.cwd))
        if self.target_session_key is not None:
            if not isinstance(self.target_session_key, SessionKey):
                raise ValidationError("target_session_key must be a SessionKey")
            if (
                self.target_session_key.host_id != self.host_id
                or self.target_session_key.provider is not self.provider
            ):
                raise ValidationError("target session does not match host/provider")
        if self.kind is LaunchRequestKind.OPEN:
            if self.target_session_key is None:
                raise ValidationError("open request requires target_session_key")
            if any(
                (self.project_id, self.location_id, self.cwd, self.source_handoff_id)
            ):
                raise ValidationError("open request cannot include new-session context")
        elif self.kind is LaunchRequestKind.NEW:
            if not all((self.project_id, self.location_id, self.cwd)):
                raise ValidationError("new request requires project, location, and cwd")
            if self.target_session_key is not None:
                raise ValidationError("new request cannot target an existing session")
        elif self.kind is LaunchRequestKind.HISTORY:
            if not all((self.project_id, self.location_id, self.cwd)):
                raise ValidationError(
                    "history request requires project, location, and cwd"
                )
            if (
                self.provider is not ProviderId.CLAUDE
                or self.target_session_key is not None
                or self.source_handoff_id is not None
            ):
                raise ValidationError(
                    "history request requires unbound Claude project context"
                )
        elif self.kind is LaunchRequestKind.MANAGE:
            if any(
                (
                    self.target_session_key,
                    self.project_id,
                    self.location_id,
                    self.cwd,
                    self.source_handoff_id,
                )
            ):
                raise ValidationError("manage request cannot include session context")

    def normalized(self) -> dict[str, str]:
        result = {
            "hostId": str(self.host_id),
            "kind": self.kind,
            "provider": self.provider,
        }
        optional: tuple[tuple[str, Any], ...] = (
            ("targetSessionKey", self.target_session_key),
            ("projectId", self.project_id),
            ("locationId", self.location_id),
            ("cwd", self.cwd),
            ("sourceHandoffId", self.source_handoff_id),
        )
        result.update({key: str(value) for key, value in optional if value is not None})
        return result

    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.normalized(), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def ensure_same_launch_request(original: LaunchRequest, retry: LaunchRequest) -> None:
    if original.fingerprint() != retry.fingerprint():
        raise RequestConflictError("request_conflict")


@dataclass(frozen=True, slots=True)
class PresentationContext:
    has_current_terminal: bool
    current_tmux_client: str | None
    can_focus_desktop: bool
    can_launch_terminal: bool

    def __post_init__(self) -> None:
        for field_name in (
            "has_current_terminal",
            "can_focus_desktop",
            "can_launch_terminal",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValidationError(f"{field_name} must be boolean")
        if self.current_tmux_client is not None:
            object.__setattr__(
                self,
                "current_tmux_client",
                _text(self.current_tmux_client, "current_tmux_client", maximum=1024),
            )
            if not self.has_current_terminal:
                raise ValidationError("tmux client requires a current terminal")


def merge_projects(
    projects: list[Project] | tuple[Project, ...],
) -> tuple[Project, ...]:
    """Merge same-ID declarations and expose incompatible global fields."""

    merged: dict[ProjectId, Project] = {}
    for project in projects:
        current = merged.get(project.project_id)
        if current is None:
            merged[project.project_id] = project
            continue
        conflicts = tuple(
            field_name
            for field_name in (
                "name",
                "default_provider",
                "default_transport",
                "context_sources",
            )
            if getattr(current, field_name) != getattr(project, field_name)
        )
        if conflicts:
            raise ProjectConflictError(project.project_id, conflicts)
        aliases = sanitize_aliases([*current.aliases, *project.aliases])
        created_values = [
            value for value in (current.created_at, project.created_at) if value
        ]
        updated_values = [
            value for value in (current.updated_at, project.updated_at) if value
        ]
        merged[project.project_id] = replace(
            current,
            aliases=aliases,
            created_at=min(created_values) if created_values else None,
            updated_at=max(updated_values) if updated_values else None,
        )
    return tuple(merged[key] for key in sorted(merged, key=str))


def merge_locations(
    locations: list[ProjectLocation] | tuple[ProjectLocation, ...],
) -> tuple[ProjectLocation, ...]:
    merged: dict[LocationId, ProjectLocation] = {}
    for location in locations:
        current = merged.get(location.location_id)
        if current is not None:
            declaration_fields = (
                "project_id",
                "host_id",
                "path",
                "display_name",
                "repository_identity",
                "provider_override",
                "transport_override",
                "is_default",
            )
            if any(
                getattr(current, field_name) != getattr(location, field_name)
                for field_name in declaration_fields
            ):
                raise LocationConflictError(
                    f"location {location.location_id} has conflicting declarations"
                )
            observed = [
                value
                for value in (current.last_observed_at, location.last_observed_at)
                if value is not None
            ]
            location = replace(
                current, last_observed_at=max(observed) if observed else None
            )
        merged[location.location_id] = location
    defaults: set[tuple[ProjectId, HostId]] = set()
    for location in merged.values():
        if not location.is_default:
            continue
        key = (location.project_id, location.host_id)
        if key in defaults:
            raise LocationConflictError(
                f"project {location.project_id} has multiple defaults "
                f"on {location.host_id}"
            )
        defaults.add(key)
    return tuple(merged[key] for key in sorted(merged, key=str))


def match_project_location(
    cwd: str | Path,
    host_id: HostId,
    locations: list[ProjectLocation] | tuple[ProjectLocation, ...],
) -> ProjectLocation | None:
    """Apply canonical same-host longest-path containment matching."""

    path = canonical_path(cwd)
    if not isinstance(host_id, HostId):
        host_id = HostId(host_id)
    matches = [
        location
        for location in locations
        if location.host_id == host_id
        and (path == location.path or location.path in path.parents)
    ]
    if not matches:
        return None
    depth = max(len(location.path.parts) for location in matches)
    most_specific = tuple(
        location for location in matches if len(location.path.parts) == depth
    )
    unique = {location.location_id: location for location in most_specific}
    if len(unique) > 1:
        raise AmbiguousLocationError(path, tuple(unique.values()))
    return next(iter(unique.values()))


def assign_location(
    session: AgentSession,
    locations: list[ProjectLocation] | tuple[ProjectLocation, ...],
) -> AgentSession:
    if session.project_id is not None or session.location_id is not None:
        return session
    location = match_project_location(session.cwd, session.host_id, locations)
    if location is None:
        return session
    return replace(
        session,
        project_id=location.project_id,
        location_id=location.location_id,
        metadata_source="location_match",
    )
