"""Track durable name provenance and bound host runtime-tail reads."""

from __future__ import annotations

VERSION = 3
NAME = "name_provenance_runtime_index"

STATEMENTS = (
    """
    ALTER TABLE sessions ADD COLUMN provider_name TEXT
        CHECK (provider_name IS NULL OR length(provider_name) <= 512)
    """,
    """
    ALTER TABLE sessions ADD COLUMN name_source TEXT NOT NULL DEFAULT 'unknown'
        CHECK (name_source IN ('unknown', 'provider', 'curated'))
    """,
    """
    UPDATE sessions
    SET provider_name = name
    WHERE metadata_source = 'provider'
    """,
    """
    UPDATE sessions
    SET name_source = 'curated'
    WHERE name IS NOT NULL
    """,
    """
    CREATE INDEX runtime_observations_host_recent
        ON runtime_observations(host_id, observed_at DESC, observation_id DESC)
    """,
)
