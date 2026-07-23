#!/usr/bin/env python3
"""Exercise conservative managed-worktree ownership in a disposable repository."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
from collections.abc import Sequence
from dataclasses import replace

from spikes.thread_workstream.evidence import StudyResult, StudyStatus
from spikes.thread_workstream.isolation import IsolationLayout
from spikes.thread_workstream.worktrees import (
    ManagedWorktrees,
    Ownership,
    WorktreeError,
    discover_worktrees,
)


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _rejected(action, code: str) -> bool:  # type: ignore[no-untyped-def]
    try:
        action()
    except WorktreeError as error:
        return error.code == code
    return False


def run_study() -> tuple[str, str, StudyStatus, dict[str, bool], dict[str, bool]]:
    started = time.monotonic()
    version = subprocess.run(
        ["git", "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "contract": "porcelain-z-create-switch-status-conservative-retire-v1",
                "version": version,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    layout = IsolationLayout.create()
    assertions: dict[str, bool] = {}
    cleanup: dict[str, bool] = {}
    status = StudyStatus.FALSIFIED
    try:
        manager = ManagedWorktrees(
            layout.repository,
            disposable_root=layout.root,
            marker_token=layout.marker_token,
        )
        shared = manager.shared_claim()
        recorded = shared.recorded_commit
        initial_count = len(discover_worktrees(layout.repository))
        first = manager.create("feature", recorded_commit=recorded)
        second = manager.create("feature", recorded_commit=recorded)
        assertions["collision_free_creation"] = (
            first.branch != second.branch
            and first.location != second.location
            and len(discover_worktrees(layout.repository)) == initial_count + 2
        )
        assertions["provider_working_directory_exact"] = (
            subprocess.run(
                ["pwd"],
                cwd=manager.switch(first),
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            == str(first.location)
        )
        assertions["workstream_switching_exact"] = (
            manager.switch(second) == second.location
            and manager.switch(shared) == layout.repository
        )

        feature = first.location / "feature.txt"
        feature.write_text("managed worktree evidence\n", encoding="utf-8")
        _git(first.location, "add", "feature.txt")
        _git(first.location, "commit", "-q", "-m", "managed evidence")
        ahead = manager.status(first)
        assertions["ahead_status_reported"] = (
            not ahead.dirty
            and ahead.ahead == 1
            and ahead.behind == 0
            and not ahead.merged
        )
        assertions["unmerged_retirement_rejected"] = _rejected(
            lambda: manager.retire(first),
            "retirement_unmerged",
        )
        _git(layout.repository, "merge", "--ff-only", first.branch)
        merged = manager.status(first)
        assertions["merged_status_reported"] = (
            not merged.dirty
            and merged.ahead == 0
            and merged.behind == 0
            and merged.merged
        )
        manager.retire(first)
        retained_branches = set(
            _git(
                layout.repository,
                "branch",
                "--format=%(refname:short)",
            ).stdout.splitlines()
        )
        assertions["exact_clean_managed_retirement"] = (
            not first.location.exists()
            and first.branch not in retained_branches
        )

        dirty_file = second.location / "dirty.txt"
        dirty_file.write_text("must not be discarded\n", encoding="utf-8")
        behind = manager.status(second)
        assertions["dirty_and_behind_status_reported"] = (
            behind.dirty and behind.behind == 1 and behind.ahead == 0
        )
        assertions["dirty_retirement_rejected"] = _rejected(
            lambda: manager.retire(second),
            "retirement_dirty",
        )

        external_location = layout.root / "external-worktree"
        _git(
            layout.repository,
            "worktree",
            "add",
            "-b",
            "external-evidence",
            str(external_location),
            "HEAD",
        )
        external = manager.external_claim(external_location)
        assertions["external_retirement_rejected"] = _rejected(
            lambda: manager.retire(external),
            "retirement_ownership_forbidden",
        )
        assertions["shared_retirement_rejected"] = _rejected(
            lambda: manager.retire(shared),
            "retirement_ownership_forbidden",
        )
        assertions["mismatched_claim_rejected"] = _rejected(
            lambda: manager.retire(
                replace(second, branch="asb-spike/forged")
            ),
            "managed_claim_mismatch",
        )
        source = Path(__file__).with_name("worktrees.py").read_text()
        assertions["no_stash_or_forced_removal"] = (
            '"stash"' not in source
            and '"--force"' not in source
            and '"remove",' in source
        )
        assertions["dirty_and_external_preserved"] = (
            dirty_file.exists()
            and external.location.exists()
            and external.ownership is Ownership.EXTERNAL
        )
        status = (
            StudyStatus.PASS
            if assertions and all(assertions.values())
            else StudyStatus.FALSIFIED
        )
    finally:
        root = layout.root
        layout.cleanup()
        cleanup["temporary_root_deleted"] = not root.exists()
    assertions["bounded_runtime"] = int((time.monotonic() - started) * 1000) < 30_000
    if status is StudyStatus.PASS and (
        not all(assertions.values()) or not all(cleanup.values())
    ):
        status = StudyStatus.FALSIFIED
    return version, fingerprint, status, assertions, cleanup


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    started = time.monotonic()
    version, fingerprint, status, assertions, cleanup = run_study()
    result = StudyResult(
        study="managed-worktree-ownership",
        provider="git",
        installed_version=version,
        contract_fingerprint=fingerprint,
        status=status,
        assertions=assertions,
        event_order=[
            "shared-observed",
            "managed-created",
            "workstreams-switched",
            "status-observed",
            "managed-retired",
            "unsafe-retirements-rejected",
        ],
        isolation={
            "disposable_repository": True,
            "no_repository_remotes": True,
            "temporary_worktree_root": True,
        },
        cleanup=cleanup,
        timings_ms={"total": int((time.monotonic() - started) * 1000)},
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
