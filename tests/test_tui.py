from __future__ import annotations

import asyncio
import copy
import importlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

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
HOST_ID = "11111111-1111-4111-8111-111111111111"
LOCATION_ID = "44444444-4444-4444-8444-444444444444"
SURFACE_ID = "33333333-3333-4333-8333-333333333333"
STOP_SURFACE_ID = "33333333-3333-4333-8333-333333333334"
TMUX_CLIENT = "/dev/pts/7"
REQUEST_IDS = (
    UUID("99999999-9999-4999-8999-999999999991"),
    UUID("99999999-9999-4999-8999-999999999992"),
    UUID("99999999-9999-4999-8999-999999999993"),
)


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


def _stoppable_snapshot() -> Any:
    value = _mixed_snapshot().to_dict()
    claude = value["sessions"][1]
    surface = copy.deepcopy(value["surfaces"][0])
    surface.update(
        {
            "surfaceId": STOP_SURFACE_ID,
            "provider": "claude",
            "currentSessionKey": claude["sessionKey"],
            "transportLocator": "as-claude:@2.%2",
            "launchId": "66666666-6666-4666-8666-666666666667",
        }
    )
    claude["surfaceId"] = surface["surfaceId"]
    value["surfaces"].append(surface)
    return protocol_module.SnapshotEnvelope.from_dict(value)


def _plan(kind: str, *, client: str | None = None) -> Any:
    fields: dict[str, Any] = {
        "kind": kind,
        "hostId": HOST_ID,
        "surfaceId": SURFACE_ID,
        "workspaceId": "as-test",
        "tmuxTarget": "as-test:@1.%1",
    }
    if client is not None:
        fields["tmuxClient"] = client
    return protocol_module.PresentationPlanEnvelope.from_dict(
        {
            "schemaVersion": 1,
            "protocolVersion": 1,
            "plan": fields,
        }
    )


def _blocked_plan(
    code: str = "surface_unavailable",
    message: str = "The selected session cannot be presented.",
) -> Any:
    return protocol_module.PresentationPlanEnvelope.from_dict(
        {
            "schemaVersion": 1,
            "protocolVersion": 1,
            "plan": {
                "kind": "blocked",
                "hostId": HOST_ID,
                "error": {
                    "code": code,
                    "message": message,
                    "scope": "session",
                    "retryable": True,
                    "observedAt": NOW_MS,
                },
            },
        }
    )


def _stop_action(status: str, *, blocked: bool = False) -> Any:
    session_key = _stoppable_snapshot().sessions[1]["sessionKey"]
    action: dict[str, Any] = {
        "kind": "stop",
        "status": status,
        "hostId": HOST_ID,
        "sessionKey": session_key,
    }
    if blocked:
        action["error"] = {
            "code": "stop_revalidation_failed",
            "message": "The session is no longer safe to stop.",
            "scope": "session",
            "retryable": True,
            "observedAt": NOW_MS,
            "hostId": HOST_ID,
            "provider": "claude",
            "sessionKey": session_key,
        }
    return protocol_module.SessionActionEnvelope.from_dict(
        {
            "schemaVersion": 1,
            "protocolVersion": 1,
            "action": action,
        }
    )


class FakeGateway:
    def __init__(
        self,
        *,
        retained: Any,
        full: list[Any] | None = None,
        full_started: asyncio.Event | None = None,
        full_release: asyncio.Event | None = None,
        plan: Any | None = None,
        action: Any | None = None,
        prepare_started: asyncio.Event | None = None,
        prepare_release: asyncio.Event | None = None,
        prepare_cancelled: asyncio.Event | None = None,
    ) -> None:
        self.retained = retained
        self.full = [] if full is None else list(full)
        self.full_started = full_started
        self.full_release = full_release
        self.plan = _blocked_plan() if plan is None else plan
        self.stop_action = (
            _stop_action("blocked", blocked=True) if action is None else action
        )
        self.prepare_started = prepare_started
        self.prepare_release = prepare_release
        self.prepare_cancelled = prepare_cancelled
        self.calls: list[str] = []
        self.action_calls: list[tuple[Any, ...]] = []

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

    async def _prepare(self) -> Any:
        if self.prepare_started is not None:
            self.prepare_started.set()
        try:
            if self.prepare_release is not None:
                await self.prepare_release.wait()
        except asyncio.CancelledError:
            if self.prepare_cancelled is not None:
                self.prepare_cancelled.set()
            raise
        if isinstance(self.plan, BaseException):
            raise self.plan
        return self.plan

    async def prepare_open(
        self,
        session_key: str,
        *,
        request_id: str,
        context: Any,
    ) -> Any:
        self.action_calls.append(("open", session_key, request_id, context))
        return await self._prepare()

    async def prepare_new(
        self,
        project_id: str,
        *,
        location_id: str | None,
        provider: str,
        request_id: str,
        context: Any,
    ) -> Any:
        self.action_calls.append(
            ("new", project_id, location_id, provider, request_id, context)
        )
        return await self._prepare()

    async def prepare_history(
        self,
        project_id: str,
        *,
        location_id: str | None,
        request_id: str,
        context: Any,
    ) -> Any:
        self.action_calls.append(
            ("history", project_id, location_id, request_id, context)
        )
        return await self._prepare()

    async def stop_session(self, session_key: str) -> Any:
        self.action_calls.append(("stop", session_key))
        return self.stop_action

    async def select_surface(self, surface_id: str, *, client: str) -> None:
        self.action_calls.append(("select", surface_id, client))

    def attach_surface_command(self, surface_id: str) -> tuple[str, ...]:
        self.action_calls.append(("attach", surface_id))
        return ("/fake/swbctl", "attach-surface", surface_id)


def _app(
    gateway: FakeGateway,
    *,
    tmux_client: str | None = None,
    request_ids: tuple[UUID, ...] = REQUEST_IDS,
) -> Any:
    ids = iter(request_ids)
    return tui_module.SwitchboardApp(
        gateway=gateway,
        terminal_context=domain_module.PresentationContext(
            True,
            tmux_client,
            False,
            False,
        ),
        now_ms=lambda: NOW_MS,
        request_id_factory=lambda: next(ids),
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


def test_application_renders_status_navigation_details_and_help() -> None:
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


def test_plain_terminal_open_returns_public_attach_handoff() -> None:
    async def exercise() -> None:
        context = domain_module.PresentationContext(True, None, False, False)
        gateway = FakeGateway(retained=_mixed_snapshot(), plan=_plan("attach"))
        app = _app(gateway)
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            session_key = app.model.selected_row.session_key
            await pilot.press("o")
            await _wait_until(
                pilot,
                lambda: not app.is_running,
                message="attach plan did not close the TUI",
            )
            assert app.return_value == (
                "/fake/swbctl",
                "attach-surface",
                SURFACE_ID,
            )
            assert gateway.action_calls == [
                ("open", session_key, str(REQUEST_IDS[0]), context),
                ("attach", SURFACE_ID),
            ]

    asyncio.run(exercise())


def test_tmux_open_selects_only_inherited_client_then_exits() -> None:
    async def exercise() -> None:
        context = domain_module.PresentationContext(
            True,
            TMUX_CLIENT,
            False,
            False,
        )
        gateway = FakeGateway(
            retained=_mixed_snapshot(),
            plan=_plan("switch", client=TMUX_CLIENT),
        )
        app = _app(gateway, tmux_client=TMUX_CLIENT)
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            session_key = app.model.selected_row.session_key
            await pilot.press("enter")
            await _wait_until(
                pilot,
                lambda: not app.is_running,
                message="switch plan did not close the TUI",
            )
            assert app.return_value is None
            assert gateway.action_calls == [
                ("open", session_key, str(REQUEST_IDS[0]), context),
                ("select", SURFACE_ID, TMUX_CLIENT),
            ]

    asyncio.run(exercise())


def test_blocked_open_stays_visible_and_later_retry_gets_fresh_request_id() -> None:
    async def exercise() -> None:
        gateway = FakeGateway(retained=_mixed_snapshot(), plan=_blocked_plan())
        app = _app(gateway)
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            table = app.query_one("#sessions", widgets_module.DataTable)
            await pilot.press("o")
            await _wait_until(
                pilot,
                lambda: app.action_error is not None and not app.action_busy,
                message="blocked plan was not published",
            )
            assert app.is_running is True
            assert table.has_focus
            assert "surface_unavailable" in str(
                app.query_one("#issues", widgets_module.Static).content
            )
            assert "ACTION ERROR surface_unavailable" in str(
                app.query_one("#status", widgets_module.Static).content
            )

            await pilot.press("o")
            await _wait_until(
                pilot,
                lambda: len(gateway.action_calls) == 2 and not app.action_busy,
                message="independent retry did not complete",
            )
            assert [call[2] for call in gateway.action_calls] == [
                str(REQUEST_IDS[0]),
                str(REQUEST_IDS[1]),
            ]

    asyncio.run(exercise())


def test_duplicate_inflight_open_is_ignored_and_quit_cancels_worker() -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        cancelled = asyncio.Event()
        gateway = FakeGateway(
            retained=_mixed_snapshot(),
            plan=_blocked_plan(),
            prepare_started=started,
            prepare_release=release,
            prepare_cancelled=cancelled,
        )
        app = _app(gateway)
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            await pilot.press("o")
            await started.wait()
            await pilot.press("o")
            await pilot.press("x")
            await pilot.press("n")
            await pilot.pause()
            assert len(gateway.action_calls) == 1
            assert gateway.action_calls[0][2] == str(REQUEST_IDS[0])
            assert app.action_error is None
            assert app.action_busy is True
            await pilot.press("q")
            await _wait_until(
                pilot,
                cancelled.is_set,
                message="preparation worker was not cancelled",
            )
            assert app.is_running is False

    asyncio.run(exercise())


def test_prepare_command_failure_stays_in_tui_with_bounded_error() -> None:
    async def exercise() -> None:
        failure = gateway_module.GatewayError(
            "command_timeout",
            "The Switchboard command exceeded its deadline.",
            retryable=True,
        )
        gateway = FakeGateway(retained=_mixed_snapshot(), plan=failure)
        app = _app(gateway)
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            await pilot.press("o")
            await _wait_until(
                pilot,
                lambda: app.action_error is not None and not app.action_busy,
                message="command failure was not published",
            )
            assert app.is_running is True
            assert app.action_error.code == "command_timeout"
            assert "exceeded its deadline" in str(
                app.query_one("#issues", widgets_module.Static).content
            )
            assert "ACTION ERROR command_timeout" in str(
                app.query_one("#status", widgets_module.Static).content
            )

    asyncio.run(exercise())


def test_new_and_history_use_declared_target_picker() -> None:
    async def exercise_new() -> None:
        context = domain_module.PresentationContext(True, None, False, False)
        gateway = FakeGateway(retained=_mixed_snapshot(), plan=_plan("attach"))
        app = _app(gateway)
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            await pilot.press("n")
            assert isinstance(app.screen, tui_module.TargetPicker)
            await pilot.press("enter")
            await _wait_until(
                pilot,
                lambda: not app.is_running,
                message="new-session attach plan did not exit",
            )
            assert gateway.action_calls[:1] == [
                (
                    "new",
                    PROJECT_ID,
                    LOCATION_ID,
                    "codex",
                    str(REQUEST_IDS[0]),
                    context,
                )
            ]

    async def exercise_history() -> None:
        context = domain_module.PresentationContext(True, None, False, False)
        gateway = FakeGateway(retained=_mixed_snapshot(), plan=_blocked_plan())
        app = _app(gateway)
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            await pilot.press("h")
            picker = app.screen
            assert isinstance(picker, tui_module.TargetPicker)
            assert len(picker.targets) == 1
            assert picker.targets[0].provider.value == "claude"
            await pilot.press("escape")
            await pilot.pause()
            assert gateway.action_calls == []

            await pilot.press("h")
            await pilot.press("enter")
            await _wait_until(
                pilot,
                lambda: app.action_error is not None and not app.action_busy,
                message="history blocked plan did not complete",
            )
            assert gateway.action_calls == [
                (
                    "history",
                    PROJECT_ID,
                    LOCATION_ID,
                    str(REQUEST_IDS[0]),
                    context,
                )
            ]

    asyncio.run(exercise_new())
    asyncio.run(exercise_history())


def test_stop_requires_public_eligibility_confirmation_and_revalidation() -> None:
    async def exercise_ineligible() -> None:
        gateway = FakeGateway(retained=_mixed_snapshot())
        app = _app(gateway)
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            await pilot.press("x")
            assert app.action_error.code == "stop_not_eligible"
            assert gateway.action_calls == []

    async def exercise_eligible() -> None:
        snapshot = _stoppable_snapshot()
        gateway = FakeGateway(
            retained=snapshot,
            action=_stop_action("stopped"),
        )
        app = _app(gateway)
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            session_key = app.model.selected_row.session_key
            assert app.model.selected_row.can_stop is True
            await pilot.press("x")
            assert isinstance(app.screen, tui_module.StopConfirmation)
            await pilot.press("n")
            await pilot.pause()
            assert gateway.action_calls == []

            await pilot.press("x")
            await pilot.press("y")
            await _wait_until(
                pilot,
                lambda: (
                    gateway.action_calls == [("stop", session_key)]
                    and not app.action_busy
                    and not app.refreshing
                ),
                message="confirmed stop did not refresh retained state",
            )
            assert app.action_message == "Session stopped"
            assert gateway.calls == ["none", "none"]

    asyncio.run(exercise_ineligible())
    asyncio.run(exercise_eligible())


def test_blocked_stop_stays_in_tui_with_stable_reason() -> None:
    async def exercise() -> None:
        gateway = FakeGateway(retained=_stoppable_snapshot())
        app = _app(gateway)
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            await pilot.press("x")
            await pilot.press("y")
            await _wait_until(
                pilot,
                lambda: app.action_error is not None and not app.action_busy,
                message="blocked stop did not complete",
            )
            assert app.is_running is True
            assert app.action_error.code == "stop_revalidation_failed"
            assert "no longer safe to stop" in str(
                app.query_one("#issues", widgets_module.Static).content
            )

    asyncio.run(exercise())


def test_terminal_handoff_exec_is_exact_and_failure_is_restored(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = ("/installed/swbctl", "attach-surface", SURFACE_ID)
    calls: list[tuple[str, tuple[str, ...]]] = []

    def returned(executable: str, argv: Any) -> None:
        calls.append((executable, tuple(argv)))

    assert tui_module._execute_terminal_handoff(command, exec_replace=returned) == 1
    assert calls == [(command[0], command)]
    assert "terminal restored" in capsys.readouterr().err

    def failed(_executable: str, _argv: Any) -> None:
        raise OSError("private failure")

    assert tui_module._execute_terminal_handoff(command, exec_replace=failed) == 1
    error = capsys.readouterr().err
    assert "terminal restored" in error
    assert "private failure" not in error
