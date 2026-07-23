"""Terminal-native entry and exact tmux client handoff for managed views."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .domain import (
    FailureRecord,
    FrameId,
    HostId,
    ProjectId,
    RecoveryActionability,
    RecoveryId,
    RecoveryState,
    RequestId,
    ViewId,
    ViewMode,
)
from .protocol import DirectiveKind, PresentationDirective
from .remote import RemoteRuntime
from .storage import ConflictError
from .tmux_view import TmuxExecutor
from .views import ViewRuntime, ViewRuntimeError

MAX_CROSS_HOST_HOPS = 4


@dataclass(frozen=True, slots=True)
class EntryTarget:
    project_id: ProjectId | None = None
    reuse_view_id: ViewId | None = None
    view_id: ViewId | None = None
    frame_id: FrameId | None = None
    recovery_id: RecoveryId | None = None


@dataclass(frozen=True, slots=True)
class EntryResult:
    view_id: ViewId
    exec_argv: tuple[str, ...] | None = None
    directive: PresentationDirective | None = None


def _child_request(request_id: RequestId, operation: str) -> RequestId:
    return RequestId(
        uuid5(
            NAMESPACE_URL,
            f"agent-switchboard:v3:view-enter:{request_id}:{operation}",
        )
    )


class TerminalEntryRuntime:
    """Prepare an owner-local view, then enter through one exact terminal client."""

    def __init__(
        self,
        runtime: ViewRuntime,
        *,
        environment: Mapping[str, str] | None = None,
        remote_runtime: RemoteRuntime | None = None,
    ) -> None:
        self.runtime = runtime
        self.opened = runtime.opened
        self.environment = dict(os.environ if environment is None else environment)
        self.remote_runtime = remote_runtime

    def _managed_source(
        self,
    ) -> tuple[TmuxExecutor, str, ViewId, int] | None:
        inherited = TmuxExecutor.inherited_context(self.environment)
        if inherited is None:
            return None
        socket_path, pane_id = inherited
        executor = TmuxExecutor(socket_path, executable=self.runtime.tmux.executable)
        pane = executor._pane(pane_id)
        if pane.view_id is None or pane.generation_id != str(self.opened.generation_id):
            raise ViewRuntimeError(
                "managed_view_required",
                "detach from the current tmux client, then run view enter again "
                "from a plain shell",
            )
        try:
            view_id = ViewId(pane.view_id)
            self.opened.registry.get_view(view_id)
        except (ValueError, ConflictError) as error:
            raise ViewRuntimeError(
                "managed_view_required",
                "invoking pane is not owned by the current Switchboard generation",
            ) from error
        depth = executor.pane_hop_depth(
            generation_id=self.opened.generation_id,
            view_id=view_id,
            pane_id=pane_id,
        )
        return executor, pane_id, view_id, depth

    def _prepare_recovery(self, recovery_id: RecoveryId, *, now: int):
        recovery = self.opened.registry.get_recovery(recovery_id)
        if recovery.actionability is RecoveryActionability.MANUAL:
            raise ViewRuntimeError(
                "manual_recovery_required", recovery.bounded_explanation
            )
        if recovery.subject_type != "view":
            raise ViewRuntimeError(
                "recovery_route_unavailable",
                "recovery does not own a presentable view",
            )
        view_id = ViewId(recovery.subject_id)
        if recovery.actionability is RecoveryActionability.SAFE_AUTO:
            view = self.runtime.recover_view(view_id, now=now).view
            self.opened.registry.settle_recovery(
                recovery.recovery_id, RecoveryState.RESOLVED, now=now
            )
            return view
        return self.runtime.open_view(view_id, now=now).view

    def _prepare_local(
        self,
        target: EntryTarget,
        *,
        request_id: RequestId,
        mode: ViewMode,
        confirm_background_transfer: bool,
        hop_depth: int,
        now: int,
    ):
        if target.project_id is not None:
            view = self.runtime.open_project(
                target.project_id,
                request_id=_child_request(request_id, "project"),
                view_id=target.reuse_view_id,
                mode=mode,
                now=now,
            ).view
        elif target.view_id is not None:
            view = self.runtime.open_view(target.view_id, now=now).view
            if target.frame_id is not None and view.active_frame_id != target.frame_id:
                view = self.runtime.focus_frame(
                    view.view_id,
                    target.frame_id,
                    request_id=_child_request(request_id, "frame"),
                    confirm_background_transfer=confirm_background_transfer,
                    now=now,
                )
        elif target.recovery_id is not None:
            view = self._prepare_recovery(target.recovery_id, now=now)
        else:  # pragma: no cover - CLI and tests construct exactly one target
            raise ValueError("view entry target is missing")
        if view.mode is not mode:
            view = self.runtime.set_mode(
                view.view_id,
                mode,
                request_id=_child_request(request_id, "mode"),
                now=now,
            )
        executor = self.runtime._tmux_for_view(view)
        shell = executor.inspect_shell(
            self.opened.config.tmux.naming_prefix,
            self.opened.generation_id,
            view.view_id,
            view.mode,
        )
        executor.set_pane_hop_depth(
            generation_id=self.opened.generation_id,
            view_id=view.view_id,
            pane_id=shell.active.pane_id,
            depth=hop_depth,
        )
        return view, executor

    @staticmethod
    def _remote_arguments(
        host_id: HostId,
        target: EntryTarget,
        *,
        request_id: RequestId,
        mode: ViewMode,
        confirm_background_transfer: bool,
        hop_depth: int,
    ) -> list[str]:
        arguments = ["view", "enter", "--host", str(host_id)]
        if target.project_id is not None:
            arguments.extend(("--project", str(target.project_id)))
            if target.reuse_view_id is not None:
                arguments.extend(("--reuse-view", str(target.reuse_view_id)))
        elif target.view_id is not None:
            arguments.extend(("--view", str(target.view_id)))
            if target.frame_id is not None:
                arguments.extend(("--frame", str(target.frame_id)))
        elif target.recovery_id is not None:
            arguments.extend(("--recovery", str(target.recovery_id)))
        arguments.extend(
            (
                "--mode",
                mode.value,
                "--request-id",
                str(request_id),
                "--hop-depth",
                str(hop_depth),
                "--preflight-only",
            )
        )
        if confirm_background_transfer:
            arguments.append("--confirm-background-transfer")
        return arguments

    def enter(
        self,
        host_id: HostId,
        target: EntryTarget,
        *,
        request_id: RequestId,
        mode: ViewMode,
        confirm_background_transfer: bool,
        preflight_only: bool,
        hop_depth: int | None,
        now: int,
    ) -> EntryResult:
        source = self._managed_source()
        local_host = self.opened.config.host.host_id
        if host_id == local_host:
            depth = (
                hop_depth
                if preflight_only and hop_depth is not None
                else (0 if source is None else source[3])
            )
            if not 0 <= depth <= MAX_CROSS_HOST_HOPS:
                raise ViewRuntimeError(
                    "hop_depth_invalid", "hop depth must be between 0 and 4"
                )
            view, target_executor = self._prepare_local(
                target,
                request_id=request_id,
                mode=mode,
                confirm_background_transfer=confirm_background_transfer,
                hop_depth=depth,
                now=now,
            )
            if preflight_only:
                directive = self.runtime.presentation_directive(
                    view,
                    request_id=request_id,
                    can_focus_desktop=False,
                    can_launch_terminal=True,
                    now=now,
                )
                return EntryResult(view.view_id, directive=directive)
            if source is None:
                directive = self.runtime.presentation_directive(
                    view,
                    request_id=request_id,
                    can_focus_desktop=False,
                    can_launch_terminal=True,
                    now=now,
                )
                if directive.kind is not DirectiveKind.ATTACH:
                    failure = directive.error or FailureRecord(
                        "terminal_entry_blocked",
                        "local owner did not offer attachment",
                    )
                    raise ViewRuntimeError(failure.code, failure.message)
                attached = self.runtime.attach_view(
                    view.view_id, request_id=request_id, now=now
                )
                return EntryResult(
                    view.view_id,
                    exec_argv=attached.attach_argv,
                )
            source_executor, source_pane, source_view_id, _source_depth = source
            if source_view_id == view.view_id:
                return EntryResult(view.view_id)
            if Path(source_executor.socket_path or "") != Path(
                target_executor.socket_path or ""
            ):
                raise ViewRuntimeError(
                    "local_tmux_server_mismatch",
                    "target view uses another tmux server; detach and enter it from "
                    "a plain shell",
                )
            client = source_executor.exact_client_for_pane(source_pane)
            target_session = target_executor.names(
                self.opened.config.tmux.naming_prefix, view.view_id
            ).view_session
            source_executor.switch_exact_client(
                client_name=client,
                source_pane_id=source_pane,
                target_session=target_session,
            )
            return EntryResult(view.view_id)

        current_depth = 0 if source is None else source[3]
        if current_depth >= MAX_CROSS_HOST_HOPS:
            raise ViewRuntimeError(
                "cross_host_hop_limit",
                "detach the outer SSH connection and run view enter directly for "
                "the desired host",
            )
        remote_depth = current_depth + 1
        remote = self.remote_runtime or RemoteRuntime(
            self.opened.config, self.opened.registry
        )
        directive = asyncio.run(
            remote.directive(
                host_id,
                self._remote_arguments(
                    host_id,
                    target,
                    request_id=request_id,
                    mode=mode,
                    confirm_background_transfer=confirm_background_transfer,
                    hop_depth=remote_depth,
                ),
            )
        )
        if directive.kind is not DirectiveKind.ATTACH or directive.view_id is None:
            failure = directive.error or FailureRecord(
                "remote_entry_blocked", "remote owner did not offer attachment"
            )
            raise ViewRuntimeError(failure.code, failure.message)
        attach = remote.attach_command(
            host_id, view_id=directive.view_id, request_id=str(request_id)
        )
        if source is None:
            return EntryResult(ViewId(directive.view_id), exec_argv=attach)
        source_executor, source_pane, _source_view, _source_depth = source
        client = source_executor.exact_client_for_pane(source_pane)
        source_executor.replace_exact_client(
            client_name=client,
            source_pane_id=source_pane,
            command=attach,
        )
        return EntryResult(ViewId(directive.view_id))


__all__ = [
    "MAX_CROSS_HOST_HOPS",
    "EntryResult",
    "EntryTarget",
    "TerminalEntryRuntime",
]
