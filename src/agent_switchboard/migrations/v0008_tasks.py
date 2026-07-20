"""Add explicit host-owned tasks and task/session launch bindings."""

from __future__ import annotations

VERSION = 8
NAME = "tasks"
REQUIRES_FOREIGN_KEYS_OFF = True

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
    CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY CHECK ({_UUID_CHECK.format(column="task_id")}),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        checkout_id TEXT REFERENCES checkouts(checkout_id) ON DELETE RESTRICT,
        title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 256),
        purpose TEXT CHECK (purpose IS NULL OR length(purpose) <= 4096),
        preferred_provider TEXT CHECK (
            preferred_provider IS NULL
            OR preferred_provider IN ('codex', 'claude')
        ),
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
        pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
        current_session_key TEXT REFERENCES sessions(session_key) ON DELETE RESTRICT,
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
        closed_at INTEGER CHECK (closed_at IS NULL OR closed_at >= created_at),
        CHECK ((status = 'closed') = (closed_at IS NOT NULL))
    )
    """,
    """
    ALTER TABLE sessions ADD COLUMN task_id TEXT
        REFERENCES tasks(task_id) ON DELETE RESTRICT
    """,
    """
    ALTER TABLE launch_intents ADD COLUMN task_id TEXT
        REFERENCES tasks(task_id) ON DELETE RESTRICT
    """,
    """
    CREATE INDEX tasks_project_status_recent
        ON tasks(project_id, status, pinned DESC, updated_at DESC)
    """,
    """
    CREATE INDEX sessions_task_recent
        ON sessions(task_id, last_observed_at DESC)
    """,
    """
    CREATE UNIQUE INDEX launch_intents_one_pending_per_task
        ON launch_intents(task_id)
        WHERE task_id IS NOT NULL AND state IN (
            'reserved', 'surface_ready', 'waiting_for_client', 'provider_started'
        )
    """,
    """
    CREATE TRIGGER tasks_checkout_matches_identity_insert
    BEFORE INSERT ON tasks
    WHEN NEW.checkout_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM checkouts AS checkout
        JOIN project_repositories AS membership
          ON membership.repository_id = checkout.repository_id
        WHERE checkout.checkout_id = NEW.checkout_id
          AND checkout.host_id = NEW.host_id
          AND membership.project_id = NEW.project_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'task checkout does not match project and host');
    END
    """,
    """
    CREATE TRIGGER tasks_checkout_matches_identity_update
    BEFORE UPDATE OF project_id, checkout_id, host_id ON tasks
    WHEN NEW.checkout_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM checkouts AS checkout
        JOIN project_repositories AS membership
          ON membership.repository_id = checkout.repository_id
        WHERE checkout.checkout_id = NEW.checkout_id
          AND checkout.host_id = NEW.host_id
          AND membership.project_id = NEW.project_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'task checkout does not match project and host');
    END
    """,
    """
    CREATE TRIGGER tasks_worktree_claim_insert
    BEFORE INSERT ON tasks
    WHEN NEW.status = 'open' AND NEW.checkout_id IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM checkouts
          WHERE checkout_id = NEW.checkout_id AND kind = 'worktree'
      )
      AND EXISTS (
          SELECT 1 FROM tasks
          WHERE checkout_id = NEW.checkout_id AND status = 'open'
      )
    BEGIN
        SELECT RAISE(ABORT, 'worktree already belongs to an open task');
    END
    """,
    """
    CREATE TRIGGER tasks_worktree_claim_update
    BEFORE UPDATE OF status, checkout_id ON tasks
    WHEN NEW.status = 'open' AND NEW.checkout_id IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM checkouts
          WHERE checkout_id = NEW.checkout_id AND kind = 'worktree'
      )
      AND EXISTS (
          SELECT 1 FROM tasks
          WHERE checkout_id = NEW.checkout_id AND status = 'open'
            AND task_id != NEW.task_id
      )
    BEGIN
        SELECT RAISE(ABORT, 'worktree already belongs to an open task');
    END
    """,
    """
    CREATE TRIGGER sessions_task_matches_identity_insert
    BEFORE INSERT ON sessions
    WHEN NEW.task_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM tasks AS task
        WHERE task.task_id = NEW.task_id
          AND task.host_id = NEW.host_id
          AND task.project_id = NEW.project_id
          AND task.checkout_id IS NEW.checkout_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'session task does not match project and checkout');
    END
    """,
    """
    CREATE TRIGGER sessions_task_matches_identity_update
    BEFORE UPDATE OF task_id, host_id, project_id, checkout_id ON sessions
    WHEN NEW.task_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM tasks AS task
        WHERE task.task_id = NEW.task_id
          AND task.host_id = NEW.host_id
          AND task.project_id = NEW.project_id
          AND task.checkout_id IS NEW.checkout_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'session task does not match project and checkout');
    END
    """,
    """
    CREATE TRIGGER launch_intents_task_matches_identity_insert
    BEFORE INSERT ON launch_intents
    WHEN NEW.task_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM tasks AS task
        WHERE task.task_id = NEW.task_id
          AND task.host_id = NEW.host_id
          AND task.project_id = NEW.project_id
          AND task.checkout_id IS NEW.checkout_id
          AND task.status = 'open'
    )
    BEGIN
        SELECT RAISE(ABORT, 'launch task does not match open task context');
    END
    """,
    """
    CREATE TRIGGER tasks_current_session_matches_insert
    BEFORE INSERT ON tasks
    WHEN NEW.current_session_key IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM sessions AS session
        WHERE session.session_key = NEW.current_session_key
          AND session.task_id = NEW.task_id
          AND session.host_id = NEW.host_id
          AND session.project_id = NEW.project_id
          AND session.checkout_id IS NEW.checkout_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'task current session does not match task context');
    END
    """,
    """
    CREATE TRIGGER tasks_current_session_matches_update
    BEFORE UPDATE OF current_session_key, host_id, project_id, checkout_id ON tasks
    WHEN NEW.current_session_key IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM sessions AS session
        WHERE session.session_key = NEW.current_session_key
          AND session.task_id = NEW.task_id
          AND session.host_id = NEW.host_id
          AND session.project_id = NEW.project_id
          AND session.checkout_id IS NEW.checkout_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'task current session does not match task context');
    END
    """,
)
