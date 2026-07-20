from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import agent_switchboard.cli as cli_module
import agent_switchboard.local as local_module
import agent_switchboard.snapshot as snapshot_module
import agent_switchboard.storage as storage_module
from agent_switchboard import __version__
from agent_switchboard.cli import main
from agent_switchboard.domain import HostId, SessionKey
from agent_switchboard.protocol import (
    ErrorRecord,
    ErrorScope,
    FleetEnvelope,
    PresentationPlan,
    PresentationPlanEnvelope,
    PresentationPlanKind,
    SessionDetailEnvelope,
    SnapshotEnvelope,
)
from agent_switchboard.storage import Registry

ROOT = Path(__file__).parents[1]
FAKE_CODEX = ROOT / "tests" / "fakes" / "fake_codex.py"
FAKE_CLAUDE = ROOT / "tests" / "fakes" / "fake_claude.py"
APP_DIR = "agent-switchboard"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"
LOCATION_ID = "33333333-3333-4333-8333-333333333333"
CURATION_HOST_ID = "11111111-1111-4111-8111-111111111111"
CURATION_SESSION_ID = "44444444-4444-4444-8444-444444444444"
CURATION_SESSION_KEY = f"{CURATION_HOST_ID}:codex:{CURATION_SESSION_ID}"
CURATION_HANDOFF_ID = "55555555-5555-4555-8555-555555555555"
CURATION_TASK_ID = "66666666-6666-4666-8666-666666666666"


@dataclass(frozen=True, slots=True)
class CliEnvironment:
    config: Path
    database: Path
    host_id: Path
    executable: Path
    plan: Path
    log: Path

    def write_config(self, value: str) -> None:
        self.config.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if "config_version" in value else "config_version = 2\n"
        self.config.write_text(f"{prefix}{value}", encoding="utf-8")

    def write_plan(self, value: dict[str, Any]) -> None:
        self.plan.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def cli_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliEnvironment:
    config_home = tmp_path / "configuration"
    state_home = tmp_path / "state"
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    executable = binary_directory / "codex"
    executable.symlink_to(FAKE_CODEX.resolve())
    (binary_directory / "claude").symlink_to(FAKE_CLAUDE.resolve())

    plan = tmp_path / "plan.json"
    log = tmp_path / "fake-codex.log"
    plan.write_text("{}", encoding="utf-8")
    claude_plan = tmp_path / "claude-plan.json"
    claude_plan.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(binary_directory), os.environ.get("PATH", ""))),
    )
    monkeypatch.setenv("FAKE_CODEX_PLAN", str(plan))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_PLAN", str(claude_plan))
    claude_settings = tmp_path / "home" / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True)
    claude_settings.write_text('{"disableAgentView":true}', encoding="utf-8")
    # CLI contract tests must never inspect the developer's real /proc or tmux
    # state. Live-reconciliation behavior is covered with isolated fakes.
    monkeypatch.setattr(
        local_module,
        "reconcile_live",
        lambda _registry, _host_id: type("LiveResult", (), {"errors": ()})(),
    )
    return CliEnvironment(
        config=config_home / APP_DIR / "config.toml",
        database=state_home / APP_DIR / "switchboard.db",
        host_id=state_home / APP_DIR / "host-id",
        executable=executable,
        plan=plan,
        log=log,
    )


def thread(
    number: int,
    *,
    name: str = "A safe title",
    cwd: str | None = None,
) -> dict[str, Any]:
    return {
        "id": f"00000000-0000-4000-8000-{number:012d}",
        "cwd": cwd if cwd is not None else f"/work/session-{number}",
        "cliVersion": "0.144.4",
        "createdAt": 100 + number,
        "updatedAt": 200 + number,
        "recencyAt": 300 + number,
        "ephemeral": False,
        "modelProvider": "openai",
        "preview": f"SECRET PREVIEW {number}",
        "sessionId": f"10000000-0000-4000-8000-{number:012d}",
        "source": "cli",
        "status": {"type": "idle"},
        "turns": [{"content": f"SECRET TRANSCRIPT {number}"}],
        "name": name,
        "path": f"/private/provider/history-{number}.jsonl",
        "extra": {"raw": "provider-private"},
    }


def complete_plan(*sessions: dict[str, Any]) -> dict[str, Any]:
    return {"app": {"pages": [[{"result": {"data": list(sessions)}}]]}}


def run_json(
    capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> tuple[dict[str, Any], str]:
    assert main(arguments) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.endswith("\n")
    assert captured.out.count("\n") == 1
    SnapshotEnvelope.from_json(captured.out)
    return json.loads(captured.out), captured.out


def test_parser_requires_json_and_retains_global_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for arguments in (
        ["snapshot"],
        ["list"],
        ["fleet"],
        ["prepare-open", "invalid"],
    ):
        with pytest.raises(SystemExit) as exit_info:
            main(arguments)
        assert exit_info.value.code == 2
        assert "--json" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"swbctl {__version__}\n"


def test_fleet_cli_emits_one_local_host_without_network(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["fleet", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    fleet = FleetEnvelope.from_json(captured.out)
    assert len(fleet.hosts) == 1
    assert fleet.hosts[0].source.value == "local"
    assert fleet.hosts[0].snapshot is not None


def test_tui_command_is_lazy_and_returns_the_frontend_status(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []

    class FakeTui:
        @staticmethod
        def run_tui(*, swbctl_executable: Path) -> int:
            assert swbctl_executable == cli_environment.executable
            return 7

    def import_module(name: str, package: str | None = None) -> FakeTui:
        calls.append((name, package))
        return FakeTui()

    monkeypatch.setattr(cli_module.importlib, "import_module", import_module)
    monkeypatch.setattr(
        cli_module,
        "resolve_swbctl_executable",
        lambda: cli_environment.executable,
    )

    assert main(["tui"]) == 7
    assert capsys.readouterr() == ("", "")
    assert calls == [(".tui", "agent_switchboard")]


@pytest.mark.parametrize("missing_module", ("rich", "textual"))
def test_tui_command_has_an_actionable_missing_extra_error(
    missing_module: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_optional(_name: str, _package: str | None = None) -> None:
        raise ModuleNotFoundError(
            f"No module named {missing_module!r}",
            name=missing_module,
        )

    monkeypatch.setattr(cli_module.importlib, "import_module", missing_optional)

    assert main(["tui"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "swbctl: TUI support is not installed; install it with: "
        "pip install 'agent-switchboard[tui]'\n"
    )
    assert "Traceback" not in captured.err


def test_agent_cli_projects_exact_machine_forms_through_one_service(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class Envelope:
        def __init__(self, value: str) -> None:
            self.value = value

        def to_json(self) -> str:
            return json.dumps({"operation": self.value}, separators=(",", ":"))

    class Service:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            calls.append(("authorize",))

        def current(self) -> Envelope:
            calls.append(("current",))
            return Envelope("current")

        def context(self) -> Envelope:
            calls.append(("context",))
            return Envelope("context")

        def list_tasks(self) -> dict[str, str]:
            calls.append(("tasks",))
            return {"operation": "tasks"}

        def task(self) -> dict[str, str]:
            calls.append(("task",))
            return {"operation": "task"}

        def handoff(self, handoff_id: str) -> Envelope:
            calls.append(("handoff-read", handoff_id))
            return Envelope("handoff-read")

        def list_task_handoffs(self, *, limit: int) -> dict[str, str]:
            calls.append(("handoffs", limit))
            return {"operation": "handoffs"}

        def search(self, query: str, *, limit: int) -> Envelope:
            calls.append(("search", query, limit))
            return Envelope("search")

        def memory_search(self, query: str, *, limit: int) -> Envelope:
            calls.append(("memory", query, limit))
            return Envelope("memory")

        def update_task(self, values: dict[str, object]) -> dict[str, str]:
            calls.append(("update", values))
            return {"operation": "update"}

        def append_handoff(
            self,
            *,
            summary: str,
            next_action: str,
            handoff_id: str | None,
            close: bool,
        ) -> dict[str, str]:
            calls.append(("handoff", summary, next_action, handoff_id, close))
            return {"operation": "close" if close else "handoff"}

    monkeypatch.setattr(cli_module, "AgentToolService", Service)

    assert main(["agent", "current", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"operation": "current"}
    assert main(["agent", "context", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"operation": "context"}
    assert main(["agent", "tasks", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"operation": "tasks"}
    assert main(["agent", "task", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"operation": "task"}
    assert main(["agent", "handoff-read", CURATION_HANDOFF_ID, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"operation": "handoff-read"}
    assert (
        main(
            [
                "agent",
                "handoffs",
                "--limit",
                "7",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"operation": "handoffs"}
    assert main(["agent", "search", "alignment", "--limit", "6", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"operation": "search"}
    assert main(["agent", "memory", "history", "--limit", "5", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"operation": "memory"}
    assert (
        main(
            [
                "agent",
                "update",
                "--title",
                "Agent title",
                "--pin",
                "on",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"operation": "update"}

    payload = {
        "handoffId": CURATION_HANDOFF_ID,
        "summary": "Agent summary.",
        "nextAction": "Agent next action.",
    }
    monkeypatch.setattr("sys.stdin", io.BytesIO(json.dumps(payload).encode()))
    assert main(["agent", "handoff", "--json-stdin", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"operation": "handoff"}
    monkeypatch.setattr("sys.stdin", io.BytesIO(json.dumps(payload).encode()))
    assert main(["agent", "close", "--json-stdin", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"operation": "close"}

    assert calls == [
        ("authorize",),
        ("current",),
        ("authorize",),
        ("context",),
        ("authorize",),
        ("tasks",),
        ("authorize",),
        ("task",),
        ("authorize",),
        ("handoff-read", CURATION_HANDOFF_ID),
        ("authorize",),
        ("handoffs", 7),
        ("authorize",),
        ("search", "alignment", 6),
        ("authorize",),
        ("memory", "history", 5),
        ("authorize",),
        ("update", {"title": "Agent title", "pinned": True}),
        ("authorize",),
        (
            "handoff",
            "Agent summary.",
            "Agent next action.",
            CURATION_HANDOFF_ID,
            False,
        ),
        ("authorize",),
        (
            "handoff",
            "Agent summary.",
            "Agent next action.",
            CURATION_HANDOFF_ID,
            True,
        ),
    ]


def test_agent_cli_authorization_failure_has_no_output_or_capability(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = "private-capability-that-must-not-appear"

    class RejectedService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise cli_module.AgentToolError("agent authorization failed")

    monkeypatch.setattr(cli_module, "AgentToolService", RejectedService)
    monkeypatch.setenv("AGENT_SWITCHBOARD_CAPABILITY", capability)

    assert main(["agent", "current", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "swbctl: agent authorization failed\n"
    assert capability not in captured.err


def test_agent_mcp_cli_uses_the_same_authorized_service(
    cli_environment: CliEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorized = object()
    calls: list[object] = []

    def service(*_args: object, **_kwargs: object) -> object:
        calls.append("authorize")
        return authorized

    def serve(value: object, input_stream: object, output_stream: object) -> int:
        assert value is authorized
        assert input_stream is not None and output_stream is not None
        calls.append("serve")
        return 0

    monkeypatch.setattr(cli_module, "AgentToolService", service)
    monkeypatch.setattr(cli_module, "run_mcp_server", serve)

    assert main(["agent-mcp"]) == 0
    assert calls == ["authorize", "serve"]


def seed_curation_registry(environment: CliEnvironment) -> None:
    with Registry(environment.database) as registry:
        registry.upsert_host(CURATION_HOST_ID, "local", is_local=True, observed_at=1)
        registry.upsert_session(
            {
                "session_key": CURATION_SESSION_KEY,
                "host_id": CURATION_HOST_ID,
                "provider": "codex",
                "provider_session_id": CURATION_SESSION_ID,
                "name": "initial",
                "cwd": "/work/curation",
                "first_observed_at": 2,
                "last_observed_at": 2,
            }
        )


def test_curation_cli_round_trip_and_strict_json_input(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_curation_registry(cli_environment)
    monkeypatch.setattr(
        cli_module, "load_or_create_host_id", lambda: HostId(CURATION_HOST_ID)
    )

    assert main(["show", CURATION_SESSION_KEY, "--json"]) == 0
    detail = SessionDetailEnvelope.from_json(capsys.readouterr().out)
    assert detail.session["name"] == "initial"

    assert (
        main(
            [
                "session",
                "name",
                CURATION_SESSION_KEY,
                "curated title",
                "--json",
            ]
        )
        == 0
    )
    detail = SessionDetailEnvelope.from_json(capsys.readouterr().out)
    assert detail.session["name"] == "curated title"

    assert (
        main(
            [
                "session",
                "purpose",
                CURATION_SESSION_KEY,
                "Finish Phase 4B",
                "--json",
            ]
        )
        == 0
    )
    detail = SessionDetailEnvelope.from_json(capsys.readouterr().out)
    assert detail.session["purpose"] == "Finish Phase 4B"

    assert main(["session", "pin", CURATION_SESSION_KEY, "--json"]) == 0
    detail = SessionDetailEnvelope.from_json(capsys.readouterr().out)
    assert detail.session["pinned"] is True

    monkeypatch.setattr(
        "sys.stdin",
        io.BytesIO(
            json.dumps(
                {
                    "handoffId": CURATION_HANDOFF_ID,
                    "summary": "Core curation is complete.",
                    "nextAction": "Implement continuation.",
                }
            ).encode()
        ),
    )
    assert (
        main(["session", "wrap", CURATION_SESSION_KEY, "--json-stdin", "--json"]) == 0
    )
    detail = SessionDetailEnvelope.from_json(capsys.readouterr().out)
    assert detail.session["wrappedAt"] is not None
    assert detail.session["latestHandoffId"] == CURATION_HANDOFF_ID
    assert detail.handoffs[0]["source"] == "user"

    with Registry(cli_environment.database) as registry:
        stored = registry.get_session(CURATION_SESSION_KEY)
        assert stored is not None
        assert stored["last_observed_at"] == 2


def test_curation_cli_current_and_fail_closed_argument_shapes(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_curation_registry(cli_environment)
    key = SessionKey.parse(CURATION_SESSION_KEY)
    monkeypatch.setattr(
        cli_module, "load_or_create_host_id", lambda: HostId(CURATION_HOST_ID)
    )
    monkeypatch.setattr(cli_module, "resolve_current_session_key", lambda *a, **k: key)

    assert main(["current", "--json"]) == 0
    SessionDetailEnvelope.from_json(capsys.readouterr().out)
    assert main(["session", "name", "--current", "current title", "--json"]) == 0
    detail = SessionDetailEnvelope.from_json(capsys.readouterr().out)
    assert detail.session["name"] == "current title"
    assert main(["session", "name", "--current", "--clear", "--json"]) == 0
    detail = SessionDetailEnvelope.from_json(capsys.readouterr().out)
    assert detail.session.get("name") is None

    assert main(["session", "pin", CURATION_SESSION_KEY, "--current"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "choose exactly one" in captured.err
    assert main(["session", "purpose", CURATION_SESSION_KEY]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "requires SESSION_KEY and one value" in captured.err


def test_prepare_open_emits_one_presentation_envelope_and_context_flags(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    session_key = SessionKey.parse(
        "11111111-1111-4111-8111-111111111111:codex:"
        "22222222-2222-4222-8222-222222222222"
    )
    request_id = "33333333-3333-4333-8333-333333333333"
    observed: list[argparse.Namespace] = []
    plan = PresentationPlan(
        PresentationPlanKind.BLOCKED,
        HostId("11111111-1111-4111-8111-111111111111"),
        error=ErrorRecord(
            "unmanaged_surface",
            "No managed surface.",
            ErrorScope.SESSION,
            False,
            1,
            session_key=session_key,
        ),
    )

    def prepare(arguments):
        observed.append(arguments)
        return PresentationPlanEnvelope(plan).to_json()

    monkeypatch.setattr(cli_module, "_prepare_open", prepare)

    assert (
        main(
            [
                "prepare-open",
                str(session_key),
                "--request-id",
                request_id,
                "--can-focus-desktop",
                "--can-launch-terminal",
                "--json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    parsed = PresentationPlanEnvelope.from_json(captured.out).plan
    assert parsed.kind is PresentationPlanKind.BLOCKED
    assert observed[0].session_key == str(session_key)
    assert observed[0].request_id == request_id
    assert observed[0].can_focus_desktop
    assert observed[0].can_launch_terminal


def test_prepare_task_create_emits_one_presentation_envelope_and_context_flags(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = "44444444-4444-4444-8444-444444444444"
    observed: list[argparse.Namespace] = []
    plan = PresentationPlan(
        PresentationPlanKind.BLOCKED,
        HostId("11111111-1111-4111-8111-111111111111"),
        error=ErrorRecord(
            "project_checkout_missing",
            "No local checkout.",
            ErrorScope.PROJECT,
            False,
            1,
            host_id=HostId("11111111-1111-4111-8111-111111111111"),
            provider="codex",
        ),
    )

    def prepare(arguments):
        observed.append(arguments)
        return PresentationPlanEnvelope(plan).to_json()

    monkeypatch.setattr(cli_module, "_prepare_task", prepare)

    assert (
        main(
            [
                "prepare-task",
                CURATION_TASK_ID,
                "--create",
                "--project",
                PROJECT_ID,
                "--title",
                "Implement the task contract",
                "--checkout",
                LOCATION_ID,
                "--provider",
                "claude",
                "--request-id",
                request_id,
                "--can-focus-desktop",
                "--can-launch-terminal",
                "--json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    parsed = PresentationPlanEnvelope.from_json(captured.out).plan
    assert parsed.kind is PresentationPlanKind.BLOCKED
    assert observed[0].project == PROJECT_ID
    assert observed[0].checkout == LOCATION_ID
    assert observed[0].provider == "claude"
    assert observed[0].request_id == request_id
    assert observed[0].can_focus_desktop
    assert observed[0].can_launch_terminal
    assert observed[0].task_id == CURATION_TASK_ID
    assert observed[0].create
    assert observed[0].title == "Implement the task contract"


def test_prepare_task_accepts_an_existing_task_without_project_flags(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[argparse.Namespace] = []
    request_id = "66666666-6666-4666-8666-666666666666"
    plan = PresentationPlan(
        PresentationPlanKind.BLOCKED,
        HostId(CURATION_HOST_ID),
        error=ErrorRecord(
            "continuation_handoff_not_found",
            "No retained handoff.",
            ErrorScope.LAUNCH,
            False,
            1,
            host_id=HostId(CURATION_HOST_ID),
        ),
    )

    def prepare(arguments: argparse.Namespace) -> str:
        observed.append(arguments)
        return PresentationPlanEnvelope(plan).to_json()

    monkeypatch.setattr(cli_module, "_prepare_task", prepare)
    assert (
        main(
            [
                "prepare-task",
                CURATION_TASK_ID,
                "--request-id",
                request_id,
                "--can-launch-terminal",
                "--json",
            ]
        )
        == 0
    )
    PresentationPlanEnvelope.from_json(capsys.readouterr().out)
    assert observed[0].task_id == CURATION_TASK_ID
    assert observed[0].project is None
    assert not observed[0].create


def test_surface_action_commands_are_quiet_and_fail_safely(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    surface_id = "44444444-4444-4444-8444-444444444444"
    observed: list[tuple[str, str | None]] = []

    def act(arguments):
        observed.append((arguments.command, getattr(arguments, "client", None)))
        return 0

    monkeypatch.setattr(cli_module, "_surface_action", act)
    assert main(["select-surface", surface_id, "--client", "/dev/pts/8"]) == 0
    assert main(["attach-surface", surface_id]) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert observed == [
        ("select-surface", "/dev/pts/8"),
        ("attach-surface", None),
    ]

    monkeypatch.setattr(
        cli_module,
        "_surface_action",
        lambda _arguments: (_ for _ in ()).throw(OSError("first\n\x1bsecond")),
    )
    assert main(["attach-surface", surface_id]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "swbctl: first second\n"


def test_cli_composes_exact_reconciliation_modes(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[str] = []

    def build(*, reconcile: str) -> str:
        observed.append(reconcile)
        return "{}"

    monkeypatch.setattr(cli_module, "build_local_snapshot_json", build)
    commands = (
        (["snapshot", "--reconcile", "none", "--json"], "none"),
        (["snapshot", "--reconcile", "live", "--json"], "live"),
        (["snapshot", "--reconcile", "full", "--json"], "full"),
        (["list", "--json"], "none"),
        (["list", "--refresh", "--json"], "full"),
    )
    for argv, expected in commands:
        assert main(argv) == 0
        assert observed[-1] == expected
        assert capsys.readouterr().out == "{}\n"


def test_event_ingests_stdin_without_stdout_or_provider_io(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "session_id": "11111111-1111-4111-8111-111111111111",
        "cwd": "/work/session",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "prompt": "SECRET prompt",
        "transcript_path": "/private/SECRET-transcript.jsonl",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert main(["event", "--provider", "codex"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert not cli_environment.log.exists()
    with Registry(cli_environment.database) as registry:
        session = registry.list_sessions()[0]
        assert session["runtime_presence"] == "live"
        assert session["activity"] == "ready"
        assert (
            registry.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            == 1
        )
    assert b"SECRET" not in cli_environment.database.read_bytes()


def test_event_failure_has_no_stdout_and_bounded_stderr(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO('{"prompt":"SECRET"}'))

    assert main(["event", "--provider", "codex"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "SECRET" not in captured.err
    assert not cli_environment.log.exists()
    assert not cli_environment.database.exists()


def test_event_write_lock_fails_within_hook_latency_budget(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "11111111-1111-4111-8111-111111111111"
    start = {
        "session_id": session_id,
        "cwd": "/work/session",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(start)))
    assert main(["event", "--provider", "codex"]) == 0
    capsys.readouterr()

    stop = {
        "session_id": session_id,
        "cwd": "/work/session",
        "hook_event_name": "Stop",
        "turn_id": "turn-under-lock",
    }
    with Registry(cli_environment.database) as writer:
        writer.connection.execute("BEGIN IMMEDIATE")
        try:
            monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(stop)))
            started = time.monotonic()
            result = main(["event", "--provider", "codex"])
            elapsed = time.monotonic() - started
        finally:
            writer.connection.execute("ROLLBACK")

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "locked" in captured.err.lower()
    assert elapsed < 0.9


def test_claude_event_cli_registers_one_privacy_safe_session(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "77777777-7777-4777-8777-777777777777"
    start = {
        "session_id": session_id,
        "cwd": "/work/claude-session",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "prompt": "SECRET prompt",
        "transcript_path": "/private/SECRET.jsonl",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(start)))

    assert main(["event", "--provider", "claude"]) == 0
    assert capsys.readouterr().out == ""
    with Registry(cli_environment.database) as registry:
        sessions = [
            session
            for session in registry.list_sessions()
            if session["provider"] == "claude"
        ]
    assert len(sessions) == 1
    assert sessions[0]["provider_session_id"] == session_id
    assert b"SECRET" not in cli_environment.database.read_bytes()


def test_cli_has_no_config_override_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["snapshot", "--json", "--config", "/tmp/config.toml"])

    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unrecognized arguments: --config" in captured.err


def test_core_error_diagnostic_is_bounded_and_single_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*, reconcile: str) -> str:
        assert reconcile == "none"
        raise OSError(f"first line\n\x1bsecond line {'x' * 2_000}")

    monkeypatch.setattr(cli_module, "build_local_snapshot_json", fail)

    assert main(["list", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "\x1b" not in captured.err
    assert len(captured.err) <= len("swbctl: ") + 1_024 + 1


def test_missing_implicit_config_bootstraps_defaults_without_provider_io(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, _ = run_json(capsys, ["list", "--json"])

    assert not cli_environment.config.exists()
    assert cli_environment.database.is_file()
    assert cli_environment.host_id.is_file()
    assert (
        cli_environment.host_id.read_text(encoding="ascii").strip()
        == first["host"]["hostId"]
    )
    assert first["sessions"] == []
    assert first["capabilities"] == []
    assert first["errors"] == []
    assert not cli_environment.log.exists()

    with Registry(cli_environment.database) as registry:
        before = registry.get_host(first["host"]["hostId"])
    assert before is not None
    monkeypatch.setattr(storage_module, "now_ms", lambda: 9_999_999_999_999)

    second, _ = run_json(capsys, ["list", "--json"])
    with Registry(cli_environment.database) as registry:
        after = registry.get_host(second["host"]["hostId"])

    assert after is not None
    assert after["updated_at"] == before["updated_at"]
    assert not cli_environment.log.exists()


def test_existing_no_refresh_ignores_invalid_config_without_writes_or_provider_io(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, _ = run_json(capsys, ["list", "--json"])
    with Registry(cli_environment.database) as registry:
        before = registry.get_host(first["host"]["hostId"])
    assert before is not None

    cli_environment.write_config("[providers.codex]\nunknown = true\n")
    monkeypatch.setattr(storage_module, "now_ms", lambda: 9_999_999_999_999)

    snapshot, _ = run_json(capsys, ["snapshot", "--json"])
    listed, _ = run_json(capsys, ["list", "--json"])
    with Registry(cli_environment.database) as registry:
        after = registry.get_host(first["host"]["hostId"])

    assert snapshot["host"] == first["host"]
    assert listed["host"] == first["host"]
    assert after is not None
    assert (after["created_at"], after["updated_at"]) == (
        before["created_at"],
        before["updated_at"],
    )
    assert not cli_environment.log.exists()


def test_full_reconcile_paginates_and_emits_one_private_safe_snapshot(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_environment.write_plan(
        {
            "app": {
                "pages": [
                    [
                        {
                            "result": {
                                "data": [thread(1)],
                                "nextCursor": "opaque-next",
                            }
                        }
                    ],
                    [{"result": {"data": [thread(2)], "nextCursor": None}}],
                ]
            }
        }
    )

    snapshot, raw = run_json(
        capsys,
        ["snapshot", "--reconcile", "full", "--json"],
    )

    assert [item["providerSessionId"] for item in snapshot["sessions"]] == [
        thread(1)["id"],
        thread(2)["id"],
    ]
    assert {item["provider"] for item in snapshot["capabilities"]} == {
        "claude",
        "codex",
    }
    codex = next(
        item for item in snapshot["capabilities"] if item["provider"] == "codex"
    )
    assert codex["available"] is True
    assert snapshot["errors"] == []
    for private_value in (
        "SECRET PREVIEW",
        "SECRET TRANSCRIPT",
        "provider-private",
        "/private/provider/history",
    ):
        assert private_value not in raw


def test_repeated_full_reconcile_is_idempotent(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_environment.write_plan(complete_plan(thread(1), thread(2)))

    first, _ = run_json(
        capsys,
        ["snapshot", "--reconcile", "full", "--json"],
    )
    second, _ = run_json(
        capsys,
        ["snapshot", "--reconcile", "full", "--json"],
    )

    assert [item["sessionKey"] for item in second["sessions"]] == [
        item["sessionKey"] for item in first["sessions"]
    ]
    with Registry(cli_environment.database) as registry:
        assert len(registry.list_sessions(host_id=first["host"]["hostId"])) == 2


def test_live_reconcile_skips_app_server_and_full_runs_live_after_degradation(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_environment.write_plan(complete_plan(thread(1)))
    run_json(capsys, ["snapshot", "--reconcile", "full", "--json"])
    provider_log = cli_environment.log.read_text(encoding="utf-8")
    calls: list[str] = []

    def live(_registry: Registry, host_id: str):
        calls.append(host_id)
        return type("LiveResult", (), {"errors": ()})()

    monkeypatch.setattr(local_module, "reconcile_live", live)
    run_json(capsys, ["snapshot", "--reconcile", "live", "--json"])
    assert len(calls) == 1
    assert cli_environment.log.read_text(encoding="utf-8") == provider_log

    missing = cli_environment.executable.parent / "missing-codex"
    cli_environment.write_config(f'[providers.codex]\nexecutable = "{missing}"\n')
    degraded, _ = run_json(capsys, ["snapshot", "--reconcile", "full", "--json"])
    assert len(calls) == 2
    assert degraded["errors"][-1]["code"] == "provider_not_found"


@pytest.mark.parametrize("failure", ["mid-pagination", "provider-not-found"])
def test_provider_failure_retains_rows_and_exits_successfully(
    failure: str,
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_environment.write_plan(complete_plan(thread(1)))
    seeded, _ = run_json(
        capsys,
        ["snapshot", "--reconcile", "full", "--json"],
    )

    expected_code: str
    if failure == "mid-pagination":
        cli_environment.write_plan(
            {
                "app": {
                    "pages": [
                        [
                            {
                                "result": {
                                    "data": [thread(2)],
                                    "nextCursor": "more",
                                }
                            }
                        ],
                        [
                            {
                                "error": {
                                    "code": -32000,
                                    "message": "SECRET provider failure payload",
                                }
                            }
                        ],
                    ]
                }
            }
        )
        expected_code = "app_server_rpc_error"
    else:
        missing = cli_environment.executable.parent / "missing-codex"
        cli_environment.write_config(f'[providers.codex]\nexecutable = "{missing}"\n')
        expected_code = "provider_not_found"

    failed, raw = run_json(
        capsys,
        ["snapshot", "--reconcile", "full", "--json"],
    )

    assert [item["sessionKey"] for item in failed["sessions"]] == [
        item["sessionKey"] for item in seeded["sessions"]
    ]
    assert failed["sessions"][0]["resumability"] == "resumable"
    codex = next(item for item in failed["capabilities"] if item["provider"] == "codex")
    assert codex["available"] is False
    assert expected_code in {item["code"] for item in codex["degradedReasons"]}
    assert [item["code"] for item in failed["errors"]][-1] == expected_code
    assert "SECRET provider failure payload" not in raw


def test_oversized_provider_integer_is_structured_and_retains_rows(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_environment.write_plan(complete_plan(thread(1)))
    seeded, _ = run_json(
        capsys,
        ["snapshot", "--reconcile", "full", "--json"],
    )
    private_marker = "SECRET OVERSIZED PROVIDER PAYLOAD"
    cli_environment.write_plan(
        {
            "app": {
                "pages": [
                    [
                        {
                            "raw": (
                                '{"id":1,"result":{"data":[],'
                                f'"private":"{private_marker}","oversized":'
                                + "1" * 5_000
                                + "}}\n"
                            )
                        }
                    ]
                ]
            }
        }
    )

    degraded, raw = run_json(
        capsys,
        ["snapshot", "--reconcile", "full", "--json"],
    )

    assert [item["sessionKey"] for item in degraded["sessions"]] == [
        item["sessionKey"] for item in seeded["sessions"]
    ]
    codex = next(
        item for item in degraded["capabilities"] if item["provider"] == "codex"
    )
    assert codex["available"] is False
    assert codex["degradedReasons"][-1]["code"] == ("app_server_malformed_json")
    assert degraded["errors"][-1]["code"] == "app_server_malformed_json"
    assert private_marker not in raw


def test_no_refresh_commands_reuse_retained_state_without_provider_io(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_environment.write_plan(complete_plan(thread(1)))
    seeded, _ = run_json(
        capsys,
        ["snapshot", "--reconcile", "full", "--json"],
    )
    provider_log = cli_environment.log.read_text(encoding="utf-8")

    snapshot, _ = run_json(
        capsys,
        ["snapshot", "--reconcile", "none", "--json"],
    )
    listed, _ = run_json(capsys, ["list", "--json"])

    for retained in (snapshot, listed):
        assert [item["sessionKey"] for item in retained["sessions"]] == [
            item["sessionKey"] for item in seeded["sessions"]
        ]
        assert retained["capabilities"] == []
        assert retained["errors"] == []
    assert cli_environment.log.read_text(encoding="utf-8") == provider_log


def test_no_refresh_cli_reports_snapshot_session_truncation_without_data_loss(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_environment.write_plan(complete_plan(thread(1)))
    seeded, _ = run_json(
        capsys,
        ["snapshot", "--reconcile", "full", "--json"],
    )
    monkeypatch.setattr(snapshot_module, "_SNAPSHOT_SESSION_BYTE_BUDGET", 2)

    truncated, _ = run_json(capsys, ["list", "--json"])

    assert seeded["sessions"]
    assert truncated["sessions"] == []
    assert truncated["errors"][-1]["code"] == "snapshot_sessions_truncated"
    assert truncated["errors"][-1]["details"] == {
        "emittedCount": 0,
        "retainedCount": 1,
    }
    with Registry(cli_environment.database) as registry:
        assert len(registry.list_sessions(host_id=seeded["host"]["hostId"])) == 1


def test_list_refresh_uses_the_snapshot_envelope_and_refreshes_codex(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_environment.write_plan(complete_plan(thread(7)))

    snapshot, _ = run_json(capsys, ["list", "--refresh", "--json"])

    assert set(snapshot) == {
        "schemaVersion",
        "protocolVersion",
        "generatedAt",
        "host",
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
    }
    assert snapshot["sessions"][0]["providerSessionId"] == thread(7)["id"]
    codex = next(
        item for item in snapshot["capabilities"] if item["provider"] == "codex"
    )
    assert codex["available"] is True


def test_disabled_codex_is_not_invoked_or_reported(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_environment.write_config("[providers.codex]\nenabled = false\n")

    snapshot, _ = run_json(
        capsys,
        ["snapshot", "--reconcile", "full", "--json"],
    )

    assert [item["provider"] for item in snapshot["capabilities"]] == ["claude"]
    assert snapshot["capabilities"][0]["available"] is True
    assert snapshot["errors"] == []
    assert not cli_environment.log.exists()


def test_refresh_materializes_configured_projects(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    session_cwd = checkout / "nested" / "src"
    session_cwd.mkdir(parents=True)
    cli_environment.write_plan(complete_plan(thread(7, cwd=str(session_cwd))))
    cli_environment.write_config(
        f'''
config_version = 2
[host]
display_name = "starship"

[projects."{PROJECT_ID}"]
name = "Switchboard"
aliases = ["sessions", " router "]
default_provider = "codex"

[[projects."{PROJECT_ID}".repositories]]
repository_id = "{PROJECT_ID}"
name = "agent-switchboard"
is_primary = true
context_sources = ["AGENTS.md"]

[[projects."{PROJECT_ID}".repositories.checkouts]]
checkout_id = "{LOCATION_ID}"
path = "{checkout}"
display_name = "main checkout"
is_default = true
'''
    )

    snapshot, _ = run_json(
        capsys,
        ["snapshot", "--reconcile", "full", "--json"],
    )

    assert snapshot["host"]["displayName"] == "starship"
    assert snapshot["projects"][0]["projectId"] == PROJECT_ID
    assert snapshot["projects"][0]["aliases"] == ["router", "sessions"]
    assert snapshot["repositories"][0]["contextSources"] == ["AGENTS.md"]
    assert snapshot["checkouts"][0]["checkoutId"] == LOCATION_ID
    assert snapshot["checkouts"][0]["path"] == str(checkout.resolve())
    assert snapshot["sessions"][0]["projectId"] == PROJECT_ID
    assert snapshot["sessions"][0]["checkoutId"] == LOCATION_ID
    assert snapshot["sessions"][0]["metadataSource"] == "checkout_match"


@pytest.mark.parametrize(
    "invalid_kind",
    ["invalid-toml", "oversized-integer", "unreadable-path"],
)
def test_invalid_implicit_config_is_a_safe_core_failure(
    invalid_kind: str,
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_environment.config.parent.mkdir(parents=True, exist_ok=True)
    if invalid_kind == "invalid-toml":
        cli_environment.config.write_text(
            "[providers.codex]\nunknown = true\n",
            encoding="utf-8",
        )
    elif invalid_kind == "oversized-integer":
        cli_environment.config.write_text(
            "[defaults]\nrefresh_interval_seconds = " + "1" * 5_000,
            encoding="utf-8",
        )
    else:
        cli_environment.config.mkdir()

    assert main(["snapshot", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("swbctl: ")
    assert captured.err.count("\n") == 1
    assert "Traceback" not in captured.err
    assert not cli_environment.database.exists()


def test_storage_failure_has_no_json_or_traceback(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_environment.database.mkdir(parents=True)

    assert main(["list", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("swbctl: ")
    assert captured.err.count("\n") == 1
    assert "Traceback" not in captured.err
    assert not cli_environment.log.exists()


def test_protocol_failure_has_no_partial_json(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "protocol-checkout"
    checkout.mkdir()
    cli_environment.write_config(
        f'''
config_version = 2
[providers.codex]
enabled = false
[projects."{PROJECT_ID}"]
name = "Switchboard"
[[projects."{PROJECT_ID}".repositories]]
repository_id = "{PROJECT_ID}"
name = "agent-switchboard"
is_primary = true
[[projects."{PROJECT_ID}".repositories.checkouts]]
checkout_id = "{LOCATION_ID}"
path = "{checkout}"
'''
    )
    run_json(capsys, ["list", "--json"])
    with sqlite3.connect(cli_environment.database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE projects SET aliases_json = '{}' WHERE project_id = ?",
            (PROJECT_ID,),
        )

    assert main(["list", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("swbctl: stored project aliases_json")
    assert captured.err.count("\n") == 1
    assert "Traceback" not in captured.err
