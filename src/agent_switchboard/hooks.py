"""Privacy-safe normalization for provider lifecycle hook input."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Final

from .domain import HostId, SessionKey
from .state import HOOK_SOURCE_PRIORITY, HookEvent, hook_transition

MAX_HOOK_INPUT_BYTES: Final = 8 * 1024 * 1024
MAX_HOOK_JSON_DEPTH: Final = 32
_TURN_EVENTS: Final = frozenset(
    {
        HookEvent.USER_PROMPT_SUBMIT,
        HookEvent.PERMISSION_REQUEST,
        HookEvent.POST_TOOL_USE,
        HookEvent.STOP,
    }
)
_SESSION_START_SOURCES: Final = frozenset({"startup", "resume", "clear", "compact"})
_TMUX_PANE = re.compile(r"^%[0-9]+$")
_PROCESS_BIRTH_DIGEST_LENGTH: Final = 64
_MAX_TMUX_SOCKET_LENGTH: Final = 4096
_MAX_TMUX_PANE_LENGTH: Final = 256


class HookInputError(ValueError):
    """A hook payload cannot be accepted without risking incorrect state."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bounded_string(
    payload: Mapping[str, Any],
    field: str,
    *,
    maximum: int,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise HookInputError(f"hook field {field!r} must be a bounded string")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise HookInputError(f"hook field {field!r} contains control characters")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise HookInputError(f"hook field {field!r} contains invalid Unicode")
    return value


def _canonical_uuid(payload: Mapping[str, Any], field: str) -> str:
    value = _bounded_string(payload, field, maximum=36)
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise HookInputError(f"hook field {field!r} must be a UUID") from error
    if parsed.int == 0 or str(parsed) != value:
        raise HookInputError(f"hook field {field!r} must be a canonical non-nil UUID")
    return value


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
    """Read one bounded JSON object without retaining its raw bytes."""

    raw = stream.read(MAX_HOOK_INPUT_BYTES + 1)
    if len(raw) > MAX_HOOK_INPUT_BYTES:
        raise HookInputError("hook input exceeds the 8 MiB limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise HookInputError("hook input is not valid bounded JSON") from error
    finally:
        del raw
    if not isinstance(value, Mapping):
        raise HookInputError("hook input must be one JSON object")
    if _json_depth(value) > MAX_HOOK_JSON_DEPTH:
        raise HookInputError("hook input exceeds the maximum JSON depth")
    return value


def _linux_process_birth_id() -> str | None:
    """Return an opaque birth token for the nearest Codex-like ancestor."""

    try:
        boot_id = (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        )
    except (OSError, UnicodeError):
        return None

    pid = os.getppid()
    fallback: tuple[int, str] | None = None
    seen: set[int] = set()
    for _ in range(8):
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        try:
            stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
            close = stat.rfind(")")
            fields = stat[close + 2 :].split()
            parent_pid = int(fields[1])
            start_ticks = fields[19]
            comm = stat[stat.find("(") + 1 : close].casefold()
        except (OSError, UnicodeError, ValueError, IndexError):
            break
        fallback = fallback or (pid, start_ticks)
        if "codex" in comm:
            fallback = (pid, start_ticks)
            break
        pid = parent_pid
    if fallback is None:
        return None
    return _digest({"boot": boot_id, "pid": fallback[0], "start": fallback[1]})


def _tmux_locator(environment: Mapping[str, str]) -> tuple[str | None, str | None]:
    tmux = environment.get("TMUX")
    pane = environment.get("TMUX_PANE")
    if not isinstance(tmux, str) or not isinstance(pane, str):
        return None, None
    pieces = tmux.rsplit(",", 2)
    if len(pieces) != 3:
        return None, None
    socket = pieces[0]
    if (
        not socket
        or len(socket) > _MAX_TMUX_SOCKET_LENGTH
        or not Path(socket).is_absolute()
        or any(unicodedata.category(character) == "Cc" for character in socket)
        or any(0xD800 <= ord(character) <= 0xDFFF for character in socket)
        or len(pane) > _MAX_TMUX_PANE_LENGTH
        or not _TMUX_PANE.fullmatch(pane)
    ):
        return None, None
    return socket, pane


@dataclass(frozen=True, slots=True)
class NormalizedHookEvent:
    """Provider-neutral lifecycle evidence safe for durable storage."""

    provider: str
    provider_session_id: str
    cwd: str
    event_kind: HookEvent
    provider_turn_id: str | None
    idempotency_key: str
    source_priority: int
    kind_priority: int
    entry_ns: int
    observed_at: int
    received_at: int
    launch_id: str | None
    surface_id: str | None
    process_birth_id: str | None
    tmux_socket: str | None
    tmux_pane: str | None

    def storage_mapping(self, host_id: HostId | str) -> dict[str, Any]:
        key = SessionKey(HostId(str(host_id)), self.provider, self.provider_session_id)
        return {
            "idempotency_key": self.idempotency_key,
            "host_id": str(host_id),
            "provider": self.provider,
            "provider_session_id": self.provider_session_id,
            "session_key": str(key),
            "cwd": self.cwd,
            "event_kind": self.event_kind.value,
            "provider_turn_id": self.provider_turn_id,
            "source_priority": self.source_priority,
            "kind_priority": self.kind_priority,
            "entry_ns": self.entry_ns,
            "observed_at": self.observed_at,
            "received_at": self.received_at,
            "launch_id": self.launch_id,
            "surface_id": self.surface_id,
            "process_birth_id": self.process_birth_id,
            "tmux_socket": self.tmux_socket,
            "tmux_pane": self.tmux_pane,
        }


def normalize_codex_event(
    payload: Mapping[str, Any],
    environment: Mapping[str, str],
    *,
    entry_ns: int | None = None,
    observed_at: int | None = None,
    process_birth_id: str | None = None,
) -> NormalizedHookEvent:
    """Allowlist Codex identity/lifecycle fields and discard everything else."""

    if not isinstance(payload, Mapping):
        raise HookInputError("hook input must be one JSON object")
    event_name = _bounded_string(payload, "hook_event_name", maximum=64)
    try:
        event_kind = HookEvent(event_name)
    except ValueError as error:
        raise HookInputError("unsupported Codex hook event") from error
    if event_kind is HookEvent.SESSION_END:
        raise HookInputError("unsupported Codex hook event")

    provider_session_id = _canonical_uuid(payload, "session_id")
    cwd = _bounded_string(payload, "cwd", maximum=4096)
    if not Path(cwd).is_absolute():
        raise HookInputError("hook field 'cwd' must be an absolute path")

    provider_turn_id: str | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None
    start_source: str | None = None
    if event_kind in _TURN_EVENTS:
        provider_turn_id = _bounded_string(payload, "turn_id", maximum=256)
    if event_kind in {HookEvent.PERMISSION_REQUEST, HookEvent.POST_TOOL_USE}:
        tool_name = _bounded_string(payload, "tool_name", maximum=256)
    if event_kind is HookEvent.POST_TOOL_USE:
        tool_use_id = _bounded_string(payload, "tool_use_id", maximum=256)
    if event_kind is HookEvent.SESSION_START:
        start_source = _bounded_string(payload, "source", maximum=32)
        if start_source not in _SESSION_START_SOURCES:
            raise HookInputError("unsupported Codex SessionStart source")

    timestamp_ns = time.time_ns() if entry_ns is None else entry_ns
    if (
        isinstance(timestamp_ns, bool)
        or not isinstance(timestamp_ns, int)
        or timestamp_ns < 0
    ):
        raise HookInputError("hook entry time must be a non-negative integer")
    timestamp_ms = timestamp_ns // 1_000_000 if observed_at is None else observed_at
    if (
        isinstance(timestamp_ms, bool)
        or not isinstance(timestamp_ms, int)
        or timestamp_ms < 0
    ):
        raise HookInputError("hook observation time must be a non-negative integer")

    birth_id = (
        process_birth_id if process_birth_id is not None else _linux_process_birth_id()
    )
    if birth_id is not None and (
        len(birth_id) != _PROCESS_BIRTH_DIGEST_LENGTH
        or any(character not in "0123456789abcdef" for character in birth_id)
    ):
        raise HookInputError(
            "process birth identity must be an opaque lowercase digest"
        )

    launch_id = environment.get("AGENT_SWITCHBOARD_LAUNCH_ID")
    surface_id = environment.get("AGENT_SWITCHBOARD_SURFACE_ID")
    if (launch_id is None) != (surface_id is None):
        raise HookInputError("launch and surface identities must be supplied together")
    if launch_id is not None:
        launch_id = _canonical_uuid(
            {"launch_id": launch_id},
            "launch_id",
        )
        surface_id = _canonical_uuid(
            {"surface_id": surface_id},
            "surface_id",
        )
    tmux_socket, tmux_pane = _tmux_locator(environment)

    identity: dict[str, object] = {
        "provider": "codex",
        "session": provider_session_id,
        "event": event_kind.value,
    }
    if event_kind is HookEvent.SESSION_START:
        identity.update(
            source=start_source,
            process=birth_id or "unknown",
            occurrence_bucket=timestamp_ns // 1_000_000_000,
        )
    else:
        identity["turn"] = provider_turn_id
        if event_kind is HookEvent.PERMISSION_REQUEST:
            identity["tool"] = tool_name
        elif event_kind is HookEvent.POST_TOOL_USE:
            identity["tool_use"] = tool_use_id

    return NormalizedHookEvent(
        provider="codex",
        provider_session_id=provider_session_id,
        cwd=cwd,
        event_kind=event_kind,
        provider_turn_id=provider_turn_id,
        idempotency_key=f"hook:{_digest(identity)}",
        source_priority=HOOK_SOURCE_PRIORITY,
        kind_priority=hook_transition(event_kind).kind_priority,
        entry_ns=timestamp_ns,
        observed_at=timestamp_ms,
        received_at=timestamp_ms,
        launch_id=launch_id,
        surface_id=surface_id,
        process_birth_id=birth_id,
        tmux_socket=tmux_socket,
        tmux_pane=tmux_pane,
    )


__all__ = [
    "HOOK_SOURCE_PRIORITY",
    "MAX_HOOK_INPUT_BYTES",
    "MAX_HOOK_JSON_DEPTH",
    "HookInputError",
    "NormalizedHookEvent",
    "normalize_codex_event",
    "read_hook_json",
]
