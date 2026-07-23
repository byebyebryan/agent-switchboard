"""Fail-closed disposable roots for thread/workstream studies."""

from __future__ import annotations

from contextlib import suppress
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final


MARKER_NAME: Final = ".agent-switchboard-disposable-spike"
SOCKET_PREFIX: Final = "asb-thread-workstream-spike-"


class IsolationError(RuntimeError):
    """A study target overlaps non-disposable user state."""


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _run_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


@dataclass(slots=True)
class IsolationLayout:
    """Every mutable study dependency rooted below one temporary directory."""

    _temporary: tempfile.TemporaryDirectory[str]
    root: Path
    codex_home: Path
    switchboard_state: Path
    repository: Path
    private_events: Path
    tmux_socket: str
    marker_token: str
    keep_private_events: bool = False

    @classmethod
    def create(cls, *, keep_private_events: bool = False) -> IsolationLayout:
        temporary = tempfile.TemporaryDirectory(prefix="asb-thread-workstream-")
        root = Path(temporary.name).resolve()
        token = uuid.uuid4().hex
        layout = cls(
            _temporary=temporary,
            root=root,
            codex_home=root / "codex-home",
            switchboard_state=root / "switchboard-state",
            repository=root / "repository",
            private_events=root / "private" / "events.jsonl",
            tmux_socket=SOCKET_PREFIX + uuid.uuid4().hex[:16],
            marker_token=token,
            keep_private_events=keep_private_events,
        )
        layout._initialize()
        return layout

    def _initialize(self) -> None:
        for directory in (
            self.codex_home,
            self.switchboard_state,
            self.repository,
            self.private_events.parent,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        marker = self.repository / MARKER_NAME
        marker.write_text(self.marker_token + "\n", encoding="ascii")
        marker.chmod(0o600)
        _run_git(self.repository, "init", "-q")
        _run_git(self.repository, "config", "user.name", "Switchboard Spike")
        _run_git(
            self.repository,
            "config",
            "user.email",
            "switchboard-spike.invalid@example.invalid",
        )
        readme = self.repository / "README.md"
        readme.write_text(
            "# Disposable rollover evidence repository\n\n"
            "This repository exists only for an isolated falsification study.\n",
            encoding="utf-8",
        )
        _run_git(self.repository, "add", MARKER_NAME, "README.md")
        _run_git(self.repository, "commit", "-q", "-m", "spike baseline")
        self.validate()

    def validate(self) -> None:
        roots = (
            self.codex_home,
            self.switchboard_state,
            self.repository,
            self.private_events.parent,
        )
        if self.root == Path("/") or not all(
            _inside(path, self.root) for path in roots
        ):
            raise IsolationError("study roots must remain below the temporary root")
        marker = self.repository / MARKER_NAME
        if (
            not marker.is_file()
            or marker.read_text(encoding="ascii").strip() != self.marker_token
        ):
            raise IsolationError("repository lacks its exact disposable marker")
        if not (self.repository / ".git").is_dir():
            raise IsolationError("study repository is not an owned checkout")
        if _run_git(self.repository, "remote").stdout.strip():
            raise IsolationError("disposable repository must have no remotes")
        if not self.tmux_socket.startswith(SOCKET_PREFIX):
            raise IsolationError("tmux socket is not spike-owned")
        inherited_tmux = os.environ.get("TMUX", "").split(",", 1)[0]
        inherited_switchboard = os.environ.get("SWB_V3_TMUX_SOCKET", "")
        if self.tmux_socket in {inherited_tmux, inherited_switchboard}:
            raise IsolationError("study tmux socket overlaps inherited state")
        source_codex_home = Path(
            os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
        )
        if self.codex_home.resolve() == source_codex_home.resolve():
            raise IsolationError("study Codex home overlaps the selected user home")
        for name in ("SWB_V3_CONFIG_ROOT", "SWB_V3_STATE_ROOT"):
            inherited = os.environ.get(name)
            if (
                inherited
                and self.switchboard_state.resolve() == Path(inherited).resolve()
            ):
                raise IsolationError("study Switchboard state overlaps inherited state")

    def provider_environment(self) -> dict[str, str]:
        self.validate()
        environment = dict(os.environ)
        environment.pop("TMUX", None)
        environment.pop("TMUX_PANE", None)
        environment["CODEX_HOME"] = str(self.codex_home)
        environment["SWB_V3_CONFIG_ROOT"] = str(self.switchboard_state / "config")
        environment["SWB_V3_STATE_ROOT"] = str(self.switchboard_state / "state")
        environment["SWB_V3_TMUX_SOCKET"] = self.tmux_socket
        environment["ASB_SPIKE_DISPOSABLE_ROOT"] = str(self.root)
        return environment

    def erase_private_events(self) -> bool:
        if self.keep_private_events:
            return not self.private_events.exists() or self.private_events.is_file()
        with suppress(FileNotFoundError):
            self.private_events.unlink()
        return not self.private_events.exists()

    def cleanup(self) -> None:
        self.erase_private_events()
        self._temporary.cleanup()

    def __enter__(self) -> IsolationLayout:
        self.validate()
        return self

    def __exit__(
        self, _exc_type: object, _exc_value: object, _traceback: object
    ) -> None:
        self.cleanup()


def reject_repository(
    repository: Path,
    *,
    expected_root: Path,
    expected_token: str,
) -> None:
    """Standalone guard used immediately before any destructive study action."""

    repository = repository.resolve()
    if not _inside(repository, expected_root):
        raise IsolationError("repository is outside the disposable study root")
    marker = repository / MARKER_NAME
    if not marker.is_file() or marker.read_text().strip() != expected_token:
        raise IsolationError("repository marker does not authorize study mutation")
    if not (repository / ".git").is_dir():
        raise IsolationError("repository does not own a Git directory")
    git = shutil.which("git")
    if not git:
        raise IsolationError("git is unavailable")
    remotes = subprocess.run(
        [git, "-C", str(repository), "remote"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if remotes:
        raise IsolationError("repository with remotes is not disposable")


__all__ = [
    "MARKER_NAME",
    "SOCKET_PREFIX",
    "IsolationError",
    "IsolationLayout",
    "reject_repository",
]
