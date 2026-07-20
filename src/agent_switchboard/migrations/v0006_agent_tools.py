"""Add session-scoped agent authorization and name attribution."""

from __future__ import annotations

VERSION = 6
NAME = "agent_tools"
REQUIRES_FOREIGN_KEYS_OFF = True

STATEMENTS = (
    """
    ALTER TABLE launch_intents ADD COLUMN agent_capability_hash TEXT
        CHECK (
            agent_capability_hash IS NULL
            OR (
                length(agent_capability_hash) = 64
                AND agent_capability_hash NOT GLOB '*[^0-9a-f]*'
            )
        )
    """,
    """
    CREATE UNIQUE INDEX launch_intents_agent_capability
        ON launch_intents(agent_capability_hash)
        WHERE agent_capability_hash IS NOT NULL
    """,
    """
    ALTER TABLE sessions ADD COLUMN name_actor TEXT
        CHECK (name_actor IS NULL OR name_actor IN ('user', 'agent'))
    """,
)
