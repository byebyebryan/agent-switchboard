"""Optional Textual application shell for the terminal frontend."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Static


class SwitchboardApp(App[None]):
    """Phase 4A application shell; product rows arrive in a later increment."""

    TITLE = "Switchboard"
    SUB_TITLE = "Terminal session router"
    BINDINGS = (Binding("q", "quit", "Quit"),)

    def compose(self) -> ComposeResult:
        yield Static(
            "Switchboard terminal frontend foundation",
            id="phase-4a-foundation",
        )
        yield Footer()


def run_tui() -> int:
    """Run the optional terminal frontend."""

    SwitchboardApp().run()
    return 0
