"""Production tmux mechanics for durable Phase 6 user views.

This layer owns only bounded tmux inspection and exact shell operations.  It
does not decide semantic navigation, launch providers, or mutate the registry.
Every destructive operation first revalidates Switchboard metadata and no code
path invokes ``kill-server``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import NAMESPACE_URL, uuid4, uuid5

from .domain import GenerationId, HostId, TmuxServer, TmuxServerId, ViewId, ViewMode

COMMAND_TIMEOUT_SECONDS: Final = 5.0
MAX_COMMAND_OUTPUT_BYTES: Final = 1024 * 1024
VIEW_ID_OPTION: Final = "@agent_switchboard_view_id"
FRAME_ID_OPTION: Final = "@agent_switchboard_frame_id"
SURFACE_ID_OPTION: Final = "@agent_switchboard_surface_id"
ROLE_OPTION: Final = "@agent_switchboard_role"
GENERATION_OPTION: Final = "@agent_switchboard_generation_id"
ZOOM_OPTION: Final = "@agent_switchboard_zoomed"
ROLE_SIDEBAR: Final = "sidebar"
ROLE_ACTIVE: Final = "active"
ROLE_PLACEHOLDER: Final = "placeholder"
ROLE_SURFACE: Final = "surface"


class TmuxViewError(RuntimeError):
    """An exact tmux operation failed or observed unexpected structure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ShellNames:
    view_session: str
    holding_session: str


@dataclass(frozen=True, slots=True)
class PaneObservation:
    pane_id: str
    session_name: str
    window_name: str
    window_id: str
    dead: bool
    input_off: bool
    process_id: int
    role: str | None
    view_id: str | None
    frame_id: str | None
    surface_id: str | None
    generation_id: str | None
    left: int
    top: int
    width: int
    height: int

    @property
    def geometry(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)


@dataclass(frozen=True, slots=True)
class ShellObservation:
    names: ShellNames
    mode: ViewMode
    active: PaneObservation
    sidebar: PaneObservation | None
    holding_panes: tuple[PaneObservation, ...]
    zoomed: bool


class TmuxExecutor:
    """One exact tmux server connection using fixed argv and bounded output."""

    def __init__(
        self,
        socket_path: str | Path | None = None,
        *,
        executable: str = "tmux",
    ) -> None:
        self.socket_path = None if socket_path is None else str(Path(socket_path))
        self.executable = executable
        self._bootstrap_session: str | None = None

    def _argv(self, *arguments: str) -> list[str]:
        command = [self.executable]
        if self.socket_path is not None:
            command.extend(("-S", self.socket_path))
        command.extend(arguments)
        return command

    def run(
        self,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        if self.socket_path is not None:
            environment.pop("TMUX", None)
            environment.pop("TMUX_PANE", None)
        try:
            result = subprocess.run(
                self._argv(*arguments),
                check=False,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TmuxViewError("tmux_unavailable", str(error)) from error
        if (
            len(result.stdout.encode("utf-8")) > MAX_COMMAND_OUTPUT_BYTES
            or len(result.stderr.encode("utf-8")) > MAX_COMMAND_OUTPUT_BYTES
        ):
            raise TmuxViewError("tmux_output_overflow", "tmux output exceeded limit")
        if check and result.returncode != 0:
            detail = " ".join(result.stderr.strip().split())[:1024]
            raise TmuxViewError(
                "tmux_command_failed",
                detail or f"tmux exited {result.returncode}",
            )
        return result

    def server_evidence(self, host_id: HostId, *, observed_at: int) -> TmuxServer:
        result = self.run(
            "display-message",
            "-p",
            "#{socket_path}\t#{pid}\t#{start_time}",
            check=False,
        )
        if result.returncode != 0:
            # A just-killed server may briefly accept one command before its
            # socket disappears.  Retry bounded startup and evidence reads;
            # never unlink or replace an externally owned socket ourselves.
            for _attempt in range(5):
                self._bootstrap_session = None
                try:
                    self._start_bootstrap()
                except TmuxViewError:
                    time.sleep(0.05)
                    continue
                result = self.run(
                    "display-message",
                    "-p",
                    "#{socket_path}\t#{pid}\t#{start_time}",
                    check=False,
                )
                if result.returncode == 0:
                    break
                time.sleep(0.05)
            else:
                raise TmuxViewError("tmux_unavailable", "tmux server did not stabilize")
        fields = result.stdout.strip().split("\t")
        if len(fields) != 3:
            raise TmuxViewError("tmux_evidence_invalid", "tmux evidence is malformed")
        socket_path, raw_pid, raw_start = fields
        if not Path(socket_path).is_absolute():
            raise TmuxViewError(
                "tmux_evidence_invalid", "tmux socket path is not absolute"
            )
        try:
            pid = int(raw_pid)
            start = int(raw_start)
        except ValueError as error:
            raise TmuxViewError(
                "tmux_evidence_invalid", "tmux generation values are malformed"
            ) from error
        self.socket_path = socket_path
        stable = uuid5(
            NAMESPACE_URL,
            f"agent-switchboard:tmux:{host_id}:{socket_path}:{pid}:{start}",
        )
        return TmuxServer(
            TmuxServerId(stable), host_id, socket_path, pid, start, observed_at
        )

    def _start_bootstrap(self) -> None:
        if self._bootstrap_session is not None:
            return
        name = f"as-bootstrap-{uuid4().hex}"
        self.run(
            "new-session",
            "-d",
            "-s",
            name,
            "-n",
            "bootstrap",
            "sleep 86400",
        )
        self.run("set-option", "-t", name, "destroy-unattached", "off")
        self._bootstrap_session = name

    @staticmethod
    def names(prefix: str, view_id: ViewId) -> ShellNames:
        opaque = str(view_id).replace("-", "")
        return ShellNames(f"{prefix}-view-{opaque}", f"{prefix}-hold-{opaque}")

    @staticmethod
    def _command(arguments: tuple[str, ...]) -> str:
        if not arguments or any(
            not isinstance(value, str) or not value for value in arguments
        ):
            raise TmuxViewError("tmux_command_invalid", "pane command is empty")
        if any("\x00" in value for value in arguments):
            raise TmuxViewError("tmux_command_invalid", "pane command contains NUL")
        return shlex.join(arguments)

    def _set_metadata(
        self,
        pane_id: str,
        *,
        view_id: ViewId,
        generation_id: GenerationId,
        role: str,
        frame_id: str | None = None,
        surface_id: str | None = None,
    ) -> None:
        values = {
            VIEW_ID_OPTION: str(view_id),
            GENERATION_OPTION: str(generation_id),
            ROLE_OPTION: role,
            FRAME_ID_OPTION: frame_id or "",
            SURFACE_ID_OPTION: surface_id or "",
        }
        for option, value in values.items():
            self.run("set-option", "-p", "-t", pane_id, option, value)

    def _dead_placeholder_session(self, session: str, window: str) -> str:
        self.run(
            "new-session",
            "-d",
            "-x",
            "140",
            "-y",
            "40",
            "-s",
            session,
            "-n",
            window,
            "sleep 86400",
        )
        self.run("set-option", "-t", session, "status", "off")
        self.run("set-option", "-t", session, "destroy-unattached", "off")
        self.run(
            "set-window-option",
            "-t",
            f"{session}:{window}",
            "remain-on-exit",
            "on",
        )
        pane = self.run(
            "display-message", "-p", "-t", f"{session}:{window}", "#{pane_id}"
        ).stdout.strip()
        self.run("respawn-pane", "-k", "-t", pane, "/usr/bin/true")
        self._wait_dead(pane)
        return pane

    def _window_zoomed(self, pane_id: str) -> bool:
        value = self.run(
            "display-message", "-p", "-t", pane_id, "#{window_zoomed_flag}"
        ).stdout.strip()
        if value not in {"0", "1"}:
            raise TmuxViewError(
                "tmux_observation_invalid", "window zoom evidence is malformed"
            )
        return value == "1"

    def _stored_zoom(self, pane_id: str) -> bool:
        value = self.run(
            "show-window-options",
            "-v",
            "-t",
            pane_id,
            ZOOM_OPTION,
            check=False,
        ).stdout.strip()
        return value == "1"

    def _set_stored_zoom(self, pane_id: str, zoomed: bool) -> None:
        self.run(
            "set-window-option",
            "-t",
            pane_id,
            ZOOM_OPTION,
            "1" if zoomed else "0",
        )

    def _reject_independent_clients(self, session_name: str) -> None:
        result = self.run(
            "list-clients",
            "-t",
            session_name,
            "-F",
            "#{client_flags}",
            check=False,
        )
        if result.returncode != 0:
            return
        if any(
            "active-pane" in {flag.strip() for flag in row.split(",")}
            for row in result.stdout.splitlines()
        ):
            raise TmuxViewError(
                "independent_client_unsupported",
                "a client has an independent active pane",
            )

    def _wait_dead(self, pane_id: str) -> None:
        deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            result = self.run(
                "display-message",
                "-p",
                "-t",
                pane_id,
                "#{pane_dead}",
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip() == "1":
                return
            time.sleep(0.02)
        raise TmuxViewError("tmux_placeholder_timeout", "placeholder did not exit")

    def create_shell(
        self,
        *,
        prefix: str,
        generation_id: GenerationId,
        view_id: ViewId,
        frame_id: str,
        mode: ViewMode,
        sidebar_command: tuple[str, ...],
    ) -> ShellObservation:
        names = self.names(prefix, view_id)
        for session in (names.view_session, names.holding_session):
            if self.run("has-session", "-t", session, check=False).returncode == 0:
                raise TmuxViewError(
                    "view_shell_exists", "tmux view shell already exists"
                )
        main = self._dead_placeholder_session(names.view_session, "main")
        self.run(
            "set-window-option",
            "-t",
            f"{names.view_session}:main",
            "window-size",
            "latest",
        )
        self._set_stored_zoom(main, False)
        self._set_metadata(
            main,
            view_id=view_id,
            generation_id=generation_id,
            role=ROLE_PLACEHOLDER,
            frame_id=frame_id,
        )
        holding = self._dead_placeholder_session(names.holding_session, "placeholder")
        self._set_metadata(
            holding,
            view_id=view_id,
            generation_id=generation_id,
            role=ROLE_PLACEHOLDER,
        )
        if mode is ViewMode.NAVIGATOR:
            self.run(
                "respawn-pane",
                "-k",
                "-t",
                main,
                self._command(sidebar_command),
            )
            self._set_metadata(
                main,
                view_id=view_id,
                generation_id=generation_id,
                role=ROLE_SIDEBAR,
            )
            active = self.run(
                "split-window",
                "-d",
                "-h",
                "-l",
                "104",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                main,
                "/usr/bin/true",
            ).stdout.strip()
            self.run(
                "set-window-option",
                "-t",
                f"{names.view_session}:main",
                "remain-on-exit",
                "on",
            )
            self._wait_dead(active)
            self._set_metadata(
                active,
                view_id=view_id,
                generation_id=generation_id,
                role=ROLE_ACTIVE,
                frame_id=frame_id,
            )
            self.run("select-pane", "-t", active)
        else:
            self._set_metadata(
                main,
                view_id=view_id,
                generation_id=generation_id,
                role=ROLE_ACTIVE,
                frame_id=frame_id,
            )
        if self._bootstrap_session is not None:
            self.run("kill-session", "-t", self._bootstrap_session, check=False)
            self._bootstrap_session = None
        return self.inspect_shell(prefix, generation_id, view_id, mode)

    def panes(self) -> tuple[PaneObservation, ...]:
        expression = "\t".join(
            (
                "#{pane_id}",
                "#{session_name}",
                "#{window_name}",
                "#{window_id}",
                "#{pane_dead}",
                "#{pane_input_off}",
                "#{pane_pid}",
                f"#{{{ROLE_OPTION}}}",
                f"#{{{VIEW_ID_OPTION}}}",
                f"#{{{FRAME_ID_OPTION}}}",
                f"#{{{SURFACE_ID_OPTION}}}",
                f"#{{{GENERATION_OPTION}}}",
                "#{pane_left}",
                "#{pane_top}",
                "#{pane_width}",
                "#{pane_height}",
            )
        )
        result = self.run("list-panes", "-a", "-F", expression, check=False)
        if result.returncode != 0:
            return ()
        observations: list[PaneObservation] = []
        for line in result.stdout.splitlines():
            values = line.split("\t")
            if len(values) != 16:
                raise TmuxViewError("tmux_observation_invalid", "pane row is malformed")
            observations.append(
                PaneObservation(
                    values[0],
                    values[1],
                    values[2],
                    values[3],
                    values[4] == "1",
                    values[5] == "1",
                    int(values[6]),
                    values[7] or None,
                    values[8] or None,
                    values[9] or None,
                    values[10] or None,
                    values[11] or None,
                    int(values[12]),
                    int(values[13]),
                    int(values[14]),
                    int(values[15]),
                )
            )
        return tuple(observations)

    def inspect_shell(
        self,
        prefix: str,
        generation_id: GenerationId,
        view_id: ViewId,
        mode: ViewMode,
    ) -> ShellObservation:
        names = self.names(prefix, view_id)
        windows = self.run(
            "list-windows",
            "-t",
            names.view_session,
            "-F",
            "#{window_name}",
            check=False,
        )
        if windows.returncode != 0 or windows.stdout.splitlines() != ["main"]:
            raise TmuxViewError(
                "view_shell_invalid", "attached view must expose only main"
            )
        candidates = [
            pane
            for pane in self.panes()
            if pane.view_id == str(view_id) and pane.generation_id == str(generation_id)
        ]
        main = [
            pane
            for pane in candidates
            if pane.session_name == names.view_session and pane.window_name == "main"
        ]
        holding = tuple(
            pane for pane in candidates if pane.session_name == names.holding_session
        )
        sidebar = next((pane for pane in main if pane.role == ROLE_SIDEBAR), None)
        active_rows = [pane for pane in main if pane.role != ROLE_SIDEBAR]
        expected_count = 2 if mode is ViewMode.NAVIGATOR else 1
        if len(main) != expected_count or len(active_rows) != 1 or not holding:
            raise TmuxViewError(
                "view_shell_invalid", "view pane topology does not match mode"
            )
        if (mode is ViewMode.NAVIGATOR) != (sidebar is not None):
            raise TmuxViewError("view_shell_invalid", "sidebar does not match mode")
        return ShellObservation(
            names,
            mode,
            active_rows[0],
            sidebar,
            holding,
            self._window_zoomed(active_rows[0].pane_id),
        )

    def spawn_surface(
        self,
        *,
        prefix: str,
        generation_id: GenerationId,
        view_id: ViewId,
        frame_id: str,
        surface_id: str,
        command: tuple[str, ...],
    ) -> PaneObservation:
        names = self.names(prefix, view_id)
        pane = self.run(
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            f"{names.holding_session}:",
            "-n",
            f"staged-{surface_id.replace('-', '')}",
            self._command(command),
        ).stdout.strip()
        self.run("set-window-option", "-t", pane, "remain-on-exit", "on")
        self._set_metadata(
            pane,
            view_id=view_id,
            generation_id=generation_id,
            role=ROLE_SURFACE,
            frame_id=frame_id,
            surface_id=surface_id,
        )
        self.run("select-pane", "-d", "-t", pane)
        return self._pane(pane)

    def launch_surface(
        self,
        *,
        generation_id: GenerationId,
        view_id: ViewId,
        frame_id: str,
        surface_id: str,
        pane_id: str,
        command: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
    ) -> PaneObservation:
        """Exec one authorized provider command in an exact presented pane."""

        target = self._pane(pane_id)
        if (
            target.view_id != str(view_id)
            or target.generation_id != str(generation_id)
            or target.frame_id != frame_id
            or target.surface_id != surface_id
        ):
            raise TmuxViewError(
                "surface_authority", "provider bootstrap pane authority differs"
            )
        if not Path(cwd).is_absolute():
            raise TmuxViewError("surface_cwd_invalid", "provider cwd is not absolute")
        arguments = ["respawn-pane", "-k", "-t", pane_id, "-c", str(cwd)]
        for key, value in sorted(environment.items()):
            if (
                not key
                or "=" in key
                or "\x00" in key
                or not isinstance(value, str)
                or "\x00" in value
            ):
                raise TmuxViewError(
                    "surface_environment_invalid", "provider environment is invalid"
                )
            arguments.extend(("-e", f"{key}={value}"))
        arguments.append(self._command(command))
        self.run(*arguments)
        self.run("select-pane", "-e", "-t", pane_id)
        deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            observed = self._pane(pane_id)
            if not observed.dead and not observed.input_off:
                return observed
            time.sleep(0.02)
        raise TmuxViewError(
            "surface_start_uncertain", "provider process did not become observable"
        )

    def set_pane_input(
        self,
        *,
        generation_id: GenerationId,
        view_id: ViewId,
        pane_id: str,
        enabled: bool,
    ) -> PaneObservation:
        target = self._pane(pane_id)
        if target.view_id != str(view_id) or target.generation_id != str(generation_id):
            raise TmuxViewError("pane_authority", "pane authority differs")
        self.run("select-pane", "-e" if enabled else "-d", "-t", pane_id)
        observed = self._pane(pane_id)
        if observed.input_off == enabled:
            raise TmuxViewError(
                "pane_input_uncertain", "pane input fencing did not settle"
            )
        return observed

    def submit_control_prompt(
        self,
        *,
        generation_id: GenerationId,
        view_id: ViewId,
        pane_id: str,
        literal: str,
    ) -> PaneObservation:
        """Submit one fixed literal in one tmux queue, then input-fence it."""

        target = self._pane(pane_id)
        if (
            target.view_id != str(view_id)
            or target.generation_id != str(generation_id)
            or target.dead
            or not target.input_off
        ):
            raise TmuxViewError(
                "control_target_unready", "control target is not exact and fenced"
            )
        self.run(
            "select-pane",
            "-e",
            "-t",
            pane_id,
            ";",
            "send-keys",
            "-t",
            pane_id,
            "-l",
            literal,
            ";",
            "send-keys",
            "-t",
            pane_id,
            "Enter",
            ";",
            "select-pane",
            "-d",
            "-t",
            pane_id,
        )
        observed = self._pane(pane_id)
        if not observed.input_off:
            raise TmuxViewError(
                "control_submit_uncertain", "control target did not remain fenced"
            )
        return observed

    def stop_surface(
        self,
        *,
        generation_id: GenerationId,
        view_id: ViewId,
        surface_id: str,
        pane_id: str,
    ) -> PaneObservation:
        """Stop only an exact owned provider pane, retaining a dead placeholder."""

        target = self._pane(pane_id)
        if (
            target.view_id != str(view_id)
            or target.generation_id != str(generation_id)
            or target.surface_id != surface_id
        ):
            raise TmuxViewError("surface_authority", "surface pane authority differs")
        self.run("select-pane", "-d", "-t", pane_id)
        self.run("respawn-pane", "-k", "-t", pane_id, "/usr/bin/true")
        self._wait_dead(pane_id)
        return self._pane(pane_id)

    def discard_staged_surface(
        self,
        *,
        generation_id: GenerationId,
        view_id: ViewId,
        surface_id: str,
        pane_id: str,
    ) -> None:
        """Remove only an exact input-fenced staged surface pane."""

        target = self._pane(pane_id)
        if (
            target.view_id != str(view_id)
            or target.generation_id != str(generation_id)
            or target.surface_id != surface_id
            or not target.input_off
        ):
            raise TmuxViewError(
                "staged_surface_authority", "staged surface is not an exact target"
            )
        self.run("kill-pane", "-t", pane_id)
        if any(pane.pane_id == pane_id for pane in self.panes()):
            raise TmuxViewError(
                "staged_surface_cleanup_uncertain", "staged pane still exists"
            )

    def spawn_placeholder(
        self,
        *,
        prefix: str,
        generation_id: GenerationId,
        view_id: ViewId,
        frame_id: str,
    ) -> PaneObservation:
        names = self.names(prefix, view_id)
        pane = self.run(
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            f"{names.holding_session}:",
            "-n",
            f"placeholder-{frame_id.replace('-', '')}",
            "/usr/bin/true",
        ).stdout.strip()
        self.run("set-window-option", "-t", pane, "remain-on-exit", "on")
        self._wait_dead(pane)
        self._set_metadata(
            pane,
            view_id=view_id,
            generation_id=generation_id,
            role=ROLE_PLACEHOLDER,
            frame_id=frame_id,
        )
        return self._pane(pane)

    def _pane(self, pane_id: str) -> PaneObservation:
        matches = [pane for pane in self.panes() if pane.pane_id == pane_id]
        if len(matches) != 1:
            raise TmuxViewError("tmux_pane_missing", "exact pane was not observed")
        return matches[0]

    def present_surface(
        self,
        *,
        prefix: str,
        generation_id: GenerationId,
        view_id: ViewId,
        mode: ViewMode,
        surface_id: str,
    ) -> ShellObservation:
        matches = [
            pane
            for pane in self.panes()
            if pane.surface_id == surface_id
            and pane.view_id == str(view_id)
            and pane.generation_id == str(generation_id)
        ]
        if len(matches) != 1:
            raise TmuxViewError("surface_pane_missing", "surface pane is not exact")
        return self.present_pane(
            prefix=prefix,
            generation_id=generation_id,
            view_id=view_id,
            mode=mode,
            pane_id=matches[0].pane_id,
        )

    def present_pane(
        self,
        *,
        prefix: str,
        generation_id: GenerationId,
        view_id: ViewId,
        mode: ViewMode,
        pane_id: str,
    ) -> ShellObservation:
        before = self.inspect_shell(prefix, generation_id, view_id, mode)
        self._reject_independent_clients(before.names.view_session)
        target = self._pane(pane_id)
        if target.view_id != str(view_id) or target.generation_id != str(generation_id):
            raise TmuxViewError("pane_authority", "target pane authority differs")
        if target.pane_id == before.active.pane_id:
            return before
        geometry = before.active.geometry
        self.run(
            "swap-pane",
            "-d",
            "-s",
            target.pane_id,
            "-t",
            before.active.pane_id,
            ";",
            "select-pane",
            "-t",
            target.pane_id,
            ";",
            "select-pane",
            "-d",
            "-t",
            before.active.pane_id,
        )
        after = self.inspect_shell(prefix, generation_id, view_id, mode)
        if (
            after.active.pane_id != target.pane_id
            or after.active.geometry != geometry
            or after.zoomed != before.zoomed
        ):
            raise TmuxViewError(
                "surface_presentation_uncertain",
                "pane movement did not produce the intended active slot",
            )
        return after

    def set_mode(
        self,
        *,
        prefix: str,
        generation_id: GenerationId,
        view_id: ViewId,
        current_mode: ViewMode,
        target_mode: ViewMode,
        sidebar_command: tuple[str, ...],
    ) -> ShellObservation:
        before = self.inspect_shell(prefix, generation_id, view_id, current_mode)
        self._reject_independent_clients(before.names.view_session)
        if current_mode is target_mode:
            return before
        restore_zoom = before.zoomed or self._stored_zoom(before.active.pane_id)
        if current_mode is ViewMode.NAVIGATOR:
            self._set_stored_zoom(before.active.pane_id, before.zoomed)
        if target_mode is ViewMode.DIRECT:
            assert before.sidebar is not None
            if before.sidebar.role != ROLE_SIDEBAR:
                raise TmuxViewError("sidebar_identity", "sidebar metadata is missing")
            self.run("kill-pane", "-t", before.sidebar.pane_id)
        else:
            sidebar = self.run(
                "split-window",
                "-d",
                "-b",
                "-h",
                "-l",
                "32",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                before.active.pane_id,
                self._command(sidebar_command),
            ).stdout.strip()
            self._set_metadata(
                sidebar,
                view_id=view_id,
                generation_id=generation_id,
                role=ROLE_SIDEBAR,
            )
            self.run("select-pane", "-t", before.active.pane_id)
        after = self.inspect_shell(prefix, generation_id, view_id, target_mode)
        if target_mode is ViewMode.NAVIGATOR and restore_zoom and not after.zoomed:
            self.run("resize-pane", "-Z", "-t", after.active.pane_id)
            after = self.inspect_shell(prefix, generation_id, view_id, target_mode)
        if after.active.pane_id != before.active.pane_id:
            raise TmuxViewError(
                "mode_change_uncertain", "active provider pane identity changed"
            )
        if target_mode is ViewMode.NAVIGATOR and after.zoomed != restore_zoom:
            raise TmuxViewError("mode_change_uncertain", "native zoom state changed")
        return after

    def restart_sidebar(
        self,
        *,
        prefix: str,
        generation_id: GenerationId,
        view_id: ViewId,
        sidebar_command: tuple[str, ...],
    ) -> ShellObservation:
        """Restart only an observed dead navigator pane."""

        before = self.inspect_shell(prefix, generation_id, view_id, ViewMode.NAVIGATOR)
        if before.sidebar is None:
            raise TmuxViewError("sidebar_missing", "navigator sidebar is absent")
        if not before.sidebar.dead:
            return before
        self.run(
            "respawn-pane",
            "-k",
            "-t",
            before.sidebar.pane_id,
            self._command(sidebar_command),
        )
        self._set_metadata(
            before.sidebar.pane_id,
            view_id=view_id,
            generation_id=generation_id,
            role=ROLE_SIDEBAR,
        )
        after = self.inspect_shell(prefix, generation_id, view_id, ViewMode.NAVIGATOR)
        if after.active.pane_id != before.active.pane_id:
            raise TmuxViewError(
                "sidebar_restart_uncertain", "active pane identity changed"
            )
        return after

    def attach_argv(self, prefix: str, view_id: ViewId) -> tuple[str, ...]:
        names = self.names(prefix, view_id)
        if self.socket_path is None:
            raise TmuxViewError("tmux_evidence_missing", "socket path is unknown")
        return (
            self.executable,
            "-S",
            self.socket_path,
            "attach-session",
            "-t",
            f"{names.view_session}:main",
        )

    def retire_shell(
        self,
        *,
        prefix: str,
        generation_id: GenerationId,
        view_id: ViewId,
        mode: ViewMode,
    ) -> None:
        observed = self.inspect_shell(prefix, generation_id, view_id, mode)
        if any(
            pane.role == ROLE_SURFACE and not pane.dead
            for pane in (observed.active, *observed.holding_panes)
        ):
            raise TmuxViewError("live_surface", "view shell still contains a surface")
        for session in (observed.names.view_session, observed.names.holding_session):
            self.run("kill-session", "-t", session)


def process_birth_id(process_id: int) -> str | None:
    """Return Linux PID plus start ticks without exposing command-line content."""

    try:
        raw = Path(f"/proc/{process_id}/stat").read_text(encoding="ascii")
        closing = raw.rfind(")")
        fields = raw[closing + 2 :].split()
        start_ticks = fields[19]
    except (OSError, IndexError, ValueError):
        return None
    return f"{process_id}:{start_ticks}"


__all__ = [
    "FRAME_ID_OPTION",
    "GENERATION_OPTION",
    "ROLE_OPTION",
    "SURFACE_ID_OPTION",
    "VIEW_ID_OPTION",
    "ZOOM_OPTION",
    "PaneObservation",
    "ShellNames",
    "ShellObservation",
    "TmuxExecutor",
    "TmuxViewError",
    "process_birth_id",
]
