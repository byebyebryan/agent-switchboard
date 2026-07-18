"""Command-line entry point for Switchboard."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections.abc import Sequence

from . import __version__
from .config import SwitchboardConfig, load_config
from .doctor import run_all_doctors
from .domain import HostId, PresentationContext, ProviderId, ValidationError
from .hook_config import edit_claude_hooks, edit_codex_hooks, resolve_swbctl_executable
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
from .protocol import PresentationPlanEnvelope
from .storage import Registry, StorageError
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
    prepare_new.add_argument("--project", required=True)
    prepare_new.add_argument("--location")
    prepare_new.add_argument("--provider", choices=("codex",))
    prepare_new.add_argument("--request-id", required=True)
    prepare_new.add_argument("--has-current-terminal", action="store_true")
    prepare_new.add_argument("--current-tmux-client")
    prepare_new.add_argument("--can-focus-desktop", action="store_true")
    prepare_new.add_argument("--can-launch-terminal", action="store_true")
    prepare_new.add_argument("--json", action="store_true", required=True)

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

    if arguments.command in {
        "prepare-open",
        "prepare-new",
        "bootstrap",
        "select-surface",
        "attach-surface",
    }:
        try:
            if arguments.command == "prepare-open":
                sys.stdout.write(f"{_prepare_open(arguments)}\n")
                return 0
            if arguments.command == "prepare-new":
                sys.stdout.write(f"{_prepare_new(arguments)}\n")
                return 0
            if arguments.command == "bootstrap":
                return _bootstrap(arguments)
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
