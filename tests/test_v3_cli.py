from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from test_v3_cutover import (
    GENERATION,
    GENERATION_2,
    HOST,
    PROJECT,
    SESSION_KEY,
    cutover_evidence,
    export_legacy,
    roots,
    seeded_legacy,
)

from agent_switchboard._v3.cli import main as v3_main
from agent_switchboard._v3.config import render_config
from agent_switchboard._v3.domain import ProviderId, ViewId, ViewMode
from agent_switchboard._v3.generation import GenerationError
from agent_switchboard._v3.hook_config import STATUS_MESSAGE
from agent_switchboard._v3.provider_runtime import ProviderContract
from agent_switchboard._v3.tmux_view import ROLE_SURFACE, TmuxExecutor

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")


def test_global_hook_noops_only_outside_managed_authority(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AGENT_SWITCHBOARD_CAPABILITY", raising=False)
    monkeypatch.delenv("SWB_V3_SESSION_KEY", raising=False)
    monkeypatch.delenv("SWB_V3_GENERATION_ID", raising=False)

    assert v3_main(["hook", "--provider", "codex"]) == 0
    assert capsys.readouterr() == ("", "")

    monkeypatch.setenv("AGENT_SWITCHBOARD_CAPABILITY", "opaque")
    assert v3_main(["hook", "--provider", "codex"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "swbctl: incomplete managed hook authority\n"

    monkeypatch.setenv("AGENT_SWITCHBOARD_CAPABILITY", "legacy-opaque")
    monkeypatch.setenv(
        "SWB_V3_SESSION_KEY",
        "040f6a81-67b6-42ce-b7ca-2068bb190e88:codex:"
        "019f6a67-a897-7661-97c5-41ca255d1284",
    )
    assert v3_main(["hook", "--provider", "codex"]) == 0
    assert capsys.readouterr() == ("", "")

    monkeypatch.setenv("SWB_V3_GENERATION_ID", str(GENERATION))
    monkeypatch.setattr(
        "agent_switchboard._v3.cli.resolve_current",
        lambda _paths: GENERATION_2,
    )
    assert v3_main(["hook", "--provider", "codex"]) == 0
    assert capsys.readouterr() == ("", "")

    def missing_state(_paths: object) -> None:
        raise GenerationError("generation_missing", "state was discarded")

    monkeypatch.setattr("agent_switchboard._v3.cli.resolve_current", missing_state)
    assert v3_main(["hook", "--provider", "codex"]) == 0
    assert capsys.readouterr() == ("", "")

    monkeypatch.delenv("AGENT_SWITCHBOARD_CAPABILITY")
    monkeypatch.setenv(
        "SWB_V3_SESSION_KEY",
        "040f6a81-67b6-42ce-b7ca-2068bb190e88:codex:"
        "019f6a67-a897-7661-97c5-41ca255d1284",
    )
    assert v3_main(["hook", "--provider", "codex"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "swbctl: incomplete managed hook authority\n"


def test_managed_hook_rejection_writes_safe_feedback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_SWITCHBOARD_CAPABILITY", "opaque")
    monkeypatch.setenv(
        "SWB_V3_SESSION_KEY",
        "040f6a81-67b6-42ce-b7ca-2068bb190e88:codex:"
        "019f6a67-a897-7661-97c5-41ca255d1284",
    )
    monkeypatch.setenv("SWB_V3_GENERATION_ID", str(GENERATION))
    monkeypatch.setattr(
        "agent_switchboard._v3.cli.resolve_current", lambda _paths: GENERATION
    )
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"not-json")))

    assert v3_main(["hook", "--provider", "codex"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "swbctl: managed hook event rejected\n"


def test_fresh_cli_init_reset_and_opt_in_hooks_are_self_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database, legacy = seeded_legacy(tmp_path)
    template = export_legacy(database, legacy, exported_at=100).target_config(
        GENERATION
    )
    template_path = tmp_path / "template.toml"
    template_path.write_text(
        render_config(template).replace(f'generation_id = "{GENERATION}"\n', ""),
        encoding="utf-8",
    )
    paths = roots(tmp_path)
    base = [
        "--config-root",
        str(paths.config_root),
        "--state-root",
        str(paths.state_root),
    ]
    assert (
        v3_main(
            [
                *base,
                "init",
                "--config",
                str(template_path),
                "--generation-id",
                str(GENERATION),
                "--at",
                "101",
            ]
        )
        == 0
    )
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["sourceKind"] == "fresh"
    assert initialized["generationId"] == str(GENERATION)
    assert initialized["previousGenerationId"] is None

    command = tmp_path / "swbctl"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o700)
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert (
        v3_main(
            [
                *base,
                "hooks",
                "install",
                "--provider",
                "codex",
                "--executable",
                str(command),
            ]
        )
        == 0
    )
    installed = json.loads(capsys.readouterr().out)
    assert installed["installedHandlers"] == 5
    hook_document = json.loads((codex_home / "hooks.json").read_text())
    owned_hooks = [
        handler
        for groups in hook_document["hooks"].values()
        for group in groups
        for handler in group["hooks"]
        if handler.get("statusMessage") == STATUS_MESSAGE
    ]
    assert len(owned_hooks) == 5
    assert {handler["timeout"] for handler in owned_hooks} == {10}

    assert (
        v3_main(
            [
                *base,
                "reset",
                "--confirm-generation",
                str(GENERATION),
                "--generation-id",
                "88888888-8888-4888-8888-888888888888",
                "--at",
                "102",
            ]
        )
        == 0
    )
    replaced = json.loads(capsys.readouterr().out)
    assert replaced["sourceKind"] == "fresh"
    assert replaced["previousGenerationId"] == str(GENERATION)
    assert v3_main([*base, "state", "host", "--json", "--at", "103"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["frames"] == []
    assert state["sessions"] == []


def test_fresh_cli_ssh_attach_and_reset_leave_existing_tmux_view_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database, legacy = seeded_legacy(tmp_path)
    template = export_legacy(database, legacy, exported_at=100).target_config(
        GENERATION
    )
    template_path = tmp_path / "template.toml"
    template_path.write_text(render_config(template), encoding="utf-8")
    paths = roots(tmp_path)
    base = [
        "--config-root",
        str(paths.config_root),
        "--state-root",
        str(paths.state_root),
    ]
    socket = tmp_path / "fresh-tmux.sock"
    monkeypatch.setenv("SWB_V3_TMUX_SOCKET", str(socket))
    tmux = TmuxExecutor(socket)
    try:
        assert (
            v3_main(
                [
                    *base,
                    "init",
                    "--config",
                    str(template_path),
                    "--generation-id",
                    str(GENERATION),
                    "--at",
                    "101",
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert (
            v3_main(
                [
                    *base,
                    "view",
                    "open",
                    "--host",
                    HOST,
                    "--project",
                    PROJECT,
                    "--request-id",
                    "aaaaaaaa-3131-4131-8131-313131313131",
                    "--json",
                    "--at",
                    "102",
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert v3_main([*base, "view", "list", "--at", "102"]) == 0
        views = json.loads(capsys.readouterr().out)
        view_id = ViewId(views[0]["viewId"])
        assert v3_main([*base, "doctor", "--json"]) == 0
        doctor = json.loads(capsys.readouterr().out)
        assert doctor["viewHealth"] == {
            "status": "healthy",
            "checkedViews": 1,
            "degradedViews": [],
            "warnings": [],
        }
        before = tmux.inspect_shell("agent", GENERATION, view_id, ViewMode.NAVIGATOR)

        class Attached(RuntimeError):
            pass

        captured: list[str] = []

        def capture_exec(_executable: str, argv: tuple[str, ...]) -> None:
            captured.extend(argv)
            raise Attached

        monkeypatch.setattr("os.execvp", capture_exec)
        with pytest.raises(Attached):
            v3_main(
                [
                    *base,
                    "view",
                    "attach",
                    "--view",
                    str(view_id),
                    "--at",
                    "103",
                ]
            )
        assert captured[-1] == f"{tmux.names('agent', view_id).view_session}:main"

        assert (
            v3_main(
                [
                    *base,
                    "reset",
                    "--confirm-generation",
                    str(GENERATION),
                    "--generation-id",
                    "88888888-8888-4888-8888-888888888888",
                    "--at",
                    "104",
                ]
            )
            == 0
        )
        capsys.readouterr()
        after = tmux.inspect_shell("agent", GENERATION, view_id, ViewMode.NAVIGATOR)
        assert after.active.pane_id == before.active.pane_id
        assert after == before
    finally:
        tmux.run("kill-server", check=False)


def test_private_cli_runs_staged_reads_then_committed_view_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database, config = seeded_legacy(tmp_path)
    bundle = export_legacy(database, config, exported_at=100)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(bundle.to_json(), encoding="utf-8")
    paths = roots(tmp_path)
    base = [
        "--config-root",
        str(paths.config_root),
        "--state-root",
        str(paths.state_root),
    ]
    socket = tmp_path / "tmux.sock"
    monkeypatch.setenv("SWB_V3_TMUX_SOCKET", str(socket))
    tmux = TmuxExecutor(socket)
    try:
        assert (
            v3_main(
                [
                    *base,
                    "cutover",
                    "import",
                    "--bundle",
                    str(bundle_path),
                    "--generation-id",
                    str(GENERATION),
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert v3_main([*base, "state", "navigator", "--at", "101"]) == 0
        staged = json.loads(capsys.readouterr().out)
        assert staged["hosts"][0]["activationState"] == "cutover_staged"
        assert (
            v3_main(
                [
                    *base,
                    "view",
                    "open",
                    "--host",
                    HOST,
                    "--project",
                    PROJECT,
                    "--request-id",
                    "aaaaaaaa-0000-4000-8000-000000000001",
                    "--can-launch-terminal",
                    "--json",
                    "--at",
                    "102",
                ]
            )
            == 2
        )
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "cutover_staged"
        assert (
            v3_main(
                [
                    *base,
                    "hooks",
                    "install",
                    "--provider",
                    "codex",
                    "--executable",
                    "/bin/true",
                    "--dry-run",
                ]
            )
            == 2
        )
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "cutover_staged"

        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_bytes(
            cutover_evidence(GENERATION, captured_at=102).to_json()
        )
        assert (
            v3_main(
                [
                    *base,
                    "cutover",
                    "commit",
                    "--evidence",
                    str(evidence_path),
                    "--at",
                    "103",
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert (
            v3_main(
                [
                    *base,
                    "view",
                    "open",
                    "--host",
                    HOST,
                    "--project",
                    PROJECT,
                    "--request-id",
                    "aaaaaaaa-1111-4111-8111-111111111111",
                    "--can-launch-terminal",
                    "--json",
                    "--at",
                    "104",
                ]
            )
            == 0
        )
        opened = json.loads(capsys.readouterr().out)
        view_id = ViewId(opened["viewId"])
        assert opened["kind"] == "attach"

        assert v3_main([*base, "frame", "list", "--at", "104"]) == 0
        frames = json.loads(capsys.readouterr().out)
        assert len(frames) == 1
        frame_id = frames[0]["frameId"]
        fake = tmp_path / "fake-codex"
        fake.write_text(
            "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        monkeypatch.setattr(
            "agent_switchboard._v3.workflow.probe_contract",
            lambda provider, executable: ProviderContract(
                ProviderId(provider), str(fake), "99.0.0"
            ),
        )
        assert (
            v3_main(
                [
                    *base,
                    "frame",
                    "reopen",
                    "--host",
                    HOST,
                    "--frame",
                    frame_id,
                    "--session",
                    SESSION_KEY,
                    "--request-id",
                    "aaaaaaaa-1212-4212-8212-121212121212",
                    "--at",
                    "104",
                ]
            )
            == 0
        )
        reopened = json.loads(capsys.readouterr().out)
        assert reopened == {
            "frameId": frame_id,
            "runtimePresence": "live",
            "sessionKey": SESSION_KEY,
        }
        assert v3_main([*base, "state", "host", "--json", "--at", "104"]) == 0
        host_state = json.loads(capsys.readouterr().out)
        assert host_state["workContexts"][0]["claimState"] == "held"
        assert host_state["workContexts"][0]["foregroundFrameId"] == frame_id
        surface_panes = [
            pane
            for pane in tmux.panes()
            if pane.role == ROLE_SURFACE and pane.frame_id == frame_id
        ]
        assert len(surface_panes) == 1
        assert (
            surface_panes[0].session_name == tmux.names("agent", view_id).view_session
        )
        assert not surface_panes[0].dead

        assert (
            v3_main(
                [
                    *base,
                    "view",
                    "mode",
                    "--view",
                    str(view_id),
                    "--mode",
                    "navigator",
                    "--request-id",
                    "aaaaaaaa-2222-4222-8222-222222222222",
                    "--at",
                    "105",
                ]
            )
            == 0
        )
        capsys.readouterr()
        deadline = time.monotonic() + 3
        shell = tmux.inspect_shell("agent", GENERATION, view_id, ViewMode.NAVIGATOR)
        while shell.sidebar is not None and shell.sidebar.dead:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
            shell = tmux.inspect_shell("agent", GENERATION, view_id, ViewMode.NAVIGATOR)
        assert shell.sidebar is not None
        assert not shell.sidebar.dead

        class ExecCalled(RuntimeError):
            pass

        captured: list[str] = []

        def capture_exec(_executable: str, argv: tuple[str, ...]) -> None:
            captured.extend(argv)
            raise ExecCalled

        monkeypatch.setattr("os.execvp", capture_exec)
        with pytest.raises(ExecCalled):
            v3_main(
                [
                    *base,
                    "view",
                    "attach",
                    "--host",
                    HOST,
                    "--view",
                    str(view_id),
                    "--request-id",
                    "aaaaaaaa-1111-4111-8111-111111111111",
                    "--at",
                    "106",
                ]
            )
        assert captured[-1].endswith(":main")
        captured.clear()
        with pytest.raises(ExecCalled):
            v3_main(
                [
                    *base,
                    "view",
                    "attach",
                    "--view",
                    str(view_id),
                    "--at",
                    "106",
                ]
            )
        assert captured[-1].endswith(":main")
        assert (
            v3_main(
                [
                    *base,
                    "session",
                    "stop",
                    "--session",
                    SESSION_KEY,
                    "--at",
                    "107",
                ]
            )
            == 0
        )
        stopped = json.loads(capsys.readouterr().out)
        assert stopped["runtimePresence"] == "stopped"
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket), "kill-server"],
            check=False,
            capture_output=True,
        )
