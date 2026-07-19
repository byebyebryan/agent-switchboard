from __future__ import annotations

from pathlib import Path

import pytest

from agent_switchboard.executable import ExecutableError, resolve_swbctl_executable


def test_resolution_prefers_the_invoked_absolute_entry_point(tmp_path: Path) -> None:
    invoked = tmp_path / "current" / "swbctl"
    invoked.parent.mkdir()
    invoked.touch(mode=0o755)
    searches: list[str] = []

    def search(command: str) -> str | None:
        searches.append(command)
        return None

    assert resolve_swbctl_executable(invoked_as=str(invoked), search=search) == invoked
    assert searches == []


def test_resolution_uses_bounded_path_lookup_and_revalidates_result(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "bin" / "swbctl"
    executable.parent.mkdir()
    executable.touch(mode=0o755)

    assert (
        resolve_swbctl_executable(
            invoked_as="python",
            search=lambda command: str(executable) if command == "swbctl" else None,
        )
        == executable
    )

    executable.chmod(0o644)
    with pytest.raises(ExecutableError, match="not executable"):
        resolve_swbctl_executable(
            invoked_as="python",
            search=lambda _command: str(executable),
        )

    with pytest.raises(ExecutableError, match="not available"):
        resolve_swbctl_executable(invoked_as="python", search=lambda _command: None)
