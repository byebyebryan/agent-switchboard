"""Command-line entry point for Switchboard."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Sequence

from . import __version__
from .config import ConfigError, HooksConfig, load_config
from .doctor import run_doctor
from .domain import HostId, ProviderId, ValidationError
from .hook_config import edit_codex_hooks, resolve_swbctl_executable
from .hooks import HookInputError
from .local import build_local_snapshot_json
from .local_events import ingest_local_event
from .migrations import MigrationError
from .storage import StorageError

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
        help="refresh Codex before reading",
    )
    list_command.add_argument("--json", action="store_true", required=True)

    event = commands.add_parser(
        "event",
        help="ingest one provider lifecycle event from standard input",
    )
    event.add_argument("--provider", choices=("codex",), required=True)

    hooks = commands.add_parser(
        "hooks",
        help="manage explicit provider lifecycle hooks",
    )
    hook_actions = hooks.add_subparsers(dest="hook_action", required=True)
    for action in ("install", "uninstall"):
        hook_action = hook_actions.add_parser(action)
        hook_action.add_argument("--provider", choices=("codex",), required=True)
        hook_action.add_argument("--dry-run", action="store_true")

    commands.add_parser(
        "doctor",
        help="diagnose provider hooks and local event latency",
    )
    return parser


def _codex_executable() -> tuple[str, HooksConfig]:
    config = load_config(host_id=_EPHEMERAL_CONFIG_HOST_ID)
    for provider in config.providers:
        if provider.provider is ProviderId.CODEX:
            if not provider.enabled:
                raise ConfigError("providers.codex is disabled")
            return provider.executable or "codex", config.hooks
    raise ConfigError("providers.codex is unavailable")


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
            result = edit_codex_hooks(
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
        print(f"Codex hooks {qualifier}: {result.path}")
        if arguments.hook_action == "install":
            print("Review and trust the Agent Switchboard hooks with Codex /hooks.")
        return 0

    if arguments.command == "doctor":
        try:
            codex_executable, hooks_config = _codex_executable()
            result = run_doctor(
                codex_executable=codex_executable,
                swbctl_executable=resolve_swbctl_executable(),
                hooks=hooks_config,
            )
        except (ValidationError, OSError, ValueError) as error:
            print(f"swbctl: {_safe_error_message(error)}", file=sys.stderr)
            return 1
        print(result.render())
        return 0 if result.healthy else 1

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
