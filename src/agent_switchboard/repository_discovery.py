"""Bounded, read-only Git repository and linked-worktree discovery."""

from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import time
import unicodedata
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

GIT_TIMEOUT_SECONDS: Final = 2.0
GIT_OUTPUT_LIMIT: Final = 256 * 1024
MAX_WORKTREES: Final = 128


class RepositoryDiscoveryError(RuntimeError):
    """One bounded repository probe failed without exposing Git output."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class GitCheckoutObservation:
    path: Path
    kind: str
    branch: str | None
    head_oid: str
    git_common_dir: Path
    git_dir: Path


@dataclass(frozen=True, slots=True)
class GitRepositoryObservation:
    git_common_dir: Path
    checkouts: tuple[GitCheckoutObservation, ...]


GitRunner = Callable[[Path, Sequence[str]], bytes]


def _terminate(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_git(path: Path, arguments: Sequence[str]) -> bytes:
    command = ("git", "-C", os.fspath(path), *arguments)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as error:
        raise RepositoryDiscoveryError("git_not_found", "Git was not found.") from error
    except OSError as error:
        raise RepositoryDiscoveryError(
            "git_start_failed", "Git could not be started."
        ) from error
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate(process)
                raise RepositoryDiscoveryError(
                    "git_timeout", "Git repository discovery timed out."
                )
            events = selector.select(remaining)
            if not events:
                continue
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output = streams[str(key.data)]
                output.extend(chunk)
                if len(output) > GIT_OUTPUT_LIMIT:
                    _terminate(process)
                    raise RepositoryDiscoveryError(
                        "git_output_overflow",
                        "Git repository discovery produced too much output.",
                    )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate(process)
            raise RepositoryDiscoveryError(
                "git_timeout", "Git repository discovery timed out."
            )
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            _terminate(process)
            raise RepositoryDiscoveryError(
                "git_timeout", "Git repository discovery timed out."
            ) from error
    finally:
        selector.close()
    if return_code != 0:
        raise RepositoryDiscoveryError(
            "git_probe_failed", "The configured path is not a readable Git checkout."
        )
    return bytes(streams["stdout"])


def _decode(value: bytes, field: str) -> str:
    try:
        decoded = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RepositoryDiscoveryError(
            "git_invalid_utf8", f"Git returned invalid UTF-8 for {field}."
        ) from error
    if "\x00" in decoded or any(
        unicodedata.category(character) == "Cc" and character not in "\n\t"
        for character in decoded
    ):
        raise RepositoryDiscoveryError(
            "git_malformed_output", f"Git returned malformed {field}."
        )
    return decoded


def _line(path: Path, arguments: Sequence[str], field: str, runner: GitRunner) -> str:
    value = _decode(runner(path, arguments), field)
    if value.endswith("\n"):
        value = value[:-1]
    if not value or any(unicodedata.category(character) == "Cc" for character in value):
        raise RepositoryDiscoveryError(
            "git_malformed_output", f"Git returned malformed {field}."
        )
    return value


def _canonical_git_path(base: Path, value: str, field: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return candidate.resolve(strict=False)
    except OSError as error:
        raise RepositoryDiscoveryError(
            "git_path_invalid", f"Git returned an invalid {field}."
        ) from error


def _parse_worktrees(raw: bytes) -> tuple[tuple[Path, str, str | None], ...]:
    try:
        chunks = raw.decode("utf-8", errors="strict").split("\x00\x00")
    except UnicodeDecodeError as error:
        raise RepositoryDiscoveryError(
            "git_invalid_utf8", "Git returned invalid UTF-8 worktree data."
        ) from error
    records: list[tuple[Path, str, str | None]] = []
    for chunk in chunks:
        if not chunk:
            continue
        fields = chunk.split("\x00")
        values: dict[str, str] = {}
        flags: set[str] = set()
        for field in fields:
            name, separator, value = field.partition(" ")
            if (
                re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", name) is None
                or name in values
                or name in flags
                or any(unicodedata.category(character) == "Cc" for character in value)
            ):
                raise RepositoryDiscoveryError(
                    "git_malformed_output", "Git returned malformed worktree data."
                )
            if separator:
                values[name] = value
            else:
                flags.add(name)
        root = values.get("worktree")
        head = values.get("HEAD")
        if (
            root is None
            or head is None
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head) is None
        ):
            raise RepositoryDiscoveryError(
                "git_malformed_output", "Git returned incomplete worktree data."
            )
        if "bare" in flags or "prunable" in flags:
            continue
        branch = values.get("branch")
        if branch is not None:
            prefix = "refs/heads/"
            if not branch.startswith(prefix):
                raise RepositoryDiscoveryError(
                    "git_malformed_output", "Git returned an invalid branch ref."
                )
            branch = branch[len(prefix) :]
        root_path = Path(root)
        if not root_path.is_absolute():
            raise RepositoryDiscoveryError(
                "git_malformed_output", "Git returned a non-absolute worktree root."
            )
        try:
            canonical_root = root_path.resolve(strict=False)
        except OSError as error:
            raise RepositoryDiscoveryError(
                "git_path_invalid", "Git returned an invalid worktree root."
            ) from error
        records.append((canonical_root, head, branch))
    if not records or len(records) > MAX_WORKTREES:
        raise RepositoryDiscoveryError(
            "git_worktree_count_invalid", "Git returned an invalid worktree set."
        )
    if len({record[0] for record in records}) != len(records):
        raise RepositoryDiscoveryError(
            "git_duplicate_worktree", "Git returned duplicate worktree roots."
        )
    return tuple(records)


def probe_git_repository(
    configured_path: str | Path,
    *,
    runner: GitRunner = _run_git,
) -> GitRepositoryObservation:
    """Return bounded identity evidence for a configured checkout and its worktrees."""

    configured = Path(configured_path).resolve(strict=False)
    root = _canonical_git_path(
        configured,
        _line(configured, ("rev-parse", "--show-toplevel"), "worktree root", runner),
        "worktree root",
    )
    raw_worktrees = runner(root, ("worktree", "list", "--porcelain", "-z"))
    worktrees = _parse_worktrees(raw_worktrees)
    if root not in {item[0] for item in worktrees}:
        raise RepositoryDiscoveryError(
            "git_root_missing",
            "The configured checkout is absent from its worktree set.",
        )
    observations: list[GitCheckoutObservation] = []
    common_identity: Path | None = None
    for worktree, head_oid, branch in worktrees:
        git_dir = _canonical_git_path(
            worktree,
            _line(
                worktree,
                ("rev-parse", "--absolute-git-dir"),
                "Git directory",
                runner,
            ),
            "Git directory",
        )
        common_dir = _canonical_git_path(
            worktree,
            _line(
                worktree,
                ("rev-parse", "--git-common-dir"),
                "Git common directory",
                runner,
            ),
            "Git common directory",
        )
        if common_identity is None:
            common_identity = common_dir
        elif common_dir != common_identity:
            raise RepositoryDiscoveryError(
                "git_common_dir_conflict",
                "Configured worktrees do not share one Git common directory.",
            )
        observations.append(
            GitCheckoutObservation(
                path=worktree,
                kind="main" if git_dir == common_dir else "worktree",
                branch=branch,
                head_oid=head_oid,
                git_common_dir=common_dir,
                git_dir=git_dir,
            )
        )
    assert common_identity is not None
    return GitRepositoryObservation(common_identity, tuple(observations))


__all__ = [
    "GitCheckoutObservation",
    "GitRepositoryObservation",
    "RepositoryDiscoveryError",
    "probe_git_repository",
]
