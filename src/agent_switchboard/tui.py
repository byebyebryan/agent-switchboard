"""Optional read-only Textual frontend for local Switchboard sessions."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Input, Select, Static

from .domain import PresentationContext, ValidationError
from .tui_gateway import (
    GatewayError,
    SnapshotSource,
    SwbctlGateway,
    resolve_terminal_context,
)
from .tui_model import FrontendModel, SessionRow, ViewFilters

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


class SwitchboardApp(App[None]):
    """Read-only Phase 4A terminal session index."""

    TITLE = "Switchboard"
    SUB_TITLE = "Terminal session router"
    BINDINGS = (
        Binding("/", "focus_search", "Search"),
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
    ) -> None:
        super().__init__()
        self.gateway = gateway
        self.snapshots = SnapshotSource(gateway)
        self.terminal_context = terminal_context
        self.model: FrontendModel | None = None
        self.refreshing = False
        self.last_error: GatewayError | None = None
        self._now_ms = (lambda: int(time.time() * 1000)) if now_ms is None else now_ms
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
                "/ search · arrows navigate · r refresh · Ctrl+L clear filters · "
                "e issues · ? help · q quit\n"
                "This view is read-only. Open, new, history, and stop arrive in 4A.4.",
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
        if not issues and self.last_error is None:
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
        last_error_is_projected = self.last_error is None or any(
            issue.source.value == "frontend" and issue.code == self.last_error.code
            for issue in ordered_issues
        )
        if self.last_error is not None and not last_error_is_projected:
            lines.append(
                f"- frontend/{self.last_error.code}: {self.last_error.message}"
            )
        lines.extend(
            f"- {issue.source.value}/{issue.code}: {issue.message}"
            for issue in ordered_issues[: ISSUE_DISPLAY_LIMIT - len(lines)]
        )
        issue_count = len(ordered_issues) + (0 if last_error_is_projected else 1)
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
        parts.append(
            "tmux client"
            if self.terminal_context.current_tmux_client is not None
            else "plain terminal"
        )
        parts.append("read-only")
        self.query_one("#status", Static).update(" · ".join(parts))

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


def run_tui(*, swbctl_executable: str | Path) -> int:
    """Run the optional terminal frontend."""

    SwitchboardApp(
        gateway=SwbctlGateway(swbctl_executable),
        terminal_context=resolve_terminal_context(),
    ).run()
    return 0


__all__ = [
    "MIN_TERMINAL_HEIGHT",
    "MIN_TERMINAL_WIDTH",
    "SwitchboardApp",
    "run_tui",
]
