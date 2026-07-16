from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from agent_switchboard.domain import HostId, PresentationContext
from agent_switchboard.presentation import (
    LaunchCoordinator,
    PresentationError,
    actionable_surface_locator,
    attach_surface_argv,
    select_surface,
)
from agent_switchboard.protocol import PresentationPlanKind
from agent_switchboard.storage import Registry
from agent_switchboard.tmux import (
    TmuxLocator,
    TmuxMetadata,
    TmuxSurfaceObservation,
    TmuxTargetMissing,
)

HOST_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
SECOND_SESSION_ID = "33333333-3333-4333-8333-333333333333"
SESSION_KEY = f"{HOST_ID}:codex:{SESSION_ID}"
SECOND_SESSION_KEY = f"{HOST_ID}:codex:{SECOND_SESSION_ID}"
REQUEST_ID = "44444444-4444-4444-8444-444444444444"
SOCKET = "/tmp/tmux-1000/default"
LOCATOR = TmuxLocator(SOCKET, "as-surface", "@4", "%7")


def stable_uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"presentation-test:{label}"))


class FakeTmux:
    def __init__(self) -> None:
        self.locator = LOCATOR
        self.metadata = TmuxMetadata(None, None, None, None, None)
        self.attached = False
        self.client_ids: tuple[str, ...] = ()
        self.create_calls: list[dict[str, object]] = []
        self.killed = False
        self.target_missing = False
        self.selected: tuple[TmuxLocator, str] | None = None

    def observation(self) -> TmuxSurfaceObservation:
        if self.target_missing:
            raise TmuxTargetMissing("target missing")
        return TmuxSurfaceObservation(self.locator, self.attached, self.metadata)

    def inspect_locator(self, locator: TmuxLocator) -> TmuxSurfaceObservation:
        assert locator == self.locator
        return self.observation()

    def inspect_pane(self, socket: str, target: str) -> TmuxSurfaceObservation:
        assert (socket, target) == (self.locator.socket, self.locator.pane)
        return self.observation()

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
        assert locator == self.locator
        self.metadata = TmuxMetadata(surface_id, session_key, provider, launch_id, role)

    def create_surface(self, **values: object) -> TmuxSurfaceObservation:
        self.create_calls.append(values)
        self.metadata = TmuxMetadata(
            str(values["surface_id"]),
            str(values["session_key"]),
            str(values["provider"]),
            str(values["launch_id"]),
            str(values["role"]),
        )
        return self.observation()

    def kill_surface(self, locator: TmuxLocator) -> None:
        assert locator == self.locator
        self.killed = True

    def client_exists(self, locator: TmuxLocator, client: str) -> bool:
        assert locator == self.locator
        return client in self.client_ids

    def clients(self, locator: TmuxLocator) -> tuple[str, ...]:
        assert locator == self.locator
        return self.client_ids

    def wait_for_client(self, locator: TmuxLocator, *, deadline: float) -> bool:
        assert locator == self.locator
        assert deadline > 0
        return self.attached

    def select_surface(self, locator: TmuxLocator, *, client: str) -> None:
        self.selected = (locator, client)

    @staticmethod
    def attach_argv(locator: TmuxLocator) -> list[str]:
        return ["tmux", "-S", locator.socket, "-u", "attach-session"]


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    value = Registry(tmp_path / "switchboard.db")
    value.upsert_host(HOST_ID, "local", is_local=True, observed_at=1)
    yield value
    value.close()


def add_session(
    registry: Registry,
    cwd: Path,
    *,
    session_key: str = SESSION_KEY,
    session_id: str = SESSION_ID,
    runtime_presence: str = "stopped",
    resumability: str = "resumable",
    tmux: FakeTmux | None = None,
) -> dict[str, object]:
    stored = registry.upsert_session(
        {
            "session_key": session_key,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": session_id,
            "cwd": str(cwd),
            "runtime_presence": runtime_presence,
            "resumability": resumability,
            "activity": "ready",
            "activity_reason": "turn_complete",
            "attachment": "detached" if runtime_presence == "live" else "none",
            "metadata_source": "provider",
            "state_confidence": "confirmed",
            "first_observed_at": 1,
            "last_observed_at": 1,
        }
    )
    if tmux is not None:
        registry.connection.execute(
            """
            UPDATE sessions
            SET tmux_socket = ?, tmux_session = ?, tmux_window = ?, tmux_pane = ?
            WHERE session_key = ?
            """,
            (
                tmux.locator.socket,
                tmux.locator.session,
                "4",
                tmux.locator.pane,
                session_key,
            ),
        )
        stored = registry.get_session(session_key)
        assert stored is not None
    return stored


def coordinator(
    registry: Registry, tmux: FakeTmux, *, clock: int = 100
) -> LaunchCoordinator:
    return LaunchCoordinator(
        registry,
        host_id=HostId(HOST_ID),
        tmux=tmux,  # type: ignore[arg-type]
        swbctl_executable="/opt/swbctl",
        codex_executable="/opt/codex",
        launch_timeout_seconds=2,
        clock=lambda: clock,
        sleeper=lambda _seconds: None,
    )


DMS_CONTEXT = PresentationContext(False, None, True, True)
ATTACH_CONTEXT = PresentationContext(False, None, False, True)


def test_parked_open_creates_one_waiting_surface_and_is_idempotent(
    registry: Registry, tmp_path: Path
) -> None:
    tmux = FakeTmux()
    add_session(registry, tmp_path)
    launch = coordinator(registry, tmux)

    plan = launch.prepare_open(
        SESSION_KEY,
        request_id=REQUEST_ID,
        context=DMS_CONTEXT,
    )

    assert plan.kind is PresentationPlanKind.ATTACH
    assert plan.lease_expires_at == 2_100
    assert plan.desktop_token == f"surface:{plan.surface_id}"
    launches = registry.list_launches(target_session_key=SESSION_KEY)
    assert len(launches) == 1
    assert launches[0]["state"] == "waiting_for_client"
    assert len(tmux.create_calls) == 1
    create = tmux.create_calls[0]
    assert create["command"] == (
        "/opt/swbctl",
        "bootstrap",
        launches[0]["launch_id"],
    )
    assert create["environment"] == {
        "AGENT_SWITCHBOARD_LAUNCH_ID": launches[0]["launch_id"],
        "AGENT_SWITCHBOARD_SURFACE_ID": str(plan.surface_id),
    }

    retry = launch.prepare_open(
        SESSION_KEY,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )
    assert retry.surface_id == plan.surface_id
    assert len(tmux.create_calls) == 1


def test_request_conflict_is_a_blocked_plan(registry: Registry, tmp_path: Path) -> None:
    tmux = FakeTmux()
    add_session(registry, tmp_path)
    add_session(
        registry,
        tmp_path,
        session_key=SECOND_SESSION_KEY,
        session_id=SECOND_SESSION_ID,
    )
    launch = coordinator(registry, tmux)
    launch.prepare_open(SESSION_KEY, request_id=REQUEST_ID, context=ATTACH_CONTEXT)

    blocked = launch.prepare_open(
        SECOND_SESSION_KEY,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )

    assert blocked.kind is PresentationPlanKind.BLOCKED
    assert blocked.error is not None and blocked.error.code == "request_conflict"
    assert len(tmux.create_calls) == 1


def test_live_unmanaged_session_is_blocked_without_duplicate_launch(
    registry: Registry, tmp_path: Path
) -> None:
    tmux = FakeTmux()
    add_session(registry, tmp_path, runtime_presence="live")

    blocked = coordinator(registry, tmux).prepare_open(
        SESSION_KEY,
        request_id=REQUEST_ID,
        context=DMS_CONTEXT,
    )

    assert blocked.kind is PresentationPlanKind.BLOCKED
    assert blocked.error is not None and blocked.error.code == "unmanaged_surface"
    assert registry.list_launches() == []
    assert tmux.create_calls == []


def test_live_tmux_session_is_adopted_and_focused(
    registry: Registry, tmp_path: Path
) -> None:
    tmux = FakeTmux()
    tmux.attached = True
    tmux.client_ids = ("/dev/pts/8",)
    add_session(registry, tmp_path, runtime_presence="live", tmux=tmux)

    plan = coordinator(registry, tmux).prepare_open(
        SESSION_KEY,
        request_id=REQUEST_ID,
        context=DMS_CONTEXT,
    )

    assert plan.kind is PresentationPlanKind.FOCUS
    assert plan.surface_id is not None
    surface = registry.get_surface(str(plan.surface_id))
    assert surface is not None
    assert surface["current_session_key"] == SESSION_KEY
    assert surface["binding_confidence"] == "confirmed"
    assert tmux.metadata.surface_id == str(plan.surface_id)
    assert tmux.metadata.session_key == SESSION_KEY
    assert registry.list_launches() == []


def test_existing_surface_switches_only_the_supplied_live_client(
    registry: Registry, tmp_path: Path
) -> None:
    tmux = FakeTmux()
    tmux.attached = True
    tmux.client_ids = ("/dev/pts/8",)
    add_session(registry, tmp_path, runtime_presence="live", tmux=tmux)
    launch = coordinator(registry, tmux)
    adopted = launch.prepare_open(
        SESSION_KEY,
        request_id=REQUEST_ID,
        context=DMS_CONTEXT,
    )
    assert adopted.surface_id is not None

    switched = launch.prepare_open(
        SESSION_KEY,
        request_id=stable_uuid("switch-request"),
        context=PresentationContext(True, "/dev/pts/8", False, False),
    )

    assert switched.kind is PresentationPlanKind.SWITCH
    assert switched.surface_id == adopted.surface_id
    assert switched.tmux_client == "/dev/pts/8"

    stale = launch.prepare_open(
        SESSION_KEY,
        request_id=stable_uuid("stale-client-request"),
        context=PresentationContext(True, "/dev/pts/9", False, False),
    )
    assert stale.kind is PresentationPlanKind.BLOCKED
    assert stale.error is not None and stale.error.code == "tmux_client_stale"


def test_bootstrap_expires_without_a_client(registry: Registry, tmp_path: Path) -> None:
    tmux = FakeTmux()
    add_session(registry, tmp_path)
    launch = coordinator(registry, tmux)
    launch.prepare_open(SESSION_KEY, request_id=REQUEST_ID, context=ATTACH_CONTEXT)
    launch_id = str(registry.list_launches()[0]["launch_id"])

    result = launch.bootstrap(launch_id)

    assert result == 1
    assert registry.get_launch(launch_id)["state"] == "expired"


class ExecCaptured(RuntimeError):
    pass


def test_bootstrap_starts_exact_resume_only_after_attachment(
    registry: Registry, tmp_path: Path
) -> None:
    tmux = FakeTmux()
    add_session(registry, tmp_path)
    launch = coordinator(registry, tmux)
    launch.prepare_open(SESSION_KEY, request_id=REQUEST_ID, context=ATTACH_CONTEXT)
    launch_id = str(registry.list_launches()[0]["launch_id"])
    tmux.attached = True
    captured: list[object] = []

    def capture(executable: str, argv: Sequence[str]) -> None:
        captured.extend((executable, tuple(argv)))
        raise ExecCaptured

    with pytest.raises(ExecCaptured):
        launch.bootstrap(launch_id, exec_provider=capture)  # type: ignore[arg-type]

    assert captured == [
        "/opt/codex",
        ("/opt/codex", "resume", SESSION_ID),
    ]
    assert registry.get_launch(launch_id)["state"] == "provider_started"


def test_bootstrap_final_reconciliation_refuses_a_duplicate_runtime(
    registry: Registry, tmp_path: Path
) -> None:
    tmux = FakeTmux()
    add_session(registry, tmp_path)
    launch = coordinator(registry, tmux)
    launch.prepare_open(SESSION_KEY, request_id=REQUEST_ID, context=ATTACH_CONTEXT)
    launch_id = str(registry.list_launches()[0]["launch_id"])
    tmux.attached = True

    def discover_duplicate() -> None:
        registry.upsert_session(
            {
                "session_key": SESSION_KEY,
                "runtime_presence": "live",
                "last_observed_at": 2,
            }
        )

    result = launch.bootstrap(launch_id, reconcile_runtime=discover_duplicate)

    assert result == 1
    failed = registry.get_launch(launch_id)
    assert failed["state"] == "failed"
    assert failed["failure_code"] == "duplicate_runtime_detected"


def test_surface_actions_revalidate_stored_identity_and_pending_lease(
    registry: Registry, tmp_path: Path
) -> None:
    tmux = FakeTmux()
    add_session(registry, tmp_path)
    now = time.time_ns() // 1_000_000
    launch = coordinator(registry, tmux, clock=now)
    plan = launch.prepare_open(
        SESSION_KEY, request_id=REQUEST_ID, context=ATTACH_CONTEXT
    )
    assert plan.surface_id is not None

    locator = actionable_surface_locator(
        registry,
        host_id=HOST_ID,
        surface_id=str(plan.surface_id),
        tmux=tmux,  # type: ignore[arg-type]
        observed_at=now + 100,
    )
    assert locator == LOCATOR
    assert attach_surface_argv(
        registry,
        host_id=HOST_ID,
        surface_id=str(plan.surface_id),
        tmux=tmux,  # type: ignore[arg-type]
    ) == ["tmux", "-S", SOCKET, "-u", "attach-session"]
    select_surface(
        registry,
        host_id=HOST_ID,
        surface_id=str(plan.surface_id),
        client="/dev/pts/8",
        tmux=tmux,  # type: ignore[arg-type]
    )
    assert tmux.selected == (LOCATOR, "/dev/pts/8")

    with pytest.raises(PresentationError, match="lease has expired"):
        actionable_surface_locator(
            registry,
            host_id=HOST_ID,
            surface_id=str(plan.surface_id),
            tmux=tmux,  # type: ignore[arg-type]
            observed_at=now + 2_000,
        )
