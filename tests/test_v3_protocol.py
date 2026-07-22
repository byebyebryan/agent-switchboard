from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from agent_switchboard._v3.domain import (
    Activity,
    ActivityReason,
    Checkout,
    CheckoutId,
    CheckoutKind,
    FailureRecord,
    FrameId,
    FramePlacement,
    FrameSession,
    FrameSessionId,
    GenerationId,
    HostId,
    HostStateCache,
    MembershipReason,
    PlacementId,
    PlacementState,
    Project,
    ProjectId,
    ProjectRepository,
    ProviderId,
    ProviderSession,
    Reachability,
    Repository,
    RepositoryId,
    RepositoryKind,
    Resumability,
    RuntimePresence,
    SessionKey,
    UserView,
    ViewId,
    ViewMode,
    ViewState,
    WorkContextId,
)
from agent_switchboard._v3.protocol import (
    DirectiveKind,
    HostState,
    NavigatorState,
    PresentationDirective,
    ProtocolError,
    build_host_state,
    build_navigator_from_registry,
    build_navigator_state,
)
from agent_switchboard._v3.storage import Registry

FIXTURES = Path(__file__).parent / "fixtures" / "phase6" / "v1"
GENERATION = GenerationId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
HOST = HostId("11111111-1111-4111-8111-111111111111")
OTHER_HOST = HostId("00000000-0000-4000-8000-000000000001")
OTHER_HOST_TWO = HostId("00000000-0000-4000-8000-000000000002")
PROJECT = ProjectId("44444444-4444-4444-8444-444444444444")
REPOSITORY = RepositoryId("55555555-5555-4555-8555-555555555555")
CHECKOUT = CheckoutId("66666666-6666-4666-8666-666666666666")
CONTEXT = WorkContextId("77777777-7777-4777-8777-777777777777")
WORKSPACE = FrameId("88888888-8888-4888-8888-888888888888")
VIEW = ViewId("99999999-9999-4999-8999-999999999999")
PLACEMENT = PlacementId("99999999-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def registry() -> Registry:
    return Registry(
        ":memory:",
        generation_id=GENERATION,
        local_host_id=HOST,
        local_display_name="starship",
        now=10,
    )


def seed_structural_state(opened: Registry) -> None:
    opened.materialize_catalog(
        HOST,
        [Project(PROJECT, "Switchboard", ("Agent Router",))],
        [
            Repository(
                REPOSITORY,
                "switchboard",
                RepositoryKind.GIT,
                ("AGENTS.md", "docs/design.md"),
            )
        ],
        [ProjectRepository(PROJECT, REPOSITORY, True)],
        [
            Checkout(
                CHECKOUT,
                REPOSITORY,
                HOST,
                Path("/home/bryan/code/agent-switchboard"),
                CheckoutKind.MAIN,
                display_name="main",
                is_default=True,
            )
        ],
        now=20,
    )
    opened.ensure_workspace(
        CONTEXT, WORKSPACE, HOST, PROJECT, CHECKOUT, "Switchboard", now=21
    )
    opened.create_view(
        UserView(
            VIEW,
            HOST,
            ViewMode.NAVIGATOR,
            WORKSPACE,
            ViewState.READY,
            0,
            "private-desktop-token",
            None,
            22,
            None,
            22,
        ),
        FramePlacement(
            PLACEMENT,
            HOST,
            VIEW,
            WORKSPACE,
            None,
            PlacementState.ACTIVE,
            0,
            22,
            22,
        ),
    )


def provider_session(number: int, *, associated: bool = False) -> ProviderSession:
    session_uuid = UUID(f"00000000-0000-4000-8000-{number:012x}")
    key = SessionKey(HOST, ProviderId.CODEX, session_uuid)
    return ProviderSession(
        key,
        HOST,
        ProviderId.CODEX,
        session_uuid,
        PROJECT if associated else None,
        CHECKOUT if associated else None,
        f"session-{number}",
        None,
        False,
        RuntimePresence.STOPPED,
        Resumability.RESUMABLE,
        Activity.READY,
        ActivityReason.TURN_COMPLETE,
        number,
        number,
        number,
        number,
    )


def test_v1_golden_fixtures_are_deterministic_and_canonical() -> None:
    with registry() as opened:
        host = build_host_state(opened, generated_at=100)
        expected_host = HostState.from_json(
            (FIXTURES / "host-state.json").read_text(encoding="utf-8")
        )
        assert host.to_json() == expected_host.to_json()
        navigator = build_navigator_state([host], local_host_id=HOST, generated_at=100)
        expected_navigator = NavigatorState.from_json(
            (FIXTURES / "navigator-state.json").read_text(encoding="utf-8")
        )
        assert navigator.to_json() == expected_navigator.to_json()

    directive = PresentationDirective.from_json(
        (FIXTURES / "presentation-directive.json").read_text(encoding="utf-8")
    )
    assert (
        directive.to_json()
        == PresentationDirective(
            "22222222-2222-4222-8222-222222222222",
            str(HOST),
            DirectiveKind.FOCUS,
            "33333333-3333-4333-8333-333333333333",
            7,
            "switchboard:view:33333333",
        ).to_json()
    )


def test_host_projection_orders_records_and_excludes_authority_and_paths() -> None:
    with registry() as opened:
        seed_structural_state(opened)
        state = build_host_state(opened, generated_at=30)
        encoded = state.to_json()
        assert "/home/bryan" not in encoded
        assert "private-desktop-token" not in encoded
        assert all(
            forbidden not in encoded.casefold()
            for forbidden in (
                "tmux_server",
                "pane_id",
                "process_id",
                "capability_digest",
                "purpose",
            )
        )
        assert state.data["projects"][0]["projectId"] == str(PROJECT)  # type: ignore[index]
        assert state.data["views"][0]["activeFrameId"] == str(WORKSPACE)  # type: ignore[index]
        navigator = build_navigator_state([state], local_host_id=HOST, generated_at=31)
        project = navigator.data["projects"][0]  # type: ignore[index]
        assert project["viewId"] == str(VIEW)
        assert project["entryFrameId"] == str(WORKSPACE)
        navigator_json = navigator.to_json().casefold()
        assert all(
            forbidden not in navigator_json
            for forbidden in ("sessions", "surfaces", "checkouts", "repositoryid")
        )


def test_unknown_safe_fields_are_omitted_but_sensitive_and_corrupt_input_fails() -> (
    None
):
    with registry() as opened:
        seed_structural_state(opened)
        data = build_host_state(opened, generated_at=30).to_dict()
    data["futureLabel"] = "safe"
    data["host"]["futureColor"] = "blue"  # type: ignore[index]
    parsed = HostState(data)
    assert "futureLabel" not in parsed.to_dict()
    assert "futureColor" not in parsed.to_dict()["host"]

    unsafe = dict(data)
    unsafe["rawPrompt"] = "do something"
    with pytest.raises(ProtocolError, match="forbidden"):
        HostState(unsafe)

    oversized = dict(parsed.to_dict())
    oversized["futureLabel"] = "x" * (64 * 1024 + 1)
    with pytest.raises(ProtocolError, match="oversized"):
        HostState(oversized)

    too_many = dict(parsed.to_dict())
    too_many["futureItems"] = [None] * 100_001
    with pytest.raises(ProtocolError, match="too many"):
        HostState(too_many)

    nonfinite = dict(parsed.to_dict())
    nonfinite["futureNumber"] = float("nan")
    with pytest.raises(ProtocolError, match="non-finite"):
        HostState(nonfinite)

    too_large = dict(parsed.to_dict())
    too_large["futureNumber"] = 2**64
    with pytest.raises(ProtocolError, match="out-of-range"):
        HostState(too_large)

    corrupt = parsed.to_dict()
    corrupt["views"][0]["activeFrameId"] = (  # type: ignore[index]
        "00000000-0000-4000-8000-000000000099"
    )
    with pytest.raises(ProtocolError, match="view reference"):
        HostState(corrupt)


def test_truncation_is_bounded_and_retains_valid_references() -> None:
    with registry() as opened:
        opened.upsert_provider_session(provider_session(1))
        opened.upsert_provider_session(provider_session(2))
        state = build_host_state(opened, generated_at=30, collection_limit=1)
        assert len(state.data["sessions"]) == 1
        assert state.data["truncation"] == {
            "sessions": {"emittedCount": 1, "retainedCount": 2}
        }
        assert state.data["warnings"][0]["code"] == "projection_truncated"  # type: ignore[index]

    with registry() as opened:
        seed_structural_state(opened)
        opened.upsert_provider_session(provider_session(1, associated=True))
        current = provider_session(2, associated=True)
        opened.upsert_provider_session(current)
        opened.append_frame_session(
            FrameSession(
                FrameSessionId("00000000-0000-4000-8000-000000000003"),
                WORKSPACE,
                current.session_key,
                1,
                MembershipReason.STARTED,
                25,
            )
        )
        state = build_host_state(opened, generated_at=30, collection_limit=1)
        assert state.data["frames"] == []
        assert state.data["views"] == []
        assert state.data["truncation"]["frames"] == {  # type: ignore[index]
            "emittedCount": 0,
            "retainedCount": 1,
        }


def test_cached_remote_state_is_validated_and_projects_without_authority() -> None:
    with registry() as opened:
        local = build_host_state(opened, generated_at=100)
        remote_data = local.to_dict()
        remote_data["generationId"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        remote_data["generatedAt"] = 90
        remote_data["host"] = {
            "hostId": str(OTHER_HOST),
            "displayName": "snap",
        }
        remote = HostState(remote_data)
        remote_json = remote.to_json()
        opened.cache_host_state(
            HostStateCache(
                "snap",
                OTHER_HOST,
                remote_json,
                sha256(remote_json.encode()).hexdigest(),
                90,
                95,
                95,
                Reachability.ONLINE,
                None,
            )
        )
        navigator = build_navigator_from_registry(opened, generated_at=100)
        assert [host["hostId"] for host in navigator.data["hosts"]] == [  # type: ignore[union-attr]
            str(OTHER_HOST),
            str(HOST),
        ]
        assert navigator.data["hosts"][0]["reachability"] == "online"  # type: ignore[index]
        truncated = build_navigator_state(
            [local, remote],
            local_host_id=HOST,
            generated_at=100,
            collection_limit=1,
        )
        assert [host["hostId"] for host in truncated.data["hosts"]] == [  # type: ignore[union-attr]
            str(HOST)
        ]
        assert truncated.data["truncation"]["hosts"] == {  # type: ignore[index]
            "emittedCount": 1,
            "retainedCount": 2,
        }

        changed = json.loads(remote_json)
        changed["host"]["hostId"] = str(HOST)
        changed_json = json.dumps(changed, separators=(",", ":"), sort_keys=True)
        with pytest.raises(ProtocolError, match="identity"):
            opened.cache_host_state(
                HostStateCache(
                    "snap-two",
                    OTHER_HOST_TWO,
                    changed_json,
                    sha256(changed_json.encode()).hexdigest(),
                    91,
                    96,
                    96,
                    Reachability.ONLINE,
                    None,
                )
            )
            build_navigator_from_registry(opened, generated_at=101)


def test_presentation_directive_shapes_are_exclusive_and_authority_free() -> None:
    attach = PresentationDirective(
        "22222222-2222-4222-8222-222222222222",
        str(HOST),
        DirectiveKind.ATTACH,
        "33333333-3333-4333-8333-333333333333",
        8,
        "switchboard:view:33333333",
        500,
    )
    assert PresentationDirective.from_json(attach.to_json()) == attach
    blocked = PresentationDirective(
        "22222222-2222-4222-8222-222222222222",
        str(HOST),
        DirectiveKind.BLOCKED,
        error=FailureRecord("view_busy", "View is transitioning.", True),
    )
    assert PresentationDirective.from_json(blocked.to_json()) == blocked
    with pytest.raises(ProtocolError, match="forbidden"):
        PresentationDirective.from_json(
            attach.to_json()[:-1] + ',"tmuxTarget":"session:1"}'
        )
    with pytest.raises(ProtocolError, match="repeats key"):
        PresentationDirective.from_json('{"directiveVersion":1,"directiveVersion":1}')
    with pytest.raises(ProtocolError, match="lease"):
        PresentationDirective(
            "22222222-2222-4222-8222-222222222222",
            str(HOST),
            DirectiveKind.ATTACH,
            "33333333-3333-4333-8333-333333333333",
            8,
            "switchboard:view:33333333",
        )
