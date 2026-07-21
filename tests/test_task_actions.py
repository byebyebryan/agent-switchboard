from __future__ import annotations

from pathlib import Path

from agent_switchboard.domain import HostId, ProviderId, SessionKey
from agent_switchboard.protocol import (
    ErrorRecord,
    ErrorScope,
    RuntimeDisposition,
    SessionAction,
    SessionActionStatus,
    TaskCloseStatus,
)
from agent_switchboard.storage import Registry
from agent_switchboard.task_actions import TaskCloseController

HOST_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"
CHECKOUT_ID = "33333333-3333-4333-8333-333333333333"
TASK_ID = "44444444-4444-4444-8444-444444444444"
SESSION_ID = "55555555-5555-4555-8555-555555555555"
SESSION_KEY = f"{HOST_ID}:codex:{SESSION_ID}"


def configured_registry(path: Path, *, with_session: bool) -> Registry:
    registry = Registry(path / "switchboard.db")
    registry.upsert_host(HOST_ID, "local", is_local=True, observed_at=1)
    registry.materialize_projects(
        HOST_ID,
        [
            {
                "project_id": PROJECT_ID,
                "name": "project",
                "default_provider": "codex",
                "default_transport": "tmux",
                "checkouts": [
                    {
                        "checkout_id": CHECKOUT_ID,
                        "path": str(path),
                        "is_default": True,
                    }
                ],
            }
        ],
        observed_at=2,
    )
    registry.create_task(
        task_id=TASK_ID,
        host_id=HOST_ID,
        project_id=PROJECT_ID,
        checkout_id=CHECKOUT_ID,
        title="Close controller task",
        observed_at=3,
    )
    if with_session:
        registry.upsert_session(
            {
                "session_key": SESSION_KEY,
                "host_id": HOST_ID,
                "provider": "codex",
                "provider_session_id": SESSION_ID,
                "project_id": PROJECT_ID,
                "checkout_id": CHECKOUT_ID,
                "cwd": str(path),
                "runtime_presence": "live",
                "resumability": "resumable",
                "activity": "ready",
                "activity_reason": "turn_complete",
                "attachment": "detached",
                "metadata_source": "launch",
                "state_confidence": "confirmed",
                "first_observed_at": 4,
                "last_observed_at": 4,
            }
        )
        registry.adopt_session(
            task_id=TASK_ID,
            session_key=SESSION_KEY,
            observed_at=5,
        )
    return registry


def stopped_action() -> SessionAction:
    return SessionAction(
        SessionActionStatus.STOPPED,
        HostId(HOST_ID),
        SessionKey.parse(SESSION_KEY),
    )


def test_close_without_session_is_immediate_and_idempotent(tmp_path: Path) -> None:
    with configured_registry(tmp_path, with_session=False) as registry:
        calls: list[str] = []
        controller = TaskCloseController(
            registry,
            host_id=HOST_ID,
            reconcile_runtime=lambda: calls.append("reconcile"),
            stop_session=lambda key: calls.append(key) or stopped_action(),
            clock=lambda: 10,
        )

        first = controller.close(TASK_ID)
        second = controller.close(TASK_ID)

        assert first.status is TaskCloseStatus.CLOSED
        assert second.status is TaskCloseStatus.ALREADY_CLOSED
        assert first.runtime_disposition is RuntimeDisposition.NO_SESSION
        assert calls == ["reconcile", "reconcile"]


def test_close_preserves_handoffs_and_wrap_state_then_stops(tmp_path: Path) -> None:
    with configured_registry(tmp_path, with_session=True) as registry:
        registry.curate_session_handoff(
            SESSION_KEY,
            host_id=HOST_ID,
            summary="Prior checkpoint.",
            next_action="Close without changing it.",
            wrap=False,
            observed_at=6,
        )
        before = registry.get_session(SESSION_KEY)
        assert before is not None
        action = TaskCloseController(
            registry,
            host_id=HOST_ID,
            reconcile_runtime=lambda: None,
            stop_session=lambda _key: stopped_action(),
            clock=lambda: 10,
        ).close(TASK_ID)

        after = registry.get_session(SESSION_KEY)
        assert action.runtime_disposition is RuntimeDisposition.STOPPED
        assert after is not None and after["wrapped_at"] == before["wrapped_at"]
        assert len(registry.list_handoffs(SESSION_KEY)) == 1


def test_close_succeeds_with_warning_when_live_runtime_is_retained(
    tmp_path: Path,
) -> None:
    with configured_registry(tmp_path, with_session=True) as registry:
        key = SessionKey.parse(SESSION_KEY)
        blocked = SessionAction(
            SessionActionStatus.BLOCKED,
            HostId(HOST_ID),
            key,
            ErrorRecord(
                "surface_not_owned",
                "The runtime is not safely owned.",
                ErrorScope.SESSION,
                False,
                8,
                host_id=HostId(HOST_ID),
                provider=ProviderId.CODEX,
                session_key=key,
            ),
        )
        action = TaskCloseController(
            registry,
            host_id=HOST_ID,
            reconcile_runtime=lambda: None,
            stop_session=lambda _key: blocked,
            clock=lambda: 10,
        ).close(TASK_ID)

        assert action.status is TaskCloseStatus.CLOSED
        assert action.runtime_disposition is RuntimeDisposition.RETAINED
        assert action.warning is not None
        task = registry.get_task(TASK_ID)
        assert task is not None and task["status"] == "closed"
