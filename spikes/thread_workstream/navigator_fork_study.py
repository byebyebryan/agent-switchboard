#!/usr/bin/env python3
"""Fork a settled Codex prefix beside an actively working isolated TUI."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time
from collections.abc import Mapping, Sequence
from typing import Any
import uuid

from spikes.thread_workstream.codex_app_server import (
    AppServerError,
    CodexAppServer,
    provider_version,
    schema_fingerprint,
)
from spikes.thread_workstream.codex_rollover import (
    EVENT_TIMEOUT_SECONDS,
    PrivateTmuxTui,
    _default_tmux_panes,
    _existing_processes_unchanged,
    _read_events,
    _run,
    _selected_agent_processes,
    _wait_for,
    _write_minimal_codex_home,
)
from spikes.thread_workstream.evidence import StudyResult, StudyStatus
from spikes.thread_workstream.isolation import IsolationLayout


TURN_TIMEOUT_SECONDS = 120.0
UI_TIMEOUT_SECONDS = 30.0


class NavigatorForkError(RuntimeError):
    """The live TUI fork boundary was ambiguous or disturbed."""


def _stage(name: str) -> None:
    print(json.dumps({"stage": name}, separators=(",", ":")), flush=True)


def _composer_idle(tui: PrivateTmuxTui) -> bool:
    view = tui.capture_view()
    return (
        "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}" in view
        and "esc to interrupt" not in view.lower()
    )


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise NavigatorForkError(f"provider returned invalid {name}")
    return value


def _identity(value: Mapping[str, Any], name: str) -> str:
    identity = value.get("id")
    if not isinstance(identity, str) or not identity:
        raise NavigatorForkError(f"provider returned invalid {name} identity")
    return identity


def _latest_completed_turn(thread: Mapping[str, Any]) -> dict[str, Any]:
    turns = thread.get("turns")
    if not isinstance(turns, list):
        raise NavigatorForkError("provider thread has no readable turns")
    completed = [
        turn
        for turn in turns
        if isinstance(turn, dict) and turn.get("status") == "completed"
    ]
    if not completed:
        raise NavigatorForkError("provider thread has no completed fork boundary")
    return completed[-1]


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


def _text_input(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text}]


def _process_parent(process: Path) -> int | None:
    try:
        status = (process / "status").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    for line in status.splitlines():
        if line.startswith("PPid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _descendant_command_running(parent: int, command: str) -> bool:
    processes: dict[int, tuple[int, str]] = {}
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        process_parent = _process_parent(process)
        if process_parent is None:
            continue
        try:
            name = (process / "comm").read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            continue
        processes[int(process.name)] = (process_parent, name)
    descendants = {parent}
    changed = True
    while changed:
        changed = False
        for process_id, (process_parent, _name) in processes.items():
            if process_parent in descendants and process_id not in descendants:
                descendants.add(process_id)
                changed = True
    return any(
        process_id in descendants and name == command
        for process_id, (_process_parent_id, name) in processes.items()
    )


def run_study(
    *,
    codex: str = "codex",
) -> tuple[
    str,
    str,
    StudyStatus,
    dict[str, bool],
    dict[str, bool],
    dict[str, bool],
    int,
]:
    started = time.monotonic()
    preexisting_agents = _selected_agent_processes()
    preexisting_panes = _default_tmux_panes()
    source_home = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).resolve()
    layout = IsolationLayout.create()
    root = layout.root
    tui: PrivateTmuxTui | None = None
    server: CodexAppServer | None = None
    private_socket_path: Path | None = None
    assertions: dict[str, bool] = {}
    isolation = {
        "disposable_repository": True,
        "no_repository_remotes": True,
        "private_provider_home": True,
        "private_switchboard_root": True,
        "private_tmux_server": True,
    }
    cleanup: dict[str, bool] = {}
    status = StudyStatus.FALSIFIED
    version = "unavailable"
    fingerprint = "0" * 64
    try:
        hook_script = Path(__file__).with_name("hook_recorder.py").resolve()
        _write_minimal_codex_home(
            layout,
            source_home=source_home,
            hook_script=hook_script,
        )
        environment = layout.provider_environment()
        environment["ASB_SPIKE_LAUNCH_TOKEN"] = uuid.uuid4().hex
        environment["ASB_SPIKE_SURFACE_TOKEN"] = uuid.uuid4().hex
        version = provider_version(codex)
        fingerprint = schema_fingerprint(codex, environment)
        tui = PrivateTmuxTui.launch(
            layout,
            codex=codex,
            environment=environment,
        )
        _stage("tui-started")
        socket_result = _run(
            layout.tmux_socket,
            "display-message",
            "-p",
            "#{socket_path}",
        )
        private_socket_path = Path(socket_result.stdout.strip())

        if not _wait_for(lambda: _composer_idle(tui), UI_TIMEOUT_SECONDS):
            raise NavigatorForkError("source TUI composer did not become idle")
        time.sleep(2.0)

        tui.paste_and_enter(
            "Reply with one short confirmation. Do not use tools or edit files."
        )
        _stage("baseline-submitted")
        if not _wait_for(
            lambda: (
                sum(
                    event.get("event") == "UserPromptSubmit"
                    for event in _read_events(layout.private_events)
                )
                == 1
            ),
            EVENT_TIMEOUT_SECONDS,
        ):
            raise NavigatorForkError("baseline prompt identity was not observed")
        prompt_events = [
            event
            for event in _read_events(layout.private_events)
            if event.get("event") == "UserPromptSubmit"
            and isinstance(event.get("provider_identity"), str)
        ]
        if len(prompt_events) != 1:
            raise NavigatorForkError("source provider identity is ambiguous")
        source_id = prompt_events[0]["provider_identity"]
        if not _wait_for(
            lambda: any(
                event.get("provider_identity") == source_id
                and event.get("event") == "Stop"
                for event in _read_events(layout.private_events)
            ),
            EVENT_TIMEOUT_SECONDS,
        ):
            raise NavigatorForkError("baseline source turn did not complete")
        _stage("baseline-completed")

        server = CodexAppServer(codex, environment)
        source_before = server.thread_read(source_id, include_turns=True)
        baseline_turn = _latest_completed_turn(source_before)
        baseline_id = _identity(baseline_turn, "baseline turn")
        baseline_stop_count = sum(
            event.get("provider_identity") == source_id and event.get("event") == "Stop"
            for event in _read_events(layout.private_events)
        )
        if not _wait_for(lambda: _composer_idle(tui), UI_TIMEOUT_SECONDS):
            raise NavigatorForkError("source TUI did not return to its composer")
        time.sleep(2.0)

        tui.paste_and_enter(
            "Run the shell command sleep 45 exactly once. Do not edit files. "
            "After it finishes, reply with one short confirmation."
        )
        _stage("source-active-turn-submitted")
        if not _wait_for(
            lambda: _descendant_command_running(tui.provider_pid, "sleep"),
            EVENT_TIMEOUT_SECONDS,
        ):
            raise NavigatorForkError("source TUI command did not become active")
        _stage("source-command-active")
        facts_before_fork = tui.current_facts()

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
        _stage("stable-prefix-fork-created")

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
        _stage("alternative-completed")

        facts_after_fork = tui.current_facts()
        current_events = _read_events(layout.private_events)
        assertions.update(
            {
                "fork_identity_is_distinct": fork_id != source_id,
                "provider_records_source_lineage": (
                    fork_thread.get("forkedFromId") == source_id
                ),
                "fork_contains_only_settled_prefix": (
                    isinstance(fork_turns, list)
                    and len(fork_turns) == 1
                    and isinstance(fork_turns[0], Mapping)
                    and fork_turns[0].get("id") == baseline_id
                    and fork_turns[0].get("status") == "completed"
                ),
                "fork_alternative_completed": (
                    alternative_completed.get("status") == "completed"
                ),
                "source_tui_command_still_running": _descendant_command_running(
                    tui.provider_pid,
                    "sleep",
                ),
                "source_managed_surface_stable": (
                    facts_after_fork == facts_before_fork
                ),
                "source_tui_has_no_completion_after_fork": (
                    sum(
                        event.get("provider_identity") == source_id
                        and event.get("event") == "Stop"
                        for event in current_events
                    )
                    == baseline_stop_count
                ),
                "disposable_repository_unchanged": not subprocess.run(
                    ["git", "-C", str(layout.repository), "status", "--porcelain"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout,
            }
        )
        status = (
            StudyStatus.PASS
            if assertions and all(assertions.values())
            else StudyStatus.FALSIFIED
        )
    except AppServerError:
        status = StudyStatus.BLOCKED
    except (OSError, NavigatorForkError, subprocess.SubprocessError, ValueError):
        status = StudyStatus.FALSIFIED
    finally:
        if server is not None:
            with contextlib.suppress(OSError):
                server.close()
        if tui is not None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                tui.stop()
        _run(layout.tmux_socket, "kill-server", check=False)
        cleanup["private_tmux_server_stopped"] = (
            _run(layout.tmux_socket, "has-session", check=False).returncode != 0
        )
        if (
            private_socket_path is not None
            and private_socket_path.name == layout.tmux_socket
            and private_socket_path.exists()
            and stat.S_ISSOCK(private_socket_path.stat().st_mode)
        ):
            private_socket_path.unlink()
        cleanup["private_tmux_endpoint_deleted"] = (
            private_socket_path is None or not private_socket_path.exists()
        )
        cleanup["private_capture_deleted"] = layout.erase_private_events()
        layout.cleanup()
        cleanup["temporary_root_deleted"] = not root.exists()
        cleanup["unrelated_agent_processes_unchanged"] = _existing_processes_unchanged(
            preexisting_agents
        )
        cleanup["unrelated_tmux_panes_unchanged"] = (
            _default_tmux_panes() == preexisting_panes
        )
    if status is StudyStatus.PASS and (
        not all(isolation.values()) or not all(cleanup.values())
    ):
        status = StudyStatus.FALSIFIED
    return (
        version,
        fingerprint,
        status,
        assertions,
        isolation,
        cleanup,
        int((time.monotonic() - started) * 1_000),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    (
        version,
        fingerprint,
        status,
        assertions,
        isolation,
        cleanup,
        duration,
    ) = run_study(codex=arguments.codex)
    result = StudyResult(
        study="codex-navigator-fork-beside-running-tui",
        provider="codex",
        installed_version=version,
        contract_fingerprint=fingerprint,
        status=status,
        assertions=assertions or {"provider_contract_observed": False},
        event_order=[
            "source-baseline-completed",
            "source-tui-command-started",
            "navigator-stable-prefix-fork-created",
            "fork-alternative-completed",
            "source-tui-still-running",
            "source-tui-stopped-for-cleanup",
        ],
        isolation=isolation,
        cleanup=cleanup,
        timings_ms={"total": duration},
        limitations=[
            "filesystem isolation composes with separate managed worktree evidence",
            "source TUI stopped only after concurrent fork proof",
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
