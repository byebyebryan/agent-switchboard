"""Fast host-local lifecycle event ingestion with no provider I/O."""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from typing import BinaryIO, Final

from .hooks import normalize_codex_event, read_hook_json
from .paths import database_path, load_or_create_host_id
from .storage import HookIngestionResult, Registry

HOOK_BUSY_TIMEOUT_MS: Final = 250


def ingest_local_event(
    provider: str,
    stream: BinaryIO,
    *,
    environment: Mapping[str, str] | None = None,
    entry_ns: int | None = None,
) -> HookIngestionResult:
    """Normalize stdin and perform one local SQLite ingestion transaction."""

    if provider != "codex":
        raise ValueError("event ingestion is not implemented for this provider")
    payload = read_hook_json(stream)
    try:
        normalized = normalize_codex_event(
            payload,
            os.environ if environment is None else environment,
            entry_ns=entry_ns,
        )
    finally:
        # The raw provider object may contain prompts, transcripts, or tool
        # payloads. Destroy our mutable copy as soon as allowlist normalization
        # has completed, before any filesystem or database work begins.
        if isinstance(payload, dict):
            payload.clear()
        del payload
    host_id = load_or_create_host_id()
    with Registry(database_path(), busy_timeout_ms=HOOK_BUSY_TIMEOUT_MS) as registry:
        return registry.ingest_hook_event(
            normalized.storage_mapping(host_id),
            host_display_name=socket.gethostname(),
        )


__all__ = ["HOOK_BUSY_TIMEOUT_MS", "ingest_local_event"]
