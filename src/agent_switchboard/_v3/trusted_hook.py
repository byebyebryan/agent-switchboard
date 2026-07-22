"""Trusted provider-hook routing for the bounded Phase 6D hot path."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO, Final
from uuid import UUID

from .domain import (
    Activity,
    ActivityReason,
    ProviderId,
    ProviderSession,
    RuntimePresence,
    SessionKey,
)
from .workflow import StopResult, WorkflowError, WorkflowRuntime

MAX_HOOK_INPUT_BYTES: Final = 8 * 1024 * 1024
MAX_HOOK_JSON_DEPTH: Final = 32


class HookInputError(ValueError):
    """A provider hook is malformed or lacks exact managed authority."""


class HookEvent(StrEnum):
    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PERMISSION_REQUEST = "PermissionRequest"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"
    SESSION_END = "SessionEnd"


@dataclass(frozen=True, slots=True)
class NormalizedHookEvent:
    provider_session_id: str
    event_kind: HookEvent
    provider_turn_id: str | None


def _json_depth(value: object) -> int:
    maximum = 1
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        candidate, depth = stack.pop()
        maximum = max(maximum, depth)
        if maximum > MAX_HOOK_JSON_DEPTH:
            return maximum
        if isinstance(candidate, Mapping):
            stack.extend((nested, depth + 1) for nested in candidate.values())
        elif isinstance(candidate, list):
            stack.extend((nested, depth + 1) for nested in candidate)
    return maximum


def read_hook_json(stream: BinaryIO) -> Mapping[str, Any]:
    raw = stream.read(MAX_HOOK_INPUT_BYTES + 1)
    if len(raw) > MAX_HOOK_INPUT_BYTES:
        raise HookInputError("hook input exceeds the 8 MiB limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise HookInputError("hook input is not valid bounded JSON") from error
    finally:
        del raw
    if not isinstance(value, Mapping) or _json_depth(value) > MAX_HOOK_JSON_DEPTH:
        raise HookInputError("hook input must be one bounded JSON object")
    return value


def _text(payload: Mapping[str, Any], field: str, *, maximum: int) -> str:
    value = payload.get(field)
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode()) > maximum
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise HookInputError(f"hook field {field!r} must be bounded text")
    return value


def _uuid(payload: Mapping[str, Any], field: str) -> str:
    value = _text(payload, field, maximum=36)
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise HookInputError(f"hook field {field!r} must be a UUID") from error
    if parsed.int == 0 or str(parsed) != value:
        raise HookInputError(f"hook field {field!r} must be canonical")
    return value


def _normalize_event(
    provider: ProviderId, payload: Mapping[str, Any]
) -> NormalizedHookEvent:
    try:
        event = HookEvent(_text(payload, "hook_event_name", maximum=64))
    except ValueError as error:
        raise HookInputError("unsupported provider hook event") from error
    supported = {
        HookEvent.SESSION_START,
        HookEvent.USER_PROMPT_SUBMIT,
        HookEvent.PERMISSION_REQUEST,
        HookEvent.POST_TOOL_USE,
        HookEvent.STOP,
    }
    if event not in supported:
        raise HookInputError("unsupported provider hook event")
    session_id = _uuid(payload, "session_id")
    cwd = Path(_text(payload, "cwd", maximum=4_096))
    if not cwd.is_absolute():
        raise HookInputError("hook cwd must be absolute")
    turn_id = None
    if event in {
        HookEvent.USER_PROMPT_SUBMIT,
        HookEvent.PERMISSION_REQUEST,
        HookEvent.POST_TOOL_USE,
        HookEvent.STOP,
    }:
        turn_id = (
            _text(payload, "turn_id", maximum=256)
            if provider is ProviderId.CODEX
            else _uuid(payload, "prompt_id")
        )
    return NormalizedHookEvent(session_id, event, turn_id)


@dataclass(frozen=True, slots=True)
class HookResult:
    event: str
    action: str
    transition_id: str | None = None


def _session_activity(
    workflow: WorkflowRuntime,
    session_key: SessionKey,
    *,
    activity: Activity,
    reason: ActivityReason,
    now: int,
) -> None:
    current = workflow.registry.get_provider_session(session_key)
    workflow.registry.upsert_provider_session(
        ProviderSession(
            current.session_key,
            current.host_id,
            current.provider,
            current.provider_session_id,
            current.project_id,
            current.checkout_id,
            current.name,
            current.purpose,
            current.pinned,
            RuntimePresence.LIVE,
            current.resumability,
            activity,
            reason,
            current.created_at,
            now,
            now,
            now,
        )
    )


def handle_trusted_event(
    workflow: WorkflowRuntime,
    provider: ProviderId,
    payload: Mapping[str, Any],
    environment: Mapping[str, str],
    *,
    now: int,
) -> HookResult:
    """Normalize, bind to exact environment authority, and route one hook."""

    normalized = _normalize_event(provider, payload)
    raw_capability = environment.get("AGENT_SWITCHBOARD_CAPABILITY")
    raw_session_key = environment.get("SWB_V3_SESSION_KEY")
    if not raw_capability or not raw_session_key:
        raise HookInputError("managed hook authority is missing")
    try:
        session_key = SessionKey.parse(raw_session_key)
    except Exception as error:
        raise HookInputError("managed hook session identity is invalid") from error
    if (
        session_key.host_id != workflow.host_id
        or session_key.provider is not provider
        or str(session_key.provider_session_id) != normalized.provider_session_id
    ):
        raise HookInputError("managed hook session identity differs")
    capability = workflow.registry.validate_capability(raw_capability, now=now)
    if capability.session_key != session_key:
        raise HookInputError("managed hook capability identifies another session")
    if normalized.event_kind is HookEvent.SESSION_START:
        return HookResult(normalized.event_kind.value, "verified")
    if normalized.event_kind is HookEvent.USER_PROMPT_SUBMIT:
        assert normalized.provider_turn_id is not None
        _session_activity(
            workflow,
            session_key,
            activity=Activity.WORKING,
            reason=ActivityReason.UNKNOWN,
            now=now,
        )
        control = workflow.observe_prompt(
            raw_capability,
            prompt_id=normalized.provider_turn_id,
            now=now,
        )
        return HookResult(
            normalized.event_kind.value,
            "observed" if control is not None else "ignored",
            None if control is None else str(control.transition_id),
        )
    if normalized.event_kind is HookEvent.STOP:
        _session_activity(
            workflow,
            session_key,
            activity=Activity.READY,
            reason=ActivityReason.TURN_COMPLETE,
            now=now,
        )
        stopped: StopResult = workflow.trusted_stop(raw_capability, now=now)
        return HookResult(
            normalized.event_kind.value,
            stopped.action,
            None if stopped.transition_id is None else str(stopped.transition_id),
        )
    if normalized.event_kind is HookEvent.PERMISSION_REQUEST:
        _session_activity(
            workflow,
            session_key,
            activity=Activity.NEEDS_INPUT,
            reason=ActivityReason.PERMISSION,
            now=now,
        )
        return HookResult(normalized.event_kind.value, "recorded")
    return HookResult(normalized.event_kind.value, "ignored")


__all__ = [
    "HookInputError",
    "HookResult",
    "WorkflowError",
    "handle_trusted_event",
    "read_hook_json",
]
