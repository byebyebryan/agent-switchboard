#!/usr/bin/env python3
"""Drive two native Codex Plan-mode rollovers on an isolated TUI surface."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spikes.thread_workstream.codex_app_server import (
    AppServerError,
    CodexAppServer,
    latest_plan,
    provider_version,
    schema_fingerprint,
)
from spikes.thread_workstream.evidence import (
    StudyResult,
    StudyStatus,
    assert_private_file,
    sanitize_hook_order,
)
from spikes.thread_workstream.isolation import IsolationLayout, reject_repository


COMMAND_TIMEOUT_SECONDS = 10.0
EVENT_TIMEOUT_SECONDS = 300.0
UI_TIMEOUT_SECONDS = 30.0
QUIET_SECONDS = 2.0
PLAN_TRANSFER_PREFIX = (
    "A previous agent produced the plan below to accomplish the user's task. "
    "Implement the plan in a fresh context. Treat the plan as the source of "
    "user intent, re-read files as needed, and carry the work through "
    "implementation and verification."
)
PLAN_REQUESTS = (
    "Create the final plan for a harmless disposable-repository task. "
    "The complete task is to read README.md and make no file changes. "
    "Do not ask questions. Finish with the approved implementation plan now.",
    "Create the final plan for a second harmless disposable-repository task. "
    "The complete task is to inspect Git status and make no file changes. "
    "Do not ask questions. Finish with the approved implementation plan now.",
)


class LiveStudyError(RuntimeError):
    """A core provider invariant was falsified or became ambiguous."""


def _run(
    socket: str,
    *arguments: str,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    return subprocess.run(
        ["tmux", "-L", socket, *arguments],
        check=check,
        capture_output=True,
        text=True,
        input=input_text,
        timeout=COMMAND_TIMEOUT_SECONDS,
        env=environment,
    )


def _wait_for(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _process_birth(pid: int) -> int:
    raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    close = raw.rfind(")")
    return int(raw[close + 2 :].split()[19])


def _selected_agent_processes() -> dict[tuple[int, int], str]:
    result: dict[tuple[int, int], str] = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            command = (proc / "comm").read_text(encoding="utf-8").strip()
            if command not in {"codex", "claude"}:
                continue
            pid = int(proc.name)
            result[(pid, _process_birth(pid))] = command
        except (FileNotFoundError, OSError, ValueError):
            continue
    return result


def _default_tmux_panes() -> set[tuple[str, str]]:
    environment = dict(os.environ)
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        env=environment,
    )
    if result.returncode != 0:
        return set()
    return {
        tuple(line.split("\t", 1))
        for line in result.stdout.splitlines()
        if "\t" in line
    }


def _existing_processes_unchanged(
    before: Mapping[tuple[int, int], str],
) -> bool:
    after = _selected_agent_processes()
    return all(after.get(identity) == command for identity, command in before.items())


def _write_minimal_codex_home(
    layout: IsolationLayout,
    *,
    source_home: Path,
    hook_script: Path,
) -> None:
    source_auth = source_home / "auth.json"
    if not source_auth.is_file():
        raise AppServerError("isolated live study has no importable provider login")
    destination_auth = layout.codex_home / "auth.json"
    shutil.copyfile(source_auth, destination_auth)
    destination_auth.chmod(0o600)
    config = layout.codex_home / "config.toml"
    config.write_text(
        "[features]\n"
        "hooks = true\n"
        "memories = false\n"
        "multi_agent = false\n\n"
        "[tui]\n"
        "animations = false\n"
        "show_tooltips = false\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    command = shlex.join(
        (sys.executable, str(hook_script), str(layout.private_events))
    )
    handler = {
        "type": "command",
        "command": command,
        "timeout": 10,
        "statusMessage": "Switchboard isolated rollover evidence",
    }
    hooks = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "^(startup|resume|clear)$",
                    "hooks": [handler],
                }
            ],
            "UserPromptSubmit": [{"hooks": [handler]}],
            "Stop": [{"hooks": [handler]}],
        }
    }
    hooks_path = layout.codex_home / "hooks.json"
    hooks_path.write_text(
        json.dumps(hooks, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    hooks_path.chmod(0o600)


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    assert_private_file(path)
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise LiveStudyError("private hook capture contains a non-object")
        events.append(value)
    return events


@dataclass(slots=True)
class PrivateTmuxTui:
    socket: str
    pane: str
    provider_pid: int
    provider_birth: int
    disposable_trust_accepted: bool

    @classmethod
    def launch(
        cls,
        layout: IsolationLayout,
        *,
        codex: str,
        environment: Mapping[str, str],
    ) -> PrivateTmuxTui:
        layout.validate()
        reject_repository(
            layout.repository,
            expected_root=layout.root,
            expected_token=layout.marker_token,
        )
        executable = shutil.which(codex) if "/" not in codex else codex
        if not executable:
            raise AppServerError("Codex executable is unavailable")
        command = shlex.join(
            (
                "exec",
                executable,
                "--dangerously-bypass-hook-trust",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C",
                str(layout.repository),
            )
        )
        _run(
            layout.tmux_socket,
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-x",
            "140",
            "-y",
            "45",
            "-s",
            "rollover",
            "-n",
            "provider",
            "/bin/sh",
        )
        for key, value in environment.items():
            if key in {
                "CODEX_HOME",
                "SWB_V3_CONFIG_ROOT",
                "SWB_V3_STATE_ROOT",
                "SWB_V3_TMUX_SOCKET",
                "ASB_SPIKE_DISPOSABLE_ROOT",
                "ASB_SPIKE_LAUNCH_TOKEN",
                "ASB_SPIKE_SURFACE_TOKEN",
            }:
                _run(layout.tmux_socket, "set-environment", "-g", key, value)
        pane = _run(
            layout.tmux_socket,
            "display-message",
            "-p",
            "-t",
            "rollover:provider",
            "#{pane_id}",
        ).stdout.strip()
        _run(
            layout.tmux_socket,
            "respawn-pane",
            "-k",
            "-t",
            pane,
            command,
        )
        if not _wait_for(
            lambda: _run(
                layout.tmux_socket,
                "display-message",
                "-p",
                "-t",
                pane,
                "#{pane_current_command}",
                check=False,
            ).stdout.strip()
            == "codex",
            UI_TIMEOUT_SECONDS,
        ):
            raise LiveStudyError("Codex TUI did not start on the private pane")
        provider_pid = int(
            _run(
                layout.tmux_socket,
                "display-message",
                "-p",
                "-t",
                pane,
                "#{pane_pid}",
            ).stdout.strip()
        )
        tui = cls(
            layout.tmux_socket,
            pane,
            provider_pid,
            _process_birth(provider_pid),
            False,
        )
        trust_prompt = "Do you trust the contents of this directory?"
        if _wait_for(
            lambda: trust_prompt in tui.capture_view()
            or "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"
            in tui.capture_view(),
            UI_TIMEOUT_SECONDS,
        ) and trust_prompt in tui.capture_view():
            layout.validate()
            reject_repository(
                layout.repository,
                expected_root=layout.root,
                expected_token=layout.marker_token,
            )
            tui.key("Enter")
            if not _wait_for(
                lambda: trust_prompt not in tui.capture_view(),
                UI_TIMEOUT_SECONDS,
            ):
                raise LiveStudyError("disposable directory trust did not settle")
            tui.disposable_trust_accepted = True
        return tui

    def capture(self) -> str:
        return _run(
            self.socket,
            "capture-pane",
            "-p",
            "-e",
            "-S",
            "-200",
            "-t",
            self.pane,
        ).stdout

    def capture_plain(self) -> str:
        return _run(
            self.socket,
            "capture-pane",
            "-p",
            "-S",
            "-200",
            "-t",
            self.pane,
        ).stdout

    def capture_view(self) -> str:
        return _run(
            self.socket,
            "capture-pane",
            "-p",
            "-t",
            self.pane,
        ).stdout

    def paste_and_enter(self, text: str) -> None:
        buffer_name = "spike-" + uuid.uuid4().hex[:12]
        _run(
            self.socket,
            "load-buffer",
            "-b",
            buffer_name,
            "-",
            input_text=text,
        )
        _run(
            self.socket,
            "paste-buffer",
            "-d",
            "-p",
            "-b",
            buffer_name,
            "-t",
            self.pane,
        )
        _run(self.socket, "send-keys", "-t", self.pane, "Enter")

    def key(self, *keys: str) -> None:
        _run(self.socket, "send-keys", "-t", self.pane, *keys)

    def current_facts(self) -> tuple[int, int, str, str]:
        value = _run(
            self.socket,
            "display-message",
            "-p",
            "-t",
            self.pane,
            "#{pane_pid}\t#{pane_current_path}\t#{pane_id}",
        ).stdout.strip()
        raw_pid, cwd, pane = value.split("\t")
        pid = int(raw_pid)
        return pid, _process_birth(pid), cwd, pane

    def stable_tip(self) -> bool:
        first = self.capture()
        time.sleep(QUIET_SECONDS)
        return first == self.capture()

    def stop(self) -> None:
        socket_result = _run(
            self.socket,
            "display-message",
            "-p",
            "#{socket_path}",
            check=False,
        )
        socket_path = (
            Path(socket_result.stdout.strip())
            if socket_result.returncode == 0 and socket_result.stdout.strip()
            else None
        )
        self.key("C-c")
        if not _wait_for(
            lambda: _run(
                self.socket,
                "has-session",
                "-t",
                "rollover",
                check=False,
            ).returncode
            != 0,
            3.0,
        ):
            _run(self.socket, "kill-server", check=False)
        if (
            socket_path is not None
            and socket_path.name == self.socket
            and socket_path.exists()
            and stat.S_ISSOCK(socket_path.stat().st_mode)
        ):
            socket_path.unlink()


def _event_count(
    events: Sequence[Mapping[str, Any]],
    *,
    provider_identity: str | None = None,
    kind: str | None = None,
    source: str | None = None,
) -> int:
    return sum(
        1
        for event in events
        if (provider_identity is None or event.get("provider_identity") == provider_identity)
        and (kind is None or event.get("event") == kind)
        and (source is None or event.get("source") == source)
    )


def _wait_event(
    path: Path,
    predicate: Callable[[Sequence[Mapping[str, Any]]], bool],
    *,
    timeout: float = EVENT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    if not _wait_for(lambda: predicate(_read_events(path)), timeout):
        raise LiveStudyError("expected provider hook event did not arrive")
    return _read_events(path)


def _find_new_clear_identity(
    events: Sequence[Mapping[str, Any]],
    known: set[str],
) -> str | None:
    for event in events:
        identity = event.get("provider_identity")
        if (
            event.get("event") == "SessionStart"
            and event.get("source") == "clear"
            and isinstance(identity, str)
            and identity not in known
        ):
            return identity
    return None


def _structured_plan_matches(
    app_server: CodexAppServer,
    source_identity: str,
    destination_prompt: object,
) -> bool:
    if not isinstance(destination_prompt, str):
        return False
    source = app_server.thread_read(source_identity, include_turns=True)
    plan = latest_plan(source)
    return (
        isinstance(plan, str)
        and destination_prompt.startswith(PLAN_TRANSFER_PREFIX + "\n\n")
        and destination_prompt.removeprefix(PLAN_TRANSFER_PREFIX + "\n\n") == plan
    )


@dataclass(slots=True)
class LiveObservations:
    assertions: dict[str, bool] = field(default_factory=dict)
    isolation: dict[str, bool] = field(default_factory=dict)
    cleanup: dict[str, bool] = field(default_factory=dict)
    timings_ms: dict[str, int] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


def run_live_study(
    *,
    codex: str,
    credential_home: Path,
    keep_private_events: bool,
) -> tuple[str, str, StudyStatus, LiveObservations]:
    started = time.monotonic()
    version = provider_version(codex)
    layout = IsolationLayout.create(keep_private_events=keep_private_events)
    observations = LiveObservations()
    tui: PrivateTmuxTui | None = None
    private_socket_path: Path | None = None
    status = StudyStatus.FALSIFIED
    preexisting_agents = _selected_agent_processes()
    preexisting_panes = _default_tmux_panes()
    hook_script = Path(__file__).with_name("hook_recorder.py").resolve()
    try:
        environment = layout.provider_environment()
        environment["ASB_SPIKE_LAUNCH_TOKEN"] = "launch-" + uuid.uuid4().hex
        environment["ASB_SPIKE_SURFACE_TOKEN"] = "surface-" + uuid.uuid4().hex
        _write_minimal_codex_home(
            layout,
            source_home=credential_home,
            hook_script=hook_script,
        )
        fingerprint = schema_fingerprint(codex, environment)
        observations.isolation.update(
            {
                "disposable_repository": True,
                "private_provider_home": True,
                "private_switchboard_state": True,
                "private_tmux_server": True,
            }
        )
        generation_before = None
        tui = PrivateTmuxTui.launch(
            layout,
            codex=codex,
            environment=environment,
        )
        observations.isolation["disposable_directory_trust_only"] = (
            tui.disposable_trust_accepted
        )
        launch_facts = tui.current_facts()
        generation_before = _run(
            layout.tmux_socket,
            "display-message",
            "-p",
            "#{socket_path}\t#{pid}\t#{start_time}",
        ).stdout.strip()
        private_socket_path = Path(generation_before.split("\t", 1)[0])
        with CodexAppServer(codex, environment) as app_server:
            identities: list[str] = []
            result_tip_checks: list[bool] = []
            plan_matches: list[bool] = []
            exactly_once: list[bool] = []
            for index, request in enumerate(PLAN_REQUESTS):
                if not _wait_for(
                    lambda: "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"
                    in tui.capture_view()
                    and "esc to interrupt" not in tui.capture_view().lower(),
                    UI_TIMEOUT_SECONDS,
                ):
                    raise LiveStudyError("Codex TUI composer did not become idle")
                time.sleep(QUIET_SECONDS)
                tui.key("BTab")
                if not _wait_for(
                    lambda: "Plan mode" in tui.capture_view(),
                    UI_TIMEOUT_SECONDS,
                ):
                    raise LiveStudyError("Codex TUI did not enter Plan mode")
                if identities:
                    before_prompts = _event_count(
                        _read_events(layout.private_events),
                        provider_identity=identities[-1],
                        kind="UserPromptSubmit",
                    )
                    before_stops = _event_count(
                        _read_events(layout.private_events),
                        provider_identity=identities[-1],
                        kind="Stop",
                    )
                else:
                    before_prompts = 0
                    before_stops = 0
                tui.paste_and_enter(request)
                if not identities:
                    events = _wait_event(
                        layout.private_events,
                        lambda rows: _event_count(
                            rows,
                            kind="UserPromptSubmit",
                        )
                        == 1,
                    )
                    initial_prompts = [
                        event
                        for event in events
                        if event.get("event") == "UserPromptSubmit"
                        and isinstance(event.get("provider_identity"), str)
                    ]
                    if len(initial_prompts) != 1:
                        raise LiveStudyError("source prompt identity is ambiguous")
                    source_identity = initial_prompts[0]["provider_identity"]
                    identities.append(source_identity)
                _wait_event(
                    layout.private_events,
                    lambda rows, required=before_stops + 1: _event_count(
                        rows,
                        provider_identity=identities[-1],
                        kind="Stop",
                    )
                    == required,
                )
                if not _wait_for(
                    lambda: "Implement this plan?" in tui.capture_view(),
                    UI_TIMEOUT_SECONDS,
                ):
                    raise LiveStudyError("native plan implementation picker did not open")
                if index == 0:
                    observations.assertions["source_named_before_rollover"] = (
                        app_server.set_name(identities[-1], "spike-thread-a")
                    )
                tui.key("Down", "Enter")
                events = _wait_event(
                    layout.private_events,
                    lambda rows: _find_new_clear_identity(rows, set(identities))
                    is not None,
                )
                destination = _find_new_clear_identity(events, set(identities))
                if destination is None:
                    raise LiveStudyError("native clear identity is ambiguous")
                identities.append(destination)
                events = _wait_event(
                    layout.private_events,
                    lambda rows, identity=destination: _event_count(
                        rows,
                        provider_identity=identity,
                        kind="UserPromptSubmit",
                    )
                    == 1,
                )
                destination_events = [
                    event
                    for event in events
                    if event.get("provider_identity") == destination
                    and event.get("event") == "UserPromptSubmit"
                ]
                plan_matches.append(
                    len(destination_events) == 1
                    and _structured_plan_matches(
                        app_server,
                        identities[-2],
                        destination_events[0].get("provider_input"),
                    )
                )
                exactly_once.append(
                    _event_count(
                        events,
                        provider_identity=identities[-2],
                        kind="UserPromptSubmit",
                    )
                    == before_prompts + 1
                    and len(destination_events) == 1
                )
                _wait_event(
                    layout.private_events,
                    lambda rows, identity=destination: _event_count(
                        rows,
                        provider_identity=identity,
                        kind="Stop",
                    )
                    == 1,
                )
                result_tip_checks.append(tui.stable_tip())
                observations.assertions[f"destination_{index + 1}_nameable"] = (
                    app_server.set_name(destination, f"spike-thread-{chr(98 + index)}")
                )
                observations.assertions[f"source_{index + 1}_resumable"] = (
                    app_server.thread_read(
                        identities[-2],
                        include_turns=False,
                    ).get("id")
                    == identities[-2]
                )

            final_events = _read_events(layout.private_events)
            observations.events = final_events
            facts = tui.current_facts()
            generation_after = _run(
                layout.tmux_socket,
                "display-message",
                "-p",
                "#{socket_path}\t#{pid}\t#{start_time}",
            ).stdout.strip()
            observations.assertions.update(
                {
                    "three_distinct_provider_identities": len(set(identities)) == 3,
                    "source_first_input_observed": _event_count(
                        final_events,
                        provider_identity=identities[0],
                        kind="UserPromptSubmit",
                    )
                    == 1,
                    "same_managed_pane": facts[3] == tui.pane,
                    "same_tui_process": facts[:2]
                    == (tui.provider_pid, tui.provider_birth),
                    "same_process_working_directory": facts[2] == launch_facts[2],
                    "provider_working_directory_exact": all(
                        isinstance(event.get("provider_cwd"), str)
                        and Path(event["provider_cwd"]).resolve()
                        == layout.repository.resolve()
                        for event in final_events
                    ),
                    "same_launch_and_surface": all(
                        event.get("launch_token")
                        == environment["ASB_SPIKE_LAUNCH_TOKEN"]
                        and event.get("surface_token")
                        == environment["ASB_SPIKE_SURFACE_TOKEN"]
                        and event.get("tmux_pane") == tui.pane
                        for event in final_events
                    ),
                    "same_tmux_generation": generation_after == generation_before,
                    "clear_precedes_destination_input": all(
                        next(
                            index
                            for index, event in enumerate(final_events)
                            if event.get("provider_identity") == identity
                            and event.get("event") == "SessionStart"
                            and event.get("source") == "clear"
                        )
                        < next(
                            index
                            for index, event in enumerate(final_events)
                            if event.get("provider_identity") == identity
                            and event.get("event") == "UserPromptSubmit"
                        )
                        for identity in identities[1:]
                    ),
                    "accepted_plans_carried_exactly": all(plan_matches),
                    "one_destination_submission_per_rollover": all(exactly_once),
                    "result_tips_stable_before_next_action": all(result_tip_checks),
                    "no_post_result_management_traffic": (
                        final_events[-1].get("provider_identity") == identities[-1]
                        and final_events[-1].get("event") == "Stop"
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
            if all(observations.assertions.values())
            else StudyStatus.FALSIFIED
        )
    except AppServerError:
        fingerprint = "0" * 64
        status = StudyStatus.BLOCKED
        observations.limitations.append("provider contract unavailable")
    except LiveStudyError as error:
        fingerprint = locals().get("fingerprint", "0" * 64)
        status = StudyStatus.FALSIFIED
        observations.limitations.append(str(error))
    except (OSError, subprocess.SubprocessError, ValueError):
        fingerprint = locals().get("fingerprint", "0" * 64)
        status = StudyStatus.FALSIFIED
        observations.limitations.append("automatic native rollover invariant failed")
    finally:
        if tui is not None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                tui.stop()
        _run(layout.tmux_socket, "kill-server", check=False)
        observations.cleanup["private_tmux_server_stopped"] = (
            _run(
                layout.tmux_socket,
                "has-session",
                check=False,
            ).returncode
            != 0
        )
        if (
            private_socket_path is not None
            and private_socket_path.name == layout.tmux_socket
            and private_socket_path.exists()
            and stat.S_ISSOCK(private_socket_path.stat().st_mode)
        ):
            private_socket_path.unlink()
        observations.cleanup["private_tmux_endpoint_deleted"] = (
            private_socket_path is None or not private_socket_path.exists()
        )
        observations.cleanup["private_capture_deleted"] = layout.erase_private_events()
        observations.cleanup["unrelated_agent_processes_unchanged"] = (
            _existing_processes_unchanged(preexisting_agents)
        )
        observations.cleanup["unrelated_tmux_panes_unchanged"] = (
            _default_tmux_panes() == preexisting_panes
        )
        root = layout.root
        layout.cleanup()
        observations.cleanup["temporary_root_deleted"] = not root.exists()
        observations.timings_ms["total"] = int(
            (time.monotonic() - started) * 1_000
        )
    if status is StudyStatus.PASS and (
        not all(observations.cleanup.values())
        or not all(observations.isolation.values())
    ):
        status = StudyStatus.FALSIFIED
    return version, fingerprint, status, observations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run two isolated native Codex plan rollovers"
    )
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument(
        "--credential-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--keep-private-events",
        action="store_true",
        help="diagnostic only; forces a non-passing result",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if not arguments.codex:
        raise SystemExit("codex executable was not found")
    version, fingerprint, status, observations = run_live_study(
        codex=arguments.codex,
        credential_home=arguments.credential_home,
        keep_private_events=arguments.keep_private_events,
    )
    if arguments.keep_private_events and status is StudyStatus.PASS:
        status = StudyStatus.BLOCKED
        observations.limitations.append("diagnostic capture retention requested")
    result = StudyResult(
        study="codex-native-repeated-rollover",
        provider="codex",
        installed_version=version,
        contract_fingerprint=fingerprint,
        status=status,
        assertions=observations.assertions or {"provider_contract_available": False},
        event_order=(
            sanitize_hook_order(observations.events) if observations.events else []
        ),
        isolation=observations.isolation or {"isolated_launch_completed": False},
        cleanup=observations.cleanup,
        timings_ms=observations.timings_ms,
        limitations=observations.limitations,
        assisted=arguments.keep_private_events,
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


def raise_keyboard_interrupt() -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        signal.signal(signal.SIGTERM, lambda *_args: raise_keyboard_interrupt())
        raise SystemExit(main())
