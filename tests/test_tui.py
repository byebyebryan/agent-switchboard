from __future__ import annotations

import asyncio
import importlib
import sys

import pytest

pytest.importorskip("textual")
tui_module = importlib.import_module("agent_switchboard.tui")
domain_module = importlib.import_module("agent_switchboard.domain")
gateway_module = importlib.import_module("agent_switchboard.tui_gateway")


def test_textual_application_runs_headlessly() -> None:
    async def exercise() -> None:
        app = tui_module.SwitchboardApp(
            gateway=gateway_module.SwbctlGateway(sys.executable),
            terminal_context=domain_module.PresentationContext(
                True, None, False, False
            ),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.query_one("#phase-4a-foundation") is not None
            assert app.snapshots.gateway is app.gateway
            await pilot.press("q")

    asyncio.run(exercise())
