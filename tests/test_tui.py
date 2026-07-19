from __future__ import annotations

import asyncio
import copy
import importlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")
tui_module = importlib.import_module("agent_switchboard.tui")
domain_module = importlib.import_module("agent_switchboard.domain")
gateway_module = importlib.import_module("agent_switchboard.tui_gateway")
protocol_module = importlib.import_module("agent_switchboard.protocol")
widgets_module = importlib.import_module("textual.widgets")

ROOT = Path(__file__).parents[1]
SNAPSHOT_FIXTURE = ROOT / "tests/fixtures/protocol/v1/snapshot.json"
NOW_MS = 1_784_142_010_000
PROJECT_ID = "22222222-2222-4222-8222-222222222222"


def _value() -> dict[str, Any]:
    return json.loads(SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))


def _mixed_snapshot(*, degraded: bool = False) -> Any:
    value = _value()
    codex = value["sessions"][0]
    codex["name"] = "Codex Build"
    codex["lastActivityAt"] = value["generatedAt"]
    claude = copy.deepcopy(codex)
    provider_session_id = "77777777-7777-4777-8777-777777777777"
    claude.update(
        {
            "sessionKey": (
                "11111111-1111-4111-8111-111111111111:claude:" + provider_session_id
            ),
            "provider": "claude",
            "providerSessionId": provider_session_id,
            "name": "Claude Review",
            "runtimePresence": "live",
            "activity": "needs_input",
            "activityReason": "permission",
            "attachment": "attached",
            "lastActivityAt": int(value["generatedAt"]) - 1_000,
            "lastObservedAt": int(value["generatedAt"]) - 1_000,
        }
    )
    claude.pop("surfaceId", None)
    value["sessions"] = [codex, claude]
    if degraded:
        value["capabilities"].append(
            {
                "provider": "claude",
                "available": False,
                "providerVersion": "2.1.210",
                "testedContractRange": {
                    "minimum": "2.1.210",
                    "maximum": "2.1.210",
                },
                "features": ["hooks", "native_resume", "tmux_runtime"],
                "degradedReasons": [
                    {
                        "code": "agent_view_enabled",
                        "message": "Agent View must be disabled.",
                        "feature": "tmux_runtime",
                        "retryable": False,
                    }
                ],
            }
        )
        value["errors"] = [
            {
                "code": "provider_probe_failed",
                "message": "Claude capability probe failed.",
                "scope": "provider",
                "provider": "claude",
                "retryable": True,
                "observedAt": value["generatedAt"],
            }
        ]
    return protocol_module.SnapshotEnvelope.from_dict(value)


def _changed_snapshot() -> Any:
    base = _mixed_snapshot().to_dict()
    value = copy.deepcopy(base)
    value["generatedAt"] = int(value["generatedAt"]) + 5_000
    template = copy.deepcopy(value["sessions"][0])
    provider_session_id = "88888888-8888-4888-8888-888888888888"
    template.update(
        {
            "sessionKey": (
                "11111111-1111-4111-8111-111111111111:codex:" + provider_session_id
            ),
            "providerSessionId": provider_session_id,
            "name": "Codex Ready",
            "activity": "ready",
            "activityReason": "turn_complete",
            "attachment": "none",
            "lastActivityAt": value["generatedAt"],
            "lastObservedAt": value["generatedAt"],
        }
    )
    template.pop("surfaceId", None)
    value["sessions"].append(template)
    return protocol_module.SnapshotEnvelope.from_dict(value)


def _empty_snapshot() -> Any:
    value = _value()
    for collection in (
        "projects",
        "locations",
        "sessions",
        "runtimes",
        "surfaces",
        "capabilities",
        "errors",
    ):
        value[collection] = []
    return protocol_module.SnapshotEnvelope.from_dict(value)


class FakeGateway:
    def __init__(
        self,
        *,
        retained: Any,
        full: list[Any] | None = None,
        full_started: asyncio.Event | None = None,
        full_release: asyncio.Event | None = None,
    ) -> None:
        self.retained = retained
        self.full = [] if full is None else list(full)
        self.full_started = full_started
        self.full_release = full_release
        self.calls: list[str] = []

    async def snapshot(self, *, reconcile: str) -> Any:
        self.calls.append(reconcile)
        if reconcile == "none":
            return self.retained
        if reconcile != "full" or not self.full:
            raise AssertionError(f"unexpected snapshot mode: {reconcile}")
        if self.full_started is not None:
            self.full_started.set()
        if self.full_release is not None:
            await self.full_release.wait()
        result = self.full.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _app(gateway: FakeGateway) -> Any:
    return tui_module.SwitchboardApp(
        gateway=gateway,
        terminal_context=domain_module.PresentationContext(True, None, False, False),
        now_ms=lambda: NOW_MS,
    )


async def _wait_until(
    pilot: Any,
    condition: Callable[[], bool],
    *,
    message: str,
) -> None:
    deadline = asyncio.get_running_loop().time() + 2
    while not condition():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(message)
        await pilot.pause(0.01)


def test_read_only_application_renders_status_navigation_details_and_help() -> None:
    async def exercise() -> None:
        gateway = FakeGateway(retained=_mixed_snapshot(degraded=True))
        app = _app(gateway)
        async with app.run_test(size=(120, 32)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            table = app.query_one("#sessions", widgets_module.DataTable)
            assert table.row_count == 2
            assert "! needs input" in str(table.get_row_at(0)[0])
            assert "Claude Review" in str(
                app.query_one("#details", widgets_module.Static).content
            )
            assert "agent_view_enabled" in str(
                app.query_one("#issues", widgets_module.Static).content
            )
            status = str(app.query_one("#status", widgets_module.Static).content)
            assert "2/2 sessions" in status
            assert "2 issue(s)" in status
            assert "claude degraded" in status
            assert "plain terminal" in status
            assert "read-only" in status

            await _wait_until(
                pilot,
                lambda: table.has_focus,
                message="initial session table focus was not established",
            )
            await pilot.press("down")
            await _wait_until(
                pilot,
                lambda: app.model.selected_row.name == "Codex Build",
                message="keyboard navigation did not retain the highlighted row",
            )
            assert "~ working" in str(
                app.query_one("#details", widgets_module.Static).content
            )

            await pilot.press("?")
            assert app.query_one("#help", widgets_module.Static).display is True
            await pilot.press("e")
            assert app.query_one("#side-panel").has_focus
            await pilot.press("q")
            await pilot.pause()
            assert app.is_running is False

    asyncio.run(exercise())


def test_search_and_all_filter_axes_update_the_pure_model() -> None:
    async def exercise() -> None:
        app = _app(FakeGateway(retained=_mixed_snapshot()))
        async with app.run_test(size=(120, 32)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            await pilot.press("/")
            await pilot.press(*tuple("review"))
            await _wait_until(
                pilot,
                lambda: len(app.model.visible_rows) == 1,
                message="search did not filter rows",
            )
            assert app.model.visible_rows[0].name == "Claude Review"

            await pilot.press("ctrl+l")
            await _wait_until(
                pilot,
                lambda: len(app.model.visible_rows) == 2,
                message="clear filters did not restore rows",
            )
            await pilot.press("q")
            await _wait_until(
                pilot,
                lambda: app.query_one("#search", widgets_module.Input).value == "q",
                message="search input did not retain printable binding keys",
            )
            assert app.is_running is True
            await pilot.press("ctrl+l")
            app.query_one("#provider-filter", widgets_module.Select).value = "claude"
            app.query_one("#project-filter", widgets_module.Select).value = PROJECT_ID
            app.query_one(
                "#activity-filter", widgets_module.Select
            ).value = "needs_input"
            app.query_one("#runtime-filter", widgets_module.Select).value = "live"
            app.query_one(
                "#attachment-filter", widgets_module.Select
            ).value = "attached"
            await _wait_until(
                pilot,
                lambda: len(app.model.visible_rows) == 1,
                message="axis filters did not converge",
            )
            assert app.model.visible_rows[0].name == "Claude Review"

            app.query_one("#activity-filter", widgets_module.Select).value = "working"
            await _wait_until(
                pilot,
                lambda: not app.model.visible_rows,
                message="conflicting filter did not produce an empty result",
            )
            assert "No sessions match" in str(
                app.query_one("#details", widgets_module.Static).content
            )

    asyncio.run(exercise())


def test_refresh_is_coalesced_and_applies_one_new_snapshot() -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        changed = _changed_snapshot()
        gateway = FakeGateway(
            retained=_mixed_snapshot(),
            full=[changed],
            full_started=started,
            full_release=release,
        )
        app = _app(gateway)
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            app.query_one("#sessions", widgets_module.DataTable).focus()
            await pilot.press("r")
            await started.wait()
            await pilot.press("r")
            await pilot.pause()
            assert gateway.calls == ["none", "full"]
            release.set()
            await _wait_until(
                pilot,
                lambda: (
                    app.model.generated_at == changed.generated_at
                    and not app.refreshing
                ),
                message="coalesced refresh did not render",
            )
            assert len(app.model.rows) == 3

    asyncio.run(exercise())


def test_refresh_failure_preserves_rows_and_exposes_then_clears_error() -> None:
    async def exercise() -> None:
        retained = _mixed_snapshot()
        changed = _changed_snapshot()
        failure = gateway_module.GatewayError(
            "command_timeout",
            "Refresh timed out.",
            retryable=True,
        )
        gateway = FakeGateway(retained=retained, full=[failure, changed])
        app = _app(gateway)
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            app.query_one("#sessions", widgets_module.DataTable).focus()
            await pilot.press("r")
            await _wait_until(
                pilot,
                lambda: app.last_error is not None and not app.refreshing,
                message="refresh error was not published",
            )
            assert len(app.model.rows) == 2
            assert "ERROR command_timeout" in str(
                app.query_one("#status", widgets_module.Static).content
            )
            assert "Refresh timed out." in str(
                app.query_one("#issues", widgets_module.Static).content
            )

            await pilot.press("r")
            await _wait_until(
                pilot,
                lambda: app.last_error is None and not app.refreshing,
                message="successful refresh did not clear the error",
            )
            assert len(app.model.rows) == 3
            assert (
                str(app.query_one("#issues", widgets_module.Static).content)
                == "No current issues."
            )

    asyncio.run(exercise())


def test_narrow_and_empty_layout_remain_usable() -> None:
    async def exercise() -> None:
        app = _app(FakeGateway(retained=_empty_snapshot()))
        async with app.run_test(size=(120, 32)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="empty snapshot did not render",
            )
            assert not app.query_one("#content").has_class("narrow")
            await pilot.resize_terminal(
                tui_module.MIN_TERMINAL_WIDTH,
                tui_module.MIN_TERMINAL_HEIGHT,
            )
            await pilot.pause()
            assert app.query_one("#content").has_class("narrow")
            assert app.query_one("#filters").has_class("narrow")
            assert app.query_one("#sessions").region.height > 0
            assert app.query_one("#side-panel").region.height > 0
            assert "0/0 sessions" in str(
                app.query_one("#status", widgets_module.Static).content
            )
            assert "No sessions are currently known" in str(
                app.query_one("#details", widgets_module.Static).content
            )

    asyncio.run(exercise())
