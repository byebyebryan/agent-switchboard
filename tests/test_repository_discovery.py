from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_switchboard.repository_discovery import (
    MAX_WORKTREES,
    RepositoryDiscoveryError,
    probe_git_repository,
)


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_probe_discovers_main_and_linked_worktree_without_mutation(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", "-b", "main", cwd=repository)
    _git("config", "user.email", "switchboard@example.invalid", cwd=repository)
    _git("config", "user.name", "Switchboard Test", cwd=repository)
    (repository / "README.md").write_text("test\n", encoding="utf-8")
    _git("add", "README.md", cwd=repository)
    _git("commit", "-m", "initial", cwd=repository)
    worktree = tmp_path / "feature"
    _git("worktree", "add", "-b", "feature", str(worktree), cwd=repository)

    before = subprocess.run(
        ("git", "status", "--porcelain=v1"),
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    observation = probe_git_repository(repository)
    after = subprocess.run(
        ("git", "status", "--porcelain=v1"),
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout

    assert before == after == b""
    assert {item.path for item in observation.checkouts} == {
        repository.resolve(),
        worktree.resolve(),
    }
    by_path = {item.path: item for item in observation.checkouts}
    assert by_path[repository.resolve()].kind == "main"
    assert by_path[repository.resolve()].branch == "main"
    assert by_path[worktree.resolve()].kind == "worktree"
    assert by_path[worktree.resolve()].branch == "feature"
    assert all(
        item.git_common_dir == observation.git_common_dir
        for item in observation.checkouts
    )


def test_probe_uses_only_fixed_read_argv_and_rejects_malformed_output(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    git_dir = root / ".git"
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def runner(path: Path, arguments: tuple[str, ...]) -> bytes:
        calls.append((path, arguments))
        responses = {
            ("rev-parse", "--show-toplevel"): f"{root}\n".encode(),
            ("worktree", "list", "--porcelain", "-z"): (
                f"worktree {root}\0HEAD {'a' * 40}\0branch refs/heads/main\0\0"
            ).encode(),
            ("rev-parse", "--absolute-git-dir"): f"{git_dir}\n".encode(),
            ("rev-parse", "--git-common-dir"): f"{git_dir}\n".encode(),
        }
        return responses[arguments]

    observation = probe_git_repository(root, runner=runner)
    assert observation.checkouts[0].path == root
    assert [arguments for _path, arguments in calls] == [
        ("rev-parse", "--show-toplevel"),
        ("worktree", "list", "--porcelain", "-z"),
        ("rev-parse", "--absolute-git-dir"),
        ("rev-parse", "--git-common-dir"),
    ]

    def invalid_runner(path: Path, arguments: tuple[str, ...]) -> bytes:
        del path
        if arguments == ("rev-parse", "--show-toplevel"):
            return f"{root}\n".encode()
        return b"\xff"

    with pytest.raises(RepositoryDiscoveryError) as failure:
        probe_git_repository(root, runner=invalid_runner)
    assert failure.value.code == "git_invalid_utf8"


def test_probe_rejects_unbounded_worktree_sets(tmp_path: Path) -> None:
    root = tmp_path.resolve()

    def runner(path: Path, arguments: tuple[str, ...]) -> bytes:
        del path
        if arguments == ("rev-parse", "--show-toplevel"):
            return f"{root}\n".encode()
        if arguments == ("worktree", "list", "--porcelain", "-z"):
            return b"".join(
                f"worktree {root / str(index)}\0HEAD {'a' * 40}\0\0".encode()
                for index in range(MAX_WORKTREES + 1)
            )
        raise AssertionError("worktree overflow must fail before per-root probes")

    with pytest.raises(RepositoryDiscoveryError) as failure:
        probe_git_repository(root, runner=runner)
    assert failure.value.code == "git_worktree_count_invalid"


@pytest.mark.parametrize(
    "record",
    (
        "worktree relative\0HEAD " + "a" * 40 + "\0\0",
        "worktree /safe\nunsafe\0HEAD " + "a" * 40 + "\0\0",
        "worktree /safe\0HEAD " + "g" * 40 + "\0\0",
        "worktree /safe\0HEAD " + "a" * 40 + "\0branch refs/heads/bad\tref\0\0",
    ),
)
def test_probe_rejects_unsafe_worktree_identity(tmp_path: Path, record: str) -> None:
    root = tmp_path.resolve()

    def runner(path: Path, arguments: tuple[str, ...]) -> bytes:
        del path
        if arguments == ("rev-parse", "--show-toplevel"):
            return f"{root}\n".encode()
        if arguments == ("worktree", "list", "--porcelain", "-z"):
            return record.encode()
        raise AssertionError("unsafe worktree data must fail before per-root probes")

    with pytest.raises(RepositoryDiscoveryError) as failure:
        probe_git_repository(root, runner=runner)
    assert failure.value.code == "git_malformed_output"
