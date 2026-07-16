"""Command-line entry point for Switchboard."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence

from . import __version__
from .domain import ValidationError
from .local import build_local_snapshot_json
from .migrations import MigrationError
from .storage import StorageError

_MAX_ERROR_MESSAGE_LENGTH = 1_024


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
        choices=("none", "full"),
        default="none",
        help="refresh Codex before reading (default: none)",
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the zero-configuration JSON command surface."""

    arguments = build_parser().parse_args(argv)
    refresh = (
        arguments.reconcile == "full"
        if arguments.command == "snapshot"
        else arguments.refresh
    )
    try:
        payload = build_local_snapshot_json(refresh=refresh)
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
