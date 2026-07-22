"""Offline-only parser for the exact Config v2 cutover source."""

from __future__ import annotations

import tomllib
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    DefaultsConfig,
    HooksConfig,
    HostConfig,
    MemoryConfig,
    ProviderConfig,
    RemoteConfig,
    TmuxConfig,
)
from .domain import (
    CheckoutId,
    CheckoutKind,
    HostId,
    ProjectId,
    ProviderId,
    RepositoryId,
    RepositoryKind,
    Transport,
)


class LegacyConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LegacyProject:
    project_id: ProjectId
    name: str
    aliases: tuple[str, ...]
    default_provider: ProviderId | None
    default_transport: Transport


@dataclass(frozen=True, slots=True)
class LegacyRepository:
    repository_id: RepositoryId
    name: str
    kind: RepositoryKind
    context_sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LegacyMembership:
    project_id: ProjectId
    repository_id: RepositoryId
    is_primary: bool


@dataclass(frozen=True, slots=True)
class LegacyCheckout:
    checkout_id: CheckoutId
    repository_id: RepositoryId
    host_id: HostId
    path: Path
    kind: CheckoutKind
    display_name: str | None
    provider_override: ProviderId | None
    transport_override: Transport | None
    is_default: bool


@dataclass(frozen=True, slots=True)
class LegacyConfig:
    host: HostConfig
    providers: tuple[ProviderConfig, ...]
    remotes: tuple[RemoteConfig, ...]
    projects: tuple[LegacyProject, ...]
    repositories: tuple[LegacyRepository, ...]
    project_repositories: tuple[LegacyMembership, ...]
    checkouts: tuple[LegacyCheckout, ...]
    defaults: DefaultsConfig
    tmux: TmuxConfig
    hooks: HooksConfig
    memory: MemoryConfig


def _table(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise LegacyConfigError(f"{path} must be a table")
    return value


def _known(table: Mapping[str, Any], fields: set[str], path: str) -> None:
    unknown = sorted(set(table) - fields)
    if unknown:
        raise LegacyConfigError(f"{path} has unknown fields: {', '.join(unknown)}")


def _text(value: object, path: str, *, maximum: int = 4_096) -> str:
    if not isinstance(value, str):
        raise LegacyConfigError(f"{path} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized.encode()) > maximum:
        raise LegacyConfigError(f"{path} is invalid")
    return normalized


def _bool(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise LegacyConfigError(f"{path} must be boolean")
    return value


def _integer(value: object, path: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise LegacyConfigError(f"{path} is outside its integer bounds")
    return value


def _enum(enum_type, value: object, path: str):
    try:
        return enum_type(_text(value, path, maximum=64))
    except ValueError as error:
        raise LegacyConfigError(f"{path} is unsupported") from error


def _uuid(value_type, value: object, path: str):
    try:
        return value_type(_text(value, path, maximum=36))
    except ValueError as error:
        raise LegacyConfigError(f"{path} is not a canonical UUID") from error


def _optional_enum(enum_type, table: Mapping[str, Any], key: str, path: str):
    return None if key not in table else _enum(enum_type, table[key], f"{path}.{key}")


def parse_legacy_config(data: bytes | str, *, host_id: HostId) -> LegacyConfig:
    """Parse only the bounded v2 fields required for an offline export."""

    try:
        document = tomllib.loads(data.decode() if isinstance(data, bytes) else data)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise LegacyConfigError("configuration is not valid TOML") from error
    root = _table(document, "configuration")
    _known(
        root,
        {
            "config_version",
            "host",
            "providers",
            "remotes",
            "projects",
            "defaults",
            "tmux",
            "hooks",
            "memory",
        },
        "configuration",
    )
    if root.get("config_version") != 2:
        raise LegacyConfigError("configuration.config_version must be 2")
    host_table = _table(root.get("host", {}), "host")
    _known(host_table, {"display_name"}, "host")
    host = HostConfig(
        host_id,
        _text(
            host_table.get("display_name", "local"),
            "host.display_name",
            maximum=256,
        ),
    )

    providers_table = _table(root.get("providers", {}), "providers")
    _known(providers_table, {item.value for item in ProviderId}, "providers")
    providers: list[ProviderConfig] = []
    for provider in ProviderId:
        item = _table(
            providers_table.get(provider.value, {}), f"providers.{provider.value}"
        )
        _known(item, {"enabled", "executable"}, f"providers.{provider.value}")
        executable = (
            None
            if "executable" not in item
            else _text(item["executable"], "provider executable")
        )
        providers.append(
            ProviderConfig(
                provider,
                _bool(item.get("enabled", True), "provider enabled"),
                executable,
            )
        )

    remotes_table = _table(root.get("remotes", {}), "remotes")
    remotes: list[RemoteConfig] = []
    for alias in sorted(remotes_table):
        item = _table(remotes_table[alias], f"remotes.{alias}")
        _known(item, {"ssh_target", "display_name"}, f"remotes.{alias}")
        target = _text(item.get("ssh_target"), f"remotes.{alias}.ssh_target")
        if target.startswith("-") or any(character.isspace() for character in target):
            raise LegacyConfigError(f"remotes.{alias}.ssh_target is unsafe")
        remotes.append(
            RemoteConfig(
                alias,
                target,
                _text(
                    item.get("display_name", alias),
                    f"remotes.{alias}.display_name",
                    maximum=256,
                ),
            )
        )

    projects_table = _table(root.get("projects", {}), "projects")
    projects: list[LegacyProject] = []
    repositories: list[LegacyRepository] = []
    memberships: list[LegacyMembership] = []
    checkouts: list[LegacyCheckout] = []
    for project_key in sorted(projects_table):
        project_id = _uuid(ProjectId, project_key, "project ID")
        item = _table(projects_table[project_key], f"projects.{project_key}")
        _known(
            item,
            {
                "name",
                "aliases",
                "default_provider",
                "default_transport",
                "repositories",
            },
            f"projects.{project_key}",
        )
        raw_aliases = item.get("aliases", [])
        if not isinstance(raw_aliases, Sequence) or isinstance(
            raw_aliases, (str, bytes)
        ):
            raise LegacyConfigError("project aliases must be an array")
        aliases = tuple(
            dict.fromkeys(
                " ".join(_text(value, "project alias", maximum=256).split())
                for value in raw_aliases
            )
        )
        projects.append(
            LegacyProject(
                project_id,
                _text(item.get("name"), "project name", maximum=256),
                aliases,
                _optional_enum(ProviderId, item, "default_provider", "project"),
                _enum(
                    Transport,
                    item.get("default_transport", "tmux"),
                    "project.default_transport",
                ),
            )
        )
        raw_repositories = item.get("repositories", [])
        if not isinstance(raw_repositories, list):
            raise LegacyConfigError("project repositories must be an array")
        primary_count = 0
        for repository_item in raw_repositories:
            repository = _table(repository_item, "repository")
            _known(
                repository,
                {
                    "repository_id",
                    "name",
                    "kind",
                    "is_primary",
                    "context_sources",
                    "checkouts",
                },
                "repository",
            )
            repository_id = _uuid(
                RepositoryId, repository.get("repository_id"), "repository ID"
            )
            raw_sources = repository.get("context_sources", [])
            if not isinstance(raw_sources, list):
                raise LegacyConfigError("context_sources must be an array")
            sources = tuple(
                dict.fromkeys(
                    _text(value, "context source", maximum=1_024)
                    for value in raw_sources
                )
            )
            repositories.append(
                LegacyRepository(
                    repository_id,
                    _text(repository.get("name"), "repository name", maximum=256),
                    _enum(
                        RepositoryKind,
                        repository.get("kind", "git"),
                        "repository kind",
                    ),
                    sources,
                )
            )
            is_primary = _bool(
                repository.get("is_primary", False), "repository.is_primary"
            )
            primary_count += int(is_primary)
            memberships.append(LegacyMembership(project_id, repository_id, is_primary))
            raw_checkouts = repository.get("checkouts", [])
            if not isinstance(raw_checkouts, list):
                raise LegacyConfigError("repository checkouts must be an array")
            for checkout_item in raw_checkouts:
                checkout = _table(checkout_item, "checkout")
                _known(
                    checkout,
                    {
                        "checkout_id",
                        "path",
                        "kind",
                        "display_name",
                        "provider_override",
                        "transport_override",
                        "is_default",
                    },
                    "checkout",
                )
                path = Path(_text(checkout.get("path"), "checkout path"))
                if not path.is_absolute():
                    raise LegacyConfigError("checkout path must be absolute")
                checkouts.append(
                    LegacyCheckout(
                        _uuid(
                            CheckoutId,
                            checkout.get("checkout_id"),
                            "checkout ID",
                        ),
                        repository_id,
                        host_id,
                        path,
                        _enum(
                            CheckoutKind,
                            checkout.get("kind", "main"),
                            "checkout kind",
                        ),
                        (
                            None
                            if "display_name" not in checkout
                            else _text(
                                checkout["display_name"],
                                "checkout display name",
                                maximum=256,
                            )
                        ),
                        _optional_enum(
                            ProviderId, checkout, "provider_override", "checkout"
                        ),
                        _optional_enum(
                            Transport, checkout, "transport_override", "checkout"
                        ),
                        _bool(checkout.get("is_default", False), "checkout.is_default"),
                    )
                )
        if raw_repositories and primary_count != 1:
            raise LegacyConfigError("project must contain one primary repository")

    defaults_table = _table(root.get("defaults", {}), "defaults")
    defaults = DefaultsConfig(
        Transport.TMUX,
        _integer(
            defaults_table.get("refresh_interval_seconds", 30),
            "defaults.refresh_interval_seconds",
            1,
            86_400,
        ),
        _integer(
            defaults_table.get("staleness_interval_seconds", 120),
            "defaults.staleness_interval_seconds",
            1,
            604_800,
        ),
    )
    tmux_table = _table(root.get("tmux", {}), "tmux")
    hooks_table = _table(root.get("hooks", {}), "hooks")
    memory_table = _table(root.get("memory", {}), "memory")
    raw_command = memory_table.get("command", [])
    if not isinstance(raw_command, list):
        raise LegacyConfigError("memory.command must be an array")
    return LegacyConfig(
        host,
        tuple(providers),
        tuple(remotes),
        tuple(sorted(projects, key=lambda item: str(item.project_id))),
        tuple(sorted(repositories, key=lambda item: str(item.repository_id))),
        tuple(
            sorted(
                memberships,
                key=lambda item: (str(item.project_id), str(item.repository_id)),
            )
        ),
        tuple(sorted(checkouts, key=lambda item: str(item.checkout_id))),
        defaults,
        TmuxConfig(
            _text(
                tmux_table.get("naming_prefix", "as"),
                "tmux.naming_prefix",
                maximum=32,
            ),
            _integer(
                tmux_table.get("launch_timeout_seconds", 30),
                "tmux.launch_timeout_seconds",
                1,
                300,
            ),
        ),
        HooksConfig(
            _integer(
                hooks_table.get("timeout_seconds", 1),
                "hooks.timeout_seconds",
                1,
                30,
            ),
            _integer(
                hooks_table.get("latency_budget_ms", 250),
                "hooks.latency_budget_ms",
                10,
                5_000,
            ),
        ),
        MemoryConfig(
            _bool(memory_table.get("enabled", False), "memory.enabled"),
            tuple(_text(value, "memory command") for value in raw_command),
            _text(memory_table.get("tool", "search"), "memory.tool", maximum=128),
            _integer(
                memory_table.get("timeout_seconds", 5),
                "memory.timeout_seconds",
                1,
                60,
            ),
        ),
    )


__all__ = ["LegacyConfigError", "parse_legacy_config"]
