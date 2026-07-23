#!/usr/bin/env python3
"""Replay live rollover through adoption and native-history boundary spikes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from spikes.thread_workstream.adoption import (
    AdoptionMachine,
    AtomicBindingStore,
    BindingRecord,
    ClearObservation,
    InputObservation,
    TransitionClassification,
)
from spikes.thread_workstream.codex_app_server import CodexAppServer, latest_plan
from spikes.thread_workstream.codex_rollover import (
    PLAN_TRANSFER_PREFIX,
    LiveStudyError,
    PrivateTmuxTui,
    _event_count,
    _read_events,
    _run,
    _wait_for,
    run_live_study,
)
from spikes.thread_workstream.evidence import (
    StudyResult,
    StudyStatus,
    sanitize_hook_order,
)
from spikes.thread_workstream.isolation import IsolationLayout
from spikes.thread_workstream.navigator import (
    NavigatorState,
    TransitionVisibility,
)

_PROBE_LIMITATIONS: list[str] = []


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _provider_birth(event: Mapping[str, Any]) -> int | None:
    ancestry = event.get("process_ancestry")
    if not isinstance(ancestry, list):
        return None
    for item in ancestry:
        if (
            isinstance(item, Mapping)
            and item.get("command") == "codex"
            and isinstance(item.get("process_birth_ticks"), int)
        ):
            return item["process_birth_ticks"]
    return None


def _provider_exists(app_server: CodexAppServer, identity: str) -> bool:
    try:
        return (
            app_server.thread_read(identity, include_turns=False).get("id") == identity
        )
    except RuntimeError:
        return False


def _event_after(
    events: Sequence[Mapping[str, Any]],
    *,
    start: int,
    identity: str,
    kind: str,
) -> tuple[int, Mapping[str, Any]]:
    for index in range(start + 1, len(events)):
        event = events[index]
        if event.get("provider_identity") == identity and event.get("event") == kind:
            return index, event
    raise LiveStudyError("live adoption event order was incomplete")


def _private_status(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LiveStudyError("input fence status was malformed")
    return value


def _trust_and_history_probe_impl(
    layout: IsolationLayout,
    tui: PrivateTmuxTui,
    environment: Mapping[str, str],
    app_server: CodexAppServer,
    identities: Sequence[str],
    events: Sequence[Mapping[str, Any]],
    stage: list[str],
) -> Mapping[str, bool]:
    _PROBE_LIMITATIONS.clear()
    if len(identities) != 3:
        raise LiveStudyError("live adoption did not receive three identities")
    generation = _run(
        layout.tmux_socket,
        "display-message",
        "-p",
        "#{socket_path}\t#{pid}\t#{start_time}",
    ).stdout.strip()
    capabilities = iter(
        ("capability-" + uuid.uuid4().hex, "capability-" + uuid.uuid4().hex)
    )
    machine = AdoptionMachine(
        AtomicBindingStore(
            BindingRecord(
                version=1,
                provider_identity=identities[0],
                capability="capability-" + uuid.uuid4().hex,
                launch=environment["ASB_SPIKE_LAUNCH_TOKEN"],
                surface=environment["ASB_SPIKE_SURFACE_TOKEN"],
                server_generation=generation,
                pane=tui.pane,
                process_birth=tui.provider_birth,
                working_directory=str(layout.repository),
            )
        ),
        capability_factory=lambda: next(capabilities),
    )
    receipts = []
    clear_indexes = [
        index
        for index, event in enumerate(events)
        if event.get("event") == "SessionStart" and event.get("source") == "clear"
    ]
    for transition_number, clear_index in enumerate(clear_indexes, start=1):
        clear = events[clear_index]
        destination = clear.get("provider_identity")
        if not isinstance(destination, str):
            raise LiveStudyError("live clear lacked provider identity")
        source = machine.current.provider_identity
        input_index, input_event = _event_after(
            events,
            start=clear_index,
            identity=destination,
            kind="UserPromptSubmit",
        )
        plan = latest_plan(app_server.thread_read(source, include_turns=True))
        carried = input_event.get("provider_input")
        carried_plan = (
            carried.removeprefix(PLAN_TRANSFER_PREFIX + "\n\n")
            if isinstance(carried, str)
            and carried.startswith(PLAN_TRANSFER_PREFIX + "\n\n")
            else None
        )
        clear_birth = _provider_birth(clear)
        input_birth = _provider_birth(input_event)
        machine.begin_clear(
            ClearObservation(
                nonce=f"live-clear-{transition_number}",
                order=clear_index + 1,
                predecessor_identity=source,
                destination_identity=destination,
                source=str(clear.get("source")),
                launch=str(clear.get("launch_token")),
                surface=str(clear.get("surface_token")),
                server_generation=(
                    generation
                    if clear.get("tmux_server") == layout.tmux_socket
                    else "generation-mismatch"
                ),
                pane=str(clear.get("tmux_pane")),
                process_birth=clear_birth or -1,
                working_directory=str(clear.get("provider_cwd")),
                provider_ancestor=clear_birth == tui.provider_birth,
                provider_thread_exists=_provider_exists(app_server, destination),
                accepted_plan_digest=_digest(plan) if isinstance(plan, str) else None,
            )
        )
        receipts.append(
            machine.confirm_input(
                InputObservation(
                    nonce=f"live-input-{transition_number}",
                    order=input_index + 1,
                    provider_identity=destination,
                    launch=str(input_event.get("launch_token")),
                    surface=str(input_event.get("surface_token")),
                    server_generation=(
                        generation
                        if input_event.get("tmux_server") == layout.tmux_socket
                        else "generation-mismatch"
                    ),
                    pane=str(input_event.get("tmux_pane")),
                    process_birth=input_birth or -1,
                    working_directory=str(input_event.get("provider_cwd")),
                    provider_ancestor=input_birth == tui.provider_birth,
                    provider_thread_exists=_provider_exists(app_server, destination),
                    carried_plan_digest=(
                        _digest(carried_plan) if isinstance(carried_plan, str) else None
                    ),
                )
            )
        )

    capability_rotation = len(receipts) == 2 and all(
        receipt.capability_rotated for receipt in receipts
    )
    semantic_lineage = all(
        receipt.classification is TransitionClassification.TASK_TRANSITION
        for receipt in receipts
    )

    stage[0] = "navigator"
    pending = NavigatorState(
        previous="thread-b",
        current="thread-c",
        transition=TransitionVisibility.PENDING,
        active_tip="thread-c",
    )
    confirmed = NavigatorState(
        previous="thread-b",
        current="thread-c",
        transition=TransitionVisibility.CONFIRMED,
        active_tip="thread-c",
    )
    navigator_text = confirmed.render()
    provider_tip_before_navigator = tui.capture()
    display = Path(__file__).with_name("navigator_display.py").resolve()
    navigator_command = shlex.join(
        (
            "exec",
            sys.executable,
            str(display),
            "--previous",
            confirmed.previous,
            "--current",
            confirmed.current,
            "--transition",
            confirmed.transition.value,
        )
    )
    _run(
        layout.tmux_socket,
        "new-window",
        "-d",
        "-t",
        "rollover:",
        "-n",
        "navigator",
        navigator_command,
    )
    navigator_pane = _run(
        layout.tmux_socket,
        "display-message",
        "-p",
        "-t",
        "rollover:navigator",
        "#{pane_id}",
    ).stdout.strip()
    navigator_visible = _wait_for(
        lambda: (
            "Transition: confirmed"
            in _run(
                layout.tmux_socket,
                "capture-pane",
                "-p",
                "-t",
                navigator_pane,
            ).stdout
        ),
        10,
    )
    provider_tip_after_navigator = tui.capture()

    source = identities[0]
    before_source = app_server.thread_read(source, include_turns=True)
    before_turns = json.dumps(
        before_source.get("turns"),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    before_inputs = _event_count(
        _read_events(layout.private_events),
        provider_identity=source,
        kind="UserPromptSubmit",
    )
    before_resumes = _event_count(
        _read_events(layout.private_events),
        provider_identity=source,
        kind="SessionStart",
        source="resume",
    )
    stage[0] = "history-start"
    fence_status = layout.private_events.parent / "input-fence.json"
    fence = Path(__file__).with_name("input_fence.py").resolve()
    codex = shutil.which("codex")
    if not codex:
        raise LiveStudyError("historical provider executable was unavailable")
    history_surface = "history-" + uuid.uuid4().hex
    inspection_command = shlex.join(
        (
            "exec",
            "env",
            f"ASB_SPIKE_SURFACE_TOKEN={history_surface}",
            sys.executable,
            str(fence),
            "--status",
            str(fence_status),
            "--",
            codex,
            "--dangerously-bypass-hook-trust",
            "--dangerously-bypass-approvals-and-sandbox",
            "resume",
            "-C",
            str(layout.repository),
            source,
        )
    )
    inspection_pane = ""
    try:
        _run(
            layout.tmux_socket,
            "new-window",
            "-d",
            "-t",
            "rollover:",
            "-n",
            "inspect",
            inspection_command,
        )
        inspection_pane = _run(
            layout.tmux_socket,
            "display-message",
            "-p",
            "-t",
            "rollover:inspect",
            "#{pane_id}",
        ).stdout.strip()
        fence_started = _wait_for(
            lambda: (
                fence_status.exists()
                and _private_status(fence_status).get("childStarted") is True
            ),
            15,
        )
        resume_observed = _wait_for(
            lambda: (
                _event_count(
                    _read_events(layout.private_events),
                    provider_identity=source,
                    kind="SessionStart",
                    source="resume",
                )
                == before_resumes + 1
            ),
            30,
        )
        if not resume_observed:
            _PROBE_LIMITATIONS.append("historical resume hook was not observed")
        native_view = _run(
            layout.tmux_socket,
            "capture-pane",
            "-p",
            "-t",
            inspection_pane,
        ).stdout
        stage[0] = "history-input"
        forbidden = "input-must-not-reach-history"
        _run(
            layout.tmux_socket,
            "send-keys",
            "-l",
            "-t",
            inspection_pane,
            forbidden,
        )
        _run(
            layout.tmux_socket,
            "send-keys",
            "-t",
            inspection_pane,
            "Enter",
        )
        input_dropped = _wait_for(
            lambda: (
                fence_status.exists()
                and isinstance(_private_status(fence_status).get("droppedBytes"), int)
                and _private_status(fence_status)["droppedBytes"] >= len(forbidden)
            ),
            10,
        )
        time.sleep(2)
        after_source = app_server.thread_read(source, include_turns=True)
        after_turns = json.dumps(
            after_source.get("turns"),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        after_inputs = _event_count(
            _read_events(layout.private_events),
            provider_identity=source,
            kind="UserPromptSubmit",
        )

        stage[0] = "history-return"
        _run(
            layout.tmux_socket,
            "select-window",
            "-t",
            "rollover:navigator",
        )
        target, action_count = confirmed.activate_current()
        _run(
            layout.tmux_socket,
            "select-window",
            "-t",
            "rollover:provider",
        )
        active_window = _run(
            layout.tmux_socket,
            "display-message",
            "-p",
            "-t",
            "rollover",
            "#{window_name}",
        ).stdout.strip()
        provider_tip_after_return = tui.capture()
    finally:
        _run(
            layout.tmux_socket,
            "kill-window",
            "-t",
            "rollover:inspect",
            check=False,
        )
        _wait_for(
            lambda: (
                fence_status.exists()
                and _private_status(fence_status).get("childStarted") is False
            ),
            5,
        )
        fence_stopped = (
            fence_status.exists()
            and _private_status(fence_status).get("childStarted") is False
        )
        _run(
            layout.tmux_socket,
            "kill-window",
            "-t",
            "rollover:navigator",
            check=False,
        )

    return {
        "active_binding_rebound_twice": machine.current.provider_identity
        == identities[2]
        and machine.current.version == 3,
        "capability_rotated_twice": capability_rotation,
        "semantic_lineage_confirmed_twice": semantic_lineage,
        "stable_environment_authority": machine.current.launch
        == environment["ASB_SPIKE_LAUNCH_TOKEN"]
        and machine.current.surface == environment["ASB_SPIKE_SURFACE_TOKEN"],
        "navigator_pending_and_confirmed_visible": (
            "Transition: pending" in pending.render()
            and "Transition: confirmed" in navigator_text
            and navigator_visible
        ),
        "navigator_source_current_visible": (
            "Previous: thread-b" in navigator_text
            and "Current: thread-c" in navigator_text
        ),
        "provider_tip_unchanged_by_navigator": (
            provider_tip_before_navigator == provider_tip_after_navigator
        ),
        "historical_fence_started": fence_started,
        "historical_provider_started": fence_started and bool(native_view.strip()),
        "historical_native_view_visible": bool(native_view.strip()),
        "historical_input_dropped": input_dropped,
        "historical_turns_unchanged": before_turns == after_turns,
        "historical_no_input_submitted": before_inputs == after_inputs,
        "active_tip_remained_current": (
            machine.current.provider_identity == identities[2] and target == "thread-c"
        ),
        "returned_in_one_action": action_count == 1 and active_window == "provider",
        "provider_tip_unchanged_after_history": (
            provider_tip_before_navigator == provider_tip_after_return
        ),
        "historical_runtime_stopped": fence_stopped,
    }


def _trust_and_history_probe(
    layout: IsolationLayout,
    tui: PrivateTmuxTui,
    environment: Mapping[str, str],
    app_server: CodexAppServer,
    identities: Sequence[str],
    events: Sequence[Mapping[str, Any]],
) -> Mapping[str, bool]:
    stage = ["adoption"]
    try:
        return _trust_and_history_probe_impl(
            layout,
            tui,
            environment,
            app_server,
            identities,
            events,
            stage,
        )
    except LiveStudyError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise LiveStudyError(f"{stage[0]} spike operation failed") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run live rollover trust and native-history falsification gates"
    )
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument(
        "--credential-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if not arguments.codex:
        raise SystemExit("codex executable was not found")
    version, fingerprint, status, observations = run_live_study(
        codex=arguments.codex,
        credential_home=arguments.credential_home,
        keep_private_events=False,
        post_rollover=_trust_and_history_probe,
    )
    observations.limitations.extend(_PROBE_LIMITATIONS)
    result = StudyResult(
        study="rollover-trust-and-history-boundaries",
        provider="codex",
        installed_version=version,
        contract_fingerprint=fingerprint,
        status=status,
        assertions=observations.assertions or {"live_boundary_completed": False},
        event_order=(
            sanitize_hook_order(observations.events) if observations.events else []
        ),
        isolation=observations.isolation or {"isolated_launch_completed": False},
        cleanup=observations.cleanup,
        timings_ms=observations.timings_ms,
        limitations=observations.limitations,
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
