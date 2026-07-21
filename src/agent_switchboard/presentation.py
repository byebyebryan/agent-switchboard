"""Atomic local launch preparation and waiting-bootstrap orchestration."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Never

from .domain import (
    Checkout,
    CheckoutId,
    HostId,
    PresentationContext,
    Project,
    ProjectId,
    ProjectRepository,
    ProviderId,
    SessionKey,
    TaskId,
    Transport,
    ValidationError,
)
from .protocol import (
    ErrorRecord,
    ErrorScope,
    PresentationPlan,
    PresentationPlanKind,
)
from .storage import (
    ContinuationError,
    Registry,
    RequestConflict,
    StorageError,
    TaskConflict,
)
from .tmux import (
    TmuxController,
    TmuxError,
    TmuxLocator,
    TmuxSurfaceObservation,
    TmuxTargetMissing,
)

PREPARE_CAPABILITY_HASH = hashlib.sha256(
    b"agent-switchboard:phase-3a:local-codex-existing-session"
).hexdigest()
PREPARE_CLAUDE_CAPABILITY_HASH = hashlib.sha256(
    b"agent-switchboard:phase-3c:local-claude-existing-session"
).hexdigest()
PREPARE_NEW_CAPABILITY_HASH = hashlib.sha256(
    b"agent-switchboard:phase-3b:local-codex-new-session"
).hexdigest()
PREPARE_NEW_CLAUDE_CAPABILITY_HASH = hashlib.sha256(
    b"agent-switchboard:phase-3c:local-claude-new-session"
).hexdigest()
PREPARE_CLAUDE_HISTORY_CAPABILITY_HASH = hashlib.sha256(
    b"agent-switchboard:phase-3c:local-claude-history-picker"
).hexdigest()
PREPARE_SURFACE_WAIT_SECONDS = 2.0
BOOTSTRAP_START_WAIT_SECONDS = 5.0
PROVIDER_BIND_GRACE_SECONDS = 300

Clock = Callable[[], int]
Sleeper = Callable[[float], None]
ExecProvider = Callable[[str, Sequence[str]], Never]
ReconcileRuntime = Callable[[], object]
CwdReader = Callable[[], Path]
AgentCapabilityFactory = Callable[[], str]

_AGENT_CAPABILITY_RE = re.compile(r"[A-Za-z0-9_-]{43,128}")


class PresentationError(RuntimeError):
    """A local presentation action could not be prepared safely."""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _exec_provider(executable: str, argv: Sequence[str]) -> Never:
    os.execvp(executable, list(argv))


def _synchronize_bound_unbound_metadata(
    registry: Registry,
    tmux: TmuxController,
    surface: dict[str, object],
    observed: TmuxSurfaceObservation,
    expected_session_key: str,
) -> TmuxSurfaceObservation:
    """Promote an atomically bound unbound-launch identity into pane metadata."""

    metadata = observed.metadata
    if metadata.session_key == expected_session_key:
        return observed
    try:
        expected_provider = SessionKey.parse(expected_session_key).provider.value
    except ValidationError:
        return observed
    launch_id = surface.get("launch_id")
    if metadata.session_key is not None or not isinstance(launch_id, str):
        return observed
    launch = registry.get_launch(launch_id)
    if (
        launch is None
        or launch["action"] not in {"new", "history"}
        or launch["state"] != "bound"
        or launch["target_session_key"] != expected_session_key
        or launch["provider"] != expected_provider
        or surface.get("current_session_key") != expected_session_key
        or surface.get("provider") != expected_provider
        or surface.get("binding_confidence") != "confirmed"
        or metadata.surface_id != surface.get("surface_id")
        or metadata.provider != expected_provider
        or metadata.launch_id != launch_id
        or metadata.role != "session"
    ):
        return observed
    tmux.set_metadata(
        observed.locator,
        surface_id=str(surface["surface_id"]),
        session_key=expected_session_key,
        provider=expected_provider,
        launch_id=launch_id,
        role="session",
    )
    return tmux.inspect_locator(observed.locator)


class LaunchCoordinator:
    """Prepare local managed surfaces without exposing provider or tmux internals."""

    def __init__(
        self,
        registry: Registry,
        *,
        host_id: HostId | str,
        tmux: TmuxController,
        swbctl_executable: str | Path,
        codex_executable: str | None = "codex",
        claude_executable: str | None = "claude",
        projects: Sequence[Project] = (),
        project_repositories: Sequence[ProjectRepository] = (),
        checkouts: Sequence[Checkout] = (),
        naming_prefix: str = "as",
        launch_timeout_seconds: int = 30,
        clock: Clock = _now_ms,
        sleeper: Sleeper = time.sleep,
        cwd_reader: CwdReader = Path.cwd,
        agent_capability_factory: AgentCapabilityFactory = (
            lambda: secrets.token_urlsafe(32)
        ),
    ) -> None:
        self.registry = registry
        self.host_id = host_id if isinstance(host_id, HostId) else HostId(host_id)
        self.tmux = tmux
        self.swbctl_executable = str(swbctl_executable)
        self.codex_executable = codex_executable
        self.claude_executable = claude_executable
        self.projects = {str(project.project_id): project for project in projects}
        self.project_repository_ids = {
            str(project_id): {
                str(membership.repository_id)
                for membership in project_repositories
                if membership.project_id == project_id
            }
            for project_id in {item.project_id for item in project_repositories}
        }
        if not self.project_repository_ids:
            self.project_repository_ids = {
                project_id: {
                    str(checkout.repository_id)
                    for checkout in checkouts
                    if str(checkout.repository_id) == project_id
                }
                for project_id in self.projects
            }
        self.checkouts = {str(checkout.checkout_id): checkout for checkout in checkouts}
        self.naming_prefix = naming_prefix.replace(".", "-")
        self.launch_timeout_seconds = launch_timeout_seconds
        self.clock = clock
        self.sleeper = sleeper
        self.cwd_reader = cwd_reader
        self.agent_capability_factory = agent_capability_factory
        if not Path(self.swbctl_executable).is_absolute():
            raise PresentationError("swbctl executable must be an absolute path")
        for provider_name, executable in (
            ("Codex", self.codex_executable),
            ("Claude", self.claude_executable),
        ):
            if executable is not None and (not executable or "\x00" in executable):
                raise PresentationError(f"{provider_name} executable is invalid")
        if not self.naming_prefix:
            raise PresentationError("tmux naming prefix is invalid")
        if not 1 <= self.launch_timeout_seconds <= 300:
            raise PresentationError("launch timeout must be between 1 and 300 seconds")

    def _issue_agent_capability(
        self, provider: ProviderId, action: str
    ) -> tuple[str | None, str | None]:
        if provider not in {ProviderId.CODEX, ProviderId.CLAUDE} or action not in {
            "new",
            "resume",
        }:
            return None, None
        capability = self.agent_capability_factory()
        if (
            not isinstance(capability, str)
            or _AGENT_CAPABILITY_RE.fullmatch(capability) is None
        ):
            raise PresentationError("agent capability generation failed")
        return capability, hashlib.sha256(capability.encode("ascii")).hexdigest()

    def prepare_new(
        self,
        project_id: str | None,
        *,
        task_id: str | None,
        checkout_id: str | None,
        provider: str | None,
        source_ref: str | None = None,
        request_id: str,
        context: PresentationContext,
        task_create: Mapping[str, object] | None = None,
        imported_handoff: Mapping[str, object] | None = None,
    ) -> PresentationPlan:
        request_id = self._request_id(request_id)
        now = self.clock()
        if task_id is None:
            return self._blocked_new(
                "task_required", "A new session must belong to a task.", now
            )
        parsed_task_id = self._stable_id(task_id, TaskId, "task ID")
        if source_ref is not None and imported_handoff is not None:
            return self._blocked_new(
                "continuation_source_conflict",
                "A continuation may use one local or imported source.",
                now,
            )
        source = None
        if source_ref is not None:
            try:
                source = self.registry.resolve_continuation_source(
                    source_ref, host_id=str(self.host_id)
                )
            except (StorageError, ValidationError) as error:
                code = (
                    error.code
                    if isinstance(error, ContinuationError)
                    else "continuation_source_invalid"
                )
                return self._blocked_new(code, str(error), now)
            source_project_id = str(source.session["project_id"])
            source_checkout_id = str(source.session["checkout_id"])
            if project_id is not None and project_id != source_project_id:
                return self._blocked_new(
                    "continuation_project_conflict",
                    "Continuation cannot change the source project.",
                    now,
                )
            if checkout_id is not None and checkout_id != source_checkout_id:
                return self._blocked_new(
                    "continuation_checkout_conflict",
                    "Continuation cannot change the source project checkout.",
                    now,
                )
            project_id = source_project_id
            checkout_id = source_checkout_id
        if project_id is None:
            return self._blocked_new(
                "project_missing",
                "A project or continuation source is required.",
                now,
            )
        parsed_project_id = self._stable_id(project_id, ProjectId, "project ID")
        parsed_checkout_id = (
            self._stable_id(checkout_id, CheckoutId, "checkout ID")
            if checkout_id is not None
            else None
        )
        project = self.projects.get(str(parsed_project_id))
        if project is None:
            return self._blocked_new(
                "project_not_found",
                "The selected project is not declared on this host.",
                now,
            )

        candidates = sorted(
            (
                checkout
                for checkout in self.checkouts.values()
                if str(checkout.repository_id)
                in self.project_repository_ids.get(str(parsed_project_id), set())
                and checkout.host_id == self.host_id
            ),
            key=lambda checkout: str(checkout.checkout_id),
        )
        checkout: Checkout | None = None
        if parsed_checkout_id is not None:
            checkout = self.checkouts.get(str(parsed_checkout_id))
            if (
                checkout is None
                or str(checkout.repository_id)
                not in self.project_repository_ids.get(str(parsed_project_id), set())
                or checkout.host_id != self.host_id
            ):
                return self._blocked_new(
                    "checkout_not_found",
                    "The selected checkout does not belong to this local project.",
                    now,
                )
        elif len(candidates) == 1:
            checkout = candidates[0]
        else:
            defaults = [candidate for candidate in candidates if candidate.is_default]
            if len(defaults) == 1:
                checkout = defaults[0]
            elif not candidates:
                return self._blocked_new(
                    "project_checkout_missing",
                    "The selected project has no checkout on this host.",
                    now,
                )
            else:
                return self._blocked_new(
                    "project_checkout_ambiguous",
                    "The selected project requires an explicit checkout.",
                    now,
                )
        assert checkout is not None

        resolved_provider: ProviderId | None
        if provider is not None:
            try:
                resolved_provider = ProviderId(provider)
            except ValueError:
                return self._blocked_new(
                    "provider_not_supported",
                    "The selected provider is not supported.",
                    now,
                )
        else:
            resolved_provider = (
                ProviderId(str(source.session["provider"]))
                if source is not None
                else checkout.provider_override or project.default_provider
            )
        if resolved_provider is None:
            return self._blocked_new(
                "project_provider_missing",
                "The selected project does not resolve a provider.",
                now,
            )
        if self._provider_executable(resolved_provider) is None:
            return self._blocked_new(
                "provider_unavailable",
                f"{self._provider_label(resolved_provider)} is disabled in the "
                "current host configuration.",
                now,
                provider=resolved_provider,
            )
        transport = checkout.transport_override or project.default_transport
        if transport is not Transport.TMUX:
            return self._blocked_new(
                "transport_not_supported",
                "This phase requires the tmux transport.",
                now,
                provider=resolved_provider,
            )
        if not checkout.path.is_absolute() or not checkout.path.is_dir():
            return self._blocked_new(
                "working_directory_unavailable",
                "The selected project checkout is unavailable.",
                now,
                provider=resolved_provider,
            )
        return self._prepare_new(
            project,
            checkout,
            task_id=str(parsed_task_id),
            provider=resolved_provider,
            request_id=request_id,
            context=context,
            now=now,
            source_handoff_id=(
                str(source.handoff["handoff_id"])
                if source is not None
                else (
                    None
                    if imported_handoff is None
                    else str(imported_handoff["handoff_id"])
                )
            ),
            source_session_key=(
                str(source.session["session_key"])
                if source is not None and source.from_session
                else None
            ),
            task_create=task_create,
            imported_handoff=imported_handoff,
        )

    def prepare_task_create(
        self,
        *,
        task_id: str,
        project_id: str,
        title: str,
        checkout_id: str | None,
        provider: str,
        purpose: str | None = None,
        request_id: str,
        context: PresentationContext,
        imported_handoff: Mapping[str, object] | None = None,
    ) -> PresentationPlan:
        """Atomically create a task and reserve its first provider launch."""

        return self.prepare_new(
            project_id,
            task_id=task_id,
            checkout_id=checkout_id,
            provider=provider,
            request_id=request_id,
            context=context,
            task_create={
                "task_id": task_id,
                "title": title,
                "purpose": purpose,
                "preferred_provider": provider,
            },
            imported_handoff=imported_handoff,
        )

    def prepare_task(
        self,
        task_id: str,
        *,
        provider: str | None,
        request_id: str,
        context: PresentationContext,
        reopen: bool = False,
    ) -> PresentationPlan:
        """Open a task's current session or start its first/next session."""

        parsed_task_id = self._stable_id(task_id, TaskId, "task ID")
        task = self.registry.get_task(str(parsed_task_id))
        now = self.clock()
        if task is None or task["host_id"] != str(self.host_id):
            return self._blocked_new("task_not_found", "The task is not local.", now)
        if task["status"] != "open":
            if not reopen:
                return self._blocked_new("task_closed", "The task is closed.", now)
            project_id = str(task["project_id"])
            checkout_id = task.get("checkout_id")
            selected_checkout = (
                None if checkout_id is None else self.checkouts.get(str(checkout_id))
            )
            if selected_checkout is None and checkout_id is None:
                candidates = sorted(
                    (
                        checkout
                        for checkout in self.checkouts.values()
                        if str(checkout.repository_id)
                        in self.project_repository_ids.get(project_id, set())
                        and checkout.host_id == self.host_id
                    ),
                    key=lambda checkout: str(checkout.checkout_id),
                )
                defaults = [
                    candidate for candidate in candidates if candidate.is_default
                ]
                selected_checkout = (
                    candidates[0]
                    if len(candidates) == 1
                    else defaults[0]
                    if len(defaults) == 1
                    else None
                )
            if (
                selected_checkout is None
                or selected_checkout.host_id != self.host_id
                or str(selected_checkout.repository_id)
                not in self.project_repository_ids.get(project_id, set())
            ):
                return self._blocked_new(
                    "task_checkout_missing",
                    "The closed task has no valid local checkout.",
                    now,
                )
            if (
                not selected_checkout.path.is_absolute()
                or not selected_checkout.path.is_dir()
            ):
                return self._blocked_new(
                    "working_directory_unavailable",
                    "The selected task checkout is unavailable.",
                    now,
                )
            try:
                task = self.registry.reopen_task(
                    str(parsed_task_id),
                    host_id=str(self.host_id),
                    checkout_id=(
                        str(selected_checkout.checkout_id)
                        if checkout_id is None
                        else None
                    ),
                    observed_at=now,
                )
            except TaskConflict as error:
                return self._blocked_new(error.code, str(error), now)
        current_session_key = task.get("current_session_key")
        if isinstance(current_session_key, str):
            current = self.registry.get_session(current_session_key)
            if current is None:
                return self._blocked_new(
                    "task_current_session_missing",
                    "The task's current session is missing.",
                    now,
                )
            if provider is None or provider == current["provider"]:
                return self.prepare_open(
                    current_session_key,
                    request_id=request_id,
                    context=context,
                )
            if current["wrapped_at"] is None:
                return self._blocked_new(
                    "task_current_session_active",
                    "Close or hand off the current session before switching provider.",
                    now,
                )
            source_ref: str | None = current_session_key
        else:
            source_ref = None
        selected_provider = provider or task.get("preferred_provider")
        if task["checkout_id"] is None:
            project_id = str(task["project_id"])
            candidates = sorted(
                (
                    checkout
                    for checkout in self.checkouts.values()
                    if str(checkout.repository_id)
                    in self.project_repository_ids.get(project_id, set())
                    and checkout.host_id == self.host_id
                ),
                key=lambda checkout: str(checkout.checkout_id),
            )
            defaults = [checkout for checkout in candidates if checkout.is_default]
            selected_checkout = (
                candidates[0]
                if len(candidates) == 1
                else defaults[0]
                if len(defaults) == 1
                else None
            )
            if selected_checkout is None:
                return self._blocked_new(
                    "task_checkout_missing",
                    "The task requires an explicit checkout before it can start.",
                    now,
                )
            try:
                task = self.registry.route_task(
                    str(parsed_task_id),
                    host_id=str(self.host_id),
                    checkout_id=str(selected_checkout.checkout_id),
                    observed_at=now,
                )
            except TaskConflict as error:
                return self._blocked_new(error.code, str(error), now)
        return self.prepare_new(
            str(task["project_id"]),
            task_id=str(parsed_task_id),
            checkout_id=(
                None if task["checkout_id"] is None else str(task["checkout_id"])
            ),
            provider=(None if selected_provider is None else str(selected_provider)),
            source_ref=source_ref,
            request_id=request_id,
            context=context,
        )

    def prepare_history(
        self,
        project_id: str,
        *,
        checkout_id: str | None,
        request_id: str,
        context: PresentationContext,
    ) -> PresentationPlan:
        parsed_project_id = self._stable_id(project_id, ProjectId, "project ID")
        parsed_checkout_id = (
            self._stable_id(checkout_id, CheckoutId, "checkout ID")
            if checkout_id is not None
            else None
        )
        request_id = self._request_id(request_id)
        now = self.clock()
        project = self.projects.get(str(parsed_project_id))
        if project is None:
            return self._blocked_new(
                "project_not_found",
                "The selected project is not declared on this host.",
                now,
                provider=ProviderId.CLAUDE,
            )
        candidates = sorted(
            (
                checkout
                for checkout in self.checkouts.values()
                if str(checkout.repository_id)
                in self.project_repository_ids.get(str(parsed_project_id), set())
                and checkout.host_id == self.host_id
            ),
            key=lambda checkout: str(checkout.checkout_id),
        )
        checkout: Checkout | None = None
        if parsed_checkout_id is not None:
            checkout = self.checkouts.get(str(parsed_checkout_id))
            if (
                checkout is None
                or str(checkout.repository_id)
                not in self.project_repository_ids.get(str(parsed_project_id), set())
                or checkout.host_id != self.host_id
            ):
                return self._blocked_new(
                    "checkout_not_found",
                    "The selected checkout does not belong to this local project.",
                    now,
                    provider=ProviderId.CLAUDE,
                )
        elif len(candidates) == 1:
            checkout = candidates[0]
        else:
            defaults = [candidate for candidate in candidates if candidate.is_default]
            if len(defaults) == 1:
                checkout = defaults[0]
            elif not candidates:
                return self._blocked_new(
                    "project_checkout_missing",
                    "The selected project has no checkout on this host.",
                    now,
                    provider=ProviderId.CLAUDE,
                )
            else:
                return self._blocked_new(
                    "project_checkout_ambiguous",
                    "The selected project requires an explicit checkout.",
                    now,
                    provider=ProviderId.CLAUDE,
                )
        assert checkout is not None
        if self.claude_executable is None:
            return self._blocked_new(
                "provider_unavailable",
                "Claude is disabled in the current host configuration.",
                now,
                provider=ProviderId.CLAUDE,
            )
        transport = checkout.transport_override or project.default_transport
        if transport is not Transport.TMUX:
            return self._blocked_new(
                "transport_not_supported",
                "Claude history requires the tmux transport.",
                now,
                provider=ProviderId.CLAUDE,
            )
        if not checkout.path.is_absolute() or not checkout.path.is_dir():
            return self._blocked_new(
                "working_directory_unavailable",
                "The selected project checkout is unavailable.",
                now,
                provider=ProviderId.CLAUDE,
            )
        return self._prepare_unbound(
            project,
            checkout,
            provider=ProviderId.CLAUDE,
            action="history",
            capability_hash=PREPARE_CLAUDE_HISTORY_CAPABILITY_HASH,
            request_id=request_id,
            context=context,
            now=now,
        )

    @staticmethod
    def _stable_id[T: ProjectId | CheckoutId | TaskId](
        value: str,
        value_type: type[T],
        field: str,
    ) -> T:
        parsed = value_type(value)
        if str(parsed) != value:
            raise ValidationError(f"{field} must use canonical UUID spelling")
        return parsed

    def prepare_open(
        self,
        session_key: str,
        *,
        request_id: str,
        context: PresentationContext,
    ) -> PresentationPlan:
        parsed_key = self._session_key(session_key)
        request_id = self._request_id(request_id)
        now = self.clock()
        if parsed_key.host_id != self.host_id:
            return self._blocked(
                "remote_open_not_supported",
                "This phase can open only sessions owned by the local host.",
                parsed_key,
                now,
            )
        session = self.registry.get_session(str(parsed_key))
        if session is None:
            return self._blocked(
                "session_not_found",
                "The selected session is not present in the local registry.",
                parsed_key,
                now,
            )

        existing = self._existing_surface_plan(session, parsed_key, context, now)
        if existing is not None:
            return existing
        if session["runtime_presence"] == "live":
            return self._adopt_live_surface(session, parsed_key, context, now)
        if session["resumability"] != "resumable":
            return self._blocked(
                "session_not_resumable",
                "The selected session is not currently resumable.",
                parsed_key,
                now,
            )
        if self._provider_executable(parsed_key.provider) is None:
            return self._blocked(
                "provider_unavailable",
                f"{self._provider_label(parsed_key.provider)} is disabled in the "
                "current host configuration.",
                parsed_key,
                now,
            )
        cwd = session.get("cwd")
        if (
            not isinstance(cwd, str)
            or not Path(cwd).is_absolute()
            or not Path(cwd).is_dir()
        ):
            return self._blocked(
                "working_directory_unavailable",
                "The selected session's working directory is unavailable.",
                parsed_key,
                now,
            )
        return self._prepare_resume(
            session,
            parsed_key,
            request_id=request_id,
            context=context,
            now=now,
        )

    def _provider_executable(self, provider: ProviderId) -> str | None:
        if provider is ProviderId.CODEX:
            return self.codex_executable
        return self.claude_executable

    @staticmethod
    def _provider_label(provider: ProviderId) -> str:
        return "Codex" if provider is ProviderId.CODEX else "Claude"

    @staticmethod
    def _session_key(value: str) -> SessionKey:
        parsed = SessionKey.parse(value)
        if str(parsed) != value:
            raise ValidationError("session key must use canonical UUID spelling")
        return parsed

    @staticmethod
    def _request_id(value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValidationError("request ID must be a non-nil UUID") from error
        if parsed.int == 0 or str(parsed) != value:
            raise ValidationError("request ID must use canonical non-nil UUID spelling")
        return value

    def _existing_surface_plan(
        self,
        session: dict[str, object],
        session_key: SessionKey,
        context: PresentationContext,
        now: int,
    ) -> PresentationPlan | None:
        surface_id = session.get("surface_id")
        if not isinstance(surface_id, str):
            return None
        surface = self.registry.get_surface(surface_id)
        if surface is None:
            return self._blocked(
                "surface_record_missing",
                "The selected session references a missing surface.",
                session_key,
                now,
                retryable=True,
            )
        if (
            surface["host_id"] != str(self.host_id)
            or surface["provider"] != session_key.provider.value
            or surface["role"] != "session"
            or surface["retired_at"] is not None
            or surface["current_session_key"] != str(session_key)
            or surface["binding_confidence"] != "confirmed"
        ):
            return self._blocked(
                "surface_binding_untrusted",
                "The selected session does not have a confirmed managed surface.",
                session_key,
                now,
                retryable=True,
            )
        try:
            locator = TmuxLocator.from_storage(surface["transport_locator"])
            observed = self.tmux.inspect_locator(locator)
        except TmuxTargetMissing:
            observed_at = max(now, int(surface["last_observed_at"]))
            self.registry.retire_surface(surface_id, observed_at=observed_at)
            return None
        except TmuxError:
            return self._blocked(
                "surface_unavailable",
                "The selected session's managed surface could not be verified.",
                session_key,
                now,
                retryable=True,
            )
        observed = _synchronize_bound_unbound_metadata(
            self.registry,
            self.tmux,
            surface,
            observed,
            str(session_key),
        )
        if not self._metadata_matches(
            observed,
            surface_id=surface_id,
            session_key=str(session_key),
            provider=session_key.provider.value,
            launch_id=surface.get("launch_id"),
        ):
            return self._blocked(
                "surface_identity_mismatch",
                "The selected tmux target no longer has the expected identity.",
                session_key,
                now,
                retryable=True,
            )
        self._refresh_attachment(surface, observed, now)
        plan = self._shape_plan(surface, observed, context)
        if (
            plan.kind is not PresentationPlanKind.BLOCKED
            and session.get("wrapped_at") is not None
        ):
            self.registry.clear_session_wrapped(
                str(session_key), host_id=str(self.host_id)
            )
        return plan

    def _adopt_live_surface(
        self,
        session: dict[str, object],
        session_key: SessionKey,
        context: PresentationContext,
        now: int,
    ) -> PresentationPlan:
        socket = session.get("tmux_socket")
        pane = session.get("tmux_pane")
        if not isinstance(socket, str) or not isinstance(pane, str):
            return self._blocked(
                "unmanaged_surface",
                "The live runtime has no trustworthy tmux surface locator.",
                session_key,
                now,
            )
        try:
            observed = self.tmux.inspect_pane(socket, pane)
        except TmuxError:
            return self._blocked(
                "unmanaged_surface",
                "The live runtime's tmux surface could not be verified.",
                session_key,
                now,
                retryable=True,
            )
        metadata = observed.metadata
        if any(
            value is not None
            for value in (
                metadata.surface_id,
                metadata.session_key,
                metadata.provider,
                metadata.launch_id,
                metadata.role,
            )
        ):
            return self._blocked(
                "surface_identity_occupied",
                "The live tmux pane already carries unrelated surface metadata.",
                session_key,
                now,
                retryable=True,
            )

        surface_id = str(uuid.uuid4())
        surface = {
            "surface_id": surface_id,
            "host_id": str(self.host_id),
            "provider": session_key.provider.value,
            "transport": "tmux",
            "transport_locator": observed.locator.to_storage(),
            "workspace_id": observed.locator.session,
            "role": "session",
            "created_at": now,
            "last_observed_at": now,
            "client_attached": observed.client_attached,
        }
        try:
            stored = self.registry.adopt_bound_surface(
                surface,
                str(session_key),
                observed_at=now,
            )
        except (StorageError, sqlite3.Error):
            refreshed_session = self.registry.get_session(str(session_key))
            if refreshed_session is not None and refreshed_session["surface_id"]:
                existing = self._existing_surface_plan(
                    refreshed_session, session_key, context, self.clock()
                )
                if existing is not None:
                    return existing
            raise
        try:
            self.tmux.set_metadata(
                observed.locator,
                surface_id=surface_id,
                session_key=str(session_key),
                provider=session_key.provider.value,
                launch_id=None,
                role="session",
            )
            verified = self.tmux.inspect_locator(observed.locator)
        except TmuxError:
            self.registry.retire_surface(surface_id, observed_at=max(now, self.clock()))
            raise
        if not self._metadata_matches(
            verified,
            surface_id=surface_id,
            session_key=str(session_key),
            provider=session_key.provider.value,
            launch_id=None,
        ):
            self.registry.retire_surface(surface_id, observed_at=max(now, self.clock()))
            raise PresentationError("adopted tmux metadata did not revalidate")
        plan = self._shape_plan(stored, verified, context)
        if (
            plan.kind is not PresentationPlanKind.BLOCKED
            and session.get("wrapped_at") is not None
        ):
            self.registry.clear_session_wrapped(
                str(session_key), host_id=str(self.host_id)
            )
        return plan

    def _prepare_resume(
        self,
        session: dict[str, object],
        session_key: SessionKey,
        *,
        request_id: str,
        context: PresentationContext,
        now: int,
    ) -> PresentationPlan:
        provider = session_key.provider
        agent_capability, agent_capability_hash = self._issue_agent_capability(
            provider, "resume"
        )
        launch_id = str(uuid.uuid4())
        lease_owner = self._lease_owner(launch_id)
        request = {
            "host_id": str(self.host_id),
            "provider": provider.value,
            "action": "resume",
            "project_id": None,
            "checkout_id": None,
            "cwd": None,
            "source_handoff_id": None,
            "target_session_key": str(session_key),
            "transport": "tmux",
        }
        try:
            reservation = self.registry.reserve_launch(
                request,
                request_id=request_id,
                launch_id=launch_id,
                lease_owner=lease_owner,
                capability_hash=(
                    PREPARE_CAPABILITY_HASH
                    if provider is ProviderId.CODEX
                    else PREPARE_CLAUDE_CAPABILITY_HASH
                ),
                expires_at=now + self.launch_timeout_seconds * 1_000,
                agent_capability_hash=agent_capability_hash,
                created_at=now,
            )
        except RequestConflict:
            return self._blocked(
                "request_conflict",
                "The request ID was already used for a different open action.",
                session_key,
                now,
            )
        launch = reservation.launch
        if reservation.kind != "created":
            return self._plan_for_launch(launch, session_key, context)

        surface_id = str(uuid.uuid4())
        session_name = f"{self.naming_prefix}-{surface_id[:12]}"
        environment = {
            "AGENT_SWITCHBOARD_LAUNCH_ID": launch_id,
            "AGENT_SWITCHBOARD_SURFACE_ID": surface_id,
        }
        if agent_capability is not None:
            environment["AGENT_SWITCHBOARD_CAPABILITY"] = agent_capability
        if provider is ProviderId.CLAUDE:
            environment["CLAUDE_CODE_DISABLE_AGENT_VIEW"] = "1"
        observed: TmuxSurfaceObservation | None = None
        try:
            observed = self.tmux.create_surface(
                name=session_name,
                cwd=Path(str(session["cwd"])),
                command=(self.swbctl_executable, "bootstrap", launch_id),
                environment=environment,
                surface_id=surface_id,
                session_key=str(session_key),
                provider=provider.value,
                launch_id=launch_id,
                role="session",
            )
            activated = self.registry.activate_launch_surface(
                launch_id,
                {
                    "surface_id": surface_id,
                    "host_id": str(self.host_id),
                    "provider": provider.value,
                    "transport": "tmux",
                    "transport_locator": observed.locator.to_storage(),
                    "workspace_id": observed.locator.session,
                    "role": "session",
                    "launch_id": launch_id,
                    "created_at": now,
                    "client_attached": observed.client_attached,
                },
                lease_owner=lease_owner,
                observed_at=max(now, self.clock()),
            )
        except (StorageError, TmuxError, sqlite3.Error) as error:
            if observed is not None:
                with suppress(TmuxError):
                    self.tmux.kill_surface(observed.locator)
            self._fail_launch(launch_id, lease_owner, "surface_creation_failed")
            raise PresentationError("managed tmux surface creation failed") from error
        return self._shape_plan(
            activated.surface,
            observed,
            context,
            lease_expires_at=int(activated.launch["expires_at"]),
        )

    def _prepare_new(
        self,
        project: Project,
        checkout: Checkout,
        *,
        task_id: str | None = None,
        provider: ProviderId,
        request_id: str,
        context: PresentationContext,
        now: int,
        source_handoff_id: str | None = None,
        source_session_key: str | None = None,
        task_create: Mapping[str, object] | None = None,
        imported_handoff: Mapping[str, object] | None = None,
    ) -> PresentationPlan:
        capability_hash = (
            PREPARE_NEW_CAPABILITY_HASH
            if provider is ProviderId.CODEX
            else PREPARE_NEW_CLAUDE_CAPABILITY_HASH
        )
        return self._prepare_unbound(
            project,
            checkout,
            task_id=task_id,
            provider=provider,
            action="new",
            capability_hash=capability_hash,
            request_id=request_id,
            context=context,
            now=now,
            source_handoff_id=source_handoff_id,
            source_session_key=source_session_key,
            task_create=task_create,
            imported_handoff=imported_handoff,
        )

    def _prepare_unbound(
        self,
        project: Project,
        checkout: Checkout,
        *,
        task_id: str | None = None,
        provider: ProviderId,
        action: str,
        capability_hash: str,
        request_id: str,
        context: PresentationContext,
        now: int,
        source_handoff_id: str | None = None,
        source_session_key: str | None = None,
        task_create: Mapping[str, object] | None = None,
        imported_handoff: Mapping[str, object] | None = None,
    ) -> PresentationPlan:
        if action not in {"new", "history"}:
            raise PresentationError("unbound launch action is unsupported")
        agent_capability, agent_capability_hash = self._issue_agent_capability(
            provider, action
        )
        launch_id = str(uuid.uuid4())
        lease_owner = self._lease_owner(launch_id)
        request = {
            "host_id": str(self.host_id),
            "provider": provider.value,
            "action": action,
            "project_id": str(project.project_id),
            "task_id": task_id,
            "checkout_id": str(checkout.checkout_id),
            "cwd": str(checkout.path),
            "source_handoff_id": source_handoff_id,
            "target_session_key": None,
            "transport": "tmux",
        }
        try:
            reservation = self.registry.reserve_launch(
                request,
                request_id=request_id,
                launch_id=launch_id,
                lease_owner=lease_owner,
                capability_hash=capability_hash,
                expires_at=now + self.launch_timeout_seconds * 1_000,
                agent_capability_hash=agent_capability_hash,
                created_at=now,
                source_session_key=source_session_key,
                task_create=task_create,
                imported_handoff=imported_handoff,
            )
        except RequestConflict:
            return self._blocked_new(
                "request_conflict",
                "The request ID was already used for a different unbound action.",
                now,
                provider=provider,
            )
        except ContinuationError as error:
            return self._blocked_new(
                error.code,
                str(error),
                now,
                provider=provider,
            )
        except TaskConflict as error:
            return self._blocked_new(
                error.code,
                str(error),
                now,
                provider=provider,
            )
        launch = reservation.launch
        if reservation.kind != "created":
            return self._plan_for_launch(launch, None, context)

        surface_id = str(uuid.uuid4())
        session_name = f"{self.naming_prefix}-{surface_id[:12]}"
        environment = {
            "AGENT_SWITCHBOARD_LAUNCH_ID": launch_id,
            "AGENT_SWITCHBOARD_SURFACE_ID": surface_id,
        }
        if agent_capability is not None:
            environment["AGENT_SWITCHBOARD_CAPABILITY"] = agent_capability
        if provider is ProviderId.CLAUDE:
            environment["CLAUDE_CODE_DISABLE_AGENT_VIEW"] = "1"
        observed: TmuxSurfaceObservation | None = None
        try:
            observed = self.tmux.create_surface(
                name=session_name,
                cwd=checkout.path,
                command=(self.swbctl_executable, "bootstrap", launch_id),
                environment=environment,
                surface_id=surface_id,
                session_key=None,
                provider=provider.value,
                launch_id=launch_id,
                role="session",
            )
            activated = self.registry.activate_launch_surface(
                launch_id,
                {
                    "surface_id": surface_id,
                    "host_id": str(self.host_id),
                    "provider": provider.value,
                    "transport": "tmux",
                    "transport_locator": observed.locator.to_storage(),
                    "workspace_id": observed.locator.session,
                    "role": "session",
                    "launch_id": launch_id,
                    "created_at": now,
                    "client_attached": observed.client_attached,
                },
                lease_owner=lease_owner,
                observed_at=max(now, self.clock()),
            )
        except (StorageError, TmuxError, sqlite3.Error) as error:
            if observed is not None:
                with suppress(TmuxError):
                    self.tmux.kill_surface(observed.locator)
            self._fail_launch(launch_id, lease_owner, "surface_creation_failed")
            raise PresentationError("managed tmux surface creation failed") from error
        return self._shape_plan(
            activated.surface,
            observed,
            context,
            lease_expires_at=int(activated.launch["expires_at"]),
        )

    def _plan_for_launch(
        self,
        launch: dict[str, object],
        session_key: SessionKey | None,
        context: PresentationContext,
    ) -> PresentationPlan:
        provider = ProviderId(str(launch["provider"]))
        deadline = time.monotonic() + PREPARE_SURFACE_WAIT_SECONDS
        while launch["state"] in {"reserved", "surface_ready"}:
            if time.monotonic() >= deadline:
                return self._blocked_target(
                    "launch_preparing",
                    "Another request is still preparing the session surface.",
                    session_key,
                    self.clock(),
                    retryable=True,
                    provider=provider,
                )
            self.sleeper(0.02)
            refreshed = self.registry.get_launch(str(launch["launch_id"]))
            if refreshed is None:
                raise PresentationError("reserved launch disappeared")
            launch = refreshed
        if launch["state"] in {"failed", "expired"}:
            code = "launch_failed" if launch["state"] == "failed" else "launch_expired"
            return self._blocked_target(
                code,
                "The prior session-open attempt is no longer executable.",
                session_key,
                self.clock(),
                retryable=True,
                provider=provider,
            )
        if (
            launch["state"] == "bound"
            and session_key is None
            and isinstance(launch.get("target_session_key"), str)
        ):
            session_key = SessionKey.parse(str(launch["target_session_key"]))
        surface_id = launch.get("surface_id")
        if not isinstance(surface_id, str):
            raise PresentationError("active launch has no surface")
        surface = self.registry.get_surface(surface_id)
        if surface is None or surface["retired_at"] is not None:
            return self._blocked_target(
                "launch_surface_unavailable",
                "The prepared session surface is unavailable.",
                session_key,
                self.clock(),
                retryable=True,
                provider=provider,
            )
        try:
            locator = TmuxLocator.from_storage(surface["transport_locator"])
            observed = self.tmux.inspect_locator(locator)
        except TmuxError:
            return self._blocked_target(
                "launch_surface_unavailable",
                "The prepared session surface could not be verified.",
                session_key,
                self.clock(),
                retryable=True,
                provider=provider,
            )
        if session_key is not None:
            observed = _synchronize_bound_unbound_metadata(
                self.registry,
                self.tmux,
                surface,
                observed,
                str(session_key),
            )
        if not self._metadata_matches(
            observed,
            surface_id=surface_id,
            session_key=(str(session_key) if session_key is not None else None),
            provider=str(launch["provider"]),
            launch_id=str(launch["launch_id"]),
        ):
            return self._blocked_target(
                "surface_identity_mismatch",
                "The prepared tmux target no longer has the expected identity.",
                session_key,
                self.clock(),
                retryable=True,
                provider=provider,
            )
        lease = None if launch["state"] == "bound" else int(launch["expires_at"])
        return self._shape_plan(surface, observed, context, lease_expires_at=lease)

    @staticmethod
    def _metadata_matches(
        observed: TmuxSurfaceObservation,
        *,
        surface_id: str,
        session_key: str | None,
        provider: str,
        launch_id: object,
    ) -> bool:
        metadata = observed.metadata
        return (
            metadata.surface_id == surface_id
            and metadata.session_key == session_key
            and metadata.provider == provider
            and metadata.launch_id == launch_id
            and metadata.role == "session"
        )

    def _refresh_attachment(
        self,
        surface: dict[str, object],
        observed: TmuxSurfaceObservation,
        now: int,
    ) -> None:
        if bool(surface["client_attached"]) == observed.client_attached:
            return
        observed_at = max(now, int(surface["last_observed_at"]) + 1)
        self.registry.upsert_surface(
            {
                "surface_id": surface["surface_id"],
                "host_id": surface["host_id"],
                "provider": surface["provider"],
                "transport": surface["transport"],
                "transport_locator": surface["transport_locator"],
                "workspace_id": surface["workspace_id"],
                "role": surface["role"],
                "launch_id": surface["launch_id"],
                "last_observed_at": observed_at,
                "client_attached": observed.client_attached,
            }
        )

    def _shape_plan(
        self,
        surface: dict[str, object],
        observed: TmuxSurfaceObservation,
        context: PresentationContext,
        *,
        lease_expires_at: int | None = None,
    ) -> PresentationPlan:
        surface_id = str(surface["surface_id"])
        locator = observed.locator
        workspace_id = (
            str(surface["workspace_id"])
            if surface.get("workspace_id") is not None
            else locator.session
        )
        desktop_token = f"surface:{surface_id}"
        if context.current_tmux_client is not None:
            if not self.tmux.client_exists(locator, context.current_tmux_client):
                session_key = observed.metadata.session_key
                return self._blocked_target(
                    "tmux_client_stale",
                    "The caller's tmux client could not be revalidated.",
                    SessionKey.parse(session_key) if session_key is not None else None,
                    self.clock(),
                    retryable=True,
                    provider=ProviderId(str(surface["provider"])),
                )
            plan = PresentationPlan(
                PresentationPlanKind.SWITCH,
                self.host_id,
                surface_id=surface_id,
                workspace_id=workspace_id,
                tmux_target=locator.to_storage(),
                tmux_client=context.current_tmux_client,
                desktop_token=(desktop_token if context.can_focus_desktop else None),
                lease_expires_at=lease_expires_at,
            )
        elif context.can_focus_desktop and len(self.tmux.clients(locator)) == 1:
            plan = PresentationPlan(
                PresentationPlanKind.FOCUS,
                self.host_id,
                surface_id=surface_id,
                workspace_id=workspace_id,
                desktop_token=desktop_token,
            )
        elif context.has_current_terminal or context.can_launch_terminal:
            plan = PresentationPlan(
                PresentationPlanKind.ATTACH,
                self.host_id,
                surface_id=surface_id,
                workspace_id=workspace_id,
                tmux_target=locator.to_storage(),
                desktop_token=(desktop_token if context.can_launch_terminal else None),
                lease_expires_at=lease_expires_at,
            )
        else:
            session_key = observed.metadata.session_key
            return self._blocked_target(
                "presentation_unavailable",
                "The caller cannot focus, switch, or attach this session surface.",
                SessionKey.parse(session_key) if session_key is not None else None,
                self.clock(),
                retryable=True,
                provider=ProviderId(str(surface["provider"])),
            )
        plan.validate_for_context(context)
        return plan

    def _blocked(
        self,
        code: str,
        message: str,
        session_key: SessionKey,
        observed_at: int,
        *,
        retryable: bool = False,
    ) -> PresentationPlan:
        error = ErrorRecord.from_dict(
            ErrorRecord(
                code,
                message,
                ErrorScope.SESSION,
                retryable,
                observed_at,
                host_id=session_key.host_id,
                provider=session_key.provider,
                session_key=session_key,
            ).to_dict()
        )
        return PresentationPlan(PresentationPlanKind.BLOCKED, self.host_id, error=error)

    def _blocked_new(
        self,
        code: str,
        message: str,
        observed_at: int,
        *,
        retryable: bool = False,
        provider: ProviderId | None = ProviderId.CODEX,
    ) -> PresentationPlan:
        error = ErrorRecord.from_dict(
            ErrorRecord(
                code,
                message,
                ErrorScope.PROJECT,
                retryable,
                observed_at,
                host_id=self.host_id,
                provider=provider,
            ).to_dict()
        )
        return PresentationPlan(PresentationPlanKind.BLOCKED, self.host_id, error=error)

    def _blocked_target(
        self,
        code: str,
        message: str,
        session_key: SessionKey | None,
        observed_at: int,
        *,
        retryable: bool = False,
        provider: ProviderId | None = ProviderId.CODEX,
    ) -> PresentationPlan:
        if session_key is not None:
            return self._blocked(
                code,
                message,
                session_key,
                observed_at,
                retryable=retryable,
            )
        return self._blocked_new(
            code,
            message,
            observed_at,
            retryable=retryable,
            provider=provider,
        )

    @staticmethod
    def _lease_owner(launch_id: str) -> str:
        return f"bootstrap:{launch_id}"

    def _fail_launch(self, launch_id: str, lease_owner: str, code: str) -> None:
        launch = self.registry.get_launch(launch_id)
        if launch is None or launch["state"] in {"bound", "failed", "expired"}:
            return
        observed_at = max(self.clock(), int(launch["updated_at"]))
        if observed_at >= int(launch["expires_at"]):
            with suppress(StorageError, sqlite3.Error):
                self.registry.transition_launch(
                    launch_id,
                    "expired",
                    observed_at=max(observed_at, int(launch["expires_at"])),
                )
            return
        with suppress(StorageError, sqlite3.Error):
            self.registry.transition_launch(
                launch_id,
                "failed",
                lease_owner=lease_owner,
                observed_at=observed_at,
                failure_code=code,
            )

    def bootstrap(
        self,
        launch_id: str,
        *,
        expected_surface_id: str | None = None,
        reconcile_runtime: ReconcileRuntime | None = None,
        exec_provider: ExecProvider = _exec_provider,
    ) -> int:
        launch_id = self._request_id(launch_id)
        lease_owner = self._lease_owner(launch_id)
        deadline = time.monotonic() + BOOTSTRAP_START_WAIT_SECONDS
        launch = self.registry.get_launch(launch_id)
        while launch is not None and launch["state"] in {"reserved", "surface_ready"}:
            if time.monotonic() >= deadline:
                self._fail_launch(launch_id, lease_owner, "surface_activation_timeout")
                return 1
            self.sleeper(0.02)
            launch = self.registry.get_launch(launch_id)
        if launch is None:
            raise PresentationError("unknown bootstrap launch")
        try:
            provider = ProviderId(str(launch["provider"]))
        except (TypeError, ValueError) as error:
            raise PresentationError(
                "bootstrap launch has an invalid provider"
            ) from error
        if launch["host_id"] != str(self.host_id) or launch["action"] not in {
            "new",
            "resume",
            "history",
        }:
            raise PresentationError("bootstrap launch is not a supported local session")
        if launch["state"] != "waiting_for_client":
            return 0 if launch["state"] == "bound" else 1
        surface_id = launch.get("surface_id")
        target_session_key = launch.get("target_session_key")
        if (
            not isinstance(surface_id, str)
            or (
                launch["action"] == "resume" and not isinstance(target_session_key, str)
            )
            or (
                launch["action"] in {"new", "history"}
                and target_session_key is not None
            )
        ):
            self._fail_launch(launch_id, lease_owner, "invalid_launch_identity")
            return 1
        if expected_surface_id is not None and surface_id != expected_surface_id:
            self._fail_launch(launch_id, lease_owner, "surface_identity_mismatch")
            return 1
        surface = self.registry.get_surface(surface_id)
        if surface is None:
            self._fail_launch(launch_id, lease_owner, "surface_missing")
            return 1
        if (
            surface["host_id"] != str(self.host_id)
            or surface["provider"] != provider.value
            or surface["role"] != "session"
            or surface["launch_id"] != launch_id
            or surface["retired_at"] is not None
        ):
            self._fail_launch(launch_id, lease_owner, "surface_identity_mismatch")
            return 1
        try:
            locator = TmuxLocator.from_storage(surface["transport_locator"])
            observed = self.tmux.inspect_locator(locator)
        except TmuxError:
            self._fail_launch(launch_id, lease_owner, "surface_unavailable")
            return 1
        if not self._metadata_matches(
            observed,
            surface_id=surface_id,
            session_key=target_session_key,
            provider=provider.value,
            launch_id=launch_id,
        ):
            self._fail_launch(launch_id, lease_owner, "surface_identity_mismatch")
            return 1
        remaining = max(0.0, (int(launch["expires_at"]) - self.clock()) / 1_000)
        try:
            attached = self.tmux.wait_for_client(
                locator,
                deadline=time.monotonic() + remaining,
            )
        except TmuxError:
            self._fail_launch(launch_id, lease_owner, "surface_unavailable")
            return 1
        if not attached:
            observed_at = max(self.clock(), int(launch["expires_at"]))
            with suppress(StorageError, sqlite3.Error):
                self.registry.transition_launch(
                    launch_id,
                    "expired",
                    observed_at=observed_at,
                )
            return 1
        if reconcile_runtime is not None:
            try:
                reconcile_runtime()
            except (OSError, StorageError, sqlite3.Error, ValueError):
                self._fail_launch(
                    launch_id, lease_owner, "runtime_reconciliation_failed"
                )
                return 1
        refreshed_launch = self.registry.get_launch(launch_id)
        if refreshed_launch is None:
            raise PresentationError("bootstrap launch disappeared")
        if refreshed_launch["state"] == "bound":
            return 0
        if refreshed_launch["state"] != "waiting_for_client":
            self._fail_launch(launch_id, lease_owner, "launch_state_changed")
            return 1
        launch = refreshed_launch
        if launch["action"] == "resume":
            assert isinstance(target_session_key, str)
            session = self.registry.get_session(target_session_key)
            if session is None:
                self._fail_launch(launch_id, lease_owner, "target_session_missing")
                return 1
            if session["runtime_presence"] == "live":
                self._fail_launch(launch_id, lease_owner, "duplicate_runtime_detected")
                return 1
        elif not self._new_target_is_current(launch):
            self._fail_launch(launch_id, lease_owner, "launch_target_changed")
            return 1
        transition_at = max(self.clock(), int(launch["updated_at"]))
        provider_executable = self._provider_executable(provider)
        if provider_executable is None:
            self._fail_launch(launch_id, lease_owner, "provider_unavailable")
            return 1
        try:
            self.registry.renew_launch_lease(
                launch_id,
                lease_owner=lease_owner,
                expires_at=transition_at + PROVIDER_BIND_GRACE_SECONDS * 1_000,
                observed_at=transition_at,
            )
            self.registry.transition_launch(
                launch_id,
                "provider_started",
                lease_owner=lease_owner,
                observed_at=transition_at,
            )
        except StorageError:
            self._fail_launch(launch_id, lease_owner, "provider_start_rejected")
            return 1
        if launch["action"] == "resume":
            assert isinstance(target_session_key, str)
            parsed_key = SessionKey.parse(target_session_key)
            if provider is ProviderId.CODEX:
                argv = (
                    provider_executable,
                    "resume",
                    str(parsed_key.provider_session_id),
                )
            else:
                argv = (
                    provider_executable,
                    "--resume",
                    str(parsed_key.provider_session_id),
                )
        elif launch["action"] == "history":
            argv = (provider_executable, "--resume")
        else:
            argv = (provider_executable,)
        try:
            exec_provider(provider_executable, argv)
        except OSError:
            self._fail_launch(launch_id, lease_owner, "provider_exec_failed")
            return 1
        self._fail_launch(launch_id, lease_owner, "provider_exec_returned")
        return 1

    def _new_target_is_current(self, launch: dict[str, object]) -> bool:
        project_id = launch.get("project_id")
        checkout_id = launch.get("checkout_id")
        cwd = launch.get("cwd")
        if not all(isinstance(value, str) for value in (project_id, checkout_id, cwd)):
            return False
        project = self.projects.get(str(project_id))
        checkout = self.checkouts.get(str(checkout_id))
        if project is None or checkout is None:
            return False
        if (
            str(checkout.repository_id)
            not in self.project_repository_ids.get(str(project.project_id), set())
            or checkout.host_id != self.host_id
            or str(checkout.path) != cwd
            or not checkout.path.is_dir()
        ):
            return False
        transport = checkout.transport_override or project.default_transport
        if transport is not Transport.TMUX:
            return False
        try:
            process_cwd = self.cwd_reader().resolve(strict=False)
        except OSError:
            return False
        return process_cwd == checkout.path


def actionable_surface_locator(
    registry: Registry,
    *,
    host_id: HostId | str,
    surface_id: str,
    tmux: TmuxController,
    observed_at: int | None = None,
) -> TmuxLocator:
    """Revalidate one stored surface before an attach or client switch."""

    parsed_host = host_id if isinstance(host_id, HostId) else HostId(host_id)
    now = _now_ms() if observed_at is None else observed_at
    surface = registry.get_surface(surface_id)
    if surface is None:
        raise PresentationError("unknown surface")
    try:
        provider = ProviderId(str(surface["provider"]))
    except (TypeError, ValueError) as error:
        raise PresentationError("surface has an invalid provider") from error
    if (
        surface["host_id"] != str(parsed_host)
        or surface["transport"] != "tmux"
        or surface["role"] != "session"
        or surface["retired_at"] is not None
    ):
        raise PresentationError("surface is not an active local session surface")

    launch_id = surface["launch_id"]
    expected_session_key = surface["current_session_key"]
    if launch_id is not None:
        launch = registry.get_launch(str(launch_id))
        if (
            launch is None
            or launch["surface_id"] != surface_id
            or launch["host_id"] != str(parsed_host)
            or launch["provider"] != provider.value
            or launch["action"] not in {"new", "resume", "history"}
            or launch["state"]
            not in {"waiting_for_client", "provider_started", "bound"}
        ):
            raise PresentationError("surface launch is no longer actionable")
        if launch["state"] != "bound" and now >= int(launch["expires_at"]):
            raise PresentationError("surface launch lease has expired")
        if expected_session_key is None and launch["action"] == "resume":
            expected_session_key = launch["target_session_key"]
        if launch["state"] == "bound":
            if (
                not isinstance(launch["target_session_key"], str)
                or expected_session_key != launch["target_session_key"]
                or surface["binding_confidence"] != "confirmed"
            ):
                raise PresentationError("bound surface identity is inconsistent")
        elif launch["action"] in {"new", "history"} and (
            expected_session_key is not None
            or launch["target_session_key"] is not None
            or surface["binding_confidence"] != "unknown"
        ):
            raise PresentationError("waiting new surface is already bound")
    elif expected_session_key is None or surface["binding_confidence"] != "confirmed":
        raise PresentationError("surface does not have a confirmed session binding")
    if expected_session_key is not None and not isinstance(expected_session_key, str):
        raise PresentationError("surface has no target session")

    try:
        locator = TmuxLocator.from_storage(surface["transport_locator"])
        observed = tmux.inspect_locator(locator)
    except TmuxError as error:
        raise PresentationError("surface tmux target is unavailable") from error
    if isinstance(expected_session_key, str):
        observed = _synchronize_bound_unbound_metadata(
            registry,
            tmux,
            surface,
            observed,
            expected_session_key,
        )
    if not LaunchCoordinator._metadata_matches(
        observed,
        surface_id=surface_id,
        session_key=expected_session_key,
        provider=provider.value,
        launch_id=launch_id,
    ):
        raise PresentationError("surface tmux identity did not revalidate")
    return locator


def select_surface(
    registry: Registry,
    *,
    host_id: HostId | str,
    surface_id: str,
    client: str,
    tmux: TmuxController,
) -> None:
    locator = actionable_surface_locator(
        registry,
        host_id=host_id,
        surface_id=surface_id,
        tmux=tmux,
    )
    tmux.select_surface(locator, client=client)


def attach_surface_argv(
    registry: Registry,
    *,
    host_id: HostId | str,
    surface_id: str,
    tmux: TmuxController,
) -> list[str]:
    locator = actionable_surface_locator(
        registry,
        host_id=host_id,
        surface_id=surface_id,
        tmux=tmux,
    )
    return tmux.attach_argv(locator)


__all__ = [
    "BOOTSTRAP_START_WAIT_SECONDS",
    "PREPARE_CAPABILITY_HASH",
    "PREPARE_CLAUDE_CAPABILITY_HASH",
    "PREPARE_CLAUDE_HISTORY_CAPABILITY_HASH",
    "PREPARE_NEW_CAPABILITY_HASH",
    "PREPARE_NEW_CLAUDE_CAPABILITY_HASH",
    "PREPARE_SURFACE_WAIT_SECONDS",
    "PROVIDER_BIND_GRACE_SECONDS",
    "LaunchCoordinator",
    "PresentationError",
    "actionable_surface_locator",
    "attach_surface_argv",
    "select_surface",
]
