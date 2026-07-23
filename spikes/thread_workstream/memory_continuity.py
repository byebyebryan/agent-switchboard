"""External-memory continuity classification without memory authority."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ContinuityProfile(StrEnum):
    """The continuity available to a destination provider turn."""

    FULL = "full"
    IMMEDIATE_ONLY = "immediate-only"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class TransitionCapsule:
    """Immediate transition state that remains authoritative without memory."""

    accepted_plan: str
    triggering_input: str
    project_scope: str

    @property
    def sufficient(self) -> bool:
        return all(
            value.strip()
            for value in (
                self.accepted_plan,
                self.triggering_input,
                self.project_scope,
            )
        )


@dataclass(frozen=True, slots=True)
class MemoryObservation:
    """Content-free facts about one memory lookup."""

    available: bool
    healthy: bool
    scope_exact: bool
    recent_context: bool
    planning_context: bool
    latency_ms: int


class MemoryAdapter(Protocol):
    """A read-only memory reference used only to enrich a transition."""

    def observe(self, project_scope: str) -> MemoryObservation:
        """Return content-free facts for one project-scoped lookup."""


@dataclass(frozen=True, slots=True)
class ContinuityOutcome:
    """Classification for two pre-turn memory observations."""

    profile: ContinuityProfile
    plan_preserved: bool
    source: MemoryObservation | None
    destination: MemoryObservation | None
    memory_authorized_transition: bool = False


def _supports_full(
    observation: MemoryObservation | None,
    *,
    deadline_ms: int,
) -> bool:
    return bool(
        observation is not None
        and observation.available
        and observation.healthy
        and observation.scope_exact
        and observation.recent_context
        and observation.planning_context
        and observation.latency_ms <= deadline_ms
    )


def evaluate_continuity(
    capsule: TransitionCapsule,
    adapter: MemoryAdapter,
    *,
    deadline_ms: int = 2_000,
) -> ContinuityOutcome:
    """Classify memory enrichment while preserving capsule authority."""

    if not capsule.sufficient:
        return ContinuityOutcome(
            profile=ContinuityProfile.BLOCKED,
            plan_preserved=False,
            source=None,
            destination=None,
        )

    observations: list[MemoryObservation | None] = []
    for _role in ("source", "destination"):
        try:
            observations.append(adapter.observe(capsule.project_scope))
        except (OSError, TimeoutError, ValueError):
            observations.append(None)

    source, destination = observations
    profile = (
        ContinuityProfile.FULL
        if _supports_full(source, deadline_ms=deadline_ms)
        and _supports_full(destination, deadline_ms=deadline_ms)
        else ContinuityProfile.IMMEDIATE_ONLY
    )
    return ContinuityOutcome(
        profile=profile,
        plan_preserved=True,
        source=source,
        destination=destination,
    )


class FixedMemoryAdapter:
    """A deterministic adapter for degradation and replay tests."""

    def __init__(self, observation: MemoryObservation) -> None:
        self._observation = observation

    def observe(self, project_scope: str) -> MemoryObservation:
        del project_scope
        return self._observation


class FailingMemoryAdapter:
    """A deterministic unavailable adapter."""

    def observe(self, project_scope: str) -> MemoryObservation:
        del project_scope
        raise OSError("memory unavailable")


class HealthyReferenceAdapter:
    """Read bounded health and immediate-context facts from claude-mem."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:37700",
        timeout_seconds: float = 3,
        maximum_bytes: int = 1_048_576,
        opener: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._maximum_bytes = maximum_bytes
        self._opener = opener
        self.installed_version = "unknown"

    def _read(self, url: str) -> bytes:
        response = self._opener(url, timeout=self._timeout_seconds)
        try:
            status = getattr(response, "status", 200)
            if status != 200:
                raise OSError("memory reference returned non-success")
            payload = response.read(self._maximum_bytes + 1)
        finally:
            response.close()
        if len(payload) > self._maximum_bytes:
            raise ValueError("memory reference exceeded bounded response")
        return payload

    def observe(self, project_scope: str) -> MemoryObservation:
        started = time.monotonic()
        health = json.loads(self._read(f"{self._base_url}/api/health").decode("utf-8"))
        version = health.get("version")
        if isinstance(version, str) and version:
            self.installed_version = version
        healthy = (
            health.get("status") == "ok"
            and health.get("initialized") is True
            and health.get("mcpReady") is True
        )
        query = urllib.parse.urlencode({"project": project_scope})
        context = self._read(f"{self._base_url}/api/context/inject?{query}").lower()
        scope_marker = project_scope.casefold().encode("utf-8")
        planning_context = b"workstream" in context and (
            b"rollover" in context or b"clear context" in context
        )
        return MemoryObservation(
            available=bool(context.strip()),
            healthy=healthy,
            scope_exact=scope_marker in context,
            recent_context=planning_context,
            planning_context=planning_context,
            latency_ms=int((time.monotonic() - started) * 1_000),
        )


__all__ = [
    "ContinuityOutcome",
    "ContinuityProfile",
    "FailingMemoryAdapter",
    "FixedMemoryAdapter",
    "HealthyReferenceAdapter",
    "MemoryAdapter",
    "MemoryObservation",
    "TransitionCapsule",
    "evaluate_continuity",
]
