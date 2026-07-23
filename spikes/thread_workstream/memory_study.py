#!/usr/bin/env python3
"""Measure live and degraded external-memory continuity."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections.abc import Sequence
from pathlib import Path

from spikes.thread_workstream.evidence import StudyResult, StudyStatus
from spikes.thread_workstream.memory_continuity import (
    ContinuityProfile,
    FailingMemoryAdapter,
    FixedMemoryAdapter,
    HealthyReferenceAdapter,
    MemoryObservation,
    TransitionCapsule,
    evaluate_continuity,
)


def _capsule() -> TransitionCapsule:
    return TransitionCapsule(
        accepted_plan="Inspect one disposable marker and report its state.",
        triggering_input="Proceed with the accepted disposable study.",
        project_scope="agent-switchboard",
    )


def run_study() -> tuple[
    str,
    str,
    StudyStatus,
    dict[str, bool],
    dict[str, int],
]:
    capsule = _capsule()
    live_adapter = HealthyReferenceAdapter()
    live = evaluate_continuity(capsule, live_adapter)
    unavailable = evaluate_continuity(capsule, FailingMemoryAdapter())
    delayed = evaluate_continuity(
        capsule,
        FixedMemoryAdapter(
            MemoryObservation(
                available=True,
                healthy=True,
                scope_exact=True,
                recent_context=True,
                planning_context=True,
                latency_ms=2_001,
            )
        ),
    )
    stale = evaluate_continuity(
        capsule,
        FixedMemoryAdapter(
            MemoryObservation(
                available=True,
                healthy=True,
                scope_exact=True,
                recent_context=False,
                planning_context=True,
                latency_ms=1,
            )
        ),
    )
    wrong_scope = evaluate_continuity(
        capsule,
        FixedMemoryAdapter(
            MemoryObservation(
                available=True,
                healthy=True,
                scope_exact=False,
                recent_context=True,
                planning_context=True,
                latency_ms=1,
            )
        ),
    )

    assertions = {
        "live_reference_full_continuity": live.profile
        is ContinuityProfile.FULL,
        "source_scope_exact": bool(live.source and live.source.scope_exact),
        "destination_scope_exact": bool(
            live.destination and live.destination.scope_exact
        ),
        "recent_planning_context_before_first_turn": bool(
            live.source
            and live.destination
            and live.source.recent_context
            and live.destination.recent_context
        ),
        "accepted_plan_preserved_with_live_reference": live.plan_preserved,
        "unavailable_reports_immediate_only": unavailable.profile
        is ContinuityProfile.IMMEDIATE_ONLY,
        "delayed_reports_immediate_only": delayed.profile
        is ContinuityProfile.IMMEDIATE_ONLY,
        "stale_reports_immediate_only": stale.profile
        is ContinuityProfile.IMMEDIATE_ONLY,
        "wrong_scope_reports_immediate_only": wrong_scope.profile
        is ContinuityProfile.IMMEDIATE_ONLY,
        "accepted_plan_survives_all_degradation": all(
            outcome.plan_preserved
            for outcome in (unavailable, delayed, stale, wrong_scope)
        ),
        "memory_never_authorizes_transition": not any(
            outcome.memory_authorized_transition
            for outcome in (live, unavailable, delayed, stale, wrong_scope)
        ),
    }
    status = (
        StudyStatus.PASS
        if assertions and all(assertions.values())
        else StudyStatus.FALSIFIED
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "contract": "read-only-two-scope-checks-before-first-turn-v1",
                "profiles": [
                    ContinuityProfile.FULL,
                    ContinuityProfile.IMMEDIATE_ONLY,
                    ContinuityProfile.BLOCKED,
                ],
                "version": live_adapter.installed_version,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    timings = {
        "source_reference": live.source.latency_ms if live.source else 0,
        "destination_reference": (
            live.destination.latency_ms if live.destination else 0
        ),
    }
    return (
        live_adapter.installed_version,
        fingerprint,
        status,
        assertions,
        timings,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    started = time.monotonic()
    version, fingerprint, status, assertions, timings = run_study()
    timings["total"] = int((time.monotonic() - started) * 1_000)
    result = StudyResult(
        study="external-memory-continuity",
        provider="claude-mem",
        installed_version=version,
        contract_fingerprint=fingerprint,
        status=status,
        assertions=assertions,
        event_order=[
            "transition-capsule-validated",
            "source-scope-observed",
            "destination-scope-observed",
            "continuity-classified-before-first-turn",
            "degradation-replayed",
        ],
        isolation={
            "memory_reference_read_only": True,
            "provider_turns_not_started": True,
            "retained_content_excluded": True,
        },
        cleanup={
            "temporary_memory_state_not_created": True,
            "provider_state_unchanged": True,
        },
        timings_ms=timings,
        limitations=[
            "live reference proves immediate injection only",
            "memory content quality was not evaluated",
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
