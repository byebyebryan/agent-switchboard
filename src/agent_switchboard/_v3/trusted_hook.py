"""Trusted provider-hook routing for the bounded Phase 6D hot path."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_switchboard.hooks import (
    HookInputError,
    normalize_claude_event,
    normalize_codex_event,
)
from agent_switchboard.state import HookEvent

from .domain import (
    Activity,
    ActivityReason,
    ProviderId,
    ProviderSession,
    RuntimePresence,
    SessionKey,
)
from .workflow import StopResult, WorkflowError, WorkflowRuntime


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

    normalized = (
        normalize_codex_event(payload, environment, observed_at=now)
        if provider is ProviderId.CODEX
        else normalize_claude_event(payload, environment, observed_at=now)
    )
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


__all__ = ["HookResult", "WorkflowError", "handle_trusted_event"]
