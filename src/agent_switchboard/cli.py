"""Command-line entry point for Switchboard."""

from __future__ import annotations

import argparse
import importlib
import os
import sqlite3
import sys
import time
from collections.abc import Sequence

from . import __version__
from .config import SwitchboardConfig, load_config
from .curation import (
    CurationError,
    format_session_detail,
    read_handoff_input,
    read_session_detail,
    resolve_current_session_key,
)
from .doctor import run_all_doctors
from .domain import HostId, PresentationContext, ProviderId, SessionKey, ValidationError
from .executable import resolve_swbctl_executable
from .hook_config import edit_claude_hooks, edit_codex_hooks
from .hooks import HookInputError
from .live import reconcile_live
from .local import build_local_snapshot_json, materialize_configured_projects
from .local_events import ingest_local_event
from .migrations import MigrationError
from .paths import database_path, load_or_create_host_id
from .presentation import (
    LaunchCoordinator,
    PresentationError,
    attach_surface_argv,
    select_surface,
)
from .protocol import (
    PresentationPlanEnvelope,
    SessionActionEnvelope,
    SessionDetailEnvelope,
)
from .session_actions import ManagedSessionController
from .storage import DEFAULT_HANDOFF_LIMIT, MAX_HANDOFF_LIMIT, Registry, StorageError
from .tmux import TmuxController, TmuxError

_MAX_ERROR_MESSAGE_LENGTH = 1_024
_EPHEMERAL_CONFIG_HOST_ID = HostId("00000000-0000-4000-8000-000000000000")


def _safe_error_message(error: BaseException) -> str:
    printable = "".join(
        character if character.isprintable() else " " for character in str(error)
    )
    message = " ".join(printable.split())
    return message[:_MAX_ERROR_MESSAGE_LENGTH] or "operation failed"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swbctl",
        description="Inspect and route provider-native coding-agent sessions.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    snapshot = commands.add_parser(
        "snapshot",
        help="emit the host-local snapshot envelope",
    )
    snapshot.add_argument(
        "--reconcile",
        choices=("none", "live", "full"),
        default="none",
        help="repair retained state before reading (default: none)",
    )
    snapshot.add_argument("--json", action="store_true", required=True)

    list_command = commands.add_parser(
        "list",
        help="emit retained host-local state",
    )
    list_command.add_argument(
        "--refresh",
        action="store_true",
        help="refresh enabled providers before reading",
    )
    list_command.add_argument("--json", action="store_true", required=True)

    event = commands.add_parser(
        "event",
        help="ingest one provider lifecycle event from standard input",
    )
    event.add_argument("--provider", choices=("codex", "claude"), required=True)

    hooks = commands.add_parser(
        "hooks",
        help="manage explicit provider lifecycle hooks",
    )
    hook_actions = hooks.add_subparsers(dest="hook_action", required=True)
    for action in ("install", "uninstall"):
        hook_action = hook_actions.add_parser(action)
        hook_action.add_argument(
            "--provider", choices=("codex", "claude"), required=True
        )
        hook_action.add_argument("--dry-run", action="store_true")

    commands.add_parser(
        "doctor",
        help="diagnose provider hooks and local event latency",
    )

    commands.add_parser(
        "tui",
        help="open the optional terminal session picker",
    )

    show = commands.add_parser(
        "show",
        help="show one retained local session and its newest handoffs",
    )
    show.add_argument("session_key")
    show.add_argument(
        "--handoff-limit",
        type=int,
        default=DEFAULT_HANDOFF_LIMIT,
        help=f"newest handoffs to include (1-{MAX_HANDOFF_LIMIT})",
    )
    show.add_argument("--json", action="store_true")

    current = commands.add_parser(
        "current",
        help="show the session bound to this exact inherited tmux pane",
    )
    current.add_argument(
        "--handoff-limit",
        type=int,
        default=DEFAULT_HANDOFF_LIMIT,
        help=f"newest handoffs to include (1-{MAX_HANDOFF_LIMIT})",
    )
    current.add_argument("--json", action="store_true")

    session = commands.add_parser(
        "session",
        help="curate one retained local session",
    )
    session_actions = session.add_subparsers(dest="session_action", required=True)
    for action, help_text in (
        ("name", "set or clear a curated session name"),
        ("purpose", "set or clear an explicit session purpose"),
    ):
        edit = session_actions.add_parser(action, help=help_text)
        edit.add_argument(
            "values",
            nargs="*",
            help="SESSION_KEY VALUE, or VALUE with --current",
        )
        edit.add_argument("--current", action="store_true")
        edit.add_argument("--clear", action="store_true")
        edit.add_argument("--json", action="store_true")
    pin = session_actions.add_parser("pin", help="pin or unpin one session")
    pin.add_argument("session_key", nargs="?")
    pin.add_argument("--current", action="store_true")
    pin.add_argument("--off", action="store_true")
    pin.add_argument("--json", action="store_true")
    for action, help_text in (
        ("handoff", "append one immutable user handoff"),
        ("wrap", "append one immutable user handoff and wrap the session"),
    ):
        handoff = session_actions.add_parser(action, help=help_text)
        handoff.add_argument("session_key", nargs="?")
        handoff.add_argument("--current", action="store_true")
        handoff.add_argument("--json-stdin", action="store_true", required=True)
        handoff.add_argument("--json", action="store_true")

    prepare_open = commands.add_parser(
        "prepare-open",
        help="atomically prepare an existing local session for presentation",
    )
    prepare_open.add_argument("session_key")
    prepare_open.add_argument("--request-id", required=True)
    prepare_open.add_argument("--has-current-terminal", action="store_true")
    prepare_open.add_argument("--current-tmux-client")
    prepare_open.add_argument("--can-focus-desktop", action="store_true")
    prepare_open.add_argument("--can-launch-terminal", action="store_true")
    prepare_open.add_argument("--json", action="store_true", required=True)

    prepare_new = commands.add_parser(
        "prepare-new",
        help="atomically prepare a new local project session for presentation",
    )
    prepare_new.add_argument("--project")
    prepare_new.add_argument("--location")
    prepare_new.add_argument("--provider", choices=("codex", "claude"))
    prepare_new.add_argument(
        "--from",
        dest="source_ref",
        help="exact handoff ID or local source session key",
    )
    prepare_new.add_argument("--request-id", required=True)
    prepare_new.add_argument("--has-current-terminal", action="store_true")
    prepare_new.add_argument("--current-tmux-client")
    prepare_new.add_argument("--can-focus-desktop", action="store_true")
    prepare_new.add_argument("--can-launch-terminal", action="store_true")
    prepare_new.add_argument("--json", action="store_true", required=True)

    prepare_history = commands.add_parser(
        "prepare-history",
        help="atomically prepare the native Claude history picker",
    )
    prepare_history.add_argument("--project", required=True)
    prepare_history.add_argument("--location")
    prepare_history.add_argument("--request-id", required=True)
    prepare_history.add_argument("--has-current-terminal", action="store_true")
    prepare_history.add_argument("--current-tmux-client")
    prepare_history.add_argument("--can-focus-desktop", action="store_true")
    prepare_history.add_argument("--can-launch-terminal", action="store_true")
    prepare_history.add_argument("--json", action="store_true", required=True)

    select = commands.add_parser(
        "select-surface",
        help="switch one revalidated tmux client to a managed surface",
    )
    select.add_argument("surface_id")
    select.add_argument("--client", required=True)

    attach = commands.add_parser(
        "attach-surface",
        help="attach this terminal to a revalidated managed surface",
    )
    attach.add_argument("surface_id")

    stop = commands.add_parser(
        "stop-session",
        help="stop one revalidated launch-owned Claude session",
    )
    stop.add_argument("session_key")
    stop.add_argument("--json", action="store_true", required=True)

    bootstrap = commands.add_parser(
        "bootstrap", help="internal waiting-surface bootstrap"
    )
    bootstrap.add_argument("launch_id")
    return parser


def _configured_codex_executable(config: SwitchboardConfig) -> str | None:
    for provider in config.providers:
        if provider.provider is ProviderId.CODEX:
            return (provider.executable or "codex") if provider.enabled else None
    return None


def _run_tui_command() -> int:
    try:
        tui = importlib.import_module(".tui", __package__)
    except ModuleNotFoundError as error:
        optional_roots = ("rich", "textual")
        if error.name is not None and any(
            error.name == root or error.name.startswith(f"{root}.")
            for root in optional_roots
        ):
            print(
                "swbctl: TUI support is not installed; install it with: "
                "pip install 'agent-switchboard[tui]'",
                file=sys.stderr,
            )
            return 1
        raise
    try:
        return int(tui.run_tui(swbctl_executable=resolve_swbctl_executable()))
    except (ValidationError, TmuxError, OSError, ValueError) as error:
        print(f"swbctl: {_safe_error_message(error)}", file=sys.stderr)
        return 1


def _configured_claude_executable(config: SwitchboardConfig) -> str | None:
    for provider in config.providers:
        if provider.provider is ProviderId.CLAUDE:
            return (provider.executable or "claude") if provider.enabled else None
    return None


def _coordinator(
    registry: Registry,
    *,
    host_id: HostId,
    config: SwitchboardConfig,
) -> LaunchCoordinator:
    return LaunchCoordinator(
        registry,
        host_id=host_id,
        tmux=TmuxController(),
        swbctl_executable=resolve_swbctl_executable(),
        codex_executable=_configured_codex_executable(config),
        claude_executable=_configured_claude_executable(config),
        projects=config.projects,
        locations=config.locations,
        naming_prefix=config.tmux.naming_prefix,
        launch_timeout_seconds=config.tmux.launch_timeout_seconds,
    )


def _prepare_open(arguments: argparse.Namespace) -> str:
    # Full reconciliation is bounded and authoritative for the duplicate-runtime
    # decision. Its JSON result is intentionally discarded; preparation reads the
    # resulting retained transaction directly.
    build_local_snapshot_json(reconcile="full")
    host_id = load_or_create_host_id()
    config = load_config(host_id=host_id)
    context = PresentationContext(
        arguments.has_current_terminal,
        arguments.current_tmux_client,
        arguments.can_focus_desktop,
        arguments.can_launch_terminal,
    )
    with Registry(database_path()) as registry:
        plan = _coordinator(registry, host_id=host_id, config=config).prepare_open(
            arguments.session_key,
            request_id=arguments.request_id,
            context=context,
        )
    return PresentationPlanEnvelope(plan).to_json()


def _prepare_new(arguments: argparse.Namespace) -> str:
    host_id = load_or_create_host_id()
    config = load_config(host_id=host_id)
    context = PresentationContext(
        arguments.has_current_terminal,
        arguments.current_tmux_client,
        arguments.can_focus_desktop,
        arguments.can_launch_terminal,
    )
    with Registry(database_path()) as registry:
        materialize_configured_projects(registry, str(host_id), config)
        reconcile_live(registry, str(host_id))
        plan = _coordinator(registry, host_id=host_id, config=config).prepare_new(
            arguments.project,
            location_id=arguments.location,
            provider=arguments.provider,
            source_ref=arguments.source_ref,
            request_id=arguments.request_id,
            context=context,
        )
    return PresentationPlanEnvelope(plan).to_json()


def _prepare_history(arguments: argparse.Namespace) -> str:
    host_id = load_or_create_host_id()
    config = load_config(host_id=host_id)
    context = PresentationContext(
        arguments.has_current_terminal,
        arguments.current_tmux_client,
        arguments.can_focus_desktop,
        arguments.can_launch_terminal,
    )
    with Registry(database_path()) as registry:
        materialize_configured_projects(registry, str(host_id), config)
        reconcile_live(registry, str(host_id))
        plan = _coordinator(registry, host_id=host_id, config=config).prepare_history(
            arguments.project,
            location_id=arguments.location,
            request_id=arguments.request_id,
            context=context,
        )
    return PresentationPlanEnvelope(plan).to_json()


def _bootstrap(arguments: argparse.Namespace) -> int:
    launch_environment = os.environ.get("AGENT_SWITCHBOARD_LAUNCH_ID")
    surface_environment = os.environ.get("AGENT_SWITCHBOARD_SURFACE_ID")
    if launch_environment != arguments.launch_id or surface_environment is None:
        raise PresentationError("bootstrap environment does not match its launch")
    host_id = load_or_create_host_id()
    config = load_config(host_id=host_id)
    with Registry(database_path()) as registry:
        materialize_configured_projects(registry, str(host_id), config)
        coordinator = _coordinator(registry, host_id=host_id, config=config)
        return coordinator.bootstrap(
            arguments.launch_id,
            expected_surface_id=surface_environment,
            reconcile_runtime=lambda: reconcile_live(registry, str(host_id)),
        )


def _stop_session(arguments: argparse.Namespace) -> str:
    host_id = load_or_create_host_id()
    config = load_config(host_id=host_id)
    with Registry(database_path()) as registry:
        materialize_configured_projects(registry, str(host_id), config)

        def reconcile() -> object:
            return reconcile_live(registry, str(host_id))

        action = ManagedSessionController(
            registry,
            host_id=host_id,
            tmux=TmuxController(),
            reconcile_runtime=reconcile,
        ).stop(arguments.session_key)
    return SessionActionEnvelope(action).to_json()


def _explicit_or_current_target(
    arguments: argparse.Namespace,
    *,
    registry: Registry,
    host_id: HostId,
) -> str:
    explicit = getattr(arguments, "session_key", None)
    current = bool(getattr(arguments, "current", False))
    if current == (explicit is not None):
        raise CurationError("choose exactly one session key or --current")
    if current:
        return str(
            resolve_current_session_key(
                registry,
                host_id=host_id,
            )
        )
    return str(SessionKey.parse(explicit))


def _edit_target_and_value(
    arguments: argparse.Namespace,
    *,
    registry: Registry,
    host_id: HostId,
) -> tuple[str, str | None]:
    values = list(arguments.values)
    if arguments.current:
        expected = 0 if arguments.clear else 1
        if len(values) != expected:
            raise CurationError(
                "--current requires one value, or no value together with --clear"
            )
        target = str(resolve_current_session_key(registry, host_id=host_id))
        return target, None if arguments.clear else values[0]
    expected = 1 if arguments.clear else 2
    if len(values) != expected:
        raise CurationError(
            "an explicit edit requires SESSION_KEY and one value, "
            "or SESSION_KEY with --clear"
        )
    target = str(SessionKey.parse(values[0]))
    return target, None if arguments.clear else values[1]


def _render_detail(arguments: argparse.Namespace, detail: SessionDetailEnvelope) -> str:
    return detail.to_json() if arguments.json else format_session_detail(detail)


def _curation_command(arguments: argparse.Namespace) -> str:
    host_id = load_or_create_host_id()
    with Registry(database_path()) as registry:
        if arguments.command == "show":
            detail = read_session_detail(
                registry,
                host_id=host_id,
                session_key=arguments.session_key,
                handoff_limit=arguments.handoff_limit,
            )
            return _render_detail(arguments, detail)
        if arguments.command == "current":
            key = resolve_current_session_key(registry, host_id=host_id)
            detail = read_session_detail(
                registry,
                host_id=host_id,
                session_key=key,
                handoff_limit=arguments.handoff_limit,
            )
            return _render_detail(arguments, detail)

        if arguments.session_action in {"name", "purpose"}:
            session_key, value = _edit_target_and_value(
                arguments, registry=registry, host_id=host_id
            )
            if arguments.session_action == "name":
                registry.set_session_name(session_key, host_id=str(host_id), name=value)
            else:
                registry.set_session_purpose(
                    session_key, host_id=str(host_id), purpose=value
                )
        else:
            session_key = _explicit_or_current_target(
                arguments, registry=registry, host_id=host_id
            )
            if arguments.session_action == "pin":
                registry.set_session_pinned(
                    session_key,
                    host_id=str(host_id),
                    pinned=not arguments.off,
                )
            else:
                stream = getattr(sys.stdin, "buffer", sys.stdin)
                handoff = read_handoff_input(stream)
                registry.curate_session_handoff(
                    session_key,
                    host_id=str(host_id),
                    summary=handoff.summary,
                    next_action=handoff.next_action,
                    handoff_id=handoff.handoff_id,
                    wrap=arguments.session_action == "wrap",
                )
        detail = read_session_detail(
            registry,
            host_id=host_id,
            session_key=session_key,
        )
        return _render_detail(arguments, detail)


def _surface_action(arguments: argparse.Namespace) -> int:
    host_id = load_or_create_host_id()
    tmux = TmuxController()
    with Registry(database_path()) as registry:
        if arguments.command == "select-surface":
            select_surface(
                registry,
                host_id=host_id,
                surface_id=arguments.surface_id,
                client=arguments.client,
                tmux=tmux,
            )
            return 0
        argv = attach_surface_argv(
            registry,
            host_id=host_id,
            surface_id=arguments.surface_id,
            tmux=tmux,
        )
    os.execvp(argv[0], argv)
    raise PresentationError("tmux attach unexpectedly returned")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the zero-configuration JSON command surface."""

    entry_ns = time.time_ns()
    arguments = build_parser().parse_args(argv)
    if arguments.command == "event":
        try:
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            ingest_local_event(
                arguments.provider,
                stream,
                entry_ns=entry_ns,
            )
        except (
            HookInputError,
            ValidationError,
            StorageError,
            MigrationError,
            sqlite3.Error,
            OSError,
            ValueError,
        ) as error:
            print(f"swbctl: {_safe_error_message(error)}", file=sys.stderr)
            return 1
        return 0

    if arguments.command == "hooks":
        try:
            config = load_config(host_id=_EPHEMERAL_CONFIG_HOST_ID)
            executable = resolve_swbctl_executable()
            editor = (
                edit_codex_hooks if arguments.provider == "codex" else edit_claude_hooks
            )
            result = editor(
                arguments.hook_action,
                executable=executable,
                timeout_seconds=config.hooks.timeout_seconds,
                dry_run=arguments.dry_run,
            )
        except (ValidationError, OSError, ValueError) as error:
            print(f"swbctl: {_safe_error_message(error)}", file=sys.stderr)
            return 1
        qualifier = "would update" if result.dry_run else "updated"
        if not result.changed:
            qualifier = (
                "already current"
                if arguments.hook_action == "install"
                else "not installed"
            )
        provider_name = "Codex" if arguments.provider == "codex" else "Claude"
        print(f"{provider_name} hooks {qualifier}: {result.path}")
        if arguments.hook_action == "install" and arguments.provider == "codex":
            print("Review and trust the Agent Switchboard hooks with Codex /hooks.")
        return 0

    if arguments.command == "doctor":
        try:
            config = load_config(host_id=_EPHEMERAL_CONFIG_HOST_ID)
            result = run_all_doctors(
                config=config,
                swbctl_executable=resolve_swbctl_executable(),
            )
        except (ValidationError, OSError, ValueError) as error:
            print(f"swbctl: {_safe_error_message(error)}", file=sys.stderr)
            return 1
        print(result.render())
        return 0 if result.healthy else 1

    if arguments.command in {"show", "current", "session"}:
        try:
            sys.stdout.write(f"{_curation_command(arguments)}\n")
        except (
            CurationError,
            ValidationError,
            StorageError,
            TmuxError,
            MigrationError,
            sqlite3.Error,
            OSError,
            ValueError,
        ) as error:
            print(f"swbctl: {_safe_error_message(error)}", file=sys.stderr)
            return 1
        return 0

    if arguments.command == "tui":
        return _run_tui_command()

    if arguments.command in {
        "prepare-open",
        "prepare-new",
        "prepare-history",
        "bootstrap",
        "select-surface",
        "attach-surface",
        "stop-session",
    }:
        try:
            if arguments.command == "prepare-open":
                sys.stdout.write(f"{_prepare_open(arguments)}\n")
                return 0
            if arguments.command == "prepare-new":
                sys.stdout.write(f"{_prepare_new(arguments)}\n")
                return 0
            if arguments.command == "prepare-history":
                sys.stdout.write(f"{_prepare_history(arguments)}\n")
                return 0
            if arguments.command == "bootstrap":
                return _bootstrap(arguments)
            if arguments.command == "stop-session":
                sys.stdout.write(f"{_stop_session(arguments)}\n")
                return 0
            return _surface_action(arguments)
        except (
            ValidationError,
            StorageError,
            PresentationError,
            TmuxError,
            MigrationError,
            sqlite3.Error,
            OSError,
            ValueError,
        ) as error:
            print(f"swbctl: {_safe_error_message(error)}", file=sys.stderr)
            return 1

    reconcile = (
        arguments.reconcile
        if arguments.command == "snapshot"
        else ("full" if arguments.refresh else "none")
    )
    try:
        payload = build_local_snapshot_json(reconcile=reconcile)
    except (
        ValidationError,
        StorageError,
        MigrationError,
        sqlite3.Error,
        OSError,
    ) as error:
        print(f"swbctl: {_safe_error_message(error)}", file=sys.stderr)
        return 1
    sys.stdout.write(f"{payload}\n")
    return 0
