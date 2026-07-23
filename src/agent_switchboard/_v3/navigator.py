"""Resident Textual navigator over bounded Phase 6 state and CLI actions."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol
from uuid import uuid4

from .domain import ViewId, ViewMode
from .generation import GenerationPaths, open_generation
from .process import ProcessError, run_bounded_command
from .protocol import NavigatorState, build_navigator_from_registry
from .views import ViewRuntime

ACTION_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class NavigatorProject:
    host_id: str
    project_id: str
    name: str
    view_id: str | None
    entry_frame_id: str | None
    frames: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class NavigatorView:
    host_id: str
    view_id: str
    title: str
    mode: str
    state: str
    activity: str
    attention: str
    transition_state: str | None
    breadcrumb: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NavigatorModel:
    host_id: str
    view_id: str
    mode: str
    view_state: str
    activity: str
    attention: str
    transition_state: str | None
    control_state: str | None
    active_frame_id: str | None
    active_project_id: str | None
    breadcrumb: str
    views: tuple[NavigatorView, ...]
    projects: tuple[NavigatorProject, ...]
    recoveries: tuple[dict[str, Any], ...]
    hosts: tuple[dict[str, Any], ...]
    warnings: tuple[dict[str, Any], ...]

    @classmethod
    def from_state(cls, state: NavigatorState, view_id: ViewId) -> NavigatorModel:
        data = state.to_dict()
        views = [row for row in data["views"] if row["viewId"] == str(view_id)]
        if len(views) != 1:
            raise ValueError("navigator view is absent from current state")
        view = views[0]
        projects = tuple(
            NavigatorProject(
                str(row["hostId"]),
                str(row["projectId"]),
                str(row["name"]),
                None if row["viewId"] is None else str(row["viewId"]),
                None if row["entryFrameId"] is None else str(row["entryFrameId"]),
                tuple(dict(frame) for frame in row["frames"]),
            )
            for row in data["projects"]
        )
        active_frame_id = view["activeFrameId"]
        active_project = next(
            (
                project
                for project in projects
                if any(frame["frameId"] == active_frame_id for frame in project.frames)
            ),
            None,
        )
        host = next(row for row in data["hosts"] if row["hostId"] == view["hostId"])
        breadcrumbs = [str(item) for item in view["breadcrumb"]]
        if not breadcrumbs or breadcrumbs[0] != str(host["displayName"]):
            breadcrumbs.insert(0, str(host["displayName"]))
        return cls(
            str(view["hostId"]),
            str(view_id),
            str(view["mode"]),
            str(view["state"]),
            str(view["activity"]),
            str(view["attention"]),
            (None if view["transitionState"] is None else str(view["transitionState"])),
            None if view["controlState"] is None else str(view["controlState"]),
            None if active_frame_id is None else str(active_frame_id),
            None if active_project is None else active_project.project_id,
            " / ".join(breadcrumbs),
            tuple(
                NavigatorView(
                    str(row["hostId"]),
                    str(row["viewId"]),
                    str(row["title"]),
                    str(row["mode"]),
                    str(row["state"]),
                    str(row["activity"]),
                    str(row["attention"]),
                    (
                        None
                        if row["transitionState"] is None
                        else str(row["transitionState"])
                    ),
                    tuple(str(item) for item in row["breadcrumb"]),
                )
                for row in data["views"]
            ),
            projects,
            tuple(dict(row) for row in data["recoveries"]),
            tuple(dict(row) for row in data["hosts"]),
            tuple(dict(row) for row in data["warnings"]),
        )


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    ok: bool
    code: str | None = None
    message: str | None = None
    payload: dict[str, Any] | None = None


class ActionRunner(Protocol):
    def __call__(self, arguments: list[str]) -> Awaitable[ActionOutcome]: ...


def build_model(
    opened: Any,
    paths: GenerationPaths,
    view_id: ViewId,
    *,
    generated_at: int,
) -> NavigatorModel:
    health = ViewRuntime(opened, paths).observe_health(now=generated_at)
    state = build_navigator_from_registry(
        opened.registry,
        generated_at=generated_at,
        view_state_overrides=health.view_states,
        additional_warnings=health.warnings,
    )
    return NavigatorModel.from_state(state, view_id)


def _command_prefix(paths: GenerationPaths) -> list[str]:
    return [
        sys.executable,
        "-m",
        f"{__package__}.cli",
        "--config-root",
        str(paths.config_root),
        "--state-root",
        str(paths.state_root),
    ]


def bounded_action_runner(paths: GenerationPaths) -> ActionRunner:
    async def run(arguments: list[str]) -> ActionOutcome:
        try:
            output = await run_bounded_command(
                [*_command_prefix(paths), *arguments],
                timeout_seconds=ACTION_TIMEOUT_SECONDS,
            )
        except ProcessError as error:
            return ActionOutcome(False, error.code, str(error))
        payload: dict[str, Any] | None = None
        if output.stdout:
            try:
                decoded = json.loads(output.stdout)
                if isinstance(decoded, dict):
                    payload = decoded
            except (UnicodeDecodeError, json.JSONDecodeError):
                return ActionOutcome(
                    False, "action_output_invalid", "action returned invalid JSON"
                )
        if output.exit_code != 0:
            error = None if payload is None else payload.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message")
                if isinstance(code, str) and isinstance(message, str):
                    return ActionOutcome(False, code, message, payload)
            return ActionOutcome(
                False,
                "action_failed",
                f"action exited with status {output.exit_code}",
                payload,
            )
        if output.stderr:
            return ActionOutcome(
                False, "action_diagnostic", "action emitted unexpected diagnostics"
            )
        return ActionOutcome(True, payload=payload)

    return run


def create_navigator_app(
    paths: GenerationPaths,
    view_id: ViewId,
    *,
    action_runner: ActionRunner | None = None,
    opened_factory: Callable[[GenerationPaths], Any] = open_generation,
):
    """Build a testable resident app with an injected bounded action runner."""

    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import VerticalScroll
    from textual.widgets import Footer, OptionList, Static, TabbedContent, TabPane
    from textual.widgets.option_list import Option

    runner = action_runner or bounded_action_runner(paths)

    class Phase6Navigator(App[None]):
        CSS = """
        Screen { layout: vertical; }
        #crumb { height: auto; padding: 0 1; text-style: bold; }
        #status { height: auto; padding: 0 1; color: $text-muted; }
        #action-status { height: auto; padding: 0 1; }
        TabbedContent { height: 1fr; }
        OptionList { height: 1fr; }
        .panel { padding: 0 1; }
        Footer { height: 1; }
        """
        BINDINGS: ClassVar[list[Binding]] = [
            Binding("r", "refresh_state", "Refresh"),
            Binding("n", "start_workspace", "Start"),
            Binding("d", "direct", "Direct"),
            Binding("b", "back", "Back"),
            Binding("c", "close_task", "Close"),
            Binding("y", "confirm_background", "Confirm"),
            Binding("v", "show_tab('views')", "Views"),
            Binding("p", "show_tab('projects')", "Projects"),
            Binding("t", "show_tab('tasks')", "Tasks"),
            Binding("h", "show_tab('history')", "History"),
            Binding("x", "show_tab('recovery')", "Recovery"),
            Binding("s", "show_tab('settings')", "Settings"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.opened = opened_factory(paths)
            self.model = build_model(self.opened, paths, view_id, generated_at=_now())
            self.action_pending = False
            self.action_status = "ready"
            self.confirmation: tuple[list[str], str] | None = None

        def compose(self) -> ComposeResult:
            yield Static(self.model.breadcrumb, id="crumb")
            yield Static(id="status")
            yield Static(self.action_status, id="action-status")
            with TabbedContent(initial="views", id="panels"):
                with TabPane("Views", id="views"):
                    yield OptionList(id="view-list")
                with TabPane("Projects", id="projects"):
                    yield OptionList(id="project-list")
                with TabPane("Tasks", id="tasks"):
                    yield OptionList(id="task-list")
                with TabPane("History", id="history"):
                    yield OptionList(id="history-list")
                with TabPane("Recovery", id="recovery"):
                    yield OptionList(id="recovery-list")
                with TabPane("Settings", id="settings"):
                    yield VerticalScroll(Static(id="settings-body"), classes="panel")
            yield Footer()

        def on_mount(self) -> None:
            self._render_model()
            self.set_interval(1.0, self._refresh_local)
            self.set_interval(
                self.opened.config.defaults.refresh_interval_seconds,
                self._schedule_remote_refresh,
            )

        def on_unmount(self) -> None:
            self.opened.close()

        def _status_text(self) -> str:
            transition = self.model.transition_state or "none"
            control = self.model.control_state or "none"
            return (
                f"{self.model.mode} · {self.model.view_state} · "
                f"activity {self.model.activity} · attention {self.model.attention} · "
                f"transition {transition}/{control}"
            )

        @staticmethod
        def _host_available(model: NavigatorModel, host_id: str) -> bool:
            host = next(row for row in model.hosts if row["hostId"] == host_id)
            return bool(host["isLocal"]) or (
                host["reachability"] == "online" and not host["stale"]
            )

        def _render_model(self) -> None:
            self.query_one("#crumb", Static).update(self.model.breadcrumb)
            self.query_one("#status", Static).update(self._status_text())
            self.query_one("#action-status", Static).update(self.action_status)

            views = self.query_one("#view-list", OptionList)
            views.clear_options()
            for row in self.model.views:
                marker = ">" if row.view_id == self.model.view_id else " "
                views.add_option(
                    Option(
                        f"{marker} {row.title} [{row.activity}/{row.attention}]",
                        id=f"view:{row.host_id}:{row.view_id}",
                        disabled=not self._host_available(self.model, row.host_id),
                    )
                )

            projects = self.query_one("#project-list", OptionList)
            projects.clear_options()
            tasks = self.query_one("#task-list", OptionList)
            tasks.clear_options()
            history = self.query_one("#history-list", OptionList)
            history.clear_options()
            for project in self.model.projects:
                marker = (
                    ">" if project.project_id == self.model.active_project_id else " "
                )
                available = self._host_available(self.model, project.host_id)
                projects.add_option(
                    Option(
                        f"{marker} {project.name}",
                        id=f"project:{project.host_id}:{project.project_id}",
                        disabled=not available,
                    )
                )
                for frame in project.frames:
                    lifecycle = str(frame["lifecycleState"])
                    label = f"{frame['activity'][:1]} {frame['title']} [{project.name}]"
                    if lifecycle == "closed":
                        history.add_option(Option(label, disabled=True))
                    elif frame["role"] == "task":
                        closing = lifecycle == "closing"
                        if closing:
                            label = (
                                f"{frame['activity'][:1]} {frame['title']} "
                                f"[{project.name}/finishing]"
                            )
                        task_id = (
                            None
                            if project.view_id is None
                            else f"frame:{project.host_id}:{project.view_id}:"
                            f"{frame['frameId']}"
                        )
                        tasks.add_option(
                            Option(
                                label,
                                id=None if closing else task_id,
                                disabled=closing or not available or task_id is None,
                            )
                        )
            if not any(
                frame["lifecycleState"] == "closed"
                for project in self.model.projects
                for frame in project.frames
            ):
                history.add_option(Option("No closed frames", disabled=True))

            recovery = self.query_one("#recovery-list", OptionList)
            recovery.clear_options()
            if not self.model.recoveries:
                recovery.add_option(Option("No open recovery", disabled=True))
            for row in self.model.recoveries:
                host_id = str(row["hostId"])
                recovery.add_option(
                    Option(
                        f"{row['kind']}: {row['explanation']}",
                        id=f"recovery:{host_id}:{row['recoveryId']}",
                        disabled=not self._host_available(self.model, host_id),
                    )
                )

            host_lines = [
                f"{row['displayName']}: {row['reachability']}"
                f"{' stale' if row['stale'] else ''}"
                for row in self.model.hosts
            ]
            warning_lines = [
                f"{row['code']}: {row['message']}" for row in self.model.warnings
            ]
            settings = [
                f"Mode: {self.model.mode}",
                f"View: {self.model.view_id[:8]}",
                "",
                "Hosts",
                *(host_lines or ["none"]),
                "",
                "Warnings",
                *(warning_lines or ["none"]),
            ]
            self.query_one("#settings-body", Static).update("\n".join(settings))

        def _refresh_local(self) -> None:
            try:
                self.model = build_model(
                    self.opened, paths, view_id, generated_at=_now()
                )
            except Exception as error:  # Textual must remain a non-owning surface.
                self.action_status = f"refresh error: {str(error)[:160]}"
            self._render_model()

        def _schedule_remote_refresh(self) -> None:
            if not self.action_pending:
                self.run_worker(
                    self._execute_action(
                        ["state", "navigator", "--refresh"], "remote refresh"
                    ),
                    exclusive=False,
                )

        def _queue(self, arguments: list[str], label: str) -> None:
            if self.action_pending:
                self.action_status = "busy: one action is already running"
                self._render_model()
                return
            self.run_worker(self._execute_action(arguments, label), exclusive=False)

        async def _execute_action(
            self,
            arguments: list[str],
            label: str,
            *,
            confirmed: bool = False,
        ) -> ActionOutcome:
            if self.action_pending:
                return ActionOutcome(False, "action_busy", "another action is running")
            self.action_pending = True
            self.action_status = f"pending: {label}"
            self._render_model()
            try:
                outcome = await runner(arguments)
                if outcome.ok:
                    self.confirmation = None
                    self.action_status = f"success: {label}"
                    self._refresh_local()
                elif (
                    outcome.code == "background_confirmation_required" and not confirmed
                ):
                    self.confirmation = (list(arguments), label)
                    self.action_status = (
                        "confirmation required: press y to allow background transfer"
                    )
                else:
                    self.confirmation = None
                    self.action_status = (
                        f"error {outcome.code or 'unknown'}: "
                        f"{(outcome.message or 'action failed')[:160]}"
                    )
                return outcome
            finally:
                self.action_pending = False
                self._render_model()

        def _enter_arguments(self, host_id: str) -> list[str]:
            return [
                "view",
                "enter",
                "--host",
                host_id,
                "--mode",
                ViewMode.NAVIGATOR.value,
                "--request-id",
                str(uuid4()),
            ]

        def action_refresh_state(self) -> None:
            self._queue(["state", "navigator", "--refresh"], "refresh")

        def action_start_workspace(self) -> None:
            active = next(
                (
                    frame
                    for project in self.model.projects
                    for frame in project.frames
                    if frame["frameId"] == self.model.active_frame_id
                ),
                None,
            )
            if (
                active is None
                or active["role"] != "workspace"
                or active["lifecycleState"] != "open"
                or active["currentSession"] is not None
            ):
                self.action_status = "start unavailable: foreground is not empty"
                self._render_model()
                return
            self._queue(
                [
                    "frame",
                    "start",
                    "--host",
                    self.model.host_id,
                    "--frame",
                    str(active["frameId"]),
                    "--request-id",
                    str(uuid4()),
                ],
                "start workspace",
            )

        def action_direct(self) -> None:
            self._queue(
                [
                    "view",
                    "enter",
                    "--host",
                    self.model.host_id,
                    "--view",
                    self.model.view_id,
                    "--mode",
                    ViewMode.DIRECT.value,
                    "--request-id",
                    str(uuid4()),
                ],
                "direct mode",
            )

        def action_back(self) -> None:
            self._queue(
                [
                    "view",
                    "back",
                    "--view",
                    self.model.view_id,
                    "--request-id",
                    str(uuid4()),
                ],
                "back",
            )

        def action_close_task(self) -> None:
            self._queue(
                [
                    "view",
                    "close",
                    "--view",
                    self.model.view_id,
                    "--request-id",
                    str(uuid4()),
                ],
                "human close",
            )

        def action_confirm_background(self) -> None:
            if self.confirmation is None:
                self.action_status = "nothing is awaiting confirmation"
                self._render_model()
                return
            arguments, label = self.confirmation
            self.confirmation = None
            self.run_worker(
                self._execute_action(
                    [*arguments, "--confirm-background-transfer"],
                    label,
                    confirmed=True,
                ),
                exclusive=False,
            )

        def action_show_tab(self, tab_id: str) -> None:
            self.query_one("#panels", TabbedContent).active = tab_id

        def on_option_list_option_selected(
            self, event: OptionList.OptionSelected
        ) -> None:
            option_id = event.option.id
            if option_id is None:
                return
            fields = str(option_id).split(":")
            kind = fields[0]
            if kind == "view" and len(fields) == 3:
                _kind, host_id, selected_view = fields
                self._queue(
                    [
                        *self._enter_arguments(host_id),
                        "--view",
                        selected_view,
                    ],
                    "enter view",
                )
            elif kind == "project" and len(fields) == 3:
                _kind, host_id, project_id = fields
                arguments = [
                    *self._enter_arguments(host_id),
                    "--project",
                    project_id,
                ]
                if host_id == self.model.host_id:
                    arguments.extend(("--reuse-view", self.model.view_id))
                self._queue(arguments, "enter project")
            elif kind == "frame" and len(fields) == 4:
                _kind, host_id, selected_view, frame_id = fields
                self._queue(
                    [
                        *self._enter_arguments(host_id),
                        "--view",
                        selected_view,
                        "--frame",
                        frame_id,
                    ],
                    "enter task",
                )
            elif kind == "recovery" and len(fields) == 3:
                _kind, host_id, recovery_id = fields
                self._queue(
                    [
                        *self._enter_arguments(host_id),
                        "--recovery",
                        recovery_id,
                    ],
                    "open recovery",
                )

    return Phase6Navigator()


def run_navigator(paths: GenerationPaths, view_id: ViewId) -> int:
    try:
        app = create_navigator_app(paths, view_id)
    except ImportError:
        print(
            "Phase 6 navigator requires Textual; reinstall agent-switchboard.",
            file=sys.stderr,
        )
        return 2
    app.run()
    return 0


def _now() -> int:
    return int(time.time() * 1_000)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"python -m {__package__}.navigator")
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--view", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    return run_navigator(
        GenerationPaths(arguments.config_root, arguments.state_root),
        ViewId(arguments.view),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ActionOutcome",
    "NavigatorModel",
    "NavigatorProject",
    "NavigatorView",
    "bounded_action_runner",
    "build_model",
    "create_navigator_app",
    "main",
]
