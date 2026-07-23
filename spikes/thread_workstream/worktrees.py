"""Spike-only managed-worktree ownership and retirement rules."""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from spikes.thread_workstream.isolation import reject_repository


class WorktreeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Ownership(StrEnum):
    MANAGED = "managed"
    EXTERNAL = "external"
    SHARED = "shared"


@dataclass(frozen=True, slots=True)
class WorktreeEntry:
    location: Path
    commit: str
    branch: str | None


@dataclass(frozen=True, slots=True)
class WorktreeClaim:
    token: str
    ownership: Ownership
    repository_identity: Path
    location: Path
    branch: str
    recorded_commit: str


@dataclass(frozen=True, slots=True)
class WorktreeStatus:
    dirty: bool
    ahead: int
    behind: int
    merged: bool


def _git(
    repository: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=check,
        capture_output=True,
        text=True,
        timeout=15,
    )


def repository_identity(repository: Path) -> Path:
    raw = _git(repository, "rev-parse", "--git-common-dir").stdout.strip()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = repository / candidate
    return candidate.resolve()


def discover_worktrees(repository: Path) -> tuple[WorktreeEntry, ...]:
    raw = _git(repository, "worktree", "list", "--porcelain", "-z").stdout
    result: list[WorktreeEntry] = []
    current: dict[str, str] = {}
    for field in raw.split("\0"):
        if not field:
            if current:
                location = current.get("worktree")
                commit = current.get("HEAD")
                if not location or not commit:
                    raise WorktreeError("worktree_porcelain_incomplete")
                branch = current.get("branch")
                result.append(
                    WorktreeEntry(
                        Path(location).resolve(),
                        commit,
                        (
                            branch.removeprefix("refs/heads/")
                            if branch is not None
                            else None
                        ),
                    )
                )
                current = {}
            continue
        name, separator, value = field.partition(" ")
        if not separator:
            current[name] = ""
        else:
            current[name] = value
    if current:
        raise WorktreeError("worktree_porcelain_unterminated")
    return tuple(result)


class ManagedWorktrees:
    def __init__(
        self,
        repository: Path,
        *,
        disposable_root: Path,
        marker_token: str,
    ) -> None:
        reject_repository(
            repository,
            expected_root=disposable_root,
            expected_token=marker_token,
        )
        self.repository = repository.resolve()
        self.disposable_root = disposable_root.resolve()
        self.marker_token = marker_token
        self.identity = repository_identity(repository)
        self._claims: dict[str, WorktreeClaim] = {}
        self._active: str | None = None

    def shared_claim(self) -> WorktreeClaim:
        branch = _git(
            self.repository,
            "branch",
            "--show-current",
        ).stdout.strip()
        commit = _git(self.repository, "rev-parse", "HEAD").stdout.strip()
        return WorktreeClaim(
            token="shared-primary",
            ownership=Ownership.SHARED,
            repository_identity=self.identity,
            location=self.repository,
            branch=branch,
            recorded_commit=commit,
        )

    def external_claim(self, location: Path) -> WorktreeClaim:
        entry = self._entry(location)
        if entry.branch is None:
            raise WorktreeError("external_worktree_detached")
        return WorktreeClaim(
            token="external-observed",
            ownership=Ownership.EXTERNAL,
            repository_identity=self.identity,
            location=entry.location,
            branch=entry.branch,
            recorded_commit=entry.commit,
        )

    def create(self, slug: str, *, recorded_commit: str) -> WorktreeClaim:
        if (
            not slug
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
                for character in slug
            )
            or slug.startswith("-")
            or slug.endswith("-")
        ):
            raise WorktreeError("managed_slug_invalid")
        reject_repository(
            self.repository,
            expected_root=self.disposable_root,
            expected_token=self.marker_token,
        )
        if (
            _git(
                self.repository,
                "cat-file",
                "-e",
                f"{recorded_commit}^{{commit}}",
                check=False,
            ).returncode
            != 0
        ):
            raise WorktreeError("recorded_commit_unknown")
        base = self.disposable_root / "managed-worktrees"
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        for suffix in range(1, 100):
            label = slug if suffix == 1 else f"{slug}-{suffix}"
            branch = f"asb-spike/{label}"
            location = (base / label).resolve()
            branch_exists = (
                _git(
                    self.repository,
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch}",
                    check=False,
                ).returncode
                == 0
            )
            if location.exists() or branch_exists:
                continue
            _git(
                self.repository,
                "worktree",
                "add",
                "-b",
                branch,
                str(location),
                recorded_commit,
            )
            claim = WorktreeClaim(
                token="managed-" + uuid.uuid4().hex,
                ownership=Ownership.MANAGED,
                repository_identity=self.identity,
                location=location,
                branch=branch,
                recorded_commit=recorded_commit,
            )
            self._claims[claim.token] = claim
            self._validate_exact(claim)
            return claim
        raise WorktreeError("managed_worktree_collision_exhausted")

    def _entry(self, location: Path) -> WorktreeEntry:
        target = location.resolve()
        matches = [
            entry
            for entry in discover_worktrees(self.repository)
            if entry.location == target
        ]
        if len(matches) != 1:
            raise WorktreeError("worktree_not_exactly_discovered")
        return matches[0]

    def _validate_exact(self, claim: WorktreeClaim) -> WorktreeEntry:
        retained = self._claims.get(claim.token)
        if retained != claim:
            raise WorktreeError("managed_claim_mismatch")
        if claim.repository_identity != self.identity:
            raise WorktreeError("managed_repository_mismatch")
        if not claim.location.is_relative_to(self.disposable_root):
            raise WorktreeError("managed_location_external")
        if repository_identity(claim.location) != self.identity:
            raise WorktreeError("managed_repository_drift")
        entry = self._entry(claim.location)
        if entry.branch != claim.branch:
            raise WorktreeError("managed_branch_mismatch")
        return entry

    def switch(self, claim: WorktreeClaim) -> Path:
        if claim.ownership is Ownership.MANAGED:
            self._validate_exact(claim)
        elif claim.ownership is Ownership.SHARED:
            if (
                claim.location != self.repository
                or claim.repository_identity != self.identity
            ):
                raise WorktreeError("shared_claim_mismatch")
        else:
            self._entry(claim.location)
        self._active = claim.token
        return claim.location

    def status(self, claim: WorktreeClaim) -> WorktreeStatus:
        self._validate_exact(claim)
        dirty = bool(_git(claim.location, "status", "--porcelain").stdout)
        comparison = _git(
            self.repository,
            "rev-list",
            "--left-right",
            "--count",
            f"HEAD...{claim.branch}",
        ).stdout.split()
        if len(comparison) != 2:
            raise WorktreeError("managed_divergence_invalid")
        behind, ahead = (int(value) for value in comparison)
        merged = (
            _git(
                self.repository,
                "merge-base",
                "--is-ancestor",
                claim.branch,
                "HEAD",
                check=False,
            ).returncode
            == 0
        )
        return WorktreeStatus(dirty, ahead, behind, merged)

    def retire(self, claim: WorktreeClaim) -> None:
        if claim.ownership is not Ownership.MANAGED:
            raise WorktreeError("retirement_ownership_forbidden")
        self._validate_exact(claim)
        status = self.status(claim)
        if status.dirty:
            raise WorktreeError("retirement_dirty")
        if not status.merged:
            raise WorktreeError("retirement_unmerged")
        if self._active == claim.token:
            raise WorktreeError("retirement_active")
        _git(
            self.repository,
            "worktree",
            "remove",
            str(claim.location),
        )
        _git(self.repository, "branch", "-d", claim.branch)
        del self._claims[claim.token]
        if claim.location.exists() or any(
            entry.location == claim.location
            for entry in discover_worktrees(self.repository)
        ):
            raise WorktreeError("retirement_incomplete")


__all__ = [
    "ManagedWorktrees",
    "Ownership",
    "WorktreeClaim",
    "WorktreeEntry",
    "WorktreeError",
    "WorktreeStatus",
    "discover_worktrees",
    "repository_identity",
]
