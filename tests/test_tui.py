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
SNAPSHOT_FIXTURE = ROOT / "tests/fixtures/protocol/v2/snapshot.json"
NOW_MS = 1_784_142_010_000
PROJECT_ID = "22222222-2222-4222-8222-222222222222"
HOST_ID = "11111111-1111-4111-8111-111111111111"
LOCATION_ID = "44444444-4444-4444-8444-444444444444"
TASK_ID = "88888888-8888-4888-8888-888888888888"
SURFACE_ID = "33333333-3333-4333-8333-333333333333"
STOP_SURFACE_ID = "33333333-3333-4333-8333-333333333334"
TMUX_CLIENT = "/dev/pts/7"
REQUEST_IDS = (
    UUID("99999999-9999-4999-8999-999999999991"),
    UUID("99999999-9999-4999-8999-999999999992"),
    UUID("99999999-9999-4999-8999-999999999993"),
)
HANDOFF_IDS = (
    UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"),
    UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"),
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
    codex.pop("taskId", None)
    claude.pop("taskId", None)
    value["tasks"] = []
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
        "projectRepositories",
        "repositories",
        "checkouts",
        "tasks",
        "sessions",
        "runtimes",
        "surfaces",
        "capabilities",
        "errors",
    ):
        value[collection] = []
    return protocol_module.SnapshotEnvelope.from_dict(value)


def _fleet(snapshot: Any) -> Any:
    return protocol_module.FleetEnvelope(
        generated_at=snapshot.generated_at,
        local_host_id=snapshot.host.host_id,
        hosts=(
            protocol_module.FleetHost(
                source=protocol_module.FleetSource.LOCAL,
                remote_name=None,
                host_id=snapshot.host.host_id,
                display_name=snapshot.host.display_name,
                reachability=protocol_module.FleetReachability.ONLINE,
                snapshot_observed_at=snapshot.generated_at,
                snapshot_received_at=snapshot.generated_at,
                last_attempt_at=snapshot.generated_at,
                stale=False,
                error=None,
                snapshot=snapshot,
            ),
        ),
    )


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


def _closed_task_snapshot() -> Any:
    value = _value()
    task = value["tasks"][0]
    task["status"] = "closed"
    task["closedAt"] = value["generatedAt"]
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
            "schemaVersion": 2,
            "protocolVersion": 2,
            "plan": fields,
        }
    )


def _blocked_plan(
    code: str = "surface_unavailable",
    message: str = "The selected session cannot be presented.",
) -> Any:
    return protocol_module.PresentationPlanEnvelope.from_dict(
        {
            "schemaVersion": 2,
            "protocolVersion": 2,
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
            "schemaVersion": 2,
            "protocolVersion": 2,
            "action": action,
        }
    )


def _detail(snapshot: Any, session_key: str) -> Any:
    value = snapshot.to_dict()
    session = next(
        session for session in value["sessions"] if session["sessionKey"] == session_key
    )
    return protocol_module.SessionDetailEnvelope.from_dict(
        {
            "schemaVersion": 2,
            "protocolVersion": 2,
            "generatedAt": value["generatedAt"],
            "session": session,
            "handoffs": [],
            "handoffsTruncated": False,
        }
    )


def _curated_detail(
    snapshot: Any,
    session_key: str,
    *,
    generated_at: int | None = None,
    session_updates: dict[str, Any] | None = None,
    handoffs: tuple[tuple[str, int, str, str], ...] = (),
    truncated: bool = False,
) -> Any:
    value = snapshot.to_dict()
    session = copy.deepcopy(
        next(
            session
            for session in value["sessions"]
            if session["sessionKey"] == session_key
        )
    )
    if session_updates is not None:
        session.update(session_updates)
    if handoffs:
        session["latestHandoffId"] = handoffs[0][0]
    records = [
        {
            "handoffId": handoff_id,
            "sessionKey": session_key,
            "sequence": sequence,
            "summary": summary,
            "nextAction": next_action,
            "source": "user",
            "sourceHostId": session["hostId"],
            "createdAt": int(value["generatedAt"]) + sequence,
            "contentHash": domain_module.handoff_content_hash(summary, next_action),
        }
        for handoff_id, sequence, summary, next_action in handoffs
    ]
    return protocol_module.SessionDetailEnvelope.from_dict(
        {
            "schemaVersion": 2,
            "protocolVersion": 2,
            "generatedAt": (
                int(value["generatedAt"]) if generated_at is None else generated_at
            ),
            "session": session,
            "handoffs": records,
            "handoffsTruncated": truncated,
        }
    )


def _catalog() -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "protocolVersion": 2,
        "catalogVersion": 1,
        "generatedAt": NOW_MS,
        "hostId": HOST_ID,
        "operation": None,
        "projects": [
            {
                "projectId": PROJECT_ID,
                "name": "Switchboard",
                "aliases": ["asb"],
                "defaultProvider": "codex",
                "defaultTransport": "tmux",
                "declared": True,
                "references": {},
                "repositories": [
                    {
                        "repositoryId": "33333333-3333-4333-8333-333333333333",
                        "name": "Switchboard",
                        "kind": "git",
                        "isPrimary": True,
                        "declared": True,
                        "contextSources": ["AGENTS.md"],
                        "references": {},
                        "checkouts": [
                            {
                                "checkoutId": LOCATION_ID,
                                "path": "/work/switchboard",
                                "kind": "main",
                                "displayName": "main",
                                "providerOverride": None,
                                "transportOverride": None,
                                "isDefault": True,
                                "declared": True,
                                "present": True,
                                "branch": "main",
                                "headOid": None,
                                "references": {},
                            }
                        ],
                    }
                ],
            }
        ],
    }


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
        close_action: Any | None = None,
        prepare_started: asyncio.Event | None = None,
        prepare_release: asyncio.Event | None = None,
        prepare_cancelled: asyncio.Event | None = None,
        detail: Any | None = None,
        mutation_detail: Any | None = None,
        catalog: Any | None = None,
    ) -> None:
        self.retained = retained
        self.full = [] if full is None else list(full)
        self.full_started = full_started
        self.full_release = full_release
        self.plan = _blocked_plan() if plan is None else plan
        self.stop_action = (
            _stop_action("blocked", blocked=True) if action is None else action
        )
        self.close_action = (
            protocol_module.TaskCloseActionEnvelope(
                protocol_module.TaskCloseAction(
                    protocol_module.TaskCloseStatus.CLOSED,
                    domain_module.HostId(HOST_ID),
                    domain_module.TaskId(TASK_ID),
                    protocol_module.RuntimeDisposition.NO_SESSION,
                )
            )
            if close_action is None
            else close_action
        )
        self.prepare_started = prepare_started
        self.prepare_release = prepare_release
        self.prepare_cancelled = prepare_cancelled
        self.detail = detail
        self.mutation_detail = mutation_detail
        self.catalog = _catalog() if catalog is None else catalog
        self.calls: list[str] = []
        self.detail_calls: list[str] = []
        self.action_calls: list[tuple[Any, ...]] = []

    @staticmethod
    def _result(configured: Any, fallback: Callable[[], Any]) -> Any:
        if isinstance(configured, list):
            result = configured.pop(0) if configured else None
        else:
            result = configured
        result = fallback() if result is None else result
        if isinstance(result, BaseException):
            raise result
        return result

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

    async def fleet(self, *, refresh: bool) -> Any:
        snapshot = await self.snapshot(reconcile="full" if refresh else "none")
        return _fleet(snapshot)

    async def project_catalog(self, *, include_archived: bool = True) -> Any:
        assert include_archived is True
        self.action_calls.append(("project-list",))
        return self.catalog

    async def project_action(self, arguments: tuple[str, ...] | list[str]) -> Any:
        self.action_calls.append(("project-action", *arguments))
        return self.catalog

    async def project_export(self, project_id: str) -> Any:
        self.action_calls.append(("project-export", project_id))
        return {
            "schemaVersion": 2,
            "protocolVersion": 2,
            "projectExportVersion": 1,
            "generatedAt": NOW_MS,
            "project": {"projectId": project_id},
        }

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
        host_id: str | None = None,
    ) -> Any:
        assert host_id is None
        self.action_calls.append(("open", session_key, request_id, context))
        return await self._prepare()

    async def prepare_task_create(
        self,
        task_id: str,
        *,
        project_id: str,
        title: str,
        checkout_id: str | None,
        provider: str,
        purpose: str | None = None,
        request_id: str,
        context: Any,
        host_id: str | None = None,
    ) -> Any:
        assert host_id is None
        self.action_calls.append(
            (
                "new",
                task_id,
                project_id,
                title,
                checkout_id,
                provider,
                purpose,
                request_id,
                context,
            )
        )
        return await self._prepare()

    async def prepare_history(
        self,
        project_id: str,
        *,
        checkout_id: str | None,
        request_id: str,
        context: Any,
        host_id: str | None = None,
    ) -> Any:
        assert host_id is None
        self.action_calls.append(
            ("history", project_id, checkout_id, request_id, context)
        )
        return await self._prepare()

    async def prepare_task(
        self,
        task_id: str,
        *,
        provider: str | None,
        request_id: str,
        context: Any,
        host_id: str | None = None,
        reopen: bool = False,
    ) -> Any:
        assert host_id is None
        self.action_calls.append(
            (
                "reopen-open" if reopen else "continue",
                task_id,
                provider,
                request_id,
                context,
            )
        )
        return await self._prepare()

    async def close_task(
        self,
        task_id: str,
        *,
        host_id: str | None = None,
    ) -> Any:
        assert host_id is None
        self.action_calls.append(("close", task_id))
        return self.close_action

    async def stop_session(
        self,
        session_key: str,
        *,
        host_id: str | None = None,
    ) -> Any:
        assert host_id is None
        self.action_calls.append(("stop", session_key))
        return self.stop_action

    async def session_detail(
        self,
        session_key: str,
        *,
        handoff_limit: int = 20,
    ) -> Any:
        self.detail_calls.append(session_key)
        return self._result(
            self.detail,
            lambda: _detail(self.retained, session_key),
        )

    async def set_session_name(self, session_key: str, value: str | None) -> Any:
        self.action_calls.append(("name", session_key, value))
        return self._result(
            self.mutation_detail,
            lambda: _detail(self.retained, session_key),
        )

    async def set_session_purpose(self, session_key: str, value: str | None) -> Any:
        self.action_calls.append(("purpose", session_key, value))
        return self._result(
            self.mutation_detail,
            lambda: _detail(self.retained, session_key),
        )

    async def set_session_pinned(self, session_key: str, *, pinned: bool) -> Any:
        self.action_calls.append(("pin", session_key, pinned))
        return self._result(
            self.mutation_detail,
            lambda: _detail(self.retained, session_key),
        )

    async def append_session_handoff(
        self,
        session_key: str,
        *,
        handoff_id: str,
        summary: str,
        next_action: str,
        wrap: bool,
    ) -> Any:
        self.action_calls.append(
            (
                "wrap" if wrap else "handoff",
                session_key,
                handoff_id,
                summary,
                next_action,
            )
        )
        return self._result(
            self.mutation_detail,
            lambda: _detail(self.retained, session_key),
        )

    async def select_surface(
        self,
        surface_id: str,
        *,
        client: str,
        host_id: str | None = None,
    ) -> None:
        assert host_id is None
        self.action_calls.append(("select", surface_id, client))

    def attach_surface_command(
        self,
        surface_id: str,
        *,
        host_id: str | None = None,
    ) -> tuple[str, ...]:
        assert host_id is None
        self.action_calls.append(("attach", surface_id))
        return ("/fake/swbctl", "attach-surface", surface_id)


def _app(
    gateway: FakeGateway,
    *,
    tmux_client: str | None = None,
    request_ids: tuple[UUID, ...] = REQUEST_IDS,
    handoff_ids: tuple[UUID, ...] = HANDOFF_IDS,
    initial_view: str = "inbox",
    initial_project_id: str | None = None,
    add_project: bool = False,
) -> Any:
    ids = iter(request_ids)
    handoffs = iter(handoff_ids)
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
        handoff_id_factory=lambda: next(handoffs),
        initial_view=initial_view,
        initial_project_id=initial_project_id,
        add_project=add_project,
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
            assert "2/2 Inbox sessions" in status
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
            await pilot.press("i")
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


def test_projects_view_lists_hierarchy_and_edits_through_gateway() -> None:
    async def exercise() -> None:
        gateway = FakeGateway(retained=_empty_snapshot())
        app = _app(gateway, initial_view="open")
        async with app.run_test(size=(120, 34)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and not app.refreshing,
                message="retained snapshot did not render",
            )
            await pilot.press("4")
            await _wait_until(
                pilot,
                lambda: (
                    isinstance(app.screen, tui_module.ProjectManagerScreen)
                    and not app.screen.busy
                ),
                message="project manager did not load",
            )
            manager = app.screen
            table = manager.query_one("#catalog-table", widgets_module.DataTable)
            assert table.row_count == 3
            assert "Project: Switchboard" in str(
                manager.query_one("#catalog-detail-text", widgets_module.Static).content
            )

            await pilot.press("e")
            assert isinstance(app.screen, tui_module.CatalogFormScreen)
            app.screen.query_one(
                "#catalog-field-name", widgets_module.Input
            ).value = "Switchboard Core"
            await pilot.press("ctrl+s")
            await _wait_until(
                pilot,
                lambda: (
                    isinstance(app.screen, tui_module.ProjectManagerScreen)
                    and not app.screen.busy
                ),
                message="project edit did not complete",
            )
            assert gateway.action_calls[-1] == (
                "project-action",
                "update",
                PROJECT_ID,
                "--name",
                "Switchboard Core",
                "--provider",
                "codex",
                "--alias",
                "asb",
            )

    asyncio.run(exercise())


def test_projects_startup_add_flag_opens_path_first_form() -> None:
    async def exercise() -> None:
        gateway = FakeGateway(retained=_empty_snapshot())
        app = _app(gateway, initial_view="projects", add_project=True)
        async with app.run_test(size=(110, 32)) as pilot:
            await _wait_until(
                pilot,
                lambda: (
                    isinstance(app.screen, tui_module.CatalogFormScreen)
                    and len(app.screen.query("#catalog-field-path")) == 1
                    and app.screen.query_one(
                        "#catalog-field-path", widgets_module.Input
                    ).has_focus
                ),
                message="path-first add form did not open",
            )
            app.screen.query_one(
                "#catalog-field-path", widgets_module.Input
            ).value = "/work/new-project"
            app.screen.query_one(
                "#catalog-field-name", widgets_module.Input
            ).value = "New Project"
            await pilot.press("ctrl+s")
            await _wait_until(
                pilot,
                lambda: (
                    isinstance(app.screen, tui_module.ProjectManagerScreen)
                    and not app.screen.busy
                ),
                message="path-first add did not complete",
            )
            assert gateway.action_calls[-1] == (
                "project-action",
                "add",
                "/work/new-project",
                "--kind",
                "auto",
                "--provider",
                "codex",
                "--name",
                "New Project",
            )

    asyncio.run(exercise())


def test_projects_view_routes_structural_actions_with_confirmation() -> None:
    async def exercise() -> None:
        gateway = FakeGateway(retained=_empty_snapshot())
        app = _app(gateway, initial_view="projects")
        async with app.run_test(size=(110, 32)) as pilot:
            await _wait_until(
                pilot,
                lambda: (
                    isinstance(app.screen, tui_module.ProjectManagerScreen)
                    and not app.screen.busy
                ),
                message="project manager did not load",
            )
            manager = app.screen
            await pilot.press("down", "m")
            await _wait_until(
                pilot,
                lambda: not manager.busy and len(gateway.action_calls) >= 2,
                message="primary repository action did not complete",
            )
            assert gateway.action_calls[-1] == (
                "project-action",
                "repository",
                "primary",
                PROJECT_ID,
                "33333333-3333-4333-8333-333333333333",
            )

            await pilot.press("down", "d")
            await _wait_until(
                pilot,
                lambda: not manager.busy and len(gateway.action_calls) >= 3,
                message="default checkout action did not complete",
            )
            assert gateway.action_calls[-1] == (
                "project-action",
                "checkout",
                "default",
                LOCATION_ID,
            )

            await pilot.press("x")
            assert isinstance(app.screen, tui_module.CatalogConfirmation)
            await pilot.press("y")
            await _wait_until(
                pilot,
                lambda: (
                    isinstance(app.screen, tui_module.ProjectManagerScreen)
                    and not app.screen.busy
                ),
                message="checkout archive action did not complete",
            )
            assert gateway.action_calls[-1] == (
                "project-action",
                "checkout",
                "archive",
                LOCATION_ID,
                "--confirm",
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
            assert "0/0 Inbox sessions" in str(
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
            assert isinstance(app.screen, tui_module.TextEditScreen)
            app.screen.query_one("#edit-value", widgets_module.Input).value = "Phase 4D"
            await pilot.press("enter")
            await _wait_until(
                pilot,
                lambda: not app.is_running,
                message="new-session attach plan did not exit",
            )
            assert gateway.action_calls[:1] == [
                (
                    "new",
                    str(REQUEST_IDS[0]),
                    PROJECT_ID,
                    "Phase 4D",
                    LOCATION_ID,
                    "codex",
                    None,
                    str(REQUEST_IDS[1]),
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


def test_detail_loading_renders_curation_lineage_and_bounded_handoffs() -> None:
    async def exercise() -> None:
        value = _mixed_snapshot().to_dict()
        session = value["sessions"][1]
        session_key = str(session["sessionKey"])
        session.update(
            {
                "purpose": "Finish the terminal curation slice",
                "pinned": True,
                "wrappedAt": NOW_MS - 1_000,
                "latestHandoffId": str(HANDOFF_IDS[0]),
                "continuedFromHandoffId": str(HANDOFF_IDS[1]),
            }
        )
        snapshot = protocol_module.SnapshotEnvelope.from_dict(value)
        detail = _curated_detail(
            snapshot,
            session_key,
            handoffs=(
                (
                    str(HANDOFF_IDS[0]),
                    1,
                    "The vertical slice is ready for review.\nNo transcript needed.",
                    "Run the installed acceptance loop.",
                ),
            ),
        )
        gateway = FakeGateway(retained=snapshot, detail=detail)
        app = _app(gateway)

        async with app.run_test(size=(120, 32)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and app.model.selected_detail is not None,
                message="selected detail did not load",
            )
            table = app.query_one("#sessions", widgets_module.DataTable)
            assert "[pin wrap cont]" in str(table.get_row_at(0)[0])
            rendered = str(app.query_one("#details", widgets_module.Static).content)
            assert "Purpose: Finish the terminal curation slice" in rendered
            assert "Pinned: yes" in rendered
            assert "Wrapped: yes" in rendered
            assert f"Continued from handoff: {HANDOFF_IDS[1]}" in rendered
            assert "The vertical slice is ready for review. No transcript needed." in (
                rendered
            )
            assert "Next: Run the installed acceptance loop." in rendered
            assert gateway.detail_calls == [session_key]

    asyncio.run(exercise())


def test_name_purpose_and_pin_flows_apply_detail_then_refresh_last_good() -> None:
    async def exercise() -> None:
        snapshot = _mixed_snapshot()
        session_key = str(snapshot.sessions[1]["sessionKey"])
        initial = _detail(snapshot, session_key)
        curated = _curated_detail(
            snapshot,
            session_key,
            session_updates={"name": "Curated Claude"},
        )
        cleared = _curated_detail(
            snapshot,
            session_key,
            session_updates={"name": "Curated Claude", "purpose": None},
        )
        pinned = _curated_detail(
            snapshot,
            session_key,
            session_updates={
                "name": "Curated Claude",
                "purpose": None,
                "pinned": True,
            },
        )
        gateway = FakeGateway(
            retained=snapshot,
            detail=initial,
            mutation_detail=[curated, cleared, pinned],
        )
        app = _app(gateway)

        async with app.run_test(size=(110, 30)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and app.model.selected_detail is not None,
                message="initial detail did not load",
            )

            await pilot.press("a")
            assert isinstance(app.screen, tui_module.TextEditScreen)
            app.screen.query_one(
                "#edit-value", widgets_module.Input
            ).value = "  Curated Claude  "
            await pilot.press("enter")
            await _wait_until(
                pilot,
                lambda: len(gateway.action_calls) == 1 and not app.action_busy,
                message="name mutation did not complete",
            )

            await pilot.press("p")
            assert isinstance(app.screen, tui_module.TextEditScreen)
            await pilot.press("ctrl+d")
            await _wait_until(
                pilot,
                lambda: len(gateway.action_calls) == 2 and not app.action_busy,
                message="purpose clear did not complete",
            )

            await pilot.press("v")
            await _wait_until(
                pilot,
                lambda: len(gateway.action_calls) == 3 and not app.action_busy,
                message="pin mutation did not complete",
            )
            assert gateway.action_calls == [
                ("name", session_key, "Curated Claude"),
                ("purpose", session_key, None),
                ("pin", session_key, True),
            ]
            assert gateway.calls == ["none", "none", "none", "none"]
            rendered = str(app.query_one("#details", widgets_module.Static).content)
            assert rendered.startswith("Curated Claude\n")
            assert "Purpose: None" in rendered
            assert "Pinned: yes" in rendered
            assert app.action_message == "Session pinned"

    asyncio.run(exercise())


def test_mutation_detail_wins_an_equal_timestamp_inflight_read() -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        snapshot = _mixed_snapshot()
        session_key = str(snapshot.sessions[1]["sessionKey"])
        old_detail = _detail(snapshot, session_key)
        pinned_detail = _curated_detail(
            snapshot,
            session_key,
            session_updates={"pinned": True},
        )

        class DelayedDetailGateway(FakeGateway):
            async def session_detail(
                self,
                requested_key: str,
                *,
                handoff_limit: int = 20,
            ) -> Any:
                self.detail_calls.append(requested_key)
                started.set()
                await release.wait()
                return old_detail

        gateway = DelayedDetailGateway(
            retained=snapshot,
            mutation_detail=pinned_detail,
        )
        app = _app(gateway)
        async with app.run_test(size=(100, 28)) as pilot:
            await started.wait()
            assert app.model is not None
            await pilot.press("v")
            await _wait_until(
                pilot,
                lambda: not app.action_busy and app.model.selected_detail is not None,
                message="pin mutation did not publish detail",
            )
            release.set()
            await pilot.pause()
            assert app.model.selected_detail.pinned is True
            assert gateway.action_calls == [("pin", session_key, True)]

    asyncio.run(exercise())


def test_handoff_and_wrap_forms_validate_cancel_and_reuse_retry_id() -> None:
    async def exercise() -> None:
        snapshot = _mixed_snapshot()
        session_key = str(snapshot.sessions[1]["sessionKey"])
        failure = gateway_module.GatewayError(
            "command_timeout",
            "The handoff command exceeded its deadline.",
            retryable=True,
        )
        handoff_detail = _curated_detail(
            snapshot,
            session_key,
            handoffs=(
                (
                    str(HANDOFF_IDS[0]),
                    1,
                    "Slice complete",
                    "Run acceptance",
                ),
            ),
        )
        wrap_detail = _curated_detail(
            snapshot,
            session_key,
            session_updates={"wrappedAt": NOW_MS},
            handoffs=(
                (
                    str(HANDOFF_IDS[1]),
                    2,
                    "Work paused",
                    "Resume after review",
                ),
            ),
        )
        gateway = FakeGateway(
            retained=snapshot,
            mutation_detail=[failure, handoff_detail, wrap_detail],
        )
        app = _app(gateway)

        async with app.run_test(size=(110, 30)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and app.model.selected_detail is not None,
                message="initial detail did not load",
            )

            await pilot.press("g")
            assert isinstance(app.screen, tui_module.HandoffEditor)
            await pilot.press("escape")
            await pilot.pause()
            assert gateway.action_calls == []

            await pilot.press("g")
            await pilot.press("ctrl+s")
            assert isinstance(app.screen, tui_module.HandoffEditor)
            assert "must not be empty" in str(
                app.screen.query_one("#handoff-error", widgets_module.Static).content
            )
            app.screen.query_one(
                "#handoff-summary", widgets_module.Input
            ).value = "  Slice complete  "
            app.screen.query_one(
                "#handoff-next-action", widgets_module.Input
            ).value = "Run acceptance"
            await pilot.press("ctrl+s")
            await _wait_until(
                pilot,
                lambda: app.action_error is not None and not app.action_busy,
                message="failed handoff did not remain visible",
            )

            await pilot.press("g")
            assert isinstance(app.screen, tui_module.HandoffEditor)
            assert (
                app.screen.query_one("#handoff-summary", widgets_module.Input).value
                == "Slice complete"
            )
            await pilot.press("ctrl+s")
            await _wait_until(
                pilot,
                lambda: len(gateway.action_calls) == 2 and not app.action_busy,
                message="handoff retry did not complete",
            )

            await pilot.press("w")
            assert isinstance(app.screen, tui_module.HandoffEditor)
            app.screen.query_one(
                "#handoff-summary", widgets_module.Input
            ).value = "Work paused"
            app.screen.query_one(
                "#handoff-next-action", widgets_module.Input
            ).value = "Resume after review"
            await pilot.press("ctrl+s")
            await _wait_until(
                pilot,
                lambda: len(gateway.action_calls) == 3 and not app.action_busy,
                message="wrap did not complete",
            )

            first, retry, wrapped = gateway.action_calls
            assert (
                first
                == retry
                == (
                    "handoff",
                    session_key,
                    str(HANDOFF_IDS[0]),
                    "Slice complete",
                    "Run acceptance",
                )
            )
            assert wrapped == (
                "wrap",
                session_key,
                str(HANDOFF_IDS[1]),
                "Work paused",
                "Resume after review",
            )
            assert app.action_message == "Session wrapped"
            assert "Wrapped: yes" in str(
                app.query_one("#details", widgets_module.Static).content
            )

    asyncio.run(exercise())


def test_detail_reload_failure_preserves_last_good_and_retry_replaces_it() -> None:
    async def exercise() -> None:
        snapshot = _mixed_snapshot()
        session_key = str(snapshot.sessions[1]["sessionKey"])
        first = _curated_detail(
            snapshot,
            session_key,
            generated_at=snapshot.generated_at + 1,
            handoffs=(
                (
                    str(HANDOFF_IDS[0]),
                    1,
                    "Last good detail",
                    "Retry detail loading",
                ),
            ),
        )
        failure = gateway_module.GatewayError(
            "command_timeout",
            "Detail loading timed out.",
            retryable=True,
        )
        replacement = _curated_detail(
            snapshot,
            session_key,
            generated_at=snapshot.generated_at + 2,
            handoffs=(
                (
                    str(HANDOFF_IDS[1]),
                    2,
                    "Replacement detail",
                    "Continue with curation",
                ),
            ),
        )
        gateway = FakeGateway(retained=snapshot, detail=[first, failure, replacement])
        app = _app(gateway)

        async with app.run_test(size=(110, 30)) as pilot:
            await _wait_until(
                pilot,
                lambda: app.model is not None and app.model.selected_detail is not None,
                message="initial detail did not load",
            )
            await pilot.press("d")
            await _wait_until(
                pilot,
                lambda: app.detail_error is not None,
                message="detail failure was not published",
            )
            rendered = str(app.query_one("#details", widgets_module.Static).content)
            assert "Last good detail" in rendered
            assert "DETAIL ERROR command_timeout" in str(
                app.query_one("#status", widgets_module.Static).content
            )

            await pilot.press("d")
            await _wait_until(
                pilot,
                lambda: (
                    app.detail_error is None
                    and app.model.selected_detail.latest_handoff_id
                    == str(HANDOFF_IDS[1])
                ),
                message="detail retry did not replace the cache",
            )
            assert "Replacement detail" in str(
                app.query_one("#details", widgets_module.Static).content
            )
            assert gateway.detail_calls == [session_key, session_key, session_key]

    asyncio.run(exercise())


def test_task_continuation_uses_task_identity_and_terminal_plan_path() -> None:
    async def exercise() -> None:
        context = domain_module.PresentationContext(True, None, False, False)
        snapshot = protocol_module.SnapshotEnvelope.from_dict(_value())
        gateway = FakeGateway(
            retained=snapshot,
            plan=_plan("attach"),
        )
        app = _app(gateway, initial_view="open")
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app._selected_task_id == f"{HOST_ID}:{TASK_ID}",
                message="open task did not become selected",
            )
            await pilot.press("c")
            await _wait_until(
                pilot,
                lambda: not app.is_running,
                message="continuation attach plan did not exit",
            )
            assert gateway.action_calls == [
                (
                    "continue",
                    TASK_ID,
                    None,
                    str(REQUEST_IDS[0]),
                    context,
                ),
                ("attach", SURFACE_ID),
            ]
            assert app.return_value == (
                "/fake/swbctl",
                "attach-surface",
                SURFACE_ID,
            )

    asyncio.run(exercise())


def test_task_close_is_one_action_without_handoff_modal() -> None:
    async def exercise() -> None:
        snapshot = protocol_module.SnapshotEnvelope.from_dict(_value())
        closed = _closed_task_snapshot()
        gateway = FakeGateway(retained=snapshot, full=[closed])
        app = _app(gateway, initial_view="open")
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app._selected_task_id == f"{HOST_ID}:{TASK_ID}",
                message="open task did not become selected",
            )
            await pilot.press("z")
            await _wait_until(
                pilot,
                lambda: (
                    ("close", TASK_ID) in gateway.action_calls and not app.action_busy
                ),
                message="task close did not complete",
            )

            assert not isinstance(app.screen, tui_module.HandoffEditor)
            assert app.action_message == "Task closed; no runtime to stop"
            assert gateway.calls == ["none", "full"]

    asyncio.run(exercise())


def test_closed_task_open_reopens_and_presents_in_one_action() -> None:
    async def exercise() -> None:
        context = domain_module.PresentationContext(True, None, False, False)
        gateway = FakeGateway(retained=_closed_task_snapshot(), plan=_plan("attach"))
        app = _app(gateway, initial_view="closed")
        async with app.run_test(size=(100, 28)) as pilot:
            await _wait_until(
                pilot,
                lambda: app._selected_task_id == f"{HOST_ID}:{TASK_ID}",
                message="closed task did not become selected",
            )
            await pilot.press("o")
            await _wait_until(
                pilot,
                lambda: not app.is_running,
                message="reopen and open did not present the task",
            )

            assert gateway.action_calls == [
                (
                    "reopen-open",
                    TASK_ID,
                    None,
                    str(REQUEST_IDS[0]),
                    context,
                ),
                ("attach", SURFACE_ID),
            ]

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
