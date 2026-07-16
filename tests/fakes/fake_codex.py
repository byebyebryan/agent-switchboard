#!/usr/bin/env python3
"""Plan-driven fake Codex executable for provider contract tests."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any


def load_plan() -> dict[str, Any]:
    source = os.environ.get("FAKE_CODEX_PLAN")
    return json.loads(Path(source).read_text(encoding="utf-8")) if source else {}


def append_log(value: dict[str, Any]) -> None:
    target = os.environ.get("FAKE_CODEX_LOG")
    if not target:
        return
    with Path(target).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, separators=(",", ":")) + "\n")


def emit(value: object) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def run_actions(actions: list[dict[str, Any]], request_id: int) -> bool:
    for action in actions:
        if "sleep" in action:
            time.sleep(float(action["sleep"]))
        elif "stderrBytes" in action:
            remaining = int(action["stderrBytes"])
            chunk = "x" * min(remaining, 4096)
            while remaining:
                written = min(remaining, len(chunk))
                sys.stderr.write(chunk[:written])
                sys.stderr.flush()
                remaining -= written
        elif "json" in action:
            emit(action["json"])
        elif "wrongResult" in action:
            emit({"id": request_id + 1000, "result": action["wrongResult"]})
        elif "result" in action:
            emit({"id": request_id, "result": action["result"]})
        elif "error" in action:
            emit({"id": request_id, "error": action["error"]})
        elif "raw" in action:
            sys.stdout.write(str(action["raw"]))
            sys.stdout.flush()
        elif "rawHex" in action:
            sys.stdout.buffer.write(bytes.fromhex(action["rawHex"]))
            sys.stdout.buffer.flush()
        elif "lineBytes" in action:
            sys.stdout.write("x" * int(action["lineBytes"]) + "\n")
            sys.stdout.flush()
        elif action.get("exit"):
            return False
        else:
            raise RuntimeError("unknown fake action")
    return True


def app_server(plan: dict[str, Any]) -> int:
    app = plan.get("app", {})
    append_log({"event": "start", "pid": os.getpid(), "argv": sys.argv[1:]})

    def terminate(_signum: int, _frame: object) -> None:
        if app.get("ignoreTerm", False):
            append_log({"event": "terminate_ignored", "pid": os.getpid()})
            return
        append_log({"event": "terminate", "pid": os.getpid()})
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    page = 0
    for raw in sys.stdin:
        request = json.loads(raw)
        append_log({"event": "request", "message": request})
        method = request.get("method")
        if method == "initialized":
            continue
        request_id = request.get("id")
        if not isinstance(request_id, int):
            return 20
        if method == "initialize":
            actions = app.get(
                "initializeActions",
                [
                    {
                        "result": {
                            "userAgent": "fake-codex",
                            "codexHome": "/fake/codex-home",
                            "platformFamily": "unix",
                            "platformOs": "linux",
                        }
                    }
                ],
            )
        elif method == "thread/list":
            pages = app.get("pages", [[{"result": {"data": []}}]])
            actions = pages[min(page, len(pages) - 1)]
            page += 1
        elif method == "hooks/list":
            actions = app.get("hooks", [{"result": {"data": []}}])
        else:
            actions = [
                {"error": {"code": -32601, "message": "unsupported fake method"}}
            ]
        if not run_actions(actions, request_id):
            return 0
    append_log({"event": "eof", "pid": os.getpid()})
    time.sleep(float(app.get("lingerOnEof", 0)))
    return 0


def main() -> int:
    plan = load_plan()
    append_log(
        {
            "event": "invoke",
            "argv": sys.argv[1:],
            "environmentMarker": os.environ.get("SWITCHBOARD_TEST_ENV"),
            "codexHome": os.environ.get("CODEX_HOME"),
            "xdgStateHome": os.environ.get("XDG_STATE_HOME"),
        }
    )
    if sys.argv[1:] == ["--version"]:
        version = plan.get("version", {})
        time.sleep(float(version.get("sleep", 0)))
        sys.stdout.write(version.get("stdout", "codex-cli 0.144.4\n"))
        sys.stdout.flush()
        return int(version.get("returncode", 0))
    if len(sys.argv) >= 3 and sys.argv[1:3] == [
        "app-server",
        "generate-json-schema",
    ]:
        schema = plan.get("schema", {})
        time.sleep(float(schema.get("sleep", 0)))
        if schema.get("returncode", 0):
            return int(schema["returncode"])
        output = Path(sys.argv[sys.argv.index("--out") + 1])
        append_log(
            {
                "event": "schema",
                "mode": output.stat().st_mode & 0o777,
                "argv": sys.argv[1:],
            }
        )
        if not schema.get("omit", False):
            target = output / "codex_app_server_protocol.v2.schemas.json"
            kind = schema.get("kind", "file")
            if kind == "symlink":
                source = output / "schema-target.json"
                source.write_text("{}", encoding="utf-8")
                target.symlink_to(source)
            elif kind == "fifo":
                os.mkfifo(target)
            elif kind == "directory":
                target.mkdir()
            elif "rawHex" in schema:
                target.write_bytes(bytes.fromhex(schema["rawHex"]))
            elif "raw" in schema:
                target.write_text(schema["raw"], encoding="utf-8")
            elif "value" not in schema:
                fixture = (
                    Path(__file__).resolve().parents[2]
                    / "spikes"
                    / "fixtures"
                    / "codex"
                    / "0.144.4"
                    / "nonexperimental"
                    / "codex_app_server_protocol.v2.schemas.json"
                )
                target.write_bytes(fixture.read_bytes())
            else:
                value = schema["value"]
                target.write_text(json.dumps(value), encoding="utf-8")
        return 0
    if sys.argv[1:] == ["app-server", "--stdio"]:
        return app_server(plan)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
