"""Phase 6D workspace/one-child ownership and trusted transition runtime."""

from __future__ import annotations

import secrets
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .codex_app_server import delete_empty_session, reserve_named_session
from .domain import (
    Activity,
    ActivityReason,
    AgentCapability,
    BackgroundState,
    BriefId,
    CapabilityId,
    CloseReason,
    CompleteReturnPolicy,
    CompletionHandoff,
    ControlKind,
    ControlState,
    ControlTransport,
    ControlTurn,
    ControlTurnId,
    ControlTurnPolicy,
    CreatedBy,
    Frame,
    FrameId,
    FrameLifecycleState,
    FramePlacement,
    FrameRole,
    FrameSession,
    FrameSessionId,
    HandoffId,
    LaunchAction,
    LaunchId,
    LaunchIntent,
    LaunchState,
    MembershipReason,
    PlacementId,
    PlacementState,
    ProviderId,
    ProviderSession,
    Recovery,
    RecoveryActionability,
    RecoveryId,
    RecoveryState,
    RequestId,
    Resumability,
    RuntimePresence,
    SessionKey,
    Surface,
    SurfaceId,
    SurfaceState,
    TaskPushPolicy,
    TransitionBrief,
    TransitionId,
    TransitionKind,
    TransitionState,
    TransportPhase,
    ViewId,
    ViewState,
    ViewTransition,
    content_hash,
    request_fingerprint,
)
from .generation import GenerationPaths, OpenGeneration
from .provider_runtime import (
    CONTROL_PROMPT,
    ProviderCommand,
    ProviderContract,
    build_new_command,
    build_resume_command,
    probe_contract,
)
from .storage import ConflictError, Registry, TransitionClaim
from .tmux_view import PaneObservation, TmuxExecutor

CAPABILITY_TTL_MS = 24 * 60 * 60 * 1_000
EXECUTION_LEASE_MS = 30_000


def spawn_control_watchdog(
    paths: GenerationPaths,
    generation_id,
    transition_id: TransitionId,
    *,
    delay_seconds: int,
) -> None:
    """Start one detached, generation-bound settlement helper."""

    subprocess.Popen(
        (
            sys.executable,
            "-m",
            "agent_switchboard",
            "--config-root",
            str(paths.config_root),
            "--state-root",
            str(paths.state_root),
            "control-watchdog",
            "--transition",
            str(transition_id),
            "--generation-id",
            str(generation_id),
            "--delay-ms",
            str(delay_seconds * 1_000),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


class WorkflowError(RuntimeError):
    """A Phase 6D workflow precondition or exact side effect failed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class SessionAllocator(Protocol):
    def allocate(
        self, provider: ProviderId, title: str, contract: ProviderContract
    ) -> UUID: ...

    def cleanup(
        self, provider: ProviderId, session_id: UUID, contract: ProviderContract
    ) -> None: ...


class NativeSessionAllocator:
    """Reserve only provider UUIDs supported by an accepted native contract."""

    def allocate(
        self, provider: ProviderId, title: str, contract: ProviderContract
    ) -> UUID:
        if provider is ProviderId.CODEX:
            return reserve_named_session(contract.executable, title)
        return uuid4()

    def cleanup(
        self, provider: ProviderId, session_id: UUID, contract: ProviderContract
    ) -> None:
        if provider is ProviderId.CODEX:
            delete_empty_session(contract.executable, session_id)


@dataclass(frozen=True, slots=True)
class PreparedTransition:
    transition_id: TransitionId
    source_frame_id: FrameId
    target_frame_id: FrameId
    state: TransitionState


@dataclass(frozen=True, slots=True)
class StopResult:
    action: str
    transition_id: TransitionId | None
    state: TransitionState | None


def _stable_id(value_type, *parts: object):
    raw = ":".join(str(part) for part in parts)
    return value_type(uuid5(NAMESPACE_URL, f"agent-switchboard:v3:workflow:{raw}"))


class WorkflowRuntime:
    """Coordinate exact durable ownership with native tmux/provider runtimes."""

    def __init__(
        self,
        opened: OpenGeneration,
        paths: GenerationPaths,
        *,
        tmux: TmuxExecutor | None = None,
        allocator: SessionAllocator | None = None,
        contracts: dict[ProviderId, ProviderContract] | None = None,
        capability_factory: Callable[[], str] | None = None,
        watchdog_launcher: Callable[[TransitionId], None] | None = None,
    ) -> None:
        self.opened = opened
        self.paths = paths
        self.registry: Registry = opened.registry
        self.config = opened.config
        self.host_id = opened.config.host.host_id
        self.generation_id = opened.generation_id
        self.tmux = tmux or TmuxExecutor()
        self.allocator = allocator or NativeSessionAllocator()
        self.contracts = dict(contracts or {})
        self.capability_factory = capability_factory or (
            lambda: secrets.token_urlsafe(48)
        )
        self.watchdog_launcher = watchdog_launcher or (lambda _transition_id: None)

    def _watch_control(self, transition_id: TransitionId) -> None:
        try:
            self.watchdog_launcher(transition_id)
        except OSError as error:
            raise WorkflowError(
                "control_watchdog_start_failed",
                "control settlement watchdog could not start",
            ) from error

    def _require_mutation(self, operation: str) -> None:
        self.opened.require_mutation(operation)

    def _provider_contract(self, provider: ProviderId) -> ProviderContract:
        cached = self.contracts.get(provider)
        if cached is not None:
            return cached
        configured = next(
            (item for item in self.config.providers if item.provider is provider), None
        )
        if configured is None or not configured.enabled:
            raise WorkflowError(
                "provider_disabled", f"{provider.value} provider is not enabled"
            )
        contract = probe_contract(provider, executable=configured.executable)
        self.contracts[provider] = contract
        return contract

    def _tmux_for_view(self, view_id) -> tuple[object, TmuxExecutor]:
        view = self.registry.get_view(view_id)
        if view.tmux_server_id is None:
            raise WorkflowError("view_tmux_missing", "view has no tmux server")
        server = self.registry.get_tmux_server(view.tmux_server_id)
        return view, TmuxExecutor(server.socket_path, executable=self.tmux.executable)

    def _project(self, project_id):
        return next(
            (
                project
                for project in self.config.projects
                if project.project_id == project_id
            ),
            None,
        )

    def _provider_for_frame(
        self, frame: Frame, requested: ProviderId | None
    ) -> ProviderId:
        if requested is not None:
            return requested
        project = self._project(frame.project_id)
        return (
            frame.preferred_provider
            or (None if project is None else project.default_provider)
            or (
                frame.current_session_key.provider
                if frame.current_session_key is not None
                else ProviderId.CODEX
            )
        )

    def _push_policy(self, frame: Frame) -> TaskPushPolicy:
        project = self._project(frame.project_id)
        return (
            project.task_push
            if project is not None and project.task_push is not None
            else self.config.automation.task_push
        )

    def _complete_policy(self, frame: Frame) -> CompleteReturnPolicy:
        project = self._project(frame.project_id)
        return (
            project.complete_return
            if project is not None and project.complete_return is not None
            else self.config.automation.complete_return
        )

    def _open_recovery(
        self,
        *,
        kind: str,
        subject_type: str,
        subject_id: str,
        explanation: str,
        now: int,
        actionability: RecoveryActionability = RecoveryActionability.OPEN_VIEW,
    ) -> None:
        recovery = Recovery(
            _stable_id(RecoveryId, self.host_id, kind, subject_type, subject_id),
            self.host_id,
            kind,
            subject_type,
            subject_id,
            actionability,
            RecoveryState.OPEN,
            explanation,
            now,
            now,
        )
        with suppress(ConflictError):
            self.registry.open_recovery(recovery)

    @staticmethod
    def _prepared(transition: ViewTransition) -> PreparedTransition:
        assert transition.source_frame_id is not None
        return PreparedTransition(
            transition.transition_id,
            transition.source_frame_id,
            transition.target_frame_id,
            transition.state,
        )

    def task_push(
        self,
        raw_capability: str,
        *,
        title: str,
        brief: str,
        purpose: str | None = None,
        provider: ProviderId | None = None,
        park_safe: bool = True,
        request_id: RequestId | None = None,
        now: int,
    ) -> PreparedTransition:
        self._require_mutation("task push")
        capability = self.registry.validate_capability(raw_capability, now=now)
        source = self.registry.get_frame(capability.frame_id)
        if self._push_policy(source) is TaskPushPolicy.OFF:
            raise WorkflowError("task_push_disabled", "task push is disabled")
        if source.role is not FrameRole.WORKSPACE:
            raise WorkflowError(
                "task_depth", "initial one-child automation starts at a workspace"
            )
        if self.config.automation.initial_max_depth != 1:
            raise WorkflowError("task_depth", "Phase 6D requires maximum depth one")
        context = self.registry.get_work_context(source.work_context_id)
        if (
            not park_safe
            or context.background_state is not BackgroundState.SAFE
            or context.foreground_frame_id != source.frame_id
        ):
            raise WorkflowError(
                "park_unsafe", "source has not confirmed safe foreground transfer"
            )
        request = request_id or RequestId(uuid4())
        fingerprint = request_fingerprint(
            "transition.push",
            {
                "viewId": str(capability.view_id),
                "sourceFrameId": str(source.frame_id),
                "title": title,
                "brief": brief,
                "purpose": purpose,
                "provider": None if provider is None else provider.value,
            },
        )
        existing = self.registry.find_transition_by_request(self.host_id, request)
        if existing is not None:
            if (
                existing.request_fingerprint != fingerprint
                or existing.kind is not TransitionKind.PUSH
            ):
                raise WorkflowError(
                    "request_conflict", "request identifies another operation"
                )
            return self._prepared(existing)

        selected = self._provider_for_frame(source, provider)
        contract = self._provider_contract(selected)
        session_id = self.allocator.allocate(selected, title, contract)
        session_key = SessionKey(self.host_id, selected, session_id)
        child_id = _stable_id(FrameId, self.host_id, request, "child")
        transition_id = _stable_id(TransitionId, self.host_id, request, "push")
        launch_id = _stable_id(LaunchId, self.host_id, request, "launch")
        surface_id = _stable_id(SurfaceId, self.host_id, request, "surface")
        placement_id = _stable_id(PlacementId, self.host_id, request, "placement")
        try:
            session = ProviderSession(
                session_key,
                self.host_id,
                selected,
                session_id,
                source.project_id,
                context.checkout_id,
                title,
                purpose,
                False,
                RuntimePresence.STOPPED,
                Resumability.RESUMABLE,
                Activity.READY,
                ActivityReason.TURN_COMPLETE,
                now,
                now,
                now,
                now,
            )
            frame = Frame(
                child_id,
                self.host_id,
                source.project_id,
                FrameRole.TASK,
                source.frame_id,
                source.work_context_id,
                title,
                purpose,
                selected,
                FrameLifecycleState.OPEN,
                None,
                session_key,
                CreatedBy.AGENT,
                now,
                now,
            )
            placement = FramePlacement(
                placement_id,
                self.host_id,
                capability.view_id,
                child_id,
                surface_id,
                PlacementState.STAGED,
                0,
                None,
                now,
            )
            launch = LaunchIntent(
                launch_id,
                request,
                self.host_id,
                child_id,
                selected,
                LaunchAction.NEW,
                None,
                LaunchState.PLANNED,
                None,
                now,
                now,
            )
            surface = Surface(
                surface_id,
                self.host_id,
                selected,
                None,
                launch_id,
                SurfaceState.PLANNED,
                None,
                None,
                None,
                None,
                0,
                now,
                now,
                None,
            )
            transition = ViewTransition(
                transition_id,
                request,
                fingerprint,
                self.host_id,
                capability.view_id,
                TransitionKind.PUSH,
                source.frame_id,
                child_id,
                source.work_context_id,
                self.registry.get_view(capability.view_id).revision,
                context.claim_generation,
                TransitionState.PREPARED,
                None,
                None,
                TransportPhase.INTENT,
                None,
                now,
                now,
            )
            semantic = TransitionBrief(
                _stable_id(BriefId, transition_id, "brief"),
                transition_id,
                source.frame_id,
                capability.session_key,
                child_id,
                brief,
                content_hash(brief),
                now,
                None,
            )
            control = ControlTurn(
                _stable_id(ControlTurnId, transition_id, "control"),
                transition_id,
                child_id,
                session_key,
                ControlKind.CLAIM_BRIEF,
                "control.claim.v1",
                ControlTransport.RESUME_INITIAL,
                ControlState.PREPARED,
                0,
                None,
                None,
                None,
                None,
                None,
            )
            transition = self.registry.prepare_task_push(
                session=session,
                membership=FrameSession(
                    _stable_id(FrameSessionId, child_id, session_key),
                    child_id,
                    session_key,
                    1,
                    MembershipReason.STARTED,
                    now,
                ),
                frame=frame,
                placement=placement,
                launch=launch,
                surface=surface,
                transition=transition,
                brief=semantic,
                control=control,
            )
        except Exception:
            with suppress(Exception):
                self.allocator.cleanup(selected, session_id, contract)
            raise

        try:
            _view, executor = self._tmux_for_view(capability.view_id)
            staged = executor.spawn_surface(
                prefix=self.config.tmux.naming_prefix,
                generation_id=self.generation_id,
                view_id=capability.view_id,
                frame_id=str(child_id),
                surface_id=str(surface_id),
                command=("/usr/bin/sleep", "86400"),
            )
            if not staged.input_off:
                raise WorkflowError(
                    "staged_surface_unfenced", "staged surface input is not fenced"
                )
        except Exception as error:
            self._open_recovery(
                kind="push_staging",
                subject_type="transition",
                subject_id=str(transition_id),
                explanation="The prepared child pane must be reconciled before Stop.",
                now=now,
            )
            raise WorkflowError("push_staging_failed", str(error)) from error
        return self._prepared(transition)

    def task_back(
        self,
        raw_capability: str,
        *,
        park_safe: bool = True,
        request_id: RequestId | None = None,
        now: int,
    ) -> PreparedTransition:
        return self._prepare_parent_transition(
            raw_capability,
            TransitionKind.BACK,
            park_safe=park_safe,
            request_id=request_id,
            now=now,
        )

    def _prepare_parent_transition(
        self,
        raw_capability: str,
        kind: TransitionKind,
        *,
        park_safe: bool,
        request_id: RequestId | None,
        now: int,
    ) -> PreparedTransition:
        self._require_mutation(f"task {kind.value}")
        capability = self.registry.validate_capability(raw_capability, now=now)
        source = self.registry.get_frame(capability.frame_id)
        if source.role is not FrameRole.TASK or source.parent_frame_id is None:
            raise WorkflowError("task_parent_missing", "current frame has no parent")
        context = self.registry.get_work_context(source.work_context_id)
        if (
            not park_safe
            or context.background_state is not BackgroundState.SAFE
            or context.foreground_frame_id != source.frame_id
        ):
            raise WorkflowError(
                "park_unsafe", "task has not confirmed safe foreground transfer"
            )
        request = request_id or RequestId(uuid4())
        self._stage_parent_resume(
            source.parent_frame_id,
            capability.view_id,
            request,
            now=now,
        )
        fingerprint = request_fingerprint(
            f"transition.{kind.value}",
            {
                "viewId": str(capability.view_id),
                "sourceFrameId": str(source.frame_id),
                "targetFrameId": str(source.parent_frame_id),
            },
        )
        transition = ViewTransition(
            _stable_id(TransitionId, self.host_id, request, kind.value),
            request,
            fingerprint,
            self.host_id,
            capability.view_id,
            kind,
            source.frame_id,
            source.parent_frame_id,
            source.work_context_id,
            self.registry.get_view(capability.view_id).revision,
            context.claim_generation,
            TransitionState.PREPARED,
            None,
            None,
            TransportPhase.INTENT,
            None,
            now,
            now,
        )
        return self._prepared(self.registry.prepare_transition(transition))

    def task_complete_return(
        self,
        raw_capability: str,
        *,
        summary: str,
        next_action: str,
        park_safe: bool = True,
        request_id: RequestId | None = None,
        now: int,
    ) -> PreparedTransition:
        self._require_mutation("task complete return")
        capability = self.registry.validate_capability(raw_capability, now=now)
        source = self.registry.get_frame(capability.frame_id)
        if source.role is not FrameRole.TASK or source.parent_frame_id is None:
            raise WorkflowError("task_parent_missing", "current frame has no parent")
        context = self.registry.get_work_context(source.work_context_id)
        if (
            not park_safe
            or context.background_state is not BackgroundState.SAFE
            or context.foreground_frame_id != source.frame_id
        ):
            raise WorkflowError(
                "park_unsafe", "task has not confirmed safe foreground transfer"
            )
        parent = self.registry.get_frame(source.parent_frame_id)
        if parent.current_session_key is None:
            raise WorkflowError(
                "parent_session_missing", "parent has no exact resumable session"
            )
        request = request_id or RequestId(uuid4())
        resumed_parent = self._stage_parent_resume(
            parent.frame_id,
            capability.view_id,
            request,
            now=now,
        )
        fingerprint = request_fingerprint(
            "transition.complete_return",
            {
                "viewId": str(capability.view_id),
                "sourceFrameId": str(source.frame_id),
                "targetFrameId": str(parent.frame_id),
                "summary": summary,
                "nextAction": next_action,
            },
        )
        transition_id = _stable_id(
            TransitionId, self.host_id, request, "complete_return"
        )
        transition = ViewTransition(
            transition_id,
            request,
            fingerprint,
            self.host_id,
            capability.view_id,
            TransitionKind.COMPLETE_RETURN,
            source.frame_id,
            parent.frame_id,
            source.work_context_id,
            self.registry.get_view(capability.view_id).revision,
            context.claim_generation,
            TransitionState.PREPARED,
            None,
            None,
            TransportPhase.INTENT,
            None,
            now,
            now,
        )
        handoff = CompletionHandoff(
            _stable_id(HandoffId, transition_id, "handoff"),
            transition_id,
            source.frame_id,
            capability.session_key,
            parent.frame_id,
            summary,
            next_action,
            content_hash(summary, next_action),
            now,
            None,
        )
        control = None
        if self._complete_policy(source) is CompleteReturnPolicy.SYNTHESIZE:
            control = ControlTurn(
                _stable_id(ControlTurnId, transition_id, "control"),
                transition_id,
                parent.frame_id,
                parent.current_session_key,
                ControlKind.CLAIM_HANDOFF,
                "control.claim.v1",
                (
                    ControlTransport.RESUME_INITIAL
                    if resumed_parent
                    else ControlTransport.LIVE_INPUT
                ),
                ControlState.PREPARED,
                0,
                None,
                None,
                None,
                None,
                None,
            )
        return self._prepared(
            self.registry.prepare_complete_return(transition, handoff, control)
        )

    def _stage_parent_resume(
        self,
        frame_id: FrameId,
        view_id,
        request_id: RequestId,
        *,
        now: int,
    ) -> bool:
        placement = next(
            (
                item
                for item in self.registry.list_placements(view_id=view_id)
                if item.frame_id == frame_id
            ),
            None,
        )
        if placement is None:
            raise WorkflowError(
                "parent_placement_missing", "parent has no exact view affinity"
            )
        if placement.state in {PlacementState.PARKED, PlacementState.ACTIVE}:
            frame = self.registry.get_frame(frame_id)
            session_key = frame.current_session_key
            if session_key is None:
                raise WorkflowError("parent_session_missing", "parent has no session")
            if placement.surface_id is None:
                raise WorkflowError(
                    "parent_surface_missing", "parent runtime surface is missing"
                )
            surface = self.registry.get_surface(placement.surface_id)
            if surface.lifecycle_state is not SurfaceState.LIVE:
                raise WorkflowError(
                    "parent_surface_unavailable", "parent runtime is not live"
                )
            session = self.registry.get_provider_session(session_key)
            if (
                surface.session_key != session_key
                or surface.pane_id is None
                or session.runtime_presence is not RuntimePresence.LIVE
                or session.resumability is not Resumability.RESUMABLE
                or session.activity is not Activity.READY
                or session.activity_reason is not ActivityReason.TURN_COMPLETE
            ):
                raise WorkflowError(
                    "control_target_unready",
                    "parent is not an exact verified-idle owned runtime",
                )
            if self.config.control_turns.transport is ControlTurnPolicy.LIVE_FIRST:
                return False
            _view, executor = self._tmux_for_view(view_id)
            try:
                executor.stop_surface(
                    generation_id=self.generation_id,
                    view_id=view_id,
                    surface_id=str(surface.surface_id),
                    pane_id=surface.pane_id,
                )
                self.registry.advance_surface_state(
                    surface.surface_id,
                    surface.metadata_generation,
                    SurfaceState.DEAD,
                    now=now,
                )
                placement = self.registry.advance_placement(
                    placement.placement_id,
                    placement.generation,
                    PlacementState.STOPPED_AFFINITY,
                    now=now,
                )
                self.registry.upsert_provider_session(
                    ProviderSession(
                        session.session_key,
                        session.host_id,
                        session.provider,
                        session.provider_session_id,
                        session.project_id,
                        session.checkout_id,
                        session.name,
                        session.purpose,
                        session.pinned,
                        RuntimePresence.STOPPED,
                        session.resumability,
                        session.activity,
                        session.activity_reason,
                        session.created_at,
                        session.provider_updated_at,
                        now,
                        now,
                    )
                )
            except Exception as error:
                raise WorkflowError(
                    "parent_resume_stop_failed",
                    "verified idle parent could not be stopped for exact resume",
                ) from error
        if placement.state is not PlacementState.STOPPED_AFFINITY:
            raise WorkflowError(
                "parent_affinity_unavailable", "parent affinity cannot be resumed"
            )
        frame = self.registry.get_frame(frame_id)
        session_key = frame.current_session_key
        if session_key is None:
            raise WorkflowError("parent_session_missing", "parent has no session")
        launch_id = _stable_id(LaunchId, self.host_id, request_id, "parent-resume")
        surface_id = _stable_id(SurfaceId, self.host_id, request_id, "parent-resume")
        launch = LaunchIntent(
            launch_id,
            _stable_id(RequestId, request_id, "parent-resume"),
            self.host_id,
            frame_id,
            session_key.provider,
            LaunchAction.RESUME,
            session_key,
            LaunchState.PLANNED,
            None,
            now,
            now,
        )
        surface = Surface(
            surface_id,
            self.host_id,
            session_key.provider,
            None,
            launch_id,
            SurfaceState.PLANNED,
            None,
            None,
            None,
            None,
            0,
            now,
            now,
            None,
        )
        self.registry.prepare_resume_surface(
            launch,
            surface,
            placement.placement_id,
            placement.generation,
            now=now,
        )
        try:
            _view, executor = self._tmux_for_view(view_id)
            pane = executor.spawn_surface(
                prefix=self.config.tmux.naming_prefix,
                generation_id=self.generation_id,
                view_id=view_id,
                frame_id=str(frame_id),
                surface_id=str(surface_id),
                command=("/usr/bin/sleep", "86400"),
            )
            if not pane.input_off:
                raise WorkflowError(
                    "resume_surface_unfenced", "resume surface input is not fenced"
                )
        except Exception as error:
            self._open_recovery(
                kind="parent_resume_staging",
                subject_type="frame",
                subject_id=str(frame_id),
                explanation="The exact parent resume pane needs reconciliation.",
                now=now,
            )
            raise WorkflowError("parent_resume_staging_failed", str(error)) from error
        return True

    def task_human_close(
        self,
        raw_capability: str,
        *,
        park_safe: bool = True,
        request_id: RequestId | None = None,
        now: int,
    ) -> PreparedTransition:
        return self._prepare_parent_transition(
            raw_capability,
            TransitionKind.HUMAN_CLOSE,
            park_safe=park_safe,
            request_id=request_id,
            now=now,
        )

    def human_back(
        self, view_id: ViewId, *, request_id: RequestId | None = None, now: int
    ) -> ViewTransition:
        return self._human_parent_action(
            view_id, TransitionKind.BACK, request_id=request_id, now=now
        )

    def human_close(
        self, view_id: ViewId, *, request_id: RequestId | None = None, now: int
    ) -> ViewTransition:
        return self._human_parent_action(
            view_id, TransitionKind.HUMAN_CLOSE, request_id=request_id, now=now
        )

    def _human_parent_action(
        self,
        view_id: ViewId,
        kind: TransitionKind,
        *,
        request_id: RequestId | None,
        now: int,
    ) -> ViewTransition:
        """Execute a user-requested model-free parent action at exact idle."""

        self._require_mutation(f"human {kind.value}")
        view = self.registry.get_view(view_id)
        if view.state is not ViewState.READY or view.active_frame_id is None:
            raise WorkflowError("view_not_ready", "view has no ready active frame")
        source = self.registry.get_frame(view.active_frame_id)
        if source.role is not FrameRole.TASK or source.parent_frame_id is None:
            raise WorkflowError("task_parent_missing", "active frame has no parent")
        if source.current_session_key is None:
            raise WorkflowError("source_session_missing", "task has no session")
        session = self.registry.get_provider_session(source.current_session_key)
        if (
            session.runtime_presence is not RuntimePresence.LIVE
            or session.activity is not Activity.READY
            or session.activity_reason is not ActivityReason.TURN_COMPLETE
        ):
            raise WorkflowError(
                "source_not_idle", "human navigation requires an exact idle boundary"
            )
        context = self.registry.get_work_context(source.work_context_id)
        if (
            context.background_state is not BackgroundState.SAFE
            or context.foreground_frame_id != source.frame_id
        ):
            raise WorkflowError("park_unsafe", "task is not safe to park")
        request = request_id or RequestId(uuid4())
        self._stage_parent_resume(
            source.parent_frame_id, view.view_id, request, now=now
        )
        fingerprint = request_fingerprint(
            f"transition.{kind.value}",
            {
                "viewId": str(view.view_id),
                "sourceFrameId": str(source.frame_id),
                "targetFrameId": str(source.parent_frame_id),
                "actor": "human",
            },
        )
        transition = self.registry.prepare_transition(
            ViewTransition(
                _stable_id(TransitionId, self.host_id, request, kind.value),
                request,
                fingerprint,
                self.host_id,
                view.view_id,
                kind,
                source.frame_id,
                source.parent_frame_id,
                source.work_context_id,
                view.revision,
                context.claim_generation,
                TransitionState.PREPARED,
                None,
                None,
                TransportPhase.INTENT,
                None,
                now,
                now,
            )
        )
        capability = self.registry.active_capability(view.view_id, now=now)
        _view, executor, pane, owner = self._present(transition, capability, now=now)
        presented = self.registry.get_transition(transition.transition_id)
        target = self._target_placement(presented)
        if target.surface_id is not None:
            target_launch = self.registry.get_launch(
                self.registry.get_surface(target.surface_id).launch_id
            )
            if (
                target_launch.state is LaunchState.PLANNED
                and target_launch.action is LaunchAction.RESUME
            ):
                self._launch_resumed_target(
                    presented, executor, pane, owner=owner, now=now
                )
        self._finish_model_free(
            presented,
            executor,
            pane,
            owner=owner,
            close=kind is TransitionKind.HUMAN_CLOSE,
            now=now,
        )
        return self.registry.get_transition(transition.transition_id)

    def cancel_push(
        self,
        raw_capability: str,
        transition_id: TransitionId,
        *,
        now: int,
    ) -> None:
        self._require_mutation("task push cancel")
        capability = self.registry.validate_capability(raw_capability, now=now)
        transition = self.registry.get_transition(transition_id)
        if (
            transition.kind is not TransitionKind.PUSH
            or transition.state is not TransitionState.PREPARED
            or transition.source_frame_id != capability.frame_id
            or transition.view_id != capability.view_id
        ):
            raise WorkflowError(
                "cancel_unauthorized", "capability does not own this prepared push"
            )
        self._cancel_prepared_push(transition, now=now)

    def supersede_for_manual(self, view_id, *, now: int) -> ViewTransition | None:
        """Let an explicit human view action supersede only prepared work."""

        self._require_mutation("manual transition supersession")
        transition = self.registry.nonterminal_transition_for_view(view_id)
        if transition is None:
            return None
        if transition.state is not TransitionState.PREPARED:
            raise WorkflowError(
                "transition_busy",
                "executing or later transition must settle or recover",
            )
        if transition.kind is TransitionKind.PUSH:
            self._cancel_prepared_push(transition, now=now)
            return None
        return self.registry.supersede_prepared_transition(
            transition.transition_id, now=now
        )

    def _cancel_prepared_push(self, transition: ViewTransition, *, now: int) -> None:
        placement = next(
            item
            for item in self.registry.list_placements(view_id=transition.view_id)
            if item.frame_id == transition.target_frame_id
        )
        if placement.surface_id is None:
            raise WorkflowError("cancel_surface_missing", "staged surface is missing")
        _view, executor = self._tmux_for_view(transition.view_id)
        matches = self._surface_panes(executor, transition, placement.surface_id)
        if len(matches) != 1:
            raise WorkflowError(
                "cancel_surface_uncertain", "staged surface locator is not exact"
            )
        executor.discard_staged_surface(
            generation_id=self.generation_id,
            view_id=transition.view_id,
            surface_id=str(placement.surface_id),
            pane_id=matches[0].pane_id,
        )
        session = self.registry.get_frame(
            transition.target_frame_id
        ).current_session_key
        self.registry.cancel_prepared_push(transition.transition_id)
        if session is not None:
            contract = self._provider_contract(session.provider)
            try:
                self.allocator.cleanup(
                    session.provider, session.provider_session_id, contract
                )
            except Exception as error:
                self._open_recovery(
                    kind="zero_turn_cleanup",
                    subject_type="session",
                    subject_id=str(session),
                    explanation="An exact zero-turn provider session needs cleanup.",
                    now=now,
                    actionability=RecoveryActionability.MANUAL,
                )
                raise WorkflowError("zero_turn_cleanup_failed", str(error)) from error

    def _surface_panes(
        self, executor: TmuxExecutor, transition: ViewTransition, surface_id: SurfaceId
    ) -> tuple[PaneObservation, ...]:
        return tuple(
            pane
            for pane in executor.panes()
            if pane.view_id == str(transition.view_id)
            and pane.generation_id == str(self.generation_id)
            and pane.frame_id == str(transition.target_frame_id)
            and pane.surface_id == str(surface_id)
        )

    def _target_placement(self, transition: ViewTransition) -> FramePlacement:
        matches = [
            placement
            for placement in self.registry.list_placements(view_id=transition.view_id)
            if placement.frame_id == transition.target_frame_id
        ]
        if len(matches) != 1:
            raise WorkflowError(
                "target_placement_missing", "transition target placement is not exact"
            )
        return matches[0]

    def _source_placement(self, transition: ViewTransition) -> FramePlacement:
        matches = [
            placement
            for placement in self.registry.list_placements(view_id=transition.view_id)
            if placement.frame_id == transition.source_frame_id
        ]
        if len(matches) != 1:
            raise WorkflowError(
                "source_placement_missing", "transition source placement is not exact"
            )
        return matches[0]

    def _present(
        self,
        transition: ViewTransition,
        capability: AgentCapability,
        *,
        now: int,
    ) -> tuple[object, TmuxExecutor, PaneObservation, str]:
        view, executor = self._tmux_for_view(transition.view_id)
        if view.state is not ViewState.READY:
            raise WorkflowError("view_not_ready", "transition view is not ready")
        source_surface = self.registry.get_surface(capability.surface_id)
        if source_surface.pane_id is None:
            raise WorkflowError("source_locator_missing", "source pane is missing")
        executor.set_pane_input(
            generation_id=self.generation_id,
            view_id=transition.view_id,
            pane_id=source_surface.pane_id,
            enabled=False,
        )
        target = self._target_placement(transition)
        if target.surface_id is None:
            raise WorkflowError("target_surface_missing", "target has no surface")
        surface = self.registry.get_surface(target.surface_id)
        if surface.lifecycle_state is SurfaceState.PLANNED:
            matches = self._surface_panes(executor, transition, target.surface_id)
            if len(matches) != 1 or not matches[0].input_off:
                raise WorkflowError(
                    "staged_surface_uncertain", "staged target is not exact and fenced"
                )
            target_pane = matches[0]
        else:
            if (
                surface.lifecycle_state is not SurfaceState.LIVE
                or surface.tmux_server_id != view.tmux_server_id
                or surface.pane_id is None
            ):
                raise WorkflowError(
                    "target_surface_unavailable", "target surface is unavailable"
                )
            target_pane = executor._pane(surface.pane_id)
            if not target_pane.input_off:
                raise WorkflowError(
                    "target_surface_unfenced", "parked target input is not fenced"
                )
        owner = f"trusted-stop-{uuid4()}"
        transition = self.registry.claim_transition_execution(
            transition.transition_id,
            owner,
            now + EXECUTION_LEASE_MS,
            now=now,
        )
        executor.present_pane(
            prefix=self.config.tmux.naming_prefix,
            generation_id=self.generation_id,
            view_id=transition.view_id,
            mode=view.mode,
            pane_id=target_pane.pane_id,
        )
        self.registry.advance_transport_phase(
            transition.transition_id,
            owner,
            TransportPhase.INTENT,
            TransportPhase.MOVED,
            now=now,
        )
        self.registry.advance_transport_phase(
            transition.transition_id,
            owner,
            TransportPhase.MOVED,
            TransportPhase.INSPECTED,
            now=now,
        )
        self.registry.commit_transition_presentation(
            transition.transition_id, owner, now=now
        )
        return view, executor, target_pane, owner

    def _provider_environment(
        self,
        *,
        raw_capability: str,
        transition: ViewTransition,
        session_key: SessionKey,
    ) -> dict[str, str]:
        placement = self._target_placement(transition)
        if placement.surface_id is None:
            raise WorkflowError("target_surface_missing", "target has no surface")
        surface = self.registry.get_surface(placement.surface_id)
        return {
            "AGENT_SWITCHBOARD_CAPABILITY": raw_capability,
            "AGENT_SWITCHBOARD_LAUNCH_ID": str(surface.launch_id),
            "AGENT_SWITCHBOARD_SURFACE_ID": str(surface.surface_id),
            "SWB_V3_TRANSITION_ID": str(transition.transition_id),
            "SWB_V3_SESSION_KEY": str(session_key),
            "SWB_V3_CONFIG_ROOT": str(self.paths.config_root),
            "SWB_V3_STATE_ROOT": str(self.paths.state_root),
            "SWB_V3_MCP_COMMAND": (f"{sys.executable} -m agent_switchboard agent-mcp"),
        }

    def _launch_child(
        self,
        transition: ViewTransition,
        executor: TmuxExecutor,
        pane: PaneObservation,
        *,
        owner: str,
        now: int,
    ) -> None:
        target = self._target_placement(transition)
        assert target.surface_id is not None
        surface = self.registry.get_surface(target.surface_id)
        launch = self.registry.get_launch(surface.launch_id)
        frame = self.registry.get_frame(transition.target_frame_id)
        session_key = frame.current_session_key
        if session_key is None:
            raise WorkflowError("child_session_missing", "child session is missing")
        contract = self._provider_contract(session_key.provider)
        raw = self.capability_factory()
        if not raw or "\x00" in raw:
            raise WorkflowError(
                "capability_generation_failed",
                "capability generator returned invalid data",
            )
        environment = self._provider_environment(
            raw_capability=raw, transition=transition, session_key=session_key
        )
        checkout = self.registry.get_work_context(frame.work_context_id)
        command: ProviderCommand = build_new_command(
            contract,
            cwd=self.registry.checkout_path(checkout.checkout_id),
            session_id=session_key.provider_session_id,
            prompt=CONTROL_PROMPT,
            injected_environment=environment,
            mcp_command=(
                sys.executable,
                "-m",
                "agent_switchboard",
                "agent-mcp",
            ),
        )
        control = self.registry.control_turn_for_transition(transition.transition_id)
        assert control is not None
        self.registry.advance_launch(
            launch.launch_id, LaunchState.PLANNED, LaunchState.AUTHORIZED, now=now
        )
        self.registry.advance_control_turn(
            control.control_turn_id,
            ControlState.PREPARED,
            ControlState.SUBMITTED,
            now=now,
        )
        self._watch_control(transition.transition_id)
        try:
            observed = executor.launch_surface(
                generation_id=self.generation_id,
                view_id=transition.view_id,
                frame_id=str(transition.target_frame_id),
                surface_id=str(surface.surface_id),
                pane_id=pane.pane_id,
                command=command.argv,
                cwd=command.cwd,
                environment=command.environment,
            )
        except Exception:
            with suppress(ConflictError):
                self.registry.advance_control_turn(
                    control.control_turn_id,
                    ControlState.SUBMITTED,
                    ControlState.UNCERTAIN,
                    now=now,
                )
            raise
        surface = self.registry.publish_surface(
            surface.surface_id,
            surface.metadata_generation,
            self.registry.get_view(transition.view_id).tmux_server_id,
            pane.pane_id,
            process_id=observed.process_id,
            now=now,
        )
        self.registry.advance_launch(
            launch.launch_id, LaunchState.AUTHORIZED, LaunchState.STARTED, now=now
        )
        persisted = self.registry.get_provider_session(session_key)
        self.registry.upsert_provider_session(
            ProviderSession(
                persisted.session_key,
                persisted.host_id,
                persisted.provider,
                persisted.provider_session_id,
                persisted.project_id,
                persisted.checkout_id,
                persisted.name,
                persisted.purpose,
                persisted.pinned,
                RuntimePresence.LIVE,
                Resumability.RESUMABLE,
                Activity.WORKING,
                ActivityReason.UNKNOWN,
                persisted.created_at,
                now,
                now,
                now,
            )
        )
        surface, _launch = self.registry.bind_surface_session(
            surface.surface_id,
            surface.metadata_generation,
            session_key,
            now=now,
        )
        active = self._target_placement(transition)
        self.registry.issue_capability(
            AgentCapability(
                CapabilityId(uuid4()),
                sha256(raw.encode("utf-8")).hexdigest(),
                self.host_id,
                transition.view_id,
                transition.target_frame_id,
                session_key,
                surface.surface_id,
                surface.launch_id,
                surface.tmux_server_id,
                surface.pane_id,
                active.generation,
                now,
                now + CAPABILITY_TTL_MS,
                None,
            )
        )
        self.registry.advance_transition_state(
            transition.transition_id,
            TransitionState.PRESENTED,
            TransitionState.AWAITING_CLAIM,
            execution_owner=owner,
            now=now,
        )

    def _submit_parent_control(
        self,
        transition: ViewTransition,
        executor: TmuxExecutor,
        pane: PaneObservation,
        *,
        owner: str,
        now: int,
    ) -> None:
        control = self.registry.control_turn_for_transition(transition.transition_id)
        self.registry.advance_transition_state(
            transition.transition_id,
            TransitionState.PRESENTED,
            TransitionState.AWAITING_CLAIM,
            execution_owner=owner,
            now=now,
        )
        if control is None:
            executor.set_pane_input(
                generation_id=self.generation_id,
                view_id=transition.view_id,
                pane_id=pane.pane_id,
                enabled=True,
            )
            return
        if control.transport is not ControlTransport.LIVE_INPUT:
            raise WorkflowError(
                "control_resume_required",
                "stopped parent requires exact provider resume",
            )
        target = self.registry.get_provider_session(control.target_session_key)
        if (
            target.runtime_presence is not RuntimePresence.LIVE
            or target.activity is not Activity.READY
            or target.activity_reason is not ActivityReason.TURN_COMPLETE
        ):
            raise WorkflowError(
                "control_target_unready", "parent is not at a verified idle boundary"
            )
        self.registry.advance_control_turn(
            control.control_turn_id,
            ControlState.PREPARED,
            ControlState.SUBMITTED,
            now=now,
        )
        self._watch_control(transition.transition_id)
        try:
            executor.submit_control_prompt(
                generation_id=self.generation_id,
                view_id=transition.view_id,
                pane_id=pane.pane_id,
                literal=CONTROL_PROMPT,
            )
        except Exception:
            self.registry.advance_control_turn(
                control.control_turn_id,
                ControlState.SUBMITTED,
                ControlState.UNCERTAIN,
                now=now,
            )
            self._open_recovery(
                kind="control_submit_uncertain",
                subject_type="transition",
                subject_id=str(transition.transition_id),
                explanation="Control submission is uncertain and must not be repeated.",
                now=now,
            )
            with suppress(Exception):
                executor.set_pane_input(
                    generation_id=self.generation_id,
                    view_id=transition.view_id,
                    pane_id=pane.pane_id,
                    enabled=True,
                )
            raise

    def _launch_resumed_target(
        self,
        transition: ViewTransition,
        executor: TmuxExecutor,
        pane: PaneObservation,
        *,
        owner: str,
        now: int,
    ) -> None:
        placement = self._target_placement(transition)
        assert placement.surface_id is not None
        surface = self.registry.get_surface(placement.surface_id)
        launch = self.registry.get_launch(surface.launch_id)
        if (
            launch.action is not LaunchAction.RESUME
            or launch.target_session_key is None
        ):
            raise WorkflowError("resume_identity", "target is not an exact resume")
        session_key = launch.target_session_key
        control = self.registry.control_turn_for_transition(transition.transition_id)
        prompt = None if control is None else CONTROL_PROMPT
        raw = self.capability_factory()
        if not raw or "\x00" in raw:
            raise WorkflowError(
                "capability_generation_failed",
                "capability generator returned invalid data",
            )
        contract = self._provider_contract(session_key.provider)
        frame = self.registry.get_frame(transition.target_frame_id)
        context = self.registry.get_work_context(frame.work_context_id)
        command = build_resume_command(
            contract,
            cwd=self.registry.checkout_path(context.checkout_id),
            session_id=session_key.provider_session_id,
            prompt=prompt,
            injected_environment=self._provider_environment(
                raw_capability=raw,
                transition=transition,
                session_key=session_key,
            ),
            mcp_command=(
                sys.executable,
                "-m",
                "agent_switchboard",
                "agent-mcp",
            ),
        )
        self.registry.advance_launch(
            launch.launch_id, LaunchState.PLANNED, LaunchState.AUTHORIZED, now=now
        )
        if control is not None:
            if control.transport is not ControlTransport.RESUME_INITIAL:
                raise WorkflowError(
                    "control_transport", "resumed target requires initial control"
                )
            self.registry.advance_control_turn(
                control.control_turn_id,
                ControlState.PREPARED,
                ControlState.SUBMITTED,
                now=now,
            )
            self._watch_control(transition.transition_id)
        try:
            observed = executor.launch_surface(
                generation_id=self.generation_id,
                view_id=transition.view_id,
                frame_id=str(transition.target_frame_id),
                surface_id=str(surface.surface_id),
                pane_id=pane.pane_id,
                command=command.argv,
                cwd=command.cwd,
                environment=command.environment,
            )
        except Exception:
            if control is not None:
                with suppress(ConflictError):
                    self.registry.advance_control_turn(
                        control.control_turn_id,
                        ControlState.SUBMITTED,
                        ControlState.UNCERTAIN,
                        now=now,
                    )
            raise
        view = self.registry.get_view(transition.view_id)
        assert view.tmux_server_id is not None
        surface = self.registry.publish_surface(
            surface.surface_id,
            surface.metadata_generation,
            view.tmux_server_id,
            pane.pane_id,
            process_id=observed.process_id,
            now=now,
        )
        self.registry.advance_launch(
            launch.launch_id, LaunchState.AUTHORIZED, LaunchState.STARTED, now=now
        )
        persisted = self.registry.get_provider_session(session_key)
        self.registry.upsert_provider_session(
            ProviderSession(
                persisted.session_key,
                persisted.host_id,
                persisted.provider,
                persisted.provider_session_id,
                persisted.project_id,
                persisted.checkout_id,
                persisted.name,
                persisted.purpose,
                persisted.pinned,
                RuntimePresence.LIVE,
                Resumability.RESUMABLE,
                Activity.WORKING if prompt is not None else Activity.READY,
                (
                    ActivityReason.UNKNOWN
                    if prompt is not None
                    else ActivityReason.TURN_COMPLETE
                ),
                persisted.created_at,
                now,
                now,
                now,
            )
        )
        surface, _launch = self.registry.bind_surface_session(
            surface.surface_id,
            surface.metadata_generation,
            session_key,
            now=now,
        )
        active = self._target_placement(transition)
        self.registry.issue_capability(
            AgentCapability(
                CapabilityId(uuid4()),
                sha256(raw.encode("utf-8")).hexdigest(),
                self.host_id,
                transition.view_id,
                transition.target_frame_id,
                session_key,
                surface.surface_id,
                surface.launch_id,
                surface.tmux_server_id,
                surface.pane_id,
                active.generation,
                now,
                now + CAPABILITY_TTL_MS,
                None,
            )
        )
        if control is not None:
            self.registry.advance_transition_state(
                transition.transition_id,
                TransitionState.PRESENTED,
                TransitionState.AWAITING_CLAIM,
                execution_owner=owner,
                now=now,
            )

    def _finish_model_free(
        self,
        transition: ViewTransition,
        executor: TmuxExecutor,
        pane: PaneObservation,
        *,
        owner: str,
        close: bool,
        now: int,
    ) -> None:
        executor.set_pane_input(
            generation_id=self.generation_id,
            view_id=transition.view_id,
            pane_id=pane.pane_id,
            enabled=True,
        )
        if close:
            source_placement = self._source_placement(transition)
            if source_placement.surface_id is not None:
                source_surface = self.registry.get_surface(source_placement.surface_id)
                if source_surface.pane_id is not None:
                    try:
                        executor.stop_surface(
                            generation_id=self.generation_id,
                            view_id=transition.view_id,
                            surface_id=str(source_surface.surface_id),
                            pane_id=source_surface.pane_id,
                        )
                        self.registry.advance_surface_state(
                            source_surface.surface_id,
                            source_surface.metadata_generation,
                            SurfaceState.DEAD,
                            now=now,
                        )
                        self.registry.advance_placement(
                            source_placement.placement_id,
                            source_placement.generation,
                            PlacementState.STOPPED_AFFINITY,
                            now=now,
                        )
                    except Exception:
                        self._open_recovery(
                            kind="human_close_cleanup",
                            subject_type="frame",
                            subject_id=str(transition.source_frame_id),
                            explanation=(
                                "The dismissed child runtime needs exact cleanup."
                            ),
                            now=now,
                            actionability=RecoveryActionability.MANUAL,
                        )
            frame = self.registry.get_frame(transition.source_frame_id)
            if frame.lifecycle_state is FrameLifecycleState.OPEN:
                self.registry.advance_frame_state(
                    frame.frame_id,
                    FrameLifecycleState.OPEN,
                    FrameLifecycleState.CLOSING,
                    now=now,
                )
            self.registry.advance_frame_state(
                frame.frame_id,
                FrameLifecycleState.CLOSING,
                FrameLifecycleState.CLOSED,
                close_reason=CloseReason.DISMISSED,
                now=now,
            )
        self.registry.advance_transition_state(
            transition.transition_id,
            TransitionState.PRESENTED,
            TransitionState.SETTLING,
            execution_owner=owner,
            now=now,
        )
        self.registry.advance_transition_state(
            transition.transition_id,
            TransitionState.SETTLING,
            TransitionState.COMPLETED,
            execution_owner=owner,
            now=now,
        )

    def trusted_stop(self, raw_capability: str, *, now: int) -> StopResult:
        """Settle or execute the one transition owned by this exact stopped pane."""

        self._require_mutation("trusted Stop")
        capability = self.registry.validate_capability(raw_capability, now=now)
        transition = self.registry.nonterminal_transition_for_view(capability.view_id)
        if transition is None:
            return StopResult("none", None, None)
        if (
            transition.state is TransitionState.SETTLING
            and transition.target_frame_id == capability.frame_id
        ):
            settled = self.registry.settle_transition_claim(
                transition.transition_id, now=now
            )
            return StopResult("settled", settled.transition_id, settled.state)
        if (
            transition.state is not TransitionState.PREPARED
            or transition.source_frame_id != capability.frame_id
        ):
            return StopResult("ignored", transition.transition_id, transition.state)
        try:
            _view, executor, pane, owner = self._present(
                transition, capability, now=now
            )
            presented = self.registry.get_transition(transition.transition_id)
            target = self._target_placement(presented)
            target_launch = None
            if target.surface_id is not None:
                target_surface = self.registry.get_surface(target.surface_id)
                target_launch = self.registry.get_launch(target_surface.launch_id)
            resumed = (
                target_launch is not None
                and target_launch.state is LaunchState.PLANNED
                and target_launch.action is LaunchAction.RESUME
            )
            if resumed:
                self._launch_resumed_target(
                    presented, executor, pane, owner=owner, now=now
                )
                if transition.kind is TransitionKind.COMPLETE_RETURN:
                    pass
                elif transition.kind is TransitionKind.BACK:
                    self._finish_model_free(
                        presented,
                        executor,
                        pane,
                        owner=owner,
                        close=False,
                        now=now,
                    )
                elif transition.kind is TransitionKind.HUMAN_CLOSE:
                    self._finish_model_free(
                        presented,
                        executor,
                        pane,
                        owner=owner,
                        close=True,
                        now=now,
                    )
                else:
                    raise WorkflowError(
                        "resume_transition_kind",
                        "resume target does not match transition kind",
                    )
            elif transition.kind is TransitionKind.PUSH:
                self._launch_child(presented, executor, pane, owner=owner, now=now)
            elif transition.kind is TransitionKind.COMPLETE_RETURN:
                self._submit_parent_control(
                    presented, executor, pane, owner=owner, now=now
                )
            elif transition.kind is TransitionKind.BACK:
                self._finish_model_free(
                    presented,
                    executor,
                    pane,
                    owner=owner,
                    close=False,
                    now=now,
                )
            elif transition.kind is TransitionKind.HUMAN_CLOSE:
                self._finish_model_free(
                    presented,
                    executor,
                    pane,
                    owner=owner,
                    close=True,
                    now=now,
                )
            else:
                raise WorkflowError(
                    "transition_kind", "trusted Stop does not own this transition"
                )
        except Exception as error:
            current = self.registry.get_transition(transition.transition_id)
            if current.state in {
                TransitionState.EXECUTING,
                TransitionState.PRESENTED,
                TransitionState.AWAITING_CLAIM,
            }:
                with suppress(ConflictError):
                    self.registry.advance_transition_state(
                        current.transition_id,
                        current.state,
                        TransitionState.FAILED,
                        execution_owner=current.execution_owner,
                        failure=self._failure("trusted_stop_failed"),
                        now=now,
                    )
            self._open_recovery(
                kind="trusted_stop",
                subject_type="transition",
                subject_id=str(transition.transition_id),
                explanation="Trusted post-turn execution needs exact recovery.",
                now=now,
            )
            raise WorkflowError("trusted_stop_failed", str(error)) from error
        final = self.registry.get_transition(transition.transition_id)
        return StopResult("executed", final.transition_id, final.state)

    @staticmethod
    def _failure(code: str):
        from .domain import FailureRecord

        return FailureRecord(code, "Trusted transition execution needs recovery.")

    def observe_prompt(
        self,
        raw_capability: str,
        *,
        prompt_id: str,
        now: int,
    ) -> ControlTurn | None:
        """Observe one exact UserPromptSubmit and release target user input."""

        self._require_mutation("trusted UserPromptSubmit")
        capability = self.registry.validate_capability(raw_capability, now=now)
        transition = self.registry.nonterminal_transition_for_view(capability.view_id)
        if transition is None or transition.target_frame_id != capability.frame_id:
            return None
        control = self.registry.control_turn_for_transition(transition.transition_id)
        if control is None:
            return None
        if control.state is ControlState.SUBMITTED:
            control = self.registry.advance_control_turn(
                control.control_turn_id,
                ControlState.SUBMITTED,
                ControlState.OBSERVED,
                observed_prompt_id=prompt_id,
                now=now,
            )
        elif control.state is ControlState.UNCERTAIN:
            control = self.registry.advance_control_turn(
                control.control_turn_id,
                ControlState.UNCERTAIN,
                ControlState.OBSERVED,
                observed_prompt_id=prompt_id,
                now=now,
            )
        surface = self.registry.get_surface(capability.surface_id)
        if surface.pane_id is not None:
            _view, executor = self._tmux_for_view(capability.view_id)
            executor.set_pane_input(
                generation_id=self.generation_id,
                view_id=capability.view_id,
                pane_id=surface.pane_id,
                enabled=True,
            )
        return control

    def control_watchdog(self, transition_id: TransitionId, *, now: int) -> ControlTurn:
        """Fence a timed-out submission as uncertain without ever retrying it."""

        self._require_mutation("control watchdog")
        transition = self.registry.get_transition(transition_id)
        control = self.registry.control_turn_for_transition(transition_id)
        if control is None:
            raise WorkflowError("control_missing", "transition has no control turn")
        if control.state is not ControlState.SUBMITTED:
            return control
        if control.submitted_at is None:
            raise WorkflowError(
                "control_submission_invalid", "submitted control has no timestamp"
            )
        deadline = (
            control.submitted_at
            + self.config.control_turns.watchdog_timeout_seconds * 1_000
        )
        if now < deadline:
            return control
        control = self.registry.advance_control_turn(
            control.control_turn_id,
            ControlState.SUBMITTED,
            ControlState.UNCERTAIN,
            now=now,
        )
        placement = self._target_placement(transition)
        if placement.surface_id is not None:
            surface = self.registry.get_surface(placement.surface_id)
            if surface.pane_id is not None:
                _view, executor = self._tmux_for_view(transition.view_id)
                with suppress(Exception):
                    executor.set_pane_input(
                        generation_id=self.generation_id,
                        view_id=transition.view_id,
                        pane_id=surface.pane_id,
                        enabled=True,
                    )
        self._open_recovery(
            kind="control_submit_uncertain",
            subject_type="transition",
            subject_id=str(transition_id),
            explanation="Control submission timed out and will not be repeated.",
            now=now,
        )
        return control

    def reconcile_control_turns(self, *, now: int) -> tuple[ControlTurn, ...]:
        """Fence every overdue submitted control without resubmission."""

        rows = self.registry.connection.execute(
            "SELECT transition_id FROM control_turns WHERE state = 'submitted' "
            "AND submitted_at IS NOT NULL AND submitted_at <= ? "
            "ORDER BY transition_id",
            (now - self.config.control_turns.watchdog_timeout_seconds * 1_000,),
        ).fetchall()
        return tuple(
            self.control_watchdog(TransitionId(row["transition_id"]), now=now)
            for row in rows
        )

    def claim(self, raw_capability: str, *, now: int) -> TransitionClaim:
        """Release one semantic payload to the exact active target capability."""

        self._require_mutation("transition claim")
        capability = self.registry.validate_capability(raw_capability, now=now)
        if capability.session_key is None:
            raise WorkflowError("claim_session_missing", "capability has no session")
        transition = self.registry.nonterminal_transition_for_view(capability.view_id)
        if transition is None or transition.target_frame_id != capability.frame_id:
            raise WorkflowError(
                "claim_transition_missing", "no transition awaits this target"
            )
        claim = self.registry.transition_claim(
            transition.transition_id,
            capability.session_key,
            raw_capability,
            now=now,
        )
        if claim.kind == "handoff":
            self._stop_completed_child(transition, now=now)
        return claim

    def _stop_completed_child(self, transition: ViewTransition, *, now: int) -> None:
        source = self._source_placement(transition)
        if source.surface_id is None:
            return
        surface = self.registry.get_surface(source.surface_id)
        if surface.lifecycle_state is not SurfaceState.LIVE or surface.pane_id is None:
            return
        _view, executor = self._tmux_for_view(transition.view_id)
        try:
            executor.stop_surface(
                generation_id=self.generation_id,
                view_id=transition.view_id,
                surface_id=str(surface.surface_id),
                pane_id=surface.pane_id,
            )
            self.registry.advance_surface_state(
                surface.surface_id,
                surface.metadata_generation,
                SurfaceState.DEAD,
                now=now,
            )
            self.registry.advance_placement(
                source.placement_id,
                source.generation,
                PlacementState.STOPPED_AFFINITY,
                now=now,
            )
        except Exception:
            self._open_recovery(
                kind="completed_child_cleanup",
                subject_type="frame",
                subject_id=str(transition.source_frame_id),
                explanation=(
                    "Delivered child handoff is closed but runtime cleanup is "
                    "uncertain."
                ),
                now=now,
                actionability=RecoveryActionability.MANUAL,
            )

    def resume_exact(
        self,
        frame_id: FrameId,
        *,
        raw_capability: str,
        transition: ViewTransition,
        prompt: str | None,
    ) -> ProviderCommand:
        """Build the guarded exact-UUID recovery/return resume command."""

        frame = self.registry.get_frame(frame_id)
        if frame.current_session_key is None:
            raise WorkflowError("resume_session_missing", "frame has no session")
        session = frame.current_session_key
        contract = self._provider_contract(session.provider)
        context = self.registry.get_work_context(frame.work_context_id)
        return build_resume_command(
            contract,
            cwd=self.registry.checkout_path(context.checkout_id),
            session_id=session.provider_session_id,
            prompt=prompt,
            injected_environment=self._provider_environment(
                raw_capability=raw_capability,
                transition=transition,
                session_key=session,
            ),
            mcp_command=(
                sys.executable,
                "-m",
                "agent_switchboard",
                "agent-mcp",
            ),
        )

    def reopen_imported_session(
        self,
        frame_id: FrameId,
        session_key: SessionKey,
        *,
        request_id: RequestId,
        now: int,
    ) -> ProviderSession:
        """Resume one exact imported UUID into an otherwise empty workspace."""

        self._require_mutation("frame reopen")
        frame = self.registry.get_frame(FrameId(frame_id))
        session = self.registry.get_provider_session(SessionKey.parse(str(session_key)))
        context = self.registry.get_work_context(frame.work_context_id)
        if (
            frame.role is not FrameRole.WORKSPACE
            or frame.current_session_key is not None
            or session.project_id != frame.project_id
            or session.checkout_id != context.checkout_id
            or session.runtime_presence is not RuntimePresence.STOPPED
            or session.resumability is not Resumability.RESUMABLE
        ):
            raise WorkflowError(
                "reopen_precondition",
                "frame is not empty or imported session identity is incompatible",
            )
        placements = [
            item
            for item in self.registry.list_placements()
            if item.frame_id == frame.frame_id
            and item.state is PlacementState.ACTIVE
            and item.surface_id is None
        ]
        if len(placements) != 1:
            raise WorkflowError(
                "reopen_placement", "frame has no single empty active placement"
            )
        placement = placements[0]
        view = self.registry.get_view(placement.view_id)
        if view.active_frame_id != frame.frame_id:
            raise WorkflowError("reopen_view", "frame is not the view foreground")
        self.registry.append_frame_session(
            FrameSession(
                _stable_id(FrameSessionId, frame.frame_id, session.session_key),
                frame.frame_id,
                session.session_key,
                1,
                MembershipReason.CUTOVER,
                now,
            )
        )
        self.registry.advance_placement(
            placement.placement_id,
            placement.generation,
            PlacementState.STOPPED_AFFINITY,
            now=now,
        )
        self._stage_parent_resume(
            frame.frame_id, view.view_id, RequestId(request_id), now=now
        )
        placement = next(
            item
            for item in self.registry.list_placements(view_id=view.view_id)
            if item.frame_id == frame.frame_id
        )
        if placement.surface_id is None or placement.state is not PlacementState.STAGED:
            raise WorkflowError("reopen_surface", "exact resume surface was not staged")
        surface = self.registry.get_surface(placement.surface_id)
        launch = self.registry.get_launch(surface.launch_id)
        _view, executor = self._tmux_for_view(view.view_id)
        panes = [
            item
            for item in executor.panes()
            if item.surface_id == str(surface.surface_id)
            and item.view_id == str(view.view_id)
            and item.generation_id == str(self.generation_id)
        ]
        if len(panes) != 1:
            raise WorkflowError("reopen_pane", "exact resume pane is ambiguous")
        pane = panes[0]
        raw = self.capability_factory()
        if not raw or "\x00" in raw:
            raise WorkflowError(
                "capability_generation_failed",
                "capability generator returned invalid data",
            )
        contract = self._provider_contract(session.provider)
        command = build_resume_command(
            contract,
            cwd=self.registry.checkout_path(context.checkout_id),
            session_id=session.provider_session_id,
            prompt=None,
            injected_environment={
                "AGENT_SWITCHBOARD_CAPABILITY": raw,
                "AGENT_SWITCHBOARD_LAUNCH_ID": str(surface.launch_id),
                "AGENT_SWITCHBOARD_SURFACE_ID": str(surface.surface_id),
                "SWB_V3_SESSION_KEY": str(session.session_key),
                "SWB_V3_CONFIG_ROOT": str(self.paths.config_root),
                "SWB_V3_STATE_ROOT": str(self.paths.state_root),
                "SWB_V3_MCP_COMMAND": (
                    f"{sys.executable} -m agent_switchboard agent-mcp"
                ),
            },
            mcp_command=(sys.executable, "-m", "agent_switchboard", "agent-mcp"),
        )
        self.registry.advance_launch(
            launch.launch_id, LaunchState.PLANNED, LaunchState.AUTHORIZED, now=now
        )
        observed = executor.launch_surface(
            generation_id=self.generation_id,
            view_id=view.view_id,
            frame_id=str(frame.frame_id),
            surface_id=str(surface.surface_id),
            pane_id=pane.pane_id,
            command=command.argv,
            cwd=command.cwd,
            environment=command.environment,
        )
        assert view.tmux_server_id is not None
        surface = self.registry.publish_surface(
            surface.surface_id,
            surface.metadata_generation,
            view.tmux_server_id,
            pane.pane_id,
            process_id=observed.process_id,
            now=now,
        )
        self.registry.advance_launch(
            launch.launch_id, LaunchState.AUTHORIZED, LaunchState.STARTED, now=now
        )
        self.registry.bind_surface_session(
            surface.surface_id,
            surface.metadata_generation,
            session.session_key,
            now=now,
        )
        placement = self.registry.advance_placement(
            placement.placement_id,
            placement.generation,
            PlacementState.ACTIVE,
            now=now,
        )
        session = self.registry.upsert_provider_session(
            ProviderSession(
                session.session_key,
                session.host_id,
                session.provider,
                session.provider_session_id,
                session.project_id,
                session.checkout_id,
                session.name,
                session.purpose,
                session.pinned,
                RuntimePresence.LIVE,
                session.resumability,
                Activity.READY,
                ActivityReason.TURN_COMPLETE,
                session.created_at,
                now,
                now,
                now,
            )
        )
        self.registry.issue_capability(
            AgentCapability(
                CapabilityId(uuid4()),
                sha256(raw.encode()).hexdigest(),
                self.host_id,
                view.view_id,
                frame.frame_id,
                session.session_key,
                surface.surface_id,
                surface.launch_id,
                surface.tmux_server_id,
                surface.pane_id,
                placement.generation,
                now,
                now + CAPABILITY_TTL_MS,
                None,
            )
        )
        executor.set_pane_input(
            generation_id=self.generation_id,
            view_id=view.view_id,
            pane_id=pane.pane_id,
            enabled=True,
        )
        return session

    def stop_session(self, session_key: SessionKey, *, now: int) -> ProviderSession:
        """Stop one exact verified-idle owned session without guessing."""

        self._require_mutation("session stop")
        session = self.registry.get_provider_session(SessionKey.parse(str(session_key)))
        if (
            session.runtime_presence is not RuntimePresence.LIVE
            or session.resumability is not Resumability.RESUMABLE
            or session.activity is not Activity.READY
            or session.activity_reason is not ActivityReason.TURN_COMPLETE
        ):
            raise WorkflowError(
                "session_stop_unready", "session is not at a verified idle boundary"
            )
        surfaces = [
            item
            for item in self.registry.list_surfaces(live_only=True)
            if item.session_key == session.session_key and item.pane_id is not None
        ]
        if len(surfaces) != 1:
            raise WorkflowError(
                "session_stop_ambiguous", "session has no single owned live surface"
            )
        surface = surfaces[0]
        placements = [
            item
            for item in self.registry.list_placements()
            if item.surface_id == surface.surface_id
            and item.state in {PlacementState.ACTIVE, PlacementState.PARKED}
        ]
        if len(placements) != 1:
            raise WorkflowError(
                "session_stop_ambiguous", "session has no single owned placement"
            )
        placement = placements[0]
        if self.registry.nonterminal_transition_for_view(placement.view_id) is not None:
            raise WorkflowError(
                "session_stop_transition", "session view has an active transition"
            )
        _view, executor = self._tmux_for_view(placement.view_id)
        assert surface.pane_id is not None
        executor.stop_surface(
            generation_id=self.generation_id,
            view_id=placement.view_id,
            surface_id=str(surface.surface_id),
            pane_id=surface.pane_id,
        )
        self.registry.advance_surface_state(
            surface.surface_id,
            surface.metadata_generation,
            SurfaceState.DEAD,
            now=now,
        )
        self.registry.advance_placement(
            placement.placement_id,
            placement.generation,
            PlacementState.STOPPED_AFFINITY,
            now=now,
        )
        return self.registry.upsert_provider_session(
            ProviderSession(
                session.session_key,
                session.host_id,
                session.provider,
                session.provider_session_id,
                session.project_id,
                session.checkout_id,
                session.name,
                session.purpose,
                session.pinned,
                RuntimePresence.STOPPED,
                session.resumability,
                session.activity,
                session.activity_reason,
                session.created_at,
                session.provider_updated_at,
                now,
                now,
            )
        )


__all__ = [
    "CAPABILITY_TTL_MS",
    "NativeSessionAllocator",
    "PreparedTransition",
    "SessionAllocator",
    "StopResult",
    "WorkflowError",
    "WorkflowRuntime",
    "spawn_control_watchdog",
]
