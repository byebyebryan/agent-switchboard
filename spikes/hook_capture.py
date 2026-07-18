#!/usr/bin/env python3
"""Capture provider hook structure without retaining prompt or transcript data."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import Any


SAFE_EVENT_FIELDS = {
    "agent_id",
    "agent_type",
    "hook_event_name",
    "model",
    "notification_type",
    "permission_mode",
    "prompt_id",
    "reason",
    "session_id",
    "session_title",
    "source",
    "stop_hook_active",
    "tool_name",
    "turn_id",
}

SAFE_ENV_FIELDS = (
    "AGENT_SWITCHBOARD_LAUNCH_ID",
    "AGENT_SWITCHBOARD_SURFACE_ID",
    "ASB_SPIKE_MARKER",
    "COLORTERM",
    "TERM",
    "TMUX",
    "TMUX_PANE",
)


def process_ancestry(pid: int, limit: int = 8) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()

    while pid > 1 and pid not in seen and len(result) < limit:
        seen.add(pid)
        proc = Path("/proc") / str(pid)
        try:
            comm = (proc / "comm").read_text().strip()
            stat = (proc / "stat").read_text()
            close_paren = stat.rfind(")")
            fields = stat[close_paren + 2 :].split()
            ppid = int(fields[1])
        except (FileNotFoundError, IndexError, OSError, ValueError):
            break

        result.append({"pid": pid, "ppid": ppid, "comm": comm})
        pid = ppid

    return result


def main() -> int:
    if len(sys.argv) not in {3, 4}:
        print(
            "usage: hook_capture.py PROVIDER OUTPUT.jsonl [block]",
            file=sys.stderr,
        )
        return 2

    provider, output_path = sys.argv[1:3]
    block = len(sys.argv) == 4
    if block and sys.argv[3] != "block":
        print("optional fourth argument must be 'block'", file=sys.stderr)
        return 2
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"invalid hook JSON: {exc}", file=sys.stderr)
        return 2

    event = {key: payload[key] for key in sorted(SAFE_EVENT_FIELDS) if key in payload}
    event["has_cwd"] = isinstance(payload.get("cwd"), str)
    event["has_transcript_path"] = isinstance(payload.get("transcript_path"), str)

    record = {
        "captured_at_ns": time.time_ns(),
        "provider": provider,
        "event": event,
        "environment": {
            key: os.environ[key] for key in SAFE_ENV_FIELDS if key in os.environ
        },
        "hook_cwd": os.getcwd(),
        "process_ancestry": process_ancestry(os.getpid()),
    }

    encoded = (
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    fd = os.open(output_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)
    return 2 if block else 0


if __name__ == "__main__":
    raise SystemExit(main())
