from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from test_v3_views import config as build_config

from agent_switchboard._v3.agent_mcp import (
    TOOLS,
    AgentToolService,
    run_mcp_server,
)
from agent_switchboard._v3.config import ControlTurnsConfig
from agent_switchboard._v3.domain import (
    ActivationState,
    Activity,
    ActivityReason,
    AgentCapability,
    CapabilityId,
    ControlTurnPolicy,
    FrameId,
    FrameSession,
    FrameSessionId,
    LaunchAction,
    LaunchId,
    LaunchIntent,
    LaunchState,
    MembershipReason,
    PlacementState,
    ProviderId,
    ProviderSession,
    RequestId,
    Resumability,
    RuntimePresence,
    SessionKey,
    Surface,
    SurfaceId,
    SurfaceState,
    TransitionState,
    ViewId,
    ViewMode,
)
from agent_switchboard._v3.generation import GenerationPaths, OpenGeneration
from agent_switchboard._v3.provider_runtime import ProviderContract
from agent_switchboard._v3.storage import ConflictError, Registry
from agent_switchboard._v3.tmux_view import TmuxExecutor
from agent_switchboard._v3.trusted_hook import handle_trusted_event
from agent_switchboard._v3.views import ViewRuntime
from agent_switchboard._v3.workflow import WorkflowError, WorkflowRuntime

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")

VIEW = ViewId("55555555-aaaa-4555-8555-555555555555")
PARENT_SESSION_ID = UUID("66666666-1111-4666-8666-111111111111")
CHILD_SESSION_ID = UUID("66666666-2222-4666-8666-222222222222")
PARENT_TOKEN = "phase6d-parent-capability"
CHILD_TOKEN = "phase6d-child-capability"
RESUMED_PARENT_TOKEN = "phase6d-resumed-parent-capability"
WORKSPACE_TOKEN = "phase6f-workspace-capability"


class Allocator:
    def __init__(self) -> None:
        self.cleaned: list[UUID] = []

    def allocate(self, provider, title, contract):  # type: ignore[no-untyped-def]
        assert provider is ProviderId.CODEX
        assert title == "Child task"
        assert contract.version == "0.144.6"
        return CHILD_SESSION_ID

    def cleanup(self, provider, session_id, contract):  # type: ignore[no-untyped-def]
        self.cleaned.append(session_id)


class WorkspaceAllocator:
    def __init__(self) -> None:
        self.available = [PARENT_SESSION_ID, CHILD_SESSION_ID]
        self.cleaned: list[UUID] = []

    def allocate(self, provider, title, contract):  # type: ignore[no-untyped-def]
        assert provider is ProviderId.CODEX
        assert title
        assert contract.version == "0.144.6"
        return self.available.pop(0)

    def cleanup(self, provider, session_id, contract):  # type: ignore[no-untyped-def]
        self.cleaned.append(session_id)


def _fake_provider(path: Path) -> Path:
    script = path / "fake-codex"
    script.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


def _runtime(tmp_path: Path) -> tuple[WorkflowRuntime, TmuxExecutor, str, str]:
    configured = build_config(tmp_path)
    opened = OpenGeneration(
        configured.generation_id,
        configured,
        Registry(
            tmp_path / "switchboard.db",
            generation_id=configured.generation_id,
            local_host_id=configured.host.host_id,
            local_display_name=configured.host.display_name,
            initial_activation_state=ActivationState.COMMITTED,
            now=10,
        ),
        ActivationState.COMMITTED,
    )
    opened.registry.materialize_catalog(
        configured.host.host_id,
        configured.projects,
        configured.repositories,
        configured.project_repositories,
        configured.checkouts,
        now=11,
    )
    socket = tmp_path / "tmux.sock"
    tmux = TmuxExecutor(socket)
    paths = GenerationPaths(tmp_path / "config", tmp_path / "state")
    views = ViewRuntime(opened, paths, tmux=tmux)
    project = configured.projects[0]
    result = views.create_project_view(
        project.project_id,
        request_id=RequestId("77777777-1111-4777-8777-111111111111"),
        mode=ViewMode.DIRECT,
        view_id=VIEW,
        now=20,
    )
    frame_id = result.view.active_frame_id
    assert frame_id is not None
    frame = opened.registry.get_frame(frame_id)
    context = opened.registry.get_work_context(frame.work_context_id)
    context = opened.registry.acquire_work_context(
        context.work_context_id,
        context.claim_generation,
        frame_id,
        now=21,
    )
    parent_key = SessionKey(
        configured.host.host_id, ProviderId.CODEX, PARENT_SESSION_ID
    )
    opened.registry.upsert_provider_session(
        ProviderSession(
            parent_key,
            configured.host.host_id,
            ProviderId.CODEX,
            PARENT_SESSION_ID,
            project.project_id,
            context.checkout_id,
            "Workspace",
            None,
            False,
            RuntimePresence.LIVE,
            Resumability.RESUMABLE,
            Activity.READY,
            ActivityReason.TURN_COMPLETE,
            21,
            21,
            21,
            21,
        )
    )
    opened.registry.append_frame_session(
        FrameSession(
            FrameSessionId("88888888-1111-4888-8888-111111111111"),
            frame_id,
            parent_key,
            1,
            MembershipReason.STARTED,
            21,
        )
    )
    launch_id = LaunchId("99999999-1111-4999-8999-111111111111")
    surface_id = SurfaceId("aaaaaaaa-1111-4aaa-8aaa-111111111111")
    opened.registry.plan_launch(
        LaunchIntent(
            launch_id,
            RequestId("bbbbbbbb-1111-4bbb-8bbb-111111111111"),
            configured.host.host_id,
            frame_id,
            ProviderId.CODEX,
            LaunchAction.NEW,
            None,
            LaunchState.PLANNED,
            None,
            21,
            21,
        ),
        Surface(
            surface_id,
            configured.host.host_id,
            ProviderId.CODEX,
            None,
            launch_id,
            SurfaceState.PLANNED,
            None,
            None,
            None,
            None,
            0,
            21,
            21,
            None,
        ),
    )
    opened.registry.advance_launch(
        launch_id, LaunchState.PLANNED, LaunchState.AUTHORIZED, now=22
    )
    opened.registry.advance_launch(
        launch_id, LaunchState.AUTHORIZED, LaunchState.STARTED, now=23
    )
    pane = tmux.spawn_surface(
        prefix=configured.tmux.naming_prefix,
        generation_id=configured.generation_id,
        view_id=VIEW,
        frame_id=str(frame_id),
        surface_id=str(surface_id),
        command=("/usr/bin/sleep", "60"),
    )
    server = tmux.server_evidence(configured.host.host_id, observed_at=23)
    opened.registry.record_tmux_server(server)
    assert opened.registry.get_view(VIEW).tmux_server_id == server.tmux_server_id
    surface = opened.registry.publish_surface(
        surface_id,
        0,
        server.tmux_server_id,
        pane.pane_id,
        process_id=pane.process_id,
        now=24,
    )
    opened.registry.bind_surface_session(
        surface_id, surface.metadata_generation, parent_key, now=25
    )
    placement = opened.registry.list_placements(view_id=VIEW)[0]
    placement = opened.registry.attach_surface_to_placement(
        placement.placement_id, placement.generation, surface_id, now=25
    )
    tmux.present_surface(
        prefix=configured.tmux.naming_prefix,
        generation_id=configured.generation_id,
        view_id=VIEW,
        mode=ViewMode.DIRECT,
        surface_id=str(surface_id),
    )
    tmux.set_pane_input(
        generation_id=configured.generation_id,
        view_id=VIEW,
        pane_id=pane.pane_id,
        enabled=True,
    )
    opened.registry.issue_capability(
        AgentCapability(
            CapabilityId("cccccccc-1111-4ccc-8ccc-111111111111"),
            sha256(PARENT_TOKEN.encode()).hexdigest(),
            configured.host.host_id,
            VIEW,
            frame_id,
            parent_key,
            surface_id,
            launch_id,
            server.tmux_server_id,
            pane.pane_id,
            placement.generation,
            26,
            10_000,
            None,
        )
    )
    fake = _fake_provider(tmp_path)
    generated_capabilities = iter((CHILD_TOKEN, RESUMED_PARENT_TOKEN))
    workflow = WorkflowRuntime(
        opened,
        paths,
        tmux=tmux,
        allocator=Allocator(),
        contracts={
            ProviderId.CODEX: ProviderContract(ProviderId.CODEX, str(fake), "0.144.6")
        },
        capability_factory=lambda: next(generated_capabilities),
    )
    return workflow, tmux, PARENT_TOKEN, CHILD_TOKEN


def _empty_runtime(
    tmp_path: Path,
) -> tuple[WorkflowRuntime, TmuxExecutor, FrameId, WorkspaceAllocator]:
    configured = build_config(tmp_path)
    opened = OpenGeneration(
        configured.generation_id,
        configured,
        Registry(
            tmp_path / "switchboard.db",
            generation_id=configured.generation_id,
            local_host_id=configured.host.host_id,
            local_display_name=configured.host.display_name,
            initial_activation_state=ActivationState.COMMITTED,
            now=10,
        ),
        ActivationState.COMMITTED,
    )
    opened.registry.materialize_catalog(
        configured.host.host_id,
        configured.projects,
        configured.repositories,
        configured.project_repositories,
        configured.checkouts,
        now=11,
    )
    tmux = TmuxExecutor(tmp_path / "tmux.sock")
    paths = GenerationPaths(tmp_path / "config", tmp_path / "state")
    created = ViewRuntime(opened, paths, tmux=tmux).create_project_view(
        configured.projects[0].project_id,
        request_id=RequestId("71717171-1111-4711-8711-111111111111"),
        mode=ViewMode.DIRECT,
        view_id=VIEW,
        now=20,
    )
    assert created.view.active_frame_id is not None
    allocator = WorkspaceAllocator()
    workflow = WorkflowRuntime(
        opened,
        paths,
        tmux=tmux,
        allocator=allocator,
        contracts={
            ProviderId.CODEX: ProviderContract(
                ProviderId.CODEX,
                str(_fake_provider(tmp_path)),
                "0.144.6",
            )
        },
        capability_factory=lambda: WORKSPACE_TOKEN,
    )
    return workflow, tmux, created.view.active_frame_id, allocator


def test_fresh_workspace_start_claims_context_and_enables_task_push(
    tmp_path: Path,
) -> None:
    workflow, tmux, frame_id, _allocator = _empty_runtime(tmp_path)
    request = RequestId("71717171-2222-4722-8722-222222222222")
    try:
        started = workflow.start_workspace_session(
            frame_id,
            request_id=request,
            now=21,
        )
        assert started.runtime_presence is RuntimePresence.LIVE
        assert (
            workflow.start_workspace_session(
                frame_id,
                request_id=request,
                now=22,
            )
            == started
        )
        frame = workflow.registry.get_frame(frame_id)
        assert frame.current_session_key == started.session_key
        context = workflow.registry.get_work_context(frame.work_context_id)
        assert context.claim_state.value == "held"
        assert context.foreground_frame_id == frame_id
        placement = workflow.registry.list_placements(view_id=VIEW)[0]
        assert placement.state is PlacementState.ACTIVE
        assert placement.surface_id is not None
        capability = workflow.registry.validate_capability(WORKSPACE_TOKEN, now=22)
        assert capability.frame_id == frame_id
        pushed = workflow.task_push(
            WORKSPACE_TOKEN,
            title="Child task",
            brief="Prove the fresh public workspace path can push one task.",
            request_id=RequestId("71717171-3333-4733-8733-333333333333"),
            now=23,
        )
        assert pushed.state is TransitionState.PREPARED
    finally:
        workflow.opened.close()
        if tmux.socket_path is not None:
            subprocess.run(
                ["tmux", "-S", tmux.socket_path, "kill-server"],
                check=False,
                capture_output=True,
            )


def test_fresh_workspace_start_rolls_back_before_provider_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, tmux, frame_id, allocator = _empty_runtime(tmp_path)

    def reject_authorization(*_args: object, **_kwargs: object) -> None:
        raise ConflictError("injected", "authorization did not commit")

    monkeypatch.setattr(workflow.registry, "advance_launch", reject_authorization)
    try:
        with pytest.raises(ConflictError):
            workflow.start_workspace_session(
                frame_id,
                request_id=RequestId("71717171-4444-4744-8744-444444444444"),
                now=21,
            )
        frame = workflow.registry.get_frame(frame_id)
        assert frame.current_session_key is None
        context = workflow.registry.get_work_context(frame.work_context_id)
        assert context.claim_state.value == "released"
        assert context.foreground_frame_id is None
        placement = workflow.registry.list_placements(view_id=VIEW)[0]
        assert placement.state is PlacementState.ACTIVE
        assert placement.surface_id is None
        assert workflow.registry.list_surfaces() == ()
        assert allocator.cleaned == [PARENT_SESSION_ID]
        assert not any(pane.surface_id is not None for pane in tmux.panes())
    finally:
        workflow.opened.close()
        if tmux.socket_path is not None:
            subprocess.run(
                ["tmux", "-S", tmux.socket_path, "kill-server"],
                check=False,
                capture_output=True,
            )


def test_fresh_workspace_start_preserves_bundle_when_rollback_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, tmux, frame_id, allocator = _empty_runtime(tmp_path)

    def reject_authorization(*_args: object, **_kwargs: object) -> None:
        raise ConflictError("injected", "authorization did not commit")

    def reject_rollback(*_args: object, **_kwargs: object) -> None:
        raise ConflictError("injected", "rollback did not commit")

    monkeypatch.setattr(workflow.registry, "advance_launch", reject_authorization)
    monkeypatch.setattr(
        workflow.registry,
        "rollback_workspace_start",
        reject_rollback,
    )
    try:
        with pytest.raises(WorkflowError) as caught:
            workflow.start_workspace_session(
                frame_id,
                request_id=RequestId("71717171-5555-4755-8755-555555555555"),
                now=21,
            )
        assert getattr(caught.value, "code", None) == (
            "workspace_start_rollback_uncertain"
        )
        frame = workflow.registry.get_frame(frame_id)
        assert frame.current_session_key is not None
        context = workflow.registry.get_work_context(frame.work_context_id)
        assert context.claim_state.value == "held"
        placement = workflow.registry.list_placements(view_id=VIEW)[0]
        assert placement.state is PlacementState.STAGED
        assert placement.surface_id is not None
        assert allocator.cleaned == []
        assert any(
            pane.surface_id == str(placement.surface_id) for pane in tmux.panes()
        )
    finally:
        workflow.opened.close()
        if tmux.socket_path is not None:
            subprocess.run(
                ["tmux", "-S", tmux.socket_path, "kill-server"],
                check=False,
                capture_output=True,
            )


def test_workspace_child_complete_return_is_exact_and_single_turn(
    tmp_path: Path,
) -> None:
    workflow, tmux, parent_token, child_token = _runtime(tmp_path)
    try:
        push = workflow.task_push(
            parent_token,
            title="Child task",
            brief="Implement and verify the bounded child outcome.",
            request_id=RequestId("dddddddd-1111-4ddd-8ddd-111111111111"),
            now=30,
        )
        assert push.state is TransitionState.PREPARED
        child_frame = workflow.registry.get_frame(push.target_frame_id)
        assert child_frame.current_session_key is not None
        child_environment = workflow._provider_environment(
            raw_capability=child_token,
            transition=workflow.registry.get_transition(push.transition_id),
            session_key=child_frame.current_session_key,
        )
        assert child_environment["SWB_V3_GENERATION_ID"] == str(workflow.generation_id)
        pushed = workflow.trusted_stop(parent_token, now=31)
        assert pushed.state is TransitionState.AWAITING_CLAIM
        control = workflow.observe_prompt(
            child_token, prompt_id="child-initial", now=32
        )
        assert control is not None
        assert control.submission_count == 1
        claim = workflow.claim(child_token, now=33)
        assert claim.brief == "Implement and verify the bounded child outcome."
        assert (
            workflow.trusted_stop(child_token, now=34).state
            is TransitionState.COMPLETED
        )

        complete = workflow.task_complete_return(
            child_token,
            summary="The bounded child outcome is complete.",
            next_action="Continue the workspace roadmap.",
            request_id=RequestId("dddddddd-2222-4ddd-8ddd-222222222222"),
            now=35,
        )
        assert complete.state is TransitionState.PREPARED
        returned = workflow.trusted_stop(child_token, now=36)
        assert returned.state is TransitionState.AWAITING_CLAIM
        parent_control = workflow.observe_prompt(
            parent_token, prompt_id="parent-control", now=37
        )
        assert parent_control is not None
        assert parent_control.submission_count == 1
        handoff = workflow.claim(parent_token, now=38)
        assert handoff.summary == "The bounded child outcome is complete."
        assert handoff.next_action == "Continue the workspace roadmap."
        settled = workflow.trusted_stop(parent_token, now=39)
        assert settled.state is TransitionState.COMPLETED
        assert (
            workflow.registry.get_view(VIEW).active_frame_id == handoff.target_frame_id
        )
        child = workflow.registry.get_frame(complete.source_frame_id)
        assert child.lifecycle_state.value == "closed"
        assert child.current_session_key is not None
        assert (
            workflow.registry.get_provider_session(
                child.current_session_key
            ).runtime_presence
            is RuntimePresence.STOPPED
        )
    finally:
        workflow.opened.close()
        if tmux.socket_path is not None:
            subprocess.run(
                ["tmux", "-S", tmux.socket_path, "kill-server"],
                check=False,
                capture_output=True,
                env={**os.environ, "TMUX": ""},
            )


def _enter_child(workflow: WorkflowRuntime) -> tuple[object, object]:
    push = workflow.task_push(
        PARENT_TOKEN,
        title="Child task",
        brief="Perform one bounded child outcome.",
        request_id=RequestId("eeeeeeee-1111-4eee-8eee-111111111111"),
        now=50,
    )
    workflow.trusted_stop(PARENT_TOKEN, now=51)
    workflow.observe_prompt(CHILD_TOKEN, prompt_id="child-control", now=52)
    workflow.claim(CHILD_TOKEN, now=53)
    handle_trusted_event(
        workflow,
        ProviderId.CODEX,
        _codex_payload(workflow, CHILD_TOKEN, "Stop", "child-ready"),
        _hook_environment(workflow, CHILD_TOKEN),
        now=54,
    )
    return push, workflow.registry.get_frame(push.target_frame_id)


def _hook_environment(workflow: WorkflowRuntime, raw_capability: str) -> dict[str, str]:
    capability = workflow.registry.validate_capability(raw_capability, now=100)
    assert capability.session_key is not None
    surface = workflow.registry.get_surface(capability.surface_id)
    return {
        "AGENT_SWITCHBOARD_CAPABILITY": raw_capability,
        "AGENT_SWITCHBOARD_LAUNCH_ID": str(surface.launch_id),
        "AGENT_SWITCHBOARD_SURFACE_ID": str(surface.surface_id),
        "SWB_V3_SESSION_KEY": str(capability.session_key),
    }


def _codex_payload(
    workflow: WorkflowRuntime, raw_capability: str, event: str, turn: str
) -> dict[str, str]:
    capability = workflow.registry.validate_capability(raw_capability, now=100)
    assert capability.session_key is not None
    frame = workflow.registry.get_frame(capability.frame_id)
    context = workflow.registry.get_work_context(frame.work_context_id)
    return {
        "hook_event_name": event,
        "session_id": str(capability.session_key.provider_session_id),
        "cwd": str(workflow.registry.checkout_path(context.checkout_id)),
        "turn_id": turn,
    }


def test_trusted_hooks_route_stop_and_prompt_without_semantic_payload(
    tmp_path: Path,
) -> None:
    workflow, tmux, parent_token, child_token = _runtime(tmp_path)
    try:
        push = workflow.task_push(
            parent_token,
            title="Child task",
            brief="Claim only after trusted hook transfer.",
            request_id=RequestId("abababab-1111-4aba-8aba-111111111111"),
            now=80,
        )
        stopped = handle_trusted_event(
            workflow,
            ProviderId.CODEX,
            _codex_payload(workflow, parent_token, "Stop", "source-turn"),
            _hook_environment(workflow, parent_token),
            now=81,
        )
        assert stopped.action == "executed"
        observed = handle_trusted_event(
            workflow,
            ProviderId.CODEX,
            _codex_payload(workflow, child_token, "UserPromptSubmit", "child-control"),
            _hook_environment(workflow, child_token),
            now=82,
        )
        assert observed.action == "observed"
        assert workflow.claim(child_token, now=83).brief is not None
        settled = handle_trusted_event(
            workflow,
            ProviderId.CODEX,
            _codex_payload(workflow, child_token, "Stop", "child-turn"),
            _hook_environment(workflow, child_token),
            now=84,
        )
        assert settled.action == "settled"
        assert workflow.registry.get_transition(push.transition_id).state is (
            TransitionState.COMPLETED
        )
    finally:
        workflow.opened.close()
        if tmux.socket_path is not None:
            subprocess.run(
                ["tmux", "-S", tmux.socket_path, "kill-server"],
                check=False,
                capture_output=True,
            )


def test_back_is_model_free_and_leaves_child_open(tmp_path: Path) -> None:
    workflow, tmux, _parent_token, child_token = _runtime(tmp_path)
    try:
        _push, child = _enter_child(workflow)
        back = workflow.task_back(
            child_token,
            request_id=RequestId("eeeeeeee-2222-4eee-8eee-222222222222"),
            now=55,
        )
        result = workflow.trusted_stop(child_token, now=56)
        assert result.state is TransitionState.COMPLETED
        assert workflow.registry.control_turn_for_transition(back.transition_id) is None
        assert (
            workflow.registry.get_frame(child.frame_id).lifecycle_state.value == "open"
        )
        assert workflow.registry.get_view(VIEW).active_frame_id == child.parent_frame_id
    finally:
        workflow.opened.close()
        if tmux.socket_path is not None:
            subprocess.run(
                ["tmux", "-S", tmux.socket_path, "kill-server"],
                check=False,
                capture_output=True,
            )


def test_human_close_presents_parent_then_dismisses_exact_child(
    tmp_path: Path,
) -> None:
    workflow, tmux, _parent_token, child_token = _runtime(tmp_path)
    try:
        _push, child = _enter_child(workflow)
        close = workflow.task_human_close(
            child_token,
            request_id=RequestId("eeeeeeee-3333-4eee-8eee-333333333333"),
            now=55,
        )
        result = workflow.trusted_stop(child_token, now=56)
        assert result.state is TransitionState.COMPLETED
        assert (
            workflow.registry.control_turn_for_transition(close.transition_id) is None
        )
        closed = workflow.registry.get_frame(child.frame_id)
        assert closed.lifecycle_state.value == "closed"
        assert closed.close_reason is not None
        assert closed.close_reason.value == "dismissed"
        assert closed.current_session_key is not None
        assert (
            workflow.registry.get_provider_session(
                closed.current_session_key
            ).runtime_presence
            is RuntimePresence.STOPPED
        )
        assert workflow.registry.get_view(VIEW).active_frame_id == child.parent_frame_id
    finally:
        workflow.opened.close()
        if tmux.socket_path is not None:
            subprocess.run(
                ["tmux", "-S", tmux.socket_path, "kill-server"],
                check=False,
                capture_output=True,
            )


@pytest.mark.parametrize(
    ("action", "kind", "lifecycle", "close_reason"),
    [
        ("human_back", "back", "open", None),
        ("human_close", "human_close", "closed", "dismissed"),
    ],
)
def test_idle_human_action_returns_model_free_without_agent_prompt(
    tmp_path: Path,
    action: str,
    kind: str,
    lifecycle: str,
    close_reason: str | None,
) -> None:
    workflow, tmux, _parent_token, _child_token = _runtime(tmp_path)
    try:
        _push, child = _enter_child(workflow)
        transition = getattr(workflow, action)(
            VIEW,
            request_id=RequestId(
                "eeeeeeee-7777-4eee-8eee-777777777777"
                if action == "human_back"
                else "eeeeeeee-8888-4eee-8eee-888888888888"
            ),
            now=55,
        )
        assert transition.kind.value == kind
        assert transition.state is TransitionState.COMPLETED
        assert (
            workflow.registry.control_turn_for_transition(transition.transition_id)
            is None
        )
        current = workflow.registry.get_frame(child.frame_id)
        assert current.lifecycle_state.value == lifecycle
        assert (
            None if current.close_reason is None else current.close_reason.value
        ) == close_reason
        assert workflow.registry.get_view(VIEW).active_frame_id == child.parent_frame_id
    finally:
        workflow.opened.close()
        if tmux.socket_path is not None:
            subprocess.run(
                ["tmux", "-S", tmux.socket_path, "kill-server"],
                check=False,
                capture_output=True,
            )


def test_uncertain_control_watchdog_never_submits_twice(tmp_path: Path) -> None:
    workflow, tmux, parent_token, child_token = _runtime(tmp_path)
    try:
        _push, _child = _enter_child(workflow)
        complete = workflow.task_complete_return(
            child_token,
            summary="Watchdog handoff remains durable.",
            next_action="Claim without reinjection.",
            request_id=RequestId("eeeeeeee-5555-4eee-8eee-555555555555"),
            now=57,
        )
        workflow.trusted_stop(child_token, now=58)
        early = workflow.control_watchdog(complete.transition_id, now=5_057)
        first = workflow.control_watchdog(complete.transition_id, now=5_058)
        second = workflow.control_watchdog(complete.transition_id, now=5_059)
        assert early.state.value == "submitted"
        assert first.state.value == "uncertain"
        assert second.state.value == "uncertain"
        assert second.submission_count == 1
        observed = workflow.observe_prompt(
            parent_token, prompt_id="late-exact-observation", now=5_060
        )
        assert observed is not None
        assert observed.submission_count == 1
        assert workflow.claim(parent_token, now=5_061).summary is not None
        assert (
            workflow.trusted_stop(parent_token, now=5_062).state
            is TransitionState.COMPLETED
        )
        recovery = workflow.registry.connection.execute(
            "SELECT recovery_id, state FROM recoveries "
            "WHERE kind = 'control_submit_uncertain' "
            "AND subject_type = 'transition' AND subject_id = ?",
            (str(complete.transition_id),),
        ).fetchone()
        assert recovery is not None
        assert recovery["state"] == "resolved"

        # Reconcile a legacy record persisted by a release that completed the
        # transition without resolving its earlier timeout recovery.
        with workflow.registry.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE recoveries SET state = 'open' WHERE recovery_id = ?",
                (recovery["recovery_id"],),
            )
        assert workflow.reconcile_control_turns(now=5_063) == ()
        assert (
            workflow.registry.get_recovery(recovery["recovery_id"]).state.value
            == "resolved"
        )
    finally:
        workflow.opened.close()
        if tmux.socket_path is not None:
            subprocess.run(
                ["tmux", "-S", tmux.socket_path, "kill-server"],
                check=False,
                capture_output=True,
            )


def test_resume_only_stops_only_verified_idle_parent_and_resumes_exact_uuid(
    tmp_path: Path,
) -> None:
    workflow, tmux, _parent_token, child_token = _runtime(tmp_path)
    try:
        _push, child = _enter_child(workflow)
        workflow.config = replace(
            workflow.config,
            control_turns=ControlTurnsConfig(ControlTurnPolicy.RESUME_ONLY, 5),
        )
        complete = workflow.task_complete_return(
            child_token,
            summary="Resume-only handoff is durable.",
            next_action="Claim through the exact parent UUID.",
            request_id=RequestId("eeeeeeee-9999-4eee-8eee-999999999999"),
            now=5_100,
        )
        parent = workflow.registry.get_frame(child.parent_frame_id)
        assert parent.current_session_key is not None
        assert (
            workflow.registry.get_provider_session(
                parent.current_session_key
            ).runtime_presence
            is RuntimePresence.STOPPED
        )
        control = workflow.registry.control_turn_for_transition(complete.transition_id)
        assert control is not None
        assert control.transport.value == "resume_initial"
        result = workflow.trusted_stop(child_token, now=5_101)
        assert result.state is TransitionState.AWAITING_CLAIM
        resumed = workflow.registry.get_provider_session(parent.current_session_key)
        assert resumed.runtime_presence is RuntimePresence.LIVE
        assert resumed.provider_session_id == PARENT_SESSION_ID
    finally:
        workflow.opened.close()
        if tmux.socket_path is not None:
            subprocess.run(
                ["tmux", "-S", tmux.socket_path, "kill-server"],
                check=False,
                capture_output=True,
            )


def test_back_resumes_exact_stopped_parent_uuid_without_model_turn(
    tmp_path: Path,
) -> None:
    workflow, tmux, _parent_token, child_token = _runtime(tmp_path)
    try:
        _push, child = _enter_child(workflow)
        assert child.parent_frame_id is not None
        parent_placement = next(
            item
            for item in workflow.registry.list_placements(view_id=VIEW)
            if item.frame_id == child.parent_frame_id
        )
        assert parent_placement.surface_id is not None
        parent_surface = workflow.registry.get_surface(parent_placement.surface_id)
        assert parent_surface.pane_id is not None
        tmux.stop_surface(
            generation_id=workflow.generation_id,
            view_id=VIEW,
            surface_id=str(parent_surface.surface_id),
            pane_id=parent_surface.pane_id,
        )
        workflow.registry.advance_surface_state(
            parent_surface.surface_id,
            parent_surface.metadata_generation,
            SurfaceState.DEAD,
            now=65,
        )
        workflow.registry.advance_placement(
            parent_placement.placement_id,
            parent_placement.generation,
            PlacementState.STOPPED_AFFINITY,
            now=65,
        )
        back = workflow.task_back(
            child_token,
            request_id=RequestId("eeeeeeee-6666-4eee-8eee-666666666666"),
            now=66,
        )
        result = workflow.trusted_stop(child_token, now=67)
        assert result.state is TransitionState.COMPLETED
        resumed = workflow.registry.validate_capability(RESUMED_PARENT_TOKEN, now=68)
        assert resumed.session_key is not None
        assert resumed.session_key.provider_session_id == PARENT_SESSION_ID
        launch = workflow.registry.get_launch(resumed.launch_id)
        assert launch.action is LaunchAction.RESUME
        assert launch.state is LaunchState.BOUND
        assert workflow.registry.control_turn_for_transition(back.transition_id) is None
    finally:
        workflow.opened.close()
        if tmux.socket_path is not None:
            subprocess.run(
                ["tmux", "-S", tmux.socket_path, "kill-server"],
                check=False,
                capture_output=True,
            )


def test_prepared_push_cancel_removes_only_staged_owned_resources(
    tmp_path: Path,
) -> None:
    workflow, tmux, parent_token, _child_token = _runtime(tmp_path)
    try:
        push = workflow.task_push(
            parent_token,
            title="Child task",
            brief="This prepared child will be cancelled.",
            request_id=RequestId("eeeeeeee-4444-4eee-8eee-444444444444"),
            now=60,
        )
        workflow.cancel_push(parent_token, push.transition_id, now=61)
        assert (
            workflow.registry.find_transition_by_request(
                workflow.host_id,
                RequestId("eeeeeeee-4444-4eee-8eee-444444444444"),
            )
            is None
        )
        assert isinstance(workflow.allocator, Allocator)
        assert workflow.allocator.cleaned == [CHILD_SESSION_ID]
    finally:
        workflow.opened.close()
        if tmux.socket_path is not None:
            subprocess.run(
                ["tmux", "-S", tmux.socket_path, "kill-server"],
                check=False,
                capture_output=True,
            )


def test_replacement_mcp_exposes_only_capability_derived_frame_tools(
    tmp_path: Path,
) -> None:
    workflow, tmux, parent_token, _child_token = _runtime(tmp_path)
    try:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "switchboard_current",
                    "arguments": {},
                    "_meta": {"progressToken": 3},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "switchboard_mode",
                    "arguments": {"mode": "navigator"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "task_push",
                    "arguments": {
                        "title": "Child task",
                        "brief": "Prepare via exact MCP authority.",
                        "request_id": "ffffffff-1111-4fff-8fff-111111111111",
                    },
                    "_meta": {"openai/toolCallId": "phase6f-live-shape"},
                },
            },
        ]
        source = io.BytesIO(
            b"".join(
                json.dumps(message, separators=(",", ":")).encode() + b"\n"
                for message in messages
            )
        )
        output = io.BytesIO()
        service = AgentToolService(workflow, parent_token, now=70)
        assert run_mcp_server(service, source, output) == 0
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        listed = responses[1]["result"]["tools"]
        assert [tool["name"] for tool in listed] == [tool[0] for tool in TOOLS]
        cancel_tool = next(
            tool for tool in listed if tool["name"] == "transition_cancel"
        )
        assert cancel_tool["annotations"]["idempotentHint"] is False
        assert all(
            not {
                "sourceFrameId",
                "sessionKey",
                "surfaceId",
                "launchId",
                "paneId",
            }
            & set(tool["inputSchema"]["properties"])
            for tool in listed
        )
        assert responses[2]["result"]["structuredContent"]["role"] == "workspace"
        mode = responses[3]["result"]["structuredContent"]
        assert mode["mode"] == "navigator"
        assert workflow.registry.get_view(VIEW).mode is ViewMode.NAVIGATOR
        prepared = responses[4]["result"]["structuredContent"]
        assert prepared["state"] == "prepared"
        assert parent_token not in output.getvalue().decode()
        workflow.cancel_push(
            parent_token,
            workflow.registry.nonterminal_transition_for_view(VIEW).transition_id,
            now=71,
        )
    finally:
        workflow.opened.close()
        if tmux.socket_path is not None:
            subprocess.run(
                ["tmux", "-S", tmux.socket_path, "kill-server"],
                check=False,
                capture_output=True,
            )
