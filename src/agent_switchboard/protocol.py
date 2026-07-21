"""Versioned, forward-field-tolerant machine protocol envelopes."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Self
from uuid import UUID

from .domain import (
    Activity,
    ActivityReason,
    Attachment,
    BindingConfidence,
    CheckoutId,
    CheckoutKind,
    HandoffId,
    HandoffSource,
    HostId,
    LaunchId,
    PresentationContext,
    ProjectId,
    ProviderId,
    RepositoryId,
    RepositoryKind,
    Resumability,
    RuntimePresence,
    SessionKey,
    StateConfidence,
    SurfaceId,
    SurfaceRole,
    TaskId,
    TaskStatus,
    Transport,
    ValidationError,
    handoff_content_hash,
    normalize_handoff_text,
)

SCHEMA_VERSION = 2
PROTOCOL_VERSION = 2
MAX_JSON_DEPTH = 32
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_JSON_STRING_LENGTH = 64 * 1024
MAX_JSON_ARRAY_ITEMS = 100_000
MAX_JSON_OBJECT_KEYS = 256
MAX_SNAPSHOT_RECORDS = 100_000
MAX_SESSION_DETAIL_HANDOFFS = 100
MAX_AGENT_CONTEXT_FILES = 32
MAX_AGENT_CONTEXT_FILE_BYTES = 64 * 1024
MAX_AGENT_CONTEXT_TOTAL_BYTES = 256 * 1024
MAX_AGENT_CONTEXT_SESSIONS = 20
MAX_AGENT_CONTEXT_ISSUES = 32
MAX_AGENT_PROJECT_SESSIONS = 50
MAX_AGENT_SEARCH_RESULTS = 20
MAX_AGENT_SEARCH_QUERY = 256
MAX_AGENT_MEMORY_TEXT_BYTES = 64 * 1024
FLEET_VERSION = 1
MAX_FLEET_REMOTES = 32
CONTINUATION_VERSION = 1

_SENSITIVE_KEY_PARTS = (
    "accesskey",
    "apikey",
    "argv",
    "authorization",
    "cookie",
    "credential",
    "environment",
    "history",
    "input",
    "modelresponse",
    "password",
    "passphrase",
    "privatekey",
    "refreshtoken",
    "accesstoken",
    "authtoken",
    "secret",
    "toolresult",
)
_SENSITIVE_KEYS = {
    "argv",
    "body",
    "content",
    "conversation",
    "conversationhistory",
    "cookie",
    "environment",
    "hookpayload",
    "messages",
    "modeloutput",
    "output",
    "payload",
    "prompt",
    "prompts",
    "prompttext",
    "providerargv",
    "providerpayload",
    "rawpayload",
    "rawprompt",
    "requestpayload",
    "responsepayload",
    "secret",
    "secrets",
    "setcookie",
    "stderr",
    "stdin",
    "stdout",
    "systemprompt",
    "tooloutput",
    "transcript",
    "transcriptbody",
    "transcripts",
    "userprompt",
}
_KEY_NORMALIZER = re.compile(r"[^a-z0-9]")
_SAFE_DETAIL_STRING_FIELDS = frozenset({"capability", "fallback"})
_SAFE_DETAIL_UUID_FIELDS = frozenset({"projectId", "repositoryId"})
_SAFE_DETAIL_INTEGER_FIELDS = frozenset({"emittedCount", "retainedCount"})
_SAFE_DETAIL_NUMBER_FIELDS = frozenset({"latency"})
_SAFE_DETAIL_HASH_FIELDS = frozenset({"payloadHash"})
_SAFE_DETAIL_FIELDS = (
    _SAFE_DETAIL_STRING_FIELDS
    | _SAFE_DETAIL_UUID_FIELDS
    | _SAFE_DETAIL_INTEGER_FIELDS
    | _SAFE_DETAIL_NUMBER_FIELDS
    | _SAFE_DETAIL_HASH_FIELDS
)

JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ProtocolError(ValidationError):
    """A machine envelope is malformed or incompatible."""

    code = "invalid_protocol_message"


class IncompatibleSchemaError(ProtocolError):
    code = "incompatible_schema_version"


class IncompatibleProtocolError(ProtocolError):
    code = "incompatible_protocol_version"


class ErrorScope(StrEnum):
    HOST = "host"
    PROJECT = "project"
    PROVIDER = "provider"
    SESSION = "session"
    LAUNCH = "launch"
    SURFACE = "surface"
    TASK = "task"


class PresentationPlanKind(StrEnum):
    FOCUS = "focus"
    SWITCH = "switch"
    ATTACH = "attach"
    BLOCKED = "blocked"


class SessionActionStatus(StrEnum):
    STOPPED = "stopped"
    ALREADY_STOPPED = "already_stopped"
    BLOCKED = "blocked"


class TaskCloseStatus(StrEnum):
    CLOSED = "closed"
    ALREADY_CLOSED = "already_closed"
    BLOCKED = "blocked"


class RuntimeDisposition(StrEnum):
    NO_SESSION = "no_session"
    ALREADY_STOPPED = "already_stopped"
    STOPPED = "stopped"
    RETAINED = "retained"
    UNKNOWN = "unknown"


class FleetSource(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


class FleetReachability(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


def _decode(raw: str | bytes | bytearray) -> Mapping[str, Any]:
    try:
        size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    except (AttributeError, UnicodeEncodeError) as exc:
        raise ProtocolError("protocol message must be UTF-8 JSON") from exc
    if size > MAX_JSON_BYTES:
        raise ProtocolError(f"protocol message exceeds the {MAX_JSON_BYTES}-byte limit")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    return _object(value, "envelope")


def _object(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ProtocolError(f"{path} must be an object")
    return value


def _array(value: object, path: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ProtocolError(f"{path} must be an array")
    return value


def _required(table: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in table:
        raise ProtocolError(f"{path}.{key} is required")
    return table[key]


def _string(
    value: object,
    path: str,
    *,
    optional: bool = False,
    maximum: int = 4096,
) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProtocolError(f"{path} must be a non-empty bounded string")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ProtocolError(f"{path} contains terminal control characters")
    return value


def _multiline_string(value: object, path: str, *, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ProtocolError(f"{path} must be a bounded string")
    if len(value.encode("utf-8")) > maximum:
        raise ProtocolError(f"{path} exceeds its UTF-8 byte limit")
    if any(
        unicodedata.category(character) == "Cc" and character not in "\n\t"
        for character in value
    ):
        raise ProtocolError(f"{path} contains terminal control characters")
    return value


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError(f"{path} must be a non-negative integer")
    return value


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(f"{path} must be boolean")
    return value


def _versions(
    table: Mapping[str, Any], *, allow_explicit_multiline: bool = False
) -> None:
    safe_table = _json_value(
        table,
        "envelope",
        allow_explicit_multiline=allow_explicit_multiline,
    )
    encoded = json.dumps(
        safe_table,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise ProtocolError(f"protocol message exceeds the {MAX_JSON_BYTES}-byte limit")
    schema = _integer(
        _required(table, "schemaVersion", "envelope"), "envelope.schemaVersion"
    )
    protocol = _integer(
        _required(table, "protocolVersion", "envelope"),
        "envelope.protocolVersion",
    )
    if schema != SCHEMA_VERSION:
        raise IncompatibleSchemaError(
            f"schema version {schema} is not supported; expected {SCHEMA_VERSION}"
        )
    if protocol != PROTOCOL_VERSION:
        raise IncompatibleProtocolError(
            f"protocol version {protocol} is not supported; expected {PROTOCOL_VERSION}"
        )


def _normalized_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _KEY_NORMALIZER.sub("", normalized)


def _reject_sensitive_key(value: str, path: str) -> None:
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ProtocolError(f"{path} contains a terminal control in an object key")
    normalized = _normalized_key(value)
    if not normalized or len(value) > 256:
        raise ProtocolError(f"{path} contains an invalid object key")
    if normalized in {"results", "resultstruncated"}:
        # AgentSearchEnvelope validates these curated, bounded records explicitly.
        return
    if normalized in _SENSITIVE_KEYS or any(
        part in normalized for part in _SENSITIVE_KEY_PARTS
    ):
        raise ProtocolError(f"{path} contains forbidden sensitive field {value!r}")
    if "prompt" in normalized or "transcript" in normalized:
        raise ProtocolError(f"{path} contains forbidden content field {value!r}")
    if any(
        part in normalized
        for part in ("conversation", "messages", "output", "response", "result")
    ):
        raise ProtocolError(f"{path} contains forbidden content field {value!r}")
    if normalized.startswith("raw"):
        raise ProtocolError(f"{path} contains forbidden raw field {value!r}")
    if "payload" in normalized and not normalized.endswith("payloadhash"):
        raise ProtocolError(f"{path} contains forbidden payload field {value!r}")
    if "token" in normalized and normalized != "desktoptoken":
        raise ProtocolError(f"{path} contains forbidden token field {value!r}")


def _json_value(
    value: object,
    path: str,
    *,
    depth: int = 0,
    allow_explicit_multiline: bool = False,
) -> JsonValue:
    if depth > MAX_JSON_DEPTH:
        raise ProtocolError(f"{path} exceeds maximum JSON nesting depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > MAX_JSON_STRING_LENGTH:
            raise ProtocolError(f"{path} contains an oversized string")
        if any(
            unicodedata.category(character) == "Cc"
            and not (allow_explicit_multiline and character in "\n\t")
            for character in value
        ):
            raise ProtocolError(f"{path} contains terminal control characters")
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ProtocolError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, list):
        if len(value) > MAX_JSON_ARRAY_ITEMS:
            raise ProtocolError(f"{path} contains too many array items")
        return [
            _json_value(
                item,
                f"{path}[{index}]",
                depth=depth + 1,
                allow_explicit_multiline=allow_explicit_multiline,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        if len(value) > MAX_JSON_OBJECT_KEYS:
            raise ProtocolError(f"{path} contains too many object keys")
        for key in value:
            _reject_sensitive_key(key, path)
        return {
            key: _json_value(
                item,
                f"{path}.{key}",
                depth=depth + 1,
                allow_explicit_multiline=allow_explicit_multiline,
            )
            for key, item in value.items()
        }
    raise ProtocolError(f"{path} contains a non-JSON value")


def _record(value: object, path: str) -> dict[str, JsonValue]:
    table = _object(value, path)
    safe_table = _json_value(table, path)
    assert isinstance(safe_table, dict)
    return safe_table


def _uuid[
    T: HostId
    | ProjectId
    | RepositoryId
    | CheckoutId
    | TaskId
    | LaunchId
    | HandoffId
    | SurfaceId
](value: object, path: str, value_type: type[T]) -> T:
    try:
        return value_type(_string(value, path))
    except ValidationError as exc:
        raise ProtocolError(f"{path}: {exc}") from exc


def _provider(value: object, path: str, *, optional: bool = False) -> ProviderId | None:
    text = _string(value, path, optional=optional)
    if text is None:
        return None
    try:
        return ProviderId(text)
    except ValueError as exc:
        raise ProtocolError(f"{path} has unsupported provider {text!r}") from exc


def _envelope(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        **payload,
    }


def _dump(
    value: Mapping[str, JsonValue], *, allow_explicit_multiline: bool = False
) -> str:
    safe_value = _json_value(
        value,
        "envelope",
        allow_explicit_multiline=allow_explicit_multiline,
    )
    assert isinstance(safe_value, dict)
    return json.dumps(
        safe_value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _enum[T: StrEnum](value: object, path: str, enum_type: type[T]) -> T:
    text = _string(value, path, maximum=128)
    assert text is not None
    try:
        return enum_type(text)
    except ValueError as exc:
        raise ProtocolError(f"{path} has unsupported value {text!r}") from exc


def _positive_integer(value: object, path: str) -> int:
    result = _integer(value, path)
    if result == 0:
        raise ProtocolError(f"{path} must be a positive integer")
    return result


def _hash(value: object, path: str) -> str:
    text = _string(value, path, maximum=64)
    assert text is not None
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ProtocolError(f"{path} must be a lowercase SHA-256 digest")
    return text


def _details_record(value: object, path: str) -> dict[str, JsonValue]:
    """Validate the small, retained diagnostic-details contract.

    Unlike additive envelope fields, details survive canonicalization. Keep
    their field names and value shapes explicit so generic dictionaries cannot
    become an accidental content or credential channel.
    """

    table = _record(value, path)
    unknown = set(table) - _SAFE_DETAIL_FIELDS
    if unknown:
        raise ProtocolError(
            f"{path} contains unsupported retained detail fields: {sorted(unknown)}"
        )

    result: dict[str, JsonValue] = {}
    for key, value in table.items():
        field_path = f"{path}.{key}"
        if key in _SAFE_DETAIL_STRING_FIELDS:
            result[key] = _string(value, field_path, maximum=512)
        elif key in _SAFE_DETAIL_UUID_FIELDS:
            value_type = ProjectId if key == "projectId" else RepositoryId
            result[key] = str(_uuid(value, field_path, value_type))
        elif key in _SAFE_DETAIL_INTEGER_FIELDS:
            result[key] = _integer(value, field_path)
        elif key in _SAFE_DETAIL_NUMBER_FIELDS:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
            ):
                raise ProtocolError(f"{field_path} must be a non-negative number")
            result[key] = value
        else:
            result[key] = _hash(value, field_path)
    if (
        "emittedCount" in result
        and "retainedCount" in result
        and result["emittedCount"] > result["retainedCount"]
    ):
        raise ProtocolError(f"{path}.emittedCount must not exceed retainedCount")
    return result


def _uuid_text[
    T: HostId
    | ProjectId
    | RepositoryId
    | CheckoutId
    | TaskId
    | LaunchId
    | HandoffId
    | SurfaceId
](value: object, path: str, value_type: type[T]) -> str:
    return str(_uuid(value, path, value_type))


def _session_key(value: object, path: str) -> SessionKey:
    text = _string(value, path, maximum=512)
    assert text is not None
    try:
        return SessionKey.parse(text)
    except ValidationError as exc:
        raise ProtocolError(f"{path}: {exc}") from exc


def _string_array(
    value: object,
    path: str,
    *,
    maximum_items: int = 10_000,
    maximum_string: int = 4096,
) -> list[JsonValue]:
    raw_items = _array(value, path)
    if len(raw_items) > maximum_items:
        raise ProtocolError(f"{path} contains too many items")
    items: list[JsonValue] = []
    for index, item in enumerate(raw_items):
        text = _string(item, f"{path}[{index}]", maximum=maximum_string)
        assert text is not None
        if text not in items:
            items.append(text)
    return items


def _optional_string(
    result: dict[str, JsonValue],
    table: Mapping[str, Any],
    key: str,
    path: str,
    *,
    maximum: int = 4096,
) -> None:
    if key in table:
        result[key] = _string(
            table[key], f"{path}.{key}", optional=True, maximum=maximum
        )


def _optional_integer(
    result: dict[str, JsonValue],
    table: Mapping[str, Any],
    key: str,
    path: str,
) -> None:
    if key in table:
        result[key] = (
            None if table[key] is None else _integer(table[key], f"{path}.{key}")
        )


def _optional_boolean(
    result: dict[str, JsonValue],
    table: Mapping[str, Any],
    key: str,
    path: str,
) -> None:
    if key in table:
        result[key] = _boolean(table[key], f"{path}.{key}")


def _optional_uuid[
    T: HostId
    | ProjectId
    | RepositoryId
    | CheckoutId
    | TaskId
    | LaunchId
    | HandoffId
    | SurfaceId
](
    result: dict[str, JsonValue],
    table: Mapping[str, Any],
    key: str,
    path: str,
    value_type: type[T],
) -> None:
    if key in table:
        result[key] = (
            None
            if table[key] is None
            else _uuid_text(table[key], f"{path}.{key}", value_type)
        )


def _project_record(value: object, path: str) -> dict[str, JsonValue]:
    table = _object(value, path)
    _json_value(table, path)
    result: dict[str, JsonValue] = {
        "projectId": _uuid_text(
            _required(table, "projectId", path), f"{path}.projectId", ProjectId
        ),
        "name": _string(_required(table, "name", path), f"{path}.name", maximum=256),
    }
    if "aliases" in table:
        result["aliases"] = _string_array(
            table["aliases"], f"{path}.aliases", maximum_string=128
        )
    if "defaultProvider" in table:
        result["defaultProvider"] = (
            None
            if table["defaultProvider"] is None
            else _provider(table["defaultProvider"], f"{path}.defaultProvider")
        )
    if "defaultTransport" in table:
        result["defaultTransport"] = _enum(
            table["defaultTransport"], f"{path}.defaultTransport", Transport
        )
    _optional_boolean(result, table, "declared", path)
    _optional_integer(result, table, "createdAt", path)
    _optional_integer(result, table, "updatedAt", path)
    return result


def _project_repository_record(value: object, path: str) -> dict[str, JsonValue]:
    table = _object(value, path)
    _json_value(table, path)
    return {
        "projectId": _uuid_text(
            _required(table, "projectId", path), f"{path}.projectId", ProjectId
        ),
        "repositoryId": _uuid_text(
            _required(table, "repositoryId", path),
            f"{path}.repositoryId",
            RepositoryId,
        ),
        "isPrimary": _boolean(_required(table, "isPrimary", path), f"{path}.isPrimary"),
    }


def _repository_record(value: object, path: str) -> dict[str, JsonValue]:
    table = _object(value, path)
    _json_value(table, path)
    result: dict[str, JsonValue] = {
        "repositoryId": _uuid_text(
            _required(table, "repositoryId", path),
            f"{path}.repositoryId",
            RepositoryId,
        ),
        "name": _string(_required(table, "name", path), f"{path}.name", maximum=256),
        "kind": _enum(_required(table, "kind", path), f"{path}.kind", RepositoryKind),
        "contextSources": _string_array(
            _required(table, "contextSources", path),
            f"{path}.contextSources",
            maximum_string=1024,
        ),
    }
    _optional_boolean(result, table, "declared", path)
    _optional_integer(result, table, "createdAt", path)
    _optional_integer(result, table, "updatedAt", path)
    return result


def _checkout_record(
    value: object, path: str, *, expected_host_id: HostId
) -> dict[str, JsonValue]:
    table = _object(value, path)
    _json_value(table, path)
    host_id = _uuid(_required(table, "hostId", path), f"{path}.hostId", HostId)
    if host_id != expected_host_id:
        raise ProtocolError(f"{path}.hostId does not match envelope.host.hostId")
    result: dict[str, JsonValue] = {
        "checkoutId": _uuid_text(
            _required(table, "checkoutId", path), f"{path}.checkoutId", CheckoutId
        ),
        "repositoryId": _uuid_text(
            _required(table, "repositoryId", path),
            f"{path}.repositoryId",
            RepositoryId,
        ),
        "hostId": str(host_id),
        "path": _string(_required(table, "path", path), f"{path}.path", maximum=4096),
        "kind": _enum(_required(table, "kind", path), f"{path}.kind", CheckoutKind),
    }
    _optional_string(result, table, "displayName", path, maximum=256)
    _optional_string(result, table, "branch", path, maximum=1024)
    _optional_string(result, table, "headOid", path, maximum=1024)
    if "providerOverride" in table:
        result["providerOverride"] = (
            None
            if table["providerOverride"] is None
            else _provider(table["providerOverride"], f"{path}.providerOverride")
        )
    if "transportOverride" in table:
        result["transportOverride"] = (
            None
            if table["transportOverride"] is None
            else _enum(
                table["transportOverride"],
                f"{path}.transportOverride",
                Transport,
            )
        )
    _optional_boolean(result, table, "isDefault", path)
    _optional_boolean(result, table, "declared", path)
    _optional_boolean(result, table, "present", path)
    _optional_integer(result, table, "lastObservedAt", path)
    _optional_integer(result, table, "createdAt", path)
    _optional_integer(result, table, "updatedAt", path)
    return result


def _task_record(
    value: object, path: str, *, expected_host_id: HostId
) -> dict[str, JsonValue]:
    table = _object(value, path)
    _json_value(table, path, allow_explicit_multiline=True)
    host_id = _uuid(_required(table, "hostId", path), f"{path}.hostId", HostId)
    if host_id != expected_host_id:
        raise ProtocolError(f"{path}.hostId does not match envelope.host.hostId")
    result: dict[str, JsonValue] = {
        "taskId": _uuid_text(
            _required(table, "taskId", path), f"{path}.taskId", TaskId
        ),
        "hostId": str(host_id),
        "projectId": _uuid_text(
            _required(table, "projectId", path), f"{path}.projectId", ProjectId
        ),
        "title": _string(_required(table, "title", path), f"{path}.title", maximum=256),
        "status": _enum(_required(table, "status", path), f"{path}.status", TaskStatus),
        "pinned": _boolean(_required(table, "pinned", path), f"{path}.pinned"),
        "createdAt": _integer(_required(table, "createdAt", path), f"{path}.createdAt"),
        "updatedAt": _integer(_required(table, "updatedAt", path), f"{path}.updatedAt"),
    }
    _optional_uuid(result, table, "checkoutId", path, CheckoutId)
    if "purpose" in table:
        result["purpose"] = (
            None
            if table["purpose"] is None
            else _multiline_string(table["purpose"], f"{path}.purpose", maximum=4096)
        )
    if "preferredProvider" in table:
        result["preferredProvider"] = (
            None
            if table["preferredProvider"] is None
            else _provider(table["preferredProvider"], f"{path}.preferredProvider")
        )
    if "currentSessionKey" in table:
        result["currentSessionKey"] = (
            None
            if table["currentSessionKey"] is None
            else str(
                _session_key(table["currentSessionKey"], f"{path}.currentSessionKey")
            )
        )
    _optional_integer(result, table, "closedAt", path)
    if result["status"] is TaskStatus.CLOSED and result.get("closedAt") is None:
        raise ProtocolError(f"{path}.closedAt is required for a closed task")
    if result["status"] is TaskStatus.OPEN and result.get("closedAt") is not None:
        raise ProtocolError(f"{path}.closedAt is invalid for an open task")
    return result


def _runtime_locator(value: object, path: str) -> dict[str, JsonValue]:
    table = _object(value, path)
    _json_value(table, path)
    result: dict[str, JsonValue] = {}
    if "pid" in table:
        result["pid"] = (
            None
            if table["pid"] is None
            else _positive_integer(table["pid"], f"{path}.pid")
        )
    for key in ("providerRuntimeId", "tmuxSession", "tmuxWindow", "tmuxPane"):
        _optional_string(result, table, key, path, maximum=1024)
    _optional_integer(result, table, "observedAt", path)
    return result


def _session_record(
    value: object, path: str, *, expected_host_id: HostId
) -> dict[str, JsonValue]:
    table = _object(value, path)
    _json_value(table, path)
    key = _session_key(_required(table, "sessionKey", path), f"{path}.sessionKey")
    host_id = _uuid(_required(table, "hostId", path), f"{path}.hostId", HostId)
    provider = _provider(_required(table, "provider", path), f"{path}.provider")
    provider_session_id = _string(
        _required(table, "providerSessionId", path),
        f"{path}.providerSessionId",
        maximum=256,
    )
    assert provider is not None and provider_session_id is not None
    try:
        parsed_provider_session_id = UUID(provider_session_id)
    except ValueError as exc:
        raise ProtocolError(f"{path}.providerSessionId must be a UUID") from exc
    if host_id != expected_host_id or key.host_id != expected_host_id:
        raise ProtocolError(f"{path} belongs to a different host")
    if (
        key.provider is not provider
        or key.provider_session_id != parsed_provider_session_id
    ):
        raise ProtocolError(f"{path} identity fields disagree with sessionKey")

    result: dict[str, JsonValue] = {
        "sessionKey": str(key),
        "hostId": str(host_id),
        "provider": provider,
        "providerSessionId": str(parsed_provider_session_id),
        "firstObservedAt": _integer(
            _required(table, "firstObservedAt", path), f"{path}.firstObservedAt"
        ),
        "lastObservedAt": _integer(
            _required(table, "lastObservedAt", path), f"{path}.lastObservedAt"
        ),
        "metadataSource": _string(
            _required(table, "metadataSource", path),
            f"{path}.metadataSource",
            maximum=64,
        ),
    }
    _optional_uuid(result, table, "projectId", path, ProjectId)
    _optional_uuid(result, table, "taskId", path, TaskId)
    _optional_uuid(result, table, "checkoutId", path, CheckoutId)
    _optional_string(result, table, "name", path, maximum=512)
    _optional_string(result, table, "purpose", path, maximum=4096)
    _optional_string(result, table, "cwd", path, maximum=4096)
    for field in (
        "createdAt",
        "providerUpdatedAt",
        "lastActivityAt",
        "stateObservedAt",
        "wrappedAt",
    ):
        _optional_integer(result, table, field, path)
    for field, enum_type in (
        ("runtimePresence", RuntimePresence),
        ("resumability", Resumability),
        ("activity", Activity),
        ("activityReason", ActivityReason),
        ("attachment", Attachment),
        ("stateConfidence", StateConfidence),
    ):
        result[field] = _enum(
            _required(table, field, path), f"{path}.{field}", enum_type
        )
    if "runtimeLocator" in table:
        result["runtimeLocator"] = (
            None
            if table["runtimeLocator"] is None
            else _runtime_locator(table["runtimeLocator"], f"{path}.runtimeLocator")
        )
    _optional_uuid(result, table, "surfaceId", path, SurfaceId)
    _optional_uuid(result, table, "latestHandoffId", path, HandoffId)
    _optional_uuid(result, table, "continuedFromHandoffId", path, HandoffId)
    _optional_boolean(result, table, "pinned", path)
    return result


def _handoff_record(
    value: object,
    path: str,
    *,
    expected_session_key: SessionKey,
) -> dict[str, JsonValue]:
    table = _object(value, path)
    if len(table) > MAX_JSON_OBJECT_KEYS:
        raise ProtocolError(f"{path} contains too many object keys")
    multiline_fields = {"summary", "nextAction"}
    for key, item in table.items():
        _reject_sensitive_key(key, path)
        if key not in multiline_fields:
            _json_value(item, f"{path}.{key}")
    session_key = _session_key(
        _required(table, "sessionKey", path), f"{path}.sessionKey"
    )
    if session_key != expected_session_key:
        raise ProtocolError(f"{path} belongs to a different session")
    raw_summary = _required(table, "summary", path)
    raw_next_action = _required(table, "nextAction", path)
    try:
        summary = normalize_handoff_text(raw_summary, "summary")
        next_action = normalize_handoff_text(raw_next_action, "nextAction")
    except ValidationError as exc:
        raise ProtocolError(f"{path}: {exc}") from exc
    if summary != raw_summary or next_action != raw_next_action:
        raise ProtocolError(f"{path} handoff text is not canonically normalized")
    try:
        source = HandoffSource(
            _string(_required(table, "source", path), f"{path}.source", maximum=32)
        )
    except ValueError as exc:
        raise ProtocolError(f"{path}.source is not supported") from exc
    content_hash = _hash(_required(table, "contentHash", path), f"{path}.contentHash")
    if content_hash != handoff_content_hash(summary, next_action):
        raise ProtocolError(f"{path}.contentHash does not match handoff content")
    result: dict[str, JsonValue] = {
        "handoffId": _uuid_text(
            _required(table, "handoffId", path),
            f"{path}.handoffId",
            HandoffId,
        ),
        "sessionKey": str(session_key),
        "sequence": _positive_integer(
            _required(table, "sequence", path), f"{path}.sequence"
        ),
        "summary": summary,
        "nextAction": next_action,
        "source": source,
        "sourceHostId": _uuid_text(
            _required(table, "sourceHostId", path),
            f"{path}.sourceHostId",
            HostId,
        ),
        "createdAt": _integer(_required(table, "createdAt", path), f"{path}.createdAt"),
        "contentHash": content_hash,
    }
    provenance_fields = {"sourceTaskId", "sourceProjectId", "importedAt"}
    present_provenance = provenance_fields.intersection(table)
    if present_provenance and present_provenance != provenance_fields:
        raise ProtocolError(f"{path} imported provenance is incomplete")
    if present_provenance:
        result["sourceTaskId"] = _uuid_text(
            _required(table, "sourceTaskId", path),
            f"{path}.sourceTaskId",
            TaskId,
        )
        result["sourceProjectId"] = _uuid_text(
            _required(table, "sourceProjectId", path),
            f"{path}.sourceProjectId",
            ProjectId,
        )
        result["importedAt"] = _integer(
            _required(table, "importedAt", path), f"{path}.importedAt"
        )
    return result


def _runtime_record(
    value: object, path: str, *, expected_host_id: HostId
) -> dict[str, JsonValue]:
    table = _object(value, path)
    _json_value(table, path)
    host_id = _uuid(_required(table, "hostId", path), f"{path}.hostId", HostId)
    provider = _provider(_required(table, "provider", path), f"{path}.provider")
    assert provider is not None
    if host_id != expected_host_id:
        raise ProtocolError(f"{path}.hostId does not match envelope.host.hostId")
    result: dict[str, JsonValue] = {
        "hostId": str(host_id),
        "provider": provider,
        "runtimePresence": _enum(
            _required(table, "runtimePresence", path),
            f"{path}.runtimePresence",
            RuntimePresence,
        ),
        "resumability": _enum(
            _required(table, "resumability", path),
            f"{path}.resumability",
            Resumability,
        ),
        "activity": _enum(
            _required(table, "activity", path), f"{path}.activity", Activity
        ),
        "activityReason": _enum(
            _required(table, "activityReason", path),
            f"{path}.activityReason",
            ActivityReason,
        ),
        "attachment": _enum(
            _required(table, "attachment", path),
            f"{path}.attachment",
            Attachment,
        ),
        "observedAt": _integer(
            _required(table, "observedAt", path), f"{path}.observedAt"
        ),
    }
    if "sessionKey" in table:
        if table["sessionKey"] is None:
            result["sessionKey"] = None
        else:
            key = _session_key(table["sessionKey"], f"{path}.sessionKey")
            if key.host_id != host_id or key.provider is not provider:
                raise ProtocolError(f"{path}.sessionKey does not match host/provider")
            result["sessionKey"] = str(key)
    _optional_uuid(result, table, "launchId", path, LaunchId)
    for key in ("observationId", "observationKey", "source", "providerRuntimeId"):
        _optional_string(result, table, key, path, maximum=256)
    if "sourcePriority" in table:
        result["sourcePriority"] = _integer(
            table["sourcePriority"], f"{path}.sourcePriority"
        )
    if "pid" in table:
        result["pid"] = (
            None
            if table["pid"] is None
            else _positive_integer(table["pid"], f"{path}.pid")
        )
    for key in ("tmuxSession", "tmuxWindow", "tmuxPane"):
        _optional_string(result, table, key, path, maximum=256)
    _optional_integer(result, table, "receivedAt", path)
    if "payloadHash" in table:
        result["payloadHash"] = (
            None
            if table["payloadHash"] is None
            else _hash(table["payloadHash"], f"{path}.payloadHash")
        )
    return result


def _surface_record(
    value: object, path: str, *, expected_host_id: HostId
) -> dict[str, JsonValue]:
    table = _object(value, path)
    _json_value(table, path)
    host_id = _uuid(_required(table, "hostId", path), f"{path}.hostId", HostId)
    provider = _provider(_required(table, "provider", path), f"{path}.provider")
    assert provider is not None
    if host_id != expected_host_id:
        raise ProtocolError(f"{path}.hostId does not match envelope.host.hostId")
    role = _enum(_required(table, "role", path), f"{path}.role", SurfaceRole)
    binding_confidence = _enum(
        _required(table, "bindingConfidence", path),
        f"{path}.bindingConfidence",
        BindingConfidence,
    )
    created_at = _integer(_required(table, "createdAt", path), f"{path}.createdAt")
    last_observed_at = _integer(
        _required(table, "lastObservedAt", path), f"{path}.lastObservedAt"
    )
    if last_observed_at < created_at:
        raise ProtocolError(f"{path} observation timestamps are reversed")
    result: dict[str, JsonValue] = {
        "surfaceId": _uuid_text(
            _required(table, "surfaceId", path), f"{path}.surfaceId", SurfaceId
        ),
        "hostId": str(host_id),
        "provider": provider,
        "transport": _enum(
            _required(table, "transport", path), f"{path}.transport", Transport
        ),
        "transportLocator": _string(
            _required(table, "transportLocator", path),
            f"{path}.transportLocator",
            maximum=1024,
        ),
        "role": role,
        "bindingConfidence": binding_confidence,
        "createdAt": created_at,
        "lastObservedAt": last_observed_at,
        "clientAttached": _boolean(
            _required(table, "clientAttached", path), f"{path}.clientAttached"
        ),
    }
    if (
        role is SurfaceRole.PROVIDER_MANAGER
        and binding_confidence is not BindingConfidence.UNKNOWN
    ):
        raise ProtocolError(
            f"{path}.bindingConfidence must be unknown for provider_manager"
        )
    if "currentSessionKey" in table:
        if table["currentSessionKey"] is None:
            result["currentSessionKey"] = None
        else:
            key = _session_key(table["currentSessionKey"], f"{path}.currentSessionKey")
            if key.host_id != host_id or key.provider is not provider:
                raise ProtocolError(
                    f"{path}.currentSessionKey does not match host/provider"
                )
            if role is SurfaceRole.PROVIDER_MANAGER:
                raise ProtocolError(
                    f"{path}.currentSessionKey is invalid for provider_manager"
                )
            result["currentSessionKey"] = str(key)
    if (
        binding_confidence is BindingConfidence.CONFIRMED
        and result.get("currentSessionKey") is None
    ):
        raise ProtocolError(
            f"{path}.bindingConfidence confirmed requires currentSessionKey"
        )
    _optional_uuid(result, table, "launchId", path, LaunchId)
    _optional_string(result, table, "workspaceId", path, maximum=256)
    _optional_integer(result, table, "retiredAt", path)
    retired_at = result.get("retiredAt")
    if retired_at is not None and not created_at <= int(retired_at) <= last_observed_at:
        raise ProtocolError(f"{path}.retiredAt is outside the observation lifetime")
    if retired_at is not None and (
        result.get("currentSessionKey") is not None
        or binding_confidence is not BindingConfidence.UNKNOWN
        or result["clientAttached"] is not False
    ):
        raise ProtocolError(f"{path} retired surface is still bound or attached")
    return result


@dataclass(frozen=True, slots=True)
class HostRecord:
    host_id: HostId
    display_name: str

    @classmethod
    def from_dict(cls, value: object, path: str = "host") -> Self:
        table = _object(value, path)
        display_name = _string(
            _required(table, "displayName", path), f"{path}.displayName", maximum=256
        )
        assert display_name is not None
        return cls(
            host_id=_uuid(_required(table, "hostId", path), f"{path}.hostId", HostId),
            display_name=display_name,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {"hostId": str(self.host_id), "displayName": self.display_name}


@dataclass(frozen=True, slots=True)
class CapabilityDegradation:
    code: str
    message: str
    retryable: bool
    feature: str | None = None
    details: dict[str, JsonValue] | None = None

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        table = _object(value, path)
        code = _string(_required(table, "code", path), f"{path}.code", maximum=128)
        message = _string(
            _required(table, "message", path), f"{path}.message", maximum=2048
        )
        assert code is not None and message is not None
        details = (
            _details_record(table["details"], f"{path}.details")
            if table.get("details") is not None
            else None
        )
        return cls(
            code=code,
            message=message,
            retryable=_boolean(
                _required(table, "retryable", path), f"{path}.retryable"
            ),
            feature=_string(
                table.get("feature"),
                f"{path}.feature",
                optional=True,
                maximum=256,
            ),
            details=details,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.feature is not None:
            result["feature"] = self.feature
        if self.details is not None:
            result["details"] = self.details
        return result


@dataclass(frozen=True, slots=True)
class Capability:
    provider: ProviderId
    available: bool
    provider_version: str | None
    tested_contract_min: str
    tested_contract_max: str
    features: tuple[str, ...]
    schema_fingerprint: str | None = None
    degraded_reasons: tuple[CapabilityDegradation, ...] = ()

    @classmethod
    def from_dict(cls, value: object, path: str = "capability") -> Self:
        table = _object(value, path)
        contract = _object(
            _required(table, "testedContractRange", path),
            f"{path}.testedContractRange",
        )
        minimum = _string(
            _required(contract, "minimum", f"{path}.testedContractRange"),
            f"{path}.testedContractRange.minimum",
            maximum=256,
        )
        maximum = _string(
            _required(contract, "maximum", f"{path}.testedContractRange"),
            f"{path}.testedContractRange.maximum",
            maximum=256,
        )
        assert minimum is not None and maximum is not None
        raw_features = _array(_required(table, "features", path), f"{path}.features")
        features: list[str] = []
        for index, raw_feature in enumerate(raw_features):
            feature = _string(raw_feature, f"{path}.features[{index}]", maximum=256)
            assert feature is not None
            if feature not in features:
                features.append(feature)
        raw_degraded = _array(
            _required(table, "degradedReasons", path),
            f"{path}.degradedReasons",
        )
        available = _boolean(_required(table, "available", path), f"{path}.available")
        degraded_reasons = tuple(
            CapabilityDegradation.from_dict(item, f"{path}.degradedReasons[{index}]")
            for index, item in enumerate(raw_degraded)
        )
        if not available and not degraded_reasons:
            raise ProtocolError(
                f"{path}.degradedReasons must explain unavailable provider"
            )
        return cls(
            provider=_provider(_required(table, "provider", path), f"{path}.provider"),
            available=available,
            provider_version=_string(
                table.get("providerVersion"),
                f"{path}.providerVersion",
                optional=True,
                maximum=256,
            ),
            tested_contract_min=minimum,
            tested_contract_max=maximum,
            features=tuple(features),
            schema_fingerprint=(
                None
                if table.get("schemaFingerprint") is None
                else _hash(table["schemaFingerprint"], f"{path}.schemaFingerprint")
            ),
            degraded_reasons=degraded_reasons,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "provider": self.provider,
            "available": self.available,
            "testedContractRange": {
                "minimum": self.tested_contract_min,
                "maximum": self.tested_contract_max,
            },
            "features": list(self.features),
            "degradedReasons": [reason.to_dict() for reason in self.degraded_reasons],
        }
        if self.provider_version is not None:
            result["providerVersion"] = self.provider_version
        if self.schema_fingerprint is not None:
            result["schemaFingerprint"] = self.schema_fingerprint
        return result


@dataclass(frozen=True, slots=True)
class CapabilityEnvelope:
    capability: Capability

    @classmethod
    def from_dict(cls, value: object) -> Self:
        table = _object(value, "envelope")
        _versions(table)
        return cls(Capability.from_dict(_required(table, "capability", "envelope")))

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> Self:
        return cls.from_dict(_decode(raw))

    def _raw_dict(self) -> dict[str, JsonValue]:
        return _envelope({"capability": self.capability.to_dict()})

    def to_dict(self) -> dict[str, JsonValue]:
        normalized = type(self).from_dict(self._raw_dict())
        return normalized._raw_dict()

    def to_json(self) -> str:
        return _dump(self.to_dict())


@dataclass(frozen=True, slots=True)
class ErrorRecord:
    code: str
    message: str
    scope: ErrorScope
    retryable: bool
    observed_at: int
    host_id: HostId | None = None
    provider: ProviderId | None = None
    session_key: SessionKey | None = None
    details: dict[str, JsonValue] | None = None

    @classmethod
    def from_dict(cls, value: object, path: str = "error") -> Self:
        table = _object(value, path)
        code = _string(_required(table, "code", path), f"{path}.code", maximum=128)
        message = _string(
            _required(table, "message", path), f"{path}.message", maximum=4096
        )
        assert code is not None and message is not None
        try:
            scope = ErrorScope(
                _string(_required(table, "scope", path), f"{path}.scope")
            )
        except ValueError as exc:
            raise ProtocolError(f"{path}.scope is not supported") from exc
        session_key: SessionKey | None = None
        if table.get("sessionKey") is not None:
            try:
                session_key = SessionKey.parse(
                    _string(table["sessionKey"], f"{path}.sessionKey") or ""
                )
            except ValidationError as exc:
                raise ProtocolError(f"{path}.sessionKey: {exc}") from exc
        host_id = (
            _uuid(table["hostId"], f"{path}.hostId", HostId)
            if table.get("hostId") is not None
            else None
        )
        provider = _provider(table.get("provider"), f"{path}.provider", optional=True)
        if session_key is not None:
            if host_id is not None and session_key.host_id != host_id:
                raise ProtocolError(f"{path} session/host routing fields disagree")
            if provider is not None and session_key.provider is not provider:
                raise ProtocolError(f"{path} session/provider routing fields disagree")
        return cls(
            code=code,
            message=message,
            scope=scope,
            host_id=host_id,
            provider=provider,
            session_key=session_key,
            retryable=_boolean(
                _required(table, "retryable", path), f"{path}.retryable"
            ),
            observed_at=_integer(
                _required(table, "observedAt", path), f"{path}.observedAt"
            ),
            details=(
                _details_record(table["details"], f"{path}.details")
                if table.get("details") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "code": self.code,
            "message": self.message,
            "scope": self.scope,
            "retryable": self.retryable,
            "observedAt": self.observed_at,
        }
        if self.host_id is not None:
            result["hostId"] = str(self.host_id)
        if self.provider is not None:
            result["provider"] = self.provider
        if self.session_key is not None:
            result["sessionKey"] = str(self.session_key)
        if self.details is not None:
            result["details"] = self.details
        return result


@dataclass(frozen=True, slots=True)
class ErrorEnvelope:
    error: ErrorRecord

    @classmethod
    def from_dict(cls, value: object) -> Self:
        table = _object(value, "envelope")
        _versions(table)
        return cls(ErrorRecord.from_dict(_required(table, "error", "envelope")))

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> Self:
        return cls.from_dict(_decode(raw))

    def _raw_dict(self) -> dict[str, JsonValue]:
        return _envelope({"error": self.error.to_dict()})

    def to_dict(self) -> dict[str, JsonValue]:
        normalized = type(self).from_dict(self._raw_dict())
        return normalized._raw_dict()

    def to_json(self) -> str:
        return _dump(self.to_dict())


@dataclass(frozen=True, slots=True)
class PresentationPlan:
    kind: PresentationPlanKind
    host_id: HostId
    surface_id: SurfaceId | None = None
    workspace_id: str | None = None
    tmux_target: str | None = None
    tmux_client: str | None = None
    desktop_token: str | None = None
    lease_expires_at: int | None = None
    error: ErrorRecord | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.host_id, HostId):
            object.__setattr__(self, "host_id", HostId(self.host_id))
        if self.surface_id is not None and not isinstance(self.surface_id, SurfaceId):
            object.__setattr__(self, "surface_id", SurfaceId(self.surface_id))
        try:
            kind = (
                self.kind
                if isinstance(self.kind, PresentationPlanKind)
                else PresentationPlanKind(self.kind)
            )
        except ValueError as exc:
            raise ProtocolError(
                f"unsupported presentation plan kind: {self.kind}"
            ) from exc
        object.__setattr__(self, "kind", kind)
        for field_name, maximum in (
            ("workspace_id", 1024),
            ("tmux_target", 2048),
            ("tmux_client", 1024),
            ("desktop_token", 2048),
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _string(value, field_name, maximum=maximum),
                )
        if self.lease_expires_at is not None:
            object.__setattr__(
                self,
                "lease_expires_at",
                _integer(self.lease_expires_at, "lease_expires_at"),
            )
        if self.error is not None and not isinstance(self.error, ErrorRecord):
            raise ProtocolError("error must be an ErrorRecord")
        if kind is PresentationPlanKind.BLOCKED:
            if self.error is None:
                raise ProtocolError("blocked plan requires an error")
            if any(
                value is not None
                for value in (
                    self.surface_id,
                    self.workspace_id,
                    self.tmux_target,
                    self.tmux_client,
                    self.desktop_token,
                    self.lease_expires_at,
                )
            ):
                raise ProtocolError("blocked plan cannot contain surface locators")
            return
        if self.error is not None:
            raise ProtocolError("non-blocked plan cannot contain an error")
        if self.surface_id is None:
            raise ProtocolError("executable plan requires surface_id")
        if kind is PresentationPlanKind.FOCUS:
            if self.desktop_token is None:
                raise ProtocolError("focus plan requires desktop_token")
            if any((self.tmux_target, self.tmux_client, self.lease_expires_at)):
                raise ProtocolError("focus plan contains non-applicable fields")
        elif kind is PresentationPlanKind.SWITCH:
            if self.tmux_target is None or self.tmux_client is None:
                raise ProtocolError("switch plan requires tmux_target and tmux_client")
        elif kind is PresentationPlanKind.ATTACH:
            if self.tmux_target is None:
                raise ProtocolError("attach plan requires tmux_target")
            if self.tmux_client is not None:
                raise ProtocolError("attach plan contains non-applicable fields")

    @classmethod
    def from_dict(cls, value: object, path: str = "plan") -> Self:
        table = _object(value, path)
        try:
            kind = PresentationPlanKind(
                _string(_required(table, "kind", path), f"{path}.kind")
            )
        except ValueError as exc:
            raise ProtocolError(f"{path}.kind is not supported") from exc
        host_id = _uuid(_required(table, "hostId", path), f"{path}.hostId", HostId)
        return cls(
            kind=kind,
            host_id=host_id,
            surface_id=(
                _uuid(table["surfaceId"], f"{path}.surfaceId", SurfaceId)
                if table.get("surfaceId") is not None
                else None
            ),
            workspace_id=_string(
                table.get("workspaceId"),
                f"{path}.workspaceId",
                optional=True,
                maximum=1024,
            ),
            tmux_target=_string(
                table.get("tmuxTarget"),
                f"{path}.tmuxTarget",
                optional=True,
                maximum=2048,
            ),
            tmux_client=_string(
                table.get("tmuxClient"),
                f"{path}.tmuxClient",
                optional=True,
                maximum=1024,
            ),
            desktop_token=_string(
                table.get("desktopToken"),
                f"{path}.desktopToken",
                optional=True,
                maximum=2048,
            ),
            lease_expires_at=(
                _integer(table["leaseExpiresAt"], f"{path}.leaseExpiresAt")
                if table.get("leaseExpiresAt") is not None
                else None
            ),
            error=(
                ErrorRecord.from_dict(table["error"], f"{path}.error")
                if table.get("error") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {"kind": self.kind, "hostId": str(self.host_id)}
        for key, value in (
            ("surfaceId", self.surface_id),
            ("workspaceId", self.workspace_id),
            ("tmuxTarget", self.tmux_target),
            ("tmuxClient", self.tmux_client),
            ("desktopToken", self.desktop_token),
            ("leaseExpiresAt", self.lease_expires_at),
        ):
            if value is not None:
                result[key] = (
                    str(value) if isinstance(value, (HostId, SurfaceId)) else value
                )
        if self.error is not None:
            result["error"] = self.error.to_dict()
        return result

    def validate_for_context(self, context: PresentationContext) -> None:
        if self.kind is PresentationPlanKind.FOCUS and not context.can_focus_desktop:
            raise ProtocolError("caller cannot execute a focus plan")
        if (
            self.kind is PresentationPlanKind.SWITCH
            and context.current_tmux_client != self.tmux_client
            and not context.can_focus_desktop
        ):
            raise ProtocolError("caller cannot revalidate the switch target")
        if self.kind is PresentationPlanKind.ATTACH and not (
            context.has_current_terminal or context.can_launch_terminal
        ):
            raise ProtocolError("caller cannot execute an attach plan")


@dataclass(frozen=True, slots=True)
class PresentationPlanEnvelope:
    plan: PresentationPlan

    @classmethod
    def from_dict(cls, value: object) -> Self:
        table = _object(value, "envelope")
        _versions(table)
        return cls(PresentationPlan.from_dict(_required(table, "plan", "envelope")))

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> Self:
        return cls.from_dict(_decode(raw))

    def _raw_dict(self) -> dict[str, JsonValue]:
        return _envelope({"plan": self.plan.to_dict()})

    def to_dict(self) -> dict[str, JsonValue]:
        normalized = type(self).from_dict(self._raw_dict())
        return normalized._raw_dict()

    def to_json(self) -> str:
        return _dump(self.to_dict())


@dataclass(frozen=True, slots=True)
class SessionAction:
    status: SessionActionStatus
    host_id: HostId
    session_key: SessionKey
    error: ErrorRecord | None = None

    def __post_init__(self) -> None:
        try:
            status = (
                self.status
                if isinstance(self.status, SessionActionStatus)
                else SessionActionStatus(self.status)
            )
        except ValueError as exc:
            raise ProtocolError(
                f"unsupported session action status: {self.status}"
            ) from exc
        object.__setattr__(self, "status", status)
        if not isinstance(self.host_id, HostId):
            object.__setattr__(self, "host_id", HostId(self.host_id))
        if not isinstance(self.session_key, SessionKey):
            object.__setattr__(self, "session_key", SessionKey.parse(self.session_key))
        if self.session_key.host_id != self.host_id:
            raise ProtocolError("session action host and session identity disagree")
        if status is SessionActionStatus.BLOCKED:
            if self.error is None:
                raise ProtocolError("blocked session action requires an error")
            if (
                self.error.host_id not in {None, self.host_id}
                or self.error.provider not in {None, self.session_key.provider}
                or self.error.session_key not in {None, self.session_key}
            ):
                raise ProtocolError("blocked session action error routing disagrees")
        elif self.error is not None:
            raise ProtocolError("successful session action cannot contain an error")

    @classmethod
    def from_dict(cls, value: object, path: str = "action") -> Self:
        table = _object(value, path)
        kind = _string(_required(table, "kind", path), f"{path}.kind")
        if kind != "stop":
            raise ProtocolError(f"{path}.kind is not supported")
        try:
            status = SessionActionStatus(
                _string(_required(table, "status", path), f"{path}.status")
            )
        except ValueError as exc:
            raise ProtocolError(f"{path}.status is not supported") from exc
        host_id = _uuid(_required(table, "hostId", path), f"{path}.hostId", HostId)
        try:
            session_key = SessionKey.parse(
                _string(_required(table, "sessionKey", path), f"{path}.sessionKey")
                or ""
            )
        except ValidationError as exc:
            raise ProtocolError(f"{path}.sessionKey: {exc}") from exc
        return cls(
            status,
            host_id,
            session_key,
            error=(
                ErrorRecord.from_dict(table["error"], f"{path}.error")
                if table.get("error") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "kind": "stop",
            "status": self.status,
            "hostId": str(self.host_id),
            "sessionKey": str(self.session_key),
        }
        if self.error is not None:
            result["error"] = self.error.to_dict()
        return result


@dataclass(frozen=True, slots=True)
class SessionActionEnvelope:
    action: SessionAction

    @classmethod
    def from_dict(cls, value: object) -> Self:
        table = _object(value, "envelope")
        _versions(table)
        return cls(SessionAction.from_dict(_required(table, "action", "envelope")))

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> Self:
        return cls.from_dict(_decode(raw))

    def _raw_dict(self) -> dict[str, JsonValue]:
        return _envelope({"action": self.action.to_dict()})

    def to_dict(self) -> dict[str, JsonValue]:
        normalized = type(self).from_dict(self._raw_dict())
        return normalized._raw_dict()

    def to_json(self) -> str:
        return _dump(self.to_dict())


@dataclass(frozen=True, slots=True)
class TaskCloseAction:
    status: TaskCloseStatus
    host_id: HostId
    task_id: TaskId
    runtime_disposition: RuntimeDisposition
    current_session_key: SessionKey | None = None
    error: ErrorRecord | None = None
    warning: ErrorRecord | None = None

    def __post_init__(self) -> None:
        try:
            status = (
                self.status
                if isinstance(self.status, TaskCloseStatus)
                else TaskCloseStatus(self.status)
            )
            disposition = (
                self.runtime_disposition
                if isinstance(self.runtime_disposition, RuntimeDisposition)
                else RuntimeDisposition(self.runtime_disposition)
            )
        except ValueError as exc:
            raise ProtocolError("unsupported task close action value") from exc
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "runtime_disposition", disposition)
        if not isinstance(self.host_id, HostId):
            object.__setattr__(self, "host_id", HostId(self.host_id))
        if not isinstance(self.task_id, TaskId):
            object.__setattr__(self, "task_id", TaskId(self.task_id))
        if self.current_session_key is not None and not isinstance(
            self.current_session_key, SessionKey
        ):
            object.__setattr__(
                self,
                "current_session_key",
                SessionKey.parse(self.current_session_key),
            )
        if (
            self.current_session_key is not None
            and self.current_session_key.host_id != self.host_id
        ):
            raise ProtocolError("task close host and session identity disagree")
        if status is TaskCloseStatus.BLOCKED:
            if self.error is None:
                raise ProtocolError("blocked task close requires an error")
            if self.warning is not None:
                raise ProtocolError("blocked task close cannot contain a warning")
        elif self.error is not None:
            raise ProtocolError("successful task close cannot contain an error")
        for issue in (self.error, self.warning):
            if issue is None:
                continue
            if issue.host_id not in {None, self.host_id}:
                raise ProtocolError("task close issue host routing disagrees")
            if (
                issue.session_key is not None
                and issue.session_key != self.current_session_key
            ):
                raise ProtocolError("task close issue session routing disagrees")

    @classmethod
    def from_dict(cls, value: object, path: str = "action") -> Self:
        table = _object(value, path)
        if _string(_required(table, "kind", path), f"{path}.kind") != "close":
            raise ProtocolError(f"{path}.kind is not supported")
        try:
            status = TaskCloseStatus(
                _string(_required(table, "status", path), f"{path}.status")
            )
            disposition = RuntimeDisposition(
                _string(
                    _required(table, "runtimeDisposition", path),
                    f"{path}.runtimeDisposition",
                )
            )
        except ValueError as exc:
            raise ProtocolError(f"{path} contains an unsupported value") from exc
        session_key: SessionKey | None = None
        if table.get("currentSessionKey") is not None:
            try:
                session_key = SessionKey.parse(
                    _string(table["currentSessionKey"], f"{path}.currentSessionKey")
                    or ""
                )
            except ValidationError as exc:
                raise ProtocolError(f"{path}.currentSessionKey: {exc}") from exc
        return cls(
            status=status,
            host_id=_uuid(_required(table, "hostId", path), f"{path}.hostId", HostId),
            task_id=_uuid(_required(table, "taskId", path), f"{path}.taskId", TaskId),
            runtime_disposition=disposition,
            current_session_key=session_key,
            error=(
                ErrorRecord.from_dict(table["error"], f"{path}.error")
                if table.get("error") is not None
                else None
            ),
            warning=(
                ErrorRecord.from_dict(table["warning"], f"{path}.warning")
                if table.get("warning") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "kind": "close",
            "status": self.status,
            "hostId": str(self.host_id),
            "taskId": str(self.task_id),
            "runtimeDisposition": self.runtime_disposition,
        }
        if self.current_session_key is not None:
            result["currentSessionKey"] = str(self.current_session_key)
        if self.error is not None:
            result["error"] = self.error.to_dict()
        if self.warning is not None:
            result["warning"] = self.warning.to_dict()
        return result


@dataclass(frozen=True, slots=True)
class TaskCloseActionEnvelope:
    action: TaskCloseAction

    @classmethod
    def from_dict(cls, value: object) -> Self:
        table = _object(value, "envelope")
        _versions(table)
        return cls(TaskCloseAction.from_dict(_required(table, "action", "envelope")))

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> Self:
        return cls.from_dict(_decode(raw))

    def _raw_dict(self) -> dict[str, JsonValue]:
        return _envelope({"action": self.action.to_dict()})

    def to_dict(self) -> dict[str, JsonValue]:
        normalized = type(self).from_dict(self._raw_dict())
        return normalized._raw_dict()

    def to_json(self) -> str:
        return _dump(self.to_dict())


@dataclass(frozen=True, slots=True)
class SessionDetailEnvelope:
    """One bounded local session projection and its newest immutable handoffs."""

    generated_at: int
    session: dict[str, JsonValue]
    handoffs: tuple[dict[str, JsonValue], ...] = ()
    handoffs_truncated: bool = False

    @classmethod
    def from_dict(cls, value: object) -> Self:
        table = _object(value, "envelope")
        _versions(table, allow_explicit_multiline=True)
        known_envelope_fields = {
            "schemaVersion",
            "protocolVersion",
            "generatedAt",
            "session",
            "handoffs",
            "handoffsTruncated",
        }
        for key, item in table.items():
            if key not in known_envelope_fields:
                _json_value(item, f"envelope.{key}")
        raw_session = _object(
            _required(table, "session", "envelope"), "envelope.session"
        )
        host_id = _uuid(
            _required(raw_session, "hostId", "envelope.session"),
            "envelope.session.hostId",
            HostId,
        )
        session = _session_record(
            raw_session,
            "envelope.session",
            expected_host_id=host_id,
        )
        session_key = _session_key(session["sessionKey"], "envelope.session.sessionKey")
        raw_handoffs = _array(
            _required(table, "handoffs", "envelope"), "envelope.handoffs"
        )
        if len(raw_handoffs) > MAX_SESSION_DETAIL_HANDOFFS:
            raise ProtocolError("envelope.handoffs contains too many records")
        handoffs = tuple(
            _handoff_record(
                item,
                f"envelope.handoffs[{index}]",
                expected_session_key=session_key,
            )
            for index, item in enumerate(raw_handoffs)
        )
        handoff_ids = [str(item["handoffId"]) for item in handoffs]
        sequences = [int(item["sequence"]) for item in handoffs]
        if len(handoff_ids) != len(set(handoff_ids)):
            raise ProtocolError("envelope.handoffs contains duplicate handoff IDs")
        if len(sequences) != len(set(sequences)):
            raise ProtocolError("envelope.handoffs contains duplicate sequences")
        if sequences != sorted(sequences, reverse=True):
            raise ProtocolError(
                "envelope.handoffs must use newest-first sequence order"
            )
        latest_handoff_id = session.get("latestHandoffId")
        if handoffs and latest_handoff_id != handoffs[0]["handoffId"]:
            raise ProtocolError("envelope latest handoff pointer is inconsistent")
        if not handoffs and latest_handoff_id is not None:
            raise ProtocolError("envelope omits the retained latest handoff")
        return cls(
            generated_at=_integer(
                _required(table, "generatedAt", "envelope"),
                "envelope.generatedAt",
            ),
            session=session,
            handoffs=handoffs,
            handoffs_truncated=_boolean(
                _required(table, "handoffsTruncated", "envelope"),
                "envelope.handoffsTruncated",
            ),
        )

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> Self:
        return cls.from_dict(_decode(raw))

    def _raw_dict(self) -> dict[str, JsonValue]:
        return _envelope(
            {
                "generatedAt": self.generated_at,
                "session": self.session,
                "handoffs": list(self.handoffs),
                "handoffsTruncated": self.handoffs_truncated,
            }
        )

    def to_dict(self) -> dict[str, JsonValue]:
        normalized = type(self).from_dict(self._raw_dict())
        return normalized._raw_dict()

    def to_json(self) -> str:
        return _dump(self.to_dict(), allow_explicit_multiline=True)


def _agent_caller(value: object, path: str) -> dict[str, JsonValue]:
    table = _object(value, path)
    key = _session_key(_required(table, "sessionKey", path), f"{path}.sessionKey")
    host_id = _uuid(_required(table, "hostId", path), f"{path}.hostId", HostId)
    provider = _provider(_required(table, "provider", path), f"{path}.provider")
    if provider not in {ProviderId.CODEX, ProviderId.CLAUDE}:
        raise ProtocolError(f"{path}.provider is unsupported")
    if key.host_id != host_id or key.provider is not provider:
        raise ProtocolError(f"{path} identity fields disagree")
    return {
        "hostId": str(host_id),
        "provider": provider,
        "sessionKey": str(key),
        "surfaceId": _uuid_text(
            _required(table, "surfaceId", path), f"{path}.surfaceId", SurfaceId
        ),
        "launchId": _uuid_text(
            _required(table, "launchId", path), f"{path}.launchId", LaunchId
        ),
    }


def _agent_project(value: object, path: str) -> dict[str, JsonValue]:
    table = _object(value, path)
    project_path = _string(_required(table, "path", path), f"{path}.path", maximum=4096)
    assert project_path is not None
    if not PurePosixPath(project_path).is_absolute():
        raise ProtocolError(f"{path}.path must be absolute")
    sources = _string_array(
        _required(table, "contextSources", path),
        f"{path}.contextSources",
        maximum_items=MAX_AGENT_CONTEXT_FILES,
        maximum_string=1024,
    )
    canonical_sources: list[str] = []
    for index, source in enumerate(sources):
        parsed = PurePosixPath(source)
        if (
            parsed.is_absolute()
            or ".." in parsed.parts
            or source in {"", "."}
            or source != parsed.as_posix()
        ):
            raise ProtocolError(
                f"{path}.contextSources[{index}] must be a canonical "
                "project-relative path"
            )
        if source in canonical_sources:
            raise ProtocolError(f"{path}.contextSources contains duplicate paths")
        canonical_sources.append(source)
    return {
        "projectId": _uuid_text(
            _required(table, "projectId", path), f"{path}.projectId", ProjectId
        ),
        "name": _string(_required(table, "name", path), f"{path}.name", maximum=256),
        "checkoutId": _uuid_text(
            _required(table, "checkoutId", path), f"{path}.checkoutId", CheckoutId
        ),
        "path": project_path,
        "contextSources": canonical_sources,
    }


def _agent_source(value: object, path: str) -> dict[str, JsonValue]:
    table = _object(value, path)
    source_path = _string(_required(table, "path", path), f"{path}.path", maximum=1024)
    assert source_path is not None
    parsed = PurePosixPath(source_path)
    if parsed.is_absolute() or ".." in parsed.parts or source_path in {"", "."}:
        raise ProtocolError(f"{path}.path must be project-relative")
    text = _multiline_string(
        _required(table, "text", path),
        f"{path}.text",
        maximum=MAX_AGENT_CONTEXT_FILE_BYTES,
    )
    source_id = _string(
        _required(table, "sourceId", path),
        f"{path}.sourceId",
        maximum=1152,
    )
    if source_id != f"file:{parsed.as_posix()}":
        raise ProtocolError(f"{path}.sourceId disagrees with path")
    content_hash = _hash(_required(table, "contentHash", path), f"{path}.contentHash")
    if content_hash != hashlib.sha256(text.encode("utf-8")).hexdigest():
        raise ProtocolError(f"{path}.contentHash does not match text")
    return {
        "sourceId": source_id,
        "path": parsed.as_posix(),
        "observedAt": _integer(
            _required(table, "observedAt", path), f"{path}.observedAt"
        ),
        "text": text,
        "contentHash": content_hash,
        "truncated": _boolean(_required(table, "truncated", path), f"{path}.truncated"),
        "stale": _boolean(_required(table, "stale", path), f"{path}.stale"),
    }


def _agent_session(
    value: object,
    path: str,
    *,
    expected_host_id: HostId,
    expected_project_id: ProjectId,
) -> dict[str, JsonValue]:
    table = _object(value, path)
    key = _session_key(_required(table, "sessionKey", path), f"{path}.sessionKey")
    if key.host_id != expected_host_id:
        raise ProtocolError(f"{path} belongs to another host")
    provider = _provider(_required(table, "provider", path), f"{path}.provider")
    if provider is not key.provider:
        raise ProtocolError(f"{path}.provider disagrees with sessionKey")
    project_id = _uuid(
        _required(table, "projectId", path), f"{path}.projectId", ProjectId
    )
    if project_id != expected_project_id:
        raise ProtocolError(f"{path} belongs to another project")
    result: dict[str, JsonValue] = {
        "sessionKey": str(key),
        "projectId": str(project_id),
        "provider": provider,
        "runtimePresence": _enum(
            _required(table, "runtimePresence", path),
            f"{path}.runtimePresence",
            RuntimePresence,
        ),
        "activity": _enum(
            _required(table, "activity", path), f"{path}.activity", Activity
        ),
        "attachment": _enum(
            _required(table, "attachment", path),
            f"{path}.attachment",
            Attachment,
        ),
        "lastObservedAt": _integer(
            _required(table, "lastObservedAt", path), f"{path}.lastObservedAt"
        ),
        "pinned": _boolean(_required(table, "pinned", path), f"{path}.pinned"),
        "stale": _boolean(_required(table, "stale", path), f"{path}.stale"),
    }
    _optional_string(result, table, "name", path, maximum=512)
    _optional_string(result, table, "purpose", path, maximum=4096)
    _optional_integer(result, table, "wrappedAt", path)
    if "nameActor" in table:
        actor = _string(table["nameActor"], f"{path}.nameActor", maximum=16)
        if actor not in {"user", "agent"}:
            raise ProtocolError(f"{path}.nameActor is unsupported")
        result["nameActor"] = actor
    if "latestHandoff" in table:
        result["latestHandoff"] = _handoff_record(
            table["latestHandoff"],
            f"{path}.latestHandoff",
            expected_session_key=key,
        )
    return result


def _agent_issue(value: object, path: str) -> dict[str, JsonValue]:
    table = _object(value, path)
    return {
        "code": _string(_required(table, "code", path), f"{path}.code", maximum=128),
        "path": _string(_required(table, "path", path), f"{path}.path", maximum=1024),
        "message": _string(
            _required(table, "message", path), f"{path}.message", maximum=1024
        ),
    }


@dataclass(frozen=True, slots=True)
class AgentContextEnvelope:
    """Bounded project context for one authorized managed agent session."""

    generated_at: int
    caller: dict[str, JsonValue]
    project: dict[str, JsonValue]
    stable_sources: tuple[dict[str, JsonValue], ...] = ()
    stable_sources_truncated: bool = False
    sessions: tuple[dict[str, JsonValue], ...] = ()
    sessions_truncated: bool = False
    issues: tuple[dict[str, JsonValue], ...] = ()

    @classmethod
    def from_dict(cls, value: object) -> Self:
        table = _object(value, "envelope")
        _versions(table, allow_explicit_multiline=True)
        caller = _agent_caller(
            _required(table, "caller", "envelope"), "envelope.caller"
        )
        project = _agent_project(
            _required(table, "project", "envelope"), "envelope.project"
        )
        host_id = HostId(str(caller["hostId"]))
        project_id = ProjectId(str(project["projectId"]))
        caller_key = SessionKey.parse(str(caller["sessionKey"]))

        raw_sources = _array(
            _required(table, "stableSources", "envelope"),
            "envelope.stableSources",
        )
        if len(raw_sources) > MAX_AGENT_CONTEXT_FILES:
            raise ProtocolError("envelope.stableSources contains too many records")
        stable_sources = tuple(
            _agent_source(item, f"envelope.stableSources[{index}]")
            for index, item in enumerate(raw_sources)
        )
        source_ids = [str(item["sourceId"]) for item in stable_sources]
        source_paths = [str(item["path"]) for item in stable_sources]
        if len(source_ids) != len(set(source_ids)) or len(source_paths) != len(
            set(source_paths)
        ):
            raise ProtocolError("envelope.stableSources contains duplicate identity")
        configured_sources = [str(item) for item in project["contextSources"]]
        for source_path in source_paths:
            if not any(
                source_path == configured or source_path.startswith(f"{configured}/")
                for configured in configured_sources
            ):
                raise ProtocolError(
                    "envelope.stableSources contains an undeclared source"
                )
        if sum(len(str(item["text"]).encode("utf-8")) for item in stable_sources) > (
            MAX_AGENT_CONTEXT_TOTAL_BYTES
        ):
            raise ProtocolError("envelope.stableSources exceeds its total byte limit")

        raw_sessions = _array(
            _required(table, "sessions", "envelope"), "envelope.sessions"
        )
        if len(raw_sessions) > MAX_AGENT_CONTEXT_SESSIONS:
            raise ProtocolError("envelope.sessions contains too many records")
        sessions = tuple(
            _agent_session(
                item,
                f"envelope.sessions[{index}]",
                expected_host_id=host_id,
                expected_project_id=project_id,
            )
            for index, item in enumerate(raw_sessions)
        )
        session_keys = [str(item["sessionKey"]) for item in sessions]
        if len(session_keys) != len(set(session_keys)):
            raise ProtocolError("envelope.sessions contains duplicate session keys")
        if not session_keys or session_keys[0] != str(caller_key):
            raise ProtocolError(
                "envelope.sessions must put the authorized caller first"
            )

        raw_issues = _array(_required(table, "issues", "envelope"), "envelope.issues")
        if len(raw_issues) > MAX_AGENT_CONTEXT_ISSUES:
            raise ProtocolError("envelope.issues contains too many records")
        issues = tuple(
            _agent_issue(item, f"envelope.issues[{index}]")
            for index, item in enumerate(raw_issues)
        )
        return cls(
            generated_at=_integer(
                _required(table, "generatedAt", "envelope"),
                "envelope.generatedAt",
            ),
            caller=caller,
            project=project,
            stable_sources=stable_sources,
            stable_sources_truncated=_boolean(
                _required(table, "stableSourcesTruncated", "envelope"),
                "envelope.stableSourcesTruncated",
            ),
            sessions=sessions,
            sessions_truncated=_boolean(
                _required(table, "sessionsTruncated", "envelope"),
                "envelope.sessionsTruncated",
            ),
            issues=issues,
        )

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> Self:
        return cls.from_dict(_decode(raw))

    def _raw_dict(self) -> dict[str, JsonValue]:
        return _envelope(
            {
                "generatedAt": self.generated_at,
                "caller": self.caller,
                "project": self.project,
                "stableSources": list(self.stable_sources),
                "stableSourcesTruncated": self.stable_sources_truncated,
                "sessions": list(self.sessions),
                "sessionsTruncated": self.sessions_truncated,
                "issues": list(self.issues),
            }
        )

    def to_dict(self) -> dict[str, JsonValue]:
        normalized = type(self).from_dict(self._raw_dict())
        return normalized._raw_dict()

    def to_json(self) -> str:
        return _dump(self.to_dict(), allow_explicit_multiline=True)


@dataclass(frozen=True, slots=True)
class AgentSessionListEnvelope:
    """Bounded retained sessions for one authorized local project."""

    generated_at: int
    caller: dict[str, JsonValue]
    project: dict[str, JsonValue]
    sessions: tuple[dict[str, JsonValue], ...] = ()
    sessions_truncated: bool = False

    @classmethod
    def from_dict(cls, value: object) -> Self:
        table = _object(value, "envelope")
        _versions(table, allow_explicit_multiline=True)
        caller = _agent_caller(
            _required(table, "caller", "envelope"), "envelope.caller"
        )
        project = _agent_project(
            _required(table, "project", "envelope"), "envelope.project"
        )
        raw_sessions = _array(
            _required(table, "sessions", "envelope"), "envelope.sessions"
        )
        if len(raw_sessions) > MAX_AGENT_PROJECT_SESSIONS:
            raise ProtocolError("envelope.sessions contains too many records")
        sessions = tuple(
            _agent_session(
                item,
                f"envelope.sessions[{index}]",
                expected_host_id=HostId(str(caller["hostId"])),
                expected_project_id=ProjectId(str(project["projectId"])),
            )
            for index, item in enumerate(raw_sessions)
        )
        keys = [str(item["sessionKey"]) for item in sessions]
        if len(keys) != len(set(keys)):
            raise ProtocolError("envelope.sessions contains duplicate session keys")
        if not keys or keys[0] != str(caller["sessionKey"]):
            raise ProtocolError(
                "envelope.sessions must put the authorized caller first"
            )
        return cls(
            generated_at=_integer(
                _required(table, "generatedAt", "envelope"), "envelope.generatedAt"
            ),
            caller=caller,
            project=project,
            sessions=sessions,
            sessions_truncated=_boolean(
                _required(table, "sessionsTruncated", "envelope"),
                "envelope.sessionsTruncated",
            ),
        )

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> Self:
        return cls.from_dict(_decode(raw))

    def _raw_dict(self) -> dict[str, JsonValue]:
        return _envelope(
            {
                "generatedAt": self.generated_at,
                "caller": self.caller,
                "project": self.project,
                "sessions": list(self.sessions),
                "sessionsTruncated": self.sessions_truncated,
            }
        )

    def to_dict(self) -> dict[str, JsonValue]:
        normalized = type(self).from_dict(self._raw_dict())
        return normalized._raw_dict()

    def to_json(self) -> str:
        return _dump(self.to_dict(), allow_explicit_multiline=True)


@dataclass(frozen=True, slots=True)
class AgentHandoffEnvelope:
    """One exact immutable handoff authorized through a caller project."""

    generated_at: int
    caller: dict[str, JsonValue]
    project_id: str
    handoff: dict[str, JsonValue]

    @classmethod
    def from_dict(cls, value: object) -> Self:
        table = _object(value, "envelope")
        _versions(table, allow_explicit_multiline=True)
        caller = _agent_caller(
            _required(table, "caller", "envelope"), "envelope.caller"
        )
        project_id = _uuid_text(
            _required(table, "projectId", "envelope"),
            "envelope.projectId",
            ProjectId,
        )
        handoff_table = _object(
            _required(table, "handoff", "envelope"), "envelope.handoff"
        )
        handoff_key = _session_key(
            _required(handoff_table, "sessionKey", "envelope.handoff"),
            "envelope.handoff.sessionKey",
        )
        handoff = _handoff_record(
            handoff_table,
            "envelope.handoff",
            expected_session_key=handoff_key,
        )
        caller_host_id = HostId(str(caller["hostId"]))
        source_host_id = HostId(str(handoff["sourceHostId"]))
        if handoff["source"] is HandoffSource.IMPORTED:
            if (
                handoff_key.host_id != source_host_id
                or source_host_id == caller_host_id
            ):
                raise ProtocolError(
                    "envelope imported handoff source identity is inconsistent"
                )
        elif handoff_key.host_id != caller_host_id or source_host_id != caller_host_id:
            raise ProtocolError("envelope.handoff belongs to another host")
        return cls(
            generated_at=_integer(
                _required(table, "generatedAt", "envelope"), "envelope.generatedAt"
            ),
            caller=caller,
            project_id=project_id,
            handoff=handoff,
        )

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> Self:
        return cls.from_dict(_decode(raw))

    def _raw_dict(self) -> dict[str, JsonValue]:
        return _envelope(
            {
                "generatedAt": self.generated_at,
                "caller": self.caller,
                "projectId": self.project_id,
                "handoff": self.handoff,
            }
        )

    def to_dict(self) -> dict[str, JsonValue]:
        normalized = type(self).from_dict(self._raw_dict())
        return normalized._raw_dict()

    def to_json(self) -> str:
        return _dump(self.to_dict(), allow_explicit_multiline=True)


def _agent_search_result(
    value: object,
    path: str,
    *,
    expected_host_id: HostId,
) -> dict[str, JsonValue]:
    table = _object(value, path)
    kind = _string(_required(table, "kind", path), f"{path}.kind", maximum=16)
    key = _session_key(_required(table, "sessionKey", path), f"{path}.sessionKey")
    if key.host_id != expected_host_id:
        raise ProtocolError(f"{path} belongs to another host")
    result: dict[str, JsonValue] = {
        "kind": kind,
        "sessionKey": str(key),
        "observedAt": _integer(
            _required(table, "observedAt", path), f"{path}.observedAt"
        ),
    }
    if kind == "session":
        provider = _provider(_required(table, "provider", path), f"{path}.provider")
        if provider is not key.provider:
            raise ProtocolError(f"{path}.provider disagrees with sessionKey")
        result["provider"] = provider
        _optional_string(result, table, "name", path, maximum=512)
        _optional_string(result, table, "purpose", path, maximum=4096)
        return result
    if kind != "handoff":
        raise ProtocolError(f"{path}.kind is unsupported")
    sequence = _integer(_required(table, "sequence", path), f"{path}.sequence")
    if sequence < 1:
        raise ProtocolError(f"{path}.sequence must be positive")
    raw_summary = _required(table, "summary", path)
    raw_next_action = _required(table, "nextAction", path)
    try:
        summary = normalize_handoff_text(raw_summary, "summary")
        next_action = normalize_handoff_text(raw_next_action, "nextAction")
    except ValidationError as exc:
        raise ProtocolError(f"{path}: {exc}") from exc
    if summary != raw_summary or next_action != raw_next_action:
        raise ProtocolError(f"{path} handoff text is not canonically normalized")
    try:
        source = HandoffSource(
            _string(_required(table, "source", path), f"{path}.source", maximum=32)
        )
    except ValueError as exc:
        raise ProtocolError(f"{path}.source is not supported") from exc
    result.update(
        {
            "handoffId": _uuid_text(
                _required(table, "handoffId", path), f"{path}.handoffId", HandoffId
            ),
            "sequence": sequence,
            "summary": summary,
            "nextAction": next_action,
            "source": source,
        }
    )
    return result


@dataclass(frozen=True, slots=True)
class AgentSearchEnvelope:
    """Bounded search results from Switchboard's curated retained state."""

    generated_at: int
    caller: dict[str, JsonValue]
    project_id: str
    query: str
    results: tuple[dict[str, JsonValue], ...] = ()
    results_truncated: bool = False

    @classmethod
    def from_dict(cls, value: object) -> Self:
        table = _object(value, "envelope")
        _versions(table, allow_explicit_multiline=True)
        caller = _agent_caller(
            _required(table, "caller", "envelope"), "envelope.caller"
        )
        raw_results = _array(
            _required(table, "results", "envelope"), "envelope.results"
        )
        if len(raw_results) > MAX_AGENT_SEARCH_RESULTS:
            raise ProtocolError("envelope.results contains too many records")
        results = tuple(
            _agent_search_result(
                item,
                f"envelope.results[{index}]",
                expected_host_id=HostId(str(caller["hostId"])),
            )
            for index, item in enumerate(raw_results)
        )
        identities = [
            (str(item["kind"]), str(item.get("handoffId", item["sessionKey"])))
            for item in results
        ]
        if len(identities) != len(set(identities)):
            raise ProtocolError("envelope.results contains duplicate records")
        return cls(
            generated_at=_integer(
                _required(table, "generatedAt", "envelope"), "envelope.generatedAt"
            ),
            caller=caller,
            project_id=_uuid_text(
                _required(table, "projectId", "envelope"),
                "envelope.projectId",
                ProjectId,
            ),
            query=_string(
                _required(table, "query", "envelope"),
                "envelope.query",
                maximum=MAX_AGENT_SEARCH_QUERY,
            ),
            results=results,
            results_truncated=_boolean(
                _required(table, "resultsTruncated", "envelope"),
                "envelope.resultsTruncated",
            ),
        )

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> Self:
        return cls.from_dict(_decode(raw))

    def _raw_dict(self) -> dict[str, JsonValue]:
        return _envelope(
            {
                "generatedAt": self.generated_at,
                "caller": self.caller,
                "projectId": self.project_id,
                "query": self.query,
                "results": list(self.results),
                "resultsTruncated": self.results_truncated,
            }
        )

    def to_dict(self) -> dict[str, JsonValue]:
        normalized = type(self).from_dict(self._raw_dict())
        return normalized._raw_dict()

    def to_json(self) -> str:
        return _dump(self.to_dict(), allow_explicit_multiline=True)


@dataclass(frozen=True, slots=True)
class AgentMemoryEnvelope:
    """Bounded optional memory-adapter result with explicit availability."""

    generated_at: int
    caller: dict[str, JsonValue]
    project_id: str
    query: str
    adapter: str
    available: bool
    text: str
    truncated: bool
    issues: tuple[dict[str, JsonValue], ...] = ()

    @classmethod
    def from_dict(cls, value: object) -> Self:
        table = _object(value, "envelope")
        _versions(table, allow_explicit_multiline=True)
        caller = _agent_caller(
            _required(table, "caller", "envelope"), "envelope.caller"
        )
        raw_issues = _array(_required(table, "issues", "envelope"), "envelope.issues")
        if len(raw_issues) > MAX_AGENT_CONTEXT_ISSUES:
            raise ProtocolError("envelope.issues contains too many records")
        available = _boolean(
            _required(table, "available", "envelope"), "envelope.available"
        )
        text = _multiline_string(
            _required(table, "text", "envelope"),
            "envelope.text",
            maximum=MAX_AGENT_MEMORY_TEXT_BYTES,
        )
        truncated = _boolean(
            _required(table, "truncated", "envelope"), "envelope.truncated"
        )
        if not available and (text or truncated):
            raise ProtocolError(
                "envelope unavailable memory must have empty untruncated text"
            )
        return cls(
            generated_at=_integer(
                _required(table, "generatedAt", "envelope"), "envelope.generatedAt"
            ),
            caller=caller,
            project_id=_uuid_text(
                _required(table, "projectId", "envelope"),
                "envelope.projectId",
                ProjectId,
            ),
            query=_string(
                _required(table, "query", "envelope"),
                "envelope.query",
                maximum=MAX_AGENT_SEARCH_QUERY,
            ),
            adapter=_string(
                _required(table, "adapter", "envelope"),
                "envelope.adapter",
                maximum=128,
            ),
            available=available,
            text=text,
            truncated=truncated,
            issues=tuple(
                _agent_issue(item, f"envelope.issues[{index}]")
                for index, item in enumerate(raw_issues)
            ),
        )

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> Self:
        return cls.from_dict(_decode(raw))

    def _raw_dict(self) -> dict[str, JsonValue]:
        return _envelope(
            {
                "generatedAt": self.generated_at,
                "caller": self.caller,
                "projectId": self.project_id,
                "query": self.query,
                "adapter": self.adapter,
                "available": self.available,
                "text": self.text,
                "truncated": self.truncated,
                "issues": list(self.issues),
            }
        )

    def to_dict(self) -> dict[str, JsonValue]:
        normalized = type(self).from_dict(self._raw_dict())
        return normalized._raw_dict()

    def to_json(self) -> str:
        return _dump(self.to_dict(), allow_explicit_multiline=True)


@dataclass(frozen=True, slots=True)
class SnapshotEnvelope:
    generated_at: int
    host: HostRecord
    projects: tuple[dict[str, JsonValue], ...] = ()
    project_repositories: tuple[dict[str, JsonValue], ...] = ()
    repositories: tuple[dict[str, JsonValue], ...] = ()
    checkouts: tuple[dict[str, JsonValue], ...] = ()
    tasks: tuple[dict[str, JsonValue], ...] = ()
    sessions: tuple[dict[str, JsonValue], ...] = ()
    runtimes: tuple[dict[str, JsonValue], ...] = ()
    surfaces: tuple[dict[str, JsonValue], ...] = ()
    capabilities: tuple[Capability, ...] = ()
    errors: tuple[ErrorRecord, ...] = ()

    @classmethod
    def from_dict(cls, value: object) -> Self:
        table = _object(value, "envelope")
        _versions(table, allow_explicit_multiline=True)
        host = HostRecord.from_dict(_required(table, "host", "envelope"))

        def record_values(name: str) -> Sequence[Any]:
            values = _array(_required(table, name, "envelope"), f"envelope.{name}")
            if len(values) > MAX_SNAPSHOT_RECORDS:
                raise ProtocolError(f"envelope.{name} contains too many records")
            return values

        projects = tuple(
            _project_record(item, f"envelope.projects[{index}]")
            for index, item in enumerate(record_values("projects"))
        )
        project_repositories = tuple(
            _project_repository_record(item, f"envelope.projectRepositories[{index}]")
            for index, item in enumerate(record_values("projectRepositories"))
        )
        repositories = tuple(
            _repository_record(item, f"envelope.repositories[{index}]")
            for index, item in enumerate(record_values("repositories"))
        )
        checkouts = tuple(
            _checkout_record(
                item,
                f"envelope.checkouts[{index}]",
                expected_host_id=host.host_id,
            )
            for index, item in enumerate(record_values("checkouts"))
        )
        tasks = tuple(
            _task_record(
                item,
                f"envelope.tasks[{index}]",
                expected_host_id=host.host_id,
            )
            for index, item in enumerate(record_values("tasks"))
        )
        sessions = tuple(
            _session_record(
                item,
                f"envelope.sessions[{index}]",
                expected_host_id=host.host_id,
            )
            for index, item in enumerate(record_values("sessions"))
        )
        runtimes = tuple(
            _runtime_record(
                item,
                f"envelope.runtimes[{index}]",
                expected_host_id=host.host_id,
            )
            for index, item in enumerate(record_values("runtimes"))
        )
        surfaces = tuple(
            _surface_record(
                item,
                f"envelope.surfaces[{index}]",
                expected_host_id=host.host_id,
            )
            for index, item in enumerate(record_values("surfaces"))
        )
        raw_capabilities = _array(
            _required(table, "capabilities", "envelope"),
            "envelope.capabilities",
        )
        raw_errors = _array(_required(table, "errors", "envelope"), "envelope.errors")
        capabilities = tuple(
            Capability.from_dict(item, f"envelope.capabilities[{index}]")
            for index, item in enumerate(raw_capabilities)
        )
        errors = tuple(
            ErrorRecord.from_dict(item, f"envelope.errors[{index}]")
            for index, item in enumerate(raw_errors)
        )

        def unique(records: Sequence[Mapping[str, JsonValue]], key: str) -> set[str]:
            values = [str(record[key]) for record in records]
            if len(values) != len(set(values)):
                raise ProtocolError(f"envelope contains duplicate {key} values")
            return set(values)

        project_ids = unique(projects, "projectId")
        repository_ids = unique(repositories, "repositoryId")
        unique(checkouts, "checkoutId")
        unique(tasks, "taskId")
        session_keys = unique(sessions, "sessionKey")
        surface_ids = unique(surfaces, "surfaceId")
        if len({capability.provider for capability in capabilities}) != len(
            capabilities
        ):
            raise ProtocolError("envelope contains duplicate provider capabilities")

        memberships = {
            (str(item["projectId"]), str(item["repositoryId"])): item
            for item in project_repositories
        }
        if len(memberships) != len(project_repositories):
            raise ProtocolError("envelope contains duplicate project memberships")
        primary_count: dict[str, int] = {}
        for index, membership in enumerate(project_repositories):
            project_id = str(membership["projectId"])
            repository_id = str(membership["repositoryId"])
            if project_id not in project_ids or repository_id not in repository_ids:
                raise ProtocolError(
                    f"envelope.projectRepositories[{index}] references unknown identity"
                )
            primary_count[project_id] = primary_count.get(project_id, 0) + int(
                bool(membership["isPrimary"])
            )
        for project_id in project_ids:
            member_count = sum(1 for key in memberships if key[0] == project_id)
            if member_count and primary_count.get(project_id) != 1:
                raise ProtocolError(
                    f"envelope project {project_id} requires one primary repository"
                )

        checkouts_by_id = {
            str(checkout["checkoutId"]): checkout for checkout in checkouts
        }
        for index, checkout in enumerate(checkouts):
            if str(checkout["repositoryId"]) not in repository_ids:
                raise ProtocolError(
                    f"envelope.checkouts[{index}].repositoryId is not in repositories"
                )
        tasks_by_id = {str(task["taskId"]): task for task in tasks}
        for index, task in enumerate(tasks):
            project_id = str(task["projectId"])
            if project_id not in project_ids:
                raise ProtocolError(
                    f"envelope.tasks[{index}].projectId is not in projects"
                )
            checkout_id = task.get("checkoutId")
            if checkout_id is not None:
                checkout = checkouts_by_id.get(str(checkout_id))
                if checkout is None:
                    raise ProtocolError(
                        f"envelope.tasks[{index}].checkoutId is not in checkouts"
                    )
                if (project_id, str(checkout["repositoryId"])) not in memberships:
                    raise ProtocolError(
                        f"envelope.tasks[{index}] checkout/project disagree"
                    )
        for index, session in enumerate(sessions):
            project_id = session.get("projectId")
            task_id = session.get("taskId")
            checkout_id = session.get("checkoutId")
            if project_id is not None and str(project_id) not in project_ids:
                raise ProtocolError(
                    f"envelope.sessions[{index}].projectId is not in projects"
                )
            if checkout_id is not None:
                checkout = checkouts_by_id.get(str(checkout_id))
                if checkout is None:
                    raise ProtocolError(
                        f"envelope.sessions[{index}].checkoutId is not in checkouts"
                    )
                if (
                    project_id is None
                    or (str(project_id), str(checkout["repositoryId"]))
                    not in memberships
                ):
                    raise ProtocolError(
                        f"envelope.sessions[{index}] checkout/project disagree"
                    )
            if task_id is not None:
                task = tasks_by_id.get(str(task_id))
                if task is None:
                    raise ProtocolError(
                        f"envelope.sessions[{index}].taskId is not in tasks"
                    )
                if (
                    task["projectId"] != project_id
                    or task.get("checkoutId") != checkout_id
                ):
                    raise ProtocolError(
                        f"envelope.sessions[{index}] task context disagrees"
                    )
            if int(session["lastObservedAt"]) < int(session["firstObservedAt"]):
                raise ProtocolError(
                    f"envelope.sessions[{index}] observation timestamps are reversed"
                )
            surface_id = session.get("surfaceId")
            if surface_id is not None and str(surface_id) not in surface_ids:
                raise ProtocolError(
                    f"envelope.sessions[{index}].surfaceId is not in surfaces"
                )
        for collection_name, records, key_name in (
            ("runtimes", runtimes, "sessionKey"),
            ("surfaces", surfaces, "currentSessionKey"),
        ):
            for index, record in enumerate(records):
                session_key = record.get(key_name)
                if session_key is not None and str(session_key) not in session_keys:
                    raise ProtocolError(
                        f"envelope.{collection_name}[{index}].{key_name} "
                        "is not in sessions"
                    )
        sessions_by_key = {str(session["sessionKey"]): session for session in sessions}
        for index, task in enumerate(tasks):
            current_session_key = task.get("currentSessionKey")
            if current_session_key is None:
                continue
            session = sessions_by_key.get(str(current_session_key))
            if session is None or session.get("taskId") != task["taskId"]:
                raise ProtocolError(
                    f"envelope.tasks[{index}] current session backreference disagrees"
                )
        surfaces_by_id = {str(surface["surfaceId"]): surface for surface in surfaces}
        for index, session in enumerate(sessions):
            surface_id = session.get("surfaceId")
            if surface_id is None:
                continue
            surface = surfaces_by_id[str(surface_id)]
            if surface.get("currentSessionKey") != session["sessionKey"]:
                raise ProtocolError(
                    f"envelope.sessions[{index}] surface binding is inconsistent"
                )
        for index, surface in enumerate(surfaces):
            session_key = surface.get("currentSessionKey")
            if session_key is None:
                continue
            session = sessions_by_key[str(session_key)]
            if session.get("surfaceId") != surface["surfaceId"]:
                raise ProtocolError(
                    f"envelope.surfaces[{index}] session binding is inconsistent"
                )
        for index, error in enumerate(errors):
            if error.host_id is not None and error.host_id != host.host_id:
                raise ProtocolError(
                    f"envelope.errors[{index}].hostId belongs to another host"
                )
            if error.session_key is not None:
                if error.session_key.host_id != host.host_id:
                    raise ProtocolError(
                        f"envelope.errors[{index}].sessionKey belongs to another host"
                    )
                if (
                    error.provider is not None
                    and error.session_key.provider is not error.provider
                ):
                    raise ProtocolError(
                        f"envelope.errors[{index}] session/provider disagree"
                    )

        return cls(
            generated_at=_integer(
                _required(table, "generatedAt", "envelope"),
                "envelope.generatedAt",
            ),
            host=host,
            projects=projects,
            project_repositories=project_repositories,
            repositories=repositories,
            checkouts=checkouts,
            tasks=tasks,
            sessions=sessions,
            runtimes=runtimes,
            surfaces=surfaces,
            capabilities=capabilities,
            errors=errors,
        )

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> Self:
        return cls.from_dict(_decode(raw))

    def _raw_dict(self) -> dict[str, JsonValue]:
        return _envelope(
            {
                "generatedAt": self.generated_at,
                "host": self.host.to_dict(),
                "projects": list(self.projects),
                "projectRepositories": list(self.project_repositories),
                "repositories": list(self.repositories),
                "checkouts": list(self.checkouts),
                "tasks": list(self.tasks),
                "sessions": list(self.sessions),
                "runtimes": list(self.runtimes),
                "surfaces": list(self.surfaces),
                "capabilities": [item.to_dict() for item in self.capabilities],
                "errors": [item.to_dict() for item in self.errors],
            }
        )

    def to_dict(self) -> dict[str, JsonValue]:
        normalized = type(self).from_dict(self._raw_dict())
        return normalized._raw_dict()

    def to_json(self) -> str:
        return _dump(self.to_dict(), allow_explicit_multiline=True)


@dataclass(frozen=True, slots=True)
class ContinuationEnvelope:
    """One exact immutable handoff exported for cross-host continuation."""

    generated_at: int
    source_host_id: HostId
    source_project_id: ProjectId
    source_task_id: TaskId
    source_session_key: SessionKey
    task_title: str
    task_purpose: str | None
    handoff_id: HandoffId
    handoff_sequence: int
    summary: str
    next_action: str
    handoff_created_at: int
    content_hash: str

    @classmethod
    def from_dict(cls, value: object) -> Self:
        table = _object(value, "envelope")
        _versions(table, allow_explicit_multiline=True)
        version = _integer(
            _required(table, "continuationVersion", "envelope"),
            "envelope.continuationVersion",
        )
        if version != CONTINUATION_VERSION:
            raise ProtocolError(
                f"continuation version {version} is not supported; "
                f"expected {CONTINUATION_VERSION}"
            )
        source_host_id = _uuid(
            _required(table, "sourceHostId", "envelope"),
            "envelope.sourceHostId",
            HostId,
        )
        source_session_key = SessionKey.parse(
            _string(
                _required(table, "sourceSessionKey", "envelope"),
                "envelope.sourceSessionKey",
                maximum=512,
            )
            or ""
        )
        if source_session_key.host_id != source_host_id:
            raise ProtocolError("continuation source session belongs to another host")
        title = _string(
            _required(table, "taskTitle", "envelope"),
            "envelope.taskTitle",
            maximum=256,
        )
        assert title is not None
        raw_purpose = _required(table, "taskPurpose", "envelope")
        purpose = (
            None
            if raw_purpose is None
            else _multiline_string(
                raw_purpose,
                "envelope.taskPurpose",
                maximum=4096,
            ).strip()
        )
        if purpose == "":
            raise ProtocolError("envelope.taskPurpose must be null or non-empty")
        summary = normalize_handoff_text(
            _required(table, "summary", "envelope"),
            "summary",
        )
        next_action = normalize_handoff_text(
            _required(table, "nextAction", "envelope"),
            "next action",
        )
        content_hash = _string(
            _required(table, "contentHash", "envelope"),
            "envelope.contentHash",
            maximum=64,
        )
        if (
            content_hash is None
            or len(content_hash) != 64
            or any(character not in "0123456789abcdef" for character in content_hash)
            or content_hash != handoff_content_hash(summary, next_action)
        ):
            raise ProtocolError("continuation content hash is invalid")
        sequence = _integer(
            _required(table, "handoffSequence", "envelope"),
            "envelope.handoffSequence",
        )
        if sequence == 0:
            raise ProtocolError("envelope.handoffSequence must be positive")
        return cls(
            generated_at=_integer(
                _required(table, "generatedAt", "envelope"),
                "envelope.generatedAt",
            ),
            source_host_id=source_host_id,
            source_project_id=_uuid(
                _required(table, "sourceProjectId", "envelope"),
                "envelope.sourceProjectId",
                ProjectId,
            ),
            source_task_id=_uuid(
                _required(table, "sourceTaskId", "envelope"),
                "envelope.sourceTaskId",
                TaskId,
            ),
            source_session_key=source_session_key,
            task_title=title,
            task_purpose=purpose,
            handoff_id=_uuid(
                _required(table, "handoffId", "envelope"),
                "envelope.handoffId",
                HandoffId,
            ),
            handoff_sequence=sequence,
            summary=summary,
            next_action=next_action,
            handoff_created_at=_integer(
                _required(table, "handoffCreatedAt", "envelope"),
                "envelope.handoffCreatedAt",
            ),
            content_hash=content_hash,
        )

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> Self:
        return cls.from_dict(_decode(raw))

    def _raw_dict(self) -> dict[str, JsonValue]:
        return _envelope(
            {
                "continuationVersion": CONTINUATION_VERSION,
                "generatedAt": self.generated_at,
                "sourceHostId": str(self.source_host_id),
                "sourceProjectId": str(self.source_project_id),
                "sourceTaskId": str(self.source_task_id),
                "sourceSessionKey": str(self.source_session_key),
                "taskTitle": self.task_title,
                "taskPurpose": self.task_purpose,
                "handoffId": str(self.handoff_id),
                "handoffSequence": self.handoff_sequence,
                "summary": self.summary,
                "nextAction": self.next_action,
                "handoffCreatedAt": self.handoff_created_at,
                "contentHash": self.content_hash,
            }
        )

    def to_dict(self) -> dict[str, JsonValue]:
        normalized = type(self).from_dict(self._raw_dict())
        return normalized._raw_dict()

    def to_json(self) -> str:
        return _dump(self.to_dict(), allow_explicit_multiline=True)


@dataclass(frozen=True, slots=True)
class FleetError:
    code: str
    message: str
    retryable: bool

    @classmethod
    def from_dict(cls, value: object, path: str = "fleet error") -> Self:
        table = _object(value, path)
        code = _string(_required(table, "code", path), f"{path}.code", maximum=128)
        message = _string(
            _required(table, "message", path),
            f"{path}.message",
            maximum=2048,
        )
        assert code is not None and message is not None
        return cls(
            code=code,
            message=message,
            retryable=_boolean(
                _required(table, "retryable", path), f"{path}.retryable"
            ),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class FleetHost:
    source: FleetSource
    remote_name: str | None
    host_id: HostId | None
    display_name: str
    reachability: FleetReachability
    snapshot_observed_at: int | None
    snapshot_received_at: int | None
    last_attempt_at: int | None
    stale: bool
    error: FleetError | None
    snapshot: SnapshotEnvelope | None

    @classmethod
    def from_dict(cls, value: object, path: str = "fleet host") -> Self:
        table = _object(value, path)
        try:
            source = FleetSource(
                _string(_required(table, "source", path), f"{path}.source")
            )
            reachability = FleetReachability(
                _string(
                    _required(table, "reachability", path),
                    f"{path}.reachability",
                )
            )
        except ValueError as exc:
            raise ProtocolError(f"{path} contains an unsupported enum") from exc
        remote_name = _string(
            _required(table, "remoteName", path),
            f"{path}.remoteName",
            optional=True,
            maximum=128,
        )
        raw_host_id = _required(table, "hostId", path)
        host_id = (
            None
            if raw_host_id is None
            else _uuid(raw_host_id, f"{path}.hostId", HostId)
        )
        display_name = _string(
            _required(table, "displayName", path),
            f"{path}.displayName",
            maximum=256,
        )
        assert display_name is not None

        def optional_integer(key: str) -> int | None:
            raw = _required(table, key, path)
            return None if raw is None else _integer(raw, f"{path}.{key}")

        observed_at = optional_integer("snapshotObservedAt")
        received_at = optional_integer("snapshotReceivedAt")
        last_attempt_at = optional_integer("lastAttemptAt")
        stale = _boolean(_required(table, "stale", path), f"{path}.stale")
        raw_error = _required(table, "error", path)
        error = (
            None
            if raw_error is None
            else FleetError.from_dict(raw_error, f"{path}.error")
        )
        raw_snapshot = _required(table, "snapshot", path)
        snapshot = (
            None if raw_snapshot is None else SnapshotEnvelope.from_dict(raw_snapshot)
        )

        if source is FleetSource.LOCAL:
            if remote_name is not None:
                raise ProtocolError(f"{path}.remoteName must be null for local")
            if host_id is None or snapshot is None:
                raise ProtocolError(f"{path} local entry requires host and snapshot")
            if reachability is not FleetReachability.ONLINE or error is not None:
                raise ProtocolError(f"{path} local entry must be healthy")
            if stale:
                raise ProtocolError(f"{path} local entry cannot be stale")
        elif remote_name is None:
            raise ProtocolError(f"{path}.remoteName is required for remote")

        if snapshot is None:
            if any(value is not None for value in (observed_at, received_at)):
                raise ProtocolError(f"{path} snapshot timestamps require a snapshot")
            if reachability is FleetReachability.ONLINE:
                raise ProtocolError(f"{path} online entry requires a snapshot")
        else:
            if host_id != snapshot.host.host_id:
                raise ProtocolError(f"{path}.hostId disagrees with snapshot")
            if display_name != snapshot.host.display_name:
                raise ProtocolError(f"{path}.displayName disagrees with snapshot")
            if observed_at != snapshot.generated_at or received_at is None:
                raise ProtocolError(f"{path} snapshot timestamps are inconsistent")
        if reachability is FleetReachability.ONLINE and error is not None:
            raise ProtocolError(f"{path} online entry cannot contain an error")
        if reachability is FleetReachability.OFFLINE and error is None:
            raise ProtocolError(f"{path} offline entry requires an error")
        return cls(
            source=source,
            remote_name=remote_name,
            host_id=host_id,
            display_name=display_name,
            reachability=reachability,
            snapshot_observed_at=observed_at,
            snapshot_received_at=received_at,
            last_attempt_at=last_attempt_at,
            stale=stale,
            error=error,
            snapshot=snapshot,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "source": self.source.value,
            "remoteName": self.remote_name,
            "hostId": None if self.host_id is None else str(self.host_id),
            "displayName": self.display_name,
            "reachability": self.reachability.value,
            "snapshotObservedAt": self.snapshot_observed_at,
            "snapshotReceivedAt": self.snapshot_received_at,
            "lastAttemptAt": self.last_attempt_at,
            "stale": self.stale,
            "error": None if self.error is None else self.error.to_dict(),
            "snapshot": None if self.snapshot is None else self.snapshot.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class FleetEnvelope:
    generated_at: int
    local_host_id: HostId
    hosts: tuple[FleetHost, ...]

    @classmethod
    def from_dict(cls, value: object) -> Self:
        table = _object(value, "envelope")
        _versions(table, allow_explicit_multiline=True)
        fleet_version = _integer(
            _required(table, "fleetVersion", "envelope"),
            "envelope.fleetVersion",
        )
        if fleet_version != FLEET_VERSION:
            raise ProtocolError(
                f"fleet version {fleet_version} is not supported; "
                f"expected {FLEET_VERSION}"
            )
        local_host_id = _uuid(
            _required(table, "localHostId", "envelope"),
            "envelope.localHostId",
            HostId,
        )
        raw_hosts = _array(_required(table, "hosts", "envelope"), "envelope.hosts")
        if not raw_hosts or len(raw_hosts) > MAX_FLEET_REMOTES + 1:
            raise ProtocolError("envelope.hosts has an invalid bounded count")
        hosts = tuple(
            FleetHost.from_dict(item, f"envelope.hosts[{index}]")
            for index, item in enumerate(raw_hosts)
        )
        if hosts[0].source is not FleetSource.LOCAL:
            raise ProtocolError("envelope.hosts[0] must be local")
        if hosts[0].host_id != local_host_id:
            raise ProtocolError("envelope.localHostId disagrees with local entry")
        if any(host.source is not FleetSource.REMOTE for host in hosts[1:]):
            raise ProtocolError("envelope contains more than one local entry")
        remote_names = [host.remote_name or "" for host in hosts[1:]]
        if remote_names != sorted(remote_names):
            raise ProtocolError("envelope remote entries are not ordered by alias")
        if len(remote_names) != len(set(remote_names)):
            raise ProtocolError("envelope contains duplicate remote aliases")
        known_ids = [host.host_id for host in hosts if host.host_id is not None]
        if len(known_ids) != len(set(known_ids)):
            raise ProtocolError("envelope contains duplicate known host IDs")
        return cls(
            generated_at=_integer(
                _required(table, "generatedAt", "envelope"),
                "envelope.generatedAt",
            ),
            local_host_id=local_host_id,
            hosts=hosts,
        )

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> Self:
        return cls.from_dict(_decode(raw))

    def _raw_dict(self) -> dict[str, JsonValue]:
        return _envelope(
            {
                "fleetVersion": FLEET_VERSION,
                "generatedAt": self.generated_at,
                "localHostId": str(self.local_host_id),
                "hosts": [host.to_dict() for host in self.hosts],
            }
        )

    def to_dict(self) -> dict[str, JsonValue]:
        normalized = type(self).from_dict(self._raw_dict())
        return normalized._raw_dict()

    def to_json(self) -> str:
        return _dump(self.to_dict(), allow_explicit_multiline=True)
