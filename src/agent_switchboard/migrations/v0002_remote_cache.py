"""Add materialized remote endpoint and last-successful snapshot state."""

from __future__ import annotations

VERSION = 2
NAME = "remote_snapshot_cache"

STATEMENTS = (
    """
    CREATE TABLE remote_snapshots (
        remote_name TEXT PRIMARY KEY CHECK (length(remote_name) BETWEEN 1 AND 128),
        ssh_target TEXT NOT NULL CHECK (length(ssh_target) BETWEEN 1 AND 1024),
        display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 256),
        remote_host_id TEXT REFERENCES hosts(host_id) ON DELETE SET NULL,
        declared INTEGER NOT NULL DEFAULT 1 CHECK (declared IN (0, 1)),
        reachability TEXT NOT NULL DEFAULT 'unknown'
            CHECK (reachability IN ('online', 'offline', 'unknown')),
        snapshot_schema_version INTEGER
            CHECK (snapshot_schema_version IS NULL OR snapshot_schema_version > 0),
        snapshot_protocol_version INTEGER
            CHECK (snapshot_protocol_version IS NULL OR snapshot_protocol_version > 0),
        snapshot_json TEXT CHECK (snapshot_json IS NULL OR json_valid(snapshot_json)),
        snapshot_hash TEXT CHECK (
            snapshot_hash IS NULL
            OR (
                length(snapshot_hash) = 64
                AND snapshot_hash NOT GLOB '*[^0-9a-f]*'
            )
        ),
        snapshot_observed_at INTEGER
            CHECK (snapshot_observed_at IS NULL OR snapshot_observed_at >= 0),
        snapshot_received_at INTEGER
            CHECK (snapshot_received_at IS NULL OR snapshot_received_at >= 0),
        last_attempt_at INTEGER CHECK (last_attempt_at IS NULL OR last_attempt_at >= 0),
        error_code TEXT CHECK (error_code IS NULL OR length(error_code) <= 128),
        error_detail TEXT CHECK (error_detail IS NULL OR length(error_detail) <= 2048),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
        CHECK (
            (snapshot_json IS NULL
                AND snapshot_hash IS NULL
                AND snapshot_schema_version IS NULL
                AND snapshot_protocol_version IS NULL
                AND snapshot_observed_at IS NULL
                AND snapshot_received_at IS NULL)
            OR (snapshot_json IS NOT NULL
                AND snapshot_hash IS NOT NULL
                AND snapshot_schema_version IS NOT NULL
                AND snapshot_protocol_version IS NOT NULL
                AND snapshot_observed_at IS NOT NULL
                AND snapshot_received_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX remote_snapshots_host ON remote_snapshots(remote_host_id)
    """,
    """
    CREATE INDEX remote_snapshots_refresh
        ON remote_snapshots(declared, reachability, snapshot_received_at)
    """,
)
