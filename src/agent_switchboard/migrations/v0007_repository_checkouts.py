"""Replace project locations with repository memberships and checkouts."""

from __future__ import annotations

VERSION = 7
NAME = "repository_checkouts"
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
    "PRAGMA legacy_alter_table = ON",
    "ALTER TABLE projects RENAME TO projects_v6",
    f"""
    CREATE TABLE projects (
        project_id TEXT PRIMARY KEY CHECK ({_UUID_CHECK.format(column="project_id")}),
        name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
        aliases_json TEXT NOT NULL DEFAULT '[]'
            CHECK (json_valid(aliases_json) AND json_type(aliases_json) = 'array'),
        default_provider TEXT
            CHECK (default_provider IS NULL OR default_provider IN ('codex', 'claude')),
        default_transport TEXT
            CHECK (default_transport IS NULL OR default_transport = 'tmux'),
        declared INTEGER NOT NULL DEFAULT 1 CHECK (declared IN (0, 1)),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at)
    )
    """,
    """
    INSERT INTO projects(
        project_id, name, aliases_json, default_provider, default_transport,
        declared, created_at, updated_at
    )
    SELECT project_id, name, aliases_json, default_provider, default_transport,
           declared, created_at, updated_at
    FROM projects_v6
    """,
    f"""
    CREATE TABLE repositories (
        repository_id TEXT PRIMARY KEY CHECK (
            {_UUID_CHECK.format(column="repository_id")}
        ),
        name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
        kind TEXT NOT NULL CHECK (kind IN ('git', 'directory')),
        kind_provisional INTEGER NOT NULL DEFAULT 0
            CHECK (kind_provisional IN (0, 1)),
        context_sources_json TEXT NOT NULL DEFAULT '[]'
            CHECK (
                json_valid(context_sources_json)
                AND json_type(context_sources_json) = 'array'
            ),
        declared INTEGER NOT NULL DEFAULT 1 CHECK (declared IN (0, 1)),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at)
    )
    """,
    """
    INSERT INTO repositories(
        repository_id, name, kind, kind_provisional, context_sources_json, declared,
        created_at, updated_at
    )
    SELECT project_id, name, 'git', 1, context_sources_json, declared,
           created_at, updated_at
    FROM projects_v6
    """,
    """
    CREATE TABLE project_repositories (
        project_id TEXT NOT NULL
            REFERENCES projects(project_id) ON DELETE RESTRICT,
        repository_id TEXT NOT NULL
            REFERENCES repositories(repository_id) ON DELETE RESTRICT,
        is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
        PRIMARY KEY (project_id, repository_id)
    )
    """,
    """
    INSERT INTO project_repositories(
        project_id, repository_id, is_primary, created_at, updated_at
    )
    SELECT project_id, project_id, 1, created_at, updated_at
    FROM projects_v6
    """,
    """
    CREATE UNIQUE INDEX project_repositories_one_primary
        ON project_repositories(project_id) WHERE is_primary = 1
    """,
    "DROP TABLE projects_v6",
    "PRAGMA legacy_alter_table = OFF",
    "ALTER TABLE project_locations RENAME TO checkouts",
    "ALTER TABLE checkouts RENAME COLUMN location_id TO checkout_id",
    "ALTER TABLE checkouts RENAME COLUMN project_id TO repository_id",
    "ALTER TABLE sessions RENAME COLUMN location_id TO checkout_id",
    "ALTER TABLE launch_intents RENAME COLUMN location_id TO checkout_id",
    "PRAGMA legacy_alter_table = ON",
    "ALTER TABLE checkouts RENAME TO checkouts_v6",
    f"""
    CREATE TABLE checkouts (
        checkout_id TEXT PRIMARY KEY CHECK (
            {_UUID_CHECK.format(column="checkout_id")}
        ),
        repository_id TEXT NOT NULL
            REFERENCES repositories(repository_id) ON DELETE RESTRICT,
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        path TEXT NOT NULL CHECK (length(path) BETWEEN 1 AND 4096),
        kind TEXT NOT NULL CHECK (kind IN ('main', 'worktree', 'directory')),
        display_name TEXT CHECK (
            display_name IS NULL OR length(display_name) <= 256
        ),
        branch TEXT CHECK (branch IS NULL OR length(branch) <= 1024),
        head_oid TEXT CHECK (head_oid IS NULL OR length(head_oid) <= 1024),
        provider_override TEXT CHECK (
            provider_override IS NULL OR provider_override IN ('codex', 'claude')
        ),
        transport_override TEXT CHECK (
            transport_override IS NULL OR transport_override = 'tmux'
        ),
        is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
        declared INTEGER NOT NULL DEFAULT 1 CHECK (declared IN (0, 1)),
        present INTEGER NOT NULL DEFAULT 1 CHECK (present IN (0, 1)),
        git_common_dir TEXT CHECK (
            git_common_dir IS NULL OR length(git_common_dir) <= 4096
        ),
        git_dir TEXT CHECK (git_dir IS NULL OR length(git_dir) <= 4096),
        last_observed_at INTEGER CHECK (
            last_observed_at IS NULL OR last_observed_at >= 0
        ),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
        UNIQUE (host_id, path)
    )
    """,
    """
    INSERT INTO checkouts(
        checkout_id, repository_id, host_id, path, kind, display_name,
        provider_override, transport_override, is_default, declared, present,
        last_observed_at, created_at, updated_at
    )
    SELECT checkout_id, repository_id, host_id, path,
           CASE WHEN is_default = 1 THEN 'main' ELSE 'worktree' END,
           display_name, provider_override, transport_override, is_default,
           declared, 1, last_observed_at, created_at, updated_at
    FROM checkouts_v6
    """,
    "DROP TABLE checkouts_v6",
    "PRAGMA legacy_alter_table = OFF",
    """
    CREATE UNIQUE INDEX checkouts_one_declared_default
        ON checkouts(repository_id, host_id)
        WHERE declared = 1 AND is_default = 1
    """,
    """
    CREATE INDEX checkouts_declared_path
        ON checkouts(host_id, declared, path)
    """,
    "DROP TRIGGER IF EXISTS sessions_location_matches_identity_insert",
    "DROP TRIGGER IF EXISTS sessions_location_matches_identity_update",
    "DROP TRIGGER IF EXISTS launch_intents_location_matches_identity_insert",
    """
    CREATE TRIGGER sessions_checkout_matches_identity_insert
    BEFORE INSERT ON sessions
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
        SELECT RAISE(ABORT, 'session checkout does not match project and host');
    END
    """,
    """
    CREATE TRIGGER sessions_checkout_matches_identity_update
    BEFORE UPDATE OF project_id, checkout_id, host_id ON sessions
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
        SELECT RAISE(ABORT, 'session checkout does not match project and host');
    END
    """,
    """
    CREATE TRIGGER launch_intents_checkout_matches_identity_insert
    BEFORE INSERT ON launch_intents
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
        SELECT RAISE(ABORT, 'launch checkout does not match project and host');
    END
    """,
    """
    UPDATE registry_metadata
    SET value = '2'
    WHERE key = 'protocol_version'
    """,
)
