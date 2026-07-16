"""Persist private evidence ordering and durable runtime locator identity."""

from __future__ import annotations

VERSION = 4
NAME = "runtime_truth_ordering"

STATEMENTS = (
    """
    ALTER TABLE sessions ADD COLUMN runtime_source_priority INTEGER NOT NULL DEFAULT 0
        CHECK (runtime_source_priority >= 0)
    """,
    """
    ALTER TABLE sessions ADD COLUMN runtime_order_ns INTEGER NOT NULL DEFAULT 0
        CHECK (runtime_order_ns >= 0)
    """,
    """
    ALTER TABLE sessions
        ADD COLUMN resumability_source_priority INTEGER NOT NULL DEFAULT 0
        CHECK (resumability_source_priority >= 0)
    """,
    """
    ALTER TABLE sessions ADD COLUMN resumability_order_ns INTEGER NOT NULL DEFAULT 0
        CHECK (resumability_order_ns >= 0)
    """,
    """
    ALTER TABLE sessions ADD COLUMN activity_source_priority INTEGER NOT NULL DEFAULT 0
        CHECK (activity_source_priority >= 0)
    """,
    """
    ALTER TABLE sessions ADD COLUMN activity_order_ns INTEGER NOT NULL DEFAULT 0
        CHECK (activity_order_ns >= 0)
    """,
    """
    ALTER TABLE sessions
        ADD COLUMN attachment_source_priority INTEGER NOT NULL DEFAULT 0
        CHECK (attachment_source_priority >= 0)
    """,
    """
    ALTER TABLE sessions ADD COLUMN attachment_order_ns INTEGER NOT NULL DEFAULT 0
        CHECK (attachment_order_ns >= 0)
    """,
    """
    ALTER TABLE sessions ADD COLUMN last_hook_turn_id TEXT
        CHECK (last_hook_turn_id IS NULL OR length(last_hook_turn_id) <= 256)
    """,
    """
    ALTER TABLE sessions ADD COLUMN last_hook_entry_ns INTEGER
        CHECK (last_hook_entry_ns IS NULL OR last_hook_entry_ns >= 0)
    """,
    """
    ALTER TABLE sessions ADD COLUMN last_hook_kind_priority INTEGER
        CHECK (
            last_hook_kind_priority IS NULL OR last_hook_kind_priority >= 0
        )
    """,
    """
    ALTER TABLE sessions ADD COLUMN runtime_process_birth_id TEXT
        CHECK (
            runtime_process_birth_id IS NULL
            OR length(runtime_process_birth_id) BETWEEN 1 AND 256
        )
    """,
    """
    ALTER TABLE sessions ADD COLUMN tmux_socket TEXT
        CHECK (tmux_socket IS NULL OR length(tmux_socket) <= 4096)
    """,
    """
    ALTER TABLE runtime_observations ADD COLUMN entry_ns INTEGER
        CHECK (entry_ns IS NULL OR entry_ns >= 0)
    """,
    """
    ALTER TABLE runtime_observations ADD COLUMN process_birth_id TEXT
        CHECK (
            process_birth_id IS NULL OR length(process_birth_id) BETWEEN 1 AND 256
        )
    """,
    """
    ALTER TABLE runtime_observations ADD COLUMN tmux_socket TEXT
        CHECK (tmux_socket IS NULL OR length(tmux_socket) <= 4096)
    """,
    """
    ALTER TABLE events ADD COLUMN entry_ns INTEGER
        CHECK (entry_ns IS NULL OR entry_ns >= 0)
    """,
    """
    CREATE INDEX events_session_order
        ON events(session_key, entry_ns DESC, kind_priority DESC)
    """,
)
