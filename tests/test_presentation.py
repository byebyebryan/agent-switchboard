from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from agent_switchboard.domain import (
    Checkout,
    CheckoutId,
    HostId,
    PresentationContext,
    Project,
    ProjectId,
    ProviderId,
    handoff_content_hash,
)
from agent_switchboard.presentation import (
    PREPARE_CLAUDE_CAPABILITY_HASH,
    PREPARE_CLAUDE_HISTORY_CAPABILITY_HASH,
    PREPARE_NEW_CLAUDE_CAPABILITY_HASH,
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
CLAUDE_SESSION_KEY = f"{HOST_ID}:claude:{SESSION_ID}"
SECOND_SESSION_KEY = f"{HOST_ID}:codex:{SECOND_SESSION_ID}"
REQUEST_ID = "44444444-4444-4444-8444-444444444444"
PROJECT_ID = "55555555-5555-4555-8555-555555555555"
LOCATION_ID = "66666666-6666-4666-8666-666666666666"
TASK_ID = "67666666-6666-4666-8666-666666666666"
REMOTE_HOST_ID = "88888888-8888-4888-8888-888888888888"
NEW_SESSION_ID = "77777777-7777-4777-8777-777777777777"
NEW_SESSION_KEY = f"{HOST_ID}:codex:{NEW_SESSION_ID}"
NEW_CLAUDE_SESSION_KEY = f"{HOST_ID}:claude:{NEW_SESSION_ID}"
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
            (str(values["session_key"]) if values["session_key"] is not None else None),
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
    provider: str = "codex",
    runtime_presence: str = "stopped",
    resumability: str = "resumable",
    tmux: FakeTmux | None = None,
) -> dict[str, object]:
    stored = registry.upsert_session(
        {
            "session_key": session_key,
            "host_id": HOST_ID,
            "provider": provider,
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
    registry: Registry,
    tmux: FakeTmux,
    *,
    clock: int = 100,
    codex_executable: str | None = "/opt/codex",
    claude_executable: str | None = "/opt/claude",
) -> LaunchCoordinator:
    return LaunchCoordinator(
        registry,
        host_id=HostId(HOST_ID),
        tmux=tmux,  # type: ignore[arg-type]
        swbctl_executable="/opt/swbctl",
        codex_executable=codex_executable,
        claude_executable=claude_executable,
        launch_timeout_seconds=2,
        clock=lambda: clock,
        sleeper=lambda _seconds: None,
    )


def add_project(
    registry: Registry,
    path: Path,
    *,
    project_id: str = PROJECT_ID,
    checkouts: tuple[tuple[str, Path, bool], ...] | None = None,
    default_provider: str | None = "codex",
) -> tuple[Project, tuple[Checkout, ...]]:
    configured_checkouts = checkouts or ((LOCATION_ID, path, True),)
    registry.materialize_projects(
        HOST_ID,
        [
            {
                "project_id": project_id,
                "name": "Switchboard",
                "aliases": (),
                "default_provider": default_provider,
                "default_transport": "tmux",
                "context_sources": (),
                "checkouts": [
                    {
                        "checkout_id": checkout_id,
                        "path": str(checkout_path),
                        "display_name": checkout_path.name,
                        "is_default": is_default,
                    }
                    for checkout_id, checkout_path, is_default in configured_checkouts
                ],
            }
        ],
        observed_at=2,
    )
    project = Project(
        ProjectId(project_id),
        "Switchboard",
        default_provider=(
            ProviderId(default_provider) if default_provider is not None else None
        ),
    )
    domain_checkouts = tuple(
        Checkout(
            CheckoutId(checkout_id),
            project.project_id,
            HostId(HOST_ID),
            checkout_path,
            display_name=checkout_path.name,
            is_default=is_default,
        )
        for checkout_id, checkout_path, is_default in configured_checkouts
    )
    return project, domain_checkouts


def new_coordinator(
    registry: Registry,
    tmux: FakeTmux,
    path: Path,
    *,
    projects: tuple[Project, ...] | None = None,
    checkouts: tuple[Checkout, ...] | None = None,
    clock: int = 100,
    codex_executable: str | None = "/opt/codex",
    claude_executable: str | None = "/opt/claude",
) -> LaunchCoordinator:
    if projects is None or checkouts is None:
        project, configured_checkouts = add_project(registry, path)
        projects = (project,)
        checkouts = configured_checkouts
    if registry.get_task(TASK_ID) is None:
        defaults = tuple(checkout for checkout in checkouts if checkout.is_default)
        task_checkout = (
            defaults[0]
            if len(defaults) == 1
            else checkouts[0]
            if len(checkouts) == 1
            else None
        )
        registry.create_task(
            task_id=TASK_ID,
            host_id=HOST_ID,
            project_id=PROJECT_ID,
            checkout_id=(
                None if task_checkout is None else str(task_checkout.checkout_id)
            ),
            title="Presentation test task",
            observed_at=3,
        )
    return LaunchCoordinator(
        registry,
        host_id=HostId(HOST_ID),
        tmux=tmux,  # type: ignore[arg-type]
        swbctl_executable="/opt/swbctl",
        codex_executable=codex_executable,
        claude_executable=claude_executable,
        projects=projects,
        checkouts=checkouts,
        launch_timeout_seconds=2,
        clock=lambda: clock,
        sleeper=lambda _seconds: None,
        cwd_reader=lambda: path,
    )


DMS_CONTEXT = PresentationContext(False, None, True, True)
ATTACH_CONTEXT = PresentationContext(False, None, False, True)


def test_new_project_launch_is_unbound_waiting_and_idempotent(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    tmux = FakeTmux()
    launch = new_coordinator(
        registry,
        tmux,
        project_path,
        clock=time.time_ns() // 1_000_000,
    )

    plan = launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=None,
        provider=None,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )

    assert plan.kind is PresentationPlanKind.ATTACH
    assert plan.surface_id is not None
    launches = registry.list_launches()
    assert len(launches) == 1
    stored = launches[0]
    assert stored["action"] == "new"
    assert stored["project_id"] == PROJECT_ID
    assert stored["checkout_id"] == LOCATION_ID
    assert stored["cwd"] == str(project_path)
    assert stored["target_session_key"] is None
    assert stored["state"] == "waiting_for_client"
    assert tmux.metadata.session_key is None
    assert tmux.create_calls[0]["cwd"] == project_path
    assert tmux.create_calls[0]["command"] == (
        "/opt/swbctl",
        "bootstrap",
        stored["launch_id"],
    )
    environment = tmux.create_calls[0]["environment"]
    assert set(environment) == {
        "AGENT_SWITCHBOARD_LAUNCH_ID",
        "AGENT_SWITCHBOARD_SURFACE_ID",
        "AGENT_SWITCHBOARD_CAPABILITY",
    }
    assert environment["AGENT_SWITCHBOARD_LAUNCH_ID"] == stored["launch_id"]
    assert environment["AGENT_SWITCHBOARD_SURFACE_ID"] == str(plan.surface_id)
    capability = environment["AGENT_SWITCHBOARD_CAPABILITY"]
    assert (
        stored["agent_capability_hash"]
        == hashlib.sha256(capability.encode("ascii")).hexdigest()
    )
    assert capability not in repr(stored)
    assert attach_surface_argv(
        registry,
        host_id=HOST_ID,
        surface_id=str(plan.surface_id),
        tmux=tmux,  # type: ignore[arg-type]
    ) == ["tmux", "-S", SOCKET, "-u", "attach-session"]

    retry = launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=LOCATION_ID,
        provider="codex",
        request_id=REQUEST_ID,
        context=DMS_CONTEXT,
    )
    assert retry.surface_id == plan.surface_id
    assert len(tmux.create_calls) == 1


def test_closed_task_reopens_and_resumes_same_provider_without_handoff(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    tmux = FakeTmux()
    launch = new_coordinator(registry, tmux, project_path)
    add_session(registry, project_path)
    registry.upsert_session(
        {
            "session_key": SESSION_KEY,
            "project_id": PROJECT_ID,
            "checkout_id": LOCATION_ID,
            "last_observed_at": 2,
        }
    )
    registry.adopt_session(task_id=TASK_ID, session_key=SESSION_KEY, observed_at=3)
    registry.close_task(TASK_ID, host_id=HOST_ID, observed_at=4)

    plan = launch.prepare_task(
        TASK_ID,
        provider=None,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
        reopen=True,
    )

    assert plan.kind is PresentationPlanKind.ATTACH
    task = registry.get_task(TASK_ID)
    assert task is not None and task["status"] == "open"
    session = registry.get_session(SESSION_KEY)
    assert session is not None and session["wrapped_at"] is None


def test_reopen_precondition_failure_leaves_task_closed(
    registry: Registry, tmp_path: Path
) -> None:
    missing = tmp_path / "missing"
    tmux = FakeTmux()
    launch = new_coordinator(registry, tmux, missing)
    registry.close_task(TASK_ID, host_id=HOST_ID, observed_at=4)

    plan = launch.prepare_task(
        TASK_ID,
        provider=None,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
        reopen=True,
    )

    assert plan.kind is PresentationPlanKind.BLOCKED
    assert plan.error is not None
    assert plan.error.code == "working_directory_unavailable"
    task = registry.get_task(TASK_ID)
    assert task is not None and task["status"] == "closed"


def test_new_continuation_resolves_exact_handoff_and_retains_lineage(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    project, checkouts = add_project(registry, project_path)
    add_session(registry, project_path)
    registry.upsert_session(
        {
            "session_key": SESSION_KEY,
            "project_id": PROJECT_ID,
            "checkout_id": LOCATION_ID,
            "cwd": str(project_path),
            "last_observed_at": 2,
        }
    )
    registry.create_task(
        task_id=TASK_ID,
        host_id=HOST_ID,
        project_id=PROJECT_ID,
        checkout_id=LOCATION_ID,
        title="Continuation task",
        observed_at=2,
    )
    registry.adopt_session(task_id=TASK_ID, session_key=SESSION_KEY, observed_at=2)
    handoff = registry.curate_session_handoff(
        SESSION_KEY,
        host_id=HOST_ID,
        handoff_id=stable_uuid("continuation-handoff"),
        summary="The curation core is complete.",
        next_action="Continue in a new exact session.",
        wrap=True,
        observed_at=3,
    )
    assert handoff.handoff is not None
    tmux = FakeTmux()
    launch = new_coordinator(
        registry,
        tmux,
        project_path,
        projects=(project,),
        checkouts=checkouts,
        clock=100,
    )

    plan = launch.prepare_new(
        None,
        task_id=TASK_ID,
        checkout_id=None,
        provider=None,
        source_ref=SESSION_KEY,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )

    assert plan.kind is PresentationPlanKind.ATTACH
    stored = registry.list_launches()[0]
    assert stored["source_handoff_id"] == handoff.handoff["handoff_id"]
    assert stored["project_id"] == PROJECT_ID
    assert stored["checkout_id"] == LOCATION_ID
    assert stored["provider"] == "codex"


def test_imported_continuation_creates_destination_task_and_exact_lineage(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    tmux = FakeTmux()
    launch = new_coordinator(registry, tmux, project_path, clock=100)
    registry.upsert_host(REMOTE_HOST_ID, "remote", observed_at=50)
    task_id = stable_uuid("imported-destination-task")
    handoff_id = stable_uuid("imported-source-handoff")
    summary = "The source task is ready to move."
    next_action = "Continue on this host."
    imported = {
        "source_host_id": REMOTE_HOST_ID,
        "source_project_id": PROJECT_ID,
        "source_task_id": stable_uuid("imported-source-task"),
        "source_session_key": (
            f"{REMOTE_HOST_ID}:claude:{stable_uuid('imported-source-session')}"
        ),
        "handoff_id": handoff_id,
        "sequence": 2,
        "summary": summary,
        "next_action": next_action,
        "created_at": 50,
        "content_hash": handoff_content_hash(summary, next_action),
    }

    plan = launch.prepare_task_create(
        task_id=task_id,
        project_id=PROJECT_ID,
        title="Imported destination",
        purpose="Prove the cross-host seam",
        checkout_id=LOCATION_ID,
        provider="codex",
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
        imported_handoff=imported,
    )

    assert plan.kind is PresentationPlanKind.ATTACH
    stored = registry.list_launches()[0]
    assert stored["task_id"] == task_id
    assert stored["source_handoff_id"] == handoff_id
    assert registry.get_task_imported_handoff(task_id, handoff_id)["summary"] == summary

    tmux.attached = True

    def capture(_executable: str, _argv: Sequence[str]) -> None:
        raise ExecCaptured

    with pytest.raises(ExecCaptured):
        launch.bootstrap(str(stored["launch_id"]), exec_provider=capture)  # type: ignore[arg-type]
    bound = registry.bind_provider_session(
        str(stored["launch_id"]),
        {
            "session_key": NEW_SESSION_KEY,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": NEW_SESSION_ID,
            "runtime_presence": "live",
            "last_observed_at": 150,
        },
        lease_owner=f"bootstrap:{stored['launch_id']}",
        observed_at=150,
    )
    assert bound.kind == "bound"
    assert bound.session["continued_from_handoff_id"] == handoff_id


def test_new_continuation_blocks_without_handoff_or_matching_checkout(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    project, checkouts = add_project(registry, project_path)
    add_session(registry, project_path)
    registry.upsert_session(
        {
            "session_key": SESSION_KEY,
            "project_id": PROJECT_ID,
            "checkout_id": LOCATION_ID,
            "cwd": str(project_path),
            "last_observed_at": 2,
        }
    )
    registry.create_task(
        task_id=TASK_ID,
        host_id=HOST_ID,
        project_id=PROJECT_ID,
        checkout_id=LOCATION_ID,
        title="Continuation task",
        observed_at=2,
    )
    registry.adopt_session(task_id=TASK_ID, session_key=SESSION_KEY, observed_at=2)
    launch = new_coordinator(
        registry,
        FakeTmux(),
        project_path,
        projects=(project,),
        checkouts=checkouts,
    )

    missing = launch.prepare_new(
        None,
        task_id=TASK_ID,
        checkout_id=None,
        provider=None,
        source_ref=SESSION_KEY,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )
    assert missing.kind is PresentationPlanKind.BLOCKED
    assert missing.error is not None
    assert missing.error.code == "continuation_handoff_missing"
    assert registry.list_launches() == []

    handoff = registry.curate_session_handoff(
        SESSION_KEY,
        host_id=HOST_ID,
        summary="Ready.",
        next_action="Reject a checkout override.",
        wrap=True,
        observed_at=3,
    )
    assert handoff.handoff is not None
    conflict = launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=stable_uuid("other-checkout"),
        provider=None,
        source_ref=SESSION_KEY,
        request_id=stable_uuid("continuation-conflict"),
        context=ATTACH_CONTEXT,
    )
    assert conflict.kind is PresentationPlanKind.BLOCKED
    assert conflict.error is not None
    assert conflict.error.code == "continuation_checkout_conflict"
    assert registry.list_launches() == []


def test_new_project_resolution_blocks_missing_provider_and_ambiguous_checkout(
    registry: Registry, tmp_path: Path
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    second_checkout_id = stable_uuid("second-checkout")
    project, checkouts = add_project(
        registry,
        first,
        checkouts=((LOCATION_ID, first, False), (second_checkout_id, second, False)),
        default_provider=None,
    )
    launch = new_coordinator(
        registry,
        FakeTmux(),
        first,
        projects=(project,),
        checkouts=checkouts,
    )

    ambiguous = launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=None,
        provider="codex",
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )
    assert ambiguous.error is not None
    assert ambiguous.error.code == "project_checkout_ambiguous"

    missing_provider = launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=LOCATION_ID,
        provider=None,
        request_id=stable_uuid("missing-provider"),
        context=ATTACH_CONTEXT,
    )
    assert missing_provider.error is not None
    assert missing_provider.error.code == "project_provider_missing"
    assert registry.list_launches() == []


def test_new_project_resolution_blocks_unknown_foreign_and_unavailable(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    project, checkouts = add_project(registry, project_path)
    foreign_checkout = Checkout(
        CheckoutId(stable_uuid("foreign-checkout")),
        ProjectId(stable_uuid("foreign-project")),
        HostId(HOST_ID),
        project_path,
    )
    launch = new_coordinator(
        registry,
        FakeTmux(),
        project_path,
        projects=(project,),
        checkouts=(*checkouts, foreign_checkout),
    )

    unknown = launch.prepare_new(
        stable_uuid("unknown-project"),
        task_id=TASK_ID,
        checkout_id=None,
        provider=None,
        request_id=stable_uuid("unknown-project-request"),
        context=ATTACH_CONTEXT,
    )
    assert unknown.error is not None and unknown.error.code == "project_not_found"
    foreign = launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=str(foreign_checkout.checkout_id),
        provider=None,
        request_id=stable_uuid("foreign-checkout-request"),
        context=ATTACH_CONTEXT,
    )
    assert foreign.error is not None and foreign.error.code == "checkout_not_found"

    missing_path = tmp_path / "missing"
    missing_project, missing_checkouts = add_project(registry, missing_path)
    unavailable = new_coordinator(
        registry,
        FakeTmux(),
        missing_path,
        projects=(missing_project,),
        checkouts=missing_checkouts,
    ).prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=None,
        provider=None,
        request_id=stable_uuid("unavailable-request"),
        context=ATTACH_CONTEXT,
    )
    assert unavailable.error is not None
    assert unavailable.error.code == "working_directory_unavailable"

    assert registry.list_launches() == []


def test_new_claude_project_launch_forces_disabled_agent_view_and_starts_exact_cli(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    project, checkouts = add_project(registry, project_path)
    tmux = FakeTmux()
    launch = new_coordinator(
        registry,
        tmux,
        project_path,
        projects=(project,),
        checkouts=checkouts,
        clock=time.time_ns() // 1_000_000,
    )

    plan = launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=None,
        provider="claude",
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )

    assert plan.kind is PresentationPlanKind.ATTACH
    stored = registry.list_launches()[0]
    assert stored["provider"] == "claude"
    assert stored["capability_hash"] == PREPARE_NEW_CLAUDE_CAPABILITY_HASH
    environment = tmux.create_calls[0]["environment"]
    capability = environment["AGENT_SWITCHBOARD_CAPABILITY"]
    assert (
        stored["agent_capability_hash"]
        == hashlib.sha256(capability.encode("ascii")).hexdigest()
    )
    assert tmux.create_calls[0]["provider"] == "claude"
    assert tmux.create_calls[0]["session_key"] is None
    assert environment == {
        "AGENT_SWITCHBOARD_LAUNCH_ID": stored["launch_id"],
        "AGENT_SWITCHBOARD_SURFACE_ID": str(plan.surface_id),
        "AGENT_SWITCHBOARD_CAPABILITY": capability,
        "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1",
    }
    assert attach_surface_argv(
        registry,
        host_id=HOST_ID,
        surface_id=str(plan.surface_id),
        tmux=tmux,  # type: ignore[arg-type]
    ) == ["tmux", "-S", SOCKET, "-u", "attach-session"]

    tmux.attached = True
    captured: list[object] = []

    def capture(executable: str, argv: Sequence[str]) -> None:
        captured.extend((executable, tuple(argv)))
        raise ExecCaptured

    with pytest.raises(ExecCaptured):
        launch.bootstrap(str(stored["launch_id"]), exec_provider=capture)  # type: ignore[arg-type]

    assert captured == ["/opt/claude", ("/opt/claude",)]
    assert registry.get_launch(str(stored["launch_id"]))["state"] == "provider_started"


def test_claude_history_launch_uses_native_picker_and_binds_selected_session(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    project, checkouts = add_project(
        registry,
        project_path,
        default_provider="claude",
    )
    tmux = FakeTmux()
    launch = new_coordinator(
        registry,
        tmux,
        project_path,
        projects=(project,),
        checkouts=checkouts,
    )

    plan = launch.prepare_history(
        PROJECT_ID,
        checkout_id=LOCATION_ID,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )

    assert plan.kind is PresentationPlanKind.ATTACH
    stored = registry.list_launches()[0]
    assert stored["action"] == "history"
    assert stored["provider"] == "claude"
    assert stored["target_session_key"] is None
    assert stored["capability_hash"] == PREPARE_CLAUDE_HISTORY_CAPABILITY_HASH
    assert stored["agent_capability_hash"] is None
    assert tmux.create_calls[0]["session_key"] is None
    assert tmux.create_calls[0]["environment"] == {
        "AGENT_SWITCHBOARD_LAUNCH_ID": stored["launch_id"],
        "AGENT_SWITCHBOARD_SURFACE_ID": str(plan.surface_id),
        "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1",
    }

    tmux.attached = True
    captured: list[object] = []

    def capture(executable: str, argv: Sequence[str]) -> None:
        captured.extend((executable, tuple(argv)))
        raise ExecCaptured

    with pytest.raises(ExecCaptured):
        launch.bootstrap(str(stored["launch_id"]), exec_provider=capture)  # type: ignore[arg-type]

    assert captured == ["/opt/claude", ("/opt/claude", "--resume")]
    bound = registry.bind_provider_session(
        str(stored["launch_id"]),
        {
            "session_key": NEW_CLAUDE_SESSION_KEY,
            "host_id": HOST_ID,
            "provider": "claude",
            "provider_session_id": NEW_SESSION_ID,
            "runtime_presence": "live",
            "last_observed_at": 150,
        },
        lease_owner=f"bootstrap:{stored['launch_id']}",
        observed_at=150,
    )
    assert bound.kind == "bound"
    assert bound.launch["target_session_key"] == NEW_CLAUDE_SESSION_KEY
    assert bound.surface is not None
    assert bound.surface["current_session_key"] == NEW_CLAUDE_SESSION_KEY


def test_claude_history_blocked_plan_keeps_provider_attribution(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    project, checkouts = add_project(
        registry,
        project_path,
        default_provider="claude",
    )
    launch = new_coordinator(
        registry,
        FakeTmux(),
        project_path,
        projects=(project,),
        checkouts=checkouts,
    )

    blocked = launch.prepare_history(
        PROJECT_ID,
        checkout_id=LOCATION_ID,
        request_id=REQUEST_ID,
        context=PresentationContext(False, None, False, False),
    )

    assert blocked.kind is PresentationPlanKind.BLOCKED
    assert blocked.error is not None
    assert blocked.error.code == "presentation_unavailable"
    assert blocked.error.provider is ProviderId.CLAUDE


def test_disabled_claude_blocks_new_without_mutating_launch_state(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    project, checkouts = add_project(
        registry,
        project_path,
        default_provider="claude",
    )
    launch = new_coordinator(
        registry,
        FakeTmux(),
        project_path,
        projects=(project,),
        checkouts=checkouts,
        claude_executable=None,
    )

    blocked = launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=None,
        provider=None,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )

    assert blocked.kind is PresentationPlanKind.BLOCKED
    assert blocked.error is not None
    assert blocked.error.code == "provider_unavailable"
    assert blocked.error.provider is ProviderId.CLAUDE
    assert registry.list_launches() == []


def test_disabled_codex_blocks_new_without_mutating_launch_state(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    launch = new_coordinator(
        registry,
        FakeTmux(),
        project_path,
        codex_executable=None,
    )

    blocked = launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=None,
        provider=None,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )

    assert blocked.kind is PresentationPlanKind.BLOCKED
    assert blocked.error is not None
    assert blocked.error.code == "provider_unavailable"
    assert registry.list_launches() == []


def test_new_request_id_conflicts_when_the_configured_checkout_changes(
    registry: Registry, tmp_path: Path
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    second_checkout_id = stable_uuid("conflict-checkout")
    project, checkouts = add_project(
        registry,
        first,
        checkouts=((LOCATION_ID, first, True), (second_checkout_id, second, False)),
    )
    tmux = FakeTmux()
    launch = new_coordinator(
        registry,
        tmux,
        first,
        projects=(project,),
        checkouts=checkouts,
        clock=time.time_ns() // 1_000_000,
    )
    launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=LOCATION_ID,
        provider=None,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )

    conflict = launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=second_checkout_id,
        provider=None,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )

    assert conflict.kind is PresentationPlanKind.BLOCKED
    assert conflict.error is not None
    assert conflict.error.code == "request_conflict"
    assert len(registry.list_launches()) == 1
    assert len(tmux.create_calls) == 1


def test_new_bootstrap_starts_exact_codex_after_attachment(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    tmux = FakeTmux()
    launch = new_coordinator(registry, tmux, project_path)
    launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=None,
        provider=None,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )
    launch_id = str(registry.list_launches()[0]["launch_id"])
    tmux.attached = True
    captured: list[object] = []

    def capture(executable: str, argv: Sequence[str]) -> None:
        captured.extend((executable, tuple(argv)))
        raise ExecCaptured

    with pytest.raises(ExecCaptured):
        launch.bootstrap(launch_id, exec_provider=capture)  # type: ignore[arg-type]

    assert captured == ["/opt/codex", ("/opt/codex",)]
    started = registry.get_launch(launch_id)
    assert started["state"] == "provider_started"
    assert started["expires_at"] == 300_100


def test_new_bootstrap_rejects_disappeared_checkout(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    tmux = FakeTmux()
    launch = new_coordinator(registry, tmux, project_path)
    launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=None,
        provider=None,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )
    launch_id = str(registry.list_launches()[0]["launch_id"])
    tmux.attached = True
    project_path.rmdir()

    assert launch.bootstrap(launch_id) == 1
    failed = registry.get_launch(launch_id)
    assert failed["state"] == "failed"
    assert failed["failure_code"] == "launch_target_changed"


def test_new_bootstrap_rejects_a_reconfigured_checkout(
    registry: Registry, tmp_path: Path
) -> None:
    original = tmp_path / "original"
    changed = tmp_path / "changed"
    original.mkdir()
    changed.mkdir()
    tmux = FakeTmux()
    launch = new_coordinator(registry, tmux, original)
    launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=None,
        provider=None,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )
    launch_id = str(registry.list_launches()[0]["launch_id"])
    tmux.attached = True
    project = launch.projects[PROJECT_ID]
    changed_checkout = Checkout(
        CheckoutId(LOCATION_ID),
        project.project_id,
        HostId(HOST_ID),
        changed,
        display_name="changed",
        is_default=True,
    )
    reconfigured = new_coordinator(
        registry,
        tmux,
        changed,
        projects=(project,),
        checkouts=(changed_checkout,),
    )

    assert reconfigured.bootstrap(launch_id) == 1
    failed = registry.get_launch(launch_id)
    assert failed["state"] == "failed"
    assert failed["failure_code"] == "launch_target_changed"


def test_bound_new_session_promotes_tmux_metadata_and_reopens(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    tmux = FakeTmux()
    launch = new_coordinator(registry, tmux, project_path)
    launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=None,
        provider=None,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )
    launch_id = str(registry.list_launches()[0]["launch_id"])
    tmux.attached = True

    def capture(_executable: str, _argv: Sequence[str]) -> None:
        raise ExecCaptured

    with pytest.raises(ExecCaptured):
        launch.bootstrap(launch_id, exec_provider=capture)  # type: ignore[arg-type]
    registry.bind_provider_session(
        launch_id,
        {
            "session_key": NEW_SESSION_KEY,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": NEW_SESSION_ID,
            "runtime_presence": "live",
            "last_observed_at": 150,
        },
        lease_owner=f"bootstrap:{launch_id}",
        observed_at=150,
    )
    assert tmux.metadata.session_key is None
    tmux.client_ids = ("/dev/pts/8",)

    reopened = launch.prepare_open(
        NEW_SESSION_KEY,
        request_id=stable_uuid("reopen-new"),
        context=DMS_CONTEXT,
    )

    assert reopened.kind is PresentationPlanKind.FOCUS
    assert tmux.metadata.session_key == NEW_SESSION_KEY
    assert len(tmux.create_calls) == 1


def test_bound_new_claude_session_promotes_tmux_metadata_and_reopens(
    registry: Registry, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    project, checkouts = add_project(
        registry,
        project_path,
        default_provider="claude",
    )
    tmux = FakeTmux()
    launch = new_coordinator(
        registry,
        tmux,
        project_path,
        projects=(project,),
        checkouts=checkouts,
    )
    launch.prepare_new(
        PROJECT_ID,
        task_id=TASK_ID,
        checkout_id=None,
        provider=None,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )
    launch_id = str(registry.list_launches()[0]["launch_id"])
    tmux.attached = True

    def capture(_executable: str, _argv: Sequence[str]) -> None:
        raise ExecCaptured

    with pytest.raises(ExecCaptured):
        launch.bootstrap(launch_id, exec_provider=capture)  # type: ignore[arg-type]
    registry.bind_provider_session(
        launch_id,
        {
            "session_key": NEW_CLAUDE_SESSION_KEY,
            "host_id": HOST_ID,
            "provider": "claude",
            "provider_session_id": NEW_SESSION_ID,
            "runtime_presence": "live",
            "last_observed_at": 150,
        },
        lease_owner=f"bootstrap:{launch_id}",
        observed_at=150,
    )
    assert tmux.metadata.session_key is None
    tmux.client_ids = ("/dev/pts/8",)

    reopened = launch.prepare_open(
        NEW_CLAUDE_SESSION_KEY,
        request_id=stable_uuid("reopen-new-claude"),
        context=DMS_CONTEXT,
    )

    assert reopened.kind is PresentationPlanKind.FOCUS
    assert tmux.metadata.session_key == NEW_CLAUDE_SESSION_KEY
    assert tmux.metadata.provider == "claude"
    assert len(tmux.create_calls) == 1


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
    environment = create["environment"]
    assert set(environment) == {
        "AGENT_SWITCHBOARD_LAUNCH_ID",
        "AGENT_SWITCHBOARD_SURFACE_ID",
        "AGENT_SWITCHBOARD_CAPABILITY",
    }
    assert environment["AGENT_SWITCHBOARD_LAUNCH_ID"] == launches[0]["launch_id"]
    assert environment["AGENT_SWITCHBOARD_SURFACE_ID"] == str(plan.surface_id)
    capability = environment["AGENT_SWITCHBOARD_CAPABILITY"]
    assert (
        launches[0]["agent_capability_hash"]
        == hashlib.sha256(capability.encode("ascii")).hexdigest()
    )

    retry = launch.prepare_open(
        SESSION_KEY,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )
    assert retry.surface_id == plan.surface_id
    assert len(tmux.create_calls) == 1


def test_parked_claude_open_forces_disabled_agent_view_and_is_idempotent(
    registry: Registry, tmp_path: Path
) -> None:
    tmux = FakeTmux()
    add_session(
        registry,
        tmp_path,
        session_key=CLAUDE_SESSION_KEY,
        provider="claude",
    )
    launch = coordinator(registry, tmux)

    plan = launch.prepare_open(
        CLAUDE_SESSION_KEY,
        request_id=REQUEST_ID,
        context=DMS_CONTEXT,
    )

    assert plan.kind is PresentationPlanKind.ATTACH
    launches = registry.list_launches(target_session_key=CLAUDE_SESSION_KEY)
    assert len(launches) == 1
    assert launches[0]["provider"] == "claude"
    assert launches[0]["capability_hash"] == PREPARE_CLAUDE_CAPABILITY_HASH
    capability = tmux.create_calls[0]["environment"]["AGENT_SWITCHBOARD_CAPABILITY"]
    assert (
        launches[0]["agent_capability_hash"]
        == hashlib.sha256(capability.encode("ascii")).hexdigest()
    )
    surface = registry.get_surface(str(plan.surface_id))
    assert surface is not None and surface["provider"] == "claude"
    create = tmux.create_calls[0]
    assert create["provider"] == "claude"
    assert create["session_key"] == CLAUDE_SESSION_KEY
    assert create["environment"] == {
        "AGENT_SWITCHBOARD_LAUNCH_ID": launches[0]["launch_id"],
        "AGENT_SWITCHBOARD_SURFACE_ID": str(plan.surface_id),
        "AGENT_SWITCHBOARD_CAPABILITY": capability,
        "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1",
    }

    retry = launch.prepare_open(
        CLAUDE_SESSION_KEY,
        request_id=REQUEST_ID,
        context=ATTACH_CONTEXT,
    )
    assert retry.surface_id == plan.surface_id
    assert len(tmux.create_calls) == 1


def test_disabled_claude_blocks_resume_but_can_adopt_live_tmux(
    registry: Registry, tmp_path: Path
) -> None:
    parked_tmux = FakeTmux()
    add_session(
        registry,
        tmp_path,
        session_key=CLAUDE_SESSION_KEY,
        provider="claude",
    )
    disabled = coordinator(registry, parked_tmux, claude_executable=None)

    blocked = disabled.prepare_open(
        CLAUDE_SESSION_KEY,
        request_id=REQUEST_ID,
        context=DMS_CONTEXT,
    )
    assert blocked.kind is PresentationPlanKind.BLOCKED
    assert blocked.error is not None and blocked.error.code == "provider_unavailable"
    assert registry.list_launches() == []

    registry.connection.execute("DELETE FROM sessions")
    live_tmux = FakeTmux()
    live_tmux.attached = True
    live_tmux.client_ids = ("/dev/pts/8",)
    add_session(
        registry,
        tmp_path,
        session_key=CLAUDE_SESSION_KEY,
        provider="claude",
        runtime_presence="live",
        tmux=live_tmux,
    )
    focused = coordinator(registry, live_tmux, claude_executable=None).prepare_open(
        CLAUDE_SESSION_KEY,
        request_id=stable_uuid("disabled-claude-live"),
        context=DMS_CONTEXT,
    )
    assert focused.kind is PresentationPlanKind.FOCUS
    assert live_tmux.metadata.provider == "claude"


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


def test_disabled_codex_can_still_focus_an_existing_live_surface(
    registry: Registry, tmp_path: Path
) -> None:
    tmux = FakeTmux()
    tmux.attached = True
    tmux.client_ids = ("/dev/pts/8",)
    add_session(registry, tmp_path, runtime_presence="live", tmux=tmux)

    plan = coordinator(registry, tmux, codex_executable=None).prepare_open(
        SESSION_KEY,
        request_id=REQUEST_ID,
        context=DMS_CONTEXT,
    )

    assert plan.kind is PresentationPlanKind.FOCUS
    assert plan.surface_id is not None
    assert registry.list_launches() == []


def test_existing_surface_switches_only_the_supplied_live_client(
    registry: Registry, tmp_path: Path
) -> None:
    tmux = FakeTmux()
    tmux.attached = True
    tmux.client_ids = ("/dev/pts/8",)
    add_session(registry, tmp_path, runtime_presence="live", tmux=tmux)
    registry.curate_session_handoff(
        SESSION_KEY,
        host_id=HOST_ID,
        summary="Wrap before reopening.",
        next_action="Open the exact managed surface.",
        wrap=True,
        observed_at=2,
    )
    launch = coordinator(registry, tmux)
    adopted = launch.prepare_open(
        SESSION_KEY,
        request_id=REQUEST_ID,
        context=DMS_CONTEXT,
    )
    assert adopted.surface_id is not None
    assert registry.get_session(SESSION_KEY)["wrapped_at"] is None

    switched = launch.prepare_open(
        SESSION_KEY,
        request_id=stable_uuid("switch-request"),
        context=PresentationContext(True, "/dev/pts/8", False, False),
    )

    assert switched.kind is PresentationPlanKind.SWITCH
    assert switched.surface_id == adopted.surface_id
    assert switched.tmux_client == "/dev/pts/8"

    registry.curate_session_handoff(
        SESSION_KEY,
        host_id=HOST_ID,
        summary="Wrap before a blocked reopen.",
        next_action="Keep wrapping if presentation fails.",
        wrap=True,
        observed_at=3,
    )

    stale = launch.prepare_open(
        SESSION_KEY,
        request_id=stable_uuid("stale-client-request"),
        context=PresentationContext(True, "/dev/pts/9", False, False),
    )
    assert stale.kind is PresentationPlanKind.BLOCKED
    assert stale.error is not None and stale.error.code == "tmux_client_stale"
    assert registry.get_session(SESSION_KEY)["wrapped_at"] == 3


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
    registry.curate_session_handoff(
        SESSION_KEY,
        host_id=HOST_ID,
        summary="Wrap the parked session.",
        next_action="Resume and clear only after binding.",
        wrap=True,
        observed_at=2,
    )
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
    started = registry.get_launch(launch_id)
    assert started["state"] == "provider_started"
    assert started["expires_at"] == 300_100
    assert registry.get_session(SESSION_KEY)["wrapped_at"] == 2

    bound = registry.bind_provider_session(
        launch_id,
        {
            "session_key": SESSION_KEY,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": SESSION_ID,
            "runtime_presence": "live",
            "last_observed_at": 150,
        },
        lease_owner=f"bootstrap:{launch_id}",
        observed_at=150,
    )
    assert bound.kind == "bound"
    assert bound.session["wrapped_at"] is None


def test_bootstrap_starts_exact_claude_resume_after_attachment(
    registry: Registry, tmp_path: Path
) -> None:
    tmux = FakeTmux()
    add_session(
        registry,
        tmp_path,
        session_key=CLAUDE_SESSION_KEY,
        provider="claude",
    )
    launch = coordinator(registry, tmux)
    launch.prepare_open(
        CLAUDE_SESSION_KEY, request_id=REQUEST_ID, context=ATTACH_CONTEXT
    )
    launch_id = str(registry.list_launches()[0]["launch_id"])
    tmux.attached = True
    captured: list[object] = []

    def capture(executable: str, argv: Sequence[str]) -> None:
        captured.extend((executable, tuple(argv)))
        raise ExecCaptured

    with pytest.raises(ExecCaptured):
        launch.bootstrap(launch_id, exec_provider=capture)  # type: ignore[arg-type]

    assert captured == [
        "/opt/claude",
        ("/opt/claude", "--resume", SESSION_ID),
    ]
    started = registry.get_launch(launch_id)
    assert started["state"] == "provider_started"
    assert started["expires_at"] == 300_100


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


@pytest.mark.parametrize(
    ("session_key", "provider"),
    ((SESSION_KEY, "codex"), (CLAUDE_SESSION_KEY, "claude")),
)
def test_surface_actions_revalidate_stored_identity_and_pending_lease(
    registry: Registry, tmp_path: Path, session_key: str, provider: str
) -> None:
    tmux = FakeTmux()
    add_session(registry, tmp_path, session_key=session_key, provider=provider)
    now = time.time_ns() // 1_000_000
    launch = coordinator(registry, tmux, clock=now)
    plan = launch.prepare_open(
        session_key, request_id=REQUEST_ID, context=ATTACH_CONTEXT
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
