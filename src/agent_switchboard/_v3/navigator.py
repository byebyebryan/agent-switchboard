"""Compact resident navigator for the private Phase 6 view shell."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from .domain import ViewId, ViewMode
from .generation import GenerationPaths, open_generation
from .protocol import NavigatorState, build_navigator_from_registry


@dataclass(frozen=True, slots=True)
class NavigatorProject:
    host_id: str
    project_id: str
    name: str
    view_id: str | None
    entry_frame_id: str | None
    frames: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class NavigatorModel:
    view_id: str
    mode: str
    view_state: str
    active_frame_id: str | None
    active_project_id: str | None
    breadcrumb: str
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
                (None if row["entryFrameId"] is None else str(row["entryFrameId"])),
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
        active_frame = None
        if active_project is not None:
            active_frame = next(
                (
                    frame
                    for frame in active_project.frames
                    if frame["frameId"] == active_frame_id
                ),
                None,
            )
        host = next(row for row in data["hosts"] if row["hostId"] == view["hostId"])
        crumbs = [str(host["displayName"])]
        if active_project is not None:
            crumbs.append(active_project.name)
        if active_frame is not None:
            crumbs.append(str(active_frame["title"]))
        return cls(
            str(view_id),
            str(view["mode"]),
            str(view["state"]),
            None if active_frame_id is None else str(active_frame_id),
            None if active_project is None else active_project.project_id,
            " / ".join(crumbs),
            projects,
            tuple(dict(row) for row in data["recoveries"]),
            tuple(dict(row) for row in data["hosts"]),
            tuple(dict(row) for row in data["warnings"]),
        )


def build_model(opened: Any, view_id: ViewId, *, generated_at: int) -> NavigatorModel:
    state = build_navigator_from_registry(
        opened.registry,
        generated_at=generated_at,
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


def run_navigator(paths: GenerationPaths, view_id: ViewId) -> int:
    """Run Textual lazily so state/CLI commands have no TUI dependency."""

    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import VerticalScroll
        from textual.widgets import Footer, OptionList, Static, TabbedContent, TabPane
        from textual.widgets.option_list import Option
    except ImportError:
        print(
            "Phase 6 navigator requires the 'tui' extra: "
            "install agent-switchboard[tui]",
            file=sys.stderr,
        )
        return 2

    class Phase6Navigator(App[None]):
        CSS = """
        Screen { layout: vertical; }
        #crumb { height: auto; padding: 0 1; text-style: bold; }
        #status { height: auto; padding: 0 1; color: $text-muted; }
        TabbedContent { height: 1fr; }
        OptionList { height: 1fr; }
        .panel { padding: 0 1; }
        Footer { height: 1; }
        """
        BINDINGS: ClassVar[list[Binding]] = [
            Binding("r", "refresh_state", "Refresh"),
            Binding("d", "direct", "Direct"),
            Binding("b", "back", "Back"),
            Binding("c", "close_task", "Close task"),
            Binding("p", "show_tab('projects')", "Projects"),
            Binding("h", "show_tab('history')", "History"),
            Binding("x", "show_tab('recovery')", "Recovery"),
            Binding("s", "show_tab('settings')", "Settings"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.opened = open_generation(paths)
            self.model = build_model(self.opened, view_id, generated_at=_now())
            self.commands: list[subprocess.Popen[bytes]] = []

        def compose(self) -> ComposeResult:
            yield Static(self.model.breadcrumb, id="crumb")
            yield Static(f"{self.model.mode} · {self.model.view_state}", id="status")
            with TabbedContent(initial="projects", id="panels"):
                with TabPane("Projects", id="projects"):
                    yield OptionList(id="project-list")
                with TabPane("History", id="history"):
                    yield OptionList(id="history-list")
                with TabPane("Recovery", id="recovery"):
                    yield OptionList(id="recovery-list")
                with TabPane("Settings", id="settings"):
                    yield VerticalScroll(Static(id="settings-body"), classes="panel")
            yield Footer()

        def on_mount(self) -> None:
            self._render_model()
            self.set_interval(0.25, self._reap_commands)

        def on_unmount(self) -> None:
            self.opened.close()

        def _render_model(self) -> None:
            self.query_one("#crumb", Static).update(self.model.breadcrumb)
            self.query_one("#status", Static).update(
                f"{self.model.mode} · {self.model.view_state}"
            )
            projects = self.query_one("#project-list", OptionList)
            projects.clear_options()
            for project in self.model.projects:
                marker = (
                    ">" if project.project_id == self.model.active_project_id else " "
                )
                unavailable = (
                    project.view_id is not None
                    and project.view_id != self.model.view_id
                )
                projects.add_option(
                    Option(
                        f"{marker} {project.name}",
                        id=f"project:{project.project_id}",
                        disabled=unavailable,
                    )
                )
            history = self.query_one("#history-list", OptionList)
            history.clear_options()
            for project in self.model.projects:
                for frame in project.frames:
                    role = "workspace" if frame["role"] == "workspace" else "task"
                    history.add_option(
                        Option(
                            f"{frame['activity'][:1]} {frame['title']} [{role}]",
                            id=f"frame:{frame['frameId']}",
                            disabled=project.view_id != self.model.view_id,
                        )
                    )
            recovery = self.query_one("#recovery-list", OptionList)
            recovery.clear_options()
            if not self.model.recoveries:
                recovery.add_option(Option("No open recovery", disabled=True))
            for row in self.model.recoveries:
                recovery.add_option(
                    Option(
                        f"{row['kind']}: {row['explanation']}",
                        id=f"recovery:{row['recoveryId']}",
                    )
                )
            host_lines = [
                f"{row['displayName']}: {row['reachability']}"
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

        def _spawn(self, arguments: list[str]) -> None:
            self.commands.append(
                subprocess.Popen(
                    [*_command_prefix(paths), *arguments],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            )

        def _reap_commands(self) -> None:
            completed = [
                process for process in self.commands if process.poll() is not None
            ]
            if not completed:
                return
            self.commands = [
                process for process in self.commands if process.poll() is None
            ]
            self.action_refresh_state()

        def action_refresh_state(self) -> None:
            try:
                self.model = build_model(self.opened, view_id, generated_at=_now())
            except Exception as error:  # Textual must remain a non-owning surface.
                self.query_one("#status", Static).update(
                    f"refresh failed: {str(error)[:160]}"
                )
                return
            self._render_model()

        def action_direct(self) -> None:
            self._spawn(
                [
                    "view",
                    "mode",
                    "--view",
                    str(view_id),
                    "--mode",
                    ViewMode.DIRECT.value,
                    "--request-id",
                    str(uuid4()),
                ]
            )

        def action_back(self) -> None:
            self._spawn(
                [
                    "view",
                    "back",
                    "--view",
                    str(view_id),
                    "--request-id",
                    str(uuid4()),
                ]
            )

        def action_close_task(self) -> None:
            self._spawn(
                [
                    "view",
                    "close",
                    "--view",
                    str(view_id),
                    "--request-id",
                    str(uuid4()),
                ]
            )

        def action_show_tab(self, tab_id: str) -> None:
            self.query_one("#panels", TabbedContent).active = tab_id

        def on_option_list_option_selected(
            self, event: OptionList.OptionSelected
        ) -> None:
            option_id = event.option.id
            if option_id is None:
                return
            kind, value = str(option_id).split(":", 1)
            if kind == "project":
                self._spawn(
                    [
                        "view",
                        "open",
                        "--project",
                        value,
                        "--view",
                        str(view_id),
                        "--request-id",
                        str(uuid4()),
                    ]
                )
            elif kind == "frame":
                self._spawn(
                    [
                        "view",
                        "focus",
                        "--view",
                        str(view_id),
                        "--frame",
                        value,
                        "--request-id",
                        str(uuid4()),
                    ]
                )
            elif kind == "recovery":
                self._spawn(["view", "recover", "--view", str(view_id)])

    Phase6Navigator().run()
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


__all__ = ["NavigatorModel", "NavigatorProject", "build_model", "main"]
