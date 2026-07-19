"""Optional Textual frontend for local Switchboard sessions."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from uuid import UUID, uuid4

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    OptionList,
    Select,
    Static,
)
from textual.widgets.option_list import Option

from .domain import PresentationContext, ProviderId, ValidationError
from .protocol import (
    PresentationPlanEnvelope,
    PresentationPlanKind,
    SessionActionStatus,
)
from .tui_gateway import (
    GatewayError,
    SnapshotSource,
    SwbctlGateway,
    resolve_terminal_context,
)
from .tui_model import FrontendModel, LaunchTarget, SessionRow, ViewFilters

MIN_TERMINAL_WIDTH = 72
MIN_TERMINAL_HEIGHT = 20
WIDE_LAYOUT_WIDTH = 100
ISSUE_DISPLAY_LIMIT = 20
ROW_ISSUE_DISPLAY_LIMIT = 10
_UNASSIGNED_PROJECT = "__unassigned__"

_STATUS_CUES = {
    "needs_input": "! needs input",
    "working": "~ working",
    "completed": "✓ completed",
    "ready": "• ready",
    "parked": "○ parked",
    "offline": "x offline",
    "unavailable": "x unavailable",
    "unknown": "? unknown",
}


class TargetPicker(ModalScreen[LaunchTarget | None]):
    """Choose one declared project/location/provider launch target."""

    BINDINGS = (Binding("escape", "cancel", "Cancel"),)
    CSS = """
    TargetPicker {
        align: center middle;
    }

    #target-dialog {
        width: 80%;
        max-width: 96;
        height: 80%;
        max-height: 28;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }

    #target-title {
        height: 2;
        text-style: bold;
    }

    #target-list {
        height: 1fr;
    }

    #target-help {
        height: 2;
    }
    """

    def __init__(
        self,
        title: str,
        targets: Sequence[LaunchTarget],
    ) -> None:
        super().__init__()
        self.title = title
        self.targets = tuple(targets)

    def compose(self) -> ComposeResult:
        with Vertical(id="target-dialog"):
            yield Static(self.title, id="target-title", markup=False)
            yield OptionList(
                *(
                    Option(_target_label(target), id=str(index))
                    for index, target in enumerate(self.targets)
                ),
                id="target-list",
                markup=False,
            )
            yield Static("Enter selects · Esc cancels", id="target-help", markup=False)

    def on_mount(self) -> None:
        self.query_one("#target-list", OptionList).focus()

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        self.dismiss(self.targets[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)


class StopConfirmation(ModalScreen[bool]):
    """Require an explicit confirmation before requesting safe stop."""

    BINDINGS = (
        Binding("y", "confirm", "Stop"),
        Binding("n", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel"),
    )
    CSS = """
    StopConfirmation {
        align: center middle;
    }

    #stop-dialog {
        width: 64;
        max-width: 90%;
        height: 9;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, row: SessionRow) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        with Vertical(id="stop-dialog"):
            yield Static(
                f"Stop {self.row.label}?\n\n"
                "Switchboard will revalidate launch ownership before stopping it.\n"
                "Press y to stop or n/Esc to cancel.",
                markup=False,
            )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class SwitchboardApp(App[tuple[str, ...] | None]):
    """Phase 4A terminal session index and validated action router."""

    TITLE = "Switchboard"
    SUB_TITLE = "Terminal session router"
    BINDINGS = (
        Binding("/", "focus_search", "Search"),
        Binding("o", "open_session", "Open"),
        Binding("n", "new_session", "New"),
        Binding("h", "history", "History"),
        Binding("x", "stop_session", "Stop"),
        Binding("r", "refresh", "Refresh"),
        Binding("ctrl+l", "clear_filters", "Clear filters"),
        Binding("e", "focus_issues", "Issues"),
        Binding("?", "toggle_help", "Help"),
        Binding("escape", "focus_sessions", "Sessions", show=False),
        Binding("q", "quit", "Quit"),
    )
    CSS = """
    Screen {
        min-width: 40;
    }

    #body {
        height: 1fr;
    }

    #search {
        height: 3;
        margin: 0 1;
    }

    #filters {
        layout: horizontal;
        height: 3;
        margin: 0 1;
    }

    #filters.narrow {
        layout: grid;
        grid-size: 3 2;
        grid-columns: 1fr 1fr 1fr;
        grid-rows: 3 3;
        height: 6;
    }

    .filter-select {
        width: 1fr;
    }

    #status {
        height: 1;
        padding: 0 1;
    }

    #content {
        height: 1fr;
        layout: horizontal;
        margin: 0 1;
    }

    #sessions {
        width: 2fr;
        height: 1fr;
        border: round $primary;
    }

    #side-panel {
        width: 1fr;
        height: 1fr;
        border: round $secondary;
        padding: 0 1;
    }

    #content.narrow {
        layout: vertical;
    }

    #content.narrow #sessions {
        width: 1fr;
        height: 2fr;
    }

    #content.narrow #side-panel {
        width: 1fr;
        height: 1fr;
    }

    .panel-heading {
        text-style: bold;
        margin-top: 1;
    }

    #help {
        display: none;
        height: auto;
        max-height: 6;
        margin: 0 1;
        padding: 0 1;
        border: round $accent;
    }
    """

    def __init__(
        self,
        *,
        gateway: SwbctlGateway,
        terminal_context: PresentationContext,
        now_ms: Callable[[], int] | None = None,
        request_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        super().__init__()
        self.gateway = gateway
        self.snapshots = SnapshotSource(gateway)
        self.terminal_context = terminal_context
        self.model: FrontendModel | None = None
        self.refreshing = False
        self.last_error: GatewayError | None = None
        self.action_busy = False
        self.action_label: str | None = None
        self.action_message: str | None = None
        self.action_error: GatewayError | None = None
        self._now_ms = (lambda: int(time.time() * 1000)) if now_ms is None else now_ms
        self._request_id_factory = request_id_factory
        self._snapshot_request_id = 0
        self._filter_request_id = 0
        self._snapshot_mode = "retained"
        self._rendering_table = False
        self._help_visible = False
        self._initial_snapshot_rendered = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="body"):
            yield Input(
                placeholder="Search sessions by name, project, path, status, or ID",
                id="search",
                disabled=True,
            )
            with Grid(id="filters"):
                yield Select[str](
                    (("Codex", "codex"), ("Claude", "claude")),
                    prompt="Provider: all",
                    id="provider-filter",
                    classes="filter-select",
                    disabled=True,
                )
                yield Select[str](
                    (),
                    prompt="Project: all",
                    id="project-filter",
                    classes="filter-select",
                    disabled=True,
                )
                yield Select[str](
                    (
                        ("Needs input", "needs_input"),
                        ("Working", "working"),
                        ("Completed", "completed"),
                        ("Ready", "ready"),
                        ("Unknown", "unknown"),
                    ),
                    prompt="Activity: all",
                    id="activity-filter",
                    classes="filter-select",
                    disabled=True,
                )
                yield Select[str](
                    (("Live", "live"), ("Stopped", "stopped"), ("Unknown", "unknown")),
                    prompt="Runtime: all",
                    id="runtime-filter",
                    classes="filter-select",
                    disabled=True,
                )
                yield Select[str](
                    (
                        ("Attached", "attached"),
                        ("Detached", "detached"),
                        ("No surface", "none"),
                        ("Unknown", "unknown"),
                    ),
                    prompt="Attachment: all",
                    id="attachment-filter",
                    classes="filter-select",
                    disabled=True,
                )
            yield Static("Loading retained sessions…", id="status", markup=False)
            with Vertical(id="content"):
                yield DataTable(
                    show_row_labels=False,
                    cursor_type="row",
                    zebra_stripes=True,
                    id="sessions",
                    disabled=True,
                )
                with VerticalScroll(id="side-panel", can_focus=True):
                    yield Static(
                        "Session details", classes="panel-heading", markup=False
                    )
                    yield Static(
                        "Select a session row to inspect it.",
                        id="details",
                        markup=False,
                    )
                    yield Static("Issues", classes="panel-heading", markup=False)
                    yield Static("No current issues.", id="issues", markup=False)
            yield Static(
                "/ search · arrows navigate · Enter/o open · n new · h Claude history\n"
                "x safe stop · r refresh · Ctrl+L clear filters · e issues · "
                "? help · q quit",
                id="help",
                markup=False,
            )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sessions", DataTable)
        table.add_column("Status", key="status", width=14)
        table.add_column("Session", key="session", width=24)
        table.add_column("Project", key="project", width=18)
        table.add_column("Provider", key="provider", width=8)
        table.add_column("Attachment", key="attachment", width=11)
        table.add_column("Last", key="last", width=10)
        self._apply_responsive_layout(self.size.width)
        self.set_interval(30, self._render_status, name="snapshot-age")
        self._request_snapshot(full=False)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_responsive_layout(event.size.width)

    def _apply_responsive_layout(self, width: int) -> None:
        narrow = width < WIDE_LAYOUT_WIDTH
        self.query_one("#filters").set_class(narrow, "narrow")
        self.query_one("#content").set_class(narrow, "narrow")

    def _request_snapshot(self, *, full: bool) -> None:
        self._snapshot_request_id += 1
        self._filter_request_id += 1
        request_id = self._snapshot_request_id
        self._snapshot_mode = "full" if full else "retained"
        self.refreshing = True
        self._render_status()
        self._load_snapshot(full, request_id)

    @work(exclusive=True, group="snapshot", exit_on_error=False)
    async def _load_snapshot(self, full: bool, request_id: int) -> None:
        try:
            snapshot = (
                await self.snapshots.refresh()
                if full
                else await self.snapshots.retained()
            )
            now_ms = self._now_ms()
            current = self.model
            model = await asyncio.to_thread(
                FrontendModel.from_snapshot
                if current is None
                else current.apply_snapshot,
                snapshot,
                now_ms=now_ms,
            )
            source_error = self.snapshots.last_error
            if source_error is not None:
                model = model.with_frontend_error(
                    source_error.code,
                    source_error.message,
                    retryable=source_error.retryable,
                    observed_at=now_ms,
                )
            if request_id != self._snapshot_request_id:
                return
            self.model = model
            self.last_error = source_error
            self._refresh_project_options()
            self._set_controls_disabled(False)
            self._render_rows()
            if not self._initial_snapshot_rendered:
                self.query_one("#sessions", DataTable).focus()
                self._initial_snapshot_rendered = True
        except GatewayError as error:
            if request_id != self._snapshot_request_id:
                return
            self.last_error = error
            if self.model is not None:
                self.model = self.model.with_frontend_error(
                    error.code,
                    error.message,
                    retryable=error.retryable,
                    observed_at=self._now_ms(),
                )
            self._render_issues()
        except ValidationError:
            if request_id != self._snapshot_request_id:
                return
            error = GatewayError(
                "frontend_model_invalid",
                "The validated snapshot could not be displayed.",
                retryable=False,
            )
            self.last_error = error
            self._render_issues()
        finally:
            if request_id == self._snapshot_request_id:
                self.refreshing = False
                self._render_status()

    def _set_controls_disabled(self, disabled: bool) -> None:
        self.query_one("#search", Input).disabled = disabled
        for select in self.query(".filter-select").results(Select):
            select.disabled = disabled
        self.query_one("#sessions", DataTable).disabled = disabled

    def _refresh_project_options(self) -> None:
        model = self.model
        if model is None:
            return
        select = self.query_one("#project-filter", Select)
        previous = select.selection
        projects = {
            row.project_id: row.project_name
            for row in model.rows
            if row.project_id is not None and row.project_name is not None
        }
        options: list[tuple[Text, str]] = [
            (Text(name), project_id)
            for project_id, name in sorted(
                projects.items(),
                key=lambda item: (item[1].casefold(), item[1], item[0]),
            )
        ]
        if any(row.project_id is None for row in model.rows):
            options.append((Text("Unassigned"), _UNASSIGNED_PROJECT))
        select.set_options(options)
        if previous is not None and any(value == previous for _, value in options):
            select.value = previous

    def on_input_changed(self, _event: Input.Changed) -> None:
        self._request_filter()

    def on_select_changed(self, _event: Select.Changed) -> None:
        self._request_filter()

    def _request_filter(self) -> None:
        model = self.model
        if model is None:
            return
        try:
            filters = self._current_filters()
        except ValidationError:
            return
        self._filter_request_id += 1
        self._filter_model(model, filters, self._filter_request_id)

    def _current_filters(self) -> ViewFilters:
        def selection(widget_id: str) -> str | None:
            return self.query_one(widget_id, Select).selection

        provider = selection("#provider-filter")
        project = selection("#project-filter")
        activity = selection("#activity-filter")
        runtime = selection("#runtime-filter")
        attachment = selection("#attachment-filter")
        return ViewFilters(
            query=self.query_one("#search", Input).value,
            providers=frozenset(() if provider is None else (provider,)),
            project_ids=frozenset(
                ()
                if project is None
                else (None if project == _UNASSIGNED_PROJECT else project,)
            ),
            activities=frozenset(() if activity is None else (activity,)),
            runtime_presences=frozenset(() if runtime is None else (runtime,)),
            attachments=frozenset(() if attachment is None else (attachment,)),
        )

    @work(exclusive=True, group="filter", exit_on_error=False)
    async def _filter_model(
        self,
        base: FrontendModel,
        filters: ViewFilters,
        request_id: int,
    ) -> None:
        filtered = await asyncio.to_thread(base.with_filters, filters)
        if request_id != self._filter_request_id:
            return
        current = self.model
        if current is None or current.rows is not base.rows:
            return
        if current.selected_session_key is not None and any(
            row.session_key == current.selected_session_key
            for row in filtered.visible_rows
        ):
            filtered = filtered.with_selection(current.selected_session_key)
        self.model = filtered
        self._render_rows()

    def _render_rows(self) -> None:
        table = self.query_one("#sessions", DataTable)
        model = self.model
        self._rendering_table = True
        try:
            table.clear()
            if model is not None:
                for row in model.visible_rows:
                    table.add_row(
                        Text(_status_cue(row)),
                        Text(row.label),
                        Text(row.project_name or "Unassigned"),
                        Text(row.provider.value),
                        Text(row.attachment.value),
                        Text(_format_age(row.recency_at, self._now_ms())),
                        key=row.session_key,
                    )
                if model.selected_session_key is not None:
                    try:
                        index = table.get_row_index(model.selected_session_key)
                    except KeyError:
                        pass
                    else:
                        table.move_cursor(row=index, column=0, animate=False)
        finally:
            self._rendering_table = False
        self._render_details()
        self._render_issues()
        self._render_status()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not self.is_running or self._rendering_table or self.model is None:
            return
        session_key = event.row_key.value
        if not isinstance(session_key, str):
            return
        try:
            self.model = self.model.with_selection(session_key)
        except ValidationError:
            return
        self._render_details()

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self.action_open_session()

    def _render_details(self) -> None:
        details = self.query_one("#details", Static)
        model = self.model
        row = None if model is None else model.selected_row
        if row is None:
            if model is None:
                details.update("Select a session row to inspect it.")
            elif not model.rows:
                details.update("No sessions are currently known. Press r to refresh.")
            else:
                details.update("No sessions match the current search and filters.")
            return
        project = row.project_name or "Unassigned"
        location = row.location_name or row.location_path or "Unassigned"
        working_directory = row.cwd or row.location_path or "Unknown"
        warning_lines = [
            f"- {model.issue(issue_id).code}: {model.issue(issue_id).message}"
            for issue_id in row.issue_ids[:ROW_ISSUE_DISPLAY_LIMIT]
        ]
        if len(row.issue_ids) > ROW_ISSUE_DISPLAY_LIMIT:
            warning_lines.append(
                f"- … {len(row.issue_ids) - ROW_ISSUE_DISPLAY_LIMIT} more issue(s)"
            )
        warning_text = "\n".join(warning_lines) if warning_lines else "None"
        details.update(
            f"{row.label}\n"
            f"Status: {_status_cue(row)}\n"
            f"Runtime: {row.runtime_presence.value}\n"
            f"Attachment: {row.attachment.value}\n"
            f"Provider: {row.provider.value}\n"
            f"Project: {project}\n"
            f"Location: {location}\n"
            f"Working directory: {working_directory}\n"
            f"Last activity: {_format_age(row.recency_at, self._now_ms())}\n"
            f"Safe stop eligibility: {'eligible' if row.can_stop else 'not eligible'}\n"
            f"Session key: {row.session_key}\n\n"
            f"Warnings:\n{warning_text}"
        )

    def _render_issues(self) -> None:
        widget = self.query_one("#issues", Static)
        model = self.model
        issues = () if model is None else model.issues
        command_errors = tuple(
            error for error in (self.action_error, self.last_error) if error is not None
        )
        if not issues and not command_errors:
            widget.update("No current issues.")
            return
        ordered_issues = tuple(
            sorted(
                issues,
                key=lambda issue: (
                    issue.source.value != "frontend",
                    issue.issue_id,
                ),
            )
        )
        lines: list[str] = []
        unprojected_errors = tuple(
            error
            for error in command_errors
            if not any(
                issue.source.value == "frontend" and issue.code == error.code
                for issue in ordered_issues
            )
        )
        lines.extend(
            f"- frontend/{error.code}: {error.message}"
            for error in unprojected_errors[:ISSUE_DISPLAY_LIMIT]
        )
        lines.extend(
            f"- {issue.source.value}/{issue.code}: {issue.message}"
            for issue in ordered_issues[: ISSUE_DISPLAY_LIMIT - len(lines)]
        )
        issue_count = len(ordered_issues) + len(unprojected_errors)
        if issue_count > ISSUE_DISPLAY_LIMIT:
            lines.append(f"- … {issue_count - ISSUE_DISPLAY_LIMIT} more issue(s)")
        widget.update("\n".join(lines))

    def _render_status(self) -> None:
        if not self.is_mounted:
            return
        parts = [
            f"Refreshing {self._snapshot_mode} snapshot…"
            if self.refreshing
            else "Ready"
        ]
        model = self.model
        if model is None:
            parts.append("no snapshot")
        else:
            parts.append(f"{len(model.visible_rows)}/{len(model.rows)} sessions")
            now_ms = self._now_ms()
            parts.append(f"snapshot {_format_age(model.generated_at, now_ms)}")
            if model.is_stale or now_ms - model.generated_at > model.stale_after_ms:
                parts.append("STALE")
            if model.issues:
                parts.append(f"{len(model.issues)} issue(s)")
            parts.append(
                "providers "
                + ", ".join(
                    f"{capability.provider.value} {capability.status.value}"
                    for capability in model.capabilities
                )
            )
        if self.last_error is not None:
            parts.append(f"ERROR {self.last_error.code}")
        if self.action_busy:
            parts.append(f"ACTION {self.action_label or 'working'}…")
        elif self.action_error is not None:
            parts.append(f"ACTION ERROR {self.action_error.code}")
        elif self.action_message is not None:
            parts.append(self.action_message)
        parts.append(
            "tmux client"
            if self.terminal_context.current_tmux_client is not None
            else "plain terminal"
        )
        self.query_one("#status", Static).update(" · ".join(parts))

    def _publish_action_error(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        self.action_error = GatewayError(code, message, retryable=retryable)
        self.action_message = None
        self._render_issues()
        self._render_status()

    def _begin_action(self, label: str) -> bool:
        if self.action_busy:
            return False
        self.action_busy = True
        self.action_label = label
        self.action_message = None
        self.action_error = None
        self._render_issues()
        self._render_status()
        return True

    def _new_request_id(self) -> str:
        return str(self._request_id_factory())

    def _finish_action(self) -> None:
        self.action_busy = False
        self.action_label = None
        if self.is_mounted:
            self._render_issues()
            self._render_status()

    async def _apply_plan(self, envelope: PresentationPlanEnvelope) -> None:
        plan = envelope.plan
        if plan.kind is PresentationPlanKind.BLOCKED:
            if plan.error is None:
                raise GatewayError(
                    "response_invalid",
                    "The Switchboard plan is incompatible with this terminal.",
                    retryable=False,
                )
            self._publish_action_error(
                plan.error.code,
                plan.error.message,
                retryable=plan.error.retryable,
            )
            return
        if plan.kind is PresentationPlanKind.FOCUS:
            raise GatewayError(
                "response_invalid",
                "The Switchboard plan is incompatible with this terminal.",
                retryable=False,
            )
        if plan.surface_id is None:
            raise GatewayError(
                "response_invalid",
                "The Switchboard plan is incompatible with this terminal.",
                retryable=False,
            )
        surface_id = str(plan.surface_id)
        if plan.kind is PresentationPlanKind.ATTACH:
            command = self.gateway.attach_surface_command(surface_id)
            self.action_message = "Attaching selected surface"
            self.exit(command)
            return
        client = self.terminal_context.current_tmux_client
        if client is None:
            raise GatewayError(
                "response_invalid",
                "The Switchboard plan is incompatible with this terminal.",
                retryable=False,
            )
        await self.gateway.select_surface(surface_id, client=client)
        self.action_message = "Selected surface on current tmux client"
        self.exit()

    def action_open_session(self) -> None:
        if self.action_busy:
            return
        row = None if self.model is None else self.model.selected_row
        if row is None:
            self._publish_action_error(
                "session_not_selected",
                "Select a known session before opening it.",
                retryable=False,
            )
            return
        if self._begin_action(f"opening {row.label}"):
            self._prepare_open(row.session_key, self._new_request_id())

    @work(exclusive=False, group="action", exit_on_error=False)
    async def _prepare_open(self, session_key: str, request_id: str) -> None:
        try:
            envelope = await self.gateway.prepare_open(
                session_key,
                request_id=request_id,
                context=self.terminal_context,
            )
            await self._apply_plan(envelope)
        except GatewayError as error:
            self._publish_action_error(
                error.code,
                error.message,
                retryable=error.retryable,
            )
        finally:
            self._finish_action()

    def action_new_session(self) -> None:
        if self.action_busy:
            return
        targets = () if self.model is None else self.model.launch_targets
        if not targets:
            self._publish_action_error(
                "launch_target_unavailable",
                "No configured local launch targets are available.",
                retryable=False,
            )
            return
        self.push_screen(
            TargetPicker("Start a configured session", targets),
            self._on_new_target,
        )

    def _on_new_target(self, target: LaunchTarget | None) -> None:
        if target is None:
            return
        if self._begin_action(
            f"starting {target.provider.value} in {target.project_name}"
        ):
            self._prepare_new(target, self._new_request_id())

    @work(exclusive=False, group="action", exit_on_error=False)
    async def _prepare_new(self, target: LaunchTarget, request_id: str) -> None:
        try:
            envelope = await self.gateway.prepare_new(
                target.project_id,
                location_id=target.location_id,
                provider=target.provider.value,
                request_id=request_id,
                context=self.terminal_context,
            )
            await self._apply_plan(envelope)
        except GatewayError as error:
            self._publish_action_error(
                error.code,
                error.message,
                retryable=error.retryable,
            )
        finally:
            self._finish_action()

    def action_history(self) -> None:
        if self.action_busy:
            return
        targets = tuple(
            target
            for target in (() if self.model is None else self.model.launch_targets)
            if target.provider is ProviderId.CLAUDE
        )
        if not targets:
            self._publish_action_error(
                "history_target_unavailable",
                "No configured local Claude history targets are available.",
                retryable=False,
            )
            return
        self.push_screen(
            TargetPicker("Open Claude history", targets),
            self._on_history_target,
        )

    def _on_history_target(self, target: LaunchTarget | None) -> None:
        if target is None:
            return
        if self._begin_action(f"opening Claude history in {target.project_name}"):
            self._prepare_history(target, self._new_request_id())

    @work(exclusive=False, group="action", exit_on_error=False)
    async def _prepare_history(self, target: LaunchTarget, request_id: str) -> None:
        try:
            envelope = await self.gateway.prepare_history(
                target.project_id,
                location_id=target.location_id,
                request_id=request_id,
                context=self.terminal_context,
            )
            await self._apply_plan(envelope)
        except GatewayError as error:
            self._publish_action_error(
                error.code,
                error.message,
                retryable=error.retryable,
            )
        finally:
            self._finish_action()

    def action_stop_session(self) -> None:
        if self.action_busy:
            return
        row = None if self.model is None else self.model.selected_row
        if row is None:
            self._publish_action_error(
                "session_not_selected",
                "Select a known session before stopping it.",
                retryable=False,
            )
            return
        if not row.can_stop:
            self._publish_action_error(
                "stop_not_eligible",
                "The selected session is not eligible for safe stop.",
                retryable=False,
            )
            return
        self.push_screen(
            StopConfirmation(row),
            lambda confirmed: self._on_stop_confirmed(row, confirmed),
        )

    def _on_stop_confirmed(self, row: SessionRow, confirmed: bool) -> None:
        if not confirmed:
            return
        if self._begin_action(f"stopping {row.label}"):
            self._stop_session(row.session_key)

    @work(exclusive=False, group="action", exit_on_error=False)
    async def _stop_session(self, session_key: str) -> None:
        try:
            envelope = await self.gateway.stop_session(session_key)
            action = envelope.action
            if action.status is SessionActionStatus.BLOCKED:
                if action.error is None:
                    raise GatewayError(
                        "response_invalid",
                        "The Switchboard command emitted an incompatible response.",
                        retryable=False,
                    )
                self._publish_action_error(
                    action.error.code,
                    action.error.message,
                    retryable=action.error.retryable,
                )
                return
            self.action_message = (
                "Session stopped"
                if action.status is SessionActionStatus.STOPPED
                else "Session was already stopped"
            )
            self._request_snapshot(full=False)
        except GatewayError as error:
            self._publish_action_error(
                error.code,
                error.message,
                retryable=error.retryable,
            )
        finally:
            self._finish_action()

    def action_focus_search(self) -> None:
        search = self.query_one("#search", Input)
        if not search.disabled:
            search.focus()

    def action_focus_sessions(self) -> None:
        table = self.query_one("#sessions", DataTable)
        if not table.disabled:
            table.focus()

    def action_focus_issues(self) -> None:
        self.query_one("#side-panel", VerticalScroll).focus()

    def action_clear_filters(self) -> None:
        if self.model is None:
            return
        self.query_one("#search", Input).value = ""
        for select in self.query(".filter-select").results(Select):
            select.clear()
        self._request_filter()

    def action_refresh(self) -> None:
        self._request_snapshot(full=True)

    def action_toggle_help(self) -> None:
        self._help_visible = not self._help_visible
        self.query_one("#help", Static).display = self._help_visible


def _status_cue(row: SessionRow) -> str:
    return _STATUS_CUES[row.status.value]


def _target_label(target: LaunchTarget) -> str:
    location = target.location_name or target.location_path
    qualifiers = []
    if target.is_default:
        qualifiers.append("default location")
    if target.is_preferred_provider:
        qualifiers.append("preferred provider")
    suffix = "" if not qualifiers else f" ({', '.join(qualifiers)})"
    return (
        f"{target.project_name} · {location} · {target.provider.value}{suffix}\n"
        f"  {target.location_path}"
    )


def _format_age(timestamp_ms: int, now_ms: int) -> str:
    seconds = max(0, (now_ms - timestamp_ms) // 1000)
    if seconds < 60:
        return "now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _execute_terminal_handoff(
    command: tuple[str, ...],
    *,
    exec_replace: Callable[[str, Sequence[str]], object] = os.execv,
) -> int:
    """Replace the restored terminal process with one public attach command."""

    try:
        exec_replace(command[0], command)
    except OSError:
        print(
            "swbctl: could not attach the selected surface; terminal restored",
            file=sys.stderr,
        )
        return 1
    print(
        "swbctl: surface attachment unexpectedly returned; terminal restored",
        file=sys.stderr,
    )
    return 1


def run_tui(*, swbctl_executable: str | Path) -> int:
    """Run the optional terminal frontend."""

    command = SwitchboardApp(
        gateway=SwbctlGateway(swbctl_executable),
        terminal_context=resolve_terminal_context(),
    ).run()
    return 0 if command is None else _execute_terminal_handoff(command)


__all__ = [
    "MIN_TERMINAL_HEIGHT",
    "MIN_TERMINAL_WIDTH",
    "StopConfirmation",
    "SwitchboardApp",
    "TargetPicker",
    "run_tui",
]
