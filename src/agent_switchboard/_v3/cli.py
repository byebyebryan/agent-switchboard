"""Private Phase 6 command surface used before the clean-break activation.

This module is intentionally not registered as ``swbctl`` while 0.2 remains
installed.  It gives isolated rehearsals one complete replacement command
path without exposing two public product generations at once.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .cutover import CutoverBundle, CutoverError, export_artifacts
from .domain import FrameId, GenerationId, ProjectId, RequestId, ViewId, ViewMode
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
from .protocol import build_host_state, build_navigator_from_registry
from .storage import ConflictError
from .tmux_view import TmuxExecutor, TmuxViewError
from .views import ViewRuntime, ViewRuntimeError


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


def _open_runtime(arguments: argparse.Namespace):
    paths = _paths(arguments)
    opened = open_generation(paths)
    socket_path = os.environ.get("SWB_V3_TMUX_SOCKET")
    tmux = None if socket_path is None else TmuxExecutor(socket_path)
    return paths, opened, ViewRuntime(opened, paths, tmux=tmux)


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
            print(
                build_navigator_from_registry(
                    opened.registry, generated_at=timestamp
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
            result = runtime.open_project(
                ProjectId(arguments.project),
                request_id=_request(arguments.request_id),
                view_id=None if arguments.view is None else ViewId(arguments.view),
                mode=None if arguments.mode is None else ViewMode(arguments.mode),
                now=timestamp,
            )
            payload = {
                "created": result.created,
                "view": _view_dict(result.view),
                "attachArgv": list(result.attach_argv),
            }
            if arguments.attach:
                os.execvp(result.attach_argv[0], result.attach_argv)
            _print(payload)
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
        if arguments.view_command == "recover":
            result = runtime.recover_view(ViewId(arguments.view), now=timestamp)
            _print({"repaired": result.repaired, "view": _view_dict(result.view)})
            return 0
        if arguments.view_command == "attach":
            result = runtime.attach_view(ViewId(arguments.view), now=timestamp)
            if arguments.print_argv:
                _print(
                    {
                        "attachArgv": list(result.attach_argv),
                        "view": _view_dict(result.view),
                    }
                )
                return 0
            os.execvp(result.attach_argv[0], result.attach_argv)
        if arguments.view_command == "retire":
            _print(
                _view_dict(runtime.retire_view(ViewId(arguments.view), now=timestamp))
            )
            return 0
        raise AssertionError(arguments.view_command)  # pragma: no cover
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

    view = root.add_parser("view")
    view_sub = view.add_subparsers(dest="view_command", required=True)
    view_sub.add_parser("list").add_argument("--at", type=int)
    show = view_sub.add_parser("show")
    show.add_argument("--view", required=True)
    show.add_argument("--at", type=int)
    open_command = view_sub.add_parser("open")
    open_command.add_argument("--project", required=True)
    open_command.add_argument("--view")
    open_command.add_argument("--mode", choices=[mode.value for mode in ViewMode])
    open_command.add_argument("--request-id")
    open_command.add_argument("--attach", action="store_true")
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
    recover = view_sub.add_parser("recover")
    recover.add_argument("--view", required=True)
    recover.add_argument("--at", type=int)
    attach = view_sub.add_parser("attach")
    attach.add_argument("--view", required=True)
    attach.add_argument("--print-argv", action="store_true")
    attach.add_argument("--at", type=int)
    retire = view_sub.add_parser("retire")
    retire.add_argument("--view", required=True)
    retire.add_argument("--at", type=int)
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
        raise AssertionError(arguments.command)  # pragma: no cover
    except (
        CutoverError,
        GenerationError,
        ConflictError,
        TmuxViewError,
        ViewRuntimeError,
        ValueError,
    ) as error:
        code = getattr(error, "code", type(error).__name__)
        _print({"error": {"code": code, "message": str(error)[:1024]}})
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
