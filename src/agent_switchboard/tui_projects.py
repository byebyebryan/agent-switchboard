"""Focused Textual project-catalog manager built on the public CLI gateway."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Static

from .tui_gateway import CatalogDocument, GatewayError, SwbctlGateway


@dataclass(frozen=True, slots=True)
class CatalogField:
    key: str
    label: str
    value: str = ""
    placeholder: str = ""
    required: bool = False


class CatalogFormScreen(ModalScreen[dict[str, str] | None]):
    """Collect one bounded set of catalog values without interpreting paths."""

    BINDINGS = (
        Binding("ctrl+s", "submit", "Save"),
        Binding("escape", "cancel", "Cancel"),
    )
    CSS = """
    CatalogFormScreen { align: center middle; }
    #catalog-form-dialog {
        width: 80;
        max-width: 96%;
        height: 80%;
        max-height: 32;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #catalog-form-fields { height: 1fr; }
    .catalog-field-label { height: 1; text-style: bold; margin-top: 1; }
    .catalog-field-input { height: 3; }
    #catalog-form-error { height: 2; color: $error; }
    """

    def __init__(
        self,
        title: str,
        fields: Sequence[CatalogField],
        *,
        help_text: str,
    ) -> None:
        super().__init__()
        self.form_title = title
        self.fields = tuple(fields)
        self.help_text = help_text

    def compose(self) -> ComposeResult:
        with Vertical(id="catalog-form-dialog"):
            yield Static(self.form_title, classes="panel-heading", markup=False)
            with VerticalScroll(id="catalog-form-fields"):
                for field in self.fields:
                    required = " *" if field.required else ""
                    yield Static(
                        f"{field.label}{required}",
                        classes="catalog-field-label",
                        markup=False,
                    )
                    yield Input(
                        value=field.value,
                        placeholder=field.placeholder,
                        id=f"catalog-field-{field.key}",
                        classes="catalog-field-input",
                    )
            yield Static("", id="catalog-form-error", markup=False)
            yield Static(
                f"{self.help_text}\nCtrl+S saves · Esc cancels",
                markup=False,
            )

    def on_mount(self) -> None:
        if self.fields:
            self.call_after_refresh(self._focus_first_field)

    def _focus_first_field(self) -> None:
        self.query_one(f"#catalog-field-{self.fields[0].key}", Input).focus()

    def action_submit(self) -> None:
        values = {
            field.key: self.query_one(
                f"#catalog-field-{field.key}", Input
            ).value.strip()
            for field in self.fields
        }
        missing = [
            field.label
            for field in self.fields
            if field.required and not values[field.key]
        ]
        if missing:
            self.query_one("#catalog-form-error", Static).update(
                f"Required: {', '.join(missing)}"
            )
            return
        if any(len(value) > 4096 for value in values.values()):
            self.query_one("#catalog-form-error", Static).update(
                "A field exceeds 4096 characters."
            )
            return
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CatalogConfirmation(ModalScreen[bool]):
    BINDINGS = (
        Binding("y", "confirm", "Confirm"),
        Binding("n", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel"),
    )
    CSS = """
    CatalogConfirmation { align: center middle; }
    #catalog-confirm-dialog {
        width: 68;
        max-width: 92%;
        height: 10;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="catalog-confirm-dialog"):
            yield Static(
                f"{self.message}\n\nPress y to confirm or n/Esc to cancel.",
                markup=False,
            )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


@dataclass(frozen=True, slots=True)
class CatalogSelection:
    kind: str
    project: Mapping[str, Any]
    repository: Mapping[str, Any] | None = None
    checkout: Mapping[str, Any] | None = None


def _csv(value: object) -> str:
    return ", ".join(str(item) for item in value) if isinstance(value, list) else ""


def _choice(value: str, allowed: set[str], field: str) -> str:
    selected = value.casefold()
    if selected not in allowed:
        raise GatewayError(
            "argument_invalid",
            f"{field} must be one of: {', '.join(sorted(allowed))}.",
            retryable=False,
        )
    return selected


class ProjectManagerScreen(ModalScreen[None]):
    """List and mutate the complete local project/repository/checkout catalog."""

    BINDINGS = (
        Binding("r", "refresh", "Refresh"),
        Binding("a", "add_project", "Add project"),
        Binding("e", "edit", "Edit"),
        Binding("n", "add_child", "Add repo/checkout"),
        Binding("l", "link_repository", "Link repo"),
        Binding("m", "make_primary", "Primary"),
        Binding("d", "make_default", "Default"),
        Binding("x", "lifecycle", "Archive/restore/unlink"),
        Binding("i", "import_project", "Import"),
        Binding("o", "export_project", "Export"),
        Binding("escape", "close", "Back"),
        Binding("q", "close", "Back", show=False),
    )
    CSS = """
    ProjectManagerScreen { background: $background; }
    #catalog-body { height: 1fr; padding: 0 1; }
    #catalog-heading { height: 2; text-style: bold; }
    #catalog-content { height: 1fr; }
    #catalog-table { width: 2fr; height: 1fr; border: round $primary; }
    #catalog-detail { width: 1fr; height: 1fr; border: round $secondary; padding: 0 1; }
    #catalog-status { height: 2; }
    #catalog-help { height: 3; }
    """

    def __init__(
        self,
        gateway: SwbctlGateway,
        *,
        project_id: str | None = None,
        add_project: bool = False,
    ) -> None:
        super().__init__()
        self.gateway = gateway
        self.scope_project_id = project_id
        self.open_add_project = add_project
        self.catalog: CatalogDocument | None = None
        self.selections: dict[str, CatalogSelection] = {}
        self.selected_key: str | None = None
        self.busy = False
        self.error: GatewayError | None = None
        self.message: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="catalog-body"):
            yield Static("Projects", id="catalog-heading", markup=False)
            with Horizontal(id="catalog-content"):
                yield DataTable(
                    show_row_labels=False,
                    cursor_type="row",
                    zebra_stripes=True,
                    id="catalog-table",
                    disabled=True,
                )
                with VerticalScroll(id="catalog-detail", can_focus=True):
                    yield Static(
                        "Select a catalog entry.",
                        id="catalog-detail-text",
                        markup=False,
                    )
            yield Static("Loading project catalog…", id="catalog-status", markup=False)
            yield Static(
                "a add project · e edit · n add repository/checkout · l link repo · "
                "m primary · d default\nx archive/restore/unlink · i import · "
                "o export · "
                "r refresh · Esc back",
                id="catalog-help",
                markup=False,
            )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#catalog-table", DataTable)
        table.add_column("Type", key="type", width=12)
        table.add_column("Name", key="name", width=24)
        table.add_column("Role", key="role", width=18)
        table.add_column("Path / count", key="path", width=36)
        table.add_column("State", key="state", width=14)
        self._load_catalog()

    @work(exclusive=True, group="catalog-load", exit_on_error=False)
    async def _load_catalog(self) -> None:
        self.busy = True
        self._render_status()
        try:
            self.catalog = await self.gateway.project_catalog(include_archived=True)
            self.error = None
            self._render_catalog()
            table = self.query_one("#catalog-table", DataTable)
            table.disabled = False
            table.focus()
            if self.open_add_project:
                self.open_add_project = False
                self.call_after_refresh(self.action_add_project)
        except GatewayError as error:
            self.error = error
        finally:
            self.busy = False
            self._render_status()

    def _projects(self) -> tuple[Mapping[str, Any], ...]:
        if self.catalog is None:
            return ()
        projects = self.catalog["projects"]
        assert isinstance(projects, list)
        return tuple(
            project
            for project in projects
            if isinstance(project, dict)
            and (
                self.scope_project_id is None
                or project["projectId"] == self.scope_project_id
            )
        )

    def _render_catalog(self) -> None:
        table = self.query_one("#catalog-table", DataTable)
        previous = self.selected_key
        table.clear()
        self.selections.clear()
        row_index = 0
        selected_index = 0
        for project in self._projects():
            project_id = str(project["projectId"])
            project_key = f"project:{project_id}"
            repositories = project["repositories"]
            assert isinstance(repositories, list)
            self.selections[project_key] = CatalogSelection("project", project)
            table.add_row(
                "Project",
                str(project["name"]),
                str(project.get("defaultProvider") or "provider: inherited"),
                f"{len(repositories)} repositories",
                "active" if project["declared"] else "archived",
                key=project_key,
            )
            if previous == project_key:
                selected_index = row_index
            row_index += 1
            for repository in repositories:
                repository_id = str(repository["repositoryId"])
                repository_key = f"repository:{project_id}:{repository_id}"
                checkouts = repository["checkouts"]
                assert isinstance(checkouts, list)
                self.selections[repository_key] = CatalogSelection(
                    "repository", project, repository
                )
                table.add_row(
                    "  Repository",
                    str(repository["name"]),
                    "primary" if repository["isPrimary"] else str(repository["kind"]),
                    f"{len(checkouts)} local checkouts",
                    "active" if repository["declared"] else "archived",
                    key=repository_key,
                )
                if previous == repository_key:
                    selected_index = row_index
                row_index += 1
                for checkout in checkouts:
                    checkout_id = str(checkout["checkoutId"])
                    checkout_key = f"checkout:{checkout_id}"
                    self.selections[checkout_key] = CatalogSelection(
                        "checkout", project, repository, checkout
                    )
                    self.query_one("#catalog-table", DataTable).add_row(
                        "    Checkout",
                        str(
                            checkout.get("displayName")
                            or Path(str(checkout["path"])).name
                        ),
                        "default" if checkout["isDefault"] else str(checkout["kind"]),
                        str(checkout["path"]),
                        (
                            "present"
                            if checkout["declared"] and checkout["present"]
                            else "missing"
                            if checkout["declared"]
                            else "archived"
                        ),
                        key=checkout_key,
                    )
                    if previous == checkout_key:
                        selected_index = row_index
                    row_index += 1
        if row_index:
            table.move_cursor(row=selected_index)
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            self.selected_key = str(row_key.value)
        else:
            self.selected_key = None
        self._render_detail()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.selected_key = str(event.row_key.value)
        self._render_detail()

    def _selected(self) -> CatalogSelection | None:
        return (
            None
            if self.selected_key is None
            else self.selections.get(self.selected_key)
        )

    def _render_detail(self) -> None:
        selected = self._selected()
        detail = self.query_one("#catalog-detail-text", Static)
        if selected is None:
            detail.update("No catalog entries. Press a to add a project.")
            return
        project = selected.project
        lines = [
            f"Project: {project['name']}",
            f"ID: {project['projectId']}",
            f"Aliases: {_csv(project.get('aliases')) or 'none'}",
            f"Provider: {project.get('defaultProvider') or 'inherited'}",
            f"Transport: {project.get('defaultTransport')}",
            f"State: {'active' if project['declared'] else 'archived'}",
        ]
        if selected.repository is not None:
            repository = selected.repository
            lines.extend(
                (
                    "",
                    f"Repository: {repository['name']}",
                    f"ID: {repository['repositoryId']}",
                    f"Kind: {repository['kind']}",
                    f"Primary: {'yes' if repository['isPrimary'] else 'no'}",
                    f"Context: {_csv(repository.get('contextSources')) or 'none'}",
                )
            )
        if selected.checkout is not None:
            checkout = selected.checkout
            references = checkout.get("references", {})
            lines.extend(
                (
                    "",
                    f"Checkout: {checkout.get('displayName') or 'unnamed'}",
                    f"ID: {checkout['checkoutId']}",
                    f"Path: {checkout['path']}",
                    f"Kind: {checkout['kind']}",
                    f"Provider: {checkout.get('providerOverride') or 'inherited'}",
                    f"Transport: {checkout.get('transportOverride') or 'inherited'}",
                    f"Default: {'yes' if checkout['isDefault'] else 'no'}",
                    f"Present: {'yes' if checkout['present'] else 'no'}",
                    f"References: {json.dumps(references, sort_keys=True)}",
                )
            )
        detail.update("\n".join(lines))

    def _render_status(self) -> None:
        status = self.query_one("#catalog-status", Static)
        if self.busy:
            status.update("Working…")
        elif self.error is not None:
            status.update(f"ERROR {self.error.code}: {self.error.message}")
        elif self.message is not None:
            status.update(self.message)
        elif self.catalog is not None:
            status.update(f"{len(self._projects())} projects · Catalog v1")

    def _show_error(self, error: GatewayError) -> None:
        self.error = error
        self.message = None
        self._render_status()

    @work(exclusive=True, group="catalog-action", exit_on_error=False)
    async def _mutate(self, arguments: Sequence[str], message: str) -> None:
        self.busy = True
        self.error = None
        self.message = None
        self._render_status()
        try:
            self.catalog = await self.gateway.project_action(arguments)
            self.message = message
            self._render_catalog()
        except GatewayError as error:
            self._show_error(error)
        finally:
            self.busy = False
            self._render_status()

    def action_refresh(self) -> None:
        if not self.busy:
            self._load_catalog()

    def action_add_project(self) -> None:
        if self.busy:
            return
        self.app.push_screen(
            CatalogFormScreen(
                "Add project",
                (
                    CatalogField("path", "Existing path", required=True),
                    CatalogField("name", "Project name (optional)"),
                    CatalogField(
                        "kind", "Kind", "auto", "auto | git | directory", True
                    ),
                    CatalogField(
                        "provider",
                        "Default provider",
                        "codex",
                        "codex | claude | none",
                        True,
                    ),
                ),
                help_text=(
                    "The path is inspected first; no repository is created or cloned."
                ),
            ),
            self._add_project_result,
        )

    def _add_project_result(self, values: dict[str, str] | None) -> None:
        if values is None:
            return
        try:
            kind = _choice(values["kind"], {"auto", "git", "directory"}, "Kind")
            provider = _choice(
                values["provider"], {"codex", "claude", "none"}, "Provider"
            )
        except GatewayError as error:
            self._show_error(error)
            return
        arguments = ["add", values["path"], "--kind", kind, "--provider", provider]
        if values["name"]:
            arguments.extend(("--name", values["name"]))
        self._mutate(arguments, "Project added")

    def action_edit(self) -> None:
        if self.busy or (selected := self._selected()) is None:
            return
        if not selected.project["declared"]:
            self._show_error(
                GatewayError(
                    "project_archived",
                    "Restore the project before editing it.",
                    retryable=False,
                )
            )
            return
        if selected.kind == "project":
            self.app.push_screen(
                CatalogFormScreen(
                    "Edit project",
                    (
                        CatalogField(
                            "name", "Name", str(selected.project["name"]), required=True
                        ),
                        CatalogField(
                            "aliases",
                            "Aliases (comma-separated)",
                            _csv(selected.project.get("aliases")),
                        ),
                        CatalogField(
                            "provider",
                            "Default provider",
                            str(selected.project.get("defaultProvider") or "none"),
                            "codex | claude | none",
                            True,
                        ),
                    ),
                    help_text="Stable project identity is not editable.",
                ),
                lambda values: self._edit_project_result(selected, values),
            )
        elif selected.kind == "repository":
            assert selected.repository is not None
            self.app.push_screen(
                CatalogFormScreen(
                    "Edit repository",
                    (
                        CatalogField(
                            "name",
                            "Name",
                            str(selected.repository["name"]),
                            required=True,
                        ),
                        CatalogField(
                            "context",
                            "Context sources (comma-separated)",
                            _csv(selected.repository.get("contextSources")),
                        ),
                    ),
                    help_text=(
                        f"Kind is {selected.repository['kind']}; stable identity "
                        "is not editable."
                    ),
                ),
                lambda values: self._edit_repository_result(selected, values),
            )
        else:
            assert selected.checkout is not None
            self.app.push_screen(
                CatalogFormScreen(
                    "Edit checkout",
                    (
                        CatalogField(
                            "path",
                            "Existing path",
                            str(selected.checkout["path"]),
                            required=True,
                        ),
                        CatalogField(
                            "name",
                            "Display name",
                            str(selected.checkout.get("displayName") or ""),
                        ),
                        CatalogField(
                            "provider",
                            "Provider override",
                            str(selected.checkout.get("providerOverride") or "none"),
                            "codex | claude | none",
                            True,
                        ),
                        CatalogField(
                            "transport",
                            "Transport override",
                            str(selected.checkout.get("transportOverride") or "none"),
                            "tmux | none",
                            True,
                        ),
                        CatalogField(
                            "default",
                            "Default",
                            "yes" if selected.checkout["isDefault"] else "no",
                            "yes | no",
                            True,
                        ),
                    ),
                    help_text=(
                        "Changing a Git path requires proof it belongs to the same "
                        "repository."
                    ),
                ),
                lambda values: self._edit_checkout_result(selected, values),
            )

    def _edit_project_result(
        self, selected: CatalogSelection, values: dict[str, str] | None
    ) -> None:
        if values is None:
            return
        try:
            provider = _choice(
                values["provider"], {"codex", "claude", "none"}, "Provider"
            )
        except GatewayError as error:
            self._show_error(error)
            return
        arguments = [
            "update",
            str(selected.project["projectId"]),
            "--name",
            values["name"],
            "--provider",
            provider,
        ]
        aliases = [
            item.strip() for item in values["aliases"].split(",") if item.strip()
        ]
        if aliases:
            for alias in aliases:
                arguments.extend(("--alias", alias))
        else:
            arguments.append("--clear-aliases")
        self._mutate(arguments, "Project updated")

    def _edit_repository_result(
        self, selected: CatalogSelection, values: dict[str, str] | None
    ) -> None:
        if values is None or selected.repository is None:
            return
        arguments = [
            "repository",
            "update",
            str(selected.repository["repositoryId"]),
            "--name",
            values["name"],
        ]
        sources = [
            item.strip() for item in values["context"].split(",") if item.strip()
        ]
        if sources:
            for source in sources:
                arguments.extend(("--context-source", source))
        else:
            arguments.append("--clear-context-sources")
        self._mutate(arguments, "Repository updated")

    def _edit_checkout_result(
        self, selected: CatalogSelection, values: dict[str, str] | None
    ) -> None:
        if values is None or selected.checkout is None:
            return
        try:
            provider = _choice(
                values["provider"], {"codex", "claude", "none"}, "Provider"
            )
            transport = _choice(values["transport"], {"tmux", "none"}, "Transport")
            default = _choice(values["default"], {"yes", "no"}, "Default")
        except GatewayError as error:
            self._show_error(error)
            return
        arguments = [
            "checkout",
            "update",
            str(selected.checkout["checkoutId"]),
            "--path",
            values["path"],
            "--provider",
            provider,
            "--transport",
            transport,
            "--default",
            "on" if default == "yes" else "off",
        ]
        arguments.extend(("--display-name", values["name"])) if values[
            "name"
        ] else arguments.append("--clear-display-name")
        self._mutate(arguments, "Checkout updated")

    def action_add_child(self) -> None:
        if self.busy or (selected := self._selected()) is None:
            return
        if not selected.project["declared"]:
            self._show_error(
                GatewayError(
                    "project_archived",
                    "Restore the project before adding entries.",
                    retryable=False,
                )
            )
            return
        if selected.kind == "project":
            self.app.push_screen(
                CatalogFormScreen(
                    "Add repository",
                    (
                        CatalogField("path", "Existing path", required=True),
                        CatalogField("name", "Repository name (optional)"),
                        CatalogField(
                            "kind", "Kind", "auto", "auto | git | directory", True
                        ),
                        CatalogField("primary", "Make primary", "no", "yes | no", True),
                    ),
                    help_text=(
                        "Adds a distinct repository identity and its first local "
                        "checkout."
                    ),
                ),
                lambda values: self._add_repository_result(selected, values),
            )
        else:
            repository = selected.repository
            assert repository is not None
            self.app.push_screen(
                CatalogFormScreen(
                    "Add checkout",
                    (
                        CatalogField("path", "Existing path", required=True),
                        CatalogField("name", "Display name (optional)"),
                        CatalogField(
                            "provider",
                            "Provider override",
                            "none",
                            "codex | claude | none",
                            True,
                        ),
                        CatalogField(
                            "transport",
                            "Transport override",
                            "none",
                            "tmux | none",
                            True,
                        ),
                        CatalogField("default", "Make default", "no", "yes | no", True),
                    ),
                    help_text="Git paths must be worktrees of the selected repository.",
                ),
                lambda values: self._add_checkout_result(selected, values),
            )

    def _add_repository_result(
        self, selected: CatalogSelection, values: dict[str, str] | None
    ) -> None:
        if values is None:
            return
        try:
            kind = _choice(values["kind"], {"auto", "git", "directory"}, "Kind")
            primary = _choice(values["primary"], {"yes", "no"}, "Primary")
        except GatewayError as error:
            self._show_error(error)
            return
        arguments = [
            "repository",
            "add",
            str(selected.project["projectId"]),
            values["path"],
            "--kind",
            kind,
        ]
        if values["name"]:
            arguments.extend(("--name", values["name"]))
        if primary == "yes":
            arguments.append("--primary")
        self._mutate(arguments, "Repository added")

    def _add_checkout_result(
        self, selected: CatalogSelection, values: dict[str, str] | None
    ) -> None:
        if values is None or selected.repository is None:
            return
        try:
            provider = _choice(
                values["provider"], {"codex", "claude", "none"}, "Provider"
            )
            transport = _choice(values["transport"], {"tmux", "none"}, "Transport")
            default = _choice(values["default"], {"yes", "no"}, "Default")
        except GatewayError as error:
            self._show_error(error)
            return
        arguments = [
            "checkout",
            "add",
            str(selected.repository["repositoryId"]),
            values["path"],
            "--provider",
            provider,
            "--transport",
            transport,
        ]
        if values["name"]:
            arguments.extend(("--display-name", values["name"]))
        if default == "yes":
            arguments.append("--default")
        self._mutate(arguments, "Checkout added")

    def action_link_repository(self) -> None:
        if (
            self.busy
            or (selected := self._selected()) is None
            or selected.kind != "project"
        ):
            return
        self.app.push_screen(
            CatalogFormScreen(
                "Link existing repository",
                (
                    CatalogField("repository", "Repository UUID", required=True),
                    CatalogField("primary", "Make primary", "no", "yes | no", True),
                ),
                help_text=(
                    "Links an existing stable repository identity to this project."
                ),
            ),
            lambda values: self._link_repository_result(selected, values),
        )

    def _link_repository_result(
        self, selected: CatalogSelection, values: dict[str, str] | None
    ) -> None:
        if values is None:
            return
        try:
            primary = _choice(values["primary"], {"yes", "no"}, "Primary")
        except GatewayError as error:
            self._show_error(error)
            return
        arguments = [
            "repository",
            "link",
            str(selected.project["projectId"]),
            values["repository"],
        ]
        if primary == "yes":
            arguments.append("--primary")
        self._mutate(arguments, "Repository linked")

    def action_make_primary(self) -> None:
        selected = self._selected()
        if self.busy or selected is None or selected.repository is None:
            return
        self._mutate(
            (
                "repository",
                "primary",
                str(selected.project["projectId"]),
                str(selected.repository["repositoryId"]),
            ),
            "Primary repository updated",
        )

    def action_make_default(self) -> None:
        selected = self._selected()
        if self.busy or selected is None or selected.checkout is None:
            return
        self._mutate(
            ("checkout", "default", str(selected.checkout["checkoutId"])),
            "Default checkout updated",
        )

    def action_lifecycle(self) -> None:
        if self.busy or (selected := self._selected()) is None:
            return
        if selected.kind == "project":
            if not selected.project["declared"]:
                self._mutate(
                    ("restore", str(selected.project["projectId"])), "Project restored"
                )
                return
            message = f"Archive project {selected.project['name']}?"
        elif selected.kind == "repository":
            assert selected.repository is not None
            if not selected.project["declared"]:
                return
            message = (
                f"Unlink repository {selected.repository['name']} from this project?"
            )
        else:
            assert selected.checkout is not None
            if not selected.checkout["declared"]:
                self._mutate(
                    ("checkout", "restore", str(selected.checkout["checkoutId"])),
                    "Checkout restored",
                )
                return
            label = selected.checkout.get("displayName") or selected.checkout["path"]
            message = f"Archive checkout {label}?"
        self.app.push_screen(
            CatalogConfirmation(message),
            lambda confirmed: self._lifecycle_confirmed(selected, confirmed),
        )

    def _lifecycle_confirmed(self, selected: CatalogSelection, confirmed: bool) -> None:
        if not confirmed:
            return
        if selected.kind == "project":
            arguments = ("archive", str(selected.project["projectId"]), "--confirm")
            message = "Project archived"
        elif selected.kind == "repository":
            assert selected.repository is not None
            arguments = (
                "repository",
                "unlink",
                str(selected.project["projectId"]),
                str(selected.repository["repositoryId"]),
                "--confirm",
            )
            message = "Repository unlinked"
        else:
            assert selected.checkout is not None
            arguments = (
                "checkout",
                "archive",
                str(selected.checkout["checkoutId"]),
                "--confirm",
            )
            message = "Checkout archived"
        self._mutate(arguments, message)

    def action_import_project(self) -> None:
        if self.busy or self.scope_project_id is not None:
            return
        self.app.push_screen(
            CatalogFormScreen(
                "Import project",
                (
                    CatalogField("input", "Export JSON path", required=True),
                    CatalogField(
                        "mappings",
                        "Checkout mappings",
                        placeholder="REPO_UUID=/path;REPO_UUID=/path",
                        required=True,
                    ),
                ),
                help_text=(
                    "Map at least the primary repository to an existing local path."
                ),
            ),
            self._import_result,
        )

    def _import_result(self, values: dict[str, str] | None) -> None:
        if values is None:
            return
        mappings = [
            item.strip() for item in values["mappings"].split(";") if item.strip()
        ]
        arguments = ["import", "--input", values["input"]]
        for mapping in mappings:
            arguments.extend(("--checkout", mapping))
        self._mutate(arguments, "Project imported")

    def action_export_project(self) -> None:
        selected = self._selected()
        if self.busy or selected is None:
            return
        self.app.push_screen(
            CatalogFormScreen(
                "Export project",
                (CatalogField("path", "New JSON file", required=True),),
                help_text="The destination must not already exist.",
            ),
            lambda values: self._export_result(selected, values),
        )

    def _export_result(
        self, selected: CatalogSelection, values: dict[str, str] | None
    ) -> None:
        if values is not None:
            self._export_project(str(selected.project["projectId"]), values["path"])

    @work(exclusive=True, group="catalog-action", exit_on_error=False)
    async def _export_project(self, project_id: str, destination: str) -> None:
        self.busy = True
        self._render_status()
        descriptor: int | None = None
        target = Path(destination).expanduser()
        try:
            envelope = await self.gateway.project_export(project_id)
            payload = (
                json.dumps(
                    envelope,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            descriptor = os.open(
                target,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short write")
                offset += written
            os.fsync(descriptor)
            self.message = f"Exported to {target}"
            self.error = None
        except GatewayError as error:
            self._show_error(error)
        except OSError:
            self._show_error(
                GatewayError(
                    "project_export_write_failed",
                    "The export file could not be created.",
                    retryable=False,
                )
            )
        finally:
            if descriptor is not None:
                os.close(descriptor)
            self.busy = False
            self._render_status()

    def action_close(self) -> None:
        if not self.busy:
            self.dismiss(None)


__all__ = [
    "CatalogConfirmation",
    "CatalogField",
    "CatalogFormScreen",
    "ProjectManagerScreen",
]
