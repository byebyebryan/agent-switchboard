from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "live_codex_smoke.py"
FAKE_CODEX = ROOT / "tests" / "fakes" / "fake_codex.py"
FINGERPRINT = "5d8251e1e2f713a3c567c927386f84f2f94692d4721b90d8ff36d0ff92877621"


def _smoke_environment(
    tmp_path: Path, plan: dict[str, object]
) -> tuple[dict[str, str], Path]:
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    executable = binary_directory / "codex"
    executable.symlink_to(FAKE_CODEX.resolve())
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    environment = os.environ.copy()
    source_path = str(ROOT / "src")
    inherited_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        os.pathsep.join((source_path, inherited_python_path))
        if inherited_python_path
        else source_path
    )
    environment.update(
        {
            "FAKE_CODEX_LOG": str(tmp_path / "fake-codex.log"),
            "FAKE_CODEX_PLAN": str(plan_path),
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_STATE_HOME": str(tmp_path / "user-state"),
        }
    )
    return environment, executable


def test_smoke_environment_prepends_source_tree_and_preserves_pythonpath(
    tmp_path: Path,
    monkeypatch,
) -> None:
    inherited = os.pathsep.join(("/existing/first", "/existing/second"))
    monkeypatch.setenv("PYTHONPATH", inherited)

    environment, _ = _smoke_environment(tmp_path, {})

    assert environment["PYTHONPATH"].split(os.pathsep) == [
        str(ROOT / "src"),
        "/existing/first",
        "/existing/second",
    ]


def _run_smoke(
    tmp_path: Path,
    plan: dict[str, object],
) -> subprocess.CompletedProcess[str]:
    environment, executable = _smoke_environment(tmp_path, plan)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--codex", str(executable)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )


def test_live_smoke_prints_only_sanitized_summary_and_uses_isolated_state(
    tmp_path: Path,
) -> None:
    secret = "SECRET-LIVE-SMOKE-CONTENT"
    thread = {
        "id": "00000000-0000-4000-8000-000000000001",
        "cwd": f"/private/{secret}/checkout",
        "cliVersion": "0.144.4",
        "createdAt": 100,
        "updatedAt": 200,
        "recencyAt": 300,
        "ephemeral": False,
        "modelProvider": "openai",
        "preview": secret,
        "sessionId": "10000000-0000-4000-8000-000000000001",
        "source": "cli",
        "status": {"type": "idle"},
        "turns": [{"content": secret}],
        "name": secret,
        "path": f"/private/{secret}/history.jsonl",
    }
    completed = _run_smoke(
        tmp_path,
        {"app": {"pages": [[{"result": {"data": [thread]}}]]}},
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert secret not in completed.stdout
    summary = json.loads(completed.stdout)
    assert set(summary) == {
        "elapsedMs",
        "features",
        "providerVersion",
        "schemaFingerprint",
        "sessionCount",
    }
    assert summary["providerVersion"] == "0.144.6"
    assert summary["schemaFingerprint"] == FINGERPRINT
    assert summary["features"] == ["app_server_thread_list", "schema_fingerprint"]
    assert summary["sessionCount"] == 1
    assert not (tmp_path / "user-state" / "agent-switchboard").exists()


def test_live_smoke_failure_is_generic_and_payload_free(tmp_path: Path) -> None:
    secret = "SECRET-LIVE-SMOKE-FAILURE"
    completed = _run_smoke(
        tmp_path,
        {
            "app": {
                "pages": [
                    [
                        {
                            "error": {
                                "code": -32000,
                                "message": secret,
                                "data": {"path": f"/private/{secret}"},
                            }
                        }
                    ]
                ]
            }
        },
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "live Codex smoke failed\n"
    assert secret not in completed.stderr
