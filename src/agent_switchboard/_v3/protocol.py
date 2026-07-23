"""Bounded deterministic Phase 6 state and presentation protocols."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from .domain import (
    ActivationState,
    Activity,
    ActivityReason,
    BackgroundState,
    ClaimState,
    CloseReason,
    ControlKind,
    ControlState,
    CreatedBy,
    FailureRecord,
    FrameLifecycleState,
    FrameRole,
    HostId,
    MembershipReason,
    PlacementState,
    ProviderId,
    RecoveryActionability,
    RecoveryState,
    RepositoryKind,
    Resumability,
    RuntimePresence,
    SurfaceState,
    TransitionKind,
    TransitionState,
    Transport,
    TransportPhase,
    ViewMode,
    ViewState,
)
from .storage import Registry

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type Validator = Callable[[object, str], JsonValue]

SCHEMA_VERSION: Final = 1
PROTOCOL_VERSION: Final = 1
HOST_STATE_VERSION: Final = 1
NAVIGATOR_VERSION: Final = 1
DIRECTIVE_VERSION: Final = 1
MAX_JSON_BYTES: Final = 8 * 1024 * 1024
MAX_JSON_DEPTH: Final = 32
MAX_JSON_STRING_BYTES: Final = 64 * 1024
MAX_JSON_ARRAY_ITEMS: Final = 100_000
MAX_JSON_OBJECT_KEYS: Final = 256
MAX_JSON_INTEGER: Final = 2**63 - 1
DEFAULT_COLLECTION_LIMIT: Final = 10_000

_KEY_NORMALIZER = re.compile(r"[^a-z0-9]")
_FORBIDDEN_KEY_PARTS = (
    "argv",
    "capability",
    "command",
    "credential",
    "pane",
    "password",
    "process",
    "prompt",
    "raw",
    "secret",
    "socket",
    "tmux",
    "transcript",
)


class ProtocolError(ValueError):
    """A Phase 6 protocol value is malformed, unsafe, or inconsistent."""


class IncompatibleVersion(ProtocolError):
    """A protocol envelope declares an unsupported version."""


class DirectiveKind(StrEnum):
    FOCUS = "focus"
    ATTACH = "attach"
    BLOCKED = "blocked"


def _normalized_key(value: str) -> str:
    return _KEY_NORMALIZER.sub("", unicodedata.normalize("NFKC", value).casefold())


def _reject_key(value: str, path: str, *, allow_desktop_token: bool) -> None:
    normalized = _normalized_key(value)
    if not normalized or len(value.encode("utf-8")) > 256:
        raise ProtocolError(f"{path} contains an invalid object key")
    if "token" in normalized and not (
        allow_desktop_token and normalized == "desktoptoken"
    ):
        raise ProtocolError(f"{path} contains forbidden authority field {value!r}")
    if "path" in normalized and normalized != "actionability":
        raise ProtocolError(f"{path} contains forbidden path field {value!r}")
    if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
        raise ProtocolError(f"{path} contains forbidden field {value!r}")


def _safe_json(
    value: object,
    path: str,
    *,
    depth: int = 0,
    allow_desktop_token: bool = False,
) -> JsonValue:
    if depth > MAX_JSON_DEPTH:
        raise ProtocolError(f"{path} exceeds maximum JSON depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_JSON_STRING_BYTES:
            raise ProtocolError(f"{path} contains an oversized string")
        if any(unicodedata.category(character) == "Cc" for character in value):
            raise ProtocolError(f"{path} contains terminal control characters")
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > MAX_JSON_INTEGER:
            raise ProtocolError(f"{path} contains an out-of-range integer")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, list):
        if len(value) > MAX_JSON_ARRAY_ITEMS:
            raise ProtocolError(f"{path} contains too many array items")
        return [
            _safe_json(
                item,
                f"{path}[{index}]",
                depth=depth + 1,
                allow_desktop_token=allow_desktop_token,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        if len(value) > MAX_JSON_OBJECT_KEYS:
            raise ProtocolError(f"{path} contains too many object keys")
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            _reject_key(key, path, allow_desktop_token=allow_desktop_token)
            result[key] = _safe_json(
                item,
                f"{path}.{key}",
                depth=depth + 1,
                allow_desktop_token=allow_desktop_token,
            )
        return result
    raise ProtocolError(f"{path} contains a non-JSON value")


def _decode(
    raw: str | bytes | bytearray, *, allow_desktop_token: bool = False
) -> Mapping[str, Any]:
    try:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise ProtocolError("protocol input must be UTF-8 JSON") from error
    if len(encoded) > MAX_JSON_BYTES:
        raise ProtocolError("protocol input exceeds the 8 MiB limit")

    def no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ProtocolError(f"protocol object repeats key {key!r}")
            result[key] = item
        return result

    try:
        value = json.loads(encoded, object_pairs_hook=no_duplicate_keys)
    except ProtocolError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        raise ProtocolError("protocol input is not valid UTF-8 JSON") from error
    safe = _safe_json(value, "envelope", allow_desktop_token=allow_desktop_token)
    if not isinstance(safe, dict):
        raise ProtocolError("protocol envelope must be an object")
    return safe


def _dump(value: Mapping[str, JsonValue], *, allow_desktop_token: bool = False) -> str:
    safe = _safe_json(value, "envelope", allow_desktop_token=allow_desktop_token)
    assert isinstance(safe, dict)
    encoded = json.dumps(
        safe,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ProtocolError("protocol output exceeds the 8 MiB limit")
    return encoded


def _object(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ProtocolError(f"{path} must be an object")
    return value


def _array(value: object, path: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ProtocolError(f"{path} must be an array")
    return value


def _required(table: Mapping[str, Any], key: str, path: str) -> object:
    if key not in table:
        raise ProtocolError(f"{path}.{key} is required")
    return table[key]


def _string(value: object, path: str, *, maximum: int = 4_096) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ProtocolError(f"{path} must be a non-empty bounded string")
    return value


def _optional(validator: Validator) -> Validator:
    def validate(value: object, path: str) -> JsonValue:
        return None if value is None else validator(value, path)

    return validate


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError(f"{path} must be a non-negative integer")
    return value


def _positive_integer(value: object, path: str) -> int:
    result = _integer(value, path)
    if result < 1:
        raise ProtocolError(f"{path} must be positive")
    return result


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(f"{path} must be boolean")
    return value


def _uuid(value: object, path: str) -> str:
    text = _string(value, path, maximum=36)
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise ProtocolError(f"{path} must be a UUID") from error
    if parsed.int == 0 or str(parsed) != text:
        raise ProtocolError(f"{path} must be a canonical non-nil UUID")
    return text


def _session_key(value: object, path: str) -> str:
    text = _string(value, path, maximum=512)
    parts = text.split(":")
    if len(parts) != 3:
        raise ProtocolError(f"{path} must be a host-qualified session key")
    _uuid(parts[0], f"{path}.host")
    try:
        ProviderId(parts[1])
    except ValueError as error:
        raise ProtocolError(f"{path} has an unsupported provider") from error
    _uuid(parts[2], f"{path}.providerSession")
    return text


def _enum_validator(enum_type: type[StrEnum]) -> Validator:
    def validate(value: object, path: str) -> str:
        text = _string(value, path, maximum=128)
        try:
            enum_type(text)
        except ValueError as error:
            raise ProtocolError(f"{path} has an unsupported value") from error
        return text

    return validate


def _string_validator(maximum: int = 4_096) -> Validator:
    def validate(value: object, path: str) -> str:
        return _string(value, path, maximum=maximum)

    return validate


def _string_array(value: object, path: str) -> list[JsonValue]:
    return [
        _string(item, f"{path}[{index}]", maximum=1_024)
        for index, item in enumerate(_array(value, path))
    ]


def _record(
    value: object,
    path: str,
    fields: Mapping[str, Validator],
) -> dict[str, JsonValue]:
    table = _object(value, path)
    result: dict[str, JsonValue] = {}
    for key, validator in fields.items():
        result[key] = validator(_required(table, key, path), f"{path}.{key}")
    return result


def _records(
    value: object,
    path: str,
    fields: Mapping[str, Validator],
    order: tuple[str, ...],
) -> list[JsonValue]:
    records = [
        _record(item, f"{path}[{index}]", fields)
        for index, item in enumerate(_array(value, path))
    ]
    records.sort(key=lambda item: tuple(str(item[key]) for key in order))
    return records


def _versions(table: Mapping[str, Any], version_key: str, version: int) -> None:
    for key, expected in (
        ("schemaVersion", SCHEMA_VERSION),
        ("protocolVersion", PROTOCOL_VERSION),
        (version_key, version),
    ):
        actual = _integer(_required(table, key, "envelope"), f"envelope.{key}")
        if actual != expected:
            raise IncompatibleVersion(f"{key} {actual} is not supported")


def _failure(value: object, path: str) -> dict[str, JsonValue]:
    return _record(
        value,
        path,
        {
            "code": _string_validator(64),
            "message": _string_validator(1_024),
            "retryable": _boolean,
        },
    )


def _optional_failure(value: object, path: str) -> JsonValue:
    return None if value is None else _failure(value, path)


@dataclass(frozen=True, slots=True)
class PresentationDirective:
    request_id: str
    host_id: str
    kind: DirectiveKind
    view_id: str | None = None
    view_revision: int | None = None
    desktop_token: str | None = None
    lease_expires_at: int | None = None
    error: FailureRecord | None = None

    def __post_init__(self) -> None:
        _uuid(self.request_id, "directive.requestId")
        _uuid(self.host_id, "directive.hostId")
        if self.kind in {DirectiveKind.FOCUS, DirectiveKind.ATTACH}:
            if (
                self.view_id is None
                or self.view_revision is None
                or self.desktop_token is None
                or self.error is not None
            ):
                raise ProtocolError("focus/attach directive identity is incomplete")
            _uuid(self.view_id, "directive.viewId")
            _integer(self.view_revision, "directive.viewRevision")
            _string(self.desktop_token, "directive.desktopToken", maximum=256)
        elif (
            any(
                value is not None
                for value in (self.view_id, self.view_revision, self.desktop_token)
            )
            or self.error is None
        ):
            raise ProtocolError("blocked directive has invalid success fields")
        if (self.kind is DirectiveKind.ATTACH) != (self.lease_expires_at is not None):
            raise ProtocolError("only attach directive requires a lease expiry")
        if self.lease_expires_at is not None:
            _integer(self.lease_expires_at, "directive.leaseExpiresAt")

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "directiveVersion": DIRECTIVE_VERSION,
            "requestId": self.request_id,
            "hostId": self.host_id,
            "kind": self.kind.value,
        }
        if self.view_id is not None:
            result.update(
                {
                    "viewId": self.view_id,
                    "viewRevision": self.view_revision,
                    "desktopToken": self.desktop_token,
                }
            )
        if self.lease_expires_at is not None:
            result["leaseExpiresAt"] = self.lease_expires_at
        if self.error is not None:
            result["error"] = {
                "code": self.error.code,
                "message": self.error.message,
                "retryable": self.error.retryable,
            }
        return result

    def to_json(self) -> str:
        return _dump(self.to_dict(), allow_desktop_token=True)

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> PresentationDirective:
        table = _decode(raw, allow_desktop_token=True)
        version = _integer(
            _required(table, "directiveVersion", "directive"),
            "directive.directiveVersion",
        )
        if version != DIRECTIVE_VERSION:
            raise IncompatibleVersion(f"directiveVersion {version} is not supported")
        try:
            kind = DirectiveKind(
                _string(_required(table, "kind", "directive"), "directive.kind")
            )
        except ValueError as error:
            raise ProtocolError("directive.kind is unsupported") from error
        error_value = table.get("error")
        error = None
        if error_value is not None:
            normalized = _failure(error_value, "directive.error")
            error = FailureRecord(
                str(normalized["code"]),
                str(normalized["message"]),
                bool(normalized["retryable"]),
            )
        return cls(
            _uuid(_required(table, "requestId", "directive"), "directive.requestId"),
            _uuid(_required(table, "hostId", "directive"), "directive.hostId"),
            kind,
            None
            if table.get("viewId") is None
            else _uuid(table["viewId"], "directive.viewId"),
            None
            if table.get("viewRevision") is None
            else _integer(table["viewRevision"], "directive.viewRevision"),
            None
            if table.get("desktopToken") is None
            else _string(table["desktopToken"], "directive.desktopToken", maximum=256),
            None
            if table.get("leaseExpiresAt") is None
            else _integer(table["leaseExpiresAt"], "directive.leaseExpiresAt"),
            error,
        )


_PROJECT_FIELDS: Final[dict[str, Validator]] = {
    "projectId": _uuid,
    "name": _string_validator(256),
    "aliases": _string_array,
    "defaultProvider": _optional(_enum_validator(ProviderId)),
    "defaultTransport": _enum_validator(Transport),
    "taskPush": _optional(_string_validator(32)),
    "completeReturn": _optional(_string_validator(32)),
    "declared": _boolean,
}
_REPOSITORY_FIELDS: Final[dict[str, Validator]] = {
    "repositoryId": _uuid,
    "name": _string_validator(256),
    "kind": _enum_validator(RepositoryKind),
    "contextSources": _string_array,
    "declared": _boolean,
}
_MEMBERSHIP_FIELDS: Final[dict[str, Validator]] = {
    "projectId": _uuid,
    "repositoryId": _uuid,
    "isPrimary": _boolean,
}
_CHECKOUT_FIELDS: Final[dict[str, Validator]] = {
    "checkoutId": _uuid,
    "repositoryId": _uuid,
    "hostId": _uuid,
    "kind": _string_validator(32),
    "displayName": _optional(_string_validator(256)),
    "providerOverride": _optional(_enum_validator(ProviderId)),
    "isDefault": _boolean,
    "declared": _boolean,
}
_WORK_CONTEXT_FIELDS: Final[dict[str, Validator]] = {
    "workContextId": _uuid,
    "hostId": _uuid,
    "projectId": _uuid,
    "checkoutId": _uuid,
    "claimState": _enum_validator(ClaimState),
    "claimGeneration": _integer,
    "foregroundFrameId": _optional(_uuid),
    "backgroundState": _enum_validator(BackgroundState),
    "updatedAt": _integer,
}
_FRAME_FIELDS: Final[dict[str, Validator]] = {
    "frameId": _uuid,
    "hostId": _uuid,
    "projectId": _uuid,
    "role": _enum_validator(FrameRole),
    "parentFrameId": _optional(_uuid),
    "workContextId": _uuid,
    "title": _string_validator(256),
    "preferredProvider": _optional(_enum_validator(ProviderId)),
    "lifecycleState": _enum_validator(FrameLifecycleState),
    "closeReason": _optional(_enum_validator(CloseReason)),
    "currentSessionKey": _optional(_session_key),
    "createdBy": _enum_validator(CreatedBy),
    "createdAt": _integer,
    "updatedAt": _integer,
}
_FRAME_SESSION_FIELDS: Final[dict[str, Validator]] = {
    "frameSessionId": _uuid,
    "frameId": _uuid,
    "sessionKey": _session_key,
    "ordinal": _positive_integer,
    "membershipReason": _enum_validator(MembershipReason),
    "joinedAt": _integer,
}
_SESSION_FIELDS: Final[dict[str, Validator]] = {
    "sessionKey": _session_key,
    "hostId": _uuid,
    "provider": _enum_validator(ProviderId),
    "projectId": _optional(_uuid),
    "checkoutId": _optional(_uuid),
    "name": _optional(_string_validator(512)),
    "pinned": _boolean,
    "runtimePresence": _enum_validator(RuntimePresence),
    "resumability": _enum_validator(Resumability),
    "activity": _enum_validator(Activity),
    "activityReason": _enum_validator(ActivityReason),
    "createdAt": _optional(_integer),
    "providerUpdatedAt": _optional(_integer),
    "lastObservedAt": _integer,
    "updatedAt": _integer,
}
_SURFACE_FIELDS: Final[dict[str, Validator]] = {
    "surfaceId": _uuid,
    "hostId": _uuid,
    "provider": _enum_validator(ProviderId),
    "sessionKey": _optional(_session_key),
    "lifecycleState": _enum_validator(SurfaceState),
    "metadataGeneration": _integer,
    "createdAt": _integer,
    "updatedAt": _integer,
    "retiredAt": _optional(_integer),
}
_VIEW_FIELDS: Final[dict[str, Validator]] = {
    "viewId": _uuid,
    "hostId": _uuid,
    "mode": _enum_validator(ViewMode),
    "activeFrameId": _optional(_uuid),
    "state": _enum_validator(ViewState),
    "revision": _integer,
    "createdAt": _integer,
    "lastAttachedAt": _optional(_integer),
    "updatedAt": _integer,
}
_PLACEMENT_FIELDS: Final[dict[str, Validator]] = {
    "placementId": _uuid,
    "hostId": _uuid,
    "viewId": _uuid,
    "frameId": _uuid,
    "surfaceId": _optional(_uuid),
    "state": _enum_validator(PlacementState),
    "generation": _integer,
    "lastFocusedAt": _optional(_integer),
    "updatedAt": _integer,
}
_TRANSITION_FIELDS: Final[dict[str, Validator]] = {
    "transitionId": _uuid,
    "requestId": _uuid,
    "hostId": _uuid,
    "viewId": _uuid,
    "kind": _enum_validator(TransitionKind),
    "sourceFrameId": _optional(_uuid),
    "targetFrameId": _uuid,
    "workContextId": _optional(_uuid),
    "expectedViewRevision": _integer,
    "expectedClaimGeneration": _optional(_integer),
    "state": _enum_validator(TransitionState),
    "transportPhase": _enum_validator(TransportPhase),
    "failure": _optional_failure,
    "createdAt": _integer,
    "updatedAt": _integer,
}
_CONTROL_FIELDS: Final[dict[str, Validator]] = {
    "controlTurnId": _uuid,
    "transitionId": _uuid,
    "targetFrameId": _uuid,
    "targetSessionKey": _session_key,
    "kind": _enum_validator(ControlKind),
    "state": _enum_validator(ControlState),
    "submissionCount": _integer,
    "submittedAt": _optional(_integer),
    "claimedAt": _optional(_integer),
    "settledAt": _optional(_integer),
    "failure": _optional_failure,
}
_RECOVERY_FIELDS: Final[dict[str, Validator]] = {
    "recoveryId": _uuid,
    "hostId": _uuid,
    "kind": _string_validator(64),
    "subjectType": _string_validator(64),
    "subjectId": _string_validator(512),
    "actionability": _enum_validator(RecoveryActionability),
    "state": _enum_validator(RecoveryState),
    "explanation": _string_validator(1_024),
    "createdAt": _integer,
    "updatedAt": _integer,
}
_WARNING_FIELDS: Final[dict[str, Validator]] = {
    "code": _string_validator(64),
    "message": _string_validator(1_024),
    "hostId": _optional(_uuid),
    "subjectType": _optional(_string_validator(64)),
    "subjectId": _optional(_string_validator(512)),
}


def _truncation(value: object, path: str) -> dict[str, JsonValue]:
    table = _object(value, path)
    result: dict[str, JsonValue] = {}
    for collection, detail in table.items():
        name = _string(collection, f"{path}.key", maximum=64)
        counts = _record(
            detail,
            f"{path}.{name}",
            {"retainedCount": _integer, "emittedCount": _integer},
        )
        if int(counts["emittedCount"]) > int(counts["retainedCount"]):
            raise ProtocolError(f"{path}.{name} emitted count exceeds retained count")
        result[name] = counts
    return dict(sorted(result.items()))


def _normalized_host_state(value: object) -> dict[str, JsonValue]:
    safe = _safe_json(value, "envelope")
    table = _object(safe, "envelope")
    _versions(table, "hostStateVersion", HOST_STATE_VERSION)
    host = _record(
        _required(table, "host", "envelope"),
        "envelope.host",
        {"hostId": _uuid, "displayName": _string_validator(256)},
    )
    host_id = str(host["hostId"])
    result: dict[str, JsonValue] = {
        "schemaVersion": SCHEMA_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "hostStateVersion": HOST_STATE_VERSION,
        "generationId": _uuid(
            _required(table, "generationId", "envelope"), "envelope.generationId"
        ),
        "activationState": _enum_validator(ActivationState)(
            _required(table, "activationState", "envelope"),
            "envelope.activationState",
        ),
        "generatedAt": _integer(
            _required(table, "generatedAt", "envelope"), "envelope.generatedAt"
        ),
        "host": host,
        "projects": _records(
            _required(table, "projects", "envelope"),
            "envelope.projects",
            _PROJECT_FIELDS,
            ("projectId",),
        ),
        "repositories": _records(
            _required(table, "repositories", "envelope"),
            "envelope.repositories",
            _REPOSITORY_FIELDS,
            ("repositoryId",),
        ),
        "projectRepositories": _records(
            _required(table, "projectRepositories", "envelope"),
            "envelope.projectRepositories",
            _MEMBERSHIP_FIELDS,
            ("projectId", "repositoryId"),
        ),
        "checkouts": _records(
            _required(table, "checkouts", "envelope"),
            "envelope.checkouts",
            _CHECKOUT_FIELDS,
            ("checkoutId",),
        ),
        "workContexts": _records(
            _required(table, "workContexts", "envelope"),
            "envelope.workContexts",
            _WORK_CONTEXT_FIELDS,
            ("workContextId",),
        ),
        "frames": _records(
            _required(table, "frames", "envelope"),
            "envelope.frames",
            _FRAME_FIELDS,
            ("frameId",),
        ),
        "frameSessions": _records(
            _required(table, "frameSessions", "envelope"),
            "envelope.frameSessions",
            _FRAME_SESSION_FIELDS,
            ("frameId", "ordinal", "frameSessionId"),
        ),
        "sessions": _records(
            _required(table, "sessions", "envelope"),
            "envelope.sessions",
            _SESSION_FIELDS,
            ("sessionKey",),
        ),
        "surfaces": _records(
            _required(table, "surfaces", "envelope"),
            "envelope.surfaces",
            _SURFACE_FIELDS,
            ("surfaceId",),
        ),
        "views": _records(
            _required(table, "views", "envelope"),
            "envelope.views",
            _VIEW_FIELDS,
            ("viewId",),
        ),
        "placements": _records(
            _required(table, "placements", "envelope"),
            "envelope.placements",
            _PLACEMENT_FIELDS,
            ("placementId",),
        ),
        "transitions": _records(
            _required(table, "transitions", "envelope"),
            "envelope.transitions",
            _TRANSITION_FIELDS,
            ("transitionId",),
        ),
        "controlTurns": _records(
            _required(table, "controlTurns", "envelope"),
            "envelope.controlTurns",
            _CONTROL_FIELDS,
            ("controlTurnId",),
        ),
        "recoveries": _records(
            _required(table, "recoveries", "envelope"),
            "envelope.recoveries",
            _RECOVERY_FIELDS,
            ("recoveryId",),
        ),
        "warnings": _records(
            _required(table, "warnings", "envelope"),
            "envelope.warnings",
            _WARNING_FIELDS,
            ("code", "subjectId"),
        ),
        "truncation": _truncation(
            _required(table, "truncation", "envelope"), "envelope.truncation"
        ),
    }
    projects = {str(item["projectId"]) for item in result["projects"]}  # type: ignore[index]
    repositories = {
        str(item["repositoryId"])
        for item in result["repositories"]  # type: ignore[index]
    }
    checkouts = {str(item["checkoutId"]): item for item in result["checkouts"]}  # type: ignore[index]
    contexts = {
        str(item["workContextId"]): item
        for item in result["workContexts"]  # type: ignore[index]
    }
    frames = {str(item["frameId"]): item for item in result["frames"]}  # type: ignore[index]
    sessions = {str(item["sessionKey"]): item for item in result["sessions"]}  # type: ignore[index]
    surfaces = {str(item["surfaceId"]): item for item in result["surfaces"]}  # type: ignore[index]
    views = {str(item["viewId"]): item for item in result["views"]}  # type: ignore[index]
    transitions = {
        str(item["transitionId"]): item
        for item in result["transitions"]  # type: ignore[index]
    }
    membership_pairs = {
        (str(item["projectId"]), str(item["repositoryId"]))
        for item in result["projectRepositories"]  # type: ignore[union-attr]
    }
    frame_session_pairs = {
        (str(item["frameId"]), str(item["sessionKey"]))
        for item in result["frameSessions"]  # type: ignore[union-attr]
    }
    for membership in result["projectRepositories"]:  # type: ignore[union-attr]
        if (
            str(membership["projectId"]) not in projects
            or str(membership["repositoryId"]) not in repositories
        ):
            raise ProtocolError("project repository references missing catalog rows")
    for checkout in checkouts.values():
        if (
            checkout["hostId"] != host_id
            or str(checkout["repositoryId"]) not in repositories
        ):
            raise ProtocolError("checkout reference or owner host is invalid")
    for context in contexts.values():
        checkout = checkouts.get(str(context["checkoutId"]))
        if (
            context["hostId"] != host_id
            or str(context["projectId"]) not in projects
            or checkout is None
            or (
                str(context["projectId"]),
                str(checkout["repositoryId"]),
            )
            not in membership_pairs
        ):
            raise ProtocolError("WorkContext reference or owner host is invalid")
    for frame in frames.values():
        context = contexts.get(str(frame["workContextId"]))
        parent = (
            None
            if frame["parentFrameId"] is None
            else frames.get(str(frame["parentFrameId"]))
        )
        if (
            frame["hostId"] != host_id
            or str(frame["projectId"]) not in projects
            or context is None
            or context["projectId"] != frame["projectId"]
            or (frame["parentFrameId"] is not None and parent is None)
            or (
                parent is not None
                and (
                    parent["projectId"] != frame["projectId"]
                    or parent["workContextId"] != frame["workContextId"]
                )
            )
            or (
                frame["currentSessionKey"] is not None
                and (
                    str(frame["currentSessionKey"]) not in sessions
                    or (
                        str(frame["frameId"]),
                        str(frame["currentSessionKey"]),
                    )
                    not in frame_session_pairs
                )
            )
        ):
            raise ProtocolError("frame reference or owner host is invalid")
    for context in contexts.values():
        foreground = context["foregroundFrameId"]
        if foreground is not None and (
            str(foreground) not in frames
            or frames[str(foreground)]["workContextId"] != context["workContextId"]
        ):
            raise ProtocolError("WorkContext foreground frame is invalid")
    for membership in result["frameSessions"]:  # type: ignore[union-attr]
        if (
            str(membership["frameId"]) not in frames
            or str(membership["sessionKey"]) not in sessions
        ):
            raise ProtocolError("frame session reference is invalid")
        frame = frames[str(membership["frameId"])]
        session = sessions[str(membership["sessionKey"])]
        if (
            session["hostId"] != frame["hostId"]
            or session["projectId"] != frame["projectId"]
        ):
            raise ProtocolError("frame session identity is invalid")
    for session in sessions.values():
        session_parts = str(session["sessionKey"]).split(":")
        if (
            session["hostId"] != host_id
            or session_parts[0] != host_id
            or session_parts[1] != session["provider"]
        ):
            raise ProtocolError("provider session owner host is invalid")
        if (
            session["projectId"] is not None
            and str(session["projectId"]) not in projects
        ):
            raise ProtocolError("provider session project is missing")
        if session["checkoutId"] is not None:
            checkout = checkouts.get(str(session["checkoutId"]))
            if (
                checkout is None
                or session["projectId"] is None
                or (
                    str(session["projectId"]),
                    str(checkout["repositoryId"]),
                )
                not in membership_pairs
            ):
                raise ProtocolError("provider session checkout is invalid")
    for surface in surfaces.values():
        if surface["hostId"] != host_id or (
            surface["sessionKey"] is not None
            and str(surface["sessionKey"]) not in sessions
        ):
            raise ProtocolError("surface reference or owner host is invalid")
        if surface["sessionKey"] is not None:
            session = sessions[str(surface["sessionKey"])]
            if session["provider"] != surface["provider"]:
                raise ProtocolError("surface provider identity is invalid")
    for view in views.values():
        if view["hostId"] != host_id or (
            view["activeFrameId"] is not None
            and str(view["activeFrameId"]) not in frames
        ):
            raise ProtocolError("view reference or owner host is invalid")
    for placement in result["placements"]:  # type: ignore[union-attr]
        if (
            placement["hostId"] != host_id
            or str(placement["viewId"]) not in views
            or str(placement["frameId"]) not in frames
            or (
                placement["surfaceId"] is not None
                and str(placement["surfaceId"]) not in surfaces
            )
        ):
            raise ProtocolError("placement reference or owner host is invalid")
    for transition in transitions.values():
        if (
            transition["hostId"] != host_id
            or str(transition["viewId"]) not in views
            or str(transition["targetFrameId"]) not in frames
            or (
                transition["sourceFrameId"] is not None
                and str(transition["sourceFrameId"]) not in frames
            )
            or (
                transition["workContextId"] is not None
                and str(transition["workContextId"]) not in contexts
            )
        ):
            raise ProtocolError("transition reference or owner host is invalid")
    for control in result["controlTurns"]:  # type: ignore[union-attr]
        if (
            str(control["transitionId"]) not in transitions
            or str(control["targetFrameId"]) not in frames
            or str(control["targetSessionKey"]) not in sessions
        ):
            raise ProtocolError("control turn reference is invalid")
        if (
            str(control["targetFrameId"]),
            str(control["targetSessionKey"]),
        ) not in frame_session_pairs:
            raise ProtocolError("control turn target membership is invalid")
    for recovery in result["recoveries"]:  # type: ignore[union-attr]
        if recovery["hostId"] != host_id:
            raise ProtocolError("recovery owner host is invalid")
    return result


@dataclass(frozen=True, slots=True)
class HostState:
    data: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", _normalized_host_state(self.data))

    @property
    def host_id(self) -> HostId:
        host = self.data["host"]
        assert isinstance(host, dict)
        return HostId(str(host["hostId"]))

    @property
    def generated_at(self) -> int:
        return int(self.data["generatedAt"])  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, JsonValue]:
        value = json.loads(self.to_json())
        assert isinstance(value, dict)
        return value

    def to_json(self) -> str:
        return _dump(self.data)

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> HostState:
        return cls(_decode(raw))


def _failure_dict(row: Mapping[str, Any]) -> JsonValue:
    if row["failure_code"] is None:
        return None
    return {
        "code": str(row["failure_code"]),
        "message": str(row["failure_message"]),
        "retryable": bool(row["failure_retryable"]),
    }


def _bounded_collection(
    name: str,
    records: list[dict[str, JsonValue]],
    limit: int,
    truncation: dict[str, JsonValue],
) -> list[JsonValue]:
    if len(records) > limit:
        truncation[name] = {
            "retainedCount": len(records),
            "emittedCount": limit,
        }
        return records[:limit]
    return records


def _cohere_host_collections(
    projected: dict[str, list[dict[str, JsonValue]]],
    retained_counts: Mapping[str, int],
    truncation: dict[str, JsonValue],
) -> None:
    """Drop dependents until every truncated HostState reference is internal."""

    while True:
        before = {name: len(records) for name, records in projected.items()}
        project_ids = {str(item["projectId"]) for item in projected["projects"]}
        repository_ids = {
            str(item["repositoryId"]) for item in projected["repositories"]
        }
        checkout_ids = {str(item["checkoutId"]) for item in projected["checkouts"]}
        context_ids = {str(item["workContextId"]) for item in projected["workContexts"]}
        frame_ids = {str(item["frameId"]) for item in projected["frames"]}
        session_keys = {str(item["sessionKey"]) for item in projected["sessions"]}
        surface_ids = {str(item["surfaceId"]) for item in projected["surfaces"]}
        view_ids = {str(item["viewId"]) for item in projected["views"]}
        transition_ids = {
            str(item["transitionId"]) for item in projected["transitions"]
        }
        projected["projectRepositories"] = [
            item
            for item in projected["projectRepositories"]
            if str(item["projectId"]) in project_ids
            and str(item["repositoryId"]) in repository_ids
        ]
        projected["checkouts"] = [
            item
            for item in projected["checkouts"]
            if str(item["repositoryId"]) in repository_ids
        ]
        projected["sessions"] = [
            item
            for item in projected["sessions"]
            if (item["projectId"] is None or str(item["projectId"]) in project_ids)
            and (item["checkoutId"] is None or str(item["checkoutId"]) in checkout_ids)
        ]
        projected["workContexts"] = [
            item
            for item in projected["workContexts"]
            if str(item["projectId"]) in project_ids
            and str(item["checkoutId"]) in checkout_ids
            and (
                item["foregroundFrameId"] is None
                or str(item["foregroundFrameId"]) in frame_ids
            )
        ]
        projected["frames"] = [
            item
            for item in projected["frames"]
            if str(item["projectId"]) in project_ids
            and str(item["workContextId"]) in context_ids
            and (
                item["parentFrameId"] is None or str(item["parentFrameId"]) in frame_ids
            )
            and (
                item["currentSessionKey"] is None
                or str(item["currentSessionKey"]) in session_keys
            )
        ]
        projected["frameSessions"] = [
            item
            for item in projected["frameSessions"]
            if str(item["frameId"]) in frame_ids
            and str(item["sessionKey"]) in session_keys
        ]
        projected["surfaces"] = [
            item
            for item in projected["surfaces"]
            if item["sessionKey"] is None or str(item["sessionKey"]) in session_keys
        ]
        projected["views"] = [
            item
            for item in projected["views"]
            if item["activeFrameId"] is None or str(item["activeFrameId"]) in frame_ids
        ]
        projected["placements"] = [
            item
            for item in projected["placements"]
            if str(item["viewId"]) in view_ids
            and str(item["frameId"]) in frame_ids
            and (item["surfaceId"] is None or str(item["surfaceId"]) in surface_ids)
        ]
        projected["transitions"] = [
            item
            for item in projected["transitions"]
            if str(item["viewId"]) in view_ids
            and str(item["targetFrameId"]) in frame_ids
            and (
                item["sourceFrameId"] is None or str(item["sourceFrameId"]) in frame_ids
            )
            and (
                item["workContextId"] is None
                or str(item["workContextId"]) in context_ids
            )
        ]
        projected["controlTurns"] = [
            item
            for item in projected["controlTurns"]
            if str(item["transitionId"]) in transition_ids
            and str(item["targetFrameId"]) in frame_ids
            and str(item["targetSessionKey"]) in session_keys
        ]
        after = {name: len(records) for name, records in projected.items()}
        if after == before:
            break
    for name, retained_count in retained_counts.items():
        emitted_count = len(projected[name])
        if emitted_count < retained_count:
            truncation[name] = {
                "retainedCount": retained_count,
                "emittedCount": emitted_count,
            }


def build_host_state(
    registry: Registry,
    *,
    generated_at: int,
    collection_limit: int = DEFAULT_COLLECTION_LIMIT,
    view_state_overrides: Mapping[str, ViewState | str] | None = None,
    additional_warnings: Sequence[Mapping[str, object]] = (),
) -> HostState:
    """Project one registry into the bounded owner-host HostState v1 envelope."""

    generated_at = _integer(generated_at, "generated_at")
    if not 1 <= collection_limit <= MAX_JSON_ARRAY_ITEMS:
        raise ValueError("collection_limit is outside protocol bounds")
    connection = registry.connection
    metadata = registry.metadata()
    host_row = connection.execute(
        "SELECT host_id, display_name FROM hosts WHERE is_local = 1"
    ).fetchone()
    if host_row is None:
        raise ProtocolError("registry local host is missing")
    host_id = str(host_row["host_id"])
    truncation: dict[str, JsonValue] = {}
    overrides = {
        str(view_id): (
            state.value if isinstance(state, ViewState) else ViewState(state).value
        )
        for view_id, state in (view_state_overrides or {}).items()
    }

    projects = [
        {
            "projectId": str(row["project_id"]),
            "name": str(row["name"]),
            "aliases": json.loads(row["aliases_json"]),
            "defaultProvider": row["default_provider"],
            "defaultTransport": str(row["default_transport"]),
            "taskPush": row["task_push"],
            "completeReturn": row["complete_return"],
            "declared": bool(row["declared"]),
        }
        for row in connection.execute("SELECT * FROM projects ORDER BY project_id")
    ]
    repositories = [
        {
            "repositoryId": str(row["repository_id"]),
            "name": str(row["name"]),
            "kind": str(row["kind"]),
            "contextSources": json.loads(row["context_sources_json"]),
            "declared": bool(row["declared"]),
        }
        for row in connection.execute(
            "SELECT * FROM repositories ORDER BY repository_id"
        )
    ]
    memberships = [
        {
            "projectId": str(row["project_id"]),
            "repositoryId": str(row["repository_id"]),
            "isPrimary": bool(row["is_primary"]),
        }
        for row in connection.execute(
            "SELECT * FROM project_repositories ORDER BY project_id, repository_id"
        )
    ]
    checkouts = [
        {
            "checkoutId": str(row["checkout_id"]),
            "repositoryId": str(row["repository_id"]),
            "hostId": str(row["host_id"]),
            "kind": str(row["kind"]),
            "displayName": row["display_name"],
            "providerOverride": row["provider_override"],
            "isDefault": bool(row["is_default"]),
            "declared": bool(row["declared"]),
        }
        for row in connection.execute("SELECT * FROM checkouts ORDER BY checkout_id")
    ]
    contexts = [
        {
            "workContextId": str(row["work_context_id"]),
            "hostId": str(row["host_id"]),
            "projectId": str(row["project_id"]),
            "checkoutId": str(row["checkout_id"]),
            "claimState": str(row["claim_state"]),
            "claimGeneration": int(row["claim_generation"]),
            "foregroundFrameId": row["foreground_frame_id"],
            "backgroundState": str(row["background_state"]),
            "updatedAt": int(row["updated_at"]),
        }
        for row in connection.execute(
            "SELECT * FROM work_contexts ORDER BY work_context_id"
        )
    ]
    frames = [
        {
            "frameId": str(row["frame_id"]),
            "hostId": str(row["host_id"]),
            "projectId": str(row["project_id"]),
            "role": str(row["role"]),
            "parentFrameId": row["parent_frame_id"],
            "workContextId": str(row["work_context_id"]),
            "title": str(row["title"]),
            "preferredProvider": row["preferred_provider"],
            "lifecycleState": str(row["lifecycle_state"]),
            "closeReason": row["close_reason"],
            "currentSessionKey": row["current_session_key"],
            "createdBy": str(row["created_by"]),
            "createdAt": int(row["created_at"]),
            "updatedAt": int(row["updated_at"]),
        }
        for row in connection.execute("SELECT * FROM frames ORDER BY frame_id")
    ]
    frame_sessions = [
        {
            "frameSessionId": str(row["frame_session_id"]),
            "frameId": str(row["frame_id"]),
            "sessionKey": str(row["session_key"]),
            "ordinal": int(row["ordinal"]),
            "membershipReason": str(row["membership_reason"]),
            "joinedAt": int(row["joined_at"]),
        }
        for row in connection.execute(
            "SELECT * FROM frame_sessions ORDER BY frame_id, ordinal, frame_session_id"
        )
    ]
    sessions = [
        {
            "sessionKey": str(row["session_key"]),
            "hostId": str(row["host_id"]),
            "provider": str(row["provider"]),
            "projectId": row["project_id"],
            "checkoutId": row["checkout_id"],
            "name": row["name"],
            "pinned": bool(row["pinned"]),
            "runtimePresence": str(row["runtime_presence"]),
            "resumability": str(row["resumability"]),
            "activity": str(row["activity"]),
            "activityReason": str(row["activity_reason"]),
            "createdAt": row["created_at"],
            "providerUpdatedAt": row["provider_updated_at"],
            "lastObservedAt": int(row["last_observed_at"]),
            "updatedAt": int(row["updated_at"]),
        }
        for row in connection.execute(
            "SELECT * FROM provider_sessions ORDER BY session_key"
        )
    ]
    surfaces = [
        {
            "surfaceId": str(row["surface_id"]),
            "hostId": str(row["host_id"]),
            "provider": str(row["provider"]),
            "sessionKey": row["session_key"],
            "lifecycleState": str(row["lifecycle_state"]),
            "metadataGeneration": int(row["metadata_generation"]),
            "createdAt": int(row["created_at"]),
            "updatedAt": int(row["updated_at"]),
            "retiredAt": row["retired_at"],
        }
        for row in connection.execute("SELECT * FROM surfaces ORDER BY surface_id")
    ]
    views = [
        {
            "viewId": str(row["view_id"]),
            "hostId": str(row["host_id"]),
            "mode": str(row["mode"]),
            "activeFrameId": row["active_frame_id"],
            "state": overrides.get(str(row["view_id"]), str(row["state"])),
            "revision": int(row["revision"]),
            "createdAt": int(row["created_at"]),
            "lastAttachedAt": row["last_attached_at"],
            "updatedAt": int(row["updated_at"]),
        }
        for row in connection.execute("SELECT * FROM user_views ORDER BY view_id")
    ]
    unknown_overrides = set(overrides) - {str(view["viewId"]) for view in views}
    if unknown_overrides:
        raise ValueError("view state override references an unknown view")
    placements = [
        {
            "placementId": str(row["placement_id"]),
            "hostId": str(row["host_id"]),
            "viewId": str(row["view_id"]),
            "frameId": str(row["frame_id"]),
            "surfaceId": row["surface_id"],
            "state": str(row["state"]),
            "generation": int(row["generation"]),
            "lastFocusedAt": row["last_focused_at"],
            "updatedAt": int(row["updated_at"]),
        }
        for row in connection.execute(
            "SELECT * FROM frame_placements ORDER BY placement_id"
        )
    ]
    transitions = [
        {
            "transitionId": str(row["transition_id"]),
            "requestId": str(row["request_id"]),
            "hostId": str(row["host_id"]),
            "viewId": str(row["view_id"]),
            "kind": str(row["kind"]),
            "sourceFrameId": row["source_frame_id"],
            "targetFrameId": str(row["target_frame_id"]),
            "workContextId": row["work_context_id"],
            "expectedViewRevision": int(row["expected_view_revision"]),
            "expectedClaimGeneration": row["expected_claim_generation"],
            "state": str(row["state"]),
            "transportPhase": str(row["transport_phase"]),
            "failure": _failure_dict(row),
            "createdAt": int(row["created_at"]),
            "updatedAt": int(row["updated_at"]),
        }
        for row in connection.execute(
            "SELECT * FROM view_transitions ORDER BY transition_id"
        )
    ]
    controls = [
        {
            "controlTurnId": str(row["control_turn_id"]),
            "transitionId": str(row["transition_id"]),
            "targetFrameId": str(row["target_frame_id"]),
            "targetSessionKey": str(row["target_session_key"]),
            "kind": str(row["kind"]),
            "state": str(row["state"]),
            "submissionCount": int(row["submission_count"]),
            "submittedAt": row["submitted_at"],
            "claimedAt": row["claimed_at"],
            "settledAt": row["settled_at"],
            "failure": _failure_dict(row),
        }
        for row in connection.execute(
            "SELECT * FROM control_turns ORDER BY control_turn_id"
        )
    ]
    recoveries = [
        {
            "recoveryId": str(row["recovery_id"]),
            "hostId": str(row["host_id"]),
            "kind": str(row["kind"]),
            "subjectType": str(row["subject_type"]),
            "subjectId": str(row["subject_id"]),
            "actionability": str(row["actionability"]),
            "state": str(row["state"]),
            "explanation": str(row["bounded_explanation"]),
            "createdAt": int(row["created_at"]),
            "updatedAt": int(row["updated_at"]),
        }
        for row in connection.execute("SELECT * FROM recoveries ORDER BY recovery_id")
    ]

    collections = {
        "projects": projects,
        "repositories": repositories,
        "projectRepositories": memberships,
        "checkouts": checkouts,
        "workContexts": contexts,
        "frames": frames,
        "frameSessions": frame_sessions,
        "sessions": sessions,
        "surfaces": surfaces,
        "views": views,
        "placements": placements,
        "transitions": transitions,
        "controlTurns": controls,
        "recoveries": recoveries,
    }
    retained_counts = {name: len(records) for name, records in collections.items()}
    projected: dict[str, list[dict[str, JsonValue]]] = {
        name: list(_bounded_collection(name, records, collection_limit, truncation))
        for name, records in collections.items()
    }
    _cohere_host_collections(projected, retained_counts, truncation)
    warnings: list[dict[str, JsonValue]] = [
        dict(warning)  # type: ignore[arg-type]
        for warning in additional_warnings
    ]
    if truncation:
        warnings.append(
            {
                "code": "projection_truncated",
                "message": "One or more HostState collections were truncated.",
                "hostId": host_id,
                "subjectType": None,
                "subjectId": None,
            }
        )
    return HostState(
        {
            "schemaVersion": SCHEMA_VERSION,
            "protocolVersion": PROTOCOL_VERSION,
            "hostStateVersion": HOST_STATE_VERSION,
            "generationId": str(metadata["generation_id"]),
            "activationState": str(metadata["activation_state"]),
            "generatedAt": generated_at,
            "host": {
                "hostId": host_id,
                "displayName": str(host_row["display_name"]),
            },
            **projected,
            "warnings": warnings,
            "truncation": truncation,
        }
    )


_NAV_HOST_FIELDS: Final[dict[str, Validator]] = {
    "hostId": _uuid,
    "generationId": _uuid,
    "displayName": _string_validator(256),
    "isLocal": _boolean,
    "reachability": _string_validator(32),
    "stale": _boolean,
    "generatedAt": _integer,
    "activationState": _enum_validator(ActivationState),
}


def _nav_breadcrumb(value: object, path: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ProtocolError(f"{path} must be an array")
    if len(value) > 32:
        raise ProtocolError(f"{path} exceeds the breadcrumb depth limit")
    return [
        _string(item, f"{path}[{index}]", maximum=256)
        for index, item in enumerate(value)
    ]


_NAV_VIEW_FIELDS: Final[dict[str, Validator]] = {
    "hostId": _uuid,
    "viewId": _uuid,
    "mode": _enum_validator(ViewMode),
    "state": _enum_validator(ViewState),
    "revision": _integer,
    "activeFrameId": _optional(_uuid),
    "activeProjectId": _optional(_uuid),
    "title": _string_validator(256),
    "breadcrumb": _nav_breadcrumb,
    "activity": _enum_validator(Activity),
    "attention": _string_validator(32),
    "transitionState": _optional(_enum_validator(TransitionState)),
    "controlState": _optional(_enum_validator(ControlState)),
    "lastActivityAt": _optional(_integer),
}
_NAV_FRAME_FIELDS: Final[dict[str, Validator]] = {
    "frameId": _uuid,
    "title": _string_validator(256),
    "role": _enum_validator(FrameRole),
    "parentFrameId": _optional(_uuid),
    "lifecycleState": _enum_validator(FrameLifecycleState),
    "activity": _enum_validator(Activity),
    "currentSession": _optional(
        lambda value, path: _record(
            value,
            path,
            {
                "provider": _enum_validator(ProviderId),
                "runtimePresence": _enum_validator(RuntimePresence),
                "resumability": _enum_validator(Resumability),
                "activity": _enum_validator(Activity),
                "updatedAt": _integer,
            },
        )
    ),
    "sessionCount": _integer,
}


def _nav_frames(value: object, path: str) -> list[JsonValue]:
    return _records(value, path, _NAV_FRAME_FIELDS, ("frameId",))


_NAV_PROJECT_FIELDS: Final[dict[str, Validator]] = {
    "hostId": _uuid,
    "projectId": _uuid,
    "name": _string_validator(256),
    "viewId": _optional(_uuid),
    "entryFrameId": _optional(_uuid),
    "frames": _nav_frames,
}


def _normalized_navigator_state(value: object) -> dict[str, JsonValue]:
    safe = _safe_json(value, "envelope")
    table = _object(safe, "envelope")
    _versions(table, "navigatorVersion", NAVIGATOR_VERSION)
    result: dict[str, JsonValue] = {
        "schemaVersion": SCHEMA_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "navigatorVersion": NAVIGATOR_VERSION,
        "generationId": _uuid(
            _required(table, "generationId", "envelope"), "envelope.generationId"
        ),
        "generatedAt": _integer(
            _required(table, "generatedAt", "envelope"), "envelope.generatedAt"
        ),
        "localHostId": _uuid(
            _required(table, "localHostId", "envelope"), "envelope.localHostId"
        ),
        "hosts": _records(
            _required(table, "hosts", "envelope"),
            "envelope.hosts",
            _NAV_HOST_FIELDS,
            ("hostId",),
        ),
        "views": _records(
            _required(table, "views", "envelope"),
            "envelope.views",
            _NAV_VIEW_FIELDS,
            ("hostId", "viewId"),
        ),
        "projects": _records(
            _required(table, "projects", "envelope"),
            "envelope.projects",
            _NAV_PROJECT_FIELDS,
            ("hostId", "projectId"),
        ),
        "recoveries": _records(
            _required(table, "recoveries", "envelope"),
            "envelope.recoveries",
            _RECOVERY_FIELDS,
            ("hostId", "recoveryId"),
        ),
        "warnings": _records(
            _required(table, "warnings", "envelope"),
            "envelope.warnings",
            _WARNING_FIELDS,
            ("hostId", "code", "subjectId"),
        ),
        "truncation": _truncation(
            _required(table, "truncation", "envelope"), "envelope.truncation"
        ),
    }
    hosts = {str(item["hostId"]): item for item in result["hosts"]}  # type: ignore[index]
    local_host_id = str(result["localHostId"])
    if local_host_id not in hosts or hosts[local_host_id]["isLocal"] is not True:
        raise ProtocolError("navigator local host is missing or not local")
    if sum(item["isLocal"] is True for item in hosts.values()) != 1:
        raise ProtocolError("navigator must contain exactly one local host")
    views = {str(item["viewId"]): item for item in result["views"]}  # type: ignore[index]
    for view in views.values():
        if str(view["hostId"]) not in hosts:
            raise ProtocolError("navigator view owner host is missing")
    for project in result["projects"]:  # type: ignore[union-attr]
        project_host = str(project["hostId"])
        if project_host not in hosts:
            raise ProtocolError("navigator project owner host is missing")
        view_id = project["viewId"]
        if view_id is not None and (
            str(view_id) not in views or views[str(view_id)]["hostId"] != project_host
        ):
            raise ProtocolError("navigator project view reference is invalid")
        frame_ids = {str(frame["frameId"]) for frame in project["frames"]}
        entry = project["entryFrameId"]
        if entry is not None and str(entry) not in frame_ids:
            raise ProtocolError("navigator project entry frame is missing")
        for frame in project["frames"]:
            parent = frame["parentFrameId"]
            if parent is not None and str(parent) not in frame_ids:
                raise ProtocolError("navigator frame parent is missing")
    for recovery in result["recoveries"]:  # type: ignore[union-attr]
        if str(recovery["hostId"]) not in hosts:
            raise ProtocolError("navigator recovery owner host is missing")
    return result


@dataclass(frozen=True, slots=True)
class NavigatorState:
    data: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", _normalized_navigator_state(self.data))

    def to_dict(self) -> dict[str, JsonValue]:
        value = json.loads(self.to_json())
        assert isinstance(value, dict)
        return value

    def to_json(self) -> str:
        return _dump(self.data)

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> NavigatorState:
        return cls(_decode(raw))


def build_navigator_state(
    host_states: Sequence[HostState],
    *,
    local_host_id: HostId,
    generated_at: int,
    reachability: Mapping[str, str] | None = None,
    staleness: Mapping[str, bool] | None = None,
    collection_limit: int = DEFAULT_COLLECTION_LIMIT,
) -> NavigatorState:
    """Aggregate validated owner-host states into an authority-free navigator view."""

    generated_at = _integer(generated_at, "generated_at")
    if not 1 <= collection_limit <= MAX_JSON_ARRAY_ITEMS:
        raise ValueError("collection_limit is outside protocol bounds")
    by_host: dict[str, HostState] = {}
    for state in host_states:
        key = str(state.host_id)
        if key in by_host:
            raise ProtocolError("navigator contains duplicate host state")
        by_host[key] = state
    local_key = str(local_host_id)
    if local_key not in by_host:
        raise ProtocolError("navigator local HostState is missing")
    reachability = {} if reachability is None else reachability
    staleness = {} if staleness is None else staleness
    hosts: list[dict[str, JsonValue]] = []
    views: list[dict[str, JsonValue]] = []
    projects: list[dict[str, JsonValue]] = []
    recoveries: list[dict[str, JsonValue]] = []
    warnings: list[dict[str, JsonValue]] = []
    truncation: dict[str, JsonValue] = {}
    for host_id, state in sorted(by_host.items()):
        data = state.to_dict()
        host = data["host"]
        assert isinstance(host, dict)
        is_local = host_id == local_key
        host_reachability = (
            "online" if is_local else reachability.get(host_id, "unknown")
        )
        if host_reachability not in {"online", "offline", "unknown"}:
            raise ProtocolError("navigator reachability is unsupported")
        hosts.append(
            {
                "hostId": host_id,
                "generationId": str(data["generationId"]),
                "displayName": str(host["displayName"]),
                "isLocal": is_local,
                "reachability": host_reachability,
                "stale": False if is_local else staleness.get(host_id, True),
                "generatedAt": int(data["generatedAt"]),
                "activationState": str(data["activationState"]),
            }
        )
        host_views = {
            str(view["viewId"]): view
            for view in data["views"]  # type: ignore[union-attr]
        }
        frames_by_id = {
            str(frame["frameId"]): frame
            for frame in data["frames"]  # type: ignore[union-attr]
        }
        sessions = {
            str(session["sessionKey"]): session
            for session in data["sessions"]  # type: ignore[union-attr]
        }
        transitions_by_view: dict[str, dict[str, JsonValue]] = {}
        active_transition_states = {
            TransitionState.PREPARED.value,
            TransitionState.EXECUTING.value,
            TransitionState.PRESENTED.value,
            TransitionState.AWAITING_CLAIM.value,
            TransitionState.SETTLING.value,
        }
        for transition in data["transitions"]:  # type: ignore[union-attr]
            if transition["state"] not in active_transition_states:
                continue
            key = str(transition["viewId"])
            current = transitions_by_view.get(key)
            if current is None or int(transition["updatedAt"]) > int(
                current["updatedAt"]
            ):
                transitions_by_view[key] = transition
        controls_by_transition = {
            str(control["transitionId"]): control
            for control in data["controlTurns"]  # type: ignore[union-attr]
        }
        open_recovery_subjects = {
            str(recovery["subjectId"])
            for recovery in data["recoveries"]  # type: ignore[union-attr]
            if recovery["state"] == RecoveryState.OPEN.value
        }
        for view in host_views.values():
            frame = (
                None
                if view["activeFrameId"] is None
                else frames_by_id.get(str(view["activeFrameId"]))
            )
            session = (
                None
                if frame is None or frame["currentSessionKey"] is None
                else sessions.get(str(frame["currentSessionKey"]))
            )
            breadcrumb: list[str] = []
            cursor = frame
            seen: set[str] = set()
            while cursor is not None and str(cursor["frameId"]) not in seen:
                seen.add(str(cursor["frameId"]))
                breadcrumb.append(str(cursor["title"]))
                parent = cursor["parentFrameId"]
                cursor = None if parent is None else frames_by_id.get(str(parent))
            breadcrumb.reverse()
            transition = transitions_by_view.get(str(view["viewId"]))
            control = (
                None
                if transition is None
                else controls_by_transition.get(str(transition["transitionId"]))
            )
            activity = (
                Activity.UNKNOWN.value if session is None else str(session["activity"])
            )
            if str(view["viewId"]) in open_recovery_subjects or (
                frame is not None and str(frame["frameId"]) in open_recovery_subjects
            ):
                attention = "recovery"
            elif view["state"] == ViewState.DEGRADED.value:
                attention = "degraded"
            elif activity == Activity.NEEDS_INPUT.value:
                attention = "needs_input"
            else:
                attention = "none"
            views.append(
                {
                    "hostId": host_id,
                    "viewId": str(view["viewId"]),
                    "mode": str(view["mode"]),
                    "state": str(view["state"]),
                    "revision": int(view["revision"]),
                    "activeFrameId": view["activeFrameId"],
                    "activeProjectId": None if frame is None else frame["projectId"],
                    "title": "Empty view" if frame is None else str(frame["title"]),
                    "breadcrumb": breadcrumb,
                    "activity": activity,
                    "attention": attention,
                    "transitionState": (
                        None if transition is None else str(transition["state"])
                    ),
                    "controlState": None if control is None else str(control["state"]),
                    "lastActivityAt": (
                        int(view["updatedAt"])
                        if session is None
                        else int(session["updatedAt"])
                    ),
                }
            )
        all_frames = list(data["frames"])  # type: ignore[arg-type]
        frame_session_counts: dict[str, int] = {}
        for membership in data["frameSessions"]:  # type: ignore[union-attr]
            frame_key = str(membership["frameId"])
            frame_session_counts[frame_key] = frame_session_counts.get(frame_key, 0) + 1
        placements = list(data["placements"])  # type: ignore[arg-type]
        for project in data["projects"]:  # type: ignore[union-attr]
            if project["declared"] is not True:
                continue
            project_id = str(project["projectId"])
            project_frames = [
                frame for frame in all_frames if frame["projectId"] == project_id
            ]
            open_project_frames = [
                frame
                for frame in project_frames
                if frame["lifecycleState"] != FrameLifecycleState.CLOSED.value
            ]
            workspace = next(
                (
                    frame
                    for frame in open_project_frames
                    if frame["role"] == FrameRole.WORKSPACE.value
                ),
                None,
            )
            owning_placement = None
            if workspace is not None:
                owning_placement = next(
                    (
                        placement
                        for placement in placements
                        if placement["frameId"] == workspace["frameId"]
                        and placement["state"] != PlacementState.ORPHANED.value
                    ),
                    None,
                )
            view_id = (
                None if owning_placement is None else str(owning_placement["viewId"])
            )
            entry_frame_id = None
            if workspace is not None:
                entry_frame_id = str(workspace["frameId"])
                owner_view = None if view_id is None else host_views.get(view_id)
                if owner_view is not None and owner_view["activeFrameId"] is not None:
                    active = next(
                        (
                            frame
                            for frame in open_project_frames
                            if frame["frameId"] == owner_view["activeFrameId"]
                        ),
                        None,
                    )
                    if active is not None:
                        entry_frame_id = str(active["frameId"])
            frame_summaries: list[JsonValue] = []
            for frame in sorted(project_frames, key=lambda item: str(item["frameId"])):
                session = (
                    None
                    if frame["currentSessionKey"] is None
                    else sessions.get(str(frame["currentSessionKey"]))
                )
                frame_summaries.append(
                    {
                        "frameId": str(frame["frameId"]),
                        "title": str(frame["title"]),
                        "role": str(frame["role"]),
                        "parentFrameId": frame["parentFrameId"],
                        "lifecycleState": str(frame["lifecycleState"]),
                        "activity": Activity.UNKNOWN.value
                        if session is None
                        else str(session["activity"]),
                        "currentSession": None
                        if session is None
                        else {
                            "provider": str(session["provider"]),
                            "runtimePresence": str(session["runtimePresence"]),
                            "resumability": str(session["resumability"]),
                            "activity": str(session["activity"]),
                            "updatedAt": int(session["updatedAt"]),
                        },
                        "sessionCount": frame_session_counts.get(
                            str(frame["frameId"]), 0
                        ),
                    }
                )
            projects.append(
                {
                    "hostId": host_id,
                    "projectId": project_id,
                    "name": str(project["name"]),
                    "viewId": view_id,
                    "entryFrameId": entry_frame_id,
                    "frames": frame_summaries,
                }
            )
        for recovery in data["recoveries"]:  # type: ignore[union-attr]
            if recovery["state"] == RecoveryState.OPEN.value:
                recoveries.append(dict(recovery))
        for warning in data["warnings"]:  # type: ignore[union-attr]
            copied = dict(warning)
            copied["hostId"] = host_id
            warnings.append(copied)
        host_truncation = data["truncation"]
        assert isinstance(host_truncation, dict)
        for name, counts in host_truncation.items():
            truncation[f"{host_id}:{name}"] = counts
    hosts.sort(key=lambda item: str(item["hostId"]))
    views.sort(key=lambda item: (str(item["hostId"]), str(item["viewId"])))
    projects.sort(key=lambda item: (str(item["hostId"]), str(item["projectId"])))
    recoveries.sort(key=lambda item: (str(item["hostId"]), str(item["recoveryId"])))
    warnings.sort(
        key=lambda item: (
            str(item["hostId"]),
            str(item["code"]),
            str(item["subjectId"]),
        )
    )
    if len(hosts) > collection_limit:
        local_host = next(item for item in hosts if item["hostId"] == local_key)
        remote_hosts = [item for item in hosts if item["hostId"] != local_key]
        hosts = [local_host, *remote_hosts[: max(0, collection_limit - 1)]]
        hosts.sort(key=lambda item: str(item["hostId"]))
        truncation["hosts"] = {
            "retainedCount": len(by_host),
            "emittedCount": len(hosts),
        }
    emitted_host_ids = {str(item["hostId"]) for item in hosts}
    views = [item for item in views if str(item["hostId"]) in emitted_host_ids]
    projects = [item for item in projects if str(item["hostId"]) in emitted_host_ids]
    recoveries = [
        item for item in recoveries if str(item["hostId"]) in emitted_host_ids
    ]
    warnings = [
        item
        for item in warnings
        if item["hostId"] is None or str(item["hostId"]) in emitted_host_ids
    ]
    for project in projects:
        frames = list(project["frames"])  # type: ignore[arg-type]
        if len(frames) <= collection_limit:
            continue
        by_frame = {str(frame["frameId"]): frame for frame in frames}
        chain: list[dict[str, JsonValue]] = []
        cursor = project["entryFrameId"]
        seen: set[str] = set()
        while cursor is not None and str(cursor) not in seen:
            seen.add(str(cursor))
            frame = by_frame.get(str(cursor))
            if frame is None:
                break
            chain.append(frame)
            cursor = frame["parentFrameId"]
        chain.reverse()
        selected = chain[:collection_limit]
        selected_ids = {str(frame["frameId"]) for frame in selected}
        for frame in frames:
            if len(selected) >= collection_limit:
                break
            parent = frame["parentFrameId"]
            if str(frame["frameId"]) not in selected_ids and (
                parent is None or str(parent) in selected_ids
            ):
                selected.append(frame)
                selected_ids.add(str(frame["frameId"]))
        selected.sort(key=lambda item: str(item["frameId"]))
        project["frames"] = selected
        if project["entryFrameId"] not in selected_ids:
            project["entryFrameId"] = (
                None
                if not chain
                else str(chain[min(len(chain), collection_limit) - 1]["frameId"])
            )
        truncation[f"frames:{project['projectId']}"] = {
            "retainedCount": len(frames),
            "emittedCount": len(selected),
        }
    collections = {
        "views": views,
        "projects": projects,
        "recoveries": recoveries,
        "warnings": warnings,
    }
    projected: dict[str, JsonValue] = {
        "hosts": hosts,
        **{
            name: _bounded_collection(name, values, collection_limit, truncation)
            for name, values in collections.items()
        },
    }
    emitted_view_ids = {
        str(item["viewId"])
        for item in projected["views"]  # type: ignore[union-attr]
    }
    for project in projected["projects"]:  # type: ignore[union-attr]
        if (
            project["viewId"] is not None
            and str(project["viewId"]) not in emitted_view_ids
        ):
            project["viewId"] = None
    return NavigatorState(
        {
            "schemaVersion": SCHEMA_VERSION,
            "protocolVersion": PROTOCOL_VERSION,
            "navigatorVersion": NAVIGATOR_VERSION,
            "generationId": str(by_host[local_key].data["generationId"]),
            "generatedAt": generated_at,
            "localHostId": local_key,
            **projected,
            "truncation": truncation,
        }
    )


def build_navigator_from_registry(
    registry: Registry,
    *,
    generated_at: int,
    collection_limit: int = DEFAULT_COLLECTION_LIMIT,
    staleness_interval_seconds: int = 120,
    view_state_overrides: Mapping[str, ViewState | str] | None = None,
    additional_warnings: Sequence[Mapping[str, object]] = (),
) -> NavigatorState:
    local = build_host_state(
        registry,
        generated_at=generated_at,
        collection_limit=collection_limit,
        view_state_overrides=view_state_overrides,
        additional_warnings=additional_warnings,
    )
    remotes: list[HostState] = []
    reachability: dict[str, str] = {}
    staleness: dict[str, bool] = {}
    for cached in registry.cached_host_states():
        state = HostState.from_json(cached.state_json)
        if state.host_id != cached.host_id:
            raise ProtocolError("cached HostState host identity differs from cache key")
        remotes.append(state)
        reachability[str(cached.host_id)] = cached.reachability.value
        staleness[str(cached.host_id)] = (
            generated_at - cached.received_at > staleness_interval_seconds * 1_000
        )
    return build_navigator_state(
        [local, *remotes],
        local_host_id=local.host_id,
        generated_at=generated_at,
        reachability=reachability,
        staleness=staleness,
        collection_limit=collection_limit,
    )


__all__ = [
    "DEFAULT_COLLECTION_LIMIT",
    "DIRECTIVE_VERSION",
    "HOST_STATE_VERSION",
    "MAX_JSON_ARRAY_ITEMS",
    "MAX_JSON_BYTES",
    "MAX_JSON_DEPTH",
    "MAX_JSON_OBJECT_KEYS",
    "MAX_JSON_STRING_BYTES",
    "NAVIGATOR_VERSION",
    "PROTOCOL_VERSION",
    "SCHEMA_VERSION",
    "DirectiveKind",
    "HostState",
    "IncompatibleVersion",
    "NavigatorState",
    "PresentationDirective",
    "ProtocolError",
    "build_host_state",
    "build_navigator_from_registry",
    "build_navigator_state",
]
