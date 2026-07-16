"""Validated host-local snapshot assembly from the materialized registry."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .domain import HostId
from .protocol import (
    MAX_JSON_BYTES,
    Capability,
    ErrorRecord,
    ErrorScope,
    HostRecord,
    ProtocolError,
    SnapshotEnvelope,
)
from .storage import HostSnapshotRows, Registry, now_ms

_SNAPSHOT_SESSION_BYTE_BUDGET = MAX_JSON_BYTES // 2


class _InvalidStoredProjectJson(ValueError):
    pass


def _reject_json_constant(_value: str) -> None:
    raise _InvalidStoredProjectJson


def _project_json_array(row: Mapping[str, Any], field: str) -> list[Any]:
    raw = row[field]
    error_message = f"stored project {field} is invalid JSON"
    if not isinstance(raw, str):
        raise ProtocolError(error_message)
    if len(raw) > MAX_JSON_BYTES:
        raise ProtocolError(f"stored project {field} exceeds the safe JSON size")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ProtocolError(error_message) from error
    if len(encoded) > MAX_JSON_BYTES:
        raise ProtocolError(f"stored project {field} exceeds the safe JSON size")
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError) as error:
        raise ProtocolError(error_message) from error
    if not isinstance(value, list):
        raise ProtocolError(error_message)
    return value


def _optional(
    result: dict[str, Any],
    row: Mapping[str, Any],
    source: str,
    target: str,
) -> None:
    value = row[source]
    if value is not None:
        result[target] = value


def _project(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "projectId": row["project_id"],
        "name": row["name"],
        "aliases": _project_json_array(row, "aliases_json"),
        "contextSources": _project_json_array(row, "context_sources_json"),
        "declared": bool(row["declared"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    _optional(result, row, "default_provider", "defaultProvider")
    _optional(result, row, "default_transport", "defaultTransport")
    return result


def _location(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "locationId": row["location_id"],
        "projectId": row["project_id"],
        "hostId": row["host_id"],
        "path": row["path"],
        "isDefault": bool(row["is_default"]),
        "declared": bool(row["declared"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    for source, target in (
        ("display_name", "displayName"),
        ("repository_identity", "repositoryIdentity"),
        ("provider_override", "providerOverride"),
        ("transport_override", "transportOverride"),
        ("last_observed_at", "lastObservedAt"),
    ):
        _optional(result, row, source, target)
    return result


def _session(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "sessionKey": row["session_key"],
        "hostId": row["host_id"],
        "provider": row["provider"],
        "providerSessionId": row["provider_session_id"],
        "firstObservedAt": row["first_observed_at"],
        "lastObservedAt": row["last_observed_at"],
        "metadataSource": row["metadata_source"],
        "runtimePresence": row["runtime_presence"],
        "resumability": row["resumability"],
        "activity": row["activity"],
        "activityReason": row["activity_reason"],
        "attachment": row["attachment"],
        "stateConfidence": row["state_confidence"],
        "pinned": bool(row["pinned"]),
    }
    for source, target in (
        ("project_id", "projectId"),
        ("location_id", "locationId"),
        ("name", "name"),
        ("purpose", "purpose"),
        ("cwd", "cwd"),
        ("created_at", "createdAt"),
        ("provider_updated_at", "providerUpdatedAt"),
        ("last_activity_at", "lastActivityAt"),
        ("state_observed_at", "stateObservedAt"),
        ("surface_id", "surfaceId"),
        ("latest_handoff_id", "latestHandoffId"),
        ("wrapped_at", "wrappedAt"),
        ("continued_from_handoff_id", "continuedFromHandoffId"),
    ):
        _optional(result, row, source, target)

    runtime_fields = (
        "runtime_pid",
        "provider_runtime_id",
        "tmux_session",
        "tmux_window",
        "tmux_pane",
        "runtime_observed_at",
    )
    if any(row[field] is not None for field in runtime_fields):
        runtime_locator: dict[str, Any] = {}
        for source, target in (
            ("runtime_pid", "pid"),
            ("provider_runtime_id", "providerRuntimeId"),
            ("tmux_session", "tmuxSession"),
            ("tmux_window", "tmuxWindow"),
            ("tmux_pane", "tmuxPane"),
            ("runtime_observed_at", "observedAt"),
        ):
            _optional(runtime_locator, row, source, target)
        result["runtimeLocator"] = runtime_locator
    return result


def _runtime(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "hostId": row["host_id"],
        "provider": row["provider"],
        "observationId": row["observation_id"],
        "observationKey": row["observation_key"],
        "source": row["source"],
        "sourcePriority": row["source_priority"],
        "runtimePresence": row["runtime_presence"],
        "resumability": row["resumability"],
        "activity": row["activity"],
        "activityReason": row["activity_reason"],
        "attachment": row["attachment"],
        "observedAt": row["observed_at"],
        "receivedAt": row["received_at"],
        "payloadHash": row["payload_hash"],
    }
    for source, target in (
        ("session_key", "sessionKey"),
        ("launch_id", "launchId"),
        ("pid", "pid"),
        ("provider_runtime_id", "providerRuntimeId"),
        ("tmux_session", "tmuxSession"),
        ("tmux_window", "tmuxWindow"),
        ("tmux_pane", "tmuxPane"),
    ):
        _optional(result, row, source, target)
    return result


def _surface(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "surfaceId": row["surface_id"],
        "hostId": row["host_id"],
        "provider": row["provider"],
        "transport": row["transport"],
        "transportLocator": row["transport_locator"],
        "role": row["role"],
        "bindingConfidence": row["binding_confidence"],
        "createdAt": row["created_at"],
        "lastObservedAt": row["last_observed_at"],
        "clientAttached": bool(row["client_attached"]),
    }
    for source, target in (
        ("current_session_key", "currentSessionKey"),
        ("launch_id", "launchId"),
        ("workspace_id", "workspaceId"),
        ("retired_at", "retiredAt"),
    ):
        _optional(result, row, source, target)
    return result


def _bounded_session_rows(
    rows: HostSnapshotRows,
) -> tuple[tuple[dict[str, Any], ...], bool]:
    """Select deterministic session rows within a canonical UTF-8 budget."""

    selected: list[dict[str, Any]] = []
    encoded_bytes = 2  # JSON array delimiters.
    for row in rows.sessions:
        try:
            encoded = json.dumps(
                _session(row),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as error:
            raise ProtocolError(
                "stored session contains invalid JSON metadata"
            ) from error
        separator_bytes = 1 if selected else 0
        if (
            encoded_bytes + separator_bytes + len(encoded)
            > _SNAPSHOT_SESSION_BYTE_BUDGET
        ):
            continue
        selected.append(dict(row))
        encoded_bytes += separator_bytes + len(encoded)

    truncated = rows.retained_session_count > len(selected)
    return tuple(selected), truncated


def _bounded_snapshot_rows(
    rows: HostSnapshotRows,
) -> tuple[HostSnapshotRows, bool]:
    sessions, truncated = _bounded_session_rows(rows)
    selected_keys = {str(row["session_key"]) for row in sessions}
    runtimes = tuple(
        row
        for row in rows.runtimes
        if row["session_key"] is None or str(row["session_key"]) in selected_keys
    )
    surfaces = tuple(
        row
        for row in rows.surfaces
        if row["current_session_key"] is None
        or str(row["current_session_key"]) in selected_keys
    )
    return (
        HostSnapshotRows(
            host=rows.host,
            projects=rows.projects,
            locations=rows.locations,
            sessions=sessions,
            retained_session_count=rows.retained_session_count,
            runtimes=runtimes,
            surfaces=surfaces,
        ),
        truncated,
    )


def _assemble(
    rows: HostSnapshotRows,
    *,
    generated_at: int,
    capabilities: Sequence[Capability],
    errors: Sequence[ErrorRecord],
) -> SnapshotEnvelope:
    ordered_capabilities = tuple(
        sorted(capabilities, key=lambda capability: str(capability.provider))
    )
    ordered_errors = tuple(
        sorted(
            errors,
            key=lambda error: json.dumps(
                error.to_dict(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    )
    envelope = SnapshotEnvelope(
        generated_at=generated_at,
        host=HostRecord(
            host_id=HostId(rows.host["host_id"]),
            display_name=rows.host["display_name"],
        ),
        projects=tuple(_project(row) for row in rows.projects),
        locations=tuple(_location(row) for row in rows.locations),
        sessions=tuple(_session(row) for row in rows.sessions),
        runtimes=tuple(_runtime(row) for row in rows.runtimes),
        surfaces=tuple(_surface(row) for row in rows.surfaces),
        capabilities=ordered_capabilities,
        errors=ordered_errors,
    )
    # to_json is the final privacy and protocol gate. Reparse its canonical
    # output so callers receive exactly the validated representation.
    return SnapshotEnvelope.from_json(envelope.to_json())


def build_host_snapshot(
    registry: Registry,
    host_id: str,
    *,
    generated_at: int | None = None,
    capabilities: Sequence[Capability] = (),
    errors: Sequence[ErrorRecord] = (),
) -> SnapshotEnvelope:
    """Build a protocol-valid snapshot without querying or mutating providers."""

    rows, sessions_truncated = _bounded_snapshot_rows(
        registry.read_host_snapshot(host_id)
    )
    timestamp = now_ms() if generated_at is None else generated_at
    snapshot_errors = tuple(errors)
    if sessions_truncated:
        snapshot_errors = (
            *snapshot_errors,
            ErrorRecord(
                code="snapshot_sessions_truncated",
                message=(
                    "The snapshot omitted retained sessions to remain within "
                    "protocol limits."
                ),
                scope=ErrorScope.HOST,
                retryable=False,
                observed_at=timestamp,
                host_id=HostId(rows.host["host_id"]),
                details={
                    "retainedCount": rows.retained_session_count,
                    "emittedCount": len(rows.sessions),
                },
            ),
        )
    return _assemble(
        rows,
        generated_at=timestamp,
        capabilities=capabilities,
        errors=snapshot_errors,
    )


def build_host_snapshot_json(
    registry: Registry,
    host_id: str,
    *,
    generated_at: int | None = None,
    capabilities: Sequence[Capability] = (),
    errors: Sequence[ErrorRecord] = (),
) -> str:
    """Return canonical JSON for one validated host-local snapshot."""

    return build_host_snapshot(
        registry,
        host_id,
        generated_at=generated_at,
        capabilities=capabilities,
        errors=errors,
    ).to_json()


__all__ = ["build_host_snapshot", "build_host_snapshot_json"]
