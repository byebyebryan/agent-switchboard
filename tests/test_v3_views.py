from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_switchboard._v3.config import (
    AutomationConfig,
    ControlTurnsConfig,
    DefaultsConfig,
    HooksConfig,
    HostConfig,
    MemoryConfig,
    ProjectCatalog,
    ProviderConfig,
    SwitchboardConfig,
    TmuxConfig,
    ViewsConfig,
)
from agent_switchboard._v3.domain import (
    ActivationState,
    BackgroundState,
    Checkout,
    CheckoutId,
    CheckoutKind,
    CreatedBy,
    Frame,
    FrameId,
    FrameLifecycleState,
    FramePlacement,
    FrameRole,
    GenerationId,
    HostId,
    PlacementId,
    PlacementState,
    Project,
    ProjectId,
    ProjectRepository,
    ProviderId,
    Repository,
    RepositoryId,
    RepositoryKind,
    RequestId,
    ViewId,
    ViewMode,
    ViewState,
)
from agent_switchboard._v3.generation import (
    GenerationError,
    GenerationPaths,
    OpenGeneration,
)
from agent_switchboard._v3.navigator import NavigatorModel
from agent_switchboard._v3.protocol import DirectiveKind, build_navigator_from_registry
from agent_switchboard._v3.storage import Registry
from agent_switchboard._v3.tmux_view import TmuxExecutor
from agent_switchboard._v3.views import ViewRuntime, ViewRuntimeError

GENERATION = GenerationId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
HOST = HostId("11111111-1111-4111-8111-111111111111")
PROJECT_A = ProjectId("22222222-2222-4222-8222-222222222222")
PROJECT_B = ProjectId("22222222-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
REPOSITORY_A = RepositoryId("33333333-3333-4333-8333-333333333333")
REPOSITORY_B = RepositoryId("33333333-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CHECKOUT_A = CheckoutId("44444444-4444-4444-8444-444444444444")
CHECKOUT_B = CheckoutId("44444444-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
VIEW = ViewId("55555555-5555-4555-8555-555555555555")
TASK = FrameId("77777777-7777-4777-8777-777777777777")
TASK_PLACEMENT = PlacementId("88888888-8888-4888-8888-888888888888")

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")


def config(tmp_path: Path) -> SwitchboardConfig:
    projects = (
        Project(PROJECT_A, "Alpha", default_provider=ProviderId.CODEX),
        Project(PROJECT_B, "Beta", default_provider=ProviderId.CLAUDE),
    )
    repositories = (
        Repository(REPOSITORY_A, "alpha", RepositoryKind.GIT),
        Repository(REPOSITORY_B, "beta", RepositoryKind.GIT),
    )
    memberships = (
        ProjectRepository(PROJECT_A, REPOSITORY_A, True),
        ProjectRepository(PROJECT_B, REPOSITORY_B, True),
    )
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    checkouts = (
        Checkout(
            CHECKOUT_A,
            REPOSITORY_A,
            HOST,
            alpha,
            CheckoutKind.MAIN,
            is_default=True,
        ),
        Checkout(
            CHECKOUT_B,
            REPOSITORY_B,
            HOST,
            beta,
            CheckoutKind.MAIN,
            is_default=True,
        ),
    )
    return SwitchboardConfig(
        GENERATION,
        HostConfig(HOST, "starship"),
        (ProviderConfig(ProviderId.CODEX), ProviderConfig(ProviderId.CLAUDE)),
        (),
        ProjectCatalog(projects, repositories, memberships, checkouts),
        DefaultsConfig(),
        ViewsConfig(),
        AutomationConfig(),
        ControlTurnsConfig(),
        TmuxConfig("v3test", 30),
        HooksConfig(),
        MemoryConfig(),
    )


def runtime(
    tmp_path: Path, *, activation_state: ActivationState = ActivationState.COMMITTED
) -> tuple[ViewRuntime, TmuxExecutor]:
    configured = config(tmp_path)
    opened = OpenGeneration(
        GENERATION,
        configured,
        Registry(
            tmp_path / "switchboard.db",
            generation_id=GENERATION,
            local_host_id=HOST,
            local_display_name="starship",
            initial_activation_state=activation_state,
            now=10,
        ),
        activation_state,
    )
    opened.registry.materialize_catalog(
        HOST,
        configured.projects,
        configured.repositories,
        configured.project_repositories,
        configured.checkouts,
        now=11,
    )
    tmux = TmuxExecutor(tmp_path / "tmux.sock")
    return ViewRuntime(
        opened,
        GenerationPaths(tmp_path / "config", tmp_path / "state"),
        tmux=tmux,
    ), tmux


def stop_tmux(tmux: TmuxExecutor) -> None:
    if tmux.socket_path is not None:
        subprocess.run(
            ["tmux", "-S", tmux.socket_path, "kill-server"],
            check=False,
            capture_output=True,
        )


def seed_task_placeholder(
    app: ViewRuntime, tmux: TmuxExecutor, workspace_id: FrameId, *, now: int
) -> None:
    workspace = app.registry.get_frame(workspace_id)
    context = app.registry.get_work_context(workspace.work_context_id)
    app.registry.acquire_work_context(
        context.work_context_id,
        context.claim_generation,
        workspace.frame_id,
        now=now,
    )
    app.registry.create_task(
        Frame(
            TASK,
            HOST,
            PROJECT_A,
            FrameRole.TASK,
            workspace.frame_id,
            workspace.work_context_id,
            "Task",
            "Exercise manual focus",
            ProviderId.CODEX,
            FrameLifecycleState.OPEN,
            None,
            None,
            CreatedBy.USER,
            now,
            now,
        ),
        FramePlacement(
            TASK_PLACEMENT,
            HOST,
            VIEW,
            TASK,
            None,
            PlacementState.STAGED,
            0,
            None,
            now,
        ),
    )
    tmux.spawn_placeholder(
        prefix="v3test",
        generation_id=GENERATION,
        view_id=VIEW,
        frame_id=str(TASK),
    )


def test_project_navigation_modes_attach_and_projection_share_one_view(
    tmp_path: Path,
) -> None:
    app, tmux = runtime(tmp_path)
    try:
        opened = app.create_project_view(
            PROJECT_A,
            request_id=RequestId("66666666-1111-4111-8111-111111111111"),
            mode=ViewMode.DIRECT,
            view_id=VIEW,
            now=20,
        )
        assert opened.created
        assert opened.view.state is ViewState.READY
        first_frame = opened.view.active_frame_id

        same = app.open_project(
            PROJECT_A,
            request_id=RequestId("66666666-2222-4222-8222-222222222222"),
            now=21,
        )
        assert not same.created
        assert same.view.view_id == VIEW

        second = app.open_project(
            PROJECT_B,
            request_id=RequestId("66666666-3333-4333-8333-333333333333"),
            view_id=VIEW,
            now=22,
        )
        assert second.view.active_frame_id != first_frame
        assert len(app.registry.list_views()) == 1
        assert len(app.registry.list_placements(view_id=VIEW)) == 2

        navigator = app.set_mode(
            VIEW,
            ViewMode.NAVIGATOR,
            request_id=RequestId("66666666-4444-4444-8444-444444444444"),
            now=23,
        )
        shell = tmux.inspect_shell("v3test", GENERATION, VIEW, ViewMode.NAVIGATOR)
        assert shell.sidebar is not None
        assert navigator.mode is ViewMode.NAVIGATOR
        revision = navigator.revision
        assert (
            app.set_mode(
                VIEW,
                ViewMode.NAVIGATOR,
                request_id=RequestId("66666666-4444-4444-8444-444444444444"),
                now=24,
            ).revision
            == revision
        )
        tmux.set_mode(
            prefix="v3test",
            generation_id=GENERATION,
            view_id=VIEW,
            current_mode=ViewMode.NAVIGATOR,
            target_mode=ViewMode.DIRECT,
            sidebar_command=("sleep", "60"),
        )
        repaired = app.recover_view(VIEW, now=24)
        assert repaired.repaired
        assert repaired.observation.mode is ViewMode.NAVIGATOR

        focused = app.focus_frame(
            VIEW,
            first_frame,
            request_id=RequestId("66666666-5555-4555-8555-555555555555"),
            now=24,
        )
        assert focused.active_frame_id == first_frame
        revision = focused.revision
        assert (
            app.focus_frame(
                VIEW,
                first_frame,
                request_id=RequestId("66666666-5555-4555-8555-555555555555"),
                now=25,
            ).revision
            == revision
        )
        attach_request = RequestId("66666666-4444-4444-8444-444444444444")
        offered = app.presentation_directive(
            app.registry.get_view(VIEW),
            request_id=attach_request,
            can_focus_desktop=False,
            can_launch_terminal=True,
            now=24,
        )
        assert offered.kind is DirectiveKind.ATTACH
        attached = app.attach_view(VIEW, request_id=attach_request, now=25)
        assert attached.view.last_attached_at == 25
        assert attached.attach_argv[-1].endswith(":main")

        state = build_navigator_from_registry(app.registry, generated_at=26)
        model = NavigatorModel.from_state(state, VIEW)
        assert model.breadcrumb == "starship / Alpha / Alpha"
        assert model.active_project_id == str(PROJECT_A)
        assert [project.name for project in model.projects] == ["Alpha", "Beta"]

        focus = app.presentation_directive(
            attached.view,
            request_id=RequestId("66666666-6666-4666-8666-666666666666"),
            can_focus_desktop=True,
            can_launch_terminal=True,
            now=27,
        )
        assert focus.kind is DirectiveKind.FOCUS
        attach = app.presentation_directive(
            attached.view,
            request_id=RequestId("66666666-7777-4777-8777-777777777777"),
            can_focus_desktop=False,
            can_launch_terminal=True,
            now=28,
        )
        assert attach.kind is DirectiveKind.ATTACH
        assert attach.lease_expires_at == 15_028
    finally:
        stop_tmux(tmux)


def test_focus_transfers_foreground_only_after_explicit_background_confirmation(
    tmp_path: Path,
) -> None:
    app, tmux = runtime(tmp_path)
    try:
        opened = app.create_project_view(
            PROJECT_A,
            request_id=RequestId("99999999-1111-4111-8111-111111111111"),
            mode=ViewMode.DIRECT,
            view_id=VIEW,
            now=20,
        )
        workspace_id = opened.view.active_frame_id
        assert workspace_id is not None
        seed_task_placeholder(app, tmux, workspace_id, now=21)
        workspace = app.registry.get_frame(workspace_id)
        with app.registry.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE work_contexts SET background_state = ? "
                "WHERE work_context_id = ?",
                (BackgroundState.KNOWN.value, str(workspace.work_context_id)),
            )

        with pytest.raises(ViewRuntimeError) as blocked:
            app.focus_frame(
                VIEW,
                TASK,
                request_id=RequestId("99999999-2222-4222-8222-222222222222"),
                now=22,
            )
        assert blocked.value.code == "background_confirmation_required"
        before = app.registry.get_work_context(workspace.work_context_id)
        assert before.foreground_frame_id == workspace_id
        assert app.registry.get_view(VIEW).active_frame_id == workspace_id

        focused = app.focus_frame(
            VIEW,
            TASK,
            request_id=RequestId("99999999-3333-4333-8333-333333333333"),
            confirm_background_transfer=True,
            now=23,
        )
        after = app.registry.get_work_context(workspace.work_context_id)
        assert focused.active_frame_id == TASK
        assert after.foreground_frame_id == TASK
        assert after.claim_generation == before.claim_generation + 1
    finally:
        stop_tmux(tmux)


def test_focus_restores_source_without_recovery_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, tmux = runtime(tmp_path)
    try:
        opened = app.create_project_view(
            PROJECT_A,
            request_id=RequestId("aaaaaaaa-1111-4111-8111-111111111111"),
            mode=ViewMode.DIRECT,
            view_id=VIEW,
            now=20,
        )
        workspace_id = opened.view.active_frame_id
        assert workspace_id is not None
        seed_task_placeholder(app, tmux, workspace_id, now=21)

        def fail_commit(*_args: object, **_kwargs: object) -> None:
            from agent_switchboard._v3.storage import ConflictError

            raise ConflictError("test_conflict", "injected pre-commit conflict")

        monkeypatch.setattr(app.registry, "commit_transition_presentation", fail_commit)
        with pytest.raises(ViewRuntimeError) as failed:
            app.focus_frame(
                VIEW,
                TASK,
                request_id=RequestId("aaaaaaaa-2222-4222-8222-222222222222"),
                now=22,
            )
        assert failed.value.code == "view_presentation_failed"
        view = app.registry.get_view(VIEW)
        assert view.state is ViewState.READY
        assert view.active_frame_id == workspace_id
        shell = tmux.inspect_shell("v3test", GENERATION, VIEW, ViewMode.DIRECT)
        assert shell.active.frame_id == str(workspace_id)
        recovery_count = app.registry.connection.execute(
            "SELECT COUNT(*) FROM recoveries"
        ).fetchone()[0]
        assert recovery_count == 0
    finally:
        stop_tmux(tmux)
        app.opened.close()


def test_server_generation_loss_recreates_shell_and_fences_old_identity(
    tmp_path: Path,
) -> None:
    app, tmux = runtime(tmp_path)
    try:
        opened = app.create_project_view(
            PROJECT_A,
            request_id=RequestId("77777777-1111-4111-8111-111111111111"),
            view_id=VIEW,
            now=20,
        )
        old_server = opened.view.tmux_server_id
        stop_tmux(tmux)
        recovered = app.recover_view(VIEW, now=30)
        assert recovered.repaired
        assert recovered.view.state is ViewState.READY
        assert recovered.view.tmux_server_id != old_server
        tmux.inspect_shell("v3test", GENERATION, VIEW, recovered.view.mode)
        retired = app.retire_view(VIEW, now=31)
        assert retired.state is ViewState.RETIRED
        names = tmux.names("v3test", VIEW)
        assert (
            tmux.run("has-session", "-t", names.view_session, check=False).returncode
            != 0
        )
        assert (
            tmux.run("has-session", "-t", names.holding_session, check=False).returncode
            != 0
        )
        replacement = app.create_project_view(
            PROJECT_A,
            request_id=RequestId("77777777-2222-4222-8222-222222222222"),
            now=32,
        )
        assert replacement.created
        assert replacement.view.view_id != VIEW
    finally:
        stop_tmux(tmux)
        app.opened.close()


def test_staged_generation_blocks_view_side_effects(tmp_path: Path) -> None:
    app, tmux = runtime(tmp_path, activation_state=ActivationState.CUTOVER_STAGED)
    try:
        with pytest.raises(GenerationError) as caught:
            app.create_project_view(
                PROJECT_A,
                request_id=RequestId("88888888-1111-4111-8111-111111111111"),
                now=20,
            )
        assert caught.value.code == "cutover_staged"
        assert not Path(tmux.socket_path or "").exists()
        assert app.registry.list_views() == ()
    finally:
        app.opened.close()
