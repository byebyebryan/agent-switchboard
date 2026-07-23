#!/usr/bin/env python3
"""Fork the last settled Codex turn while the source's next turn is active."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from typing import Any

from spikes.thread_workstream.codex_app_server import (
    AppServerError,
    CodexAppServer,
    provider_version,
    schema_fingerprint,
)
from spikes.thread_workstream.codex_rollover import (
    _default_tmux_panes,
    _existing_processes_unchanged,
    _selected_agent_processes,
)
from spikes.thread_workstream.evidence import StudyResult, StudyStatus
from spikes.thread_workstream.isolation import IsolationLayout


TURN_TIMEOUT_SECONDS = 120.0


class RunningForkError(RuntimeError):
    """The provider did not preserve the stable fork boundary."""


def _write_codex_home(layout: IsolationLayout, source_home: Path) -> None:
    source_auth = source_home / "auth.json"
    if not source_auth.is_file():
        raise AppServerError("isolated live study has no importable provider login")
    destination_auth = layout.codex_home / "auth.json"
    shutil.copyfile(source_auth, destination_auth)
    destination_auth.chmod(0o600)
    config = layout.codex_home / "config.toml"
    config.write_text(
        "[features]\n"
        "memories = false\n"
        "multi_agent = false\n\n"
        "[tui]\n"
        "animations = false\n"
        "show_tooltips = false\n",
        encoding="utf-8",
    )
    config.chmod(0o600)


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RunningForkError(f"provider returned invalid {name}")
    return value


def _identity(value: Mapping[str, Any], name: str) -> str:
    identity = value.get("id")
    if not isinstance(identity, str) or not identity:
        raise RunningForkError(f"provider returned invalid {name} identity")
    return identity


def _turn_completed(
    server: CodexAppServer,
    *,
    thread_id: str,
    turn_id: str,
) -> dict[str, Any]:
    notification = server.wait_notification(
        "turn/completed",
        predicate=lambda params: (
            params.get("threadId") == thread_id
            and isinstance(params.get("turn"), Mapping)
            and params["turn"].get("id") == turn_id
        ),
        timeout=TURN_TIMEOUT_SECONDS,
    )
    params = _object(notification.get("params"), "turn completion")
    return _object(params.get("turn"), "completed turn")


def _read_turn(thread: Mapping[str, Any], turn_id: str) -> dict[str, Any] | None:
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in turns:
        if isinstance(turn, dict) and turn.get("id") == turn_id:
            return turn
    return None


def _text_input(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text}]


def run_study(
    *,
    codex: str = "codex",
) -> tuple[str, str, StudyStatus, dict[str, bool], dict[str, bool], int]:
    started = time.monotonic()
    preexisting_agents = _selected_agent_processes()
    preexisting_panes = _default_tmux_panes()
    source_home = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).resolve()
    layout = IsolationLayout.create()
    root = layout.root
    assertions: dict[str, bool] = {}
    cleanup: dict[str, bool] = {}
    status = StudyStatus.FALSIFIED
    version = "unavailable"
    fingerprint = "0" * 64
    server: CodexAppServer | None = None
    try:
        _write_codex_home(layout, source_home)
        environment = layout.provider_environment()
        version = provider_version(codex)
        fingerprint = schema_fingerprint(codex, environment)
        server = CodexAppServer(codex, environment)

        started_thread = server.request(
            "thread/start",
            {
                "approvalPolicy": "never",
                "cwd": str(layout.repository),
                "sandbox": "danger-full-access",
            },
        )
        source_thread = _object(started_thread.get("thread"), "source thread")
        source_id = _identity(source_thread, "source thread")

        baseline_started = server.request(
            "turn/start",
            {
                "threadId": source_id,
                "input": _text_input(
                    "Reply with one short confirmation. Do not use tools or edit files."
                ),
            },
        )
        baseline_turn = _object(baseline_started.get("turn"), "baseline turn")
        baseline_id = _identity(baseline_turn, "baseline turn")
        baseline_completed = _turn_completed(
            server,
            thread_id=source_id,
            turn_id=baseline_id,
        )
        assertions["baseline_turn_completed"] = (
            baseline_completed.get("status") == "completed"
        )

        active_started = server.request(
            "turn/start",
            {
                "threadId": source_id,
                "input": _text_input(
                    "Run the shell command sleep 45 exactly once. Do not edit files. "
                    "After it finishes, reply with one short confirmation."
                ),
            },
        )
        active_turn = _object(active_started.get("turn"), "active turn")
        active_id = _identity(active_turn, "active turn")
        command_started = server.wait_notification(
            "item/started",
            predicate=lambda params: (
                params.get("threadId") == source_id
                and params.get("turnId") == active_id
                and isinstance(params.get("item"), Mapping)
                and params["item"].get("type") == "commandExecution"
            ),
            timeout=TURN_TIMEOUT_SECONDS,
        )
        assertions["source_command_observed_in_progress"] = bool(command_started)

        forked = server.request(
            "thread/fork",
            {
                "approvalPolicy": "never",
                "cwd": str(layout.repository),
                "lastTurnId": baseline_id,
                "sandbox": "danger-full-access",
                "threadId": source_id,
            },
        )
        fork_thread = _object(forked.get("thread"), "fork thread")
        fork_id = _identity(fork_thread, "fork thread")
        fork_turns = fork_thread.get("turns")
        assertions["fork_identity_is_distinct"] = fork_id != source_id
        assertions["provider_records_source_lineage"] = (
            fork_thread.get("forkedFromId") == source_id
        )
        assertions["fork_contains_only_settled_prefix"] = (
            isinstance(fork_turns, list)
            and len(fork_turns) == 1
            and isinstance(fork_turns[0], Mapping)
            and fork_turns[0].get("id") == baseline_id
            and fork_turns[0].get("status") == "completed"
        )

        alternative_started = server.request(
            "turn/start",
            {
                "threadId": fork_id,
                "input": _text_input(
                    "Reply with one short alternative confirmation. "
                    "Do not use tools or edit files."
                ),
            },
        )
        alternative_turn = _object(
            alternative_started.get("turn"),
            "alternative turn",
        )
        alternative_id = _identity(alternative_turn, "alternative turn")
        alternative_completed = _turn_completed(
            server,
            thread_id=fork_id,
            turn_id=alternative_id,
        )
        assertions["fork_accepts_immediate_alternative_turn"] = (
            alternative_completed.get("status") == "completed"
        )

        source_during_alternative = server.thread_read(
            source_id,
            include_turns=True,
        )
        still_active = _read_turn(source_during_alternative, active_id)
        assertions["source_turn_remains_active_after_alternative"] = (
            still_active is not None and still_active.get("status") == "inProgress"
        )
        assertions["source_prefix_remains_intact"] = (
            _read_turn(source_during_alternative, baseline_id) is not None
        )

        server.request(
            "turn/interrupt",
            {"threadId": source_id, "turnId": active_id},
        )
        interrupted = _turn_completed(
            server,
            thread_id=source_id,
            turn_id=active_id,
        )
        assertions["source_interrupt_is_explicit_cleanup"] = (
            interrupted.get("status") == "interrupted"
        )
        assertions["disposable_repository_unchanged"] = not subprocess.run(
            ["git", "-C", str(layout.repository), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        status = (
            StudyStatus.PASS
            if assertions and all(assertions.values())
            else StudyStatus.FALSIFIED
        )
    except AppServerError:
        status = StudyStatus.BLOCKED
    except (OSError, RunningForkError, subprocess.SubprocessError, ValueError):
        status = StudyStatus.FALSIFIED
    finally:
        if server is not None:
            with contextlib.suppress(OSError):
                server.close()
        cleanup["private_capture_deleted"] = layout.erase_private_events()
        layout.cleanup()
        cleanup["temporary_root_deleted"] = not root.exists()
        cleanup["unrelated_agent_processes_unchanged"] = _existing_processes_unchanged(
            preexisting_agents
        )
        cleanup["unrelated_tmux_panes_unchanged"] = (
            _default_tmux_panes() == preexisting_panes
        )
    if status is StudyStatus.PASS and not all(cleanup.values()):
        status = StudyStatus.FALSIFIED
    return (
        version,
        fingerprint,
        status,
        assertions,
        cleanup,
        int((time.monotonic() - started) * 1_000),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    version, fingerprint, status, assertions, cleanup, duration = run_study(
        codex=arguments.codex
    )
    result = StudyResult(
        study="codex-running-source-stable-fork",
        provider="codex",
        installed_version=version,
        contract_fingerprint=fingerprint,
        status=status,
        assertions=assertions or {"provider_contract_observed": False},
        event_order=[
            "baseline-turn-completed",
            "source-next-turn-command-started",
            "stable-prefix-fork-created",
            "fork-alternative-turn-completed",
            "source-next-turn-still-running",
            "source-turn-interrupted-for-cleanup",
        ],
        isolation={
            "disposable_repository": True,
            "no_repository_remotes": True,
            "private_provider_home": True,
            "private_switchboard_root": True,
        },
        cleanup=cleanup,
        timings_ms={"total": duration},
        limitations=[
            "filesystem isolation composes with separate managed worktree evidence",
            "source interruption occurred only after concurrent fork proof",
        ],
    )
    result.write(arguments.output)
    print(
        json.dumps(
            {
                "study": result.study,
                "status": status.value,
                "outputWritten": True,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if status is StudyStatus.PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
