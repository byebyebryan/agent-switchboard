from __future__ import annotations

import asyncio
import importlib

import pytest

pytest.importorskip("textual")
tui_module = importlib.import_module("agent_switchboard.tui")


def test_textual_application_runs_headlessly() -> None:
    async def exercise() -> None:
        app = tui_module.SwitchboardApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.query_one("#phase-4a-foundation") is not None
            await pilot.press("q")

    asyncio.run(exercise())
