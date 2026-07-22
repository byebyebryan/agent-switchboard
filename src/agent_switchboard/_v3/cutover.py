"""Bounded, deterministic offline conversion from the exact 0.2 generation.

This module is deliberately the only Phase 6 code allowed to understand the
legacy Config v2 and schema-v10 registry.  It opens the source database
read-only, verifies quiescence, and emits a self-authenticating logical bundle.
The importer consumes only :class:`CutoverBundle`; it never reads legacy files.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Never
from urllib.parse import quote
from uuid import UUID, uuid4

from .config import (
    AutomationConfig,
    ControlTurnsConfig,
    DefaultsConfig,
    HooksConfig,
    HostConfig,
    MemoryConfig,
    ProjectCatalog,
    ProviderConfig,
    RemoteConfig,
    SwitchboardConfig,
    TmuxConfig,
    ViewsConfig,
)
from .domain import (
    Activity,
    ActivityReason,
    Checkout,
    CheckoutId,
    CheckoutKind,
    CompleteReturnPolicy,
    ControlTurnPolicy,
    GenerationId,
    HandoffId,
    HostId,
    Project,
    ProjectId,
    ProjectRepository,
    ProviderId,
    ProviderSession,
    Repository,
    RepositoryId,
    RepositoryKind,
    Resumability,
    RuntimePresence,
    SessionHandoff,
    SessionHandoffSource,
    SessionKey,
    TaskPushPolicy,
    Transport,
    ValidationError,
    ViewMode,
    canonical_json,
    content_hash,
)
from .legacy_v2 import LegacyConfigError, parse_legacy_config

BUNDLE_VERSION: Final = 1
LEGACY_SCHEMA_VERSION: Final = 10
LEGACY_CONFIG_VERSION: Final = 2
LEGACY_PROTOCOL_VERSION: Final = 2
MAX_BUNDLE_BYTES: Final = 16 * 1024 * 1024
MAX_RECORDS: Final = 20_000
_SHA256_LENGTH: Final = 64
_ACTIVE_LAUNCH_STATES: Final = {
    "reserved",
    "surface_ready",
    "waiting_for_client",
    "provider_started",
}


class CutoverError(RuntimeError):
    """The offline source or bundle does not meet the cutover contract."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _fail(code: str, message: str) -> Never:
    raise CutoverError(code, message)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _legacy_handoff_content_hash(summary: str, next_action: str) -> str:
    return _sha256(
        json.dumps(
            {
                "nextAction": unicodedata.normalize("NFC", next_action).strip(),
                "summary": unicodedata.normalize("NFC", summary).strip(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return canonical_json(value).encode("utf-8")


def _known(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        _fail("bundle_invalid", f"{path} has unknown fields: {', '.join(unknown)}")
    if missing:
        _fail("bundle_invalid", f"{path} is missing fields: {', '.join(missing)}")


def _object(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(k, str) for k in value):
        _fail("bundle_invalid", f"{path} must be an object")
    return value


def _array(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, list):
        _fail("bundle_invalid", f"{path} must be an array")
    if len(value) > MAX_RECORDS:
        _fail("bundle_too_large", f"{path} exceeds {MAX_RECORDS} records")
    return value


def _text(
    value: object,
    path: str,
    *,
    maximum: int = 4096,
    optional: bool = False,
    multiline: bool = False,
) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        _fail("bundle_invalid", f"{path} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        _fail("bundle_invalid", f"{path} must not be empty")
    if len(normalized.encode("utf-8")) > maximum:
        _fail("bundle_invalid", f"{path} exceeds {maximum} bytes")
    allowed = "\n\t" if multiline else ""
    if any(
        unicodedata.category(character) == "Cc" and character not in allowed
        for character in normalized
    ):
        _fail("bundle_invalid", f"{path} contains a control character")
    return normalized


def _integer(value: object, path: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("bundle_invalid", f"{path} must be a non-negative integer")
    return value


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        _fail("bundle_invalid", f"{path} must be boolean")
    return value


def _uuid(value: object, path: str) -> str:
    raw = _text(value, path, maximum=36)
    assert raw is not None
    try:
        parsed = UUID(raw)
    except ValueError:
        _fail("bundle_invalid", f"{path} must be a UUID")
    if parsed.int == 0 or str(parsed) != raw:
        _fail("bundle_invalid", f"{path} must be a canonical non-nil UUID")
    return raw


def _optional_uuid(value: object, path: str) -> str | None:
    return None if value is None else _uuid(value, path)


def _hash(value: object, path: str) -> str:
    raw = _text(value, path, maximum=_SHA256_LENGTH)
    assert raw is not None
    if len(raw) != _SHA256_LENGTH or any(c not in "0123456789abcdef" for c in raw):
        _fail("bundle_invalid", f"{path} must be a lowercase SHA-256 digest")
    return raw


def _enum(value: object, path: str, choices: set[str]) -> str:
    raw = _text(value, path, maximum=64)
    assert raw is not None
    if raw not in choices:
        _fail("bundle_invalid", f"{path} has an unsupported value")
    return raw


def _deduplicated(records: Sequence[Mapping[str, Any]], key: str, path: str) -> None:
    values = [record[key] for record in records]
    if len(values) != len(set(values)):
        _fail("bundle_invalid", f"{path} contains duplicate {key}")


@dataclass(frozen=True, slots=True)
class CutoverBundle:
    """A validated canonical CutoverBundle v1 document."""

    body: Mapping[str, Any]
    bundle_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {**self.body, "bundleHash": self.bundle_hash}

    def to_json(self) -> str:
        encoded = _canonical_bytes(self.to_dict())
        if len(encoded) > MAX_BUNDLE_BYTES:
            _fail("bundle_too_large", "bundle exceeds the byte limit")
        return encoded.decode("utf-8")

    @classmethod
    def create(cls, body: Mapping[str, Any]) -> CutoverBundle:
        normalized = _validate_body(body)
        return cls(normalized, _sha256(_canonical_bytes(normalized)))

    @classmethod
    def from_json(cls, raw: bytes | str) -> CutoverBundle:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if not isinstance(encoded, bytes):
            _fail("bundle_invalid", "bundle input must be bytes or text")
        if len(encoded) > MAX_BUNDLE_BYTES:
            _fail("bundle_too_large", "bundle exceeds the byte limit")
        try:
            document = json.loads(encoded, object_pairs_hook=_reject_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise CutoverError("bundle_invalid", f"invalid JSON: {error}") from error
        table = _object(document, "bundle")
        _known(
            table,
            {
                "bundleVersion",
                "source",
                "configuration",
                "catalog",
                "providerSessions",
                "handoffs",
                "historicalTasks",
                "bundleHash",
            },
            "bundle",
        )
        supplied = _hash(table["bundleHash"], "bundle.bundleHash")
        body = {key: value for key, value in table.items() if key != "bundleHash"}
        normalized = _validate_body(body)
        actual = _sha256(_canonical_bytes(normalized))
        if supplied != actual:
            _fail("bundle_hash_mismatch", "bundle content does not match bundleHash")
        return cls(normalized, actual)

    @property
    def host_id(self) -> HostId:
        return HostId(self.body["source"]["hostId"])

    @property
    def exported_at(self) -> int:
        return int(self.body["source"]["exportedAt"])

    def target_config(self, generation_id: GenerationId) -> SwitchboardConfig:
        """Translate the validated legacy configuration into Config v3."""

        configuration = self.body["configuration"]
        catalog = self.body["catalog"]
        projects = tuple(
            Project(
                ProjectId(row["projectId"]),
                row["name"],
                tuple(row["aliases"]),
                None
                if row["defaultProvider"] is None
                else ProviderId(row["defaultProvider"]),
                Transport.TMUX,
            )
            for row in catalog["projects"]
        )
        repositories = tuple(
            Repository(
                RepositoryId(row["repositoryId"]),
                row["name"],
                RepositoryKind(row["kind"]),
                tuple(row["contextSources"]),
            )
            for row in catalog["repositories"]
        )
        memberships = tuple(
            ProjectRepository(
                ProjectId(row["projectId"]),
                RepositoryId(row["repositoryId"]),
                row["isPrimary"],
            )
            for row in catalog["memberships"]
        )
        checkouts = tuple(
            Checkout(
                CheckoutId(row["checkoutId"]),
                RepositoryId(row["repositoryId"]),
                HostId(row["hostId"]),
                Path(row["path"]),
                CheckoutKind(row["kind"]),
                row["displayName"],
                None
                if row["providerOverride"] is None
                else ProviderId(row["providerOverride"]),
                row["isDefault"],
            )
            for row in catalog["checkouts"]
        )
        return SwitchboardConfig(
            GenerationId(generation_id),
            HostConfig(self.host_id, configuration["host"]["displayName"]),
            tuple(
                ProviderConfig(
                    ProviderId(row["provider"]), row["enabled"], row["executable"]
                )
                for row in configuration["providers"]
            ),
            tuple(
                RemoteConfig(row["alias"], row["sshTarget"], row["displayName"])
                for row in configuration["remotes"]
            ),
            ProjectCatalog(projects, repositories, memberships, checkouts),
            DefaultsConfig(
                Transport.TMUX,
                configuration["defaults"]["refreshIntervalSeconds"],
                configuration["defaults"]["stalenessIntervalSeconds"],
            ),
            ViewsConfig(ViewMode.DIRECT, ViewMode.NAVIGATOR),
            AutomationConfig(
                TaskPushPolicy.CONSERVATIVE,
                CompleteReturnPolicy.SYNTHESIZE,
                1,
            ),
            ControlTurnsConfig(ControlTurnPolicy.LIVE_FIRST),
            TmuxConfig(
                configuration["tmux"]["namingPrefix"],
                configuration["tmux"]["launchTimeoutSeconds"],
            ),
            HooksConfig(
                configuration["hooks"]["timeoutSeconds"],
                configuration["hooks"]["latencyBudgetMs"],
            ),
            MemoryConfig(
                configuration["memory"]["enabled"],
                tuple(configuration["memory"]["command"]),
                configuration["memory"]["tool"],
                configuration["memory"]["timeoutSeconds"],
            ),
        )

    def provider_sessions(self) -> tuple[ProviderSession, ...]:
        sessions: list[ProviderSession] = []
        for row in self.body["providerSessions"]:
            key = SessionKey.parse(row["sessionKey"])
            sessions.append(
                ProviderSession(
                    key,
                    HostId(row["hostId"]),
                    ProviderId(row["provider"]),
                    UUID(row["providerSessionId"]),
                    None if row["projectId"] is None else ProjectId(row["projectId"]),
                    None
                    if row["checkoutId"] is None
                    else CheckoutId(row["checkoutId"]),
                    row["name"],
                    row["purpose"],
                    row["pinned"],
                    RuntimePresence(row["runtimePresence"]),
                    Resumability(row["resumability"]),
                    Activity(row["activity"]),
                    ActivityReason(row["activityReason"]),
                    row["createdAt"],
                    row["providerUpdatedAt"],
                    row["lastObservedAt"],
                    row["updatedAt"],
                )
            )
        return tuple(sessions)

    def handoffs(self) -> tuple[SessionHandoff, ...]:
        return tuple(
            SessionHandoff(
                HandoffId(row["handoffId"]),
                SessionKey.parse(row["sessionKey"]),
                row["sequence"],
                row["summary"],
                row["nextAction"],
                SessionHandoffSource(row["source"]),
                HostId(row["sourceHostId"]),
                row["contentHash"],
                row["createdAt"],
            )
            for row in self.body["handoffs"]
        )


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field {key!r}")
        value[key] = item
    return value


def _validate_body(body: Mapping[str, Any]) -> dict[str, Any]:
    table = _object(body, "bundle")
    expected = {
        "bundleVersion",
        "source",
        "configuration",
        "catalog",
        "providerSessions",
        "handoffs",
        "historicalTasks",
    }
    _known(table, expected, "bundle")
    if table["bundleVersion"] != BUNDLE_VERSION:
        _fail("bundle_incompatible", "bundleVersion must be 1")
    source = _validate_source(table["source"])
    configuration = _validate_configuration(table["configuration"])
    catalog = _validate_catalog(table["catalog"], source["hostId"])
    sessions = _validate_sessions(table["providerSessions"], source, catalog)
    handoffs = _validate_handoffs(table["handoffs"], source, sessions)
    tasks = _validate_tasks(table["historicalTasks"], source, catalog, sessions)
    normalized = {
        "bundleVersion": BUNDLE_VERSION,
        "source": source,
        "configuration": configuration,
        "catalog": catalog,
        "providerSessions": sessions,
        "handoffs": handoffs,
        "historicalTasks": tasks,
    }
    if len(_canonical_bytes(normalized)) > MAX_BUNDLE_BYTES:
        _fail("bundle_too_large", "bundle exceeds the byte limit")
    return normalized


def _validate_source(raw: object) -> dict[str, Any]:
    table = _object(raw, "source")
    _known(
        table,
        {
            "schemaVersion",
            "protocolVersion",
            "configVersion",
            "hostId",
            "exportedAt",
            "quiescent",
        },
        "source",
    )
    if (
        table["schemaVersion"] != LEGACY_SCHEMA_VERSION
        or table["protocolVersion"] != LEGACY_PROTOCOL_VERSION
        or table["configVersion"] != LEGACY_CONFIG_VERSION
    ):
        _fail("bundle_incompatible", "source versions are not schema 10/config 2")
    if _boolean(table["quiescent"], "source.quiescent") is not True:
        _fail("source_not_quiescent", "bundle source was not quiescent")
    return {
        "schemaVersion": LEGACY_SCHEMA_VERSION,
        "protocolVersion": LEGACY_PROTOCOL_VERSION,
        "configVersion": LEGACY_CONFIG_VERSION,
        "hostId": _uuid(table["hostId"], "source.hostId"),
        "exportedAt": _integer(table["exportedAt"], "source.exportedAt"),
        "quiescent": True,
    }


def _validate_configuration(raw: object) -> dict[str, Any]:
    table = _object(raw, "configuration")
    _known(
        table,
        {"host", "providers", "remotes", "defaults", "tmux", "hooks", "memory"},
        "configuration",
    )
    host = _object(table["host"], "configuration.host")
    _known(host, {"displayName"}, "configuration.host")
    providers = []
    for index, item in enumerate(_array(table["providers"], "configuration.providers")):
        row = _object(item, f"configuration.providers[{index}]")
        _known(
            row,
            {"provider", "enabled", "executable"},
            f"configuration.providers[{index}]",
        )
        providers.append(
            {
                "provider": _enum(
                    row["provider"],
                    f"configuration.providers[{index}].provider",
                    {"codex", "claude"},
                ),
                "enabled": _boolean(
                    row["enabled"], f"configuration.providers[{index}].enabled"
                ),
                "executable": _text(
                    row["executable"],
                    f"configuration.providers[{index}].executable",
                    optional=True,
                ),
            }
        )
    provider_order = {"codex": 0, "claude": 1}
    providers.sort(key=lambda row: provider_order[row["provider"]])
    _deduplicated(providers, "provider", "configuration.providers")
    remotes = []
    for index, item in enumerate(_array(table["remotes"], "configuration.remotes")):
        row = _object(item, f"configuration.remotes[{index}]")
        _known(
            row,
            {"alias", "sshTarget", "displayName"},
            f"configuration.remotes[{index}]",
        )
        remotes.append(
            {
                "alias": _text(
                    row["alias"], f"configuration.remotes[{index}].alias", maximum=64
                ),
                "sshTarget": _text(
                    row["sshTarget"], f"configuration.remotes[{index}].sshTarget"
                ),
                "displayName": _text(
                    row["displayName"],
                    f"configuration.remotes[{index}].displayName",
                    maximum=256,
                ),
            }
        )
    remotes.sort(key=lambda row: row["alias"])
    _deduplicated(remotes, "alias", "configuration.remotes")
    defaults = _object(table["defaults"], "configuration.defaults")
    _known(
        defaults,
        {"refreshIntervalSeconds", "stalenessIntervalSeconds"},
        "configuration.defaults",
    )
    tmux = _object(table["tmux"], "configuration.tmux")
    _known(tmux, {"namingPrefix", "launchTimeoutSeconds"}, "configuration.tmux")
    hooks = _object(table["hooks"], "configuration.hooks")
    _known(hooks, {"timeoutSeconds", "latencyBudgetMs"}, "configuration.hooks")
    memory = _object(table["memory"], "configuration.memory")
    _known(
        memory, {"enabled", "command", "tool", "timeoutSeconds"}, "configuration.memory"
    )
    command = [
        _text(value, f"configuration.memory.command[{index}]")
        for index, value in enumerate(
            _array(memory["command"], "configuration.memory.command")
        )
    ]
    return {
        "host": {
            "displayName": _text(
                host["displayName"], "configuration.host.displayName", maximum=256
            )
        },
        "providers": providers,
        "remotes": remotes,
        "defaults": {
            "refreshIntervalSeconds": _integer(
                defaults["refreshIntervalSeconds"],
                "configuration.defaults.refreshIntervalSeconds",
            ),
            "stalenessIntervalSeconds": _integer(
                defaults["stalenessIntervalSeconds"],
                "configuration.defaults.stalenessIntervalSeconds",
            ),
        },
        "tmux": {
            "namingPrefix": _text(
                tmux["namingPrefix"], "configuration.tmux.namingPrefix", maximum=32
            ),
            "launchTimeoutSeconds": _integer(
                tmux["launchTimeoutSeconds"], "configuration.tmux.launchTimeoutSeconds"
            ),
        },
        "hooks": {
            "timeoutSeconds": _integer(
                hooks["timeoutSeconds"], "configuration.hooks.timeoutSeconds"
            ),
            "latencyBudgetMs": _integer(
                hooks["latencyBudgetMs"], "configuration.hooks.latencyBudgetMs"
            ),
        },
        "memory": {
            "enabled": _boolean(memory["enabled"], "configuration.memory.enabled"),
            "command": command,
            "tool": _text(memory["tool"], "configuration.memory.tool", maximum=128),
            "timeoutSeconds": _integer(
                memory["timeoutSeconds"], "configuration.memory.timeoutSeconds"
            ),
        },
    }


def _validate_catalog(raw: object, host_id: str) -> dict[str, Any]:
    table = _object(raw, "catalog")
    _known(table, {"projects", "repositories", "memberships", "checkouts"}, "catalog")
    projects = []
    for index, item in enumerate(_array(table["projects"], "catalog.projects")):
        row = _object(item, f"catalog.projects[{index}]")
        _known(
            row,
            {"projectId", "name", "aliases", "defaultProvider"},
            f"catalog.projects[{index}]",
        )
        aliases = [
            _text(value, f"catalog.projects[{index}].aliases", maximum=128)
            for value in _array(row["aliases"], f"catalog.projects[{index}].aliases")
        ]
        projects.append(
            {
                "projectId": _uuid(
                    row["projectId"], f"catalog.projects[{index}].projectId"
                ),
                "name": _text(
                    row["name"], f"catalog.projects[{index}].name", maximum=256
                ),
                "aliases": aliases,
                "defaultProvider": None
                if row["defaultProvider"] is None
                else _enum(
                    row["defaultProvider"],
                    f"catalog.projects[{index}].defaultProvider",
                    {"codex", "claude"},
                ),
            }
        )
    repositories = []
    for index, item in enumerate(_array(table["repositories"], "catalog.repositories")):
        row = _object(item, f"catalog.repositories[{index}]")
        _known(
            row,
            {"repositoryId", "name", "kind", "contextSources"},
            f"catalog.repositories[{index}]",
        )
        sources = [
            _text(value, f"catalog.repositories[{index}].contextSources", maximum=1024)
            for value in _array(
                row["contextSources"], f"catalog.repositories[{index}].contextSources"
            )
        ]
        repositories.append(
            {
                "repositoryId": _uuid(
                    row["repositoryId"], f"catalog.repositories[{index}].repositoryId"
                ),
                "name": _text(
                    row["name"], f"catalog.repositories[{index}].name", maximum=256
                ),
                "kind": _enum(
                    row["kind"],
                    f"catalog.repositories[{index}].kind",
                    {"git", "directory"},
                ),
                "contextSources": sources,
            }
        )
    memberships = []
    for index, item in enumerate(_array(table["memberships"], "catalog.memberships")):
        row = _object(item, f"catalog.memberships[{index}]")
        _known(
            row,
            {"projectId", "repositoryId", "isPrimary"},
            f"catalog.memberships[{index}]",
        )
        memberships.append(
            {
                "projectId": _uuid(
                    row["projectId"], f"catalog.memberships[{index}].projectId"
                ),
                "repositoryId": _uuid(
                    row["repositoryId"], f"catalog.memberships[{index}].repositoryId"
                ),
                "isPrimary": _boolean(
                    row["isPrimary"], f"catalog.memberships[{index}].isPrimary"
                ),
            }
        )
    checkouts = []
    for index, item in enumerate(_array(table["checkouts"], "catalog.checkouts")):
        row = _object(item, f"catalog.checkouts[{index}]")
        _known(
            row,
            {
                "checkoutId",
                "repositoryId",
                "hostId",
                "path",
                "kind",
                "displayName",
                "providerOverride",
                "isDefault",
            },
            f"catalog.checkouts[{index}]",
        )
        row_host = _uuid(row["hostId"], f"catalog.checkouts[{index}].hostId")
        if row_host != host_id:
            _fail("bundle_authority", "checkout is not owned by the source host")
        path = _text(row["path"], f"catalog.checkouts[{index}].path")
        assert path is not None
        if not Path(path).is_absolute():
            _fail("bundle_invalid", "checkout path must be absolute")
        checkouts.append(
            {
                "checkoutId": _uuid(
                    row["checkoutId"], f"catalog.checkouts[{index}].checkoutId"
                ),
                "repositoryId": _uuid(
                    row["repositoryId"], f"catalog.checkouts[{index}].repositoryId"
                ),
                "hostId": row_host,
                "path": path,
                "kind": _enum(
                    row["kind"],
                    f"catalog.checkouts[{index}].kind",
                    {"main", "worktree", "directory"},
                ),
                "displayName": _text(
                    row["displayName"],
                    f"catalog.checkouts[{index}].displayName",
                    maximum=256,
                    optional=True,
                ),
                "providerOverride": None
                if row["providerOverride"] is None
                else _enum(
                    row["providerOverride"],
                    f"catalog.checkouts[{index}].providerOverride",
                    {"codex", "claude"},
                ),
                "isDefault": _boolean(
                    row["isDefault"], f"catalog.checkouts[{index}].isDefault"
                ),
            }
        )
    projects.sort(key=lambda row: row["projectId"])
    repositories.sort(key=lambda row: row["repositoryId"])
    memberships.sort(key=lambda row: (row["projectId"], row["repositoryId"]))
    checkouts.sort(key=lambda row: row["checkoutId"])
    _deduplicated(projects, "projectId", "catalog.projects")
    _deduplicated(repositories, "repositoryId", "catalog.repositories")
    _deduplicated(checkouts, "checkoutId", "catalog.checkouts")
    project_ids = {row["projectId"] for row in projects}
    repository_ids = {row["repositoryId"] for row in repositories}
    membership_keys = {(row["projectId"], row["repositoryId"]) for row in memberships}
    if len(membership_keys) != len(memberships):
        _fail("bundle_invalid", "catalog contains duplicate membership")
    if any(
        row["projectId"] not in project_ids or row["repositoryId"] not in repository_ids
        for row in memberships
    ):
        _fail("bundle_reference", "catalog membership reference is missing")
    if any(row["repositoryId"] not in repository_ids for row in checkouts):
        _fail("bundle_reference", "checkout repository reference is missing")
    for project_id in project_ids:
        if (
            sum(
                row["isPrimary"]
                for row in memberships
                if row["projectId"] == project_id
            )
            > 1
        ):
            _fail("bundle_invalid", "project has multiple primary repositories")
    return {
        "projects": projects,
        "repositories": repositories,
        "memberships": memberships,
        "checkouts": checkouts,
    }


def _validate_sessions(
    raw: object, source: Mapping[str, Any], catalog: Mapping[str, Any]
) -> list[dict[str, Any]]:
    project_ids = {row["projectId"] for row in catalog["projects"]}
    checkout_ids = {row["checkoutId"] for row in catalog["checkouts"]}
    sessions = []
    fields = {
        "sessionKey",
        "hostId",
        "provider",
        "providerSessionId",
        "projectId",
        "checkoutId",
        "name",
        "purpose",
        "pinned",
        "runtimePresence",
        "resumability",
        "activity",
        "activityReason",
        "createdAt",
        "providerUpdatedAt",
        "lastObservedAt",
        "updatedAt",
    }
    for index, item in enumerate(_array(raw, "providerSessions")):
        row = _object(item, f"providerSessions[{index}]")
        _known(row, fields, f"providerSessions[{index}]")
        host_id = _uuid(row["hostId"], f"providerSessions[{index}].hostId")
        provider = _enum(
            row["provider"], f"providerSessions[{index}].provider", {"codex", "claude"}
        )
        provider_id = _uuid(
            row["providerSessionId"], f"providerSessions[{index}].providerSessionId"
        )
        session_key = _text(
            row["sessionKey"], f"providerSessions[{index}].sessionKey", maximum=512
        )
        if (
            host_id != source["hostId"]
            or session_key != f"{host_id}:{provider}:{provider_id}"
        ):
            _fail("bundle_authority", "provider session identity is not source-owned")
        project_id = _optional_uuid(
            row["projectId"], f"providerSessions[{index}].projectId"
        )
        checkout_id = _optional_uuid(
            row["checkoutId"], f"providerSessions[{index}].checkoutId"
        )
        if project_id is not None and project_id not in project_ids:
            _fail("bundle_reference", "session project reference is missing")
        if checkout_id is not None and checkout_id not in checkout_ids:
            _fail("bundle_reference", "session checkout reference is missing")
        sessions.append(
            {
                "sessionKey": session_key,
                "hostId": host_id,
                "provider": provider,
                "providerSessionId": provider_id,
                "projectId": project_id,
                "checkoutId": checkout_id,
                "name": _text(
                    row["name"],
                    f"providerSessions[{index}].name",
                    maximum=512,
                    optional=True,
                ),
                "purpose": _text(
                    row["purpose"],
                    f"providerSessions[{index}].purpose",
                    optional=True,
                    multiline=True,
                ),
                "pinned": _boolean(row["pinned"], f"providerSessions[{index}].pinned"),
                "runtimePresence": _enum(
                    row["runtimePresence"],
                    f"providerSessions[{index}].runtimePresence",
                    {"stopped", "unknown"},
                ),
                "resumability": _enum(
                    row["resumability"],
                    f"providerSessions[{index}].resumability",
                    {"resumable", "missing", "unknown"},
                ),
                "activity": _enum(
                    row["activity"],
                    f"providerSessions[{index}].activity",
                    {"working", "needs_input", "ready", "completed", "unknown"},
                ),
                "activityReason": _enum(
                    row["activityReason"],
                    f"providerSessions[{index}].activityReason",
                    {
                        "permission",
                        "question",
                        "elicitation",
                        "turn_complete",
                        "provider_complete",
                        "error",
                        "unknown",
                    },
                ),
                "createdAt": _integer(
                    row["createdAt"],
                    f"providerSessions[{index}].createdAt",
                    optional=True,
                ),
                "providerUpdatedAt": _integer(
                    row["providerUpdatedAt"],
                    f"providerSessions[{index}].providerUpdatedAt",
                    optional=True,
                ),
                "lastObservedAt": _integer(
                    row["lastObservedAt"], f"providerSessions[{index}].lastObservedAt"
                ),
                "updatedAt": _integer(
                    row["updatedAt"], f"providerSessions[{index}].updatedAt"
                ),
            }
        )
    sessions.sort(key=lambda row: row["sessionKey"])
    _deduplicated(sessions, "sessionKey", "providerSessions")
    return sessions


def _validate_handoffs(
    raw: object, source: Mapping[str, Any], sessions: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    session_keys = {row["sessionKey"] for row in sessions}
    handoffs = []
    fields = {
        "handoffId",
        "sessionKey",
        "sequence",
        "summary",
        "nextAction",
        "source",
        "sourceHostId",
        "contentHash",
        "createdAt",
    }
    for index, item in enumerate(_array(raw, "handoffs")):
        row = _object(item, f"handoffs[{index}]")
        _known(row, fields, f"handoffs[{index}]")
        session_key = _text(
            row["sessionKey"], f"handoffs[{index}].sessionKey", maximum=512
        )
        source_host = _uuid(row["sourceHostId"], f"handoffs[{index}].sourceHostId")
        if session_key not in session_keys or source_host != source["hostId"]:
            _fail("bundle_reference", "handoff is not linked to a source session")
        handoffs.append(
            {
                "handoffId": _uuid(row["handoffId"], f"handoffs[{index}].handoffId"),
                "sessionKey": session_key,
                "sequence": _integer(row["sequence"], f"handoffs[{index}].sequence"),
                "summary": _text(
                    row["summary"],
                    f"handoffs[{index}].summary",
                    maximum=65536,
                    multiline=True,
                ),
                "nextAction": _text(
                    row["nextAction"],
                    f"handoffs[{index}].nextAction",
                    maximum=65536,
                    multiline=True,
                ),
                "source": _enum(
                    row["source"],
                    f"handoffs[{index}].source",
                    {"user", "agent", "imported"},
                ),
                "sourceHostId": source_host,
                "contentHash": _hash(
                    row["contentHash"], f"handoffs[{index}].contentHash"
                ),
                "createdAt": _integer(row["createdAt"], f"handoffs[{index}].createdAt"),
            }
        )
    handoffs.sort(
        key=lambda row: (row["sessionKey"], row["sequence"], row["handoffId"])
    )
    _deduplicated(handoffs, "handoffId", "handoffs")
    if len({(row["sessionKey"], row["sequence"]) for row in handoffs}) != len(handoffs):
        _fail("bundle_invalid", "handoff sequence is duplicated")
    try:
        for row in handoffs:
            SessionHandoff(
                HandoffId(row["handoffId"]),
                SessionKey.parse(row["sessionKey"]),
                row["sequence"],
                row["summary"],
                row["nextAction"],
                SessionHandoffSource(row["source"]),
                HostId(row["sourceHostId"]),
                row["contentHash"],
                row["createdAt"],
            )
    except ValidationError as error:
        raise CutoverError("bundle_invalid", str(error)) from error
    return handoffs


def _validate_tasks(
    raw: object,
    source: Mapping[str, Any],
    catalog: Mapping[str, Any],
    sessions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    project_ids = {row["projectId"] for row in catalog["projects"]}
    checkout_ids = {row["checkoutId"] for row in catalog["checkouts"]}
    session_keys = {row["sessionKey"] for row in sessions}
    tasks = []
    fields = {
        "taskId",
        "hostId",
        "projectId",
        "checkoutId",
        "title",
        "purpose",
        "preferredProvider",
        "status",
        "pinned",
        "currentSessionKey",
        "createdAt",
        "updatedAt",
        "closedAt",
    }
    for index, item in enumerate(_array(raw, "historicalTasks")):
        row = _object(item, f"historicalTasks[{index}]")
        _known(row, fields, f"historicalTasks[{index}]")
        host_id = _uuid(row["hostId"], f"historicalTasks[{index}].hostId")
        project_id = _uuid(row["projectId"], f"historicalTasks[{index}].projectId")
        checkout_id = _optional_uuid(
            row["checkoutId"], f"historicalTasks[{index}].checkoutId"
        )
        session_key = _text(
            row["currentSessionKey"],
            f"historicalTasks[{index}].currentSessionKey",
            maximum=512,
            optional=True,
        )
        if (
            host_id != source["hostId"]
            or project_id not in project_ids
            or (checkout_id is not None and checkout_id not in checkout_ids)
            or (session_key is not None and session_key not in session_keys)
        ):
            _fail("bundle_reference", "historical task reference is missing")
        task_status = _enum(
            row["status"],
            f"historicalTasks[{index}].status",
            {"open", "closed"},
        )
        created_at = _integer(row["createdAt"], f"historicalTasks[{index}].createdAt")
        updated_at = _integer(row["updatedAt"], f"historicalTasks[{index}].updatedAt")
        closed_at = _integer(
            row["closedAt"], f"historicalTasks[{index}].closedAt", optional=True
        )
        if updated_at < created_at or (
            closed_at is not None and closed_at < created_at
        ):
            _fail("bundle_invalid", "historical task timestamps are inconsistent")
        if (task_status == "closed") != (closed_at is not None):
            _fail("bundle_invalid", "historical task close state is inconsistent")
        tasks.append(
            {
                "taskId": _uuid(row["taskId"], f"historicalTasks[{index}].taskId"),
                "hostId": host_id,
                "projectId": project_id,
                "checkoutId": checkout_id,
                "title": _text(
                    row["title"], f"historicalTasks[{index}].title", maximum=256
                ),
                "purpose": _text(
                    row["purpose"],
                    f"historicalTasks[{index}].purpose",
                    optional=True,
                    multiline=True,
                ),
                "preferredProvider": None
                if row["preferredProvider"] is None
                else _enum(
                    row["preferredProvider"],
                    f"historicalTasks[{index}].preferredProvider",
                    {"codex", "claude"},
                ),
                "status": task_status,
                "pinned": _boolean(row["pinned"], f"historicalTasks[{index}].pinned"),
                "currentSessionKey": session_key,
                "createdAt": created_at,
                "updatedAt": updated_at,
                "closedAt": closed_at,
            }
        )
    tasks.sort(key=lambda row: row["taskId"])
    _deduplicated(tasks, "taskId", "historicalTasks")
    return tasks


def _read_rows(connection: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql)]


def _open_legacy_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        _fail("source_missing", "legacy database does not exist")
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as error:
        raise CutoverError("source_open_failed", str(error)) from error
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def export_legacy(
    database_path: str | Path,
    config_data: bytes | str,
    *,
    exported_at: int,
) -> CutoverBundle:
    """Read exact legacy state without mutating it and return one bundle."""

    timestamp = _integer(exported_at, "exported_at")
    assert timestamp is not None
    database = Path(database_path)
    with _open_legacy_read_only(database) as connection:
        connection.execute("BEGIN")
        try:
            schema = connection.execute("PRAGMA user_version").fetchone()[0]
            if schema != LEGACY_SCHEMA_VERSION:
                _fail("source_incompatible", "legacy database must be schema v10")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                _fail("source_corrupt", "legacy database integrity check failed")
            metadata = dict(
                connection.execute("SELECT key, value FROM registry_metadata")
            )
            if metadata.get("schema_version") != str(
                LEGACY_SCHEMA_VERSION
            ) or metadata.get("protocol_version") != str(LEGACY_PROTOCOL_VERSION):
                _fail("source_incompatible", "legacy registry metadata is not v10/v2")
            hosts = _read_rows(
                connection, "SELECT * FROM hosts WHERE is_local = 1 ORDER BY host_id"
            )
            if len(hosts) != 1:
                _fail("source_authority", "legacy registry must have one local host")
            host = hosts[0]
            try:
                legacy = parse_legacy_config(
                    config_data, host_id=HostId(host["host_id"])
                )
            except LegacyConfigError as error:
                raise CutoverError("source_config_invalid", str(error)) from error
            if legacy.host.display_name != host["display_name"]:
                _fail(
                    "source_catalog_mismatch",
                    "Config v2 and registry host display identity differ",
                )
            active_launches = connection.execute(
                "SELECT count(*) FROM launch_intents WHERE state IN (?, ?, ?, ?)",
                tuple(sorted(_ACTIVE_LAUNCH_STATES)),
            ).fetchone()[0]
            live_sessions = connection.execute(
                "SELECT count(*) FROM sessions "
                "WHERE host_id = ? AND runtime_presence = 'live'",
                (host["host_id"],),
            ).fetchone()[0]
            active_surfaces = connection.execute(
                "SELECT count(*) FROM surfaces "
                "WHERE host_id = ? AND retired_at IS NULL",
                (host["host_id"],),
            ).fetchone()[0]
            if active_launches or live_sessions or active_surfaces:
                _fail(
                    "source_not_quiescent",
                    "legacy launches, runtimes, or surfaces remain active",
                )

            config_project_ids = {str(row.project_id) for row in legacy.projects}
            config_repository_ids = {
                str(row.repository_id) for row in legacy.repositories
            }
            config_checkout_ids = {str(row.checkout_id) for row in legacy.checkouts}
            projects = _read_rows(
                connection,
                "SELECT * FROM projects WHERE declared = 1 ORDER BY project_id",
            )
            repositories = _read_rows(
                connection,
                "SELECT * FROM repositories WHERE declared = 1 ORDER BY repository_id",
            )
            checkouts = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM checkouts "
                    "WHERE host_id = ? AND declared = 1 ORDER BY checkout_id",
                    (host["host_id"],),
                )
            ]
            if (
                {row["project_id"] for row in projects} != config_project_ids
                or {row["repository_id"] for row in repositories}
                != config_repository_ids
                or {row["checkout_id"] for row in checkouts} != config_checkout_ids
            ):
                _fail(
                    "source_catalog_mismatch",
                    "Config v2 and declared registry catalog differ",
                )
            memberships = [
                dict(row)
                for row in connection.execute(
                    "SELECT membership.* "
                    "FROM project_repositories AS membership "
                    "JOIN projects "
                    "  ON projects.project_id = membership.project_id "
                    "JOIN repositories "
                    "  ON repositories.repository_id = membership.repository_id "
                    "WHERE projects.declared = 1 AND repositories.declared = 1 "
                    "ORDER BY membership.project_id, membership.repository_id"
                )
            ]
            expected_projects = {
                str(item.project_id): (
                    item.name,
                    tuple(item.aliases),
                    None
                    if item.default_provider is None
                    else item.default_provider.value,
                    item.default_transport.value,
                )
                for item in legacy.projects
            }
            actual_projects = {
                row["project_id"]: (
                    row["name"],
                    tuple(json.loads(row["aliases_json"])),
                    row["default_provider"],
                    row["default_transport"],
                )
                for row in projects
            }
            expected_repositories = {
                str(item.repository_id): (
                    item.name,
                    item.kind.value,
                    tuple(item.context_sources),
                )
                for item in legacy.repositories
            }
            actual_repositories = {
                row["repository_id"]: (
                    row["name"],
                    row["kind"],
                    tuple(json.loads(row["context_sources_json"])),
                )
                for row in repositories
            }
            expected_memberships = {
                (str(item.project_id), str(item.repository_id), item.is_primary)
                for item in legacy.project_repositories
            }
            actual_memberships = {
                (
                    row["project_id"],
                    row["repository_id"],
                    bool(row["is_primary"]),
                )
                for row in memberships
            }
            expected_checkouts = {
                str(item.checkout_id): (
                    str(item.repository_id),
                    str(item.host_id),
                    str(item.path),
                    item.kind.value,
                    item.display_name,
                    None
                    if item.provider_override is None
                    else item.provider_override.value,
                    None
                    if item.transport_override is None
                    else item.transport_override.value,
                    item.is_default,
                )
                for item in legacy.checkouts
            }
            actual_checkouts = {
                row["checkout_id"]: (
                    row["repository_id"],
                    row["host_id"],
                    row["path"],
                    row["kind"],
                    row["display_name"],
                    row["provider_override"],
                    row["transport_override"],
                    bool(row["is_default"]),
                )
                for row in checkouts
            }
            if (
                actual_projects != expected_projects
                or actual_repositories != expected_repositories
                or actual_memberships != expected_memberships
                or actual_checkouts != expected_checkouts
            ):
                _fail(
                    "source_catalog_mismatch",
                    "Config v2 and declared registry catalog fields differ",
                )
            sessions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM sessions WHERE host_id = ? ORDER BY session_key",
                    (host["host_id"],),
                )
            ]
            exported_session_keys = {
                row["session_key"]
                for row in sessions
                if row["project_id"] is None or row["project_id"] in config_project_ids
            }
            sessions = [
                row for row in sessions if row["session_key"] in exported_session_keys
            ]
            handoffs = [
                dict(row)
                for row in connection.execute(
                    "SELECT handoff.* FROM handoffs AS handoff "
                    "JOIN sessions AS session "
                    "  ON session.session_key = handoff.session_key "
                    "WHERE session.host_id = ? "
                    "ORDER BY handoff.session_key, handoff.sequence, "
                    "handoff.handoff_id",
                    (host["host_id"],),
                )
                if row["session_key"] in exported_session_keys
            ]
            if any(
                row["content_hash"]
                != _legacy_handoff_content_hash(row["summary"], row["next_action"])
                for row in handoffs
            ):
                _fail("source_corrupt", "legacy handoff content hash mismatch")
            tasks = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM tasks WHERE host_id = ? ORDER BY task_id",
                    (host["host_id"],),
                )
                if row["project_id"] in config_project_ids
                and (
                    row["current_session_key"] is None
                    or row["current_session_key"] in exported_session_keys
                )
            ]
        finally:
            connection.execute("ROLLBACK")

    body = {
        "bundleVersion": BUNDLE_VERSION,
        "source": {
            "schemaVersion": LEGACY_SCHEMA_VERSION,
            "protocolVersion": LEGACY_PROTOCOL_VERSION,
            "configVersion": LEGACY_CONFIG_VERSION,
            "hostId": host["host_id"],
            "exportedAt": timestamp,
            "quiescent": True,
        },
        "configuration": {
            "host": {"displayName": legacy.host.display_name},
            "providers": [
                {
                    "provider": item.provider.value,
                    "enabled": item.enabled,
                    "executable": item.executable,
                }
                for item in legacy.providers
            ],
            "remotes": [
                {
                    "alias": item.alias,
                    "sshTarget": item.ssh_target,
                    "displayName": item.display_name,
                }
                for item in legacy.remotes
            ],
            "defaults": {
                "refreshIntervalSeconds": legacy.defaults.refresh_interval_seconds,
                "stalenessIntervalSeconds": legacy.defaults.staleness_interval_seconds,
            },
            "tmux": {
                "namingPrefix": legacy.tmux.naming_prefix,
                "launchTimeoutSeconds": legacy.tmux.launch_timeout_seconds,
            },
            "hooks": {
                "timeoutSeconds": legacy.hooks.timeout_seconds,
                "latencyBudgetMs": legacy.hooks.latency_budget_ms,
            },
            "memory": {
                "enabled": legacy.memory.enabled,
                "command": list(legacy.memory.command),
                "tool": legacy.memory.tool,
                "timeoutSeconds": legacy.memory.timeout_seconds,
            },
        },
        "catalog": {
            "projects": [
                {
                    "projectId": row["project_id"],
                    "name": row["name"],
                    "aliases": json.loads(row["aliases_json"]),
                    "defaultProvider": row["default_provider"],
                }
                for row in projects
            ],
            "repositories": [
                {
                    "repositoryId": row["repository_id"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "contextSources": json.loads(row["context_sources_json"]),
                }
                for row in repositories
            ],
            "memberships": [
                {
                    "projectId": row["project_id"],
                    "repositoryId": row["repository_id"],
                    "isPrimary": bool(row["is_primary"]),
                }
                for row in memberships
            ],
            "checkouts": [
                {
                    "checkoutId": row["checkout_id"],
                    "repositoryId": row["repository_id"],
                    "hostId": row["host_id"],
                    "path": row["path"],
                    "kind": row["kind"],
                    "displayName": row["display_name"],
                    "providerOverride": row["provider_override"],
                    "isDefault": bool(row["is_default"]),
                }
                for row in checkouts
            ],
        },
        "providerSessions": [
            {
                "sessionKey": row["session_key"],
                "hostId": row["host_id"],
                "provider": row["provider"],
                "providerSessionId": row["provider_session_id"],
                "projectId": row["project_id"],
                "checkoutId": row["checkout_id"],
                "name": row["name"],
                "purpose": row["purpose"],
                "pinned": bool(row["pinned"]),
                "runtimePresence": row["runtime_presence"],
                "resumability": row["resumability"],
                "activity": row["activity"],
                "activityReason": row["activity_reason"],
                "createdAt": row["created_at"],
                "providerUpdatedAt": row["provider_updated_at"],
                "lastObservedAt": row["last_observed_at"],
                "updatedAt": max(
                    row["last_observed_at"],
                    row["provider_updated_at"] or 0,
                    row["created_at"] or 0,
                ),
            }
            for row in sessions
        ],
        "handoffs": [
            {
                "handoffId": row["handoff_id"],
                "sessionKey": row["session_key"],
                "sequence": row["sequence"],
                "summary": row["summary"],
                "nextAction": row["next_action"],
                "source": row["source"],
                "sourceHostId": row["source_host_id"],
                "contentHash": content_hash(row["summary"], row["next_action"]),
                "createdAt": row["created_at"],
            }
            for row in handoffs
        ],
        "historicalTasks": [
            {
                "taskId": row["task_id"],
                "hostId": row["host_id"],
                "projectId": row["project_id"],
                "checkoutId": row["checkout_id"],
                "title": row["title"],
                "purpose": row["purpose"],
                "preferredProvider": row["preferred_provider"],
                "status": row["status"],
                "pinned": bool(row["pinned"]),
                "currentSessionKey": row["current_session_key"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "closedAt": row["closed_at"],
            }
            for row in tasks
        ],
    }
    return CutoverBundle.create(body)


def export_artifacts(
    database_path: str | Path,
    config_path: str | Path,
    destination: str | Path,
    *,
    exported_at: int,
) -> CutoverBundle:
    """Create one immutable offline backup directory and exact bundle.

    SQLite's backup API folds any source WAL into a consistent schema-v10 copy.
    The bundle is then derived from that retained copy, so both artifacts
    describe the same logical snapshot without writing the live source.
    """

    source_database = Path(database_path)
    source_config = Path(config_path)
    target = Path(destination)
    if target.exists():
        _fail("export_destination_exists", "export destination already exists")
    try:
        config_data = source_config.read_bytes()
    except OSError as error:
        raise CutoverError("source_config_missing", str(error)) from error
    temporary = target.parent / f".cutover-export-{uuid4()}"
    temporary.mkdir(mode=0o700, parents=False)
    try:
        database_copy = temporary / "legacy-switchboard.db"
        with _open_legacy_read_only(source_database) as source:
            destination_connection = sqlite3.connect(database_copy)
            try:
                source.backup(destination_connection)
                destination_connection.execute("PRAGMA journal_mode = DELETE")
            finally:
                destination_connection.close()
        bundle = export_legacy(database_copy, config_data, exported_at=exported_at)
        config_copy = temporary / "legacy-config.toml"
        bundle_copy = temporary / "cutover-bundle.json"
        manifest_copy = temporary / "export-manifest.json"
        config_copy.write_bytes(config_data)
        bundle_copy.write_text(bundle.to_json(), encoding="utf-8")
        manifest = {
            "artifactVersion": 1,
            "bundleHash": bundle.bundle_hash,
            "bundleSha256": _sha256(bundle_copy.read_bytes()),
            "configSha256": _sha256(config_copy.read_bytes()),
            "databaseSha256": _sha256(database_copy.read_bytes()),
            "exportedAt": exported_at,
        }
        manifest_copy.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        for path in (database_copy, config_copy, bundle_copy, manifest_copy):
            path.chmod(0o400)
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        directory = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        temporary.chmod(0o500)
        os.replace(temporary, target)
        parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return bundle
    except BaseException:
        if temporary.exists():
            temporary.chmod(0o700)
            shutil.rmtree(temporary)
        raise


__all__ = [
    "BUNDLE_VERSION",
    "LEGACY_CONFIG_VERSION",
    "LEGACY_SCHEMA_VERSION",
    "CutoverBundle",
    "CutoverError",
    "export_artifacts",
    "export_legacy",
]
