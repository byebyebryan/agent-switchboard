#!/usr/bin/env python3
"""Record private Codex hook input for one isolated rollover study."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sys
import time
from typing import Any


MAX_INPUT_BYTES = 8 * 1024 * 1024


def _ancestry(pid: int, *, limit: int = 12) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    while pid > 1 and pid not in seen and len(result) < limit:
        seen.add(pid)
        proc = Path("/proc") / str(pid)
        try:
            stat_text = (proc / "stat").read_text(encoding="ascii")
            close = stat_text.rfind(")")
            fields = stat_text[close + 2 :].split()
            parent = int(fields[1])
            started = int(fields[19])
            command = (proc / "comm").read_text(encoding="utf-8").strip()
        except (FileNotFoundError, IndexError, OSError, ValueError):
            break
        result.append(
            {
                "pid": pid,
                "parent_pid": parent,
                "process_birth_ticks": started,
                "command": command,
            }
        )
        pid = parent
    return result


def _payload() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("hook input exceeds bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("hook input must be an object")
    return value


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: hook_recorder.py EVENTS.jsonl", file=sys.stderr)
        return 2
    output = Path(sys.argv[1])
    expected_root = os.environ.get("ASB_SPIKE_DISPOSABLE_ROOT")
    if (
        not expected_root
        or not output.is_absolute()
        or not output.resolve().is_relative_to(Path(expected_root).resolve())
    ):
        print("hook output is outside the disposable root", file=sys.stderr)
        return 2
    try:
        hook = _payload()
        record = {
            "captured_at_ns": time.time_ns(),
            "provider_identity": hook.get("session_id"),
            "event": hook.get("hook_event_name"),
            "source": hook.get("source"),
            "turn_identity": hook.get("turn_id"),
            "provider_input": hook.get("prompt"),
            "provider_cwd": hook.get("cwd"),
            "transcript_path": hook.get("transcript_path"),
            "permission_mode": hook.get("permission_mode"),
            "launch_token": os.environ.get("ASB_SPIKE_LAUNCH_TOKEN"),
            "surface_token": os.environ.get("ASB_SPIKE_SURFACE_TOKEN"),
            "tmux_pane": os.environ.get("TMUX_PANE"),
            "tmux_server": os.environ.get("SWB_V3_TMUX_SOCKET"),
            "process_ancestry": _ancestry(os.getpid()),
        }
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            output,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            os.write(
                descriptor,
                json.dumps(
                    record,
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
    except (OSError, ValueError, json.JSONDecodeError):
        print("private hook capture failed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
