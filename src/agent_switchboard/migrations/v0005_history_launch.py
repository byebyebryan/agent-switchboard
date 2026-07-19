"""Add a first-class unbound Claude history-picker launch action."""

from __future__ import annotations

from . import v0001_initial

VERSION = 5
NAME = "history_launch"
REQUIRES_FOREIGN_KEYS_OFF = True


def _statement(prefix: str) -> str:
    matches = [
        statement
        for statement in v0001_initial.STATEMENTS
        if statement.lstrip().startswith(prefix)
    ]
    if len(matches) != 1:  # pragma: no cover - import-time schema assertion
        raise RuntimeError(f"expected one initial schema statement for {prefix!r}")
    return matches[0]


def _replace_once(value: str, old: str, new: str) -> str:
    if value.count(old) != 1:  # pragma: no cover - import-time schema assertion
        raise RuntimeError(f"expected one initial schema fragment for {old!r}")
    return value.replace(old, new)


_CREATE = _statement("CREATE TABLE launch_intents")
_CREATE = _replace_once(
    _CREATE,
    "action IN ('new', 'resume', 'attach', 'manage')",
    "action IN ('new', 'resume', 'attach', 'history', 'manage')",
)
_CREATE = _replace_once(
    _CREATE,
    "            OR (action IN ('resume', 'attach') AND target_session_key "
    "IS NOT NULL)\n"
    "            OR (action = 'manage' AND target_session_key IS NULL)",
    "            OR (action IN ('resume', 'attach') AND target_session_key "
    "IS NOT NULL)\n"
    """            OR (action = 'history'
                AND provider = 'claude'
                AND project_id IS NOT NULL AND location_id IS NOT NULL
                AND cwd IS NOT NULL AND source_handoff_id IS NULL
                AND (target_session_key IS NULL OR state = 'bound'))
            OR (action = 'manage' AND target_session_key IS NULL)""",
)

_TRIGGER_PREFIXES = (
    "CREATE TRIGGER launch_intents_initial_state_is_reserved",
    "CREATE UNIQUE INDEX launch_intents_one_pending_target",
    "CREATE UNIQUE INDEX launch_intents_one_active_manager",
    "CREATE INDEX launch_intents_expiry",
    "CREATE TRIGGER launch_intents_location_matches_identity_insert",
    "CREATE TRIGGER launch_intents_target_matches_identity_insert",
    "CREATE TRIGGER launch_intents_request_is_immutable",
    "CREATE TRIGGER launch_intents_target_binding_only",
    "CREATE TRIGGER launch_intents_surface_binding_only",
    "CREATE TRIGGER launch_intents_target_matches_identity_update",
    "CREATE TRIGGER launch_intents_valid_transition",
    "CREATE TRIGGER launch_intents_failed_requires_code_insert",
    "CREATE TRIGGER launch_intents_failed_requires_code_update",
)
_RECREATED = tuple(_statement(prefix) for prefix in _TRIGGER_PREFIXES)
_RECREATED = tuple(
    _replace_once(
        statement,
        "OLD.action = 'new'",
        "OLD.action IN ('new', 'history')",
    )
    if statement.lstrip().startswith(
        "CREATE TRIGGER launch_intents_target_binding_only"
    )
    else statement
    for statement in _RECREATED
)

_DROP_NAMES = (
    "launch_intents_initial_state_is_reserved",
    "launch_intents_one_pending_target",
    "launch_intents_one_active_manager",
    "launch_intents_expiry",
    "launch_intents_location_matches_identity_insert",
    "launch_intents_target_matches_identity_insert",
    "launch_intents_request_is_immutable",
    "launch_intents_target_binding_only",
    "launch_intents_surface_binding_only",
    "launch_intents_target_matches_identity_update",
    "launch_intents_valid_transition",
    "launch_intents_failed_requires_code_insert",
    "launch_intents_failed_requires_code_update",
)

_COLUMNS = (
    "launch_id, request_id, request_fingerprint, host_id, provider, action, "
    "project_id, location_id, cwd, source_handoff_id, target_session_key, "
    "surface_id, transport, state, lease_owner, capability_hash, created_at, "
    "updated_at, expires_at, failure_code, failure_detail"
)

STATEMENTS = (
    *(
        f"DROP TRIGGER IF EXISTS {name}"
        for name in _DROP_NAMES
        if "one_" not in name and name != "launch_intents_expiry"
    ),
    *(
        f"DROP INDEX IF EXISTS {name}"
        for name in _DROP_NAMES
        if "one_" in name or name == "launch_intents_expiry"
    ),
    "PRAGMA legacy_alter_table = ON",
    "ALTER TABLE launch_intents RENAME TO launch_intents_v4",
    _CREATE,
    f"INSERT INTO launch_intents({_COLUMNS}) SELECT {_COLUMNS} FROM launch_intents_v4",
    "DROP TABLE launch_intents_v4",
    *_RECREATED,
    "PRAGMA legacy_alter_table = OFF",
)
