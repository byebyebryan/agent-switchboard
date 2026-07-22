from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from test_v3_cutover import (
    GENERATION,
    HOST,
    PROJECT,
    SESSION_KEY,
    cutover_evidence,
    export_legacy,
    roots,
    seeded_legacy,
)

from agent_switchboard._v3.cli import main as v3_main
from agent_switchboard._v3.domain import ProviderId, ViewId, ViewMode
from agent_switchboard._v3.provider_runtime import ProviderContract
from agent_switchboard._v3.tmux_view import TmuxExecutor

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")


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
