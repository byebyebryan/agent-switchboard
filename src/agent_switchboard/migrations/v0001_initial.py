"""Initial local registry schema.

The migration intentionally stores normalized registry metadata only.  Provider
transcripts, prompts, credentials, and raw hook payloads do not belong here.
"""

from __future__ import annotations

VERSION = 1
NAME = "initial_registry"

STATEMENTS = (
    """
    CREATE TABLE registry_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at INTEGER NOT NULL CHECK (updated_at >= 0)
    )
    """,
    """
    CREATE TABLE hosts (
        host_id TEXT PRIMARY KEY CHECK (
            length(host_id) = 36
            AND host_id = lower(host_id)
            AND substr(host_id, 9, 1) = '-'
            AND substr(host_id, 14, 1) = '-'
            AND substr(host_id, 19, 1) = '-'
            AND substr(host_id, 24, 1) = '-'
            AND length(replace(host_id, '-', '')) = 32
            AND host_id NOT GLOB '*[^0-9a-f-]*'
            AND replace(host_id, '-', '') != printf('%032d', 0)
        ),
        display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 256),
        is_local INTEGER NOT NULL DEFAULT 0 CHECK (is_local IN (0, 1)),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at)
    )
    """,
    """
    CREATE UNIQUE INDEX hosts_one_local
        ON hosts(is_local) WHERE is_local = 1
    """,
    """
    CREATE TABLE projects (
        project_id TEXT PRIMARY KEY CHECK (
            length(project_id) = 36
            AND project_id = lower(project_id)
            AND project_id GLOB '????????-????-????-????-????????????'
            AND length(replace(project_id, '-', '')) = 32
            AND project_id NOT GLOB '*[^0-9a-f-]*'
            AND replace(project_id, '-', '') != printf('%032d', 0)
        ),
        name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
        aliases_json TEXT NOT NULL DEFAULT '[]'
            CHECK (json_valid(aliases_json) AND json_type(aliases_json) = 'array'),
        default_provider TEXT
            CHECK (default_provider IS NULL OR default_provider IN ('codex', 'claude')),
        default_transport TEXT
            CHECK (default_transport IS NULL OR default_transport = 'tmux'),
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
    CREATE TABLE project_locations (
        location_id TEXT PRIMARY KEY CHECK (
            length(location_id) = 36
            AND location_id = lower(location_id)
            AND location_id GLOB '????????-????-????-????-????????????'
            AND length(replace(location_id, '-', '')) = 32
            AND location_id NOT GLOB '*[^0-9a-f-]*'
            AND replace(location_id, '-', '') != printf('%032d', 0)
        ),
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        path TEXT NOT NULL CHECK (length(path) BETWEEN 1 AND 4096),
        display_name TEXT CHECK (display_name IS NULL OR length(display_name) <= 256),
        repository_identity TEXT
            CHECK (repository_identity IS NULL OR length(repository_identity) <= 2048),
        provider_override TEXT
            CHECK (
                provider_override IS NULL
                OR provider_override IN ('codex', 'claude')
            ),
        transport_override TEXT
            CHECK (transport_override IS NULL OR transport_override = 'tmux'),
        is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
        declared INTEGER NOT NULL DEFAULT 1 CHECK (declared IN (0, 1)),
        last_observed_at INTEGER
            CHECK (last_observed_at IS NULL OR last_observed_at >= 0),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
        UNIQUE (host_id, path)
    )
    """,
    """
    CREATE UNIQUE INDEX project_locations_one_declared_default
        ON project_locations(project_id, host_id)
        WHERE declared = 1 AND is_default = 1
    """,
    """
    CREATE INDEX project_locations_declared_path
        ON project_locations(host_id, declared, path)
    """,
    """
    CREATE TABLE sessions (
        session_key TEXT PRIMARY KEY CHECK (length(session_key) BETWEEN 1 AND 512),
        project_id TEXT REFERENCES projects(project_id) ON DELETE RESTRICT,
        location_id TEXT REFERENCES project_locations(location_id) ON DELETE RESTRICT,
        provider TEXT NOT NULL CHECK (provider IN ('codex', 'claude')),
        provider_session_id TEXT NOT NULL CHECK (
            length(provider_session_id) = 36
            AND provider_session_id = lower(provider_session_id)
            AND substr(provider_session_id, 9, 1) = '-'
            AND substr(provider_session_id, 14, 1) = '-'
            AND substr(provider_session_id, 19, 1) = '-'
            AND substr(provider_session_id, 24, 1) = '-'
            AND length(replace(provider_session_id, '-', '')) = 32
            AND provider_session_id NOT GLOB '*[^0-9a-f-]*'
            AND replace(provider_session_id, '-', '') != printf('%032d', 0)
        ),
        name TEXT CHECK (name IS NULL OR length(name) <= 512),
        purpose TEXT CHECK (purpose IS NULL OR length(purpose) <= 4096),
        cwd TEXT CHECK (cwd IS NULL OR length(cwd) <= 4096),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        created_at INTEGER CHECK (created_at IS NULL OR created_at >= 0),
        provider_updated_at INTEGER
            CHECK (provider_updated_at IS NULL OR provider_updated_at >= 0),
        last_activity_at INTEGER
            CHECK (last_activity_at IS NULL OR last_activity_at >= 0),
        first_observed_at INTEGER NOT NULL CHECK (first_observed_at >= 0),
        last_observed_at INTEGER NOT NULL CHECK (last_observed_at >= first_observed_at),
        runtime_presence TEXT NOT NULL DEFAULT 'unknown'
            CHECK (runtime_presence IN ('live', 'stopped', 'unknown')),
        resumability TEXT NOT NULL DEFAULT 'unknown'
            CHECK (resumability IN ('resumable', 'missing', 'unknown')),
        activity TEXT NOT NULL DEFAULT 'unknown'
            CHECK (
                activity IN (
                    'working', 'needs_input', 'ready', 'completed', 'unknown'
                )
            ),
        activity_reason TEXT NOT NULL DEFAULT 'unknown'
            CHECK (
                activity_reason IN (
                    'permission', 'question', 'elicitation', 'turn_complete',
                    'provider_complete', 'error', 'unknown'
                )
            ),
        attachment TEXT NOT NULL DEFAULT 'unknown'
            CHECK (attachment IN ('attached', 'detached', 'none', 'unknown')),
        runtime_pid INTEGER CHECK (runtime_pid IS NULL OR runtime_pid > 0),
        provider_runtime_id TEXT
            CHECK (provider_runtime_id IS NULL OR length(provider_runtime_id) <= 256),
        tmux_session TEXT CHECK (tmux_session IS NULL OR length(tmux_session) <= 256),
        tmux_window TEXT CHECK (tmux_window IS NULL OR length(tmux_window) <= 256),
        tmux_pane TEXT CHECK (tmux_pane IS NULL OR length(tmux_pane) <= 256),
        runtime_observed_at INTEGER
            CHECK (runtime_observed_at IS NULL OR runtime_observed_at >= 0),
        surface_id TEXT REFERENCES surfaces(surface_id) ON DELETE SET NULL,
        metadata_source TEXT NOT NULL DEFAULT 'unknown'
            CHECK (length(metadata_source) BETWEEN 1 AND 64),
        state_confidence TEXT NOT NULL DEFAULT 'unknown'
            CHECK (state_confidence IN ('confirmed', 'inferred', 'unknown')),
        state_observed_at INTEGER
            CHECK (state_observed_at IS NULL OR state_observed_at >= 0),
        latest_handoff_id TEXT REFERENCES handoffs(handoff_id) ON DELETE RESTRICT,
        wrapped_at INTEGER CHECK (wrapped_at IS NULL OR wrapped_at >= 0),
        continued_from_handoff_id TEXT
            REFERENCES handoffs(handoff_id) ON DELETE RESTRICT,
        pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
        CHECK (location_id IS NULL OR project_id IS NOT NULL),
        CHECK (
            session_key = host_id || ':' || provider || ':' || provider_session_id
        ),
        UNIQUE (host_id, provider, provider_session_id)
    )
    """,
    """
    CREATE INDEX sessions_project_recent
        ON sessions(project_id, last_observed_at DESC)
    """,
    """
    CREATE INDEX sessions_host_provider
        ON sessions(host_id, provider, provider_session_id)
    """,
    """
    CREATE UNIQUE INDEX sessions_one_surface
        ON sessions(surface_id) WHERE surface_id IS NOT NULL
    """,
    """
    CREATE TRIGGER sessions_location_matches_identity_insert
    BEFORE INSERT ON sessions
    WHEN NEW.location_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM project_locations AS location
        WHERE location.location_id = NEW.location_id
          AND location.project_id = NEW.project_id
          AND location.host_id = NEW.host_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'session location does not match project and host');
    END
    """,
    """
    CREATE TRIGGER sessions_location_matches_identity_update
    BEFORE UPDATE OF project_id, location_id, host_id ON sessions
    WHEN NEW.location_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM project_locations AS location
        WHERE location.location_id = NEW.location_id
          AND location.project_id = NEW.project_id
          AND location.host_id = NEW.host_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'session location does not match project and host');
    END
    """,
    """
    CREATE TRIGGER sessions_surface_matches_identity_insert
    BEFORE INSERT ON sessions
    WHEN NEW.surface_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM surfaces AS surface
        WHERE surface.surface_id = NEW.surface_id
          AND surface.host_id = NEW.host_id
          AND surface.provider = NEW.provider
          AND surface.role = 'session'
          AND (
              surface.current_session_key IS NULL
              OR surface.current_session_key = NEW.session_key
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'session surface does not match identity');
    END
    """,
    """
    CREATE TRIGGER sessions_surface_matches_identity_update
    BEFORE UPDATE OF surface_id, host_id, provider ON sessions
    WHEN NEW.surface_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM surfaces AS surface
        WHERE surface.surface_id = NEW.surface_id
          AND surface.host_id = NEW.host_id
          AND surface.provider = NEW.provider
          AND surface.role = 'session'
          AND (
              surface.current_session_key IS NULL
              OR surface.current_session_key = NEW.session_key
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'session surface does not match identity');
    END
    """,
    """
    CREATE TABLE handoffs (
        handoff_id TEXT PRIMARY KEY CHECK (
            length(handoff_id) = 36
            AND handoff_id = lower(handoff_id)
            AND handoff_id GLOB '????????-????-????-????-????????????'
            AND length(replace(handoff_id, '-', '')) = 32
            AND handoff_id NOT GLOB '*[^0-9a-f-]*'
            AND replace(handoff_id, '-', '') != printf('%032d', 0)
        ),
        session_key TEXT NOT NULL CHECK (length(session_key) BETWEEN 1 AND 512),
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        summary TEXT NOT NULL CHECK (
            length(CAST(summary AS BLOB)) BETWEEN 1 AND 65536
        ),
        next_action TEXT NOT NULL CHECK (
            length(CAST(next_action AS BLOB)) BETWEEN 1 AND 65536
        ),
        source TEXT NOT NULL CHECK (source IN ('user', 'agent', 'imported')),
        source_host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        content_hash TEXT NOT NULL CHECK (
            length(content_hash) = 64
            AND content_hash NOT GLOB '*[^0-9a-f]*'
        ),
        UNIQUE (session_key, sequence),
        UNIQUE (session_key, content_hash)
    )
    """,
    """
    CREATE INDEX handoffs_session_recent
        ON handoffs(session_key, sequence DESC)
    """,
    """
    CREATE TRIGGER handoffs_append_only_update
    BEFORE UPDATE ON handoffs
    BEGIN
        SELECT RAISE(ABORT, 'handoffs are append-only');
    END
    """,
    """
    CREATE TRIGGER handoffs_append_only_delete
    BEFORE DELETE ON handoffs
    BEGIN
        SELECT RAISE(ABORT, 'handoffs are append-only');
    END
    """,
    """
    CREATE TRIGGER handoffs_local_session_exists
    BEFORE INSERT ON handoffs
    WHEN NEW.source != 'imported' AND NOT EXISTS (
        SELECT 1 FROM sessions AS session
        WHERE session.session_key = NEW.session_key
    )
    BEGIN
        SELECT RAISE(ABORT, 'local handoff requires a registry session');
    END
    """,
    """
    CREATE TABLE launch_intents (
        launch_id TEXT PRIMARY KEY CHECK (
            length(launch_id) = 36
            AND launch_id = lower(launch_id)
            AND launch_id GLOB '????????-????-????-????-????????????'
            AND length(replace(launch_id, '-', '')) = 32
            AND launch_id NOT GLOB '*[^0-9a-f-]*'
            AND replace(launch_id, '-', '') != printf('%032d', 0)
        ),
        request_id TEXT NOT NULL CHECK (
            length(request_id) = 36
            AND request_id = lower(request_id)
            AND request_id GLOB '????????-????-????-????-????????????'
            AND length(replace(request_id, '-', '')) = 32
            AND request_id NOT GLOB '*[^0-9a-f-]*'
            AND replace(request_id, '-', '') != printf('%032d', 0)
        ),
        request_fingerprint TEXT NOT NULL CHECK (
            length(request_fingerprint) = 64
            AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
        ),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        provider TEXT NOT NULL CHECK (provider IN ('codex', 'claude')),
        action TEXT NOT NULL CHECK (action IN ('new', 'resume', 'attach', 'manage')),
        project_id TEXT REFERENCES projects(project_id) ON DELETE RESTRICT,
        location_id TEXT REFERENCES project_locations(location_id) ON DELETE RESTRICT,
        cwd TEXT CHECK (cwd IS NULL OR length(cwd) <= 4096),
        source_handoff_id TEXT REFERENCES handoffs(handoff_id) ON DELETE RESTRICT,
        target_session_key TEXT REFERENCES sessions(session_key) ON DELETE RESTRICT,
        surface_id TEXT REFERENCES surfaces(surface_id) ON DELETE SET NULL,
        transport TEXT NOT NULL CHECK (transport = 'tmux'),
        state TEXT NOT NULL CHECK (
            state IN (
                'reserved', 'surface_ready', 'waiting_for_client',
                'provider_started', 'bound', 'manager_ready', 'failed', 'expired'
            )
        ),
        lease_owner TEXT CHECK (lease_owner IS NULL OR length(lease_owner) <= 256),
        capability_hash TEXT NOT NULL CHECK (
            length(capability_hash) = 64
            AND capability_hash NOT GLOB '*[^0-9a-f]*'
        ),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
        expires_at INTEGER NOT NULL CHECK (expires_at > created_at),
        failure_code TEXT CHECK (failure_code IS NULL OR length(failure_code) <= 128),
        failure_detail TEXT
            CHECK (failure_detail IS NULL OR length(failure_detail) <= 2048),
        CHECK (location_id IS NULL OR project_id IS NOT NULL),
        CHECK (source_handoff_id IS NULL OR action = 'new'),
        CHECK (
            (action = 'new'
                AND project_id IS NOT NULL AND location_id IS NOT NULL
                AND cwd IS NOT NULL
                AND (target_session_key IS NULL OR state = 'bound'))
            OR (action IN ('resume', 'attach') AND target_session_key IS NOT NULL)
            OR (action = 'manage' AND target_session_key IS NULL)
        ),
        CHECK (
            state NOT IN (
                'reserved', 'surface_ready', 'waiting_for_client',
                'provider_started', 'manager_ready'
            )
            OR (lease_owner IS NOT NULL AND expires_at IS NOT NULL)
        ),
        CHECK ((state = 'failed') = (failure_code IS NOT NULL)),
        CHECK (
            state NOT IN ('bound', 'failed', 'expired') OR lease_owner IS NULL
        ),
        CHECK (
            state != 'expired'
            OR (expires_at IS NOT NULL AND updated_at >= expires_at)
        ),
        CHECK (state != 'bound' OR target_session_key IS NOT NULL),
        CHECK (state != 'bound' OR action != 'manage'),
        CHECK (state != 'manager_ready' OR action = 'manage'),
        CHECK (state != 'reserved' OR surface_id IS NULL),
        CHECK (
            action != 'manage'
            OR (project_id IS NULL AND location_id IS NULL
                AND cwd IS NULL AND source_handoff_id IS NULL)
        ),
        CHECK (
            state NOT IN (
                'surface_ready', 'waiting_for_client', 'provider_started',
                'bound', 'manager_ready'
            )
            OR surface_id IS NOT NULL
        ),
        UNIQUE (host_id, request_id)
    )
    """,
    """
    CREATE TRIGGER launch_intents_initial_state_is_reserved
    BEFORE INSERT ON launch_intents
    WHEN NEW.state != 'reserved'
    BEGIN
        SELECT RAISE(ABORT, 'launch intent must be inserted as reserved');
    END
    """,
    """
    CREATE UNIQUE INDEX launch_intents_one_pending_target
        ON launch_intents(target_session_key)
        WHERE target_session_key IS NOT NULL
          AND state IN (
              'reserved', 'surface_ready', 'waiting_for_client', 'provider_started'
          )
    """,
    """
    CREATE UNIQUE INDEX launch_intents_one_active_manager
        ON launch_intents(host_id, provider)
        WHERE action = 'manage'
          AND state IN (
              'reserved', 'surface_ready', 'waiting_for_client',
              'provider_started', 'manager_ready'
          )
    """,
    """
    CREATE INDEX launch_intents_expiry
        ON launch_intents(state, expires_at)
    """,
    """
    CREATE TRIGGER launch_intents_location_matches_identity_insert
    BEFORE INSERT ON launch_intents
    WHEN NEW.location_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM project_locations AS location
        WHERE location.location_id = NEW.location_id
          AND location.project_id = NEW.project_id
          AND location.host_id = NEW.host_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'launch location does not match project and host');
    END
    """,
    """
    CREATE TRIGGER launch_intents_target_matches_identity_insert
    BEFORE INSERT ON launch_intents
    WHEN NEW.target_session_key IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM sessions AS session
        WHERE session.session_key = NEW.target_session_key
          AND session.host_id = NEW.host_id
          AND session.provider = NEW.provider
    )
    BEGIN
        SELECT RAISE(ABORT, 'launch target does not match host and provider');
    END
    """,
    """
    CREATE TRIGGER launch_intents_request_is_immutable
    BEFORE UPDATE OF
        request_id, request_fingerprint, host_id, provider, action, project_id,
        location_id, cwd, source_handoff_id, transport, capability_hash
    ON launch_intents
    BEGIN
        SELECT RAISE(ABORT, 'launch request fields are immutable');
    END
    """,
    """
    CREATE TRIGGER launch_intents_target_binding_only
    BEFORE UPDATE OF target_session_key ON launch_intents
    WHEN NEW.target_session_key IS NOT OLD.target_session_key AND NOT (
        OLD.action = 'new'
        AND OLD.target_session_key IS NULL
        AND NEW.target_session_key IS NOT NULL
        AND NEW.state = 'bound'
    )
    BEGIN
        SELECT RAISE(ABORT, 'launch target is immutable outside new-session binding');
    END
    """,
    """
    CREATE TRIGGER launch_intents_surface_binding_only
    BEFORE UPDATE OF surface_id ON launch_intents
    WHEN NEW.surface_id IS NOT OLD.surface_id AND NOT (
        OLD.state = 'reserved'
        AND OLD.surface_id IS NULL
        AND NEW.surface_id IS NOT NULL
        AND NEW.state = 'surface_ready'
    )
    BEGIN
        SELECT RAISE(ABORT, 'launch surface is immutable outside surface readiness');
    END
    """,
    """
    CREATE TRIGGER launch_intents_target_matches_identity_update
    BEFORE UPDATE OF target_session_key ON launch_intents
    WHEN NEW.target_session_key IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM sessions AS session
        WHERE session.session_key = NEW.target_session_key
          AND session.host_id = NEW.host_id
          AND session.provider = NEW.provider
    )
    BEGIN
        SELECT RAISE(ABORT, 'launch target does not match host and provider');
    END
    """,
    """
    CREATE TRIGGER launch_intents_valid_transition
    BEFORE UPDATE OF state ON launch_intents
    WHEN NEW.state != OLD.state AND NOT (
        (OLD.state = 'reserved'
            AND NEW.state IN ('surface_ready', 'failed', 'expired'))
        OR (OLD.state = 'surface_ready'
            AND NEW.state IN ('waiting_for_client', 'failed', 'expired'))
        OR (OLD.state = 'waiting_for_client'
            AND NEW.state IN ('provider_started', 'failed', 'expired'))
        OR (OLD.state = 'provider_started'
            AND NEW.state IN ('bound', 'manager_ready', 'failed', 'expired'))
        OR (OLD.state = 'manager_ready' AND NEW.state IN ('failed', 'expired'))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid launch state transition');
    END
    """,
    """
    CREATE TRIGGER launch_intents_failed_requires_code_insert
    BEFORE INSERT ON launch_intents
    WHEN NEW.state = 'failed' AND NEW.failure_code IS NULL
    BEGIN
        SELECT RAISE(ABORT, 'failed launch requires failure code');
    END
    """,
    """
    CREATE TRIGGER launch_intents_failed_requires_code_update
    BEFORE UPDATE OF state, failure_code ON launch_intents
    WHEN NEW.state = 'failed' AND NEW.failure_code IS NULL
    BEGIN
        SELECT RAISE(ABORT, 'failed launch requires failure code');
    END
    """,
    """
    CREATE TABLE surfaces (
        surface_id TEXT PRIMARY KEY CHECK (
            length(surface_id) = 36
            AND surface_id = lower(surface_id)
            AND surface_id GLOB '????????-????-????-????-????????????'
            AND length(replace(surface_id, '-', '')) = 32
            AND surface_id NOT GLOB '*[^0-9a-f-]*'
            AND replace(surface_id, '-', '') != printf('%032d', 0)
        ),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        provider TEXT NOT NULL CHECK (provider IN ('codex', 'claude')),
        transport TEXT NOT NULL CHECK (transport = 'tmux'),
        transport_locator TEXT NOT NULL
            CHECK (length(transport_locator) BETWEEN 1 AND 1024),
        workspace_id TEXT CHECK (workspace_id IS NULL OR length(workspace_id) <= 256),
        role TEXT NOT NULL CHECK (role IN ('session', 'provider_manager')),
        current_session_key TEXT REFERENCES sessions(session_key) ON DELETE SET NULL,
        binding_confidence TEXT NOT NULL DEFAULT 'unknown'
            CHECK (binding_confidence IN ('confirmed', 'unknown')),
        launch_id TEXT REFERENCES launch_intents(launch_id) ON DELETE SET NULL,
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        last_observed_at INTEGER NOT NULL CHECK (last_observed_at >= created_at),
        client_attached INTEGER NOT NULL DEFAULT 0 CHECK (client_attached IN (0, 1)),
        retired_at INTEGER CHECK (retired_at IS NULL OR retired_at >= created_at),
        CHECK (
            role != 'provider_manager'
            OR (current_session_key IS NULL AND binding_confidence = 'unknown')
        ),
        CHECK (
            binding_confidence != 'confirmed' OR current_session_key IS NOT NULL
        )
    )
    """,
    """
    CREATE UNIQUE INDEX surfaces_live_transport_locator
        ON surfaces(host_id, transport, transport_locator)
        WHERE retired_at IS NULL
    """,
    """
    CREATE UNIQUE INDEX surfaces_one_confirmed_binding
        ON surfaces(current_session_key)
        WHERE current_session_key IS NOT NULL
          AND binding_confidence = 'confirmed'
          AND retired_at IS NULL
    """,
    """
    CREATE UNIQUE INDEX surfaces_one_active_manager
        ON surfaces(host_id, provider)
        WHERE role = 'provider_manager' AND retired_at IS NULL
    """,
    """
    CREATE UNIQUE INDEX surfaces_one_per_launch
        ON surfaces(launch_id) WHERE launch_id IS NOT NULL
    """,
    """
    CREATE TRIGGER surfaces_session_matches_identity_insert
    BEFORE INSERT ON surfaces
    WHEN NEW.current_session_key IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM sessions AS session
        WHERE session.session_key = NEW.current_session_key
          AND session.host_id = NEW.host_id
          AND session.provider = NEW.provider
    )
    BEGIN
        SELECT RAISE(ABORT, 'surface session does not match host and provider');
    END
    """,
    """
    CREATE TRIGGER surfaces_session_matches_identity_update
    BEFORE UPDATE OF current_session_key, host_id, provider ON surfaces
    WHEN NEW.current_session_key IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM sessions AS session
        WHERE session.session_key = NEW.current_session_key
          AND session.host_id = NEW.host_id
          AND session.provider = NEW.provider
    )
    BEGIN
        SELECT RAISE(ABORT, 'surface session does not match host and provider');
    END
    """,
    """
    CREATE TRIGGER surfaces_launch_matches_role_insert
    BEFORE INSERT ON surfaces
    WHEN NEW.launch_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM launch_intents AS launch
        WHERE launch.launch_id = NEW.launch_id
          AND launch.host_id = NEW.host_id
          AND launch.provider = NEW.provider
          AND (
              (NEW.role = 'provider_manager' AND launch.action = 'manage')
              OR (NEW.role = 'session' AND launch.action != 'manage')
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'surface launch does not match identity and role');
    END
    """,
    """
    CREATE TRIGGER surfaces_launch_matches_role_update
    BEFORE UPDATE OF launch_id, host_id, provider, role ON surfaces
    WHEN NEW.launch_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM launch_intents AS launch
        WHERE launch.launch_id = NEW.launch_id
          AND launch.host_id = NEW.host_id
          AND launch.provider = NEW.provider
          AND (
              (NEW.role = 'provider_manager' AND launch.action = 'manage')
              OR (NEW.role = 'session' AND launch.action != 'manage')
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'surface launch does not match identity and role');
    END
    """,
    """
    CREATE TABLE runtime_observations (
        observation_id TEXT PRIMARY KEY
            CHECK (length(observation_id) BETWEEN 1 AND 128),
        observation_key TEXT NOT NULL
            CHECK (length(observation_key) BETWEEN 1 AND 256),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        provider TEXT NOT NULL CHECK (provider IN ('codex', 'claude')),
        session_key TEXT REFERENCES sessions(session_key) ON DELETE SET NULL,
        launch_id TEXT REFERENCES launch_intents(launch_id) ON DELETE SET NULL,
        source TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 64),
        source_priority INTEGER NOT NULL CHECK (source_priority >= 0),
        runtime_presence TEXT NOT NULL
            CHECK (runtime_presence IN ('live', 'stopped', 'unknown')),
        resumability TEXT NOT NULL
            CHECK (resumability IN ('resumable', 'missing', 'unknown')),
        activity TEXT NOT NULL
            CHECK (
                activity IN (
                    'working', 'needs_input', 'ready', 'completed', 'unknown'
                )
            ),
        activity_reason TEXT NOT NULL
            CHECK (
                activity_reason IN (
                    'permission', 'question', 'elicitation', 'turn_complete',
                    'provider_complete', 'error', 'unknown'
                )
            ),
        attachment TEXT NOT NULL
            CHECK (attachment IN ('attached', 'detached', 'none', 'unknown')),
        pid INTEGER CHECK (pid IS NULL OR pid > 0),
        provider_runtime_id TEXT
            CHECK (provider_runtime_id IS NULL OR length(provider_runtime_id) <= 256),
        tmux_session TEXT CHECK (tmux_session IS NULL OR length(tmux_session) <= 256),
        tmux_window TEXT CHECK (tmux_window IS NULL OR length(tmux_window) <= 256),
        tmux_pane TEXT CHECK (tmux_pane IS NULL OR length(tmux_pane) <= 256),
        observed_at INTEGER NOT NULL CHECK (observed_at >= 0),
        received_at INTEGER NOT NULL CHECK (received_at >= 0),
        payload_hash TEXT NOT NULL CHECK (
            length(payload_hash) = 64
            AND payload_hash NOT GLOB '*[^0-9a-f]*'
        ),
        UNIQUE (host_id, provider, observation_key)
    )
    """,
    """
    CREATE INDEX runtime_observations_session_recent
        ON runtime_observations(session_key, observed_at DESC)
    """,
    """
    CREATE TRIGGER runtime_observations_session_matches_identity
    BEFORE INSERT ON runtime_observations
    WHEN NEW.session_key IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM sessions AS session
        WHERE session.session_key = NEW.session_key
          AND session.host_id = NEW.host_id
          AND session.provider = NEW.provider
    )
    BEGIN
        SELECT RAISE(ABORT, 'runtime observation session does not match identity');
    END
    """,
    """
    CREATE TRIGGER runtime_observations_launch_matches_identity
    BEFORE INSERT ON runtime_observations
    WHEN NEW.launch_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM launch_intents AS launch
        WHERE launch.launch_id = NEW.launch_id
          AND launch.host_id = NEW.host_id
          AND launch.provider = NEW.provider
          AND (
              NEW.session_key IS NULL
              OR launch.target_session_key IS NULL
              OR launch.target_session_key = NEW.session_key
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'runtime observation launch does not match identity');
    END
    """,
    """
    CREATE TABLE events (
        event_id TEXT PRIMARY KEY CHECK (length(event_id) BETWEEN 1 AND 128),
        idempotency_key TEXT NOT NULL
            CHECK (length(idempotency_key) BETWEEN 1 AND 256),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        provider TEXT NOT NULL CHECK (provider IN ('codex', 'claude')),
        session_key TEXT REFERENCES sessions(session_key) ON DELETE SET NULL,
        launch_id TEXT REFERENCES launch_intents(launch_id) ON DELETE SET NULL,
        surface_id TEXT REFERENCES surfaces(surface_id) ON DELETE SET NULL,
        event_kind TEXT NOT NULL CHECK (length(event_kind) BETWEEN 1 AND 64),
        provider_turn_id TEXT
            CHECK (provider_turn_id IS NULL OR length(provider_turn_id) <= 256),
        source_priority INTEGER NOT NULL CHECK (source_priority >= 0),
        kind_priority INTEGER NOT NULL CHECK (kind_priority >= 0),
        observed_at INTEGER NOT NULL CHECK (observed_at >= 0),
        received_at INTEGER NOT NULL CHECK (received_at >= 0),
        payload_hash TEXT NOT NULL CHECK (
            length(payload_hash) = 64
            AND payload_hash NOT GLOB '*[^0-9a-f]*'
        ),
        diagnostic_code TEXT
            CHECK (diagnostic_code IS NULL OR length(diagnostic_code) <= 128),
        diagnostic_detail TEXT
            CHECK (diagnostic_detail IS NULL OR length(diagnostic_detail) <= 2048),
        UNIQUE (host_id, provider, idempotency_key)
    )
    """,
    """
    CREATE INDEX events_received_recent ON events(received_at DESC, event_id)
    """,
    """
    CREATE TRIGGER events_session_matches_identity
    BEFORE INSERT ON events
    WHEN NEW.session_key IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM sessions AS session
        WHERE session.session_key = NEW.session_key
          AND session.host_id = NEW.host_id
          AND session.provider = NEW.provider
    )
    BEGIN
        SELECT RAISE(ABORT, 'event session does not match identity');
    END
    """,
    """
    CREATE TRIGGER events_launch_matches_identity
    BEFORE INSERT ON events
    WHEN NEW.launch_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM launch_intents AS launch
        WHERE launch.launch_id = NEW.launch_id
          AND launch.host_id = NEW.host_id
          AND launch.provider = NEW.provider
          AND (
              NEW.session_key IS NULL
              OR launch.target_session_key IS NULL
              OR launch.target_session_key = NEW.session_key
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'event launch does not match identity');
    END
    """,
    """
    CREATE TRIGGER events_surface_matches_identity
    BEFORE INSERT ON events
    WHEN NEW.surface_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM surfaces AS surface
        WHERE surface.surface_id = NEW.surface_id
          AND surface.host_id = NEW.host_id
          AND surface.provider = NEW.provider
          AND (
              NEW.launch_id IS NULL
              OR surface.launch_id = NEW.launch_id
          )
          AND (
              NEW.session_key IS NULL
              OR (
                  (surface.current_session_key IS NULL
                      OR surface.current_session_key = NEW.session_key)
                  AND EXISTS (
                      SELECT 1
                      FROM sessions AS session
                      WHERE session.session_key = NEW.session_key
                        AND (session.surface_id IS NULL
                            OR session.surface_id = NEW.surface_id)
                  )
              )
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'event surface does not match identity');
    END
    """,
)
