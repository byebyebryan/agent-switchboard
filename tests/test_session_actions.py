from __future__ import annotations

import hashlib
import signal
from pathlib import Path

import pytest

from agent_switchboard.live import ProcessIdentity, ProcessIdentityScan
from agent_switchboard.protocol import SessionActionStatus
from agent_switchboard.session_actions import ManagedSessionController
from agent_switchboard.storage import Registry
from agent_switchboard.tmux import (
    TmuxLocator,
    TmuxMetadata,
    TmuxSurfaceObservation,
)

HOST_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"
LOCATION_ID = "33333333-3333-4333-8333-333333333333"
SESSION_ID = "44444444-4444-4444-8444-444444444444"
SESSION_KEY = f"{HOST_ID}:claude:{SESSION_ID}"
REQUEST_ID = "55555555-5555-4555-8555-555555555555"
LAUNCH_ID = "66666666-6666-4666-8666-666666666666"
SURFACE_ID = "77777777-7777-4777-8777-777777777777"
LOCATOR = TmuxLocator("/tmp/switchboard-test.sock", "swb-test", "@4", "%7")
BIRTH_ID = hashlib.sha256(b"process-birth").hexdigest()
TASK_ID = "88888888-8888-4888-8888-888888888888"


class FakeTmux:
    def __init__(self) -> None:
        self.requested: list[TmuxLocator] = []
        self.killed: list[TmuxLocator] = []

    def inspect_locator(self, locator: TmuxLocator) -> TmuxSurfaceObservation:
        assert locator == LOCATOR
        return TmuxSurfaceObservation(
            LOCATOR,
            False,
            TmuxMetadata(SURFACE_ID, SESSION_KEY, "claude", LAUNCH_ID, "session"),
        )

    def request_provider_exit(self, locator: TmuxLocator) -> None:
        self.requested.append(locator)

    def kill_surface(self, locator: TmuxLocator) -> None:
        self.killed.append(locator)


class IncrementingMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        current = self.value
        self.value += 1.0
        return current


@pytest.fixture
def managed_registry(tmp_path: Path) -> Registry:
    registry = Registry(tmp_path / "switchboard.db")
    registry.upsert_host(HOST_ID, "local", is_local=True, observed_at=10)
    registry.materialize_projects(
        HOST_ID,
        [
            {
                "project_id": PROJECT_ID,
                "name": "project",
                "default_provider": "claude",
                "default_transport": "tmux",
                "checkouts": [
                    {
                        "checkout_id": LOCATION_ID,
                        "path": str(tmp_path),
                        "is_default": True,
                    }
                ],
            }
        ],
        observed_at=20,
    )
    registry.create_task(
        task_id=TASK_ID,
        host_id=HOST_ID,
        project_id=PROJECT_ID,
        checkout_id=LOCATION_ID,
        title="Managed Claude",
        observed_at=21,
    )
    registry.reserve_launch(
        {
            "host_id": HOST_ID,
            "provider": "claude",
            "action": "new",
            "project_id": PROJECT_ID,
            "task_id": TASK_ID,
            "checkout_id": LOCATION_ID,
            "cwd": str(tmp_path),
            "source_handoff_id": None,
            "target_session_key": None,
            "transport": "tmux",
        },
        launch_id=LAUNCH_ID,
        request_id=REQUEST_ID,
        lease_owner="worker",
        capability_hash="a" * 64,
        expires_at=1_000,
        created_at=100,
    )
    registry.activate_launch_surface(
        LAUNCH_ID,
        {
            "surface_id": SURFACE_ID,
            "host_id": HOST_ID,
            "provider": "claude",
            "transport": "tmux",
            "transport_locator": LOCATOR.to_storage(),
            "role": "session",
            "launch_id": LAUNCH_ID,
            "created_at": 110,
        },
        lease_owner="worker",
        observed_at=110,
    )
    registry.transition_launch(
        LAUNCH_ID,
        "provider_started",
        lease_owner="worker",
        observed_at=120,
    )
    registry.bind_provider_session(
        LAUNCH_ID,
        {
            "session_key": SESSION_KEY,
            "host_id": HOST_ID,
            "provider": "claude",
            "provider_session_id": SESSION_ID,
            "runtime_presence": "live",
            "runtime_pid": 100,
            "tmux_session": LOCATOR.session,
            "tmux_window": LOCATOR.window,
            "tmux_pane": LOCATOR.pane,
            "last_observed_at": 130,
        },
        lease_owner="worker",
        observed_at=130,
    )
    registry.connection.execute(
        """
        UPDATE sessions
        SET runtime_process_birth_id = ?, tmux_socket = ?
        WHERE session_key = ?
        """,
        (BIRTH_ID, LOCATOR.socket, SESSION_KEY),
    )
    yield registry
    registry.close()


def scan(*, present: bool) -> ProcessIdentityScan:
    processes = (ProcessIdentity(100, 1, "1000", BIRTH_ID),) if present else ()
    return ProcessIdentityScan(processes, {100: 1}, True, ())


def controller(
    registry: Registry,
    tmux: FakeTmux,
    *,
    process_scanner,
    getpgid=lambda _pid: 100,
    killpg=lambda _pid, _signal: None,
    monotonic=lambda: 0.0,
) -> ManagedSessionController:
    return ManagedSessionController(
        registry,
        host_id=HOST_ID,
        tmux=tmux,  # type: ignore[arg-type]
        reconcile_runtime=lambda: None,
        process_scanner=process_scanner,
        clock=lambda: 200,
        monotonic=monotonic,
        sleeper=lambda _seconds: None,
        getpgid=getpgid,
        killpg=killpg,
    )


def test_stop_gracefully_retires_only_exact_owned_surface(
    managed_registry: Registry,
) -> None:
    tmux = FakeTmux()
    scans = iter((scan(present=True), scan(present=False)))

    action = controller(
        managed_registry,
        tmux,
        process_scanner=lambda: next(scans),
    ).stop(SESSION_KEY)

    assert action.status is SessionActionStatus.STOPPED
    assert tmux.requested == [LOCATOR]
    assert tmux.killed == [LOCATOR]
    surface = managed_registry.get_surface(SURFACE_ID)
    assert surface is not None and surface["retired_at"] == 200


def test_stop_falls_back_to_exact_process_group_after_orderly_timeout(
    managed_registry: Registry,
) -> None:
    tmux = FakeTmux()
    alive = True
    signals: list[tuple[int, int]] = []

    def process_scanner() -> ProcessIdentityScan:
        return scan(present=alive)

    def killpg(pid: int, requested_signal: int) -> None:
        nonlocal alive
        signals.append((pid, requested_signal))
        alive = False

    action = controller(
        managed_registry,
        tmux,
        process_scanner=process_scanner,
        killpg=killpg,
        monotonic=IncrementingMonotonic(),
    ).stop(SESSION_KEY)

    assert action.status is SessionActionStatus.STOPPED
    assert signals == [(100, signal.SIGTERM)]
    assert tmux.killed == [LOCATOR]


def test_stop_blocks_when_process_group_is_not_isolated(
    managed_registry: Registry,
) -> None:
    tmux = FakeTmux()

    action = controller(
        managed_registry,
        tmux,
        process_scanner=lambda: scan(present=True),
        getpgid=lambda _pid: 99,
    ).stop(SESSION_KEY)

    assert action.status is SessionActionStatus.BLOCKED
    assert action.error is not None and action.error.code == "unsafe_process_group"
    assert tmux.requested == []
    assert tmux.killed == []
