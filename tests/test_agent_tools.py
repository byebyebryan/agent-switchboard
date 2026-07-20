from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from agent_switchboard.agent_tools import (
    AgentToolError,
    AgentToolService,
    authorize_agent,
)
from agent_switchboard.config import (
    DefaultsConfig,
    HooksConfig,
    HostConfig,
    ProjectCatalog,
    ProviderConfig,
    SwitchboardConfig,
    TmuxConfig,
)
from agent_switchboard.domain import (
    HostId,
    Project,
    ProjectId,
    ProjectLocation,
    ProviderId,
)
from agent_switchboard.local import materialize_configured_projects
from agent_switchboard.protocol import (
    AgentContextEnvelope,
    AgentMemoryEnvelope,
    AgentSearchEnvelope,
)
from agent_switchboard.storage import IdentityConflict, Registry, StorageError
from agent_switchboard.tmux import (
    TmuxLocator,
    TmuxMetadata,
    TmuxSurfaceObservation,
)

HOST_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"
LOCATION_ID = "33333333-3333-4333-8333-333333333333"
SESSION_ID = "44444444-4444-4444-8444-444444444444"
SESSION_KEY = f"{HOST_ID}:codex:{SESSION_ID}"
LAUNCH_ID = "55555555-5555-4555-8555-555555555555"
REQUEST_ID = "66666666-6666-4666-8666-666666666666"
SURFACE_ID = "77777777-7777-4777-8777-777777777777"
HANDOFF_ID = "88888888-8888-4888-8888-888888888888"
CAPABILITY = "a" * 43
LOCATOR = TmuxLocator("/tmp/agent-tools.sock", "as-session", "@1", "%1")


def stable_uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"agent-tools:{label}"))


class CurrentPane:
    def __init__(self, observed: TmuxSurfaceObservation | None) -> None:
        self.observed = observed

    def current_pane(self, environment: object) -> TmuxSurfaceObservation | None:
        del environment
        return self.observed


def config(root: Path, *sources: str) -> SwitchboardConfig:
    project = Project(
        ProjectId(PROJECT_ID),
        "Switchboard",
        aliases=("router",),
        default_provider=ProviderId.CODEX,
        context_sources=tuple(sources),
    )
    location = ProjectLocation(
        LOCATION_ID,
        PROJECT_ID,
        HOST_ID,
        root,
        display_name="main checkout",
        is_default=True,
    )
    return SwitchboardConfig(
        HostConfig(HostId(HOST_ID), "local"),
        (ProviderConfig(ProviderId.CODEX), ProviderConfig(ProviderId.CLAUDE)),
        (),
        ProjectCatalog((project,), (location,)),
        DefaultsConfig(),
        TmuxConfig(),
        HooksConfig(),
    )


def bound_agent(
    registry: Registry, root: Path, *sources: str, provider: str = "codex"
) -> tuple[SwitchboardConfig, TmuxSurfaceObservation, dict[str, str]]:
    configured = config(root, *sources)
    materialize_configured_projects(registry, HOST_ID, configured)
    session_id = SESSION_ID if provider == "codex" else stable_uuid("claude-bound")
    session_key = f"{HOST_ID}:{provider}:{session_id}"
    capability_hash = hashlib.sha256(CAPABILITY.encode("ascii")).hexdigest()
    launch = registry.reserve_launch(
        {
            "host_id": HOST_ID,
            "provider": provider,
            "action": "new",
            "project_id": PROJECT_ID,
            "location_id": LOCATION_ID,
            "cwd": str(root),
            "source_handoff_id": None,
            "target_session_key": None,
            "transport": "tmux",
        },
        request_id=REQUEST_ID,
        launch_id=LAUNCH_ID,
        lease_owner=f"bootstrap:{LAUNCH_ID}",
        capability_hash="b" * 64,
        agent_capability_hash=capability_hash,
        created_at=10,
        expires_at=1_000,
    ).launch
    registry.activate_launch_surface(
        LAUNCH_ID,
        {
            "surface_id": SURFACE_ID,
            "host_id": HOST_ID,
            "provider": provider,
            "transport": "tmux",
            "transport_locator": LOCATOR.to_storage(),
            "workspace_id": LOCATOR.session,
            "role": "session",
            "launch_id": LAUNCH_ID,
            "created_at": 11,
        },
        lease_owner=f"bootstrap:{LAUNCH_ID}",
        observed_at=11,
    )
    registry.transition_launch(
        LAUNCH_ID,
        "provider_started",
        lease_owner=f"bootstrap:{LAUNCH_ID}",
        observed_at=12,
    )
    bound = registry.bind_provider_session(
        LAUNCH_ID,
        {
            "session_key": session_key,
            "host_id": HOST_ID,
            "provider": provider,
            "provider_session_id": session_id,
            "name": "Provider title",
            "cwd": str(root),
            "last_observed_at": 13,
        },
        lease_owner=f"bootstrap:{LAUNCH_ID}",
        observed_at=13,
    )
    assert bound.launch == launch | {
        "state": "bound",
        "surface_id": SURFACE_ID,
        "target_session_key": session_key,
        "lease_owner": None,
        "updated_at": 13,
    }
    observed = TmuxSurfaceObservation(
        LOCATOR,
        True,
        TmuxMetadata(SURFACE_ID, session_key, provider, LAUNCH_ID, "session"),
    )
    environment = {
        "TMUX": "private",
        "AGENT_SWITCHBOARD_CAPABILITY": CAPABILITY,
        "AGENT_SWITCHBOARD_LAUNCH_ID": LAUNCH_ID,
        "AGENT_SWITCHBOARD_SURFACE_ID": SURFACE_ID,
    }
    return configured, observed, environment


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    value = Registry(tmp_path / "switchboard.db")
    yield value
    value.close()


def test_authorization_requires_exact_capability_launch_surface_and_pane(
    registry: Registry, tmp_path: Path
) -> None:
    _, observed, environment = bound_agent(registry, tmp_path)
    authorized = authorize_agent(
        registry,
        host_id=HOST_ID,
        environment=environment,
        tmux=CurrentPane(observed),  # type: ignore[arg-type]
    )
    assert str(authorized.session_key) == SESSION_KEY
    assert str(authorized.surface_id) == SURFACE_ID
    assert str(authorized.launch_id) == LAUNCH_ID

    variants = (
        environment | {"AGENT_SWITCHBOARD_CAPABILITY": "z" * 43},
        environment | {"AGENT_SWITCHBOARD_LAUNCH_ID": stable_uuid("other-launch")},
        environment | {"AGENT_SWITCHBOARD_SURFACE_ID": stable_uuid("other-surface")},
        {key: value for key, value in environment.items() if key != "TMUX"},
    )
    for candidate in variants:
        with pytest.raises(
            AgentToolError, match=r"^agent authorization failed$"
        ) as error:
            authorize_agent(
                registry,
                host_id=HOST_ID,
                environment=candidate,
                tmux=CurrentPane(observed if "TMUX" in candidate else None),  # type: ignore[arg-type]
            )
        assert CAPABILITY not in str(error.value)

    changed_pane = TmuxSurfaceObservation(
        TmuxLocator(LOCATOR.socket, LOCATOR.session, LOCATOR.window, "%2"),
        True,
        observed.metadata,
    )
    with pytest.raises(AgentToolError, match=r"^agent authorization failed$"):
        authorize_agent(
            registry,
            host_id=HOST_ID,
            environment=environment,
            tmux=CurrentPane(changed_pane),  # type: ignore[arg-type]
        )

    rejected_panes = (
        TmuxSurfaceObservation(
            LOCATOR,
            True,
            TmuxMetadata(SURFACE_ID, None, "codex", LAUNCH_ID, "session"),
        ),
        TmuxSurfaceObservation(
            LOCATOR,
            True,
            TmuxMetadata(
                SURFACE_ID,
                SESSION_KEY,
                "codex",
                LAUNCH_ID,
                "provider_manager",
            ),
        ),
        TmuxSurfaceObservation(
            LOCATOR,
            True,
            TmuxMetadata(
                SURFACE_ID,
                f"{HOST_ID}:claude:{stable_uuid('claude-session')}",
                "claude",
                LAUNCH_ID,
                "session",
            ),
        ),
    )
    for rejected in rejected_panes:
        with pytest.raises(AgentToolError, match=r"^agent authorization failed$"):
            authorize_agent(
                registry,
                host_id=HOST_ID,
                environment=environment,
                tmux=CurrentPane(rejected),  # type: ignore[arg-type]
            )

    registry.retire_surface(SURFACE_ID, observed_at=14)
    with pytest.raises(AgentToolError, match=r"^agent authorization failed$"):
        authorize_agent(
            registry,
            host_id=HOST_ID,
            environment=environment,
            tmux=CurrentPane(observed),  # type: ignore[arg-type]
        )


def test_authorization_supports_exact_managed_claude_new_launch(
    registry: Registry, tmp_path: Path
) -> None:
    _, observed, environment = bound_agent(registry, tmp_path, provider="claude")

    authorized = authorize_agent(
        registry,
        host_id=HOST_ID,
        environment=environment,
        tmux=CurrentPane(observed),  # type: ignore[arg-type]
    )

    assert authorized.provider is ProviderId.CLAUDE
    assert str(authorized.session_key).startswith(f"{HOST_ID}:claude:")


def test_adopted_launch_free_surface_cannot_authorize_agent_tools(
    registry: Registry, tmp_path: Path
) -> None:
    configured = config(tmp_path)
    materialize_configured_projects(registry, HOST_ID, configured)
    registry.upsert_session(
        {
            "session_key": SESSION_KEY,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": SESSION_ID,
            "project_id": PROJECT_ID,
            "location_id": LOCATION_ID,
            "name": "Adopted provider title",
            "cwd": str(tmp_path),
            "first_observed_at": 10,
            "last_observed_at": 10,
        }
    )
    registry.adopt_bound_surface(
        {
            "surface_id": SURFACE_ID,
            "host_id": HOST_ID,
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": LOCATOR.to_storage(),
            "workspace_id": LOCATOR.session,
            "role": "session",
            "created_at": 11,
            "last_observed_at": 11,
        },
        SESSION_KEY,
        observed_at=11,
    )
    observed = TmuxSurfaceObservation(
        LOCATOR,
        True,
        TmuxMetadata(SURFACE_ID, SESSION_KEY, "codex", None, "session"),
    )
    environment = {
        "TMUX": "private",
        "AGENT_SWITCHBOARD_CAPABILITY": CAPABILITY,
        "AGENT_SWITCHBOARD_LAUNCH_ID": LAUNCH_ID,
        "AGENT_SWITCHBOARD_SURFACE_ID": SURFACE_ID,
    }

    with pytest.raises(AgentToolError, match=r"^agent authorization failed$"):
        authorize_agent(
            registry,
            host_id=HOST_ID,
            environment=environment,
            tmux=CurrentPane(observed),  # type: ignore[arg-type]
        )
    stored = registry.get_session(SESSION_KEY)
    assert stored is not None
    assert stored["name"] == "Adopted provider title"


def test_agent_mutations_are_current_only_and_durably_attributed(
    registry: Registry, tmp_path: Path
) -> None:
    configured, observed, environment = bound_agent(registry, tmp_path)
    service = AgentToolService(
        registry,
        host_id=HOST_ID,
        config=configured,
        environment=environment,
        tmux=CurrentPane(observed),  # type: ignore[arg-type]
    )

    assert service.current().session["sessionKey"] == SESSION_KEY
    named = service.set_name("Agent-picked name")
    assert named.session["name"] == "Agent-picked name"
    stored = registry.get_session(SESSION_KEY)
    assert stored is not None
    assert stored["name_source"] == "curated"
    assert stored["name_actor"] == "agent"
    assert stored["last_observed_at"] == 13

    handed = service.append_handoff(
        summary="Summarize the current slice.",
        next_action="Review the agent boundary.",
        handoff_id=HANDOFF_ID,
        wrap=False,
    )
    assert handed.handoffs[0]["source"] == "agent"
    replay = service.append_handoff(
        summary="Summarize the current slice.",
        next_action="Review the agent boundary.",
        handoff_id=HANDOFF_ID,
        wrap=False,
    )
    assert len(replay.handoffs) == 1
    with pytest.raises(IdentityConflict):
        service.append_handoff(
            summary="Conflicting content.",
            next_action="Must fail.",
            handoff_id=HANDOFF_ID,
            wrap=False,
        )

    wrapped_id = stable_uuid("wrapped")
    wrapped = service.append_handoff(
        summary="The slice is ready for review.",
        next_action="Run the installed acceptance loop.",
        handoff_id=wrapped_id,
        wrap=True,
    )
    assert wrapped.session["wrappedAt"] == wrapped.handoffs[0]["createdAt"]
    assert wrapped.handoffs[0]["handoffId"] == wrapped_id

    service.set_name(None)
    cleared = registry.get_session(SESSION_KEY)
    assert cleared is not None
    assert cleared["name"] == "Provider title"
    assert cleared["name_source"] == "provider"
    assert cleared["name_actor"] is None
    registry.set_session_name(SESSION_KEY, host_id=HOST_ID, name="Human name")
    human = registry.get_session(SESSION_KEY)
    assert human is not None and human["name_actor"] == "user"


def test_context_is_bounded_same_project_and_reports_unsafe_sources(
    registry: Registry, tmp_path: Path
) -> None:
    (tmp_path / "README.md").write_text("# Switchboard\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "design.md").write_text("Design notes.\n", encoding="utf-8")
    (docs / "binary.dat").write_bytes(b"unsafe\x00data")
    outside = tmp_path.parent / f"outside-{tmp_path.name}.md"
    outside.write_text("outside", encoding="utf-8")
    os.symlink(outside, docs / "escape.md")
    configured, observed, environment = bound_agent(
        registry,
        tmp_path,
        "README.md",
        "docs",
        "missing.md",
    )
    registry.upsert_session(
        {
            "session_key": f"{HOST_ID}:codex:{stable_uuid('recent-session')}",
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": stable_uuid("recent-session"),
            "project_id": PROJECT_ID,
            "location_id": LOCATION_ID,
            "purpose": "Independent nearby work",
            "cwd": str(tmp_path),
            "first_observed_at": 20,
            "last_observed_at": 20,
        }
    )
    recent_key = f"{HOST_ID}:codex:{stable_uuid('recent-session')}"
    registry.append_handoff(
        session_key=recent_key,
        summary="Recent explicit summary.",
        source="user",
        source_host_id=HOST_ID,
        next_action="Keep it independent.",
        handoff_id=stable_uuid("recent-handoff"),
        created_at=21,
    )
    other_project = stable_uuid("other-project")
    registry.connection.execute(
        """
        INSERT INTO projects(project_id, name, created_at, updated_at)
        VALUES (?, 'Other', 1, 1)
        """,
        (other_project,),
    )
    registry.upsert_session(
        {
            "session_key": f"{HOST_ID}:codex:{stable_uuid('other-session')}",
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": stable_uuid("other-session"),
            "project_id": other_project,
            "cwd": str(tmp_path),
            "first_observed_at": 30,
            "last_observed_at": 30,
        }
    )

    service = AgentToolService(
        registry,
        host_id=HOST_ID,
        config=configured,
        environment=environment,
        tmux=CurrentPane(observed),  # type: ignore[arg-type]
    )
    context = service.context()
    reparsed = AgentContextEnvelope.from_json(context.to_json())

    assert reparsed.caller["sessionKey"] == SESSION_KEY
    assert reparsed.project["projectId"] == PROJECT_ID
    assert [source["path"] for source in reparsed.stable_sources] == [
        "README.md",
        "docs/design.md",
    ]
    assert all(
        source["contentHash"]
        == hashlib.sha256(str(source["text"]).encode("utf-8")).hexdigest()
        for source in reparsed.stable_sources
    )
    assert [session["sessionKey"] for session in reparsed.sessions] == [
        SESSION_KEY,
        recent_key,
    ]
    recent = reparsed.sessions[1]
    assert recent["latestHandoff"]["summary"] == "Recent explicit summary."
    issue_codes = {issue["code"] for issue in reparsed.issues}
    assert issue_codes == {
        "context_source_escape",
        "context_source_not_text",
        "context_source_unavailable",
    }
    assert CAPABILITY not in context.to_json()


def test_context_protocol_rejects_identity_hash_and_path_mutations(
    registry: Registry, tmp_path: Path
) -> None:
    (tmp_path / "README.md").write_text("Context.\n", encoding="utf-8")
    configured, observed, environment = bound_agent(registry, tmp_path, "README.md")
    context = AgentToolService(
        registry,
        host_id=HOST_ID,
        config=configured,
        environment=environment,
        tmux=CurrentPane(observed),  # type: ignore[arg-type]
    ).context()
    payload = context.to_dict()

    changed_hash = payload | {
        "stableSources": [dict(payload["stableSources"][0], contentHash="f" * 64)]
    }
    with pytest.raises(ValueError, match="contentHash does not match text"):
        AgentContextEnvelope.from_dict(changed_hash)

    escaped = payload | {
        "stableSources": [dict(payload["stableSources"][0], path="../README.md")]
    }
    with pytest.raises(ValueError, match="project-relative"):
        AgentContextEnvelope.from_dict(escaped)

    escaped_declaration = payload | {
        "project": dict(payload["project"], contextSources=["../README.md"])
    }
    with pytest.raises(ValueError, match="canonical project-relative"):
        AgentContextEnvelope.from_dict(escaped_declaration)

    undeclared = payload | {
        "project": dict(payload["project"], contextSources=["docs"])
    }
    with pytest.raises(ValueError, match="undeclared source"):
        AgentContextEnvelope.from_dict(undeclared)

    omitted_caller = payload | {"sessions": []}
    with pytest.raises(ValueError, match="authorized caller first"):
        AgentContextEnvelope.from_dict(omitted_caller)

    displaced_caller = payload | {
        "sessions": [
            dict(
                payload["sessions"][0],
                sessionKey=f"{HOST_ID}:codex:{stable_uuid('different-caller')}",
            ),
            payload["sessions"][0],
        ]
    }
    with pytest.raises(ValueError, match="authorized caller first"):
        AgentContextEnvelope.from_dict(displaced_caller)


def test_context_enforces_file_and_session_truncation_bounds(
    registry: Registry, tmp_path: Path
) -> None:
    (tmp_path / "large.md").write_text("x" * 70_000, encoding="utf-8")
    configured, observed, environment = bound_agent(registry, tmp_path, "large.md")
    for index in range(25):
        session_id = stable_uuid(f"bounded-session-{index}")
        registry.upsert_session(
            {
                "session_key": f"{HOST_ID}:codex:{session_id}",
                "host_id": HOST_ID,
                "provider": "codex",
                "provider_session_id": session_id,
                "project_id": PROJECT_ID,
                "location_id": LOCATION_ID,
                "cwd": str(tmp_path),
                "first_observed_at": 100 + index,
                "last_observed_at": 100 + index,
            }
        )
    context = AgentToolService(
        registry,
        host_id=HOST_ID,
        config=configured,
        environment=environment,
        tmux=CurrentPane(observed),  # type: ignore[arg-type]
    ).context()

    assert len(context.stable_sources) == 1
    assert len(str(context.stable_sources[0]["text"]).encode("utf-8")) == 64 * 1024
    assert context.stable_sources[0]["truncated"] is True
    assert context.stable_sources_truncated is True
    assert len(context.sessions) == 20
    assert context.sessions[0]["sessionKey"] == SESSION_KEY
    assert context.sessions_truncated is True


def test_context_directory_traversal_has_a_depth_bound(
    registry: Registry, tmp_path: Path
) -> None:
    root = tmp_path / "context"
    root.mkdir()
    directory = root
    for index in range(18):
        directory = directory / f"level-{index:02d}"
        directory.mkdir()
    (directory / "too-deep.md").write_text("hidden\n", encoding="utf-8")
    configured, observed, environment = bound_agent(registry, tmp_path, "context")

    context = AgentToolService(
        registry,
        host_id=HOST_ID,
        config=configured,
        environment=environment,
        tmux=CurrentPane(observed),  # type: ignore[arg-type]
    ).context()

    assert context.stable_sources == ()
    assert context.stable_sources_truncated is True
    assert [issue["code"] for issue in context.issues] == ["context_tree_truncated"]


def test_project_reads_and_search_stay_in_authorized_project(
    registry: Registry, tmp_path: Path
) -> None:
    configured, observed, environment = bound_agent(registry, tmp_path)
    nearby_id = stable_uuid("search-nearby")
    nearby_key = f"{HOST_ID}:claude:{nearby_id}"
    registry.upsert_session(
        {
            "session_key": nearby_key,
            "host_id": HOST_ID,
            "provider": "claude",
            "provider_session_id": nearby_id,
            "project_id": PROJECT_ID,
            "location_id": LOCATION_ID,
            "name": "Alignment session",
            "purpose": "Finish the 4C alignment loop",
            "cwd": str(tmp_path),
            "first_observed_at": 20,
            "last_observed_at": 20,
        }
    )
    nearby_handoff = stable_uuid("search-handoff")
    registry.append_handoff(
        session_key=nearby_key,
        summary="Alignment decisions are captured.",
        next_action="Finish the MCP acceptance loop.",
        source="user",
        source_host_id=HOST_ID,
        handoff_id=nearby_handoff,
        created_at=21,
    )
    other_project = stable_uuid("search-other-project")
    registry.connection.execute(
        """
        INSERT INTO projects(project_id, name, created_at, updated_at)
        VALUES (?, 'Other', 1, 1)
        """,
        (other_project,),
    )
    other_id = stable_uuid("search-other-session")
    other_key = f"{HOST_ID}:codex:{other_id}"
    registry.upsert_session(
        {
            "session_key": other_key,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": other_id,
            "project_id": other_project,
            "name": "Alignment must not leak",
            "cwd": str(tmp_path),
            "first_observed_at": 30,
            "last_observed_at": 30,
        }
    )
    service = AgentToolService(
        registry,
        host_id=HOST_ID,
        config=configured,
        environment=environment,
        tmux=CurrentPane(observed),  # type: ignore[arg-type]
    )

    sessions = service.list_sessions()
    assert [item["sessionKey"] for item in sessions.sessions] == [
        SESSION_KEY,
        nearby_key,
    ]
    detail = service.session_detail(nearby_key)
    assert detail.handoffs[0]["handoffId"] == nearby_handoff
    handoff = service.handoff(nearby_handoff)
    assert handoff.handoff["sessionKey"] == nearby_key
    search = service.search("alignment")
    assert {item["kind"] for item in search.results} == {"session", "handoff"}
    assert all(item["sessionKey"] != other_key for item in search.results)
    with pytest.raises(ValueError, match="too many records"):
        AgentSearchEnvelope.from_dict(
            search.to_dict() | {"results": [search.results[0]] * 21}
        )
    with pytest.raises(ValueError, match="bounded string"):
        AgentSearchEnvelope.from_dict(search.to_dict() | {"query": "x" * 257})
    with pytest.raises(ValueError, match="duplicate records"):
        AgentSearchEnvelope.from_dict(
            search.to_dict() | {"results": [search.results[0], search.results[0]]}
        )
    with pytest.raises(StorageError, match="not in the current project"):
        service.session_detail(other_key)
    missing_key = f"{HOST_ID}:codex:{stable_uuid('missing-project-session')}"
    with pytest.raises(StorageError, match=r"^session is not in the current project$"):
        service.session_detail(missing_key)

    disabled_memory = service.memory_search("alignment")
    assert disabled_memory.available is False
    assert disabled_memory.issues[0]["code"] == "memory_disabled"
    with pytest.raises(ValueError, match="oversized string"):
        AgentMemoryEnvelope.from_dict(
            disabled_memory.to_dict() | {"text": "x" * (64 * 1024 + 1)}
        )
    with pytest.raises(ValueError, match="empty untruncated text"):
        AgentMemoryEnvelope.from_dict(
            disabled_memory.to_dict() | {"text": "unexpected"}
        )
