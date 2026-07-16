"""Command-line entry point for Switchboard."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Sequence

from . import __version__
from .domain import ValidationError
from .hooks import HookInputError
from .local import build_local_snapshot_json
from .local_events import ingest_local_event
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
    return parser


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
