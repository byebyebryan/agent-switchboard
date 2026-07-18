from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "live_claude_smoke.py"
FAKE_CLAUDE = ROOT / "tests" / "fakes" / "fake_claude.py"


def _environment(
    tmp_path: Path, plan: dict[str, object]
) -> tuple[dict[str, str], Path, Path]:
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    claude = binary_directory / "claude"
    claude.symlink_to(FAKE_CLAUDE.resolve())
    swbctl = binary_directory / "swbctl"
    swbctl.write_text(
        "#!/usr/bin/env python3\n"
        "from agent_switchboard.cli import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    swbctl.chmod(0o755)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    environment = os.environ.copy()
    inherited_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT / "src"), inherited_python_path) if part
    )
    environment.update(
        {
            "FAKE_CLAUDE_PLAN": str(plan_path),
            "HOME": str(tmp_path / "home"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment, claude, swbctl


def _run(tmp_path: Path, plan: dict[str, object]) -> subprocess.CompletedProcess[str]:
    environment, claude, swbctl = _environment(tmp_path, plan)
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--claude",
            str(claude),
            "--swbctl",
            str(swbctl),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )


def test_live_claude_smoke_prints_only_sanitized_no_model_summary(
    tmp_path: Path,
) -> None:
    secret = "SECRET-CLAUDE-SMOKE-OUTPUT"
    completed = _run(
        tmp_path,
        {
            "acceptanceProbe": True,
            "outputSecret": secret,
        },
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert secret not in completed.stdout
    summary = json.loads(completed.stdout)
    assert set(summary) == {
        "elapsedMs",
        "eventCounts",
        "features",
        "providerVersion",
        "reportedCostUsd",
        "reportedTurns",
        "sessionCount",
    }
    assert summary["providerVersion"] == "2.1.214"
    assert summary["features"] == ["hooks", "native_resume", "tmux_runtime"]
    assert summary["eventCounts"] == {
        "SessionEnd": 1,
        "SessionStart": 1,
        "UserPromptSubmit": 1,
    }
    assert summary["reportedCostUsd"] == 0.0
    assert summary["reportedTurns"] == 0
    assert summary["sessionCount"] == 1


def test_live_claude_smoke_failure_is_generic_and_payload_free(
    tmp_path: Path,
) -> None:
    secret = "SECRET-CLAUDE-SMOKE-FAILURE"
    completed = _run(
        tmp_path,
        {
            "acceptanceProbe": True,
            "invalidAcceptanceOutput": True,
            "outputSecret": secret,
        },
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "live Claude smoke failed\n"
    assert secret not in completed.stderr
