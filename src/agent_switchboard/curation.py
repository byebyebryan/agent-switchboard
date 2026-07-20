"""Local human curation and exact current-session read contracts."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import BinaryIO, Final

from .domain import (
    MAX_HANDOFF_FIELD_BYTES,
    HandoffId,
    HostId,
    ProviderId,
    SessionKey,
    SurfaceId,
    ValidationError,
    normalize_handoff_text,
)
from .protocol import SessionDetailEnvelope
from .snapshot import session_record
from .storage import DEFAULT_HANDOFF_LIMIT, Registry, SessionDetailRows
from .tmux import TmuxController, TmuxError, TmuxLocator

MAX_HANDOFF_INPUT_BYTES: Final = 2 * MAX_HANDOFF_FIELD_BYTES + 8 * 1024


class CurationError(RuntimeError):
    """A local human curation request cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class HandoffInput:
    summary: str
    next_action: str
    handoff_id: str | None = None


@dataclass(frozen=True, slots=True)
class CurrentSessionBinding:
    session_key: SessionKey
    surface_id: SurfaceId
    launch_id: str | None
    provider: ProviderId


def _handoff_record(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "handoffId": row["handoff_id"],
        "sessionKey": row["session_key"],
        "sequence": row["sequence"],
        "summary": row["summary"],
        "nextAction": row["next_action"],
        "source": row["source"],
        "sourceHostId": row["source_host_id"],
        "createdAt": row["created_at"],
        "contentHash": row["content_hash"],
    }


def detail_envelope(
    rows: SessionDetailRows,
    *,
    generated_at: int | None = None,
) -> SessionDetailEnvelope:
    """Project coherent storage rows through the versioned public boundary."""

    timestamp = time.time_ns() // 1_000_000 if generated_at is None else generated_at
    return SessionDetailEnvelope.from_dict(
        {
            "schemaVersion": 1,
            "protocolVersion": 1,
            "generatedAt": timestamp,
            "session": session_record(rows.session),
            "handoffs": [_handoff_record(row) for row in rows.handoffs],
            "handoffsTruncated": rows.handoffs_truncated,
        }
    )


def read_session_detail(
    registry: Registry,
    *,
    host_id: HostId | str,
    session_key: SessionKey | str,
    handoff_limit: int = DEFAULT_HANDOFF_LIMIT,
    generated_at: int | None = None,
) -> SessionDetailEnvelope:
    host = host_id if isinstance(host_id, HostId) else HostId(host_id)
    key = (
        session_key
        if isinstance(session_key, SessionKey)
        else SessionKey.parse(session_key)
    )
    rows = registry.read_session_detail(
        str(key), host_id=str(host), handoff_limit=handoff_limit
    )
    return detail_envelope(rows, generated_at=generated_at)


def resolve_current_session_binding(
    registry: Registry,
    *,
    host_id: HostId | str,
    environment: Mapping[str, str] | None = None,
    tmux: TmuxController | None = None,
) -> CurrentSessionBinding:
    """Resolve only a confirmed session bound to the inherited exact tmux pane."""

    host = host_id if isinstance(host_id, HostId) else HostId(host_id)
    controller = TmuxController() if tmux is None else tmux
    try:
        observed = controller.current_pane(
            os.environ if environment is None else environment
        )
    except TmuxError as error:
        raise CurationError("the inherited tmux pane could not be verified") from error
    if observed is None:
        raise CurationError("the current terminal is not inside tmux")
    metadata = observed.metadata
    if (
        metadata.role != "session"
        or metadata.surface_id is None
        or metadata.session_key is None
        or metadata.provider is None
    ):
        raise CurationError("the current pane is not a bound session surface")
    try:
        surface_id = SurfaceId(metadata.surface_id)
        key = SessionKey.parse(metadata.session_key)
        provider = ProviderId(metadata.provider)
    except (ValidationError, ValueError) as error:
        raise CurationError("the current pane metadata is invalid") from error
    if key.host_id != host or key.provider is not provider:
        raise CurationError("the current pane metadata belongs to another identity")
    surface = registry.get_surface(str(surface_id))
    session = registry.get_session(str(key))
    if surface is None or session is None:
        raise CurationError("the current pane binding is not retained")
    try:
        retained_locator = TmuxLocator.from_storage(surface["transport_locator"])
    except (KeyError, TmuxError) as error:
        raise CurationError("the retained surface locator is invalid") from error
    if (
        surface["host_id"] != str(host)
        or surface["provider"] != provider.value
        or surface["transport"] != "tmux"
        or surface["role"] != "session"
        or surface["current_session_key"] != str(key)
        or surface["binding_confidence"] != "confirmed"
        or surface["retired_at"] is not None
        or surface["launch_id"] != metadata.launch_id
        or retained_locator != observed.locator
        or session["host_id"] != str(host)
        or session["provider"] != provider.value
        or session["surface_id"] != str(surface_id)
    ):
        raise CurationError("the current pane binding changed or is untrusted")
    return CurrentSessionBinding(key, surface_id, metadata.launch_id, provider)


def resolve_current_session_key(
    registry: Registry,
    *,
    host_id: HostId | str,
    environment: Mapping[str, str] | None = None,
    tmux: TmuxController | None = None,
) -> SessionKey:
    """Resolve the session key from the exact confirmed current binding."""

    return resolve_current_session_binding(
        registry,
        host_id=host_id,
        environment=environment,
        tmux=tmux,
    ).session_key


def read_handoff_input(stream: BinaryIO) -> HandoffInput:
    """Read one strict bounded handoff object from standard input."""

    raw = stream.read(MAX_HANDOFF_INPUT_BYTES + 1)
    if len(raw) > MAX_HANDOFF_INPUT_BYTES:
        raise CurationError(
            f"handoff input exceeds the {MAX_HANDOFF_INPUT_BYTES}-byte limit"
        )
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CurationError("handoff input must be one UTF-8 JSON object") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CurationError("handoff input must be one JSON object")
    unknown = set(value) - {"handoffId", "summary", "nextAction"}
    missing = {"summary", "nextAction"} - set(value)
    if unknown:
        raise CurationError(f"handoff input contains unknown fields: {sorted(unknown)}")
    if missing:
        raise CurationError(f"handoff input is missing fields: {sorted(missing)}")
    try:
        summary = normalize_handoff_text(value["summary"], "summary")
        next_action = normalize_handoff_text(value["nextAction"], "nextAction")
        raw_handoff_id = value.get("handoffId")
        handoff_id = None if raw_handoff_id is None else str(HandoffId(raw_handoff_id))
    except ValidationError as error:
        raise CurationError(str(error)) from error
    if raw_handoff_id is not None and raw_handoff_id != handoff_id:
        raise CurationError("handoffId must use canonical UUID spelling")
    return HandoffInput(summary, next_action, handoff_id)


def format_session_detail(detail: SessionDetailEnvelope) -> str:
    """Render concise human output without private provider/runtime fields."""

    session = detail.session
    label = session.get("name") or str(session["providerSessionId"])
    lines = [
        f"{label} [{session['provider']}]",
        f"session: {session['sessionKey']}",
    ]
    if session.get("purpose") is not None:
        lines.append(f"purpose: {session['purpose']}")
    lines.append(f"pinned: {'yes' if session.get('pinned') else 'no'}")
    lines.append(f"wrapped: {'yes' if session.get('wrappedAt') is not None else 'no'}")
    for handoff in detail.handoffs:
        lines.extend(
            (
                f"handoff {handoff['sequence']}: {handoff['summary']}",
                f"next: {handoff['nextAction']}",
            )
        )
    if detail.handoffs_truncated:
        lines.append("handoffs: additional older records omitted")
    return "\n".join(str(line) for line in lines)


__all__ = [
    "CurationError",
    "CurrentSessionBinding",
    "HandoffInput",
    "detail_envelope",
    "format_session_detail",
    "read_handoff_input",
    "read_session_detail",
    "resolve_current_session_binding",
    "resolve_current_session_key",
]
