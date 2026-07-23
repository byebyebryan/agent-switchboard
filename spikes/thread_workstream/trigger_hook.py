#!/usr/bin/env python3
"""Hold one isolated Codex execution input at the pre-submit hook boundary."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sys
import time
from typing import Any


MAX_INPUT_BYTES = 8 * 1024 * 1024
DECISION_TIMEOUT_SECONDS = 30.0
EXPECTED_INPUTS = {
    "ordinary": "Implement the plan.",
    "conversational": "Proceed with the accepted conversational plan.",
}


def _inside_disposable(path: Path) -> bool:
    expected_root = os.environ.get("ASB_SPIKE_DISPOSABLE_ROOT")
    return bool(
        expected_root
        and path.is_absolute()
        and path.resolve().is_relative_to(Path(expected_root).resolve())
    )


def _payload() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("hook input exceeds bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("hook input must be an object")
    return value


def _append_private(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.write(
            descriptor,
            json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            + b"\n",
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _record(hook: dict[str, Any], *, trigger_match: bool) -> dict[str, Any]:
    return {
        "captured_at_ns": time.time_ns(),
        "provider_identity": hook.get("session_id"),
        "event": hook.get("hook_event_name"),
        "source": hook.get("source"),
        "turn_identity": hook.get("turn_id"),
        "provider_input": hook.get("prompt"),
        "provider_output": hook.get("last_assistant_message"),
        "provider_cwd": hook.get("cwd"),
        "transcript_path": hook.get("transcript_path"),
        "permission_mode": hook.get("permission_mode"),
        "launch_token": os.environ.get("ASB_SPIKE_LAUNCH_TOKEN"),
        "surface_token": os.environ.get("ASB_SPIKE_SURFACE_TOKEN"),
        "tmux_pane": os.environ.get("TMUX_PANE"),
        "tmux_server": os.environ.get("SWB_V3_TMUX_SOCKET"),
        "trigger_match": trigger_match,
    }


def _wait_for_block(decision_path: Path) -> bool:
    deadline = time.monotonic() + DECISION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            value = json.loads(decision_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            time.sleep(0.02)
            continue
        return isinstance(value, dict) and value.get("decision") == "block"
    return False


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "usage: trigger_hook.py EVENTS.jsonl DECISION.json SCENARIO",
            file=sys.stderr,
        )
        return 2
    events_path = Path(sys.argv[1])
    decision_path = Path(sys.argv[2])
    scenario = sys.argv[3]
    if (
        scenario not in EXPECTED_INPUTS
        or not _inside_disposable(events_path)
        or not _inside_disposable(decision_path)
    ):
        print("trigger hook target is not authorized", file=sys.stderr)
        return 2
    try:
        hook = _payload()
        trigger_match = (
            hook.get("hook_event_name") == "UserPromptSubmit"
            and hook.get("prompt") == EXPECTED_INPUTS[scenario]
        )
        _append_private(
            events_path,
            _record(hook, trigger_match=trigger_match),
        )
        if not trigger_match:
            return 0
        if not _wait_for_block(decision_path):
            print("isolated trigger decision timed out", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": "Isolated trigger study held input before sampling.",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError, json.JSONDecodeError):
        print("private trigger hook failed closed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
