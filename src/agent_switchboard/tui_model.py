"""Pure, widget-independent frontend projection of Snapshot v1."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import IntEnum, StrEnum
from typing import Self

from .domain import (
    Activity,
    ActivityReason,
    Attachment,
    ProjectId,
    ProviderId,
    Resumability,
    RuntimePresence,
    StateConfidence,
    ValidationError,
)
from .protocol import ErrorScope, SessionDetailEnvelope, SnapshotEnvelope
from .state import DisplayStatus, HostReachability, SessionState, derive_display_status

DEFAULT_STALE_AFTER_MS = 120_000
MAX_SEARCH_QUERY_CHARS = 1024


class CapabilityStatus(StrEnum):
    """Frontend state for one provider capability record."""

    NEUTRAL = "neutral"
    AVAILABLE = "available"
    DEGRADED = "degraded"


class IssueSource(StrEnum):
    """Stable origin of an inspectable frontend issue."""

    CAPABILITY = "capability"
    SNAPSHOT = "snapshot"
    FRONTEND = "frontend"


class AttentionRank(IntEnum):
    """Documented default session ordering buckets."""

    NEEDS_INPUT = 0
    WORKING = 1
    COMPLETED = 2
    READY = 3
    PARKED = 4
    OFFLINE_OR_UNKNOWN = 5


_ATTENTION_BY_STATUS = {
    DisplayStatus.NEEDS_INPUT: AttentionRank.NEEDS_INPUT,
    DisplayStatus.WORKING: AttentionRank.WORKING,
    DisplayStatus.COMPLETED: AttentionRank.COMPLETED,
    DisplayStatus.READY: AttentionRank.READY,
    DisplayStatus.PARKED: AttentionRank.PARKED,
    DisplayStatus.OFFLINE: AttentionRank.OFFLINE_OR_UNKNOWN,
    DisplayStatus.UNAVAILABLE: AttentionRank.OFFLINE_OR_UNKNOWN,
    DisplayStatus.UNKNOWN: AttentionRank.OFFLINE_OR_UNKNOWN,
}


@dataclass(frozen=True, slots=True)
class FrontendIssue:
    issue_id: str
    source: IssueSource
    code: str
    message: str
    scope: ErrorScope
    retryable: bool
    observed_at: int
    provider: ProviderId | None = None
    session_key: str | None = None
    feature: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    provider: ProviderId
    status: CapabilityStatus
    available: bool | None
    provider_version: str | None
    features: tuple[str, ...]
    issue_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LaunchTarget:
    target_id: str
    project_id: str
    project_name: str
    location_id: str
    location_name: str | None
    location_path: str
    provider: ProviderId
    is_default: bool
    is_preferred_provider: bool


@dataclass(frozen=True, slots=True)
class SessionRow:
    session_key: str
    host_id: str
    host_name: str
    provider: ProviderId
    provider_session_id: str
    project_id: str | None
    project_name: str | None
    location_id: str | None
    location_name: str | None
    location_path: str | None
    name: str | None
    purpose: str | None
    label: str
    cwd: str | None
    runtime_presence: RuntimePresence
    resumability: Resumability
    activity: Activity
    activity_reason: ActivityReason
    attachment: Attachment
    state_confidence: StateConfidence
    status: DisplayStatus
    attention_rank: AttentionRank
    first_observed_at: int
    last_observed_at: int
    last_activity_at: int | None
    recency_at: int
    pinned: bool
    wrapped_at: int | None
    latest_handoff_id: str | None
    continued_from_handoff_id: str | None
    can_stop: bool
    issue_ids: tuple[str, ...]
    search_text: str = field(repr=False)

    @property
    def has_warnings(self) -> bool:
        return bool(self.issue_ids)


@dataclass(frozen=True, slots=True)
class HandoffView:
    handoff_id: str
    sequence: int
    summary: str
    next_action: str
    source: str
    created_at: int


@dataclass(frozen=True, slots=True)
class SessionDetailView:
    session_key: str
    generated_at: int
    name: str | None
    purpose: str | None
    pinned: bool
    wrapped_at: int | None
    latest_handoff_id: str | None
    continued_from_handoff_id: str | None
    handoffs: tuple[HandoffView, ...]
    handoffs_truncated: bool

    @classmethod
    def from_envelope(cls, envelope: SessionDetailEnvelope) -> Self:
        session = envelope.session
        return cls(
            session_key=str(session["sessionKey"]),
            generated_at=envelope.generated_at,
            name=None if session.get("name") is None else str(session["name"]),
            purpose=(
                None if session.get("purpose") is None else str(session["purpose"])
            ),
            pinned=bool(session.get("pinned", False)),
            wrapped_at=(
                None if session.get("wrappedAt") is None else int(session["wrappedAt"])
            ),
            latest_handoff_id=(
                None
                if session.get("latestHandoffId") is None
                else str(session["latestHandoffId"])
            ),
            continued_from_handoff_id=(
                None
                if session.get("continuedFromHandoffId") is None
                else str(session["continuedFromHandoffId"])
            ),
            handoffs=tuple(
                HandoffView(
                    handoff_id=str(handoff["handoffId"]),
                    sequence=int(handoff["sequence"]),
                    summary=str(handoff["summary"]),
                    next_action=str(handoff["nextAction"]),
                    source=str(handoff["source"]),
                    created_at=int(handoff["createdAt"]),
                )
                for handoff in envelope.handoffs
            ),
            handoffs_truncated=envelope.handoffs_truncated,
        )


def _enum_values[T: StrEnum](enum_type: type[T], values: object) -> frozenset[T]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ValidationError(f"invalid {enum_type.__name__} filter")
    try:
        return frozenset(enum_type(value) for value in values)
    except (TypeError, ValueError) as error:
        raise ValidationError(f"invalid {enum_type.__name__} filter") from error


@dataclass(frozen=True, slots=True)
class ViewFilters:
    """Deterministic local search and axis filters."""

    query: str = ""
    providers: frozenset[ProviderId] = frozenset()
    project_ids: frozenset[str | None] = frozenset()
    activities: frozenset[Activity] = frozenset()
    runtime_presences: frozenset[RuntimePresence] = frozenset()
    attachments: frozenset[Attachment] = frozenset()
    _query_tokens: tuple[str, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or len(self.query) > MAX_SEARCH_QUERY_CHARS:
            raise ValidationError(
                f"search query must be at most {MAX_SEARCH_QUERY_CHARS} characters"
            )
        if any(
            unicodedata.category(character) == "Cc" and not character.isspace()
            for character in self.query
        ):
            raise ValidationError("search query contains control characters")
        normalized_query = " ".join(self.query.split())
        object.__setattr__(self, "query", normalized_query)
        object.__setattr__(
            self,
            "_query_tokens",
            tuple(token for token in _search_value(normalized_query).split() if token),
        )
        object.__setattr__(
            self,
            "providers",
            _enum_values(ProviderId, self.providers),
        )
        canonical_projects: set[str | None] = set()
        try:
            for project_id in self.project_ids:
                canonical_projects.add(
                    None if project_id is None else str(ProjectId(project_id))
                )
        except (TypeError, ValidationError) as error:
            raise ValidationError("invalid project filter") from error
        object.__setattr__(self, "project_ids", frozenset(canonical_projects))
        object.__setattr__(
            self,
            "activities",
            _enum_values(Activity, self.activities),
        )
        object.__setattr__(
            self,
            "runtime_presences",
            _enum_values(RuntimePresence, self.runtime_presences),
        )
        object.__setattr__(
            self,
            "attachments",
            _enum_values(Attachment, self.attachments),
        )

    @property
    def query_tokens(self) -> tuple[str, ...]:
        return self._query_tokens

    def matches(self, row: SessionRow) -> bool:
        if self.providers and row.provider not in self.providers:
            return False
        if self.project_ids and row.project_id not in self.project_ids:
            return False
        if self.activities and row.activity not in self.activities:
            return False
        if (
            self.runtime_presences
            and row.runtime_presence not in self.runtime_presences
        ):
            return False
        if self.attachments and row.attachment not in self.attachments:
            return False
        return all(token in row.search_text for token in self.query_tokens)


@dataclass(frozen=True, slots=True)
class FrontendModel:
    """One immutable, deterministic TUI view model."""

    generated_at: int
    snapshot_age_ms: int
    stale_after_ms: int
    is_stale: bool
    host_id: str
    host_name: str
    rows: tuple[SessionRow, ...]
    visible_rows: tuple[SessionRow, ...]
    launch_targets: tuple[LaunchTarget, ...]
    capabilities: tuple[ProviderCapability, ...]
    issues: tuple[FrontendIssue, ...]
    details: tuple[SessionDetailView, ...]
    filters: ViewFilters
    selected_session_key: str | None
    ignored_snapshot_count: int = 0
    ignored_detail_count: int = 0

    @classmethod
    def from_snapshot(
        cls,
        snapshot: SnapshotEnvelope,
        *,
        now_ms: int,
        stale_after_ms: int = DEFAULT_STALE_AFTER_MS,
        filters: ViewFilters | None = None,
        selected_session_key: str | None = None,
    ) -> Self:
        return _build_model(
            snapshot,
            now_ms=now_ms,
            stale_after_ms=stale_after_ms,
            filters=ViewFilters() if filters is None else filters,
            selected_session_key=selected_session_key,
            previous_visible=(),
            frontend_issues=(),
            details=(),
            ignored_snapshot_count=0,
            ignored_detail_count=0,
        )

    @property
    def selected_row(self) -> SessionRow | None:
        return next(
            (
                row
                for row in self.visible_rows
                if row.session_key == self.selected_session_key
            ),
            None,
        )

    @property
    def selected_detail(self) -> SessionDetailView | None:
        return next(
            (
                detail
                for detail in self.details
                if detail.session_key == self.selected_session_key
            ),
            None,
        )

    def capability(self, provider: ProviderId | str) -> ProviderCapability:
        provider_id = ProviderId(provider)
        return next(item for item in self.capabilities if item.provider is provider_id)

    def issue(self, issue_id: str) -> FrontendIssue:
        try:
            return next(item for item in self.issues if item.issue_id == issue_id)
        except StopIteration as error:
            raise ValidationError(f"unknown frontend issue: {issue_id}") from error

    def with_filters(self, filters: ViewFilters) -> Self:
        visible = tuple(row for row in self.rows if filters.matches(row))
        selected = _retained_selection(
            self.visible_rows,
            visible,
            self.selected_session_key,
        )
        return replace(
            self,
            visible_rows=visible,
            filters=filters,
            selected_session_key=selected,
        )

    def with_selection(self, session_key: str | None) -> Self:
        if session_key is None:
            return replace(self, selected_session_key=None)
        if not any(row.session_key == session_key for row in self.visible_rows):
            raise ValidationError("selected session is not visible")
        return replace(self, selected_session_key=session_key)

    def with_detail(self, envelope: SessionDetailEnvelope) -> Self:
        """Retain one validated detail without allowing an older result to win."""

        detail = SessionDetailView.from_envelope(envelope)
        if str(envelope.session["hostId"]) != self.host_id:
            raise ValidationError("session detail belongs to another host")
        if not any(row.session_key == detail.session_key for row in self.rows):
            raise ValidationError("session detail is not present in the snapshot")
        existing = next(
            (item for item in self.details if item.session_key == detail.session_key),
            None,
        )
        if existing is not None and detail.generated_at < existing.generated_at:
            return replace(
                self.with_frontend_error(
                    "stale_detail_ignored",
                    "An older session-detail result was ignored.",
                    retryable=True,
                    observed_at=max(self.generated_at, existing.generated_at),
                ),
                ignored_detail_count=self.ignored_detail_count + 1,
            )
        details = tuple(
            sorted(
                (
                    *(
                        item
                        for item in self.details
                        if item.session_key != detail.session_key
                    ),
                    detail,
                ),
                key=lambda item: item.session_key,
            )
        )
        issues = tuple(
            issue
            for issue in self.issues
            if issue.issue_id != "frontend:stale_detail_ignored"
        )
        return replace(self, details=details, issues=issues)

    def with_frontend_error(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        observed_at: int,
    ) -> Self:
        code = _bounded_text(code, "frontend issue code", maximum=128)
        if _safe_issue_component(code) != code:
            raise ValidationError("frontend issue code is not a stable identifier")
        if type(retryable) is not bool:
            raise ValidationError("frontend issue retryable flag must be boolean")
        issue = FrontendIssue(
            issue_id=f"frontend:{code}",
            source=IssueSource.FRONTEND,
            code=code,
            message=_bounded_text(message, "frontend issue message", maximum=4096),
            scope=ErrorScope.HOST,
            retryable=retryable,
            observed_at=_timestamp(observed_at, "frontend issue timestamp"),
        )
        issues = (
            *(
                existing
                for existing in self.issues
                if existing.issue_id != issue.issue_id
            ),
            issue,
        )
        return replace(self, issues=issues)

    def clear_frontend_errors(self) -> Self:
        return replace(
            self,
            issues=tuple(
                issue
                for issue in self.issues
                if issue.source is not IssueSource.FRONTEND
            ),
        )

    def apply_snapshot(self, snapshot: SnapshotEnvelope, *, now_ms: int) -> Self:
        """Apply a refresh without allowing an older result to win a race."""

        if str(snapshot.host.host_id) != self.host_id:
            raise ValidationError("refreshed snapshot belongs to another host")
        if snapshot.generated_at < self.generated_at:
            age = _snapshot_age(self.generated_at, now_ms)
            retained = replace(
                self,
                snapshot_age_ms=age,
                is_stale=age > self.stale_after_ms,
                ignored_snapshot_count=self.ignored_snapshot_count + 1,
            )
            return retained.with_frontend_error(
                "stale_snapshot_ignored",
                "An older refresh result was ignored.",
                retryable=True,
                observed_at=now_ms,
            )
        return _build_model(
            snapshot,
            now_ms=now_ms,
            stale_after_ms=self.stale_after_ms,
            filters=self.filters,
            selected_session_key=self.selected_session_key,
            previous_visible=self.visible_rows,
            frontend_issues=(),
            details=self.details,
            ignored_snapshot_count=self.ignored_snapshot_count,
            ignored_detail_count=self.ignored_detail_count,
        )


def _timestamp(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{field_name} must be a non-negative integer")
    return value


def _bounded_text(value: object, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValidationError(f"{field_name} must be bounded text")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValidationError(f"{field_name} contains control characters")
    return value


def _safe_issue_component(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in value
    )[:128]


def _search_value(value: object) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).casefold()


def _search_text(*values: object) -> str:
    return " ".join(_search_value(value) for value in values if value is not None)


def _display_label(
    *,
    provider: ProviderId,
    provider_session_id: str,
    name: str | None,
    project_name: str | None,
    cwd: str | None,
) -> str:
    if name is not None:
        return name
    if project_name is not None:
        return project_name
    if cwd is not None:
        directory = cwd.rstrip("/").rsplit("/", 1)[-1]
        if directory:
            return directory
    provider_name = "Claude" if provider is ProviderId.CLAUDE else "Codex"
    return f"{provider_name} {provider_session_id[:8]}"


def _snapshot_age(generated_at: int, now_ms: int) -> int:
    return max(
        0,
        _timestamp(now_ms, "current timestamp")
        - _timestamp(generated_at, "snapshot timestamp"),
    )


def _recency(session: Mapping[str, object]) -> int:
    for key in ("lastActivityAt", "providerUpdatedAt", "createdAt", "lastObservedAt"):
        value = session.get(key)
        if value is not None:
            return int(value)
    raise AssertionError("validated sessions always contain lastObservedAt")


def _retained_selection(
    previous_visible: tuple[SessionRow, ...],
    visible: tuple[SessionRow, ...],
    selected_session_key: str | None,
) -> str | None:
    if not visible:
        return None
    if selected_session_key is not None and any(
        row.session_key == selected_session_key for row in visible
    ):
        return selected_session_key
    previous_index = next(
        (
            index
            for index, row in enumerate(previous_visible)
            if row.session_key == selected_session_key
        ),
        0,
    )
    return visible[min(previous_index, len(visible) - 1)].session_key


def _capabilities_and_issues(
    snapshot: SnapshotEnvelope,
) -> tuple[tuple[ProviderCapability, ...], tuple[FrontendIssue, ...]]:
    capabilities_by_provider = {
        capability.provider: capability for capability in snapshot.capabilities
    }
    issues: list[FrontendIssue] = []
    capabilities: list[ProviderCapability] = []
    for provider in ProviderId:
        capability = capabilities_by_provider.get(provider)
        if capability is None:
            capabilities.append(
                ProviderCapability(
                    provider,
                    CapabilityStatus.NEUTRAL,
                    None,
                    None,
                    (),
                    (),
                )
            )
            continue
        issue_ids: list[str] = []
        for index, reason in enumerate(capability.degraded_reasons):
            issue_id = f"capability:{provider.value}:{index}:{reason.code}"
            issue_ids.append(issue_id)
            issues.append(
                FrontendIssue(
                    issue_id,
                    IssueSource.CAPABILITY,
                    reason.code,
                    reason.message,
                    ErrorScope.PROVIDER,
                    reason.retryable,
                    snapshot.generated_at,
                    provider=provider,
                    feature=reason.feature,
                )
            )
        status = (
            CapabilityStatus.DEGRADED
            if not capability.available or capability.degraded_reasons
            else CapabilityStatus.AVAILABLE
        )
        capabilities.append(
            ProviderCapability(
                provider,
                status,
                capability.available,
                capability.provider_version,
                capability.features,
                tuple(issue_ids),
            )
        )
    for index, error in enumerate(snapshot.errors):
        provider = (
            error.provider
            if error.provider is not None
            else error.session_key.provider
            if error.session_key is not None
            else None
        )
        issues.append(
            FrontendIssue(
                f"snapshot:{index}:{error.code}",
                IssueSource.SNAPSHOT,
                error.code,
                error.message,
                error.scope,
                error.retryable,
                error.observed_at,
                provider=provider,
                session_key=(
                    None if error.session_key is None else str(error.session_key)
                ),
            )
        )
    return tuple(capabilities), tuple(issues)


def _launch_targets(snapshot: SnapshotEnvelope) -> tuple[LaunchTarget, ...]:
    projects = {
        str(project["projectId"]): project
        for project in snapshot.projects
        if project.get("declared") is not False
    }
    targets: list[LaunchTarget] = []
    for location in snapshot.locations:
        if location.get("declared") is False:
            continue
        project = projects.get(str(location["projectId"]))
        if project is None:
            continue
        transport = location.get("transportOverride") or project.get("defaultTransport")
        if str(transport) != "tmux":
            continue
        preferred = location.get("providerOverride") or project.get("defaultProvider")
        for provider in ProviderId:
            project_id = str(project["projectId"])
            location_id = str(location["locationId"])
            targets.append(
                LaunchTarget(
                    target_id=f"{project_id}:{location_id}:{provider.value}",
                    project_id=project_id,
                    project_name=str(project["name"]),
                    location_id=location_id,
                    location_name=(
                        None
                        if location.get("displayName") is None
                        else str(location["displayName"])
                    ),
                    location_path=str(location["path"]),
                    provider=provider,
                    is_default=bool(location.get("isDefault", False)),
                    is_preferred_provider=str(preferred) == provider.value,
                )
            )
    return tuple(
        sorted(
            targets,
            key=lambda target: (
                target.project_name.casefold(),
                target.project_name,
                target.project_id,
                not target.is_default,
                "" if target.location_name is None else target.location_name.casefold(),
                "" if target.location_name is None else target.location_name,
                target.location_id,
                0 if target.provider is ProviderId.CODEX else 1,
            ),
        )
    )


def _session_rows(
    snapshot: SnapshotEnvelope,
    issues: tuple[FrontendIssue, ...],
) -> tuple[SessionRow, ...]:
    projects = {str(project["projectId"]): project for project in snapshot.projects}
    locations = {
        str(location["locationId"]): location for location in snapshot.locations
    }
    surfaces = {str(surface["surfaceId"]): surface for surface in snapshot.surfaces}
    capability_issue_ids: dict[ProviderId, list[str]] = {
        provider: [] for provider in ProviderId
    }
    provider_issue_ids: dict[ProviderId, list[str]] = {
        provider: [] for provider in ProviderId
    }
    session_issue_ids: dict[str, list[str]] = {}
    for issue in issues:
        if issue.source is IssueSource.CAPABILITY and issue.provider is not None:
            capability_issue_ids[issue.provider].append(issue.issue_id)
        elif issue.session_key is not None:
            session_issue_ids.setdefault(issue.session_key, []).append(issue.issue_id)
        elif issue.provider is not None:
            provider_issue_ids[issue.provider].append(issue.issue_id)

    rows: list[SessionRow] = []
    for session in snapshot.sessions:
        session_key = str(session["sessionKey"])
        provider = ProviderId(str(session["provider"]))
        project_id = (
            None if session.get("projectId") is None else str(session["projectId"])
        )
        location_id = (
            None if session.get("locationId") is None else str(session["locationId"])
        )
        project = projects.get(project_id) if project_id is not None else None
        location = locations.get(location_id) if location_id is not None else None
        runtime_presence = RuntimePresence(str(session["runtimePresence"]))
        resumability = Resumability(str(session["resumability"]))
        activity = Activity(str(session["activity"]))
        activity_reason = ActivityReason(str(session["activityReason"]))
        attachment = Attachment(str(session["attachment"]))
        state_confidence = StateConfidence(str(session["stateConfidence"]))
        status = derive_display_status(
            HostReachability.ONLINE,
            SessionState(
                runtime_presence=runtime_presence,
                resumability=resumability,
                activity=activity,
                activity_reason=activity_reason,
                attachment=attachment,
            ),
        )
        provider_session_id = str(session["providerSessionId"])
        name = None if session.get("name") is None else str(session["name"])
        purpose = None if session.get("purpose") is None else str(session["purpose"])
        project_name = None if project is None else str(project["name"])
        location_name = (
            None
            if location is None or location.get("displayName") is None
            else str(location["displayName"])
        )
        location_path = None if location is None else str(location["path"])
        cwd = None if session.get("cwd") is None else str(session["cwd"])
        label = _display_label(
            provider=provider,
            provider_session_id=provider_session_id,
            name=name,
            project_name=project_name,
            cwd=cwd,
        )
        surface_id = session.get("surfaceId")
        surface = surfaces.get(str(surface_id)) if surface_id is not None else None
        can_stop = (
            provider is ProviderId.CLAUDE
            and runtime_presence is RuntimePresence.LIVE
            and surface is not None
            and str(surface.get("transport")) == "tmux"
            and str(surface.get("role")) == "session"
            and surface.get("currentSessionKey") == session_key
            and str(surface.get("bindingConfidence")) == "confirmed"
            and isinstance(surface.get("launchId"), str)
            and surface.get("retiredAt") is None
        )
        row_issue_ids = tuple(
            dict.fromkeys(
                (
                    *capability_issue_ids[provider],
                    *provider_issue_ids[provider],
                    *session_issue_ids.get(session_key, ()),
                )
            )
        )
        recency_at = _recency(session)
        rows.append(
            SessionRow(
                session_key=session_key,
                host_id=str(session["hostId"]),
                host_name=snapshot.host.display_name,
                provider=provider,
                provider_session_id=provider_session_id,
                project_id=project_id,
                project_name=project_name,
                location_id=location_id,
                location_name=location_name,
                location_path=location_path,
                name=name,
                purpose=purpose,
                label=label,
                cwd=cwd,
                runtime_presence=runtime_presence,
                resumability=resumability,
                activity=activity,
                activity_reason=activity_reason,
                attachment=attachment,
                state_confidence=state_confidence,
                status=status,
                attention_rank=_ATTENTION_BY_STATUS[status],
                first_observed_at=int(session["firstObservedAt"]),
                last_observed_at=int(session["lastObservedAt"]),
                last_activity_at=(
                    None
                    if session.get("lastActivityAt") is None
                    else int(session["lastActivityAt"])
                ),
                recency_at=recency_at,
                pinned=bool(session.get("pinned", False)),
                wrapped_at=(
                    None
                    if session.get("wrappedAt") is None
                    else int(session["wrappedAt"])
                ),
                latest_handoff_id=(
                    None
                    if session.get("latestHandoffId") is None
                    else str(session["latestHandoffId"])
                ),
                continued_from_handoff_id=(
                    None
                    if session.get("continuedFromHandoffId") is None
                    else str(session["continuedFromHandoffId"])
                ),
                can_stop=can_stop,
                issue_ids=row_issue_ids,
                search_text=_search_text(
                    label,
                    name,
                    purpose,
                    project_name,
                    *(project.get("aliases", ()) if project is not None else ()),
                    location_name,
                    location_path,
                    cwd,
                    snapshot.host.display_name,
                    provider.value,
                    status.value,
                    runtime_presence.value,
                    activity.value,
                    attachment.value,
                    provider_session_id,
                    session_key,
                ),
            )
        )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.attention_rank,
                -row.recency_at,
                row.session_key,
            ),
        )
    )


def _build_model(
    snapshot: SnapshotEnvelope,
    *,
    now_ms: int,
    stale_after_ms: int,
    filters: ViewFilters,
    selected_session_key: str | None,
    previous_visible: tuple[SessionRow, ...],
    frontend_issues: tuple[FrontendIssue, ...],
    details: tuple[SessionDetailView, ...],
    ignored_snapshot_count: int,
    ignored_detail_count: int,
) -> FrontendModel:
    stale_after_ms = _timestamp(stale_after_ms, "staleness interval")
    if stale_after_ms == 0:
        raise ValidationError("staleness interval must be positive")
    age = _snapshot_age(snapshot.generated_at, now_ms)
    capabilities, source_issues = _capabilities_and_issues(snapshot)
    rows = _session_rows(snapshot, source_issues)
    visible = tuple(row for row in rows if filters.matches(row))
    selected = _retained_selection(
        previous_visible,
        visible,
        selected_session_key,
    )
    retained_session_keys = {row.session_key for row in rows}
    return FrontendModel(
        generated_at=snapshot.generated_at,
        snapshot_age_ms=age,
        stale_after_ms=stale_after_ms,
        is_stale=age > stale_after_ms,
        host_id=str(snapshot.host.host_id),
        host_name=snapshot.host.display_name,
        rows=rows,
        visible_rows=visible,
        launch_targets=_launch_targets(snapshot),
        capabilities=capabilities,
        issues=source_issues + frontend_issues,
        details=tuple(
            detail for detail in details if detail.session_key in retained_session_keys
        ),
        filters=filters,
        selected_session_key=selected,
        ignored_snapshot_count=ignored_snapshot_count,
        ignored_detail_count=ignored_detail_count,
    )


__all__ = [
    "DEFAULT_STALE_AFTER_MS",
    "MAX_SEARCH_QUERY_CHARS",
    "AttentionRank",
    "CapabilityStatus",
    "FrontendIssue",
    "FrontendModel",
    "HandoffView",
    "IssueSource",
    "LaunchTarget",
    "ProviderCapability",
    "SessionDetailView",
    "SessionRow",
    "ViewFilters",
]
