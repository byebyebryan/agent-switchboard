from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import agent_switchboard.cli as cli_module
import agent_switchboard.snapshot as snapshot_module
import agent_switchboard.storage as storage_module
from agent_switchboard import __version__
from agent_switchboard.cli import main
from agent_switchboard.protocol import SnapshotEnvelope
from agent_switchboard.storage import Registry

ROOT = Path(__file__).parents[1]
FAKE_CODEX = ROOT / "tests" / "fakes" / "fake_codex.py"
APP_DIR = "agent-switchboard"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"
LOCATION_ID = "33333333-3333-4333-8333-333333333333"


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
        self.config.write_text(value, encoding="utf-8")

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

    plan = tmp_path / "plan.json"
    log = tmp_path / "fake-codex.log"
    plan.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(binary_directory), os.environ.get("PATH", ""))),
    )
    monkeypatch.setenv("FAKE_CODEX_PLAN", str(plan))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
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
    for arguments in (["snapshot"], ["list"]):
        with pytest.raises(SystemExit) as exit_info:
            main(arguments)
        assert exit_info.value.code == 2
        assert "--json" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"swbctl {__version__}\n"


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
    def fail(*, refresh: bool) -> str:
        assert refresh is False
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
    assert snapshot["capabilities"][0]["provider"] == "codex"
    assert snapshot["capabilities"][0]["available"] is True
    assert snapshot["errors"] == []
    assert "claude" not in raw
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
    assert failed["capabilities"][0]["available"] is False
    assert expected_code in {
        item["code"] for item in failed["capabilities"][0]["degradedReasons"]
    }
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
    assert degraded["capabilities"][0]["available"] is False
    assert degraded["capabilities"][0]["degradedReasons"][-1]["code"] == (
        "app_server_malformed_json"
    )
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
        "locations",
        "sessions",
        "runtimes",
        "surfaces",
        "capabilities",
        "errors",
    }
    assert snapshot["sessions"][0]["providerSessionId"] == thread(7)["id"]
    assert snapshot["capabilities"][0]["available"] is True


def test_disabled_codex_is_not_invoked_or_reported(
    cli_environment: CliEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_environment.write_config("[providers.codex]\nenabled = false\n")

    snapshot, _ = run_json(
        capsys,
        ["snapshot", "--reconcile", "full", "--json"],
    )

    assert snapshot["capabilities"] == []
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
[host]
display_name = "starship"

[projects."{PROJECT_ID}"]
name = "Switchboard"
aliases = ["sessions", " router "]
default_provider = "codex"
context_sources = ["AGENTS.md"]

[[projects."{PROJECT_ID}".locations]]
location_id = "{LOCATION_ID}"
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
    assert snapshot["projects"][0]["contextSources"] == ["AGENTS.md"]
    assert snapshot["locations"][0]["locationId"] == LOCATION_ID
    assert snapshot["locations"][0]["path"] == str(checkout.resolve())
    assert snapshot["sessions"][0]["projectId"] == PROJECT_ID
    assert snapshot["sessions"][0]["locationId"] == LOCATION_ID
    assert snapshot["sessions"][0]["metadataSource"] == "location_match"


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
[providers.codex]
enabled = false
[projects."{PROJECT_ID}"]
name = "Switchboard"
[[projects."{PROJECT_ID}".locations]]
location_id = "{LOCATION_ID}"
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
