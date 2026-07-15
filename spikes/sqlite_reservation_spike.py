#!/usr/bin/env python3
"""Exercise the cross-process launch-reservation invariants proposed for ASB."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid


ACTIVE_STATES = ("reserved", "surface_ready", "waiting_for_client", "provider_started")


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def prepare(path: Path, request_id: str, target: str) -> tuple[str, str]:
    connection = connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing_request = connection.execute(
            "SELECT launch_id, target FROM launches WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if existing_request:
            if existing_request[1] != target:
                connection.execute("ROLLBACK")
                return "request_conflict", existing_request[0]
            connection.execute("COMMIT")
            return "idempotent", existing_request[0]

        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        existing_target = connection.execute(
            f"SELECT launch_id FROM launches WHERE target = ? AND state IN ({placeholders})",
            (target, *ACTIVE_STATES),
        ).fetchone()
        if existing_target:
            connection.execute("COMMIT")
            return "existing", existing_target[0]

        launch_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO launches(launch_id, request_id, target, state, expires_at) "
            "VALUES (?, ?, ?, 'reserved', ?)",
            (launch_id, request_id, target, time.time() + 30),
        )
        connection.execute("COMMIT")
        return "created", launch_id
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def worker(path: str, request_id: str, target: str, start, queue) -> None:
    start.wait()
    queue.put((request_id, *prepare(Path(path), request_id, target)))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="asb-reservation-") as tmp:
        path = Path(tmp) / "state.sqlite"
        connection = connect(path)
        connection.executescript(
            """
            CREATE TABLE launches (
                launch_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                target TEXT NOT NULL,
                state TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX one_active_launch_per_target
                ON launches(target)
                WHERE state IN (
                    'reserved', 'surface_ready', 'waiting_for_client',
                    'provider_started'
                );
            """
        )
        connection.close()

        context = multiprocessing.get_context("spawn")
        start = context.Event()
        queue = context.Queue()
        requests = [str(uuid.uuid4()) for _ in range(16)]
        processes = [
            context.Process(
                target=worker,
                args=(str(path), request_id, "codex:session-1", start, queue),
            )
            for request_id in requests
        ]
        for process in processes:
            process.start()
        start.set()
        results = [queue.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)

        launch_ids = {launch_id for _, _, launch_id in results}
        created = [result for result in results if result[1] == "created"]
        winner_request = created[0][0]
        retry = prepare(path, winner_request, "codex:session-1")
        conflict = prepare(path, winner_request, "codex:different-session")

        connection = connect(path)
        active_id = connection.execute(
            "SELECT launch_id FROM launches WHERE target = ?",
            ("codex:session-1",),
        ).fetchone()[0]
        connection.execute(
            "UPDATE launches SET state = 'expired' WHERE launch_id = ?", (active_id,)
        )
        connection.close()
        replacement = prepare(path, str(uuid.uuid4()), "codex:session-1")

        passed = (
            len(created) == 1
            and len(launch_ids) == 1
            and retry[0] in {"idempotent", "existing"}
            and retry[1] == active_id
            and conflict[0] == "request_conflict"
            and replacement[0] == "created"
            and replacement[1] != active_id
            and all(process.exitcode == 0 for process in processes)
        )
        print(
            json.dumps(
                {
                    "passed": passed,
                    "workers": len(processes),
                    "created_count": len(created),
                    "unique_launch_ids": len(launch_ids),
                    "result_kinds": sorted(kind for _, kind, _ in results),
                    "idempotent_retry": retry[0],
                    "request_reuse": conflict[0],
                    "replacement_after_expiry": replacement[0],
                },
                indent=2,
            )
        )
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
