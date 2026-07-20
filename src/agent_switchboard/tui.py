"""Optional Textual frontend for local Switchboard sessions."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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

from .domain import (
    PresentationContext,
    ProviderId,
    ValidationError,
    normalize_handoff_text,
)
from .protocol import (
    PresentationPlanEnvelope,
    PresentationPlanKind,
    SessionActionStatus,
    SessionDetailEnvelope,
)
from .tui_gateway import (
    GatewayError,
    SnapshotSource,
    SwbctlGateway,
    resolve_terminal_context,
)
from .tui_model import FrontendModel, LaunchTarget, SessionRow, TaskRow, ViewFilters

MIN_TERMINAL_WIDTH = 72
MIN_TERMINAL_HEIGHT = 20
WIDE_LAYOUT_WIDTH = 100
ISSUE_DISPLAY_LIMIT = 20
ROW_ISSUE_DISPLAY_LIMIT = 10
HANDOFF_DISPLAY_LIMIT = 5
HANDOFF_TEXT_DISPLAY_CHARS = 500
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
    """Choose one declared project/checkout/provider launch target."""

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


class TaskPicker(ModalScreen[TaskRow | None]):
    """Choose one existing open task for an explicit Inbox adoption."""

    BINDINGS = (Binding("escape", "cancel", "Cancel"),)
    CSS = TargetPicker.CSS

    def __init__(self, tasks: Sequence[TaskRow]) -> None:
        super().__init__()
        self.tasks = tuple(tasks)

    def compose(self) -> ComposeResult:
        with Vertical(id="target-dialog"):
            yield Static("Adopt into open task", id="target-title", markup=False)
            yield OptionList(
                *(
                    Option(f"{task.title} · {task.project_name}", id=str(index))
                    for index, task in enumerate(self.tasks)
                ),
                id="target-list",
                markup=False,
            )
            yield Static("Enter selects · Esc cancels", id="target-help", markup=False)

    def on_mount(self) -> None:
        self.query_one("#target-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.tasks[event.option_index])

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


@dataclass(frozen=True, slots=True)
class EditResult:
    value: str | None


class TextEditScreen(ModalScreen[EditResult | None]):
    """Edit or explicitly clear one bounded single-line curation value."""

    BINDINGS = (
        Binding("ctrl+s", "submit", "Save"),
        Binding("ctrl+d", "clear", "Clear", priority=True),
        Binding("escape", "cancel", "Cancel"),
    )
    CSS = """
    TextEditScreen {
        align: center middle;
    }

    #edit-dialog {
        width: 72;
        max-width: 94%;
        height: 12;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }

    #edit-title {
        text-style: bold;
    }

    #edit-error {
        color: $error;
        height: 1;
    }
    """

    def __init__(
        self,
        title: str,
        *,
        value: str | None,
        maximum: int,
    ) -> None:
        super().__init__()
        self.title = title
        self.value = "" if value is None else value
        self.maximum = maximum

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-dialog"):
            yield Static(self.title, id="edit-title", markup=False)
            yield Input(value=self.value, id="edit-value")
            yield Static("", id="edit-error", markup=False)
            yield Static(
                "Enter/Ctrl+S saves · Ctrl+D clears · Esc cancels",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one("#edit-value", Input).focus()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        value = self.query_one("#edit-value", Input).value.strip()
        error = self.query_one("#edit-error", Static)
        if not value:
            error.update("Value must not be empty; use Ctrl+D to clear it.")
            return
        if len(value) > self.maximum:
            error.update(f"Value exceeds {self.maximum} characters.")
            return
        self.dismiss(EditResult(value))

    def action_clear(self) -> None:
        self.dismiss(EditResult(None))

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True, slots=True)
class HandoffDraft:
    summary: str
    next_action: str


class HandoffEditor(ModalScreen[HandoffDraft | None]):
    """Collect one explicit bounded summary and concrete next action."""

    BINDINGS = (
        Binding("ctrl+s", "submit", "Save"),
        Binding("escape", "cancel", "Cancel"),
    )
    CSS = """
    HandoffEditor {
        align: center middle;
    }

    #handoff-dialog {
        width: 80;
        max-width: 96%;
        height: 18;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }

    .handoff-label {
        text-style: bold;
        height: 1;
    }

    #handoff-error {
        color: $error;
        height: 2;
    }
    """

    def __init__(
        self,
        title: str,
        *,
        draft: HandoffDraft | None = None,
    ) -> None:
        super().__init__()
        self.title = title
        self.draft = draft

    def compose(self) -> ComposeResult:
        with Vertical(id="handoff-dialog"):
            yield Static(self.title, classes="handoff-label", markup=False)
            yield Static("Summary", classes="handoff-label", markup=False)
            yield Input(
                value="" if self.draft is None else self.draft.summary,
                id="handoff-summary",
            )
            yield Static("Next action", classes="handoff-label", markup=False)
            yield Input(
                value="" if self.draft is None else self.draft.next_action,
                id="handoff-next-action",
            )
            yield Static("", id="handoff-error", markup=False)
            yield Static("Ctrl+S saves · Esc cancels", markup=False)

    def on_mount(self) -> None:
        self.query_one("#handoff-summary", Input).focus()

    def action_submit(self) -> None:
        try:
            summary = normalize_handoff_text(
                self.query_one("#handoff-summary", Input).value,
                "summary",
            )
            next_action = normalize_handoff_text(
                self.query_one("#handoff-next-action", Input).value,
                "next action",
            )
        except ValidationError as error:
            self.query_one("#handoff-error", Static).update(str(error))
            return
        self.dismiss(HandoffDraft(summary, next_action))

    def action_cancel(self) -> None:
        self.dismiss(None)


class SwitchboardApp(App[tuple[str, ...] | None]):
    """Local session index, curation surface, and validated action router."""

    TITLE = "Switchboard"
    SUB_TITLE = "Terminal session router"
    BINDINGS = (
        Binding("/", "focus_search", "Search"),
        Binding("o", "open_session", "Open"),
        Binding("n", "new_session", "New task"),
        Binding("u", "adopt_session", "Adopt"),
        Binding("z", "close_task", "Close task"),
        Binding("e", "reopen_task", "Reopen task"),
        Binding("1", "show_open", "Open tasks", show=False),
        Binding("2", "show_inbox", "Inbox", show=False),
        Binding("3", "show_closed", "Closed", show=False),
        Binding("h", "history", "History"),
        Binding("x", "stop_session", "Stop"),
        Binding("a", "edit_name", "Name"),
        Binding("p", "edit_purpose", "Purpose"),
        Binding("v", "toggle_pin", "Pin"),
        Binding("g", "handoff", "Handoff"),
        Binding("w", "wrap", "Wrap"),
        Binding("c", "continue_session", "Continue"),
        Binding("d", "reload_detail", "Detail"),
        Binding("r", "refresh", "Refresh"),
        Binding("ctrl+l", "clear_filters", "Clear filters"),
        Binding("i", "focus_issues", "Issues"),
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
        handoff_id_factory: Callable[[], UUID] = uuid4,
        initial_view: str = "open",
    ) -> None:
        super().__init__()
        if initial_view not in {"open", "inbox", "closed"}:
            raise ValueError("initial view is invalid")
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
        self.detail_error: GatewayError | None = None
        self.detail_loading_key: str | None = None
        self._now_ms = (lambda: int(time.time() * 1000)) if now_ms is None else now_ms
        self._request_id_factory = request_id_factory
        self._handoff_id_factory = handoff_id_factory
        self._initial_view = initial_view
        self._snapshot_request_id = 0
        self._detail_request_id = 0
        self._filter_request_id = 0
        self._snapshot_mode = "retained"
        self._rendering_table = False
        self._selected_task_id: str | None = None
        self._pending_new_target: LaunchTarget | None = None
        self._help_visible = False
        self._initial_snapshot_rendered = False
        self._handoff_drafts: dict[tuple[str, bool], tuple[str, HandoffDraft]] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="body"):
            yield Input(
                placeholder="Search tasks or Inbox sessions",
                id="search",
                disabled=True,
            )
            with Grid(id="filters"):
                yield Select[str](
                    (
                        ("Open tasks", "open"),
                        ("Inbox", "inbox"),
                        ("Closed", "closed"),
                    ),
                    value=self._initial_view,
                    id="view-filter",
                    classes="filter-select",
                    disabled=True,
                )
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
            yield Static("Loading retained tasks…", id="status", markup=False)
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
                        "Task or session details", classes="panel-heading", markup=False
                    )
                    yield Static(
                        "Select a row to inspect it.",
                        id="details",
                        markup=False,
                    )
                    yield Static("Issues", classes="panel-heading", markup=False)
                    yield Static("No current issues.", id="issues", markup=False)
            yield Static(
                "/ search · 1 open · 2 Inbox · 3 closed · Enter/o open · n new task\n"
                "u adopt · a title/name · p purpose · v pin · z close · e reopen\n"
                "x safe stop · h Claude history · d detail · r refresh · i issues · "
                "? help · q quit",
                id="help",
                markup=False,
            )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sessions", DataTable)
        table.add_column("Status", key="status", width=22)
        table.add_column("Task / session", key="session", width=24)
        table.add_column("Project", key="project", width=18)
        table.add_column("Provider", key="provider", width=8)
        table.add_column("Checkout / surface", key="attachment", width=18)
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
        projects.update(
            {task.project_id: task.project_name for task in model.task_rows}
        )
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

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "view-filter":
            self._selected_task_id = None
            if self.model is not None:
                self.model = self.model.with_selection(None)
            self._render_rows()
        else:
            self._request_filter()

    def _view_mode(self) -> str:
        selection = self.query_one("#view-filter", Select).selection
        return "open" if selection not in {"open", "inbox", "closed"} else selection

    def _visible_tasks(self) -> tuple[TaskRow, ...]:
        model = self.model
        if model is None:
            return ()
        source = (
            model.closed_tasks if self._view_mode() == "closed" else model.open_tasks
        )
        query = self.query_one("#search", Input).value.casefold().split()
        project = self.query_one("#project-filter", Select).selection
        provider = self.query_one("#provider-filter", Select).selection
        return tuple(
            task
            for task in source
            if all(token in task.search_text for token in query)
            and (
                project is None
                or (project != _UNASSIGNED_PROJECT and task.project_id == project)
            )
            and (
                provider is None
                or (
                    task.current_provider is not None
                    and task.current_provider.value == provider
                )
                or (
                    task.current_provider is None
                    and task.preferred_provider is not None
                    and task.preferred_provider.value == provider
                )
            )
        )

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

    def _request_selected_detail(self, *, force: bool = False) -> None:
        model = self.model
        row = None if model is None else model.selected_row
        if row is None:
            return
        if not force and model.selected_detail is not None:
            return
        if self.detail_loading_key == row.session_key:
            return
        self._detail_request_id += 1
        request_id = self._detail_request_id
        self.detail_loading_key = row.session_key
        self.detail_error = None
        self._render_details()
        self._render_issues()
        self._render_status()
        self._load_detail(row.session_key, request_id)

    def _ensure_view_selection(self) -> None:
        model = self.model
        if model is None:
            return
        if self._view_mode() == "inbox":
            candidates = tuple(row for row in model.visible_rows if row.task_id is None)
            selected = (
                model.selected_session_key
                if any(
                    row.session_key == model.selected_session_key for row in candidates
                )
                else (None if not candidates else candidates[0].session_key)
            )
            self._selected_task_id = None
        else:
            tasks = self._visible_tasks()
            task = next(
                (item for item in tasks if item.task_id == self._selected_task_id),
                None if not tasks else tasks[0],
            )
            self._selected_task_id = None if task is None else task.task_id
            selected = None if task is None else task.current_session_key
            if selected is not None and not any(
                row.session_key == selected for row in model.visible_rows
            ):
                selected = None
        if model.selected_session_key != selected:
            self.model = model.with_selection(selected)

    @work(exclusive=True, group="detail", exit_on_error=False)
    async def _load_detail(self, session_key: str, request_id: int) -> None:
        try:
            envelope = await self.gateway.session_detail(
                session_key,
                handoff_limit=20,
            )
            if request_id != self._detail_request_id or self.model is None:
                return
            self.model = await asyncio.to_thread(self.model.with_detail, envelope)
            self.detail_error = None
        except GatewayError as error:
            if request_id != self._detail_request_id:
                return
            self.detail_error = error
        except ValidationError:
            if request_id != self._detail_request_id:
                return
            self.detail_error = GatewayError(
                "frontend_detail_invalid",
                "The validated session detail could not be displayed.",
                retryable=False,
            )
        finally:
            if request_id == self._detail_request_id:
                self.detail_loading_key = None
                self._render_details()
                self._render_issues()
                self._render_status()

    def _render_rows(self) -> None:
        table = self.query_one("#sessions", DataTable)
        self._ensure_view_selection()
        model = self.model
        self._rendering_table = True
        try:
            table.clear()
            if model is not None and self._view_mode() == "inbox":
                for row in model.visible_rows:
                    if row.task_id is not None:
                        continue
                    table.add_row(
                        Text(_row_cue(row)),
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
            elif model is not None:
                for task in self._visible_tasks():
                    provider = task.current_provider or task.preferred_provider
                    context = task.checkout_name or task.branch or "Default"
                    table.add_row(
                        Text(_STATUS_CUES[task.display_status.value]),
                        Text(task.title),
                        Text(task.project_name),
                        Text("—" if provider is None else provider.value),
                        Text(context),
                        Text(_format_age(task.updated_at, self._now_ms())),
                        key=task.task_id,
                    )
                if self._selected_task_id is not None:
                    try:
                        index = table.get_row_index(self._selected_task_id)
                    except KeyError:
                        pass
                    else:
                        table.move_cursor(row=index, column=0, animate=False)
        finally:
            self._rendering_table = False
        self._render_details()
        self._render_issues()
        self._render_status()
        self._request_selected_detail()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not self.is_running or self._rendering_table or self.model is None:
            return
        row_key = event.row_key.value
        if not isinstance(row_key, str):
            return
        task = next(
            (task for task in self.model.task_rows if task.task_id == row_key), None
        )
        if task is not None:
            self._selected_task_id = task.task_id
            self.model = self.model.with_selection(task.current_session_key)
        else:
            self._selected_task_id = None
            try:
                self.model = self.model.with_selection(row_key)
            except ValidationError:
                return
        self._render_details()
        self._request_selected_detail()

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self.action_open_session()

    def _render_details(self) -> None:
        details = self.query_one("#details", Static)
        model = self.model
        task = self._selected_task()
        if task is not None:
            sessions = tuple(row for row in model.rows if row.task_id == task.task_id)
            provider = task.current_provider or task.preferred_provider
            history = "\n".join(
                f"- {row.provider.value}: {row.label}"
                for row in sorted(
                    sessions, key=lambda item: item.last_observed_at, reverse=True
                )[:HANDOFF_DISPLAY_LIMIT]
            )
            details.update(
                f"{task.title}\n"
                f"Status: {task.status} · {_STATUS_CUES[task.display_status.value]}\n"
                f"Project: {task.project_name}\n"
                f"Checkout: {task.checkout_name or task.branch or 'Default'}\n"
                f"Provider: {'None' if provider is None else provider.value}\n"
                f"Purpose: {task.purpose or 'None'}\n"
                f"Pinned: {'yes' if task.pinned else 'no'}\n"
                f"Current session: {task.current_session_key or 'None'}\n"
                f"Updated: {_format_age(task.updated_at, self._now_ms())}\n"
                f"Task ID: {task.task_id}\n\n"
                f"Session history:\n{history or 'None'}"
            )
            return
        row = None if model is None else model.selected_row
        if row is None:
            if model is None:
                details.update("Select a session row to inspect it.")
            elif not model.rows:
                details.update("No sessions are currently known. Press r to refresh.")
            else:
                details.update("No sessions match the current search and filters.")
            return
        detail = model.selected_detail
        name = row.name if detail is None else detail.name
        purpose = row.purpose if detail is None else detail.purpose
        pinned = row.pinned if detail is None else detail.pinned
        wrapped_at = row.wrapped_at if detail is None else detail.wrapped_at
        latest_handoff_id = (
            row.latest_handoff_id if detail is None else detail.latest_handoff_id
        )
        continued_from_handoff_id = (
            row.continued_from_handoff_id
            if detail is None
            else detail.continued_from_handoff_id
        )
        project = row.project_name or "Unassigned"
        checkout = row.checkout_name or row.checkout_path or "Unassigned"
        working_directory = row.cwd or row.checkout_path or "Unknown"
        warning_lines = [
            f"- {model.issue(issue_id).code}: {model.issue(issue_id).message}"
            for issue_id in row.issue_ids[:ROW_ISSUE_DISPLAY_LIMIT]
        ]
        if len(row.issue_ids) > ROW_ISSUE_DISPLAY_LIMIT:
            warning_lines.append(
                f"- … {len(row.issue_ids) - ROW_ISSUE_DISPLAY_LIMIT} more issue(s)"
            )
        warning_text = "\n".join(warning_lines) if warning_lines else "None"
        if detail is None:
            handoff_text = (
                "Loading…"
                if self.detail_loading_key == row.session_key
                else "Not loaded; press d to retry."
            )
        elif not detail.handoffs:
            handoff_text = "None"
        else:
            handoff_lines: list[str] = []
            for handoff in detail.handoffs[:HANDOFF_DISPLAY_LIMIT]:
                handoff_lines.extend(
                    (
                        f"- #{handoff.sequence} [{handoff.source}] "
                        f"{_bounded_display(handoff.summary)}",
                        f"  Next: {_bounded_display(handoff.next_action)}",
                    )
                )
            omitted = len(detail.handoffs) - HANDOFF_DISPLAY_LIMIT
            if omitted > 0 or detail.handoffs_truncated:
                handoff_lines.append("- … additional older handoffs omitted")
            handoff_text = "\n".join(handoff_lines)
        details.update(
            f"{name or row.label}\n"
            f"Status: {_row_cue(row)}\n"
            f"Runtime: {row.runtime_presence.value}\n"
            f"Attachment: {row.attachment.value}\n"
            f"Provider: {row.provider.value}\n"
            f"Project: {project}\n"
            f"Checkout: {checkout}\n"
            f"Working directory: {working_directory}\n"
            f"Purpose: {purpose or 'None'}\n"
            f"Pinned: {'yes' if pinned else 'no'}\n"
            f"Wrapped: {'yes' if wrapped_at is not None else 'no'}\n"
            "Continued from handoff: "
            f"{continued_from_handoff_id or 'None'}\n"
            f"Latest handoff: {latest_handoff_id or 'None'}\n"
            f"Last activity: {_format_age(row.recency_at, self._now_ms())}\n"
            f"Safe stop eligibility: {'eligible' if row.can_stop else 'not eligible'}\n"
            f"Session key: {row.session_key}\n\n"
            f"Handoffs:\n{handoff_text}\n\n"
            f"Warnings:\n{warning_text}"
        )

    def _render_issues(self) -> None:
        widget = self.query_one("#issues", Static)
        model = self.model
        issues = () if model is None else model.issues
        command_errors = tuple(
            error
            for error in (self.action_error, self.detail_error, self.last_error)
            if error is not None
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
            if self._view_mode() == "inbox":
                visible_count = sum(row.task_id is None for row in model.visible_rows)
                parts.append(f"{visible_count}/{len(model.inbox_rows)} Inbox sessions")
            else:
                visible_tasks = self._visible_tasks()
                total_tasks = (
                    model.closed_tasks
                    if self._view_mode() == "closed"
                    else model.open_tasks
                )
                parts.append(f"{len(visible_tasks)}/{len(total_tasks)} tasks")
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
        if self.detail_loading_key is not None:
            parts.append("DETAIL loading…")
        elif self.detail_error is not None:
            parts.append(f"DETAIL ERROR {self.detail_error.code}")
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
        task = self._selected_task()
        if task is not None:
            if task.status == "closed":
                self._publish_action_error(
                    "task_closed",
                    "Reopen the selected task before opening it.",
                    retryable=False,
                )
                return
            if self._begin_action(f"opening {task.title}"):
                self._prepare_task(task.task_id, self._new_request_id())
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
    async def _prepare_task(self, task_id: str, request_id: str) -> None:
        try:
            envelope = await self.gateway.prepare_task(
                task_id,
                provider=None,
                request_id=request_id,
                context=self.terminal_context,
            )
            await self._apply_plan(envelope)
        except GatewayError as error:
            self._publish_action_error(
                error.code, error.message, retryable=error.retryable
            )
        finally:
            self._finish_action()

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
        self._pending_new_target = target
        self.push_screen(
            TextEditScreen("New task title", value=None, maximum=256),
            self._on_new_title,
        )

    def _on_new_title(self, result: EditResult | None) -> None:
        target = self._pending_new_target
        self._pending_new_target = None
        if target is None or result is None or result.value is None:
            return
        if self._begin_action(
            f"starting {target.provider.value} task in {target.project_name}"
        ):
            self._prepare_new(
                target,
                result.value,
                str(self._request_id_factory()),
                self._new_request_id(),
            )

    @work(exclusive=False, group="action", exit_on_error=False)
    async def _prepare_new(
        self, target: LaunchTarget, title: str, task_id: str, request_id: str
    ) -> None:
        try:
            envelope = await self.gateway.prepare_task_create(
                task_id,
                project_id=target.project_id,
                title=title,
                checkout_id=target.checkout_id,
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

    def action_adopt_session(self) -> None:
        if self.action_busy:
            return
        row = self._selected_row_for_action("adopting it")
        if row is None:
            return
        if row.task_id is not None:
            self._publish_action_error(
                "session_already_assigned",
                "The selected session already belongs to a task.",
                retryable=False,
            )
            return
        tasks = () if self.model is None else self.model.open_tasks
        if not tasks:
            self._publish_action_error(
                "task_unavailable",
                "Create an open task before adopting this Inbox session.",
                retryable=False,
            )
            return
        self.push_screen(
            TaskPicker(tasks),
            lambda task: self._on_adopt_task(row, task),
        )

    def _on_adopt_task(self, row: SessionRow, task: TaskRow | None) -> None:
        if task is None:
            return
        if self._begin_action(f"adopting {row.label} into {task.title}"):
            self._adopt_session(row.session_key, task.task_id)

    @work(exclusive=False, group="action", exit_on_error=False)
    async def _adopt_session(self, session_key: str, task_id: str) -> None:
        try:
            await self.gateway.adopt_session(session_key, task_id=task_id)
            self.action_message = "Session adopted into task"
            self._request_snapshot(full=False)
        except GatewayError as error:
            self._publish_action_error(
                error.code, error.message, retryable=error.retryable
            )
        finally:
            self._finish_action()

    def action_close_task(self) -> None:
        if self.action_busy:
            return
        task = self._selected_task()
        if task is None or task.status != "open":
            self._publish_action_error(
                "task_not_selected",
                "Select an open task before closing it.",
                retryable=False,
            )
            return
        if task.current_session_key is None:
            if self._begin_action(f"closing {task.title}"):
                self._close_task(task.task_id, None)
            return
        self.push_screen(
            HandoffEditor("Close task with handoff"),
            lambda draft: self._on_close_draft(task, draft),
        )

    def _on_close_draft(self, task: TaskRow, draft: HandoffDraft | None) -> None:
        if draft is None:
            return
        if self._begin_action(f"closing {task.title}"):
            self._close_task(task.task_id, draft)

    @work(exclusive=False, group="action", exit_on_error=False)
    async def _close_task(self, task_id: str, draft: HandoffDraft | None) -> None:
        try:
            await self.gateway.close_task(
                task_id,
                handoff_id=(None if draft is None else str(self._handoff_id_factory())),
                summary=None if draft is None else draft.summary,
                next_action=None if draft is None else draft.next_action,
            )
            self.action_message = "Task closed; runtime left unchanged"
            self._selected_task_id = None
            self._request_snapshot(full=False)
        except GatewayError as error:
            self._publish_action_error(
                error.code, error.message, retryable=error.retryable
            )
        finally:
            self._finish_action()

    def action_reopen_task(self) -> None:
        if self.action_busy:
            return
        task = self._selected_task()
        if task is None or task.status != "closed":
            self._publish_action_error(
                "task_not_selected",
                "Select a closed task before reopening it.",
                retryable=False,
            )
            return
        if self._begin_action(f"reopening {task.title}"):
            self._reopen_task(task.task_id)

    @work(exclusive=False, group="action", exit_on_error=False)
    async def _reopen_task(self, task_id: str) -> None:
        try:
            await self.gateway.reopen_task(task_id)
            self.action_message = "Task reopened"
            self._selected_task_id = None
            self._request_snapshot(full=False)
        except GatewayError as error:
            self._publish_action_error(
                error.code, error.message, retryable=error.retryable
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
                checkout_id=target.checkout_id,
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

    def _selected_row_for_action(self, action: str) -> SessionRow | None:
        row = None if self.model is None else self.model.selected_row
        if row is None:
            self._publish_action_error(
                "session_not_selected",
                f"Select a known session before {action}.",
                retryable=False,
            )
        return row

    def _selected_task(self) -> TaskRow | None:
        model = self.model
        if model is None or self._selected_task_id is None:
            return None
        return next(
            (
                task
                for task in model.task_rows
                if task.task_id == self._selected_task_id
            ),
            None,
        )

    def action_reload_detail(self) -> None:
        if self._selected_row_for_action("loading its detail") is not None:
            self._request_selected_detail(force=True)

    def action_edit_name(self) -> None:
        if self.action_busy:
            return
        task = self._selected_task()
        if task is not None:
            self.push_screen(
                TextEditScreen("Edit task title", value=task.title, maximum=256),
                lambda result: self._on_task_edit(task, "title", result),
            )
            return
        row = self._selected_row_for_action("editing its name")
        if row is None:
            return
        detail = None if self.model is None else self.model.selected_detail
        value = row.name if detail is None else detail.name
        self.push_screen(
            TextEditScreen("Edit session name", value=value, maximum=512),
            lambda result: self._on_edit_result(row, "name", result),
        )

    def action_edit_purpose(self) -> None:
        if self.action_busy:
            return
        task = self._selected_task()
        if task is not None:
            self.push_screen(
                TextEditScreen("Edit task purpose", value=task.purpose, maximum=4096),
                lambda result: self._on_task_edit(task, "purpose", result),
            )
            return
        row = self._selected_row_for_action("editing its purpose")
        if row is None:
            return
        detail = None if self.model is None else self.model.selected_detail
        value = row.purpose if detail is None else detail.purpose
        self.push_screen(
            TextEditScreen("Edit session purpose", value=value, maximum=4096),
            lambda result: self._on_edit_result(row, "purpose", result),
        )

    def _on_edit_result(
        self,
        row: SessionRow,
        field: str,
        result: EditResult | None,
    ) -> None:
        if result is None:
            return
        if self._begin_action(f"updating {field} for {row.label}"):
            self._edit_session(row.session_key, field, result.value)

    def _on_task_edit(
        self, task: TaskRow, field: str, result: EditResult | None
    ) -> None:
        if result is None or (field == "title" and result.value is None):
            return
        if self._begin_action(f"updating {field} for {task.title}"):
            self._edit_task(task.task_id, field, result.value)

    @work(exclusive=False, group="action", exit_on_error=False)
    async def _edit_task(self, task_id: str, field: str, value: str | None) -> None:
        try:
            if field == "title":
                assert value is not None
                await self.gateway.set_task_title(task_id, value)
            else:
                await self.gateway.set_task_purpose(task_id, value)
            self.action_message = f"Task {field} updated"
            self._request_snapshot(full=False)
        except GatewayError as error:
            self._publish_action_error(
                error.code, error.message, retryable=error.retryable
            )
        finally:
            self._finish_action()

    @work(exclusive=False, group="action", exit_on_error=False)
    async def _edit_session(
        self,
        session_key: str,
        field: str,
        value: str | None,
    ) -> None:
        try:
            envelope = (
                await self.gateway.set_session_name(session_key, value)
                if field == "name"
                else await self.gateway.set_session_purpose(session_key, value)
            )
            self._accept_curation(envelope, f"Session {field} updated")
        except GatewayError as error:
            self._publish_action_error(
                error.code,
                error.message,
                retryable=error.retryable,
            )
        finally:
            self._finish_action()

    def action_toggle_pin(self) -> None:
        if self.action_busy:
            return
        task = self._selected_task()
        if task is not None:
            if self._begin_action(
                ("unpinning " if task.pinned else "pinning ") + task.title
            ):
                self._set_task_pin(task.task_id, not task.pinned)
            return
        row = self._selected_row_for_action("changing its pin")
        if row is None:
            return
        detail = None if self.model is None else self.model.selected_detail
        pinned = row.pinned if detail is None else detail.pinned
        if self._begin_action(("unpinning " if pinned else "pinning ") + row.label):
            self._set_pin(row.session_key, not pinned)

    @work(exclusive=False, group="action", exit_on_error=False)
    async def _set_pin(self, session_key: str, pinned: bool) -> None:
        try:
            envelope = await self.gateway.set_session_pinned(
                session_key,
                pinned=pinned,
            )
            self._accept_curation(
                envelope,
                "Session pinned" if pinned else "Session unpinned",
            )
        except GatewayError as error:
            self._publish_action_error(
                error.code,
                error.message,
                retryable=error.retryable,
            )
        finally:
            self._finish_action()

    @work(exclusive=False, group="action", exit_on_error=False)
    async def _set_task_pin(self, task_id: str, pinned: bool) -> None:
        try:
            await self.gateway.set_task_pinned(task_id, pinned=pinned)
            self.action_message = "Task pinned" if pinned else "Task unpinned"
            self._request_snapshot(full=False)
        except GatewayError as error:
            self._publish_action_error(
                error.code, error.message, retryable=error.retryable
            )
        finally:
            self._finish_action()

    def action_handoff(self) -> None:
        self._open_handoff_editor(wrap=False)

    def action_wrap(self) -> None:
        self._open_handoff_editor(wrap=True)

    def _open_handoff_editor(self, *, wrap: bool) -> None:
        if self.action_busy:
            return
        row = self._selected_row_for_action(
            "wrapping it" if wrap else "recording a handoff"
        )
        if row is None:
            return
        retained = self._handoff_drafts.get((row.session_key, wrap))
        self.push_screen(
            HandoffEditor(
                "Wrap session with handoff" if wrap else "Record session handoff",
                draft=None if retained is None else retained[1],
            ),
            lambda draft: self._on_handoff_draft(row, wrap, draft),
        )

    def _on_handoff_draft(
        self,
        row: SessionRow,
        wrap: bool,
        draft: HandoffDraft | None,
    ) -> None:
        if draft is None:
            return
        key = (row.session_key, wrap)
        retained = self._handoff_drafts.get(key)
        handoff_id = (
            retained[0]
            if retained is not None and retained[1] == draft
            else str(self._handoff_id_factory())
        )
        self._handoff_drafts[key] = (handoff_id, draft)
        label = "wrapping" if wrap else "recording handoff for"
        if self._begin_action(f"{label} {row.label}"):
            self._submit_handoff(row.session_key, handoff_id, draft, wrap)

    @work(exclusive=False, group="action", exit_on_error=False)
    async def _submit_handoff(
        self,
        session_key: str,
        handoff_id: str,
        draft: HandoffDraft,
        wrap: bool,
    ) -> None:
        try:
            envelope = await self.gateway.append_session_handoff(
                session_key,
                handoff_id=handoff_id,
                summary=draft.summary,
                next_action=draft.next_action,
                wrap=wrap,
            )
            self._handoff_drafts.pop((session_key, wrap), None)
            self._accept_curation(
                envelope,
                "Session wrapped" if wrap else "Handoff recorded",
            )
        except GatewayError as error:
            self._publish_action_error(
                error.code,
                error.message,
                retryable=error.retryable,
            )
        finally:
            self._finish_action()

    def _accept_curation(
        self,
        envelope: SessionDetailEnvelope,
        message: str,
    ) -> None:
        if self.model is None:
            raise GatewayError(
                "frontend_model_missing",
                "The session list is no longer available.",
                retryable=True,
            )
        # A mutation response is authoritative for its committed state. Invalidate
        # any older on-demand read so an equal-millisecond detail cannot overwrite it.
        self._detail_request_id += 1
        self.detail_loading_key = None
        try:
            self.model = self.model.with_detail(envelope)
        except ValidationError as error:
            raise GatewayError(
                "response_invalid",
                "The Switchboard command emitted an incompatible response.",
                retryable=False,
            ) from error
        self.detail_error = None
        self.action_message = message
        self._render_details()
        self._request_snapshot(full=False)

    def action_continue_session(self) -> None:
        if self.action_busy:
            return
        task = self._selected_task()
        if task is None:
            row = self._selected_row_for_action("continuing its task")
            if row is None or row.task_id is None:
                self._publish_action_error(
                    "task_not_assigned",
                    "Adopt this Inbox session into a task before continuing it.",
                    retryable=False,
                )
                return
            task = next(
                (item for item in self.model.task_rows if item.task_id == row.task_id),
                None,
            )
        if task is None:
            return
        if self._begin_action(f"continuing {task.title}"):
            self._prepare_task(task.task_id, self._new_request_id())

    def action_focus_search(self) -> None:
        search = self.query_one("#search", Input)
        if not search.disabled:
            search.focus()

    def _show_view(self, value: str) -> None:
        self.query_one("#view-filter", Select).value = value

    def action_show_open(self) -> None:
        self._show_view("open")

    def action_show_inbox(self) -> None:
        self._show_view("inbox")

    def action_show_closed(self) -> None:
        self._show_view("closed")

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


def _row_cue(row: SessionRow) -> str:
    cues = []
    if row.pinned:
        cues.append("pin")
    if row.wrapped_at is not None:
        cues.append("wrap")
    if row.continued_from_handoff_id is not None:
        cues.append("cont")
    suffix = "" if not cues else f" [{' '.join(cues)}]"
    return f"{_status_cue(row)}{suffix}"


def _bounded_display(value: str) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= HANDOFF_TEXT_DISPLAY_CHARS:
        return normalized
    return f"{normalized[: HANDOFF_TEXT_DISPLAY_CHARS - 1]}…"


def _target_label(target: LaunchTarget) -> str:
    checkout = target.checkout_name or target.checkout_path
    qualifiers = []
    if target.is_default:
        qualifiers.append("default checkout")
    if target.is_preferred_provider:
        qualifiers.append("preferred provider")
    suffix = "" if not qualifiers else f" ({', '.join(qualifiers)})"
    return (
        f"{target.project_name} · {checkout} · {target.provider.value}{suffix}\n"
        f"  {target.checkout_path}"
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
    "EditResult",
    "HandoffDraft",
    "HandoffEditor",
    "StopConfirmation",
    "SwitchboardApp",
    "TargetPicker",
    "TextEditScreen",
    "run_tui",
]
