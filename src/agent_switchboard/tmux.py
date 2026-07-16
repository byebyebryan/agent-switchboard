"""Validated host-local tmux transport for managed session surfaces."""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import subprocess
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

MAX_TMUX_STDOUT_BYTES: Final = 1024 * 1024
MAX_TMUX_STDERR_BYTES: Final = 4096
TMUX_COMMAND_TIMEOUT_SECONDS: Final = 1.0
TMUX_CREATE_TIMEOUT_SECONDS: Final = 5.0
_LOCATOR_KEYS: Final = frozenset({"pane", "session", "socket", "window"})
_METADATA_OPTIONS: Final = {
    "surface_id": "@agent_switchboard_surface_id",
    "session_key": "@agent_switchboard_session_key",
    "provider": "@agent_switchboard_provider",
    "launch_id": "@agent_switchboard_launch_id",
    "role": "@agent_switchboard_surface_role",
}
_SESSION_NAME: Final = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class _AutoSystemdRun:
    pass


_AUTO_SYSTEMD_RUN: Final = _AutoSystemdRun()


class TmuxError(RuntimeError):
    """A bounded tmux action failed or returned an unsafe result."""


class TmuxTargetMissing(TmuxError):
    """The exact tmux target no longer exists."""


@dataclass(frozen=True, slots=True)
class TmuxLocator:
    socket: str
    session: str
    window: str
    pane: str

    def __post_init__(self) -> None:
        for field_name, maximum in (
            ("socket", 4096),
            ("session", 256),
            ("window", 256),
            ("pane", 256),
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or len(value) > maximum:
                raise TmuxError(f"tmux {field_name} must be bounded text")
            if any(unicodedata.category(character) == "Cc" for character in value):
                raise TmuxError(f"tmux {field_name} contains control characters")
        if not Path(self.socket).is_absolute():
            raise TmuxError("tmux socket must be an absolute path")
        if not self.window.startswith("@") or not self.window[1:].isdigit():
            raise TmuxError("tmux window must use canonical window ID syntax")
        if not self.pane.startswith("%") or not self.pane[1:].isdigit():
            raise TmuxError("tmux pane must use canonical pane ID syntax")

    @property
    def target(self) -> str:
        return self.pane

    def to_storage(self) -> str:
        return json.dumps(
            {
                "pane": self.pane,
                "session": self.session,
                "socket": self.socket,
                "window": self.window,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_storage(cls, value: object) -> TmuxLocator:
        if not isinstance(value, str) or len(value) > 8192:
            raise TmuxError("tmux locator must be bounded JSON text")
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, RecursionError) as error:
            raise TmuxError("tmux locator is not valid JSON") from error
        if not isinstance(decoded, dict) or set(decoded) != _LOCATOR_KEYS:
            raise TmuxError("tmux locator has an incompatible shape")
        if not all(isinstance(key, str) for key in decoded):
            raise TmuxError("tmux locator contains a non-text field")
        return cls(
            socket=decoded["socket"],
            session=decoded["session"],
            window=decoded["window"],
            pane=decoded["pane"],
        )


@dataclass(frozen=True, slots=True)
class TmuxMetadata:
    surface_id: str | None
    session_key: str | None
    provider: str | None
    launch_id: str | None
    role: str | None


@dataclass(frozen=True, slots=True)
class TmuxSurfaceObservation:
    locator: TmuxLocator
    client_attached: bool
    metadata: TmuxMetadata


CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[bytes]]


def _bounded_runner(
    argv: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[bytes]:
    command = list(argv)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    buffers = {
        process.stdout.fileno(): (process.stdout, MAX_TMUX_STDOUT_BYTES, bytearray()),
        process.stderr.fileno(): (process.stderr, MAX_TMUX_STDERR_BYTES, bytearray()),
    }
    for descriptor, (stream, _maximum, _buffer) in buffers.items():
        os.set_blocking(descriptor, False)
        selector.register(stream, selectors.EVENT_READ, descriptor)
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(command, timeout)
            for key, _mask in events:
                descriptor = key.data
                stream, maximum, buffer = buffers[descriptor]
                chunk = os.read(descriptor, min(65_536, maximum - len(buffer) + 1))
                if not chunk:
                    selector.unregister(stream)
                    continue
                buffer.extend(chunk)
                if len(buffer) > maximum:
                    raise TmuxError("tmux command output exceeded its safe bound")
        returncode = process.wait(max(0.0, deadline - time.monotonic()))
        return subprocess.CompletedProcess(
            command,
            returncode,
            bytes(buffers[process.stdout.fileno()][2]),
            bytes(buffers[process.stderr.fileno()][2]),
        )
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _safe_diagnostic(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    printable = "".join(
        character if character.isprintable() else " " for character in text
    )
    return " ".join(printable.split())[:512]


def _bounded_text(value: object, field: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise TmuxError(f"{field} must be bounded text")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise TmuxError(f"{field} contains control characters")
    return value


class TmuxController:
    """Execute only exact, revalidated tmux surface operations."""

    def __init__(
        self,
        *,
        runner: CommandRunner = _bounded_runner,
        systemd_run: str | None | _AutoSystemdRun = _AUTO_SYSTEMD_RUN,
    ) -> None:
        self._runner = runner
        self._requires_systemd_run = systemd_run is _AUTO_SYSTEMD_RUN
        self._systemd_run = (
            shutil.which("systemd-run") if self._requires_systemd_run else systemd_run
        )

    @staticmethod
    def _tmux(socket: str | None, *arguments: str) -> list[str]:
        command = ["tmux"]
        if socket is not None:
            if not Path(socket).is_absolute():
                raise TmuxError("tmux socket must be an absolute path")
            command.extend(("-S", socket))
        command.extend(arguments)
        return command

    def _run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = TMUX_COMMAND_TIMEOUT_SECONDS,
        missing_ok: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = self._runner(argv, timeout)
        except FileNotFoundError as error:
            raise TmuxError(f"required executable is unavailable: {argv[0]}") from error
        except subprocess.TimeoutExpired as error:
            raise TmuxError("tmux command exceeded its deadline") from error
        except OSError as error:
            message = f"tmux command failed to start: {type(error).__name__}"
            raise TmuxError(message) from error
        if (
            len(result.stdout) > MAX_TMUX_STDOUT_BYTES
            or len(result.stderr) > MAX_TMUX_STDERR_BYTES
        ):
            raise TmuxError("tmux command output exceeded its safe bound")
        if result.returncode != 0:
            diagnostic = _safe_diagnostic(result.stderr)
            lowered = diagnostic.casefold()
            missing = any(
                marker in lowered
                for marker in (
                    "can't find pane",
                    "can't find session",
                    "can't find window",
                    "no server running",
                    "no such file or directory",
                )
            )
            if missing:
                if missing_ok:
                    return result
                raise TmuxTargetMissing(diagnostic or "tmux target is unavailable")
            message = diagnostic or f"tmux exited with status {result.returncode}"
            raise TmuxError(message)
        return result

    def inspect_pane(self, socket: str | None, target: str) -> TmuxSurfaceObservation:
        _bounded_text(target, "tmux target", maximum=1024)
        fields = (
            "#{socket_path}\t#{session_name}\t#{window_id}\t#{pane_id}\t"
            "#{session_attached}\t#{@agent_switchboard_surface_id}\t"
            "#{@agent_switchboard_session_key}\t#{@agent_switchboard_provider}\t"
            "#{@agent_switchboard_launch_id}\t#{@agent_switchboard_surface_role}"
        )
        result = self._run(
            self._tmux(socket, "display-message", "-p", "-t", target, fields)
        )
        try:
            text = result.stdout.decode("utf-8")
        except UnicodeDecodeError as error:
            raise TmuxError("tmux returned non-UTF-8 target metadata") from error
        lines = text.splitlines()
        if len(lines) != 1:
            raise TmuxError("tmux returned an invalid target record count")
        parts = lines[0].split("\t")
        if len(parts) != 10:
            raise TmuxError("tmux returned an invalid target record")
        socket_path, session, window, pane, attached, *metadata = parts
        if attached not in {"0", "1"}:
            raise TmuxError("tmux returned an invalid attachment value")
        return TmuxSurfaceObservation(
            TmuxLocator(socket_path, session, window, pane),
            attached == "1",
            TmuxMetadata(*(value or None for value in metadata)),
        )

    def inspect_locator(self, locator: TmuxLocator) -> TmuxSurfaceObservation:
        observation = self.inspect_pane(locator.socket, locator.pane)
        if observation.locator != locator:
            raise TmuxTargetMissing("tmux target identity changed")
        return observation

    def set_metadata(
        self,
        locator: TmuxLocator,
        *,
        surface_id: str,
        session_key: str | None,
        provider: str,
        launch_id: str | None,
        role: str,
    ) -> None:
        values = {
            "surface_id": _bounded_text(surface_id, "surface ID"),
            "session_key": session_key,
            "provider": _bounded_text(provider, "provider", maximum=128),
            "launch_id": launch_id,
            "role": _bounded_text(role, "surface role", maximum=128),
        }
        for name in ("session_key", "launch_id"):
            value = values[name]
            if value is not None:
                values[name] = _bounded_text(value, name.replace("_", " "))
        previous = self.inspect_locator(locator).metadata
        previous_values = {
            "surface_id": previous.surface_id,
            "session_key": previous.session_key,
            "provider": previous.provider,
            "launch_id": previous.launch_id,
            "role": previous.role,
        }
        changed: list[str] = []
        try:
            for name, option in _METADATA_OPTIONS.items():
                self._set_pane_option(locator, option, values[name])
                changed.append(option)
        except TmuxError:
            for option in reversed(changed):
                name = next(
                    key
                    for key, candidate in _METADATA_OPTIONS.items()
                    if candidate == option
                )
                with suppress(TmuxError):
                    self._set_pane_option(locator, option, previous_values[name])
            raise

    def _set_pane_option(
        self, locator: TmuxLocator, option: str, value: str | None
    ) -> None:
        arguments = ["set-option", "-p"]
        if value is None:
            arguments.append("-u")
        arguments.extend(("-t", locator.pane, option))
        if value is not None:
            arguments.append(value)
        self._run(self._tmux(locator.socket, *arguments))

    def clear_metadata(self, locator: TmuxLocator, *, surface_id: str) -> None:
        try:
            observed = self.inspect_locator(locator)
        except TmuxTargetMissing:
            return
        if observed.metadata.surface_id != surface_id:
            return
        for option in _METADATA_OPTIONS.values():
            self._set_pane_option(locator, option, None)

    def create_surface(
        self,
        *,
        name: str,
        cwd: Path,
        command: Sequence[str],
        environment: Mapping[str, str],
        surface_id: str,
        session_key: str | None,
        provider: str,
        launch_id: str | None,
        role: str,
    ) -> TmuxSurfaceObservation:
        if _SESSION_NAME.fullmatch(name) is None:
            raise TmuxError("tmux session name is invalid")
        if not cwd.is_absolute() or not cwd.is_dir():
            message = "tmux working directory must be an existing absolute directory"
            raise TmuxError(message)
        if not command or any(
            not isinstance(item, str) or not item or "\x00" in item for item in command
        ):
            raise TmuxError("tmux bootstrap command must be a non-empty argv array")
        if sum(len(item) for item in command) > 128 * 1024:
            raise TmuxError("tmux bootstrap command exceeded its safe bound")
        fields = "#{socket_path}\t#{session_name}\t#{window_id}\t#{pane_id}"
        tmux = self._tmux(
            None,
            "new-session",
            "-d",
            "-P",
            "-F",
            fields,
            "-s",
            name,
            "-c",
            str(cwd),
        )
        for key, value in sorted(environment.items()):
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key
                or len(key) > 256
                or len(value) > 16 * 1024
                or "=" in key
                or "\x00" in key
                or "\x00" in value
            ):
                raise TmuxError("tmux environment contains an invalid entry")
            tmux.extend(("-e", f"{key}={value}"))
        tmux.extend(command)
        invocation = tmux
        if self._requires_systemd_run and self._systemd_run is None:
            raise TmuxError("systemd-run is required to create managed tmux surfaces")
        if isinstance(self._systemd_run, str):
            invocation = [
                self._systemd_run,
                "--user",
                "--scope",
                "--collect",
                "--quiet",
                "--",
                *tmux,
            ]
        result = self._run(invocation, timeout=TMUX_CREATE_TIMEOUT_SECONDS)
        try:
            record = result.stdout.decode("utf-8").strip().split("\t")
        except UnicodeDecodeError as error:
            raise TmuxError("tmux returned non-UTF-8 creation metadata") from error
        if len(record) != 4:
            raise TmuxError("tmux returned an invalid creation record")
        locator = TmuxLocator(record[0], record[1], record[2], record[3])
        try:
            self.set_metadata(
                locator,
                surface_id=surface_id,
                session_key=session_key,
                provider=provider,
                launch_id=launch_id,
                role=role,
            )
            return self.inspect_locator(locator)
        except TmuxError:
            self.kill_surface(locator)
            raise

    def kill_surface(self, locator: TmuxLocator) -> None:
        self._run(
            self._tmux(
                locator.socket,
                "kill-session",
                "-t",
                f"={locator.session}",
            ),
            missing_ok=True,
        )

    def attached(self, locator: TmuxLocator) -> bool:
        return self.inspect_locator(locator).client_attached

    def wait_for_client(
        self, locator: TmuxLocator, *, deadline: float, poll_seconds: float = 0.05
    ) -> bool:
        while time.monotonic() < deadline:
            if self.attached(locator):
                return True
            time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
        return False

    def clients(self, locator: TmuxLocator) -> tuple[str, ...]:
        fields = "#{client_tty}\t#{session_name}\t#{window_id}\t#{pane_id}"
        result = self._run(
            self._tmux(locator.socket, "list-clients", "-F", fields),
            missing_ok=True,
        )
        if result.returncode != 0:
            return ()
        try:
            lines = result.stdout.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise TmuxError("tmux returned non-UTF-8 client metadata") from error
        matched: list[str] = []
        for line in lines:
            parts = line.split("\t")
            if len(parts) != 4:
                raise TmuxError("tmux returned an invalid client record")
            client, session, window, pane = parts
            if (
                session == locator.session
                and window == locator.window
                and pane == locator.pane
            ):
                matched.append(client)
        return tuple(matched)

    def client_exists(self, locator: TmuxLocator, client: str) -> bool:
        _bounded_text(client, "tmux client ID", maximum=1024)
        result = self._run(
            self._tmux(locator.socket, "list-clients", "-F", "#{client_tty}"),
            missing_ok=True,
        )
        if result.returncode != 0:
            return False
        try:
            clients = result.stdout.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise TmuxError("tmux returned non-UTF-8 client metadata") from error
        return clients.count(client) == 1

    def select_surface(self, locator: TmuxLocator, *, client: str) -> None:
        if not self.client_exists(locator, client):
            raise TmuxTargetMissing("tmux client is stale or ambiguous")
        self.inspect_locator(locator)
        self._run(
            self._tmux(
                locator.socket,
                "switch-client",
                "-c",
                client,
                "-t",
                f"={locator.session}",
            )
        )
        self._run(self._tmux(locator.socket, "select-window", "-t", locator.window))
        self._run(self._tmux(locator.socket, "select-pane", "-t", locator.pane))

    @staticmethod
    def attach_argv(locator: TmuxLocator) -> list[str]:
        return [
            "tmux",
            "-S",
            locator.socket,
            "-u",
            "attach-session",
            "-t",
            f"={locator.session}",
        ]


__all__ = [
    "CommandRunner",
    "TmuxController",
    "TmuxError",
    "TmuxLocator",
    "TmuxMetadata",
    "TmuxSurfaceObservation",
    "TmuxTargetMissing",
]
