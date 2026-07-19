from __future__ import annotations

from io import BytesIO

import pytest

from agent_switchboard.curation import (
    CurationError,
    detail_envelope,
    read_handoff_input,
    read_session_detail,
    resolve_current_session_key,
)
from agent_switchboard.storage import Registry
from agent_switchboard.tmux import (
    TmuxLocator,
    TmuxMetadata,
    TmuxSurfaceObservation,
)

HOST_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
SESSION_KEY = f"{HOST_ID}:codex:{SESSION_ID}"
SURFACE_ID = "33333333-3333-4333-8333-333333333333"
HANDOFF_ID = "44444444-4444-4444-8444-444444444444"
LOCATOR = TmuxLocator("/tmp/tmux-test", "as-test", "@1", "%1")


class CurrentPane:
    def __init__(self, observed: TmuxSurfaceObservation | None) -> None:
        self.observed = observed

    def current_pane(self, environment: object) -> TmuxSurfaceObservation | None:
        del environment
        return self.observed


@pytest.fixture
def registry(tmp_path) -> Registry:
    value = Registry(tmp_path / "switchboard.db")
    value.upsert_host(HOST_ID, "local", is_local=True, observed_at=1)
    value.upsert_session(
        {
            "session_key": SESSION_KEY,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": SESSION_ID,
            "name": "curated test",
            "cwd": "/work/test",
            "first_observed_at": 2,
            "last_observed_at": 2,
        }
    )
    yield value
    value.close()


def bind_surface(registry: Registry) -> TmuxSurfaceObservation:
    registry.adopt_bound_surface(
        {
            "surface_id": SURFACE_ID,
            "host_id": HOST_ID,
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": LOCATOR.to_storage(),
            "workspace_id": LOCATOR.session,
            "role": "session",
            "created_at": 3,
            "last_observed_at": 3,
        },
        SESSION_KEY,
        observed_at=3,
    )
    return TmuxSurfaceObservation(
        LOCATOR,
        True,
        TmuxMetadata(SURFACE_ID, SESSION_KEY, "codex", None, "session"),
    )


def test_detail_projection_contains_only_bounded_public_records(
    registry: Registry,
) -> None:
    registry.curate_session_handoff(
        SESSION_KEY,
        host_id=HOST_ID,
        handoff_id=HANDOFF_ID,
        summary="Summary.\nExplicit detail.",
        next_action="Continue.\tThen verify.",
        observed_at=4,
    )
    detail = read_session_detail(
        registry, host_id=HOST_ID, session_key=SESSION_KEY, generated_at=5
    )
    assert detail.generated_at == 5
    assert detail.session["sessionKey"] == SESSION_KEY
    assert detail.handoffs[0]["handoffId"] == HANDOFF_ID
    assert detail.handoffs[0]["summary"] == "Summary.\nExplicit detail."
    assert "nameSource" not in detail.session
    assert "transportLocator" not in detail.session

    rows = registry.read_session_detail(SESSION_KEY, host_id=HOST_ID)
    assert detail_envelope(rows, generated_at=5) == detail


def test_handoff_input_is_strict_bounded_and_canonical() -> None:
    parsed = read_handoff_input(
        BytesIO(
            b'{"handoffId":"44444444-4444-4444-8444-444444444444",'
            b'"summary":"  Done.  ","nextAction":"  Continue.  "}'
        )
    )
    assert parsed.handoff_id == HANDOFF_ID
    assert parsed.summary == "Done."
    assert parsed.next_action == "Continue."

    with pytest.raises(CurationError, match="unknown fields"):
        read_handoff_input(
            BytesIO(b'{"summary":"x","nextAction":"y","source":"agent"}')
        )
    with pytest.raises(CurationError, match="missing fields"):
        read_handoff_input(BytesIO(b'{"summary":"x"}'))
    with pytest.raises(CurationError, match="control"):
        read_handoff_input(BytesIO(b'{"summary":"bad\\u001bvalue","nextAction":"y"}'))
    with pytest.raises(CurationError, match="canonical"):
        read_handoff_input(
            BytesIO(
                b'{"handoffId":"{44444444-4444-4444-8444-444444444444}",'
                b'"summary":"x","nextAction":"y"}'
            )
        )


def test_current_session_requires_exact_confirmed_inherited_pane(
    registry: Registry,
) -> None:
    observed = bind_surface(registry)
    resolved = resolve_current_session_key(
        registry,
        host_id=HOST_ID,
        environment={"TMUX": "opaque"},
        tmux=CurrentPane(observed),  # type: ignore[arg-type]
    )
    assert str(resolved) == SESSION_KEY

    changed = TmuxSurfaceObservation(
        TmuxLocator(LOCATOR.socket, LOCATOR.session, LOCATOR.window, "%2"),
        True,
        observed.metadata,
    )
    with pytest.raises(CurationError, match="changed or is untrusted"):
        resolve_current_session_key(
            registry,
            host_id=HOST_ID,
            environment={"TMUX": "opaque"},
            tmux=CurrentPane(changed),  # type: ignore[arg-type]
        )
    with pytest.raises(CurationError, match="not inside tmux"):
        resolve_current_session_key(
            registry,
            host_id=HOST_ID,
            environment={},
            tmux=CurrentPane(None),  # type: ignore[arg-type]
        )

    registry.retire_surface(SURFACE_ID, observed_at=4)
    with pytest.raises(CurationError, match="changed or is untrusted"):
        resolve_current_session_key(
            registry,
            host_id=HOST_ID,
            environment={"TMUX": "opaque"},
            tmux=CurrentPane(observed),  # type: ignore[arg-type]
        )
