#!/usr/bin/env python3
"""Prove a blank Codex thread can be named before its first model turn."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

from codex_thread_name_probe import RESPONSE_TIMEOUT_SECONDS, StdioAppServer

PROBE_TITLE = "agent switchboard preturn naming probe"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create and name an isolated Codex thread before its first turn"
    )
    parser.add_argument("--codex", default=shutil.which("codex"))
    arguments = parser.parse_args()
    if not arguments.codex:
        parser.error("codex executable was not found")

    version = subprocess.run(
        [arguments.codex, "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=RESPONSE_TIMEOUT_SECONDS,
    ).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="codex-prestart-name-probe-") as codex_home:
        environment = dict(os.environ)
        environment["CODEX_HOME"] = codex_home
        environment.pop("CODEX_THREAD_ID", None)
        with StdioAppServer(arguments.codex, environment=environment) as server:
            server.request(
                1,
                "initialize",
                {
                    "clientInfo": {
                        "name": "agent-switchboard-prestart-name-probe",
                        "version": "1",
                    },
                    "capabilities": {},
                },
            )
            server.send({"method": "initialized", "params": {}})
            started = server.request(2, "thread/start", {})
            thread = started.get("thread")
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                raise RuntimeError("thread/start returned no thread identity")
            thread_id = thread["id"]
            initial_name_absent = thread.get("name") is None
            server.request(
                3,
                "thread/name/set",
                {"threadId": thread_id, "name": PROBE_TITLE},
            )
            read = server.request(
                4,
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
            )
            retained = read.get("thread")
            named_before_first_turn = (
                isinstance(retained, dict)
                and retained.get("name") == PROBE_TITLE
                and retained.get("turns") == []
            )
            server.request(5, "thread/delete", {"threadId": thread_id})

    print(
        json.dumps(
            {
                "providerVersion": version,
                "feature": "precreate-name-before-turn",
                "initialNameAbsent": initial_name_absent,
                "namedBeforeFirstTurn": named_before_first_turn,
                "modelTurnsStarted": 0,
                "isolatedCodexHome": True,
            },
            separators=(",", ":"),
        )
    )
    return 0 if initial_name_absent and named_before_first_turn else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from None
