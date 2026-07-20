"""Command-line entry point for Switchboard."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .agent_tools import AgentToolError, AgentToolService
from .config import RemoteConfig, SwitchboardConfig, load_config, migrate_legacy_config
from .curation import (
    MAX_HANDOFF_INPUT_BYTES,
    CurationError,
    format_session_detail,
    read_handoff_input,
    read_session_detail,
    resolve_current_session_key,
)
from .doctor import run_all_doctors
from .domain import (
    Checkout,
    HostId,
    PresentationContext,
    ProviderId,
    SessionKey,
    ValidationError,
)
from .executable import resolve_swbctl_executable
from .hook_config import edit_claude_hooks, edit_codex_hooks
from .hooks import HookInputError
from .live import reconcile_live
from .local import build_local_snapshot_json, materialize_configured_projects
from .local_events import ingest_local_event
from .mcp_server import run_mcp_server
from .migrations import MigrationError
from .paths import database_path, load_or_create_host_id
from .presentation import (
    LaunchCoordinator,
    PresentationError,
    attach_surface_argv,
    select_surface,
)
from .protocol import (
    ContinuationEnvelope,
    FleetEnvelope,
    PresentationPlanEnvelope,
    SessionActionEnvelope,
    SessionDetailEnvelope,
    SnapshotEnvelope,
)
from .remote import (
    RemoteError,
    attach_ssh_argv,
    build_fleet_envelope,
    invoke_remote_empty,
    invoke_remote_json,
    refresh_remote_cache,
    resolve_remote_host,
)
from .session_actions import ManagedSessionController
from .storage import DEFAULT_HANDOFF_LIMIT, MAX_HANDOFF_LIMIT, Registry, StorageError
from .tmux import TmuxController, TmuxError

_MAX_ERROR_MESSAGE_LENGTH = 1_024
_MAX_REMOTE_ACTION_INPUT_BYTES = MAX_HANDOFF_INPUT_BYTES
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

    fleet = commands.add_parser(
        "fleet",
        help="emit local and retained remote host snapshots",
    )
    fleet.add_argument(
        "--refresh",
        action="store_true",
        help="fully reconcile local and declared remote hosts before reading",
    )
    fleet.add_argument("--json", action="store_true", required=True)

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

    config = commands.add_parser("config", help="inspect or migrate configuration")
    config_actions = config.add_subparsers(dest="config_action", required=True)
    migrate_v2 = config_actions.add_parser(
        "migrate-v2", help="print a canonical v2 form of one legacy config"
    )
    migrate_v2.add_argument("--input", type=Path, required=True)
    migrate_v2.add_argument(
        "--print", dest="print_output", action="store_true", required=True
    )

    task = commands.add_parser("task", help="manage explicit project tasks")
    task_actions = task.add_subparsers(dest="task_action", required=True)
    task_list = task_actions.add_parser("list", help="list local tasks")
    task_list.add_argument("--project")
    task_list.add_argument("--status", choices=("open", "closed"))
    task_list.add_argument("--json", action="store_true")
    task_show = task_actions.add_parser("show", help="show one task and its history")
    task_show.add_argument("task_id")
    task_show.add_argument("--json", action="store_true")
    task_create = task_actions.add_parser("create", help="create an open task")
    task_create.add_argument("--task-id", required=True)
    task_create.add_argument("--project", required=True)
    task_create.add_argument("--title", required=True)
    task_create.add_argument("--purpose")
    task_create.add_argument("--checkout")
    task_create.add_argument("--provider", choices=("codex", "claude"))
    task_create.add_argument("--json", action="store_true")
    task_adopt = task_actions.add_parser("adopt", help="adopt an Inbox session")
    task_adopt.add_argument("session_key")
    adopt_target = task_adopt.add_mutually_exclusive_group(required=True)
    adopt_target.add_argument("--task")
    adopt_target.add_argument("--task-id")
    task_adopt.add_argument("--title")
    task_adopt.add_argument("--project")
    task_adopt.add_argument("--checkout")
    task_adopt.add_argument("--provider", choices=("codex", "claude"))
    task_adopt.add_argument("--json", action="store_true")
    task_title = task_actions.add_parser("title", help="change a task title")
    task_title.add_argument("task_id")
    task_title.add_argument("value")
    task_title.add_argument("--json", action="store_true")
    task_purpose = task_actions.add_parser("purpose", help="set or clear task purpose")
    task_purpose.add_argument("task_id")
    task_purpose.add_argument("value", nargs="?")
    task_purpose.add_argument("--clear", action="store_true")
    task_purpose.add_argument("--json", action="store_true")
    task_pin = task_actions.add_parser("pin", help="pin or unpin a task")
    task_pin.add_argument("task_id")
    task_pin.add_argument("--off", action="store_true")
    task_pin.add_argument("--json", action="store_true")
    for action, help_text in (
        ("handoff", "append a handoff to the task's current session"),
        ("close", "wrap the current session and close the task"),
    ):
        task_handoff = task_actions.add_parser(action, help=help_text)
        task_handoff.add_argument("task_id")
        task_handoff.add_argument("--json-stdin", action="store_true", required=True)
        task_handoff.add_argument("--json", action="store_true")
    task_export = task_actions.add_parser(
        "export-handoff",
        help="export one exact local task handoff for cross-host continuation",
    )
    task_export.add_argument("task_id")
    task_export.add_argument("--handoff", required=True)
    task_export.add_argument("--json", action="store_true", required=True)
    task_reopen = task_actions.add_parser("reopen", help="reopen a closed task")
    task_reopen.add_argument("task_id")
    task_reopen.add_argument("--json", action="store_true")

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

    agent = commands.add_parser(
        "agent",
        help="use session-scoped context and curation tools",
    )
    agent_actions = agent.add_subparsers(dest="agent_action", required=True)
    for action, help_text in (
        ("current", "show the exactly authorized current session"),
        ("context", "read bounded context for the current project"),
        ("tasks", "list bounded tasks in the current project"),
        ("task", "read the authorized current task"),
    ):
        agent_read = agent_actions.add_parser(action, help=help_text)
        agent_read.add_argument("--json", action="store_true", required=True)
    agent_handoff_read = agent_actions.add_parser(
        "handoff-read", help="read one exact handoff in the current task"
    )
    agent_handoff_read.add_argument("handoff_id")
    agent_handoff_read.add_argument("--json", action="store_true", required=True)
    agent_handoffs = agent_actions.add_parser(
        "handoffs", help="read newest handoffs across the current task"
    )
    agent_handoffs.add_argument("--limit", type=int, default=DEFAULT_HANDOFF_LIMIT)
    agent_handoffs.add_argument("--json", action="store_true", required=True)
    for action, help_text in (
        ("search", "search curated current-project state"),
        ("memory", "search the optional configured memory adapter"),
    ):
        agent_search = agent_actions.add_parser(action, help=help_text)
        agent_search.add_argument("query")
        agent_search.add_argument("--limit", type=int, default=20)
        agent_search.add_argument("--json", action="store_true", required=True)
    agent_update = agent_actions.add_parser(
        "update", help="update the current task title, purpose, or pin"
    )
    agent_update.add_argument("--title")
    agent_update.add_argument("--purpose")
    agent_update.add_argument("--clear-purpose", action="store_true")
    agent_update.add_argument("--pin", choices=("on", "off"))
    agent_update.add_argument("--json", action="store_true", required=True)
    for action, help_text in (
        ("handoff", "append an agent-attributed current-task handoff"),
        ("close", "append a handoff, wrap the session, and close the task"),
    ):
        agent_handoff = agent_actions.add_parser(action, help=help_text)
        agent_handoff.add_argument("--json-stdin", action="store_true", required=True)
        agent_handoff.add_argument("--json", action="store_true", required=True)

    commands.add_parser(
        "agent-mcp", help="serve session-authorized agent tools over stdio MCP"
    )

    prepare_open = commands.add_parser(
        "prepare-open",
        help="atomically prepare an existing session on its owning host",
    )
    prepare_open.add_argument("session_key")
    prepare_open.add_argument("--host")
    prepare_open.add_argument("--request-id", required=True)
    prepare_open.add_argument("--has-current-terminal", action="store_true")
    prepare_open.add_argument("--current-tmux-client")
    prepare_open.add_argument("--can-focus-desktop", action="store_true")
    prepare_open.add_argument("--can-launch-terminal", action="store_true")
    prepare_open.add_argument("--json", action="store_true", required=True)

    prepare_task = commands.add_parser(
        "prepare-task",
        help="atomically open or continue one task",
    )
    prepare_task.add_argument("task_id")
    prepare_task.add_argument("--host")
    prepare_task.add_argument("--create", action="store_true")
    prepare_task_input = prepare_task.add_mutually_exclusive_group()
    prepare_task_input.add_argument("--json-stdin", action="store_true")
    prepare_task_input.add_argument("--continue-json-stdin", action="store_true")
    prepare_task.add_argument("--project")
    prepare_task.add_argument("--title")
    prepare_task.add_argument("--purpose")
    prepare_task.add_argument("--checkout")
    prepare_task.add_argument("--provider", choices=("codex", "claude"))
    prepare_task.add_argument("--request-id", required=True)
    prepare_task.add_argument("--has-current-terminal", action="store_true")
    prepare_task.add_argument("--current-tmux-client")
    prepare_task.add_argument("--can-focus-desktop", action="store_true")
    prepare_task.add_argument("--can-launch-terminal", action="store_true")
    prepare_task.add_argument("--json", action="store_true", required=True)

    prepare_history = commands.add_parser(
        "prepare-history",
        help="atomically prepare the native Claude history picker",
    )
    prepare_history.add_argument("--project", required=True)
    prepare_history.add_argument("--host")
    prepare_history.add_argument("--checkout")
    prepare_history.add_argument("--request-id", required=True)
    prepare_history.add_argument("--has-current-terminal", action="store_true")
    prepare_history.add_argument("--current-tmux-client")
    prepare_history.add_argument("--can-focus-desktop", action="store_true")
    prepare_history.add_argument("--can-launch-terminal", action="store_true")
    prepare_history.add_argument("--json", action="store_true", required=True)

    select = commands.add_parser(
        "select-surface",
        help="switch one revalidated owning-host tmux client to a surface",
    )
    select.add_argument("surface_id")
    select.add_argument("--host")
    select.add_argument("--client", required=True)

    attach = commands.add_parser(
        "attach-surface",
        help="attach this terminal to a revalidated owning-host surface",
    )
    attach.add_argument("surface_id")
    attach.add_argument("--host")

    stop = commands.add_parser(
        "stop-session",
        help="stop one revalidated launch-owned Claude session on its host",
    )
    stop.add_argument("session_key")
    stop.add_argument("--host")
    stop.add_argument("--json", action="store_true", required=True)

    bootstrap = commands.add_parser(
        "bootstrap", help="internal waiting-surface bootstrap"
    )
    bootstrap.add_argument("launch_id")
    return parser


def _fleet_envelope(*, refresh: bool) -> FleetEnvelope:
    host_id = load_or_create_host_id()
    config = load_config(host_id=host_id)
    local_snapshot = SnapshotEnvelope.from_json(
        build_local_snapshot_json(reconcile="full" if refresh else "none")
    )
    generated_at = time.time_ns() // 1_000_000
    with Registry(database_path()) as registry:
        if refresh:
            refresh_remote_cache(
                registry,
                config,
                local_host_id=host_id,
            )
            generated_at = time.time_ns() // 1_000_000
        return build_fleet_envelope(
            local_snapshot,
            registry.list_remotes(declared_only=True),
            generated_at=generated_at,
            staleness_interval_seconds=config.defaults.staleness_interval_seconds,
        )


def _configured_codex_executable(config: SwitchboardConfig) -> str | None:
    for provider in config.providers:
        if provider.provider is ProviderId.CODEX:
            return (provider.executable or "codex") if provider.enabled else None
    return None


def _config_command(arguments: argparse.Namespace) -> str:
    if arguments.config_action != "migrate-v2" or not arguments.print_output:
        raise ValidationError("unsupported configuration action")
    try:
        raw = arguments.input.read_bytes()
    except OSError as error:
        raise ValidationError(
            f"cannot read legacy configuration at {arguments.input}: {error}"
        ) from error
    return migrate_legacy_config(raw, host_id=_EPHEMERAL_CONFIG_HOST_ID)


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
    checkouts: list[Checkout] = []
    for project in registry.list_projects():
        for repository in project["repositories"]:
            for checkout in repository["checkouts"]:
                if checkout["host_id"] != str(host_id):
                    continue
                checkouts.append(
                    Checkout(
                        checkout["checkout_id"],
                        checkout["repository_id"],
                        checkout["host_id"],
                        Path(checkout["path"]),
                        kind=checkout["kind"],
                        display_name=checkout["display_name"],
                        branch=checkout["branch"],
                        head_oid=checkout["head_oid"],
                        provider_override=checkout["provider_override"],
                        transport_override=checkout["transport_override"],
                        is_default=bool(checkout["is_default"]),
                        declared=bool(checkout["declared"]),
                        present=bool(checkout["present"]),
                        git_common_dir=(
                            None
                            if checkout["git_common_dir"] is None
                            else Path(checkout["git_common_dir"])
                        ),
                        git_dir=(
                            None
                            if checkout["git_dir"] is None
                            else Path(checkout["git_dir"])
                        ),
                    )
                )
    return LaunchCoordinator(
        registry,
        host_id=host_id,
        tmux=TmuxController(),
        swbctl_executable=resolve_swbctl_executable(),
        codex_executable=_configured_codex_executable(config),
        claude_executable=_configured_claude_executable(config),
        projects=config.projects,
        project_repositories=config.project_repositories,
        checkouts=checkouts,
        naming_prefix=config.tmux.naming_prefix,
        launch_timeout_seconds=config.tmux.launch_timeout_seconds,
    )


def _action_host(
    arguments: argparse.Namespace,
    local_host_id: HostId,
    *,
    session_key: str | None = None,
) -> HostId:
    explicit = getattr(arguments, "host", None)
    requested = local_host_id if explicit is None else HostId(explicit)
    if session_key is None:
        return requested
    inferred = SessionKey.parse(session_key).host_id
    if explicit is not None and requested != inferred:
        raise ValidationError("requested host disagrees with the session key")
    return inferred


def _presentation_context(arguments: argparse.Namespace) -> PresentationContext:
    return PresentationContext(
        arguments.has_current_terminal,
        arguments.current_tmux_client,
        arguments.can_focus_desktop,
        arguments.can_launch_terminal,
    )


def _remote_context_arguments(arguments: argparse.Namespace) -> tuple[str, ...]:
    values: list[str] = []
    if arguments.has_current_terminal:
        values.append("--has-current-terminal")
    # A tmux client is host-local. It must never be interpreted on the owner as
    # a client on that host; remote terminal callers receive an attach plan.
    if arguments.can_focus_desktop:
        values.append("--can-focus-desktop")
    if arguments.can_launch_terminal:
        values.append("--can-launch-terminal")
    return tuple(values)


def _remote_endpoint(config: SwitchboardConfig, host_id: HostId) -> RemoteConfig:
    with Registry(database_path()) as registry:
        return resolve_remote_host(registry, config, host_id)


def _validate_remote_plan(
    envelope: PresentationPlanEnvelope,
    host_id: HostId,
) -> PresentationPlanEnvelope:
    if envelope.plan.host_id != host_id:
        raise RemoteError(
            "remote_action_host_mismatch",
            "The remote action response belongs to another host.",
            retryable=False,
        )
    return envelope


def _prepare_open(arguments: argparse.Namespace) -> str:
    host_id = load_or_create_host_id()
    config = load_config(host_id=host_id)
    target_host_id = _action_host(
        arguments,
        host_id,
        session_key=arguments.session_key,
    )
    context = _presentation_context(arguments)
    if target_host_id != host_id:
        remote = _remote_endpoint(config, target_host_id)
        envelope = asyncio.run(
            invoke_remote_json(
                remote,
                (
                    "prepare-open",
                    arguments.session_key,
                    "--request-id",
                    arguments.request_id,
                    *_remote_context_arguments(arguments),
                    "--json",
                ),
                PresentationPlanEnvelope.from_json,
            )
        )
        return _validate_remote_plan(envelope, target_host_id).to_json()
    # Full reconciliation is bounded and authoritative for the duplicate-runtime
    # decision. Its JSON result is intentionally discarded; preparation reads the
    # resulting retained transaction directly.
    build_local_snapshot_json(reconcile="full")
    with Registry(database_path()) as registry:
        plan = _coordinator(registry, host_id=host_id, config=config).prepare_open(
            arguments.session_key,
            request_id=arguments.request_id,
            context=context,
        )
    return PresentationPlanEnvelope(plan).to_json()


def _prepare_task_input(
    arguments: argparse.Namespace,
) -> tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    ContinuationEnvelope | None,
]:
    if arguments.continue_json_stdin:
        if (
            not arguments.create
            or arguments.project is not None
            or arguments.title is not None
            or arguments.purpose is not None
        ):
            raise PresentationError(
                "--continue-json-stdin requires --create and supplies project, "
                "title, and purpose"
            )
        if arguments.provider is None:
            raise PresentationError("an imported continuation requires --provider")
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        raw = stream.read(_MAX_REMOTE_ACTION_INPUT_BYTES + 1)
        if len(raw) > _MAX_REMOTE_ACTION_INPUT_BYTES:
            raise PresentationError("continuation input is too large")
        try:
            continuation = ContinuationEnvelope.from_json(raw)
        except ValidationError as error:
            raise PresentationError("continuation input is incompatible") from error
        return (
            str(continuation.source_project_id),
            continuation.task_title,
            continuation.task_purpose,
            arguments.checkout,
            arguments.provider,
            continuation,
        )
    if not arguments.json_stdin:
        return (
            arguments.project,
            arguments.title,
            arguments.purpose,
            arguments.checkout,
            arguments.provider,
            None,
        )
    if not arguments.create or any(
        value is not None
        for value in (
            arguments.project,
            arguments.title,
            arguments.purpose,
            arguments.checkout,
            arguments.provider,
        )
    ):
        raise PresentationError(
            "--json-stdin requires --create and no inline task fields"
        )
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read(_MAX_REMOTE_ACTION_INPUT_BYTES + 1)
    if len(raw) > _MAX_REMOTE_ACTION_INPUT_BYTES:
        raise PresentationError("remote task input is too large")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
        raise PresentationError("remote task input must be a JSON object") from error
    if not isinstance(value, dict) or set(value) != {
        "project",
        "title",
        "purpose",
        "checkout",
        "provider",
    }:
        raise PresentationError("remote task input has incompatible fields")
    for key in ("project", "title", "provider"):
        if not isinstance(value[key], str):
            raise PresentationError(f"remote task {key} must be text")
    for key in ("purpose", "checkout"):
        if value[key] is not None and not isinstance(value[key], str):
            raise PresentationError(f"remote task {key} must be text or null")
    return (
        value["project"],
        value["title"],
        value["purpose"],
        value["checkout"],
        value["provider"],
        None,
    )


def _prepare_task(arguments: argparse.Namespace) -> str:
    host_id = load_or_create_host_id()
    config = load_config(host_id=host_id)
    target_host_id = _action_host(arguments, host_id)
    context = _presentation_context(arguments)
    project, title, purpose, checkout, provider, continuation = _prepare_task_input(
        arguments
    )
    if target_host_id != host_id:
        remote = _remote_endpoint(config, target_host_id)
        remote_arguments = ["prepare-task", arguments.task_id]
        stdin: bytes | None = None
        if continuation is not None:
            remote_arguments.extend(("--create", "--continue-json-stdin"))
            if checkout is not None:
                remote_arguments.extend(("--checkout", checkout))
            assert provider is not None
            remote_arguments.extend(("--provider", provider))
            stdin = continuation.to_json().encode("utf-8")
        elif arguments.create:
            remote_arguments.extend(("--create", "--json-stdin"))
            stdin = json.dumps(
                {
                    "project": project,
                    "title": title,
                    "purpose": purpose,
                    "checkout": checkout,
                    "provider": provider,
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        elif provider is not None:
            remote_arguments.extend(("--provider", provider))
        remote_arguments.extend(("--request-id", arguments.request_id))
        remote_arguments.extend(_remote_context_arguments(arguments))
        remote_arguments.append("--json")
        envelope = asyncio.run(
            invoke_remote_json(
                remote,
                tuple(remote_arguments),
                PresentationPlanEnvelope.from_json,
                stdin=stdin,
            )
        )
        return _validate_remote_plan(envelope, target_host_id).to_json()
    with Registry(database_path()) as registry:
        materialize_configured_projects(registry, str(host_id), config)
        imported_handoff: dict[str, object] | None = None
        if continuation is not None:
            if continuation.source_host_id == host_id:
                raise PresentationError(
                    "an imported continuation must come from another host"
                )
            resolve_remote_host(registry, config, continuation.source_host_id)
            imported_handoff = {
                "source_host_id": str(continuation.source_host_id),
                "source_project_id": str(continuation.source_project_id),
                "source_task_id": str(continuation.source_task_id),
                "source_session_key": str(continuation.source_session_key),
                "handoff_id": str(continuation.handoff_id),
                "sequence": continuation.handoff_sequence,
                "summary": continuation.summary,
                "next_action": continuation.next_action,
                "created_at": continuation.handoff_created_at,
                "content_hash": continuation.content_hash,
            }
        reconcile_live(registry, str(host_id))
        coordinator = _coordinator(registry, host_id=host_id, config=config)
        if arguments.create:
            if not project or not title or not provider:
                raise PresentationError(
                    "--create requires --project, --title, and --provider"
                )
            plan = coordinator.prepare_task_create(
                task_id=arguments.task_id,
                project_id=project,
                title=title,
                purpose=purpose,
                checkout_id=checkout,
                provider=provider,
                request_id=arguments.request_id,
                context=context,
                imported_handoff=imported_handoff,
            )
        else:
            if any(
                value is not None
                for value in (
                    project,
                    title,
                    purpose,
                    checkout,
                )
            ):
                raise PresentationError(
                    "project, title, purpose, and checkout require --create"
                )
            plan = coordinator.prepare_task(
                arguments.task_id,
                provider=provider,
                request_id=arguments.request_id,
                context=context,
            )
    return PresentationPlanEnvelope(plan).to_json()


def _prepare_history(arguments: argparse.Namespace) -> str:
    host_id = load_or_create_host_id()
    config = load_config(host_id=host_id)
    target_host_id = _action_host(arguments, host_id)
    context = _presentation_context(arguments)
    if target_host_id != host_id:
        remote = _remote_endpoint(config, target_host_id)
        remote_arguments = [
            "prepare-history",
            "--project",
            arguments.project,
        ]
        if arguments.checkout is not None:
            remote_arguments.extend(("--checkout", arguments.checkout))
        remote_arguments.extend(("--request-id", arguments.request_id))
        remote_arguments.extend(_remote_context_arguments(arguments))
        remote_arguments.append("--json")
        envelope = asyncio.run(
            invoke_remote_json(
                remote,
                tuple(remote_arguments),
                PresentationPlanEnvelope.from_json,
            )
        )
        return _validate_remote_plan(envelope, target_host_id).to_json()
    with Registry(database_path()) as registry:
        materialize_configured_projects(registry, str(host_id), config)
        reconcile_live(registry, str(host_id))
        plan = _coordinator(registry, host_id=host_id, config=config).prepare_history(
            arguments.project,
            checkout_id=arguments.checkout,
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
    target_host_id = _action_host(
        arguments,
        host_id,
        session_key=arguments.session_key,
    )
    if target_host_id != host_id:
        remote = _remote_endpoint(config, target_host_id)
        envelope = asyncio.run(
            invoke_remote_json(
                remote,
                ("stop-session", arguments.session_key, "--json"),
                SessionActionEnvelope.from_json,
            )
        )
        if (
            envelope.action.host_id != target_host_id
            or str(envelope.action.session_key) != arguments.session_key
        ):
            raise RemoteError(
                "remote_action_target_mismatch",
                "The remote stop response disagrees with the requested session.",
                retryable=False,
            )
        return envelope.to_json()
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


def _task_record(task: dict[str, object]) -> dict[str, object]:
    record: dict[str, object] = {
        "taskId": task["task_id"],
        "hostId": task["host_id"],
        "projectId": task["project_id"],
        "title": task["title"],
        "status": task["status"],
        "pinned": bool(task["pinned"]),
        "createdAt": task["created_at"],
        "updatedAt": task["updated_at"],
    }
    for source, target in (
        ("checkout_id", "checkoutId"),
        ("purpose", "purpose"),
        ("preferred_provider", "preferredProvider"),
        ("current_session_key", "currentSessionKey"),
        ("closed_at", "closedAt"),
    ):
        if task.get(source) is not None:
            record[target] = task[source]
    return record


def _task_result(
    task: dict[str, object], *, sessions: Sequence[dict[str, object]] = ()
) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "protocolVersion": 2,
        "generatedAt": time.time_ns() // 1_000_000,
        "task": _task_record(task),
        "sessions": [
            {
                "sessionKey": session["session_key"],
                "provider": session["provider"],
                "providerSessionId": session["provider_session_id"],
                "name": session.get("name"),
                "wrappedAt": session.get("wrapped_at"),
                "latestHandoffId": session.get("latest_handoff_id"),
                "firstObservedAt": session["first_observed_at"],
                "lastObservedAt": session["last_observed_at"],
            }
            for session in sessions
        ],
    }


def _render_task_payload(payload: dict[str, object], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    task = payload["task"]
    assert isinstance(task, dict)
    lines = [
        f"{task['title']} [{task['status']}]",
        f"task: {task['taskId']}",
        f"project: {task['projectId']}",
    ]
    if task.get("purpose") is not None:
        lines.append(f"purpose: {task['purpose']}")
    if task.get("currentSessionKey") is not None:
        lines.append(f"current: {task['currentSessionKey']}")
    sessions = payload.get("sessions", [])
    assert isinstance(sessions, list)
    if sessions:
        lines.append(f"history: {len(sessions)} session(s)")
    return "\n".join(lines)


def _task_command(arguments: argparse.Namespace) -> str:
    host_id = load_or_create_host_id()
    config = load_config(host_id=host_id)
    with Registry(database_path()) as registry:
        materialize_configured_projects(registry, str(host_id), config)
        action = arguments.task_action
        if action == "list":
            tasks = registry.list_tasks(
                host_id=str(host_id),
                project_id=arguments.project,
                status=arguments.status,
            )
            payload = {
                "schemaVersion": 2,
                "protocolVersion": 2,
                "generatedAt": time.time_ns() // 1_000_000,
                "tasks": [_task_record(task) for task in tasks],
            }
            if arguments.json:
                return json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            return "\n".join(
                f"{task['task_id']}  {task['status']:<6}  {task['title']}"
                for task in tasks
            )
        if action == "show":
            task = registry.get_task(arguments.task_id)
            if task is None or task["host_id"] != str(host_id):
                raise StorageError("unknown local task")
            return _render_task_payload(
                _task_result(
                    task, sessions=registry.list_task_sessions(arguments.task_id)
                ),
                as_json=arguments.json,
            )
        if action == "export-handoff":
            task, session, handoff = registry.export_task_handoff(
                arguments.task_id,
                arguments.handoff,
                host_id=str(host_id),
            )
            return ContinuationEnvelope.from_dict(
                {
                    "schemaVersion": 2,
                    "protocolVersion": 2,
                    "continuationVersion": 1,
                    "generatedAt": time.time_ns() // 1_000_000,
                    "sourceHostId": str(host_id),
                    "sourceProjectId": task["project_id"],
                    "sourceTaskId": task["task_id"],
                    "sourceSessionKey": session["session_key"],
                    "taskTitle": task["title"],
                    "taskPurpose": task["purpose"],
                    "handoffId": handoff["handoff_id"],
                    "handoffSequence": handoff["sequence"],
                    "summary": handoff["summary"],
                    "nextAction": handoff["next_action"],
                    "handoffCreatedAt": handoff["created_at"],
                    "contentHash": handoff["content_hash"],
                }
            ).to_json()
        if action == "create":
            task = registry.create_task(
                task_id=arguments.task_id,
                host_id=str(host_id),
                project_id=arguments.project,
                checkout_id=arguments.checkout,
                title=arguments.title,
                purpose=arguments.purpose,
                preferred_provider=arguments.provider,
            )
        elif action == "adopt":
            session = registry.get_session(arguments.session_key)
            if session is None or session["host_id"] != str(host_id):
                raise StorageError("unknown local session")
            if arguments.task_id is not None:
                if arguments.title is None:
                    raise StorageError("--task-id requires --title")
                project_id = arguments.project or session.get("project_id")
                if project_id is None:
                    raise StorageError("adoption requires an explicit project")
                task = registry.create_task(
                    task_id=arguments.task_id,
                    host_id=str(host_id),
                    project_id=str(project_id),
                    checkout_id=arguments.checkout or session.get("checkout_id"),
                    title=arguments.title,
                    preferred_provider=arguments.provider or session["provider"],
                )
                task_id = str(task["task_id"])
            else:
                if any(
                    value is not None
                    for value in (
                        arguments.title,
                        arguments.project,
                        arguments.checkout,
                        arguments.provider,
                    )
                ):
                    raise StorageError(
                        "title, project, checkout, and provider require --task-id"
                    )
                task_id = arguments.task
            task = registry.adopt_session(
                task_id=task_id, session_key=arguments.session_key
            )
        elif action == "title":
            task = registry.update_task(arguments.task_id, title=arguments.value)
        elif action == "purpose":
            if arguments.clear == (arguments.value is not None):
                raise StorageError("choose one purpose value or --clear")
            task = registry.update_task(
                arguments.task_id, purpose="" if arguments.clear else arguments.value
            )
        elif action == "pin":
            task = registry.update_task(arguments.task_id, pinned=not arguments.off)
        elif action == "reopen":
            task = registry.reopen_task(arguments.task_id, host_id=str(host_id))
        elif action in {"handoff", "close"}:
            task = registry.get_task(arguments.task_id)
            if task is None or task["host_id"] != str(host_id):
                raise StorageError("unknown local task")
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            if action == "handoff" or task.get("current_session_key") is not None:
                handoff = read_handoff_input(stream)
                if action == "handoff":
                    current = task.get("current_session_key")
                    if not isinstance(current, str):
                        raise StorageError("task has no current session")
                    registry.curate_session_handoff(
                        current,
                        host_id=str(host_id),
                        summary=handoff.summary,
                        next_action=handoff.next_action,
                        handoff_id=handoff.handoff_id,
                        wrap=False,
                    )
                    task = registry.get_task(arguments.task_id)
                    assert task is not None
                else:
                    task = registry.close_task(
                        arguments.task_id,
                        host_id=str(host_id),
                        summary=handoff.summary,
                        next_action=handoff.next_action,
                        handoff_id=handoff.handoff_id,
                    )
            else:
                raw = stream.read(MAX_HANDOFF_INPUT_BYTES + 1)
                if len(raw) > MAX_HANDOFF_INPUT_BYTES:
                    raise CurationError("task close input is too large")
                try:
                    empty = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise CurationError(
                        "task close input must be an empty object"
                    ) from error
                if empty != {}:
                    raise CurationError("a never-started task close requires {}")
                task = registry.close_task(arguments.task_id, host_id=str(host_id))
        else:
            raise StorageError("unsupported task action")
        return _render_task_payload(
            _task_result(
                task, sessions=registry.list_task_sessions(str(task["task_id"]))
            ),
            as_json=arguments.json,
        )


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


def _agent_command(arguments: argparse.Namespace) -> str:
    host_id = load_or_create_host_id()
    config = load_config(host_id=host_id)
    with Registry(database_path()) as registry:
        service = AgentToolService(
            registry,
            host_id=host_id,
            config=config,
        )
        if arguments.agent_action == "current":
            return service.current().to_json()
        if arguments.agent_action == "context":
            return service.context().to_json()
        if arguments.agent_action == "tasks":
            return json.dumps(
                service.list_tasks(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        if arguments.agent_action == "task":
            return json.dumps(
                service.task(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        if arguments.agent_action == "handoff-read":
            return service.handoff(arguments.handoff_id).to_json()
        if arguments.agent_action == "handoffs":
            return json.dumps(
                service.list_task_handoffs(limit=arguments.limit),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        if arguments.agent_action == "search":
            return service.search(arguments.query, limit=arguments.limit).to_json()
        if arguments.agent_action == "memory":
            return service.memory_search(
                arguments.query, limit=arguments.limit
            ).to_json()
        if arguments.agent_action == "update":
            if arguments.clear_purpose and arguments.purpose is not None:
                raise AgentToolError("choose a purpose value or --clear-purpose")
            values: dict[str, object] = {}
            if arguments.title is not None:
                values["title"] = arguments.title
            if arguments.purpose is not None or arguments.clear_purpose:
                values["purpose"] = (
                    None if arguments.clear_purpose else arguments.purpose
                )
            if arguments.pin is not None:
                values["pinned"] = arguments.pin == "on"
            return json.dumps(
                service.update_task(values),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        handoff = read_handoff_input(stream)
        return json.dumps(
            service.append_handoff(
                summary=handoff.summary,
                next_action=handoff.next_action,
                handoff_id=handoff.handoff_id,
                close=arguments.agent_action == "close",
            ),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )


def _agent_mcp_command() -> int:
    host_id = load_or_create_host_id()
    config = load_config(host_id=host_id)
    with Registry(database_path()) as registry:
        service = AgentToolService(registry, host_id=host_id, config=config)
        return run_mcp_server(
            service,
            getattr(sys.stdin, "buffer", sys.stdin),
            getattr(sys.stdout, "buffer", sys.stdout),
        )


def _surface_action(arguments: argparse.Namespace) -> int:
    host_id = load_or_create_host_id()
    target_host_id = _action_host(arguments, host_id)
    if target_host_id != host_id:
        config = load_config(host_id=host_id)
        remote = _remote_endpoint(config, target_host_id)
        if arguments.command == "select-surface":
            asyncio.run(
                invoke_remote_empty(
                    remote,
                    (
                        "select-surface",
                        arguments.surface_id,
                        "--client",
                        arguments.client,
                    ),
                )
            )
            return 0
        argv = attach_ssh_argv(remote, arguments.surface_id)
        os.execvp(argv[0], argv)
        raise PresentationError("remote SSH attach unexpectedly returned")
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
    if arguments.command == "config":
        try:
            sys.stdout.write(f"{_config_command(arguments)}\n")
        except (ValidationError, OSError, ValueError) as error:
            print(f"swbctl: {_safe_error_message(error)}", file=sys.stderr)
            return 1
        return 0
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

    if arguments.command == "task":
        try:
            sys.stdout.write(f"{_task_command(arguments)}\n")
        except (
            CurationError,
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

    if arguments.command == "agent":
        try:
            sys.stdout.write(f"{_agent_command(arguments)}\n")
        except (
            AgentToolError,
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

    if arguments.command == "agent-mcp":
        try:
            return _agent_mcp_command()
        except (
            AgentToolError,
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

    if arguments.command == "tui":
        return _run_tui_command()

    if arguments.command == "fleet":
        try:
            sys.stdout.write(
                f"{_fleet_envelope(refresh=arguments.refresh).to_json()}\n"
            )
            return 0
        except (
            ValidationError,
            StorageError,
            RemoteError,
            MigrationError,
            sqlite3.Error,
            OSError,
            ValueError,
        ) as error:
            print(f"swbctl: {_safe_error_message(error)}", file=sys.stderr)
            return 1

    if arguments.command in {
        "prepare-open",
        "prepare-task",
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
            if arguments.command == "prepare-task":
                sys.stdout.write(f"{_prepare_task(arguments)}\n")
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
            RemoteError,
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
