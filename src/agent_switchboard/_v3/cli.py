"""Private Phase 6 command surface used before the clean-break activation.

This module is intentionally not registered as ``swbctl`` while 0.2 remains
installed.  It gives isolated rehearsals one complete replacement command
path without exposing two public product generations at once.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_switchboard.hooks import HookInputError, read_hook_json

from .agent_mcp import AgentToolService, run_mcp_server
from .cutover import CutoverBundle, CutoverError, export_artifacts
from .domain import (
    FailureRecord,
    FrameId,
    GenerationId,
    HostId,
    ProjectId,
    ProviderId,
    RecoveryActionability,
    RecoveryId,
    RecoveryState,
    RequestId,
    TransitionId,
    ViewId,
    ViewMode,
)
from .generation import (
    CutoverEvidence,
    GenerationError,
    GenerationPaths,
    commit,
    import_bundle,
    open_generation,
    rollback,
    status,
)
from .protocol import (
    DirectiveKind,
    PresentationDirective,
    build_host_state,
    build_navigator_from_registry,
)
from .remote import RemoteError, RemoteRuntime
from .storage import ConflictError
from .tmux_view import TmuxExecutor, TmuxViewError
from .trusted_hook import handle_trusted_event
from .views import ViewRuntime, ViewRuntimeError
from .workflow import WorkflowError, WorkflowRuntime


def _now() -> int:
    return int(time.time() * 1_000)


def _paths(arguments: argparse.Namespace) -> GenerationPaths:
    config_root = arguments.config_root
    state_root = arguments.state_root
    if config_root is None:
        explicit = os.environ.get("SWB_V3_CONFIG_ROOT")
        config_root = (
            Path(explicit).expanduser()
            if explicit
            else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "agent-switchboard"
        )
    if state_root is None:
        explicit = os.environ.get("SWB_V3_STATE_ROOT")
        state_root = (
            Path(explicit).expanduser()
            if explicit
            else Path(
                os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
            )
            / "agent-switchboard"
        )
    return GenerationPaths(Path(config_root), Path(state_root))


def _request(value: str | None) -> RequestId:
    return RequestId(value or uuid4())


def _timestamp(value: int | None) -> int:
    return _now() if value is None else value


def _print(value: Any) -> None:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _view_dict(view: Any) -> dict[str, Any]:
    return {
        "viewId": str(view.view_id),
        "hostId": str(view.host_id),
        "mode": view.mode.value,
        "activeFrameId": (
            None if view.active_frame_id is None else str(view.active_frame_id)
        ),
        "state": view.state.value,
        "revision": view.revision,
        "tmuxServerId": (
            None if view.tmux_server_id is None else str(view.tmux_server_id)
        ),
        "lastAttachedAt": view.last_attached_at,
        "updatedAt": view.updated_at,
    }


def _transition_dict(transition: Any) -> dict[str, Any]:
    return {
        "transitionId": str(transition.transition_id),
        "viewId": str(transition.view_id),
        "kind": transition.kind.value,
        "sourceFrameId": (
            None
            if transition.source_frame_id is None
            else str(transition.source_frame_id)
        ),
        "targetFrameId": str(transition.target_frame_id),
        "state": transition.state.value,
    }


def _open_runtime(arguments: argparse.Namespace):
    paths = _paths(arguments)
    opened = open_generation(paths)
    socket_path = os.environ.get("SWB_V3_TMUX_SOCKET")
    tmux = None if socket_path is None else TmuxExecutor(socket_path)
    return paths, opened, ViewRuntime(opened, paths, tmux=tmux)


def _open_workflow(arguments: argparse.Namespace):
    paths = _paths(arguments)
    opened = open_generation(paths)
    socket_path = os.environ.get("SWB_V3_TMUX_SOCKET")
    tmux = None if socket_path is None else TmuxExecutor(socket_path)
    return opened, WorkflowRuntime(opened, paths, tmux=tmux)


def _cutover(arguments: argparse.Namespace) -> int:
    paths = _paths(arguments)
    if arguments.cutover_command == "export":
        bundle = export_artifacts(
            arguments.database,
            arguments.config,
            arguments.destination,
            exported_at=_timestamp(arguments.at),
        )
        _print(
            {
                "bundleHash": bundle.bundle_hash,
                "destination": str(arguments.destination),
            }
        )
    elif arguments.cutover_command == "import":
        bundle = CutoverBundle.from_json(arguments.bundle.read_bytes())
        result = import_bundle(
            bundle,
            paths,
            generation_id=(
                None
                if arguments.generation_id is None
                else GenerationId(arguments.generation_id)
            ),
        )
        _print(result)
    elif arguments.cutover_command == "status":
        _print(status(paths))
    elif arguments.cutover_command == "commit":
        result = commit(
            paths,
            CutoverEvidence(
                arguments.core_version,
                arguments.dms_version,
                arguments.dms_cold_started,
                arguments.staged_reads_validated,
            ),
            committed_at=_timestamp(arguments.at),
        )
        _print(result)
    elif arguments.cutover_command == "rollback":
        previous = rollback(paths)
        _print({"previousGenerationId": None if previous is None else str(previous)})
    else:  # pragma: no cover - argparse owns the command set
        raise AssertionError(arguments.cutover_command)
    return 0


def _state(arguments: argparse.Namespace) -> int:
    with open_generation(_paths(arguments)) as opened:
        timestamp = _timestamp(arguments.at)
        if arguments.state_command == "host":
            print(build_host_state(opened.registry, generated_at=timestamp).to_json())
        else:
            if arguments.refresh:
                asyncio.run(
                    RemoteRuntime(opened.config, opened.registry).refresh(now=timestamp)
                )
            print(
                build_navigator_from_registry(
                    opened.registry,
                    generated_at=timestamp,
                    staleness_interval_seconds=(
                        opened.config.defaults.staleness_interval_seconds
                    ),
                ).to_json()
            )
    return 0


def _view(arguments: argparse.Namespace) -> int:
    _paths_value, opened, runtime = _open_runtime(arguments)
    try:
        timestamp = _timestamp(arguments.at)
        if arguments.view_command == "list":
            _print([_view_dict(view) for view in opened.registry.list_views()])
            return 0
        if arguments.view_command == "show":
            _print(_view_dict(opened.registry.get_view(ViewId(arguments.view))))
            return 0
        if arguments.view_command == "open":
            host_id = HostId(arguments.host)
            if host_id != opened.config.host.host_id:
                remote_arguments = [
                    "view",
                    "open",
                    "--host",
                    str(host_id),
                    "--view" if arguments.view is not None else "--project",
                    arguments.view if arguments.view is not None else arguments.project,
                    "--request-id",
                    arguments.request_id,
                    (
                        "--can-focus-desktop"
                        if arguments.can_focus_desktop
                        else "--no-focus-desktop"
                    ),
                ]
                if arguments.can_launch_terminal:
                    remote_arguments.append("--can-launch-terminal")
                remote_arguments.append("--json")
                directive = asyncio.run(
                    RemoteRuntime(opened.config, opened.registry).directive(
                        host_id, remote_arguments
                    )
                )
            else:
                request_id = RequestId(arguments.request_id)
                result = (
                    runtime.open_view(ViewId(arguments.view), now=timestamp)
                    if arguments.view is not None
                    else runtime.open_project(
                        ProjectId(arguments.project),
                        request_id=request_id,
                        mode=opened.config.views.desktop_default_mode,
                        now=timestamp,
                    )
                )
                directive = runtime.presentation_directive(
                    result.view,
                    request_id=request_id,
                    can_focus_desktop=arguments.can_focus_desktop,
                    can_launch_terminal=arguments.can_launch_terminal,
                    now=timestamp,
                )
            print(directive.to_json())
            return 0
        if arguments.view_command == "focus":
            view = runtime.focus_frame(
                ViewId(arguments.view),
                FrameId(arguments.frame),
                request_id=_request(arguments.request_id),
                now=timestamp,
            )
            _print(_view_dict(view))
            return 0
        if arguments.view_command == "mode":
            view = runtime.set_mode(
                ViewId(arguments.view),
                ViewMode(arguments.mode),
                request_id=_request(arguments.request_id),
                now=timestamp,
            )
            _print(_view_dict(view))
            return 0
        if arguments.view_command in {"back", "close"}:
            workflow = WorkflowRuntime(
                opened,
                _paths_value,
                tmux=runtime.tmux,
            )
            action = (
                workflow.human_back
                if arguments.view_command == "back"
                else workflow.human_close
            )
            transition = action(
                ViewId(arguments.view),
                request_id=_request(arguments.request_id),
                now=timestamp,
            )
            _print(_transition_dict(transition))
            return 0
        if arguments.view_command == "recover":
            host_id = HostId(arguments.host)
            if host_id != opened.config.host.host_id:
                remote_arguments = [
                    "view",
                    "recover",
                    "--host",
                    str(host_id),
                    "--recovery",
                    arguments.recovery,
                    "--request-id",
                    arguments.request_id,
                    (
                        "--can-focus-desktop"
                        if arguments.can_focus_desktop
                        else "--no-focus-desktop"
                    ),
                ]
                if arguments.can_launch_terminal:
                    remote_arguments.append("--can-launch-terminal")
                remote_arguments.append("--json")
                directive = asyncio.run(
                    RemoteRuntime(opened.config, opened.registry).directive(
                        host_id, remote_arguments
                    )
                )
            else:
                recovery = opened.registry.get_recovery(RecoveryId(arguments.recovery))
                request_id = RequestId(arguments.request_id)
                if recovery.actionability is RecoveryActionability.MANUAL:
                    directive = PresentationDirective(
                        str(request_id),
                        str(host_id),
                        DirectiveKind.BLOCKED,
                        error=FailureRecord(
                            "manual_recovery_required",
                            recovery.bounded_explanation,
                            False,
                        ),
                    )
                elif recovery.subject_type != "view":
                    directive = PresentationDirective(
                        str(request_id),
                        str(host_id),
                        DirectiveKind.BLOCKED,
                        error=FailureRecord(
                            "recovery_route_unavailable",
                            "Recovery does not own a presentable view.",
                            False,
                        ),
                    )
                else:
                    view_id = ViewId(recovery.subject_id)
                    if recovery.actionability is RecoveryActionability.SAFE_AUTO:
                        result = runtime.recover_view(view_id, now=timestamp)
                        opened.registry.settle_recovery(
                            recovery.recovery_id,
                            RecoveryState.RESOLVED,
                            now=timestamp,
                        )
                        view = result.view
                    else:
                        view = runtime.open_view(view_id, now=timestamp).view
                    directive = runtime.presentation_directive(
                        view,
                        request_id=request_id,
                        can_focus_desktop=arguments.can_focus_desktop,
                        can_launch_terminal=arguments.can_launch_terminal,
                        now=timestamp,
                    )
            print(directive.to_json())
            return 0
        if arguments.view_command == "attach":
            host_id = HostId(arguments.host)
            if host_id != opened.config.host.host_id:
                attach_argv = RemoteRuntime(
                    opened.config, opened.registry
                ).attach_command(
                    host_id,
                    view_id=arguments.view,
                    request_id=arguments.request_id,
                )
                os.execvp(attach_argv[0], attach_argv)
            result = runtime.attach_view(
                ViewId(arguments.view),
                request_id=RequestId(arguments.request_id),
                now=timestamp,
            )
            os.execvp(result.attach_argv[0], result.attach_argv)
        if arguments.view_command == "retire":
            _print(
                _view_dict(runtime.retire_view(ViewId(arguments.view), now=timestamp))
            )
            return 0
        raise AssertionError(arguments.view_command)  # pragma: no cover
    finally:
        opened.close()


def _agent_mcp(arguments: argparse.Namespace) -> int:
    raw_capability = os.environ.get("AGENT_SWITCHBOARD_CAPABILITY")
    if not raw_capability:
        return 2
    opened, workflow = _open_workflow(arguments)
    try:
        service = AgentToolService(
            workflow, raw_capability, now=_timestamp(arguments.at)
        )
        return run_mcp_server(service, sys.stdin.buffer, sys.stdout.buffer)
    finally:
        opened.close()


def _hook(arguments: argparse.Namespace) -> int:
    raw_capability = os.environ.get("AGENT_SWITCHBOARD_CAPABILITY")
    if not raw_capability:
        return 2
    try:
        payload = read_hook_json(sys.stdin.buffer)
        opened, workflow = _open_workflow(arguments)
        try:
            handle_trusted_event(
                workflow,
                ProviderId(arguments.provider),
                payload,
                os.environ,
                now=_timestamp(arguments.at),
            )
        finally:
            opened.close()
    except (HookInputError, WorkflowError, ConflictError, ValueError):
        return 2
    return 0


def _control_watchdog(arguments: argparse.Namespace) -> int:
    opened, workflow = _open_workflow(arguments)
    try:
        control = workflow.control_watchdog(
            TransitionId(arguments.transition), now=_timestamp(arguments.at)
        )
        _print(
            {
                "transitionId": str(control.transition_id),
                "controlState": control.state.value,
                "submissionCount": control.submission_count,
            }
        )
        return 0
    finally:
        opened.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent_switchboard._v3.cli")
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--state-root", type=Path)
    root = parser.add_subparsers(dest="command", required=True)

    cutover = root.add_parser("cutover")
    cutover_sub = cutover.add_subparsers(dest="cutover_command", required=True)
    export = cutover_sub.add_parser("export")
    export.add_argument("--database", type=Path, required=True)
    export.add_argument("--config", type=Path, required=True)
    export.add_argument("--destination", type=Path, required=True)
    export.add_argument("--at", type=int)
    import_command = cutover_sub.add_parser("import")
    import_command.add_argument("--bundle", type=Path, required=True)
    import_command.add_argument("--generation-id")
    cutover_sub.add_parser("status")
    commit_command = cutover_sub.add_parser("commit")
    commit_command.add_argument("--core-version", default="0.3.0")
    commit_command.add_argument("--dms-version", default="0.5.0")
    commit_command.add_argument("--dms-cold-started", action="store_true")
    commit_command.add_argument("--staged-reads-validated", action="store_true")
    commit_command.add_argument("--at", type=int)
    cutover_sub.add_parser("rollback")

    state = root.add_parser("state")
    state_sub = state.add_subparsers(dest="state_command", required=True)
    for name in ("host", "navigator"):
        command = state_sub.add_parser(name)
        command.add_argument("--at", type=int)
        command.add_argument("--json", action="store_true")
        if name == "navigator":
            command.add_argument("--refresh", action="store_true")

    view = root.add_parser("view")
    view_sub = view.add_subparsers(dest="view_command", required=True)
    view_sub.add_parser("list").add_argument("--at", type=int)
    show = view_sub.add_parser("show")
    show.add_argument("--view", required=True)
    show.add_argument("--at", type=int)
    open_command = view_sub.add_parser("open")
    open_command.add_argument("--host", required=True)
    open_target = open_command.add_mutually_exclusive_group(required=True)
    open_target.add_argument("--project")
    open_target.add_argument("--view")
    open_command.add_argument("--request-id", required=True)
    focus_capability = open_command.add_mutually_exclusive_group()
    focus_capability.add_argument(
        "--can-focus-desktop", action="store_true", default=False
    )
    focus_capability.add_argument(
        "--no-focus-desktop", dest="can_focus_desktop", action="store_false"
    )
    open_command.add_argument("--can-launch-terminal", action="store_true")
    open_command.add_argument("--json", action="store_true")
    open_command.add_argument("--at", type=int)
    focus = view_sub.add_parser("focus")
    focus.add_argument("--view", required=True)
    focus.add_argument("--frame", required=True)
    focus.add_argument("--request-id")
    focus.add_argument("--at", type=int)
    mode = view_sub.add_parser("mode")
    mode.add_argument("--view", required=True)
    mode.add_argument(
        "--mode", required=True, choices=[item.value for item in ViewMode]
    )
    mode.add_argument("--request-id")
    mode.add_argument("--at", type=int)
    for name in ("back", "close"):
        action = view_sub.add_parser(name)
        action.add_argument("--view", required=True)
        action.add_argument("--request-id")
        action.add_argument("--at", type=int)
    recover = view_sub.add_parser("recover")
    recover.add_argument("--host", required=True)
    recover.add_argument("--recovery", required=True)
    recover.add_argument("--request-id", required=True)
    recover_focus = recover.add_mutually_exclusive_group()
    recover_focus.add_argument(
        "--can-focus-desktop", action="store_true", default=False
    )
    recover_focus.add_argument(
        "--no-focus-desktop", dest="can_focus_desktop", action="store_false"
    )
    recover.add_argument("--can-launch-terminal", action="store_true")
    recover.add_argument("--json", action="store_true")
    recover.add_argument("--at", type=int)
    attach = view_sub.add_parser("attach")
    attach.add_argument("--host", required=True)
    attach.add_argument("--view", required=True)
    attach.add_argument("--request-id", required=True)
    attach.add_argument("--at", type=int)
    retire = view_sub.add_parser("retire")
    retire.add_argument("--view", required=True)
    retire.add_argument("--at", type=int)

    agent_mcp = root.add_parser("agent-mcp")
    agent_mcp.add_argument("--at", type=int)
    hook = root.add_parser("hook")
    hook.add_argument(
        "--provider", required=True, choices=[provider.value for provider in ProviderId]
    )
    hook.add_argument("--at", type=int)
    watchdog = root.add_parser("control-watchdog")
    watchdog.add_argument("--transition", required=True)
    watchdog.add_argument("--at", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "cutover":
            return _cutover(arguments)
        if arguments.command == "state":
            return _state(arguments)
        if arguments.command == "view":
            return _view(arguments)
        if arguments.command == "agent-mcp":
            return _agent_mcp(arguments)
        if arguments.command == "hook":
            return _hook(arguments)
        if arguments.command == "control-watchdog":
            return _control_watchdog(arguments)
        raise AssertionError(arguments.command)  # pragma: no cover
    except (
        CutoverError,
        GenerationError,
        ConflictError,
        TmuxViewError,
        ViewRuntimeError,
        WorkflowError,
        RemoteError,
        ValueError,
    ) as error:
        code = getattr(error, "code", type(error).__name__)
        _print({"error": {"code": code, "message": str(error)[:1024]}})
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
