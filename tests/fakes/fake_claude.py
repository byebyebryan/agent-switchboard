#!/usr/bin/env python3
"""Plan-driven fake Claude executable for bounded capability tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _plan() -> dict[str, object]:
    source = os.environ.get("FAKE_CLAUDE_PLAN")
    return json.loads(Path(source).read_text(encoding="utf-8")) if source else {}


def _log(value: dict[str, object]) -> None:
    target = os.environ.get("FAKE_CLAUDE_LOG")
    if target:
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, separators=(",", ":")) + "\n")


def _acceptance_probe(plan: dict[str, object]) -> int:
    try:
        settings_index = sys.argv.index("--settings") + 1
        settings = json.loads(
            Path(sys.argv[settings_index]).read_text(encoding="utf-8")
        )
        prompt = sys.stdin.read()
    except (OSError, ValueError, IndexError, json.JSONDecodeError):
        return 65
    if plan.get("invalidAcceptanceOutput"):
        sys.stdout.write(str(plan.get("outputSecret", "invalid output")))
        return 1
    session_id = "77777777-7777-4777-8777-777777777777"
    prompt_id = "88888888-8888-4888-8888-888888888888"
    events = (
        {
            "session_id": session_id,
            "cwd": os.getcwd(),
            "hook_event_name": "SessionStart",
            "source": "startup",
            "transcript_path": f"/private/{prompt.strip()}.jsonl",
        },
        {
            "session_id": session_id,
            "cwd": os.getcwd(),
            "hook_event_name": "UserPromptSubmit",
            "prompt_id": prompt_id,
            "prompt": prompt,
            "transcript_path": f"/private/{prompt.strip()}.jsonl",
        },
        {
            "session_id": session_id,
            "cwd": os.getcwd(),
            "hook_event_name": "SessionEnd",
            "prompt_id": prompt_id,
            "reason": "other",
            "transcript_path": f"/private/{prompt.strip()}.jsonl",
        },
    )
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 66
    for payload in events:
        groups = hooks.get(payload["hook_event_name"])
        if not isinstance(groups, list):
            return 67
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                return 68
            for handler in group["hooks"]:
                if not isinstance(handler, dict) or handler.get("type") != "command":
                    continue
                command = handler.get("command")
                arguments = handler.get("args", [])
                if not isinstance(command, str) or not isinstance(arguments, list):
                    return 69
                subprocess.run(
                    [command, *arguments],
                    input=json.dumps(payload),
                    text=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
    sys.stdout.write(
        json.dumps(
            {
                "is_error": True,
                "num_turns": 0,
                "result": str(plan.get("outputSecret", prompt)),
                "total_cost_usd": 0.0,
            },
            separators=(",", ":"),
        )
    )
    return 1


def main() -> int:
    plan = _plan()
    _log({"event": "invoke", "pid": os.getpid(), "argv": sys.argv[1:]})
    if sys.argv[1:] != ["--version"]:
        return _acceptance_probe(plan) if plan.get("acceptanceProbe") else 64
    child: subprocess.Popen[str] | None = None
    if plan.get("spawnChild"):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            text=True,
            stdout=(subprocess.DEVNULL if plan.get("detachChild") else None),
            stderr=(subprocess.DEVNULL if plan.get("detachChild") else None),
        )
        _log({"event": "child", "pid": child.pid})

    def terminate(_signal: int, _frame: object) -> None:
        if plan.get("ignoreTerm"):
            return
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    time.sleep(float(plan.get("sleep", 0)))
    sys.stdout.write(str(plan.get("stdout", "2.1.210 (Claude Code)\n")))
    sys.stdout.flush()
    stderr_bytes = int(plan.get("stderrBytes", 0))
    if stderr_bytes:
        sys.stderr.write("x" * stderr_bytes)
        sys.stderr.flush()
    result = int(plan.get("returncode", 0))
    if child is not None and not plan.get("detachChild"):
        child.wait()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
