#!/usr/bin/env python3
"""Observe Codex execution-intent timing without performing production cutover."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from spikes.thread_workstream.codex_app_server import (
    AppServerError,
    CodexAppServer,
    latest_agent_message,
    latest_plan,
    provider_version,
    schema_fingerprint,
)
from spikes.thread_workstream.codex_rollover import (
    LiveStudyError,
    PrivateTmuxTui,
    _default_tmux_panes,
    _event_count,
    _existing_processes_unchanged,
    _read_events,
    _run,
    _selected_agent_processes,
    _wait_event,
    _wait_for,
    _write_minimal_codex_home,
)
from spikes.thread_workstream.evidence import (
    StudyResult,
    StudyStatus,
    assert_private_file,
    sanitize_hook_order,
    write_private_json,
)
from spikes.thread_workstream.execution_trigger import (
    AtomicCutoverStore,
    CutoverBinding,
    CutoverTransaction,
    DeliveryLedger,
    ExecutionSignal,
    PlanCandidate,
    PlanProvenance,
    TriggerDecision,
    TriggerObservation,
    classify_execution_trigger,
)
from spikes.thread_workstream.isolation import IsolationLayout


UI_TIMEOUT_SECONDS = 30.0
PLAN_REQUEST = (
    "Produce the final implementation plan for a harmless disposable task. "
    "The task is to inspect README.md and make no changes. Do not ask questions."
)
CONVERSATIONAL_PLAN_REQUEST = (
    "Reply with a concise implementation plan for inspecting README.md without "
    "changing files. Stay in the current mode and do not implement it."
)
CONVERSATIONAL_ACCEPTANCE = "Proceed with the accepted conversational plan."


@dataclass(slots=True)
class ScenarioEvidence:
    assertions: dict[str, bool] = field(default_factory=dict)
    cleanup: dict[str, bool] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _turns(thread: Mapping[str, Any]) -> list[object]:
    turns = thread.get("turns")
    return list(turns) if isinstance(turns, list) else []


def _content_free_turn_appended(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    previous = _turns(before)
    current = _turns(after)
    if len(current) != len(previous) + 1 or current[: len(previous)] != previous:
        return False
    added = current[-1]
    return isinstance(added, Mapping) and added.get("items") in (None, [])


def _contains_exact_text(value: object, expected: str) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_exact_text(nested, expected) for nested in value.values())
    if isinstance(value, list):
        return any(_contains_exact_text(nested, expected) for nested in value)
    return value == expected


def _idle(tui: PrivateTmuxTui) -> bool:
    view = tui.capture_view()
    return (
        "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}" in view
        and "esc to interrupt" not in view.lower()
    )


def _source_identity(events: Sequence[Mapping[str, Any]]) -> str:
    identities = {
        event.get("provider_identity")
        for event in events
        if event.get("event") == "UserPromptSubmit"
        and isinstance(event.get("provider_identity"), str)
    }
    if len(identities) != 1:
        raise LiveStudyError("source input identity was ambiguous")
    return identities.pop()  # type: ignore[return-value]


def _write_block_decision(path: Path) -> None:
    write_private_json(path, {"decision": "block"})
    assert_private_file(path)


def _exercise_transaction(
    candidate: PlanCandidate,
    observation: TriggerObservation,
) -> bool:
    receipt = classify_execution_trigger(candidate, observation)
    if receipt.decision is not TriggerDecision.CUTOVER or not receipt.authoritative:
        return False
    store = AtomicCutoverStore(
        CutoverBinding(version=1, active_identity=observation.source_identity)
    )
    ledger = DeliveryLedger()
    transaction = CutoverTransaction.prepare(
        candidate,
        observation,
        receipt,
        store,
    )
    transaction.set_destination("destination-private")
    transaction.deliver(ledger)
    transaction.commit(store)
    consumed = transaction.consume_candidate(candidate)
    return bool(
        ledger.deliveries == 1
        and store.binding.active_identity == "destination-private"
        and consumed.consumed
        and not transaction.source_input_restored
    )


def _ordinary_assertions(
    *,
    layout: IsolationLayout,
    tui: PrivateTmuxTui,
    app_server: CodexAppServer,
    decision_path: Path,
) -> dict[str, bool]:
    if not _wait_for(lambda: _idle(tui), UI_TIMEOUT_SECONDS):
        raise LiveStudyError("ordinary scenario composer did not become idle")
    launch_facts = tui.current_facts()
    tui.key("BTab")
    if not _wait_for(
        lambda: "Plan mode" in tui.capture_view(),
        5.0,
    ):
        tui.paste_and_enter("/plan")
        if not _wait_for(
            lambda: "Plan mode" in tui.capture_view(),
            UI_TIMEOUT_SECONDS,
        ):
            raise LiveStudyError("ordinary scenario did not enter Plan mode")
    tui.paste_and_enter(PLAN_REQUEST)
    events = _wait_event(
        layout.private_events,
        lambda rows: _event_count(rows, kind="UserPromptSubmit") == 1,
    )
    source = _source_identity(events)
    _wait_event(
        layout.private_events,
        lambda rows: (
            _event_count(
                rows,
                provider_identity=source,
                kind="Stop",
            )
            == 1
        ),
    )
    if not _wait_for(
        lambda: "Implement this plan?" in tui.capture_view(),
        UI_TIMEOUT_SECONDS,
    ):
        raise LiveStudyError("ordinary implementation picker did not open")
    before = app_server.thread_read(source, include_turns=True)
    plan = latest_plan(before)
    if not isinstance(plan, str):
        raise LiveStudyError("ordinary scenario had no structured plan")
    plan_digest = _digest(plan)
    revision = len(before.get("turns", []))
    stops_before = _event_count(
        _read_events(layout.private_events),
        provider_identity=source,
        kind="Stop",
    )

    tui.key("Enter")
    held_events = _wait_event(
        layout.private_events,
        lambda rows: any(
            event.get("trigger_match") is True
            and event.get("event") == "UserPromptSubmit"
            for event in rows
        ),
    )
    held = [
        event
        for event in held_events
        if event.get("trigger_match") is True
        and event.get("event") == "UserPromptSubmit"
    ]
    if len(held) != 1:
        raise LiveStudyError("ordinary held input was ambiguous")
    during = app_server.thread_read(source, include_turns=True)
    candidate = PlanCandidate(
        source_identity=source,
        source_revision=revision,
        digest=plan_digest,
        provenance=PlanProvenance.CODEX_PLAN_ITEM,
        accepted=True,
    )
    observation = TriggerObservation(
        nonce="ordinary-live",
        order=1,
        source_identity=source,
        source_revision=revision,
        signal=ExecutionSignal.CODEX_ORDINARY_IMPLEMENT,
        before_source_commit=True,
        source_sampled=False,
        referenced_plan_digest=plan_digest,
        ordinary_coding_input=held[0].get("provider_input") == "Implement the plan.",
        mode_before="plan",
        mode_at_submit=str(held[0].get("permission_mode")),
    )
    receipt = classify_execution_trigger(candidate, observation)
    _write_block_decision(decision_path)
    if not _wait_for(lambda: _idle(tui), UI_TIMEOUT_SECONDS):
        raise LiveStudyError("ordinary source composer was not restored")
    final_events = _read_events(layout.private_events)
    after = app_server.thread_read(source, include_turns=True)
    facts = tui.current_facts()
    return {
        "ordinary_input_observed_while_held": bool(held),
        "ordinary_input_is_fixed_coding_action": observation.ordinary_coding_input,
        "ordinary_trigger_independent_of_permission_mode": observation.mode_at_submit
        != "plan",
        "structured_plan_available_while_held": latest_plan(during) == plan,
        "ordinary_trigger_authoritative": receipt.authoritative
        and receipt.decision is TriggerDecision.CUTOVER,
        "ordinary_transaction_replay_exact_once": _exercise_transaction(
            candidate,
            observation,
        ),
        "ordinary_source_identity_unchanged": all(
            event.get("provider_identity") == source for event in final_events
        ),
        "ordinary_no_clear_started": not any(
            event.get("event") == "SessionStart" and event.get("source") == "clear"
            for event in final_events
        ),
        "ordinary_block_appends_only_content_free_turn": _content_free_turn_appended(
            before,
            after,
        ),
        "ordinary_blocked_input_absent_from_source_history": not _contains_exact_text(
            after,
            "Implement the plan.",
        ),
        "ordinary_no_execution_stop_after_block": _event_count(
            final_events,
            provider_identity=source,
            kind="Stop",
        )
        == stops_before,
        "ordinary_same_surface_and_process": facts == launch_facts,
        "ordinary_source_composer_restored": _idle(tui),
    }


def _conversational_assertions(
    *,
    layout: IsolationLayout,
    tui: PrivateTmuxTui,
    app_server: CodexAppServer,
    decision_path: Path,
) -> dict[str, bool]:
    if not _wait_for(lambda: _idle(tui), UI_TIMEOUT_SECONDS):
        raise LiveStudyError("conversational scenario composer did not become idle")
    launch_facts = tui.current_facts()
    tui.paste_and_enter(CONVERSATIONAL_PLAN_REQUEST)
    events = _wait_event(
        layout.private_events,
        lambda rows: _event_count(rows, kind="UserPromptSubmit") == 1,
    )
    source = _source_identity(events)
    _wait_event(
        layout.private_events,
        lambda rows: (
            _event_count(
                rows,
                provider_identity=source,
                kind="Stop",
            )
            == 1
        ),
    )
    if not _wait_for(lambda: _idle(tui), UI_TIMEOUT_SECONDS):
        raise LiveStudyError("conversational plan did not settle")
    before = app_server.thread_read(source, include_turns=True)
    selected_plan = latest_agent_message(before)
    if not isinstance(selected_plan, str):
        raise LiveStudyError("conversational plan result was unavailable")
    plan_digest = _digest(selected_plan)
    stop_results = [
        event.get("provider_output")
        for event in _read_events(layout.private_events)
        if event.get("provider_identity") == source and event.get("event") == "Stop"
    ]
    revision = len(before.get("turns", []))
    stops_before = _event_count(
        _read_events(layout.private_events),
        provider_identity=source,
        kind="Stop",
    )

    tui.paste_and_enter(CONVERSATIONAL_ACCEPTANCE)
    held_events = _wait_event(
        layout.private_events,
        lambda rows: any(
            event.get("trigger_match") is True
            and event.get("event") == "UserPromptSubmit"
            for event in rows
        ),
    )
    held = [
        event
        for event in held_events
        if event.get("trigger_match") is True
        and event.get("event") == "UserPromptSubmit"
    ]
    if len(held) != 1:
        raise LiveStudyError("conversational held input was ambiguous")
    during = app_server.thread_read(source, include_turns=True)
    selected = PlanCandidate(
        source_identity=source,
        source_revision=revision,
        digest=plan_digest,
        provenance=PlanProvenance.EXPLICIT_SELECTION,
        accepted=True,
    )
    natural_observation = TriggerObservation(
        nonce="conversational-live",
        order=1,
        source_identity=source,
        source_revision=revision,
        signal=ExecutionSignal.NATURAL_LANGUAGE_ACCEPTANCE,
        before_source_commit=True,
        source_sampled=False,
        referenced_plan_digest=plan_digest,
        mode_before="default",
        mode_at_submit=str(held[0].get("permission_mode")),
    )
    natural = classify_execution_trigger(selected, natural_observation)
    explicit_observation = replace(
        natural_observation,
        nonce="explicit-selection-live",
        signal=ExecutionSignal.EXPLICIT_FRESH_IMPLEMENT,
    )
    explicit = classify_execution_trigger(selected, explicit_observation)
    _write_block_decision(decision_path)
    if not _wait_for(lambda: _idle(tui), UI_TIMEOUT_SECONDS):
        raise LiveStudyError("conversational source composer was not restored")
    final_events = _read_events(layout.private_events)
    after = app_server.thread_read(source, include_turns=True)
    facts = tui.current_facts()
    return {
        "conversational_input_observed_while_held": bool(held),
        "conversational_result_has_no_structured_plan": latest_plan(before) is None,
        "conversational_stop_exposes_exact_result": stop_results == [selected_plan],
        "conversational_result_explicitly_selectable": latest_agent_message(during)
        == selected_plan,
        "natural_language_is_advisory_only": natural.decision
        is TriggerDecision.ADVISORY
        and not natural.authoritative,
        "explicit_selection_authorizes_cutover": explicit.decision
        is TriggerDecision.CUTOVER
        and explicit.authoritative,
        "explicit_transaction_replay_exact_once": _exercise_transaction(
            selected,
            explicit_observation,
        ),
        "conversational_source_identity_unchanged": all(
            event.get("provider_identity") == source for event in final_events
        ),
        "conversational_block_appends_only_content_free_turn": (
            _content_free_turn_appended(before, after)
        ),
        "conversational_blocked_input_absent_from_source_history": (
            not _contains_exact_text(after, CONVERSATIONAL_ACCEPTANCE)
        ),
        "conversational_no_execution_stop_after_block": _event_count(
            final_events,
            provider_identity=source,
            kind="Stop",
        )
        == stops_before,
        "conversational_same_surface_and_process": facts == launch_facts,
        "conversational_source_composer_restored": _idle(tui),
    }


def _run_scenario(
    *,
    scenario: str,
    codex: str,
    credential_home: Path,
    keep_private_events: bool,
) -> ScenarioEvidence:
    started = time.monotonic()
    layout = IsolationLayout.create(keep_private_events=keep_private_events)
    result = ScenarioEvidence()
    tui: PrivateTmuxTui | None = None
    decision_path = layout.private_events.parent / "decision.json"
    try:
        environment = layout.provider_environment()
        environment["ASB_SPIKE_LAUNCH_TOKEN"] = "trigger-launch"
        environment["ASB_SPIKE_SURFACE_TOKEN"] = f"trigger-{scenario}"
        hook_script = Path(__file__).with_name("trigger_hook.py").resolve()
        _write_minimal_codex_home(
            layout,
            source_home=credential_home,
            hook_script=hook_script,
            hook_arguments=(str(decision_path), scenario),
            hook_timeout=45,
        )
        tui = PrivateTmuxTui.launch(
            layout,
            codex=codex,
            environment=environment,
        )
        with CodexAppServer(codex, environment) as app_server:
            if scenario == "ordinary":
                result.assertions = _ordinary_assertions(
                    layout=layout,
                    tui=tui,
                    app_server=app_server,
                    decision_path=decision_path,
                )
            elif scenario == "conversational":
                result.assertions = _conversational_assertions(
                    layout=layout,
                    tui=tui,
                    app_server=app_server,
                    decision_path=decision_path,
                )
            else:
                raise LiveStudyError("trigger scenario is unsupported")
        result.events = _read_events(layout.private_events)
    finally:
        if tui is not None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                tui.stop()
        _run(layout.tmux_socket, "kill-server", check=False)
        with contextlib.suppress(FileNotFoundError):
            decision_path.unlink()
        result.cleanup[f"{scenario}_decision_deleted"] = not decision_path.exists()
        result.cleanup[f"{scenario}_capture_deleted"] = layout.erase_private_events()
        root = layout.root
        layout.cleanup()
        result.cleanup[f"{scenario}_root_deleted"] = not root.exists()
        result.elapsed_ms = int((time.monotonic() - started) * 1_000)
    return result


def run_live_study(
    *,
    codex: str,
    credential_home: Path,
    keep_private_events: bool,
) -> tuple[str, str, StudyStatus, dict[str, Any]]:
    started = time.monotonic()
    version = provider_version(codex)
    preexisting_agents = _selected_agent_processes()
    preexisting_panes = _default_tmux_panes()
    fingerprint_layout = IsolationLayout.create()
    try:
        fingerprint = schema_fingerprint(
            codex,
            fingerprint_layout.provider_environment(),
        )
    finally:
        fingerprint_root = fingerprint_layout.root
        fingerprint_layout.cleanup()
    limitations = [
        "live destination cutover reused proven transaction evidence",
        "explicit conversational plan selection was replayed spike-only",
    ]
    try:
        ordinary = _run_scenario(
            scenario="ordinary",
            codex=codex,
            credential_home=credential_home,
            keep_private_events=keep_private_events,
        )
        conversational = _run_scenario(
            scenario="conversational",
            codex=codex,
            credential_home=credential_home,
            keep_private_events=keep_private_events,
        )
        assertions = {**ordinary.assertions, **conversational.assertions}
        cleanup = {
            **ordinary.cleanup,
            **conversational.cleanup,
            "fingerprint_root_deleted": not fingerprint_root.exists(),
            "unrelated_agent_processes_unchanged": _existing_processes_unchanged(
                preexisting_agents
            ),
            "unrelated_tmux_panes_unchanged": _default_tmux_panes()
            == preexisting_panes,
        }
        events = [*ordinary.events, *conversational.events]
        timings = {
            "ordinary": ordinary.elapsed_ms,
            "conversational": conversational.elapsed_ms,
            "total": int((time.monotonic() - started) * 1_000),
        }
        status = (
            StudyStatus.PASS
            if all(assertions.values()) and all(cleanup.values())
            else StudyStatus.FALSIFIED
        )
    except AppServerError:
        status = StudyStatus.BLOCKED
        assertions = {"provider_contract_available": False}
        cleanup = {"fingerprint_root_deleted": not fingerprint_root.exists()}
        events = []
        timings = {"total": int((time.monotonic() - started) * 1_000)}
        limitations.append("provider contract unavailable")
    except (LiveStudyError, OSError, subprocess.SubprocessError, ValueError) as error:
        status = StudyStatus.FALSIFIED
        assertions = {"live_trigger_contract_held": False}
        cleanup = {"fingerprint_root_deleted": not fingerprint_root.exists()}
        events = []
        timings = {"total": int((time.monotonic() - started) * 1_000)}
        limitations.append(str(error))
    return (
        version,
        fingerprint,
        status,
        {
            "assertions": assertions,
            "cleanup": cleanup,
            "events": events,
            "timings": timings,
            "limitations": limitations,
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Observe isolated Codex execution-intent timing"
    )
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument(
        "--credential-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--keep-private-events", action="store_true")
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
        observations["limitations"].append("diagnostic capture retention requested")
    result = StudyResult(
        study="codex-execution-intent-timing",
        provider="codex",
        installed_version=version,
        contract_fingerprint=fingerprint,
        status=status,
        assertions=observations["assertions"],
        event_order=(
            sanitize_hook_order(observations["events"])
            if observations["events"]
            else []
        ),
        isolation={
            "disposable_repositories": True,
            "private_provider_homes": True,
            "private_switchboard_state": True,
            "private_tmux_servers": True,
            "source_inputs_blocked_before_sampling": True,
        },
        cleanup=observations["cleanup"],
        timings_ms=observations["timings"],
        limitations=observations["limitations"],
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
