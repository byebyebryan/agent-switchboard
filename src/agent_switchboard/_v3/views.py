"""Durable Phase 6 view lifecycle over the registry and tmux executor."""

from __future__ import annotations

import sys
from contextlib import suppress
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid4, uuid5

from .domain import (
    DesktopAttachmentLease,
    FailureRecord,
    FrameId,
    FramePlacement,
    LeaseId,
    LeaseState,
    PlacementId,
    PlacementState,
    ProjectId,
    Recovery,
    RecoveryActionability,
    RecoveryId,
    RecoveryState,
    RequestId,
    TmuxServerId,
    TransitionId,
    TransitionKind,
    TransitionState,
    TransportPhase,
    UserView,
    ViewId,
    ViewMode,
    ViewState,
    ViewTransition,
    WorkContextId,
    request_fingerprint,
)
from .generation import GenerationPaths, OpenGeneration
from .protocol import DirectiveKind, PresentationDirective
from .storage import ConflictError
from .tmux_view import ShellObservation, TmuxExecutor, TmuxViewError


class ViewRuntimeError(RuntimeError):
    """A semantic view operation failed with one stable bounded code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ViewOpenResult:
    view: UserView
    created: bool
    attach_argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ViewRecoveryResult:
    view: UserView
    repaired: bool
    observation: ShellObservation


@dataclass(frozen=True, slots=True)
class ViewAttachResult:
    view: UserView
    attach_argv: tuple[str, ...]


def _stable_id(value_type, *parts: object):
    raw = ":".join(str(part) for part in parts)
    return value_type(uuid5(NAMESPACE_URL, f"agent-switchboard:v3:{raw}"))


class ViewRuntime:
    """Coordinate host-local view state and exact tmux side effects."""

    def __init__(
        self,
        opened: OpenGeneration,
        paths: GenerationPaths,
        *,
        tmux: TmuxExecutor | None = None,
    ) -> None:
        self.opened = opened
        self.paths = paths
        self.registry = opened.registry
        self.config = opened.config
        self.generation_id = opened.generation_id
        self.host_id = opened.config.host.host_id
        self.tmux = tmux or TmuxExecutor()

    def _require_mutation(self, operation: str) -> None:
        self.opened.require_mutation(operation)

    def _sidebar_command(self, view_id: ViewId) -> tuple[str, ...]:
        return (
            sys.executable,
            "-m",
            "agent_switchboard._v3.navigator",
            "--config-root",
            str(self.paths.config_root),
            "--state-root",
            str(self.paths.state_root),
            "--view",
            str(view_id),
        )

    def _tmux_for_view(self, view: UserView) -> TmuxExecutor:
        if view.tmux_server_id is None:
            raise ViewRuntimeError("view_tmux_missing", "view has no tmux evidence")
        server = self.registry.get_tmux_server(view.tmux_server_id)
        return TmuxExecutor(server.socket_path, executable=self.tmux.executable)

    def _record_server(self, *, now: int) -> TmuxServerId:
        server = self.tmux.server_evidence(self.host_id, observed_at=now)
        self.registry.record_tmux_server(server)
        return server.tmux_server_id

    def _project(self, project_id: ProjectId):
        matches = [
            project
            for project in self.config.projects
            if project.project_id == project_id
        ]
        if len(matches) != 1:
            raise ViewRuntimeError("project_not_found", "project is not configured")
        return matches[0]

    def _default_checkout(self, project_id: ProjectId):
        memberships = [
            value
            for value in self.config.project_repositories
            if value.project_id == project_id
        ]
        primary = next((value for value in memberships if value.is_primary), None)
        repository_ids = [
            value.repository_id
            for value in ([primary] if primary is not None else memberships)
        ]
        candidates = [
            checkout
            for checkout in self.config.checkouts
            if checkout.repository_id in repository_ids
        ]
        candidates.sort(
            key=lambda value: (not value.is_default, str(value.checkout_id))
        )
        if not candidates:
            raise ViewRuntimeError(
                "project_checkout_missing", "project has no configured checkout"
            )
        return candidates[0]

    def _workspace(self, project_id: ProjectId, *, now: int):
        project = self._project(project_id)
        checkout = self._default_checkout(project_id)
        context_id = _stable_id(
            WorkContextId, self.host_id, project_id, checkout.checkout_id, "workspace"
        )
        frame_id = _stable_id(FrameId, self.host_id, project_id, "workspace")
        return self.registry.ensure_workspace(
            context_id,
            frame_id,
            self.host_id,
            project_id,
            checkout.checkout_id,
            project.name,
            preferred_provider=project.default_provider,
            now=now,
        )

    def _placement_for_frame(self, frame_id: FrameId) -> FramePlacement | None:
        return next(
            (
                placement
                for placement in self.registry.list_placements()
                if placement.frame_id == frame_id
                and placement.state is not PlacementState.ORPHANED
            ),
            None,
        )

    def _open_recovery(
        self,
        *,
        kind: str,
        subject_type: str,
        subject_id: str,
        actionability: RecoveryActionability,
        explanation: str,
        now: int,
    ) -> Recovery:
        recovery_id = _stable_id(
            RecoveryId, self.host_id, kind, subject_type, subject_id
        )
        return self.registry.open_recovery(
            Recovery(
                recovery_id,
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
        )

    def _existing_view_result(self, view: UserView, *, now: int) -> ViewOpenResult:
        executor = self._tmux_for_view(view)
        try:
            executor.inspect_shell(
                self.config.tmux.naming_prefix,
                self.generation_id,
                view.view_id,
                view.mode,
            )
        except TmuxViewError:
            view = self.recover_view(view.view_id, now=now).view
            executor = self._tmux_for_view(view)
        else:
            if view.state is not ViewState.READY:
                view = self.recover_view(view.view_id, now=now).view
                executor = self._tmux_for_view(view)
        return ViewOpenResult(
            view,
            False,
            executor.attach_argv(self.config.tmux.naming_prefix, view.view_id),
        )

    def create_project_view(
        self,
        project_id: ProjectId,
        *,
        request_id: RequestId,
        mode: ViewMode | None = None,
        view_id: ViewId | None = None,
        now: int,
    ) -> ViewOpenResult:
        """Single-flight a project's workspace into one durable view."""

        self._require_mutation("view open")
        workspace = self._workspace(ProjectId(project_id), now=now)
        existing = self._placement_for_frame(workspace.frame.frame_id)
        if existing is not None:
            view = self.registry.get_view(existing.view_id)
            return self._existing_view_result(view, now=now)

        selected_mode = mode or self.config.views.cli_default_mode
        view_id = view_id or _stable_id(ViewId, self.host_id, request_id, "view")
        placement_id = _stable_id(PlacementId, view_id, workspace.frame.frame_id)
        server_id = self._record_server(now=now)
        desktop_token = str(
            uuid5(NAMESPACE_URL, f"agent-switchboard:desktop:{self.host_id}:{view_id}")
        )
        view = UserView(
            view_id,
            self.host_id,
            selected_mode,
            workspace.frame.frame_id,
            ViewState.DEGRADED,
            0,
            desktop_token,
            server_id,
            now,
            None,
            now,
        )
        placement = FramePlacement(
            placement_id,
            self.host_id,
            view_id,
            workspace.frame.frame_id,
            None,
            PlacementState.ACTIVE,
            0,
            now,
            now,
        )
        try:
            self.registry.create_view(view, placement)
        except ConflictError:
            existing = self._placement_for_frame(workspace.frame.frame_id)
            if existing is None:
                raise
            current = self.registry.get_view(existing.view_id)
            return self._existing_view_result(current, now=now)

        executor = self._tmux_for_view(view)
        try:
            executor.create_shell(
                prefix=self.config.tmux.naming_prefix,
                generation_id=self.generation_id,
                view_id=view_id,
                frame_id=str(workspace.frame.frame_id),
                mode=selected_mode,
                sidebar_command=self._sidebar_command(view_id),
            )
            view = self.registry.rebind_view_tmux_server(
                view_id, 0, server_id, ViewState.READY, now=now
            )
        except (TmuxViewError, ConflictError) as error:
            self._open_recovery(
                kind="view_shell_create",
                subject_type="view",
                subject_id=str(view_id),
                actionability=RecoveryActionability.SAFE_AUTO,
                explanation=(
                    "The view shell was not created completely and can be repaired."
                ),
                now=now,
            )
            raise ViewRuntimeError("view_shell_create", str(error)) from error
        return ViewOpenResult(
            view,
            True,
            executor.attach_argv(self.config.tmux.naming_prefix, view_id),
        )

    def _target_frame(
        self, view: UserView, project_id: ProjectId, *, now: int
    ) -> FrameId:
        active = (
            self.registry.get_frame(view.active_frame_id)
            if view.active_frame_id
            else None
        )
        if active is not None and active.project_id == project_id:
            return active.frame_id
        row = self.registry.connection.execute(
            "SELECT frame.frame_id FROM frames AS frame "
            "JOIN frame_placements AS placement ON placement.frame_id = frame.frame_id "
            "WHERE placement.view_id = ? AND frame.project_id = ? "
            "AND frame.lifecycle_state = 'open' "
            "AND placement.state != 'orphaned' "
            "ORDER BY placement.last_focused_at DESC, "
            "CASE frame.role WHEN 'task' THEN 0 ELSE 1 END, frame.frame_id LIMIT 1",
            (str(view.view_id), str(project_id)),
        ).fetchone()
        if row is not None:
            return FrameId(row["frame_id"])
        workspace = self._workspace(project_id, now=now)
        placement = FramePlacement(
            _stable_id(PlacementId, view.view_id, workspace.frame.frame_id),
            self.host_id,
            view.view_id,
            workspace.frame.frame_id,
            None,
            PlacementState.STAGED,
            0,
            None,
            now,
        )
        self.registry.add_placement(placement)
        executor = self._tmux_for_view(view)
        executor.spawn_placeholder(
            prefix=self.config.tmux.naming_prefix,
            generation_id=self.generation_id,
            view_id=view.view_id,
            frame_id=str(workspace.frame.frame_id),
        )
        return workspace.frame.frame_id

    def open_project(
        self,
        project_id: ProjectId,
        *,
        request_id: RequestId,
        view_id: ViewId | None = None,
        mode: ViewMode | None = None,
        now: int,
    ) -> ViewOpenResult:
        self._require_mutation("project open")
        workspace = self._workspace(ProjectId(project_id), now=now)
        owner = self._placement_for_frame(workspace.frame.frame_id)
        if owner is not None and (view_id is None or owner.view_id != ViewId(view_id)):
            view = self.registry.get_view(owner.view_id)
            target = self._target_frame(view, ProjectId(project_id), now=now)
            if target != view.active_frame_id:
                view = self.focus_frame(
                    view.view_id, target, request_id=request_id, now=now
                )
            executor = self._tmux_for_view(view)
            return ViewOpenResult(
                view,
                False,
                executor.attach_argv(self.config.tmux.naming_prefix, view.view_id),
            )
        if view_id is None:
            return self.create_project_view(
                ProjectId(project_id),
                request_id=request_id,
                mode=mode,
                now=now,
            )
        view = self.registry.get_view(ViewId(view_id))
        target = self._target_frame(view, ProjectId(project_id), now=now)
        if target != view.active_frame_id:
            view = self.focus_frame(
                view.view_id, target, request_id=request_id, now=now
            )
        executor = self._tmux_for_view(view)
        return ViewOpenResult(
            view,
            False,
            executor.attach_argv(self.config.tmux.naming_prefix, view.view_id),
        )

    def _pane_for_placement(
        self, executor: TmuxExecutor, view: UserView, placement: FramePlacement
    ) -> str:
        if placement.surface_id is not None:
            surface = self.registry.get_surface(placement.surface_id)
            if surface.pane_id is None or surface.tmux_server_id != view.tmux_server_id:
                raise ViewRuntimeError(
                    "surface_locator_stale", "target surface tmux evidence is stale"
                )
            return surface.pane_id
        matches = [
            pane
            for pane in executor.panes()
            if pane.view_id == str(view.view_id)
            and pane.generation_id == str(self.generation_id)
            and pane.frame_id == str(placement.frame_id)
            and pane.surface_id is None
        ]
        if len(matches) != 1:
            raise ViewRuntimeError(
                "placeholder_locator_stale", "target placeholder is not exact"
            )
        return matches[0].pane_id

    def _claim_transition(
        self, transition: ViewTransition, owner: str, *, now: int
    ) -> ViewTransition:
        if transition.state is TransitionState.PREPARED:
            return self.registry.claim_transition_execution(
                transition.transition_id, owner, now + 30_000, now=now
            )
        if transition.state is TransitionState.EXECUTING:
            if (
                transition.lease_expires_at is not None
                and transition.lease_expires_at <= now
            ):
                return self.registry.reclaim_transition_execution(
                    transition.transition_id, owner, now + 30_000, now=now
                )
            raise ViewRuntimeError(
                "transition_busy", "view transition execution lease is active"
            )
        raise ViewRuntimeError(
            "transition_unavailable",
            f"view transition is {transition.state.value}",
        )

    def focus_frame(
        self,
        view_id: ViewId,
        frame_id: FrameId,
        *,
        request_id: RequestId,
        now: int,
    ) -> UserView:
        self._require_mutation("view focus")
        view = self.registry.get_view(ViewId(view_id))
        target = next(
            (
                placement
                for placement in self.registry.list_placements(view_id=view.view_id)
                if placement.frame_id == FrameId(frame_id)
            ),
            None,
        )
        if target is None:
            raise ViewRuntimeError("placement_not_found", "frame is not in this view")
        source = next(
            (
                placement
                for placement in self.registry.list_placements(view_id=view.view_id)
                if placement.state is PlacementState.ACTIVE
            ),
            None,
        )
        if source is None:
            raise ViewRuntimeError(
                "active_placement_missing", "view has no active frame"
            )
        fingerprint = request_fingerprint(
            "view.focus",
            {"viewId": str(view.view_id), "frameId": str(target.frame_id)},
        )
        transition_id = _stable_id(TransitionId, self.host_id, request_id, "focus")
        transition = ViewTransition(
            transition_id,
            RequestId(request_id),
            fingerprint,
            self.host_id,
            view.view_id,
            TransitionKind.FOCUS,
            source.frame_id,
            target.frame_id,
            None,
            view.revision,
            None,
            TransitionState.PREPARED,
            None,
            None,
            TransportPhase.INTENT,
            None,
            now,
            now,
        )
        transition = self.registry.prepare_transition(transition)
        if transition.state is TransitionState.COMPLETED:
            return self.registry.get_view(view.view_id)
        owner = f"view-executor-{uuid4()}"
        transition = self._claim_transition(transition, owner, now=now)
        executor = self._tmux_for_view(view)
        try:
            pane_id = self._pane_for_placement(executor, view, target)
            executor.present_pane(
                prefix=self.config.tmux.naming_prefix,
                generation_id=self.generation_id,
                view_id=view.view_id,
                mode=view.mode,
                pane_id=pane_id,
            )
            phase = transition.transport_phase
            if phase is TransportPhase.INTENT and source.frame_id != target.frame_id:
                self.registry.advance_transport_phase(
                    transition.transition_id,
                    owner,
                    TransportPhase.INTENT,
                    TransportPhase.MOVED,
                    now=now,
                )
                phase = TransportPhase.MOVED
            if phase in {TransportPhase.INTENT, TransportPhase.MOVED}:
                self.registry.advance_transport_phase(
                    transition.transition_id,
                    owner,
                    phase,
                    TransportPhase.INSPECTED,
                    now=now,
                )
            self.registry.commit_transition_presentation(
                transition.transition_id, owner, now=now
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
        except (TmuxViewError, ConflictError, ViewRuntimeError) as error:
            with suppress(ConflictError):
                self.registry.advance_transition_state(
                    transition.transition_id,
                    TransitionState.EXECUTING,
                    TransitionState.FAILED,
                    execution_owner=owner,
                    failure=FailureRecord(
                        "view_presentation_uncertain", "View presentation needs repair."
                    ),
                    now=now,
                )
            self._open_recovery(
                kind="view_presentation",
                subject_type="view",
                subject_id=str(view.view_id),
                actionability=RecoveryActionability.SAFE_AUTO,
                explanation=(
                    "The intended frame presentation must be reconciled from pane "
                    "metadata."
                ),
                now=now,
            )
            raise ViewRuntimeError("view_presentation_uncertain", str(error)) from error
        return self.registry.get_view(view.view_id)

    def set_mode(
        self,
        view_id: ViewId,
        target_mode: ViewMode,
        *,
        request_id: RequestId,
        now: int,
    ) -> UserView:
        self._require_mutation("view mode")
        view = self.registry.get_view(ViewId(view_id))
        fingerprint = request_fingerprint(
            "view.mode",
            {"viewId": str(view.view_id), "mode": target_mode.value},
        )
        transition = self.registry.find_transition_by_request(
            self.host_id, RequestId(request_id)
        )
        if transition is not None:
            if (
                transition.request_fingerprint != fingerprint
                or transition.kind is not TransitionKind.MODE
                or transition.view_id != view.view_id
            ):
                raise ViewRuntimeError(
                    "request_conflict", "request identifies another operation"
                )
            if transition.state is TransitionState.COMPLETED:
                return view
        else:
            if view.mode is target_mode:
                return view
            transition = ViewTransition(
                _stable_id(TransitionId, self.host_id, request_id, "mode"),
                RequestId(request_id),
                fingerprint,
                self.host_id,
                view.view_id,
                TransitionKind.MODE,
                view.active_frame_id,
                view.active_frame_id,
                None,
                view.revision + 1,
                None,
                TransitionState.PREPARED,
                None,
                None,
                TransportPhase.INTENT,
                None,
                now,
                now,
            )
            transition = self.registry.prepare_transition(
                transition, desired_mode=target_mode
            )
            view = self.registry.get_view(view.view_id)
        owner = f"view-executor-{uuid4()}"
        transition = self._claim_transition(transition, owner, now=now)
        executor = self._tmux_for_view(view)
        previous_mode = (
            ViewMode.NAVIGATOR if target_mode is ViewMode.DIRECT else ViewMode.DIRECT
        )
        try:
            try:
                executor.inspect_shell(
                    self.config.tmux.naming_prefix,
                    self.generation_id,
                    view.view_id,
                    target_mode,
                )
            except TmuxViewError:
                executor.set_mode(
                    prefix=self.config.tmux.naming_prefix,
                    generation_id=self.generation_id,
                    view_id=view.view_id,
                    current_mode=previous_mode,
                    target_mode=target_mode,
                    sidebar_command=self._sidebar_command(view.view_id),
                )
            if transition.transport_phase is TransportPhase.INTENT:
                self.registry.advance_transport_phase(
                    transition.transition_id,
                    owner,
                    TransportPhase.INTENT,
                    TransportPhase.INSPECTED,
                    now=now,
                )
            self.registry.commit_transition_presentation(
                transition.transition_id, owner, now=now
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
        except (TmuxViewError, ConflictError) as error:
            with suppress(ConflictError):
                self.registry.advance_transition_state(
                    transition.transition_id,
                    TransitionState.EXECUTING,
                    TransitionState.FAILED,
                    execution_owner=owner,
                    failure=FailureRecord(
                        "mode_change_uncertain", "View mode change needs repair."
                    ),
                    now=now,
                )
            self._open_recovery(
                kind="view_mode",
                subject_type="view",
                subject_id=str(view.view_id),
                actionability=RecoveryActionability.SAFE_AUTO,
                explanation=(
                    "The sidebar topology does not match the desired view mode."
                ),
                now=now,
            )
            raise ViewRuntimeError("mode_change_uncertain", str(error)) from error
        return self.registry.get_view(view.view_id)

    def recover_view(self, view_id: ViewId, *, now: int) -> ViewRecoveryResult:
        self._require_mutation("view recover")
        view = self.registry.get_view(ViewId(view_id))
        current_server = self.tmux.server_evidence(self.host_id, observed_at=now)
        self.registry.record_tmux_server(current_server)
        executor = TmuxExecutor(
            current_server.socket_path, executable=self.tmux.executable
        )
        repaired = False
        same_generation = view.tmux_server_id == current_server.tmux_server_id
        provider_resume_required = False
        try:
            observation = executor.inspect_shell(
                self.config.tmux.naming_prefix,
                self.generation_id,
                view.view_id,
                view.mode,
            )
            if (
                view.mode is ViewMode.NAVIGATOR
                and observation.sidebar is not None
                and observation.sidebar.dead
            ):
                observation = executor.restart_sidebar(
                    prefix=self.config.tmux.naming_prefix,
                    generation_id=self.generation_id,
                    view_id=view.view_id,
                    sidebar_command=self._sidebar_command(view.view_id),
                )
                repaired = True
        except TmuxViewError:
            if same_generation:
                opposite = (
                    ViewMode.NAVIGATOR
                    if view.mode is ViewMode.DIRECT
                    else ViewMode.DIRECT
                )
                try:
                    executor.inspect_shell(
                        self.config.tmux.naming_prefix,
                        self.generation_id,
                        view.view_id,
                        opposite,
                    )
                except TmuxViewError:
                    # Unknown partial topology is never mutated blindly.
                    names = executor.names(self.config.tmux.naming_prefix, view.view_id)
                    if any(
                        executor.run("has-session", "-t", name, check=False).returncode
                        == 0
                        for name in (names.view_session, names.holding_session)
                    ):
                        raise ViewRuntimeError(
                            "view_recovery_manual",
                            "partial view topology requires pane-metadata inspection",
                        ) from None
                else:
                    observation = executor.set_mode(
                        prefix=self.config.tmux.naming_prefix,
                        generation_id=self.generation_id,
                        view_id=view.view_id,
                        current_mode=opposite,
                        target_mode=view.mode,
                        sidebar_command=self._sidebar_command(view.view_id),
                    )
                    repaired = True
            if not repaired:
                if any(
                    placement.surface_id is not None
                    for placement in self.registry.list_placements(view_id=view.view_id)
                ):
                    if view.tmux_server_id is None:
                        raise ViewRuntimeError(
                            "view_tmux_missing",
                            "surface-backed view has no tmux evidence",
                        ) from None
                    self.registry.invalidate_view_server_surfaces(
                        view.view_id, view.tmux_server_id, now=now
                    )
                    provider_resume_required = True
                    self._open_recovery(
                        kind="provider_resume_required",
                        subject_type="view",
                        subject_id=str(view.view_id),
                        actionability=RecoveryActionability.MANUAL,
                        explanation=(
                            "The tmux server was replaced; exact provider UUID "
                            "recovery is required."
                        ),
                        now=now,
                    )
                observation = executor.create_shell(
                    prefix=self.config.tmux.naming_prefix,
                    generation_id=self.generation_id,
                    view_id=view.view_id,
                    frame_id=str(view.active_frame_id),
                    mode=view.mode,
                    sidebar_command=self._sidebar_command(view.view_id),
                )
                repaired = True
        if not same_generation or view.state is not ViewState.READY:
            view = self.registry.rebind_view_tmux_server(
                view.view_id,
                view.revision,
                current_server.tmux_server_id,
                (ViewState.DEGRADED if provider_resume_required else ViewState.READY),
                now=now,
            )
            repaired = True
        return ViewRecoveryResult(view, repaired, observation)

    def presentation_directive(
        self,
        view: UserView,
        *,
        request_id: RequestId,
        can_focus_desktop: bool,
        can_launch_terminal: bool,
        now: int,
    ) -> PresentationDirective:
        self._require_mutation("view presentation")
        current = self.registry.get_view(view.view_id)
        if (
            current.revision != view.revision
            or current.state is not ViewState.READY
            or current.desktop_token != view.desktop_token
        ):
            return PresentationDirective(
                str(RequestId(request_id)),
                str(self.host_id),
                DirectiveKind.BLOCKED,
                error=FailureRecord(
                    "view_stale", "The view changed before desktop presentation."
                ),
            )
        view = current
        if can_focus_desktop:
            return PresentationDirective(
                str(RequestId(request_id)),
                str(self.host_id),
                DirectiveKind.FOCUS,
                str(view.view_id),
                view.revision,
                view.desktop_token,
                None,
                None,
            )
        if not can_launch_terminal:
            return PresentationDirective(
                str(RequestId(request_id)),
                str(self.host_id),
                DirectiveKind.BLOCKED,
                None,
                None,
                None,
                None,
                FailureRecord(
                    "desktop_unavailable", "No desktop focus or attach is permitted."
                ),
            )
        lease = self.registry.offer_desktop_lease(
            DesktopAttachmentLease(
                LeaseId(uuid4()),
                view.view_id,
                RequestId(request_id),
                LeaseState.OFFERED,
                now + 15_000,
            ),
            now=now,
        )
        return PresentationDirective(
            str(RequestId(request_id)),
            str(self.host_id),
            DirectiveKind.ATTACH,
            str(view.view_id),
            view.revision,
            view.desktop_token,
            lease.expires_at,
            None,
        )

    def attach_view(self, view_id: ViewId, *, now: int) -> ViewAttachResult:
        """Validate one exact shell and record a managed attach intent."""

        self._require_mutation("view attach")
        view = self.registry.get_view(ViewId(view_id))
        executor = self._tmux_for_view(view)
        executor.inspect_shell(
            self.config.tmux.naming_prefix,
            self.generation_id,
            view.view_id,
            view.mode,
        )
        view = self.registry.mark_view_attached(view.view_id, view.revision, now=now)
        return ViewAttachResult(
            view,
            executor.attach_argv(self.config.tmux.naming_prefix, view.view_id),
        )

    def retire_view(self, view_id: ViewId, *, now: int) -> UserView:
        self._require_mutation("view retire")
        view = self.registry.get_view(ViewId(view_id))
        retired = self.registry.retire_view(view.view_id, view.revision, now=now)
        executor = self._tmux_for_view(view)
        try:
            executor.retire_shell(
                prefix=self.config.tmux.naming_prefix,
                generation_id=self.generation_id,
                view_id=view.view_id,
                mode=view.mode,
            )
        except TmuxViewError as error:
            self._open_recovery(
                kind="retired_view_shell",
                subject_type="view",
                subject_id=str(view.view_id),
                actionability=RecoveryActionability.SAFE_AUTO,
                explanation="A retired view left an exact Switchboard tmux container.",
                now=now,
            )
            raise ViewRuntimeError("retired_view_shell", str(error)) from error
        return retired


__all__ = [
    "ViewAttachResult",
    "ViewOpenResult",
    "ViewRecoveryResult",
    "ViewRuntime",
    "ViewRuntimeError",
]
