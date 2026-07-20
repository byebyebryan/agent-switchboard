"""Link immutable imported handoffs to destination tasks."""

from __future__ import annotations

VERSION = 9
NAME = "imported_task_handoffs"
REQUIRES_FOREIGN_KEYS_OFF = False

_UUID_CHECK = """
    length({column}) = 36
    AND {column} = lower({column})
    AND {column} GLOB '????????-????-????-????-????????????'
    AND length(replace({column}, '-', '')) = 32
    AND {column} NOT GLOB '*[^0-9a-f-]*'
    AND replace({column}, '-', '') != printf('%032d', 0)
"""

STATEMENTS = (
    f"""
    CREATE TABLE task_imported_handoffs (
        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
        handoff_id TEXT NOT NULL UNIQUE
            REFERENCES handoffs(handoff_id) ON DELETE RESTRICT,
        source_task_id TEXT NOT NULL CHECK (
            {_UUID_CHECK.format(column="source_task_id")}
        ),
        source_project_id TEXT NOT NULL REFERENCES projects(project_id)
            ON DELETE RESTRICT,
        imported_at INTEGER NOT NULL CHECK (imported_at >= 0),
        PRIMARY KEY (task_id, handoff_id)
    )
    """,
    """
    CREATE INDEX task_imported_handoffs_task_recent
        ON task_imported_handoffs(task_id, imported_at DESC, handoff_id)
    """,
    """
    CREATE TRIGGER task_imported_handoff_matches_insert
    BEFORE INSERT ON task_imported_handoffs
    WHEN NOT EXISTS (
        SELECT 1
        FROM tasks AS task
        JOIN handoffs AS handoff ON handoff.handoff_id = NEW.handoff_id
        WHERE task.task_id = NEW.task_id
          AND task.project_id = NEW.source_project_id
          AND handoff.source = 'imported'
    )
    BEGIN
        SELECT RAISE(ABORT, 'imported handoff does not match destination task');
    END
    """,
    """
    CREATE TRIGGER task_imported_handoffs_append_only_update
    BEFORE UPDATE ON task_imported_handoffs
    BEGIN
        SELECT RAISE(ABORT, 'task imported handoffs are append-only');
    END
    """,
    """
    CREATE TRIGGER task_imported_handoffs_append_only_delete
    BEFORE DELETE ON task_imported_handoffs
    BEGIN
        SELECT RAISE(ABORT, 'task imported handoffs are append-only');
    END
    """,
)
