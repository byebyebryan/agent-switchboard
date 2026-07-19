"""Optional Textual application shell for the terminal frontend."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Static

from .domain import PresentationContext
from .tui_gateway import SnapshotSource, SwbctlGateway, resolve_terminal_context


class SwitchboardApp(App[None]):
    """Phase 4A application shell; product rows arrive in a later increment."""

    TITLE = "Switchboard"
    SUB_TITLE = "Terminal session router"
    BINDINGS = (Binding("q", "quit", "Quit"),)

    def __init__(
        self,
        *,
        gateway: SwbctlGateway,
        terminal_context: PresentationContext,
    ) -> None:
        super().__init__()
        self.gateway = gateway
        self.snapshots = SnapshotSource(gateway)
        self.terminal_context = terminal_context

    def compose(self) -> ComposeResult:
        yield Static(
            "Switchboard terminal frontend foundation",
            id="phase-4a-foundation",
        )
        yield Footer()


def run_tui(*, swbctl_executable: str | Path) -> int:
    """Run the optional terminal frontend."""

    SwitchboardApp(
        gateway=SwbctlGateway(swbctl_executable),
        terminal_context=resolve_terminal_context(),
    ).run()
    return 0
