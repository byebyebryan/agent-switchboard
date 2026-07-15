#!/usr/bin/env python3
"""Probe Codex app-server discovery without retaining conversation content."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import time
from typing import Any


class AppServer:
    def __init__(self) -> None:
        self.process = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.next_id = 1
        self.initialize_response, _ = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "agent_switchboard_spike",
                    "title": "Agent Switchboard spike",
                    "version": "0",
                }
            },
            request_id=0,
        )
        self.notify("initialized", {})

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"method": method, "params": params})

    def request(
        self, method: str, params: dict[str, Any], request_id: int | None = None
    ) -> tuple[dict[str, Any], float]:
        if request_id is None:
            request_id = self.next_id
            self.next_id += 1
        started = time.monotonic()
        self.send({"method": method, "id": request_id, "params": params})
        assert self.process.stdout is not None
        for line in self.process.stdout:
            response = json.loads(line)
            if response.get("id") == request_id:
                return response, time.monotonic() - started
        raise RuntimeError("app-server closed before responding")

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def thread_shape(thread: dict[str, Any]) -> dict[str, Any]:
    return {
        "keys": sorted(thread),
        "status": thread.get("status"),
        "source": thread.get("source"),
        "cliVersion": thread.get("cliVersion"),
        "hasName": thread.get("name") is not None,
        "hasPath": thread.get("path") is not None,
        "turnCount": len(thread.get("turns", [])),
        "idType": type(thread.get("id")).__name__,
        "cwdType": type(thread.get("cwd")).__name__,
    }


def initialize_shape(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "keys": sorted(result),
        "userAgentType": type(result.get("userAgent")).__name__,
        "codexHomeType": type(result.get("codexHome")).__name__,
        "platformFamily": result.get("platformFamily"),
        "platformOs": result.get("platformOs"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", type=Path)
    args = parser.parse_args()

    version = subprocess.run(
        ["codex", "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    server = AppServer()
    try:
        timings: dict[str, list[float]] = {"normal": [], "stateDbOnly": []}
        first_responses: dict[str, dict[str, Any]] = {}
        for label, state_db_only in (("normal", False), ("stateDbOnly", True)):
            for _ in range(5):
                response, elapsed = server.request(
                    "thread/list",
                    {"limit": 50, "useStateDbOnly": state_db_only},
                )
                timings[label].append(elapsed)
                first_responses.setdefault(label, response)

        pagination_response, _ = server.request("thread/list", {"limit": 2})
        first_page = pagination_response["result"]
        cursor = first_page.get("nextCursor")
        second_page = None
        if cursor:
            second_page, _ = server.request(
                "thread/list", {"limit": 2, "cursor": cursor}
            )

        total = 0
        pages = 0
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 100, "useStateDbOnly": True}
            if cursor:
                params["cursor"] = cursor
            response, _ = server.request("thread/list", params)
            result = response["result"]
            total += len(result["data"])
            pages += 1
            cursor = result.get("nextCursor")
            if not cursor:
                break

        samples = first_page.get("data", [])[:2]
        output: dict[str, Any] = {
            "providerVersion": version,
            "initialize": initialize_shape(
                server.initialize_response.get("result", {})
            ),
            "history": {"nonArchivedInteractiveThreads": total, "pagesAt100": pages},
            "latencyMs": {
                label: {
                    "median": round(statistics.median(values) * 1000, 2),
                    "min": round(min(values) * 1000, 2),
                    "max": round(max(values) * 1000, 2),
                }
                for label, values in timings.items()
            },
            "pagination": {
                "firstPageHasNextCursor": first_page.get("nextCursor") is not None,
                "firstPageHasBackwardsCursor": first_page.get("backwardsCursor")
                is not None,
                "secondPageReturned": second_page is not None,
            },
            "threadShapes": [thread_shape(thread) for thread in samples],
        }
        if args.schema:
            output["schema"] = {
                "name": args.schema.name,
                "sha256": hashlib.sha256(args.schema.read_bytes()).hexdigest(),
            }
        print(json.dumps(output, indent=2))
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
