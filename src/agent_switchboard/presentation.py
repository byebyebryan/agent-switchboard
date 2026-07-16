"""Atomic local launch preparation and waiting-bootstrap orchestration."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Never

from .domain import (
    HostId,
    PresentationContext,
    ProviderId,
    SessionKey,
    ValidationError,
)
from .protocol import (
    ErrorRecord,
    ErrorScope,
    PresentationPlan,
    PresentationPlanKind,
)
from .storage import Registry, RequestConflict, StorageError
from .tmux import (
    TmuxController,
    TmuxError,
    TmuxLocator,
    TmuxSurfaceObservation,
    TmuxTargetMissing,
)

PREPARE_CAPABILITY_HASH = hashlib.sha256(
    b"agent-switchboard:phase-3a:local-codex-existing-session"
).hexdigest()
PREPARE_SURFACE_WAIT_SECONDS = 2.0
BOOTSTRAP_START_WAIT_SECONDS = 5.0

Clock = Callable[[], int]
Sleeper = Callable[[float], None]
ExecProvider = Callable[[str, Sequence[str]], Never]
ReconcileRuntime = Callable[[], object]


class PresentationError(RuntimeError):
    """A local presentation action could not be prepared safely."""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _exec_provider(executable: str, argv: Sequence[str]) -> Never:
    os.execvp(executable, list(argv))


class LaunchCoordinator:
    """Prepare existing local Codex sessions without duplicating runtimes."""

    def __init__(
        self,
        registry: Registry,
        *,
        host_id: HostId | str,
        tmux: TmuxController,
        swbctl_executable: str | Path,
        codex_executable: str = "codex",
        naming_prefix: str = "as",
        launch_timeout_seconds: int = 30,
        clock: Clock = _now_ms,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self.registry = registry
        self.host_id = host_id if isinstance(host_id, HostId) else HostId(host_id)
        self.tmux = tmux
        self.swbctl_executable = str(swbctl_executable)
        self.codex_executable = codex_executable
        self.naming_prefix = naming_prefix.replace(".", "-")
        self.launch_timeout_seconds = launch_timeout_seconds
        self.clock = clock
        self.sleeper = sleeper
        if not Path(self.swbctl_executable).is_absolute():
            raise PresentationError("swbctl executable must be an absolute path")
        if not self.codex_executable or "\x00" in self.codex_executable:
            raise PresentationError("Codex executable is invalid")
        if not self.naming_prefix:
            raise PresentationError("tmux naming prefix is invalid")
        if not 1 <= self.launch_timeout_seconds <= 300:
            raise PresentationError("launch timeout must be between 1 and 300 seconds")

    def prepare_open(
        self,
        session_key: str,
        *,
        request_id: str,
        context: PresentationContext,
    ) -> PresentationPlan:
        parsed_key = self._session_key(session_key)
        request_id = self._request_id(request_id)
        now = self.clock()
        if parsed_key.host_id != self.host_id:
            return self._blocked(
                "remote_open_not_supported",
                "This phase can open only sessions owned by the local host.",
                parsed_key,
                now,
            )
        if parsed_key.provider is not ProviderId.CODEX:
            return self._blocked(
                "provider_open_not_supported",
                "This phase can open only existing Codex sessions.",
                parsed_key,
                now,
            )
        session = self.registry.get_session(str(parsed_key))
        if session is None:
            return self._blocked(
                "session_not_found",
                "The selected session is not present in the local registry.",
                parsed_key,
                now,
            )

        existing = self._existing_surface_plan(session, parsed_key, context, now)
        if existing is not None:
            return existing
        if session["runtime_presence"] == "live":
            return self._adopt_live_surface(session, parsed_key, context, now)
        if session["resumability"] != "resumable":
            return self._blocked(
                "session_not_resumable",
                "The selected Codex session is not currently resumable.",
                parsed_key,
                now,
            )
        cwd = session.get("cwd")
        if (
            not isinstance(cwd, str)
            or not Path(cwd).is_absolute()
            or not Path(cwd).is_dir()
        ):
            return self._blocked(
                "working_directory_unavailable",
                "The selected session's working directory is unavailable.",
                parsed_key,
                now,
            )
        return self._prepare_resume(
            session,
            parsed_key,
            request_id=request_id,
            context=context,
            now=now,
        )

    @staticmethod
    def _session_key(value: str) -> SessionKey:
        parsed = SessionKey.parse(value)
        if str(parsed) != value:
            raise ValidationError("session key must use canonical UUID spelling")
        return parsed

    @staticmethod
    def _request_id(value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValidationError("request ID must be a non-nil UUID") from error
        if parsed.int == 0 or str(parsed) != value:
            raise ValidationError("request ID must use canonical non-nil UUID spelling")
        return value

    def _existing_surface_plan(
        self,
        session: dict[str, object],
        session_key: SessionKey,
        context: PresentationContext,
        now: int,
    ) -> PresentationPlan | None:
        surface_id = session.get("surface_id")
        if not isinstance(surface_id, str):
            return None
        surface = self.registry.get_surface(surface_id)
        if surface is None:
            return self._blocked(
                "surface_record_missing",
                "The selected session references a missing surface.",
                session_key,
                now,
                retryable=True,
            )
        if (
            surface["host_id"] != str(self.host_id)
            or surface["provider"] != "codex"
            or surface["role"] != "session"
            or surface["retired_at"] is not None
            or surface["current_session_key"] != str(session_key)
            or surface["binding_confidence"] != "confirmed"
        ):
            return self._blocked(
                "surface_binding_untrusted",
                "The selected session does not have a confirmed managed surface.",
                session_key,
                now,
                retryable=True,
            )
        try:
            locator = TmuxLocator.from_storage(surface["transport_locator"])
            observed = self.tmux.inspect_locator(locator)
        except TmuxTargetMissing:
            observed_at = max(now, int(surface["last_observed_at"]))
            self.registry.retire_surface(surface_id, observed_at=observed_at)
            return None
        except TmuxError:
            return self._blocked(
                "surface_unavailable",
                "The selected session's managed surface could not be verified.",
                session_key,
                now,
                retryable=True,
            )
        if not self._metadata_matches(
            observed,
            surface_id=surface_id,
            session_key=str(session_key),
            launch_id=surface.get("launch_id"),
        ):
            return self._blocked(
                "surface_identity_mismatch",
                "The selected tmux target no longer has the expected identity.",
                session_key,
                now,
                retryable=True,
            )
        self._refresh_attachment(surface, observed, now)
        return self._shape_plan(surface, observed, context)

    def _adopt_live_surface(
        self,
        session: dict[str, object],
        session_key: SessionKey,
        context: PresentationContext,
        now: int,
    ) -> PresentationPlan:
        socket = session.get("tmux_socket")
        pane = session.get("tmux_pane")
        if not isinstance(socket, str) or not isinstance(pane, str):
            return self._blocked(
                "unmanaged_surface",
                "The live Codex runtime has no trustworthy tmux surface locator.",
                session_key,
                now,
            )
        try:
            observed = self.tmux.inspect_pane(socket, pane)
        except TmuxError:
            return self._blocked(
                "unmanaged_surface",
                "The live Codex runtime's tmux surface could not be verified.",
                session_key,
                now,
                retryable=True,
            )
        metadata = observed.metadata
        if any(
            value is not None
            for value in (
                metadata.surface_id,
                metadata.session_key,
                metadata.provider,
                metadata.launch_id,
                metadata.role,
            )
        ):
            return self._blocked(
                "surface_identity_occupied",
                "The live tmux pane already carries unrelated surface metadata.",
                session_key,
                now,
                retryable=True,
            )

        surface_id = str(uuid.uuid4())
        surface = {
            "surface_id": surface_id,
            "host_id": str(self.host_id),
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": observed.locator.to_storage(),
            "workspace_id": observed.locator.session,
            "role": "session",
            "created_at": now,
            "last_observed_at": now,
            "client_attached": observed.client_attached,
        }
        try:
            stored = self.registry.adopt_bound_surface(
                surface,
                str(session_key),
                observed_at=now,
            )
        except (StorageError, sqlite3.Error):
            refreshed_session = self.registry.get_session(str(session_key))
            if refreshed_session is not None and refreshed_session["surface_id"]:
                existing = self._existing_surface_plan(
                    refreshed_session, session_key, context, self.clock()
                )
                if existing is not None:
                    return existing
            raise
        try:
            self.tmux.set_metadata(
                observed.locator,
                surface_id=surface_id,
                session_key=str(session_key),
                provider="codex",
                launch_id=None,
                role="session",
            )
            verified = self.tmux.inspect_locator(observed.locator)
        except TmuxError:
            self.registry.retire_surface(surface_id, observed_at=max(now, self.clock()))
            raise
        if not self._metadata_matches(
            verified,
            surface_id=surface_id,
            session_key=str(session_key),
            launch_id=None,
        ):
            self.registry.retire_surface(surface_id, observed_at=max(now, self.clock()))
            raise PresentationError("adopted tmux metadata did not revalidate")
        return self._shape_plan(stored, verified, context)

    def _prepare_resume(
        self,
        session: dict[str, object],
        session_key: SessionKey,
        *,
        request_id: str,
        context: PresentationContext,
        now: int,
    ) -> PresentationPlan:
        launch_id = str(uuid.uuid4())
        lease_owner = self._lease_owner(launch_id)
        request = {
            "host_id": str(self.host_id),
            "provider": "codex",
            "action": "resume",
            "project_id": None,
            "location_id": None,
            "cwd": None,
            "source_handoff_id": None,
            "target_session_key": str(session_key),
            "transport": "tmux",
        }
        try:
            reservation = self.registry.reserve_launch(
                request,
                request_id=request_id,
                launch_id=launch_id,
                lease_owner=lease_owner,
                capability_hash=PREPARE_CAPABILITY_HASH,
                expires_at=now + self.launch_timeout_seconds * 1_000,
                created_at=now,
            )
        except RequestConflict:
            return self._blocked(
                "request_conflict",
                "The request ID was already used for a different open action.",
                session_key,
                now,
            )
        launch = reservation.launch
        if reservation.kind != "created":
            return self._plan_for_launch(launch, session_key, context)

        surface_id = str(uuid.uuid4())
        session_name = f"{self.naming_prefix}-{surface_id[:12]}"
        try:
            observed = self.tmux.create_surface(
                name=session_name,
                cwd=Path(str(session["cwd"])),
                command=(self.swbctl_executable, "bootstrap", launch_id),
                environment={
                    "AGENT_SWITCHBOARD_LAUNCH_ID": launch_id,
                    "AGENT_SWITCHBOARD_SURFACE_ID": surface_id,
                },
                surface_id=surface_id,
                session_key=str(session_key),
                provider="codex",
                launch_id=launch_id,
                role="session",
            )
            activated = self.registry.activate_launch_surface(
                launch_id,
                {
                    "surface_id": surface_id,
                    "host_id": str(self.host_id),
                    "provider": "codex",
                    "transport": "tmux",
                    "transport_locator": observed.locator.to_storage(),
                    "workspace_id": observed.locator.session,
                    "role": "session",
                    "launch_id": launch_id,
                    "created_at": now,
                    "client_attached": observed.client_attached,
                },
                lease_owner=lease_owner,
                observed_at=max(now, self.clock()),
            )
        except (StorageError, TmuxError, sqlite3.Error) as error:
            if "observed" in locals():
                with suppress(TmuxError):
                    self.tmux.kill_surface(observed.locator)
            self._fail_launch(launch_id, lease_owner, "surface_creation_failed")
            raise PresentationError("managed tmux surface creation failed") from error
        return self._shape_plan(
            activated.surface,
            observed,
            context,
            lease_expires_at=int(activated.launch["expires_at"]),
        )

    def _plan_for_launch(
        self,
        launch: dict[str, object],
        session_key: SessionKey,
        context: PresentationContext,
    ) -> PresentationPlan:
        deadline = time.monotonic() + PREPARE_SURFACE_WAIT_SECONDS
        while launch["state"] in {"reserved", "surface_ready"}:
            if time.monotonic() >= deadline:
                return self._blocked(
                    "launch_preparing",
                    "Another request is still preparing the session surface.",
                    session_key,
                    self.clock(),
                    retryable=True,
                )
            self.sleeper(0.02)
            refreshed = self.registry.get_launch(str(launch["launch_id"]))
            if refreshed is None:
                raise PresentationError("reserved launch disappeared")
            launch = refreshed
        if launch["state"] in {"failed", "expired"}:
            code = "launch_failed" if launch["state"] == "failed" else "launch_expired"
            return self._blocked(
                code,
                "The prior session-open attempt is no longer executable.",
                session_key,
                self.clock(),
                retryable=True,
            )
        surface_id = launch.get("surface_id")
        if not isinstance(surface_id, str):
            raise PresentationError("active launch has no surface")
        surface = self.registry.get_surface(surface_id)
        if surface is None or surface["retired_at"] is not None:
            return self._blocked(
                "launch_surface_unavailable",
                "The prepared session surface is unavailable.",
                session_key,
                self.clock(),
                retryable=True,
            )
        try:
            locator = TmuxLocator.from_storage(surface["transport_locator"])
            observed = self.tmux.inspect_locator(locator)
        except TmuxError:
            return self._blocked(
                "launch_surface_unavailable",
                "The prepared session surface could not be verified.",
                session_key,
                self.clock(),
                retryable=True,
            )
        if not self._metadata_matches(
            observed,
            surface_id=surface_id,
            session_key=str(session_key),
            launch_id=str(launch["launch_id"]),
        ):
            return self._blocked(
                "surface_identity_mismatch",
                "The prepared tmux target no longer has the expected identity.",
                session_key,
                self.clock(),
                retryable=True,
            )
        lease = None if launch["state"] == "bound" else int(launch["expires_at"])
        return self._shape_plan(surface, observed, context, lease_expires_at=lease)

    @staticmethod
    def _metadata_matches(
        observed: TmuxSurfaceObservation,
        *,
        surface_id: str,
        session_key: str,
        launch_id: object,
    ) -> bool:
        metadata = observed.metadata
        return (
            metadata.surface_id == surface_id
            and metadata.session_key == session_key
            and metadata.provider == "codex"
            and metadata.launch_id == launch_id
            and metadata.role == "session"
        )

    def _refresh_attachment(
        self,
        surface: dict[str, object],
        observed: TmuxSurfaceObservation,
        now: int,
    ) -> None:
        if bool(surface["client_attached"]) == observed.client_attached:
            return
        observed_at = max(now, int(surface["last_observed_at"]) + 1)
        self.registry.upsert_surface(
            {
                "surface_id": surface["surface_id"],
                "host_id": surface["host_id"],
                "provider": surface["provider"],
                "transport": surface["transport"],
                "transport_locator": surface["transport_locator"],
                "workspace_id": surface["workspace_id"],
                "role": surface["role"],
                "launch_id": surface["launch_id"],
                "last_observed_at": observed_at,
                "client_attached": observed.client_attached,
            }
        )

    def _shape_plan(
        self,
        surface: dict[str, object],
        observed: TmuxSurfaceObservation,
        context: PresentationContext,
        *,
        lease_expires_at: int | None = None,
    ) -> PresentationPlan:
        surface_id = str(surface["surface_id"])
        locator = observed.locator
        workspace_id = (
            str(surface["workspace_id"])
            if surface.get("workspace_id") is not None
            else locator.session
        )
        desktop_token = f"surface:{surface_id}"
        if context.current_tmux_client is not None:
            if not self.tmux.client_exists(locator, context.current_tmux_client):
                session_key = observed.metadata.session_key
                assert session_key is not None
                return self._blocked(
                    "tmux_client_stale",
                    "The caller's tmux client could not be revalidated.",
                    SessionKey.parse(session_key),
                    self.clock(),
                    retryable=True,
                )
            plan = PresentationPlan(
                PresentationPlanKind.SWITCH,
                self.host_id,
                surface_id=surface_id,
                workspace_id=workspace_id,
                tmux_target=locator.to_storage(),
                tmux_client=context.current_tmux_client,
                desktop_token=(desktop_token if context.can_focus_desktop else None),
                lease_expires_at=lease_expires_at,
            )
        elif context.can_focus_desktop and len(self.tmux.clients(locator)) == 1:
            plan = PresentationPlan(
                PresentationPlanKind.FOCUS,
                self.host_id,
                surface_id=surface_id,
                workspace_id=workspace_id,
                desktop_token=desktop_token,
            )
        elif context.has_current_terminal or context.can_launch_terminal:
            plan = PresentationPlan(
                PresentationPlanKind.ATTACH,
                self.host_id,
                surface_id=surface_id,
                workspace_id=workspace_id,
                tmux_target=locator.to_storage(),
                desktop_token=(desktop_token if context.can_launch_terminal else None),
                lease_expires_at=lease_expires_at,
            )
        else:
            session_key = observed.metadata.session_key
            assert session_key is not None
            return self._blocked(
                "presentation_unavailable",
                "The caller cannot focus, switch, or attach this session surface.",
                SessionKey.parse(session_key),
                self.clock(),
                retryable=True,
            )
        plan.validate_for_context(context)
        return plan

    def _blocked(
        self,
        code: str,
        message: str,
        session_key: SessionKey,
        observed_at: int,
        *,
        retryable: bool = False,
    ) -> PresentationPlan:
        error = ErrorRecord.from_dict(
            ErrorRecord(
                code,
                message,
                ErrorScope.SESSION,
                retryable,
                observed_at,
                host_id=session_key.host_id,
                provider=session_key.provider,
                session_key=session_key,
            ).to_dict()
        )
        return PresentationPlan(PresentationPlanKind.BLOCKED, self.host_id, error=error)

    @staticmethod
    def _lease_owner(launch_id: str) -> str:
        return f"bootstrap:{launch_id}"

    def _fail_launch(self, launch_id: str, lease_owner: str, code: str) -> None:
        launch = self.registry.get_launch(launch_id)
        if launch is None or launch["state"] in {"bound", "failed", "expired"}:
            return
        observed_at = max(self.clock(), int(launch["updated_at"]))
        if observed_at >= int(launch["expires_at"]):
            with suppress(StorageError, sqlite3.Error):
                self.registry.transition_launch(
                    launch_id,
                    "expired",
                    observed_at=max(observed_at, int(launch["expires_at"])),
                )
            return
        with suppress(StorageError, sqlite3.Error):
            self.registry.transition_launch(
                launch_id,
                "failed",
                lease_owner=lease_owner,
                observed_at=observed_at,
                failure_code=code,
            )

    def bootstrap(
        self,
        launch_id: str,
        *,
        reconcile_runtime: ReconcileRuntime | None = None,
        exec_provider: ExecProvider = _exec_provider,
    ) -> int:
        launch_id = self._request_id(launch_id)
        lease_owner = self._lease_owner(launch_id)
        deadline = time.monotonic() + BOOTSTRAP_START_WAIT_SECONDS
        launch = self.registry.get_launch(launch_id)
        while launch is not None and launch["state"] in {"reserved", "surface_ready"}:
            if time.monotonic() >= deadline:
                self._fail_launch(launch_id, lease_owner, "surface_activation_timeout")
                return 1
            self.sleeper(0.02)
            launch = self.registry.get_launch(launch_id)
        if launch is None:
            raise PresentationError("unknown bootstrap launch")
        if (
            launch["host_id"] != str(self.host_id)
            or launch["provider"] != "codex"
            or launch["action"] != "resume"
        ):
            raise PresentationError("bootstrap launch is not a local Codex resume")
        if launch["state"] != "waiting_for_client":
            return 0 if launch["state"] == "bound" else 1
        surface_id = launch.get("surface_id")
        target_session_key = launch.get("target_session_key")
        if not isinstance(surface_id, str) or not isinstance(target_session_key, str):
            self._fail_launch(launch_id, lease_owner, "invalid_launch_identity")
            return 1
        surface = self.registry.get_surface(surface_id)
        if surface is None:
            self._fail_launch(launch_id, lease_owner, "surface_missing")
            return 1
        if (
            surface["host_id"] != str(self.host_id)
            or surface["provider"] != "codex"
            or surface["role"] != "session"
            or surface["launch_id"] != launch_id
            or surface["retired_at"] is not None
        ):
            self._fail_launch(launch_id, lease_owner, "surface_identity_mismatch")
            return 1
        try:
            locator = TmuxLocator.from_storage(surface["transport_locator"])
            observed = self.tmux.inspect_locator(locator)
        except TmuxError:
            self._fail_launch(launch_id, lease_owner, "surface_unavailable")
            return 1
        if not self._metadata_matches(
            observed,
            surface_id=surface_id,
            session_key=target_session_key,
            launch_id=launch_id,
        ):
            self._fail_launch(launch_id, lease_owner, "surface_identity_mismatch")
            return 1
        remaining = max(0.0, (int(launch["expires_at"]) - self.clock()) / 1_000)
        try:
            attached = self.tmux.wait_for_client(
                locator,
                deadline=time.monotonic() + remaining,
            )
        except TmuxError:
            self._fail_launch(launch_id, lease_owner, "surface_unavailable")
            return 1
        if not attached:
            observed_at = max(self.clock(), int(launch["expires_at"]))
            with suppress(StorageError, sqlite3.Error):
                self.registry.transition_launch(
                    launch_id,
                    "expired",
                    observed_at=observed_at,
                )
            return 1
        if reconcile_runtime is not None:
            try:
                reconcile_runtime()
            except (OSError, StorageError, sqlite3.Error, ValueError):
                self._fail_launch(
                    launch_id, lease_owner, "runtime_reconciliation_failed"
                )
                return 1
        session = self.registry.get_session(target_session_key)
        if session is None:
            self._fail_launch(launch_id, lease_owner, "target_session_missing")
            return 1
        if session["runtime_presence"] == "live":
            self._fail_launch(launch_id, lease_owner, "duplicate_runtime_detected")
            return 1
        transition_at = max(self.clock(), int(launch["updated_at"]))
        try:
            self.registry.transition_launch(
                launch_id,
                "provider_started",
                lease_owner=lease_owner,
                observed_at=transition_at,
            )
        except StorageError:
            self._fail_launch(launch_id, lease_owner, "provider_start_rejected")
            return 1
        parsed_key = SessionKey.parse(target_session_key)
        argv = (
            self.codex_executable,
            "resume",
            str(parsed_key.provider_session_id),
        )
        try:
            exec_provider(self.codex_executable, argv)
        except OSError:
            self._fail_launch(launch_id, lease_owner, "provider_exec_failed")
            return 1
        self._fail_launch(launch_id, lease_owner, "provider_exec_returned")
        return 1


__all__ = [
    "BOOTSTRAP_START_WAIT_SECONDS",
    "PREPARE_CAPABILITY_HASH",
    "PREPARE_SURFACE_WAIT_SECONDS",
    "LaunchCoordinator",
    "PresentationError",
]
