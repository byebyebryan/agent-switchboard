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
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import __version__
from .agent_mcp import AgentToolService, run_mcp_server
from .config import parse_config_template
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
    SessionKey,
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
    initialize,
    open_generation,
    reset,
    resolve_current,
    rollback,
    status,
)
from .hook_config import HookConfigError, edit_hooks
from .protocol import (
    DirectiveKind,
    PresentationDirective,
    build_host_state,
    build_navigator_from_registry,
)
from .provider_runtime import ProviderRuntimeError, probe_contract
from .remote import RemoteError, RemoteRuntime
from .storage import ConflictError
from .tmux_view import TmuxExecutor, TmuxViewError
from .trusted_hook import HookInputError, handle_trusted_event, read_hook_json
from .views import ViewRuntime, ViewRuntimeError
from .workflow import WorkflowError, WorkflowRuntime, spawn_control_watchdog


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
    return opened, WorkflowRuntime(
        opened,
        paths,
        tmux=tmux,
        watchdog_launcher=lambda transition_id: spawn_control_watchdog(
            paths,
            opened.generation_id,
            transition_id,
            delay_seconds=opened.config.control_turns.watchdog_timeout_seconds,
        ),
    )


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
        try:
            evidence = CutoverEvidence.from_json(arguments.evidence.read_bytes())
        except OSError as error:
            raise GenerationError("cutover_evidence_invalid", str(error)) from error
        result = commit(
            paths,
            evidence,
            committed_at=_timestamp(arguments.at),
        )
        _print(result)
    elif arguments.cutover_command == "rollback":
        previous = rollback(paths)
        _print({"previousGenerationId": None if previous is None else str(previous)})
    else:  # pragma: no cover - argparse owns the command set
        raise AssertionError(arguments.cutover_command)
    return 0


def _template(path: Path, generation_id: GenerationId):
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise GenerationError("generation_config_invalid", str(error)) from error
    return parse_config_template(raw, generation_id)


def _init(arguments: argparse.Namespace) -> int:
    generation_id = (
        GenerationId.new()
        if arguments.generation_id is None
        else GenerationId(arguments.generation_id)
    )
    result = initialize(
        _template(arguments.config, generation_id),
        _paths(arguments),
        created_at=_timestamp(arguments.at),
    )
    _print(result)
    return 0


def _reset(arguments: argparse.Namespace) -> int:
    paths = _paths(arguments)
    expected = GenerationId(arguments.confirm_generation)
    generation_id = (
        GenerationId.new()
        if arguments.generation_id is None
        else GenerationId(arguments.generation_id)
    )
    if arguments.config is None:
        with open_generation(paths, expected) as opened:
            config = replace(opened.config, generation_id=generation_id)
    else:
        config = _template(arguments.config, generation_id)
    result = reset(
        config,
        paths,
        expected_current=expected,
        created_at=_timestamp(arguments.at),
    )
    _print(result)
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
            request_id = _request(arguments.request_id)
            host_id = (
                opened.config.host.host_id
                if arguments.host is None
                else HostId(arguments.host)
            )
            if host_id != opened.config.host.host_id:
                attach_argv = RemoteRuntime(
                    opened.config, opened.registry
                ).attach_command(
                    host_id,
                    view_id=arguments.view,
                    request_id=str(request_id),
                )
                os.execvp(attach_argv[0], attach_argv)
            opened_view = runtime.open_view(ViewId(arguments.view), now=timestamp)
            directive = runtime.presentation_directive(
                opened_view.view,
                request_id=request_id,
                can_focus_desktop=False,
                can_launch_terminal=True,
                now=timestamp,
            )
            if directive.kind is not DirectiveKind.ATTACH:
                raise ViewRuntimeError(
                    "ssh_attach_unavailable",
                    "view did not offer an SSH attachment lease",
                )
            result = runtime.attach_view(
                ViewId(arguments.view),
                request_id=request_id,
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
    raw_session_key = os.environ.get("SWB_V3_SESSION_KEY")
    raw_generation_id = os.environ.get("SWB_V3_GENERATION_ID")
    if not raw_capability and not raw_session_key and not raw_generation_id:
        # Provider hook configuration is global, while Switchboard authority is
        # intentionally pane-local. Unmanaged provider sessions are outside
        # our ownership boundary and must not be disrupted by the hook.
        return 0
    if raw_capability and raw_session_key and not raw_generation_id:
        # Sessions launched before Phase 6E.1 have no generation marker. They
        # may outlive a reset and are intentionally left unmanaged rather than
        # disrupted by a newly installed global hook.
        return 0
    if not raw_capability or not raw_session_key or not raw_generation_id:
        print("swbctl: incomplete managed hook authority", file=sys.stderr)
        return 2
    try:
        expected_generation = GenerationId(raw_generation_id)
    except ValueError:
        print("swbctl: incomplete managed hook authority", file=sys.stderr)
        return 2
    try:
        current_generation = resolve_current(_paths(arguments))
    except GenerationError:
        # Switchboard state is disposable. Its absence or damage must not make
        # a provider session unusable merely because a global hook remains.
        return 0
    if current_generation != expected_generation:
        return 0
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
    except (
        HookInputError,
        WorkflowError,
        ConflictError,
        GenerationError,
        ValueError,
    ):
        print("swbctl: managed hook event rejected", file=sys.stderr)
        return 2
    return 0


def _control_watchdog(arguments: argparse.Namespace) -> int:
    if not 0 <= arguments.delay_ms <= 60_000:
        raise ValueError("watchdog delay must be between 0 and 60000 ms")
    if arguments.delay_ms:
        time.sleep(arguments.delay_ms / 1_000)
    opened, workflow = _open_workflow(arguments)
    try:
        if str(opened.generation_id) != arguments.generation_id:
            _print(
                {
                    "transitionId": arguments.transition,
                    "controlState": "superseded_generation",
                }
            )
            return 0
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


def _reconcile(arguments: argparse.Namespace) -> int:
    opened, workflow = _open_workflow(arguments)
    try:
        controls = workflow.reconcile_control_turns(now=_timestamp(arguments.at))
        _print(
            {
                "generationId": str(opened.generation_id),
                "overdueControls": len(controls),
                "transitionIds": [str(item.transition_id) for item in controls],
            }
        )
        return 0
    finally:
        opened.close()


def _frame(arguments: argparse.Namespace) -> int:
    opened, workflow = _open_workflow(arguments)
    try:
        timestamp = _timestamp(arguments.at)
        state = build_host_state(opened.registry, generated_at=timestamp).to_dict()
        frames = list(state["frames"])
        if arguments.frame_command == "list":
            _print(frames)
            return 0
        if arguments.frame_command == "show":
            match = next(
                (item for item in frames if item["frameId"] == arguments.frame), None
            )
            if match is None:
                raise ConflictError("not_found", "frame does not exist")
            _print(match)
            return 0
        if arguments.frame_command == "reopen":
            host_id = HostId(arguments.host)
            if host_id != opened.config.host.host_id:
                raise WorkflowError(
                    "remote_reopen_unavailable",
                    "frame reopen must execute directly on the owner host",
                )
            session = workflow.reopen_imported_session(
                FrameId(arguments.frame),
                SessionKey.parse(arguments.session),
                request_id=RequestId(arguments.request_id),
                now=timestamp,
            )
            _print(
                {
                    "frameId": arguments.frame,
                    "sessionKey": str(session.session_key),
                    "runtimePresence": session.runtime_presence.value,
                }
            )
            return 0
        raise WorkflowError(
            "agent_authority_required",
            f"frame {arguments.frame_command} must be requested through agent tools",
        )
    finally:
        opened.close()


def _project(arguments: argparse.Namespace) -> int:
    with open_generation(_paths(arguments)) as opened:
        projects = [
            {
                "hostId": str(opened.config.host.host_id),
                "projectId": str(item.project_id),
                "name": item.name,
                "aliases": list(item.aliases),
            }
            for item in opened.config.projects
        ]
        if arguments.project_command == "list":
            _print(projects)
        else:
            match = next(
                (item for item in projects if item["projectId"] == arguments.project),
                None,
            )
            if match is None:
                raise ConflictError("not_found", "project does not exist")
            _print(match)
    return 0


def _session(arguments: argparse.Namespace) -> int:
    opened, workflow = _open_workflow(arguments)
    try:
        session_key = SessionKey.parse(arguments.session)
        if session_key.host_id != opened.config.host.host_id:
            raise WorkflowError(
                "remote_session_action_unavailable",
                "session action must execute directly on the owner host",
            )
        session = (
            workflow.stop_session(session_key, now=_timestamp(arguments.at))
            if arguments.session_command == "stop"
            else opened.registry.get_provider_session(session_key)
        )
        _print(
            {
                "sessionKey": str(session.session_key),
                "hostId": str(session.host_id),
                "provider": session.provider.value,
                "providerSessionId": str(session.provider_session_id),
                "projectId": (
                    None if session.project_id is None else str(session.project_id)
                ),
                "runtimePresence": session.runtime_presence.value,
                "resumability": session.resumability.value,
                "activity": session.activity.value,
            }
        )
        return 0
    finally:
        opened.close()


def _hooks(arguments: argparse.Namespace) -> int:
    with open_generation(_paths(arguments)) as opened:
        opened.require_mutation(f"hooks {arguments.hooks_command}")
    executable = arguments.executable
    if executable is None:
        discovered = shutil.which("swbctl")
        if discovered is None:
            raise HookConfigError("installed swbctl executable was not found")
        executable = Path(discovered)
    result = edit_hooks(
        arguments.hooks_command,
        arguments.provider,
        executable=executable,
        timeout_seconds=arguments.timeout,
        dry_run=arguments.dry_run,
    )
    _print(
        {
            "path": str(result.path),
            "changed": result.changed,
            "removedHandlers": result.removed_handlers,
            "installedHandlers": result.installed_handlers,
            "dryRun": result.dry_run,
        }
    )
    return 0


def _doctor(arguments: argparse.Namespace) -> int:
    paths = _paths(arguments)
    with open_generation(paths) as opened:
        providers: list[dict[str, Any]] = []
        for configured in opened.config.providers:
            if not configured.enabled:
                providers.append(
                    {"provider": configured.provider.value, "enabled": False}
                )
                continue
            try:
                contract = probe_contract(
                    configured.provider, executable=configured.executable
                )
                providers.append(
                    {
                        "provider": contract.provider.value,
                        "enabled": True,
                        "available": True,
                        "version": contract.version,
                        "knownGoodObservation": contract.known_good,
                    }
                )
            except ProviderRuntimeError as error:
                providers.append(
                    {
                        "provider": configured.provider.value,
                        "enabled": True,
                        "available": False,
                        "error": {"code": error.code, "message": str(error)},
                    }
                )
        _print(
            {
                "version": __version__,
                "generationId": str(opened.generation_id),
                "activationState": opened.activation_state.value,
                "hostId": str(opened.config.host.host_id),
                "providers": providers,
                "remotes": [
                    {
                        "alias": item.alias,
                        "displayName": item.display_name,
                        "cached": any(
                            cached.remote_name == item.alias
                            for cached in opened.registry.cached_host_states()
                        ),
                    }
                    for item in opened.config.remotes
                ],
            }
        )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swbctl",
        description="Own persistent project and task views for coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"swbctl {__version__}")
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--state-root", type=Path)
    root = parser.add_subparsers(dest="command", required=True)

    init = root.add_parser("init")
    init.add_argument("--config", type=Path, required=True)
    init.add_argument("--generation-id")
    init.add_argument("--at", type=int)

    reset_command = root.add_parser("reset")
    reset_command.add_argument("--confirm-generation", required=True)
    reset_command.add_argument("--config", type=Path)
    reset_command.add_argument("--generation-id")
    reset_command.add_argument("--at", type=int)

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
    commit_command.add_argument("--evidence", type=Path, required=True)
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
    attach.add_argument("--host")
    attach.add_argument("--view", required=True)
    attach.add_argument("--request-id")
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
    watchdog.add_argument("--generation-id", required=True)
    watchdog.add_argument("--delay-ms", type=int, default=0)
    watchdog.add_argument("--at", type=int)
    reconcile = root.add_parser("reconcile")
    reconcile.add_argument("--at", type=int)
    reconcile.add_argument("--json", action="store_true")

    frame = root.add_parser("frame")
    frame_sub = frame.add_subparsers(dest="frame_command", required=True)
    frame_sub.add_parser("list").add_argument("--at", type=int)
    frame_show = frame_sub.add_parser("show")
    frame_show.add_argument("--frame", required=True)
    frame_show.add_argument("--at", type=int)
    frame_reopen = frame_sub.add_parser("reopen")
    frame_reopen.add_argument("--host", required=True)
    frame_reopen.add_argument("--frame", required=True)
    frame_reopen.add_argument("--session", required=True)
    frame_reopen.add_argument("--request-id", required=True)
    frame_reopen.add_argument("--at", type=int)
    for name in ("push", "back", "complete", "close"):
        command = frame_sub.add_parser(name)
        command.add_argument("--at", type=int)

    project = root.add_parser("project")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    project_sub.add_parser("list")
    project_show = project_sub.add_parser("show")
    project_show.add_argument("--project", required=True)

    session = root.add_parser("session")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    for name in ("show", "stop"):
        command = session_sub.add_parser(name)
        command.add_argument("--session", required=True)
        command.add_argument("--at", type=int)

    hooks = root.add_parser("hooks")
    hooks_sub = hooks.add_subparsers(dest="hooks_command", required=True)
    for name in ("install", "uninstall"):
        command = hooks_sub.add_parser(name)
        command.add_argument("--provider", choices=("codex", "claude"), required=True)
        command.add_argument("--executable", type=Path)
        command.add_argument("--timeout", type=int, default=1)
        command.add_argument("--dry-run", action="store_true")

    doctor = root.add_parser("doctor")
    doctor.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "init":
            return _init(arguments)
        if arguments.command == "reset":
            return _reset(arguments)
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
        if arguments.command == "reconcile":
            return _reconcile(arguments)
        if arguments.command == "frame":
            return _frame(arguments)
        if arguments.command == "project":
            return _project(arguments)
        if arguments.command == "session":
            return _session(arguments)
        if arguments.command == "hooks":
            return _hooks(arguments)
        if arguments.command == "doctor":
            return _doctor(arguments)
        raise AssertionError(arguments.command)  # pragma: no cover
    except (
        CutoverError,
        GenerationError,
        HookConfigError,
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
