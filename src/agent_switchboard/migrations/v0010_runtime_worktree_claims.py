"""Retain worktree claims while a closed task runtime may still be active."""

from __future__ import annotations

VERSION = 10
NAME = "runtime_worktree_claims"
REQUIRES_FOREIGN_KEYS_OFF = False

_CONFLICTING_TASK = """
    task.checkout_id = NEW.checkout_id
    AND (
        task.status = 'open'
        OR (
            task.current_session_key IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM sessions AS session
                WHERE session.session_key = task.current_session_key
                  AND session.runtime_presence != 'stopped'
            )
        )
    )
"""

STATEMENTS = (
    "DROP TRIGGER tasks_worktree_claim_insert",
    "DROP TRIGGER tasks_worktree_claim_update",
    f"""
    CREATE TRIGGER tasks_worktree_claim_insert
    BEFORE INSERT ON tasks
    WHEN NEW.status = 'open' AND NEW.checkout_id IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM checkouts
          WHERE checkout_id = NEW.checkout_id AND kind = 'worktree'
      )
      AND EXISTS (
          SELECT 1 FROM tasks AS task
          WHERE {_CONFLICTING_TASK}
      )
    BEGIN
        SELECT RAISE(ABORT, 'worktree already belongs to an active task');
    END
    """,
    f"""
    CREATE TRIGGER tasks_worktree_claim_update
    BEFORE UPDATE OF status, checkout_id ON tasks
    WHEN NEW.status = 'open' AND NEW.checkout_id IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM checkouts
          WHERE checkout_id = NEW.checkout_id AND kind = 'worktree'
      )
      AND EXISTS (
          SELECT 1 FROM tasks AS task
          WHERE {_CONFLICTING_TASK}
            AND task.task_id != NEW.task_id
      )
    BEGIN
        SELECT RAISE(ABORT, 'worktree already belongs to an active task');
    END
    """,
)
