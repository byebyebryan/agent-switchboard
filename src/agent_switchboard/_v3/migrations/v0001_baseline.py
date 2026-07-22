"""Fresh Phase 6 registry baseline; no 0.2 table or migration is inherited."""

from __future__ import annotations

VERSION = 1
NAME = "phase6_baseline"


def _uuid(column: str) -> str:
    return f"""
        length({column}) = 36
        AND {column} = lower({column})
        AND {column} GLOB '????????-????-????-????-????????????'
        AND length(replace({column}, '-', '')) = 32
        AND {column} NOT GLOB '*[^0-9a-f-]*'
        AND replace({column}, '-', '') != '00000000000000000000000000000000'
    """


STATEMENTS = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version > 0),
        name TEXT NOT NULL UNIQUE,
        applied_at INTEGER NOT NULL CHECK (applied_at >= 0)
    )
    """,
    f"""
    CREATE TABLE hosts (
        host_id TEXT PRIMARY KEY CHECK ({_uuid("host_id")}),
        display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 256),
        is_local INTEGER NOT NULL CHECK (is_local IN (0, 1)),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at)
    )
    """,
    "CREATE UNIQUE INDEX hosts_one_local ON hosts(is_local) WHERE is_local = 1",
    f"""
    CREATE TABLE registry_metadata (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        protocol_version INTEGER NOT NULL CHECK (protocol_version = 1),
        generation_id TEXT NOT NULL UNIQUE CHECK ({_uuid("generation_id")}),
        local_host_id TEXT NOT NULL UNIQUE
            REFERENCES hosts(host_id) ON DELETE RESTRICT
            CHECK ({_uuid("local_host_id")}),
        activation_state TEXT NOT NULL
            CHECK (activation_state IN ('cutover_staged', 'committed')),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        committed_at INTEGER CHECK (
            (activation_state = 'committed' AND committed_at >= created_at)
            OR (activation_state = 'cutover_staged' AND committed_at IS NULL)
        )
    )
    """,
    f"""
    CREATE TABLE projects (
        project_id TEXT PRIMARY KEY CHECK ({_uuid("project_id")}),
        name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
        aliases_json TEXT NOT NULL DEFAULT '[]'
            CHECK (json_valid(aliases_json) AND json_type(aliases_json) = 'array'),
        default_provider TEXT
            CHECK (default_provider IS NULL OR default_provider IN ('codex', 'claude')),
        default_transport TEXT NOT NULL DEFAULT 'tmux'
            CHECK (default_transport = 'tmux'),
        task_push TEXT CHECK (task_push IS NULL OR task_push IN ('conservative', 'off')),
        complete_return TEXT
            CHECK (complete_return IS NULL OR complete_return IN ('synthesize', 'handoff')),
        declared INTEGER NOT NULL DEFAULT 1 CHECK (declared IN (0, 1)),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at)
    )
    """,
    f"""
    CREATE TABLE repositories (
        repository_id TEXT PRIMARY KEY CHECK ({_uuid("repository_id")}),
        name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
        kind TEXT NOT NULL CHECK (kind IN ('git', 'directory')),
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
    CREATE TABLE project_repositories (
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        repository_id TEXT NOT NULL
            REFERENCES repositories(repository_id) ON DELETE RESTRICT,
        is_primary INTEGER NOT NULL CHECK (is_primary IN (0, 1)),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
        PRIMARY KEY (project_id, repository_id)
    )
    """,
    """
    CREATE UNIQUE INDEX project_repositories_one_primary
        ON project_repositories(project_id) WHERE is_primary = 1
    """,
    f"""
    CREATE TABLE checkouts (
        checkout_id TEXT PRIMARY KEY CHECK ({_uuid("checkout_id")}),
        repository_id TEXT NOT NULL
            REFERENCES repositories(repository_id) ON DELETE RESTRICT,
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        path TEXT NOT NULL CHECK (length(path) BETWEEN 1 AND 4096),
        kind TEXT NOT NULL CHECK (kind IN ('main', 'worktree', 'directory')),
        display_name TEXT CHECK (display_name IS NULL OR length(display_name) <= 256),
        provider_override TEXT
            CHECK (provider_override IS NULL OR provider_override IN ('codex', 'claude')),
        is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
        declared INTEGER NOT NULL DEFAULT 1 CHECK (declared IN (0, 1)),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
        UNIQUE (host_id, path)
    )
    """,
    """
    CREATE UNIQUE INDEX checkouts_one_default
        ON checkouts(repository_id, host_id)
        WHERE declared = 1 AND is_default = 1
    """,
    f"""
    CREATE TABLE provider_sessions (
        session_key TEXT PRIMARY KEY CHECK (length(session_key) BETWEEN 1 AND 512),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        provider TEXT NOT NULL CHECK (provider IN ('codex', 'claude')),
        provider_session_id TEXT NOT NULL CHECK ({_uuid("provider_session_id")}),
        project_id TEXT REFERENCES projects(project_id) ON DELETE RESTRICT,
        checkout_id TEXT REFERENCES checkouts(checkout_id) ON DELETE RESTRICT,
        name TEXT CHECK (name IS NULL OR length(name) <= 512),
        purpose TEXT CHECK (purpose IS NULL OR length(purpose) <= 4096),
        pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
        runtime_presence TEXT NOT NULL DEFAULT 'unknown'
            CHECK (runtime_presence IN ('live', 'stopped', 'unknown')),
        resumability TEXT NOT NULL DEFAULT 'unknown'
            CHECK (resumability IN ('resumable', 'missing', 'unknown')),
        activity TEXT NOT NULL DEFAULT 'unknown'
            CHECK (activity IN ('working', 'needs_input', 'ready', 'completed', 'unknown')),
        activity_reason TEXT NOT NULL DEFAULT 'unknown'
            CHECK (
                activity_reason IN (
                    'permission', 'question', 'elicitation', 'turn_complete',
                    'provider_complete', 'error', 'unknown'
                )
            ),
        created_at INTEGER CHECK (created_at IS NULL OR created_at >= 0),
        provider_updated_at INTEGER
            CHECK (provider_updated_at IS NULL OR provider_updated_at >= 0),
        last_observed_at INTEGER NOT NULL CHECK (last_observed_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
        CHECK (session_key = host_id || ':' || provider || ':' || provider_session_id),
        CHECK (checkout_id IS NULL OR project_id IS NOT NULL),
        UNIQUE (host_id, provider, provider_session_id)
    )
    """,
    """
    CREATE TRIGGER provider_sessions_checkout_matches_insert
    BEFORE INSERT ON provider_sessions
    WHEN NEW.checkout_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM checkouts AS checkout
        JOIN project_repositories AS membership
          ON membership.repository_id = checkout.repository_id
        WHERE checkout.checkout_id = NEW.checkout_id
          AND checkout.host_id = NEW.host_id
          AND membership.project_id = NEW.project_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'provider session checkout does not match host/project');
    END
    """,
    f"""
    CREATE TABLE session_handoffs (
        handoff_id TEXT PRIMARY KEY CHECK ({_uuid("handoff_id")}),
        session_key TEXT NOT NULL
            REFERENCES provider_sessions(session_key) ON DELETE RESTRICT,
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 65536),
        next_action TEXT NOT NULL CHECK (length(next_action) BETWEEN 1 AND 65536),
        source TEXT NOT NULL CHECK (source IN ('user', 'agent', 'imported')),
        source_host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        content_hash TEXT NOT NULL CHECK (
            length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'
        ),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        UNIQUE (session_key, sequence)
    )
    """,
    """
    CREATE TRIGGER session_handoffs_immutable
    BEFORE UPDATE ON session_handoffs
    BEGIN SELECT RAISE(ABORT, 'session handoff is immutable'); END
    """,
    f"""
    CREATE TABLE work_contexts (
        work_context_id TEXT PRIMARY KEY CHECK ({_uuid("work_context_id")}),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        checkout_id TEXT NOT NULL REFERENCES checkouts(checkout_id) ON DELETE RESTRICT,
        claim_state TEXT NOT NULL CHECK (claim_state IN ('released', 'held', 'blocked')),
        claim_generation INTEGER NOT NULL CHECK (claim_generation >= 0),
        foreground_frame_id TEXT REFERENCES frames(frame_id) ON DELETE RESTRICT
            CHECK (foreground_frame_id IS NULL OR ({_uuid("foreground_frame_id")})),
        background_state TEXT NOT NULL
            CHECK (background_state IN ('safe', 'known', 'uncertain')),
        acquired_at INTEGER CHECK (acquired_at IS NULL OR acquired_at >= 0),
        released_at INTEGER CHECK (released_at IS NULL OR released_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
        CHECK (
            (claim_state = 'held' AND foreground_frame_id IS NOT NULL)
            OR (claim_state != 'held' AND foreground_frame_id IS NULL)
        ),
        UNIQUE (host_id, project_id, checkout_id)
    )
    """,
    """
    CREATE UNIQUE INDEX work_contexts_one_held_checkout
        ON work_contexts(checkout_id) WHERE claim_state = 'held'
    """,
    """
    CREATE TRIGGER work_contexts_checkout_matches_insert
    BEFORE INSERT ON work_contexts
    WHEN NOT EXISTS (
        SELECT 1 FROM checkouts AS checkout
        JOIN project_repositories AS membership
          ON membership.repository_id = checkout.repository_id
        WHERE checkout.checkout_id = NEW.checkout_id
          AND checkout.host_id = NEW.host_id
          AND membership.project_id = NEW.project_id
    )
    BEGIN SELECT RAISE(ABORT, 'WorkContext checkout does not match host/project'); END
    """,
    f"""
    CREATE TABLE frames (
        frame_id TEXT PRIMARY KEY CHECK ({_uuid("frame_id")}),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        role TEXT NOT NULL CHECK (role IN ('workspace', 'task')),
        parent_frame_id TEXT REFERENCES frames(frame_id) ON DELETE RESTRICT,
        work_context_id TEXT NOT NULL
            REFERENCES work_contexts(work_context_id) ON DELETE RESTRICT,
        title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 256),
        purpose TEXT CHECK (purpose IS NULL OR length(purpose) <= 4096),
        preferred_provider TEXT
            CHECK (preferred_provider IS NULL OR preferred_provider IN ('codex', 'claude')),
        lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('open', 'closing', 'closed')),
        close_reason TEXT
            CHECK (close_reason IS NULL OR close_reason IN ('completed', 'dismissed')),
        current_session_key TEXT
            REFERENCES provider_sessions(session_key) ON DELETE RESTRICT,
        created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent', 'cutover')),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
        CHECK (
            (role = 'workspace' AND parent_frame_id IS NULL)
            OR (role = 'task' AND parent_frame_id IS NOT NULL)
        ),
        CHECK (
            (lifecycle_state = 'closed' AND close_reason IS NOT NULL)
            OR (lifecycle_state != 'closed' AND close_reason IS NULL)
        )
    )
    """,
    """
    CREATE UNIQUE INDEX frames_one_workspace
        ON frames(host_id, project_id) WHERE role = 'workspace'
    """,
    """
    CREATE TRIGGER frames_context_matches_insert
    BEFORE INSERT ON frames
    WHEN NOT EXISTS (
        SELECT 1 FROM work_contexts AS context
        WHERE context.work_context_id = NEW.work_context_id
          AND context.host_id = NEW.host_id
          AND context.project_id = NEW.project_id
    )
    BEGIN SELECT RAISE(ABORT, 'frame context does not match host/project'); END
    """,
    """
    CREATE TRIGGER frames_parent_matches_insert
    BEFORE INSERT ON frames
    WHEN NEW.parent_frame_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM frames AS parent
        WHERE parent.frame_id = NEW.parent_frame_id
          AND parent.host_id = NEW.host_id
          AND parent.project_id = NEW.project_id
          AND parent.work_context_id = NEW.work_context_id
    )
    BEGIN SELECT RAISE(ABORT, 'frame parent does not match context'); END
    """,
    """
    CREATE TRIGGER frames_acyclic_parent_update
    BEFORE UPDATE OF parent_frame_id ON frames
    WHEN NEW.parent_frame_id IS NOT NULL AND EXISTS (
        WITH RECURSIVE ancestors(frame_id) AS (
            SELECT NEW.parent_frame_id
            UNION ALL
            SELECT frame.parent_frame_id
            FROM frames AS frame
            JOIN ancestors ON frame.frame_id = ancestors.frame_id
            WHERE frame.parent_frame_id IS NOT NULL
        )
        SELECT 1 FROM ancestors WHERE frame_id = NEW.frame_id
    )
    BEGIN SELECT RAISE(ABORT, 'frame parent cycle'); END
    """,
    f"""
    CREATE TABLE frame_sessions (
        frame_session_id TEXT PRIMARY KEY CHECK ({_uuid("frame_session_id")}),
        frame_id TEXT NOT NULL REFERENCES frames(frame_id) ON DELETE RESTRICT,
        session_key TEXT NOT NULL
            REFERENCES provider_sessions(session_key) ON DELETE RESTRICT UNIQUE,
        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
        membership_reason TEXT NOT NULL
            CHECK (membership_reason IN ('started', 'resumed', 'rollover', 'recovery', 'cutover')),
        joined_at INTEGER NOT NULL CHECK (joined_at >= 0),
        UNIQUE (frame_id, ordinal)
    )
    """,
    """
    CREATE TRIGGER frame_sessions_identity_insert
    BEFORE INSERT ON frame_sessions
    WHEN NOT EXISTS (
        SELECT 1 FROM frames AS frame
        JOIN provider_sessions AS session ON session.session_key = NEW.session_key
        WHERE frame.frame_id = NEW.frame_id
          AND frame.host_id = session.host_id
          AND frame.project_id = session.project_id
    )
    BEGIN SELECT RAISE(ABORT, 'frame session identity mismatch'); END
    """,
    """
    CREATE TRIGGER frames_current_membership_update
    BEFORE UPDATE OF current_session_key ON frames
    WHEN NEW.current_session_key IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM frame_sessions
        WHERE frame_id = NEW.frame_id AND session_key = NEW.current_session_key
    )
    BEGIN SELECT RAISE(ABORT, 'current session is not a frame member'); END
    """,
    """
    CREATE TRIGGER frames_current_membership_insert
    BEFORE INSERT ON frames
    WHEN NEW.current_session_key IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM frame_sessions
        WHERE frame_id = NEW.frame_id AND session_key = NEW.current_session_key
    )
    BEGIN SELECT RAISE(ABORT, 'current session is not a frame member'); END
    """,
    """
    CREATE TRIGGER work_contexts_foreground_update
    BEFORE UPDATE OF foreground_frame_id, claim_state ON work_contexts
    WHEN NEW.foreground_frame_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM frames
        WHERE frame_id = NEW.foreground_frame_id
          AND work_context_id = NEW.work_context_id
          AND lifecycle_state = 'open'
    )
    BEGIN SELECT RAISE(ABORT, 'foreground frame is not an open context member'); END
    """,
    f"""
    CREATE TABLE tmux_servers (
        tmux_server_id TEXT PRIMARY KEY CHECK ({_uuid("tmux_server_id")}),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        socket_path TEXT NOT NULL CHECK (length(socket_path) BETWEEN 1 AND 4096),
        server_pid INTEGER NOT NULL CHECK (server_pid > 0),
        server_start_time INTEGER NOT NULL CHECK (server_start_time >= 0),
        observed_at INTEGER NOT NULL CHECK (observed_at >= 0),
        UNIQUE (host_id, socket_path, server_pid, server_start_time)
    )
    """,
    f"""
    CREATE TABLE user_views (
        view_id TEXT PRIMARY KEY CHECK ({_uuid("view_id")}),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        mode TEXT NOT NULL CHECK (mode IN ('navigator', 'direct')),
        active_frame_id TEXT REFERENCES frames(frame_id) ON DELETE RESTRICT,
        state TEXT NOT NULL CHECK (state IN ('ready', 'transitioning', 'degraded', 'retired')),
        revision INTEGER NOT NULL CHECK (revision >= 0),
        desktop_token TEXT NOT NULL UNIQUE CHECK (length(desktop_token) BETWEEN 1 AND 256),
        tmux_server_id TEXT REFERENCES tmux_servers(tmux_server_id) ON DELETE RESTRICT,
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        last_attached_at INTEGER CHECK (last_attached_at IS NULL OR last_attached_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
        CHECK (
            (state IN ('ready', 'transitioning') AND active_frame_id IS NOT NULL)
            OR state IN ('degraded', 'retired')
        )
    )
    """,
    """
    CREATE TRIGGER user_views_active_frame_insert
    BEFORE INSERT ON user_views
    WHEN NEW.active_frame_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM frames
        WHERE frame_id = NEW.active_frame_id AND host_id = NEW.host_id
    )
    BEGIN SELECT RAISE(ABORT, 'view active frame does not match host'); END
    """,
    f"""
    CREATE TABLE request_records (
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        request_id TEXT NOT NULL CHECK ({_uuid("request_id")}),
        operation TEXT NOT NULL CHECK (length(operation) BETWEEN 1 AND 64),
        semantic_fingerprint TEXT NOT NULL CHECK (
            length(semantic_fingerprint) = 64
            AND semantic_fingerprint NOT GLOB '*[^0-9a-f]*'
        ),
        state TEXT NOT NULL CHECK (state IN ('prepared', 'completed', 'failed')),
        result_type TEXT CHECK (result_type IS NULL OR length(result_type) <= 64),
        result_id TEXT CHECK (result_id IS NULL OR length(result_id) <= 512),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        completed_at INTEGER CHECK (completed_at IS NULL OR completed_at >= created_at),
        PRIMARY KEY (host_id, request_id)
    )
    """,
    f"""
    CREATE TABLE launch_intents (
        launch_id TEXT PRIMARY KEY CHECK ({_uuid("launch_id")}),
        request_id TEXT NOT NULL CHECK ({_uuid("request_id")}),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        frame_id TEXT NOT NULL REFERENCES frames(frame_id) ON DELETE RESTRICT,
        provider TEXT NOT NULL CHECK (provider IN ('codex', 'claude')),
        action TEXT NOT NULL CHECK (action IN ('new', 'resume')),
        target_session_key TEXT
            REFERENCES provider_sessions(session_key) ON DELETE RESTRICT,
        state TEXT NOT NULL
            CHECK (state IN ('planned', 'authorized', 'started', 'bound', 'failed', 'superseded')),
        failure_code TEXT CHECK (failure_code IS NULL OR length(failure_code) <= 64),
        failure_message TEXT CHECK (failure_message IS NULL OR length(failure_message) <= 1024),
        failure_retryable INTEGER CHECK (failure_retryable IS NULL OR failure_retryable IN (0, 1)),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
        CHECK (
            (action = 'resume' AND target_session_key IS NOT NULL)
            OR (action = 'new' AND target_session_key IS NULL)
        ),
        CHECK (
            (state = 'failed' AND failure_code IS NOT NULL AND failure_message IS NOT NULL)
            OR (state != 'failed' AND failure_code IS NULL AND failure_message IS NULL
                AND failure_retryable IS NULL)
        ),
        UNIQUE (host_id, request_id)
    )
    """,
    """
    CREATE UNIQUE INDEX launch_intents_one_active_frame
        ON launch_intents(frame_id)
        WHERE state IN ('planned', 'authorized', 'started')
    """,
    """
    CREATE UNIQUE INDEX launch_intents_one_active_session
        ON launch_intents(target_session_key)
        WHERE target_session_key IS NOT NULL
          AND state IN ('planned', 'authorized', 'started')
    """,
    f"""
    CREATE TABLE surfaces (
        surface_id TEXT PRIMARY KEY CHECK ({_uuid("surface_id")}),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        provider TEXT NOT NULL CHECK (provider IN ('codex', 'claude')),
        session_key TEXT REFERENCES provider_sessions(session_key) ON DELETE RESTRICT,
        launch_id TEXT NOT NULL UNIQUE
            REFERENCES launch_intents(launch_id) ON DELETE RESTRICT,
        lifecycle_state TEXT NOT NULL
            CHECK (lifecycle_state IN ('planned', 'live', 'dead', 'orphaned', 'retired')),
        tmux_server_id TEXT REFERENCES tmux_servers(tmux_server_id) ON DELETE RESTRICT,
        pane_id TEXT CHECK (pane_id IS NULL OR length(pane_id) <= 64),
        process_id INTEGER CHECK (process_id IS NULL OR process_id > 0),
        process_birth_id TEXT
            CHECK (process_birth_id IS NULL OR length(process_birth_id) <= 256),
        metadata_generation INTEGER NOT NULL CHECK (metadata_generation >= 0),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
        retired_at INTEGER CHECK (retired_at IS NULL OR retired_at >= created_at),
        CHECK ((tmux_server_id IS NULL) = (pane_id IS NULL))
    )
    """,
    """
    CREATE UNIQUE INDEX surfaces_one_live_session
        ON surfaces(session_key)
        WHERE session_key IS NOT NULL AND lifecycle_state = 'live'
    """,
    f"""
    CREATE TABLE frame_placements (
        placement_id TEXT PRIMARY KEY CHECK ({_uuid("placement_id")}),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        view_id TEXT NOT NULL REFERENCES user_views(view_id) ON DELETE RESTRICT,
        frame_id TEXT NOT NULL REFERENCES frames(frame_id) ON DELETE RESTRICT,
        surface_id TEXT REFERENCES surfaces(surface_id) ON DELETE RESTRICT,
        state TEXT NOT NULL
            CHECK (state IN ('active', 'parked', 'staged', 'stopped_affinity', 'orphaned')),
        generation INTEGER NOT NULL CHECK (generation >= 0),
        last_focused_at INTEGER CHECK (last_focused_at IS NULL OR last_focused_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= 0)
    )
    """,
    """
    CREATE UNIQUE INDEX frame_placements_one_owner
        ON frame_placements(frame_id)
        WHERE state IN ('active', 'parked', 'staged', 'stopped_affinity')
    """,
    """
    CREATE UNIQUE INDEX frame_placements_one_active_view
        ON frame_placements(view_id) WHERE state = 'active'
    """,
    """
    CREATE UNIQUE INDEX frame_placements_one_surface
        ON frame_placements(surface_id) WHERE surface_id IS NOT NULL
    """,
    """
    CREATE TRIGGER frame_placements_identity_insert
    BEFORE INSERT ON frame_placements
    WHEN NOT EXISTS (
        SELECT 1 FROM user_views AS view
        JOIN frames AS frame ON frame.frame_id = NEW.frame_id
        WHERE view.view_id = NEW.view_id
          AND view.host_id = NEW.host_id
          AND frame.host_id = NEW.host_id
    )
    BEGIN SELECT RAISE(ABORT, 'placement identity mismatch'); END
    """,
    f"""
    CREATE TABLE view_transitions (
        transition_id TEXT PRIMARY KEY CHECK ({_uuid("transition_id")}),
        request_id TEXT NOT NULL CHECK ({_uuid("request_id")}),
        request_fingerprint TEXT NOT NULL CHECK (
            length(request_fingerprint) = 64
            AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
        ),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        view_id TEXT NOT NULL REFERENCES user_views(view_id) ON DELETE RESTRICT,
        kind TEXT NOT NULL
            CHECK (kind IN ('focus', 'push', 'back', 'complete_return', 'human_close', 'mode', 'recover')),
        source_frame_id TEXT REFERENCES frames(frame_id) ON DELETE RESTRICT,
        target_frame_id TEXT NOT NULL REFERENCES frames(frame_id) ON DELETE RESTRICT,
        work_context_id TEXT REFERENCES work_contexts(work_context_id) ON DELETE RESTRICT,
        expected_view_revision INTEGER NOT NULL CHECK (expected_view_revision >= 0),
        expected_claim_generation INTEGER
            CHECK (expected_claim_generation IS NULL OR expected_claim_generation >= 0),
        state TEXT NOT NULL
            CHECK (state IN ('prepared', 'executing', 'presented', 'awaiting_claim',
                             'settling', 'completed', 'cancelled', 'superseded', 'failed')),
        execution_owner TEXT CHECK (execution_owner IS NULL OR length(execution_owner) <= 128),
        lease_expires_at INTEGER CHECK (lease_expires_at IS NULL OR lease_expires_at >= 0),
        transport_phase TEXT NOT NULL
            CHECK (transport_phase IN ('intent', 'moved', 'inspected', 'committed', 'rolled_back')),
        failure_code TEXT CHECK (failure_code IS NULL OR length(failure_code) <= 64),
        failure_message TEXT CHECK (failure_message IS NULL OR length(failure_message) <= 1024),
        failure_retryable INTEGER CHECK (failure_retryable IS NULL OR failure_retryable IN (0, 1)),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
        UNIQUE (host_id, request_id)
    )
    """,
    """
    CREATE UNIQUE INDEX view_transitions_one_nonterminal
        ON view_transitions(view_id)
        WHERE state IN ('prepared', 'executing', 'presented', 'awaiting_claim', 'settling')
    """,
    f"""
    CREATE TABLE transition_briefs (
        brief_id TEXT PRIMARY KEY CHECK ({_uuid("brief_id")}),
        transition_id TEXT NOT NULL UNIQUE
            REFERENCES view_transitions(transition_id) ON DELETE RESTRICT,
        source_frame_id TEXT NOT NULL REFERENCES frames(frame_id) ON DELETE RESTRICT,
        source_session_key TEXT NOT NULL
            REFERENCES provider_sessions(session_key) ON DELETE RESTRICT,
        target_frame_id TEXT NOT NULL REFERENCES frames(frame_id) ON DELETE RESTRICT,
        brief TEXT NOT NULL CHECK (length(brief) BETWEEN 1 AND 65536),
        content_hash TEXT NOT NULL CHECK (
            length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'
        ),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        first_claimed_at INTEGER CHECK (first_claimed_at IS NULL OR first_claimed_at >= created_at)
    )
    """,
    """
    CREATE TRIGGER transition_briefs_content_immutable
    BEFORE UPDATE OF transition_id, source_frame_id, source_session_key,
                     target_frame_id, brief, content_hash, created_at
    ON transition_briefs
    BEGIN SELECT RAISE(ABORT, 'transition brief content is immutable'); END
    """,
    f"""
    CREATE TABLE completion_handoffs (
        handoff_id TEXT PRIMARY KEY CHECK ({_uuid("handoff_id")}),
        transition_id TEXT NOT NULL UNIQUE
            REFERENCES view_transitions(transition_id) ON DELETE RESTRICT,
        source_frame_id TEXT NOT NULL REFERENCES frames(frame_id) ON DELETE RESTRICT,
        source_session_key TEXT NOT NULL
            REFERENCES provider_sessions(session_key) ON DELETE RESTRICT,
        target_frame_id TEXT NOT NULL REFERENCES frames(frame_id) ON DELETE RESTRICT,
        summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 65536),
        next_action TEXT NOT NULL CHECK (length(next_action) BETWEEN 1 AND 65536),
        content_hash TEXT NOT NULL CHECK (
            length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'
        ),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        first_claimed_at INTEGER CHECK (first_claimed_at IS NULL OR first_claimed_at >= created_at)
    )
    """,
    """
    CREATE TRIGGER completion_handoffs_content_immutable
    BEFORE UPDATE OF transition_id, source_frame_id, source_session_key,
                     target_frame_id, summary, next_action, content_hash, created_at
    ON completion_handoffs
    BEGIN SELECT RAISE(ABORT, 'completion handoff content is immutable'); END
    """,
    f"""
    CREATE TABLE control_turns (
        control_turn_id TEXT PRIMARY KEY CHECK ({_uuid("control_turn_id")}),
        transition_id TEXT NOT NULL UNIQUE
            REFERENCES view_transitions(transition_id) ON DELETE RESTRICT,
        target_frame_id TEXT NOT NULL REFERENCES frames(frame_id) ON DELETE RESTRICT,
        target_session_key TEXT NOT NULL
            REFERENCES provider_sessions(session_key) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK (kind IN ('claim_brief', 'claim_handoff')),
        template_version TEXT NOT NULL CHECK (template_version = 'control.claim.v1'),
        transport TEXT NOT NULL CHECK (transport IN ('live_input', 'resume_initial')),
        state TEXT NOT NULL
            CHECK (state IN ('prepared', 'submitted', 'observed', 'claimed', 'settled',
                             'uncertain', 'failed', 'superseded')),
        submission_count INTEGER NOT NULL CHECK (submission_count IN (0, 1)),
        submitted_at INTEGER CHECK (submitted_at IS NULL OR submitted_at >= 0),
        observed_prompt_id TEXT
            CHECK (observed_prompt_id IS NULL OR length(observed_prompt_id) <= 256),
        claimed_at INTEGER CHECK (claimed_at IS NULL OR claimed_at >= 0),
        settled_at INTEGER CHECK (settled_at IS NULL OR settled_at >= 0),
        failure_code TEXT CHECK (failure_code IS NULL OR length(failure_code) <= 64),
        failure_message TEXT CHECK (failure_message IS NULL OR length(failure_message) <= 1024),
        failure_retryable INTEGER CHECK (failure_retryable IS NULL OR failure_retryable IN (0, 1))
    )
    """,
    f"""
    CREATE TABLE recoveries (
        recovery_id TEXT PRIMARY KEY CHECK ({_uuid("recovery_id")}),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 64),
        subject_type TEXT NOT NULL CHECK (length(subject_type) BETWEEN 1 AND 64),
        subject_id TEXT NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 512),
        actionability TEXT NOT NULL CHECK (actionability IN ('safe_auto', 'open_view', 'manual')),
        state TEXT NOT NULL CHECK (state IN ('open', 'resolved', 'dismissed')),
        bounded_explanation TEXT NOT NULL
            CHECK (length(bounded_explanation) BETWEEN 1 AND 1024),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= created_at)
    )
    """,
    """
    CREATE UNIQUE INDEX recoveries_one_open_subject
        ON recoveries(host_id, kind, subject_type, subject_id) WHERE state = 'open'
    """,
    f"""
    CREATE TABLE desktop_attachment_leases (
        lease_id TEXT PRIMARY KEY CHECK ({_uuid("lease_id")}),
        view_id TEXT NOT NULL REFERENCES user_views(view_id) ON DELETE RESTRICT,
        request_id TEXT NOT NULL CHECK ({_uuid("request_id")}),
        state TEXT NOT NULL CHECK (state IN ('offered', 'claimed', 'expired')),
        expires_at INTEGER NOT NULL CHECK (expires_at >= 0),
        UNIQUE (view_id, request_id)
    )
    """,
    """
    CREATE UNIQUE INDEX desktop_attachment_leases_one_offered
        ON desktop_attachment_leases(view_id) WHERE state = 'offered'
    """,
    f"""
    CREATE TABLE agent_capabilities (
        capability_id TEXT PRIMARY KEY CHECK ({_uuid("capability_id")}),
        capability_digest TEXT NOT NULL UNIQUE CHECK (
            length(capability_digest) = 64
            AND capability_digest NOT GLOB '*[^0-9a-f]*'
        ),
        host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
        view_id TEXT NOT NULL REFERENCES user_views(view_id) ON DELETE RESTRICT,
        frame_id TEXT NOT NULL REFERENCES frames(frame_id) ON DELETE RESTRICT,
        session_key TEXT REFERENCES provider_sessions(session_key) ON DELETE RESTRICT,
        surface_id TEXT NOT NULL REFERENCES surfaces(surface_id) ON DELETE RESTRICT,
        launch_id TEXT NOT NULL REFERENCES launch_intents(launch_id) ON DELETE RESTRICT,
        tmux_server_id TEXT REFERENCES tmux_servers(tmux_server_id) ON DELETE RESTRICT,
        pane_id TEXT CHECK (pane_id IS NULL OR length(pane_id) <= 64),
        placement_generation INTEGER NOT NULL CHECK (placement_generation >= 0),
        issued_at INTEGER NOT NULL CHECK (issued_at >= 0),
        expires_at INTEGER NOT NULL CHECK (expires_at > issued_at),
        revoked_at INTEGER CHECK (revoked_at IS NULL OR revoked_at >= issued_at),
        CHECK ((tmux_server_id IS NULL) = (pane_id IS NULL)),
        UNIQUE (surface_id, launch_id)
    )
    """,
    f"""
    CREATE TABLE host_state_cache (
        remote_name TEXT PRIMARY KEY CHECK (length(remote_name) BETWEEN 1 AND 64),
        host_id TEXT NOT NULL UNIQUE CHECK ({_uuid("host_id")}),
        state_json TEXT NOT NULL CHECK (length(state_json) BETWEEN 2 AND 8388608),
        content_hash TEXT NOT NULL CHECK (
            length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'
        ),
        observed_at INTEGER NOT NULL CHECK (observed_at >= 0),
        received_at INTEGER NOT NULL CHECK (received_at >= 0),
        last_attempt_at INTEGER NOT NULL CHECK (last_attempt_at >= 0),
        reachability TEXT NOT NULL CHECK (reachability IN ('online', 'offline', 'unknown')),
        error_code TEXT CHECK (error_code IS NULL OR length(error_code) <= 64),
        error_message TEXT CHECK (error_message IS NULL OR length(error_message) <= 1024),
        error_retryable INTEGER CHECK (error_retryable IS NULL OR error_retryable IN (0, 1)),
        CHECK (
            (error_code IS NULL AND error_message IS NULL AND error_retryable IS NULL)
            OR (error_code IS NOT NULL AND error_message IS NOT NULL
                AND error_retryable IS NOT NULL)
        )
    )
    """,
)

__all__ = ["NAME", "STATEMENTS", "VERSION"]
