#!/usr/bin/env python3
"""Capture the documented Claude supervisor JSON shape without user content."""

from __future__ import annotations

import json
import subprocess
from typing import Any


def row_shape(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "keys": sorted(row),
        "kind": row.get("kind"),
        "state": row.get("state"),
        "status": row.get("status"),
        "hasRuntimeId": isinstance(row.get("id"), str),
        "hasSessionId": isinstance(row.get("sessionId"), str),
        "hasPid": isinstance(row.get("pid"), int),
        "hasCwd": isinstance(row.get("cwd"), str),
        "hasName": isinstance(row.get("name"), str),
        "startedAtType": type(row.get("startedAt")).__name__,
    }


def main() -> int:
    version = subprocess.run(
        ["claude", "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    query = subprocess.run(
        ["claude", "agents", "--all", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = json.loads(query.stdout)
    shapes = [row_shape(row) for row in rows]

    unique_shapes: dict[str, dict[str, Any]] = {}
    for shape in shapes:
        key = json.dumps(shape, sort_keys=True)
        unique_shapes.setdefault(key, shape)

    print(
        json.dumps(
            {
                "providerVersion": version,
                "rowCount": len(rows),
                "stateStatusCombinations": sorted(
                    {
                        f"{row.get('kind')}:{row.get('state')}:{row.get('status')}"
                        for row in rows
                    }
                ),
                "rowShapes": list(unique_shapes.values()),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
