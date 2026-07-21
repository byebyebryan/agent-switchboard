"""Fail-closed actions for exact launch-owned provider sessions."""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from .domain import HostId, ProviderId, RuntimePresence, SessionKey, ValidationError
from .live import ProcessIdentityScan, scan_process_identities
from .presentation import PresentationError, actionable_surface_locator
from .protocol import (
    ErrorRecord,
    ErrorScope,
    SessionAction,
    SessionActionStatus,
)
from .storage import Registry
from .tmux import TmuxController, TmuxError, TmuxLocator

ORDERLY_EXIT_SECONDS: Final = 3.0
SIGNAL_EXIT_SECONDS: Final = 1.0
EXIT_POLL_SECONDS: Final = 0.05

Clock = Callable[[], int]
Monotonic = Callable[[], float]
Sleeper = Callable[[float], None]
ProcessScanner = Callable[[], ProcessIdentityScan]
ReconcileRuntime = Callable[[], object]
GetProcessGroup = Callable[[int], int]
KillProcessGroup = Callable[[int, int], None]


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class ManagedSessionController:
    """Stop only an exact, launch-owned provider process and tmux surface."""

    def __init__(
        self,
        registry: Registry,
        *,
        host_id: HostId | str,
        tmux: TmuxController,
        reconcile_runtime: ReconcileRuntime,
        process_scanner: ProcessScanner | None = None,
        clock: Clock = _now_ms,
        monotonic: Monotonic = time.monotonic,
        sleeper: Sleeper = time.sleep,
        getpgid: GetProcessGroup = os.getpgid,
        killpg: KillProcessGroup = os.killpg,
    ) -> None:
        self.registry = registry
        self.host_id = host_id if isinstance(host_id, HostId) else HostId(host_id)
        self.tmux = tmux
        self.reconcile_runtime = reconcile_runtime
        self.process_scanner = process_scanner or (
            lambda: scan_process_identities(proc_root=Path("/proc"), uid=os.getuid())
        )
        self.clock = clock
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.getpgid = getpgid
        self.killpg = killpg

    def stop(self, session_key: str) -> SessionAction:
        try:
            key = SessionKey.parse(session_key)
        except ValidationError:
            return self._blocked(
                session_key,
                "invalid_session",
                "The selected session identity is invalid.",
            )
        if key.host_id != self.host_id:
            return self._blocked(
                key,
                "unsupported_session",
                "Only host-local managed sessions can be stopped.",
            )

        self.reconcile_runtime()
        session = self.registry.get_session(str(key))
        if session is None:
            return self._blocked(
                key,
                "unknown_session",
                "The selected session is no longer retained.",
            )
        if session["runtime_presence"] == RuntimePresence.STOPPED.value:
            return SessionAction(
                SessionActionStatus.ALREADY_STOPPED,
                self.host_id,
                key,
            )

        validated = self._validate_owned_runtime(key, session)
        if isinstance(validated, SessionAction):
            return validated
        surface, locator, pid, birth_id = validated

        try:
            self.tmux.request_provider_exit(locator)
        except TmuxError:
            return self._blocked(
                key,
                "surface_changed",
                "The managed provider surface changed before it could be stopped.",
                retryable=True,
            )

        if not self._wait_for_exit(pid, birth_id, ORDERLY_EXIT_SECONDS):
            if not self._signal_exact_group(pid, birth_id, signal.SIGTERM):
                return self._blocked(
                    key,
                    "runtime_changed",
                    "The provider process identity changed during graceful stop.",
                    retryable=True,
                )
            if not self._wait_for_exit(pid, birth_id, SIGNAL_EXIT_SECONDS):
                if not self._signal_exact_group(pid, birth_id, signal.SIGKILL):
                    return self._blocked(
                        key,
                        "runtime_changed",
                        "The provider process identity changed during forced stop.",
                        retryable=True,
                    )
                if not self._wait_for_exit(pid, birth_id, SIGNAL_EXIT_SECONDS):
                    return self._blocked(
                        key,
                        "runtime_did_not_exit",
                        "The exact managed provider process did not exit.",
                        retryable=True,
                    )

        self.tmux.kill_surface(locator)
        self.registry.retire_surface(
            str(surface["surface_id"]),
            observed_at=max(self.clock(), int(str(surface["last_observed_at"]))),
        )
        self.reconcile_runtime()
        return SessionAction(SessionActionStatus.STOPPED, self.host_id, key)

    def _validate_owned_runtime(
        self, key: SessionKey, session: dict[str, object]
    ) -> tuple[dict[str, object], TmuxLocator, int, str] | SessionAction:
        surface_id = session.get("surface_id")
        pid = session.get("runtime_pid")
        birth_id = session.get("runtime_process_birth_id")
        if (
            session.get("runtime_presence") != RuntimePresence.LIVE.value
            or not isinstance(surface_id, str)
            or isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 1
            or not isinstance(birth_id, str)
        ):
            return self._blocked(
                key,
                "runtime_not_actionable",
                "The session lacks a complete live managed-runtime identity.",
            )
        surface = self.registry.get_surface(surface_id)
        if (
            surface is None
            or surface["host_id"] != str(self.host_id)
            or surface["provider"] != key.provider.value
            or surface["role"] != "session"
            or surface["current_session_key"] != str(key)
            or surface["binding_confidence"] != "confirmed"
            or surface["retired_at"] is not None
            or not isinstance(surface["launch_id"], str)
        ):
            return self._blocked(
                key,
                "surface_not_owned",
                "The live session is not bound to a launch-owned provider surface.",
            )
        launch = self.registry.get_launch(str(surface["launch_id"]))
        if (
            launch is None
            or launch["state"] != "bound"
            or launch["action"] not in {"new", "resume", "history"}
            or launch["host_id"] != str(self.host_id)
            or launch["provider"] != key.provider.value
            or launch["target_session_key"] != str(key)
            or launch["surface_id"] != surface_id
        ):
            return self._blocked(
                key,
                "launch_not_owned",
                "The live session does not have a matching completed launch.",
            )
        try:
            locator = actionable_surface_locator(
                self.registry,
                host_id=self.host_id,
                surface_id=surface_id,
                tmux=self.tmux,
            )
        except PresentationError:
            return self._blocked(
                key,
                "surface_changed",
                "The managed provider surface did not revalidate.",
                retryable=True,
            )
        if (
            session.get("tmux_socket") != locator.socket
            or session.get("tmux_session") != locator.session
            or session.get("tmux_window") != locator.window
            or session.get("tmux_pane") != locator.pane
        ):
            return self._blocked(
                key,
                "runtime_surface_mismatch",
                "The process and managed tmux identities disagree.",
                retryable=True,
            )
        scan = self.process_scanner()
        exact = [
            process
            for process in scan.processes
            if process.pid == pid and process.birth_id == birth_id
        ]
        if not scan.complete or len(exact) != 1:
            return self._blocked(
                key,
                "runtime_not_revalidated",
                "The exact provider process identity could not be revalidated.",
                retryable=not scan.complete,
            )
        try:
            process_group = self.getpgid(pid)
        except OSError:
            return self._blocked(
                key,
                "runtime_changed",
                "The provider process exited before stop could begin.",
                retryable=True,
            )
        if process_group != pid:
            return self._blocked(
                key,
                "unsafe_process_group",
                "The provider process does not own an isolated process group.",
            )
        return surface, locator, pid, birth_id

    def _exact_process_present(self, pid: int, birth_id: str) -> bool | None:
        scan = self.process_scanner()
        if not scan.complete:
            return None
        return any(
            process.pid == pid and process.birth_id == birth_id
            for process in scan.processes
        )

    def _wait_for_exit(self, pid: int, birth_id: str, seconds: float) -> bool:
        deadline = self.monotonic() + seconds
        while True:
            present = self._exact_process_present(pid, birth_id)
            if present is False:
                return True
            if present is None or self.monotonic() >= deadline:
                return False
            self.sleeper(min(EXIT_POLL_SECONDS, max(0.0, deadline - self.monotonic())))

    def _signal_exact_group(
        self, pid: int, birth_id: str, requested_signal: int
    ) -> bool:
        if self._exact_process_present(pid, birth_id) is not True:
            return False
        try:
            if self.getpgid(pid) != pid:
                return False
            self.killpg(pid, requested_signal)
        except OSError:
            return self._exact_process_present(pid, birth_id) is False
        return True

    def _blocked(
        self,
        key: SessionKey | str,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> SessionAction:
        parsed = key if isinstance(key, SessionKey) else self._fallback_key()
        return SessionAction(
            SessionActionStatus.BLOCKED,
            self.host_id,
            parsed,
            ErrorRecord(
                code,
                message,
                ErrorScope.SESSION,
                retryable,
                self.clock(),
                host_id=self.host_id,
                provider=parsed.provider,
                session_key=parsed,
            ),
        )

    def _fallback_key(self) -> SessionKey:
        return SessionKey.parse(
            f"{self.host_id}:{ProviderId.CLAUDE.value}:"
            "00000000-0000-4000-8000-000000000000"
        )


__all__ = [
    "EXIT_POLL_SECONDS",
    "ORDERLY_EXIT_SECONDS",
    "SIGNAL_EXIT_SECONDS",
    "ManagedSessionController",
]
