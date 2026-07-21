"""Strict standard-library TOML configuration loading."""

from __future__ import annotations

import json
import re
import socket
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Never

from .domain import (
    Checkout,
    CheckoutId,
    CheckoutKind,
    HostId,
    Project,
    ProjectId,
    ProjectRepository,
    ProviderId,
    Repository,
    RepositoryId,
    RepositoryKind,
    Transport,
    ValidationError,
    merge_checkouts,
    merge_project_repositories,
    merge_projects,
    merge_repositories,
)
from .paths import config_path, load_or_create_host_id
from .repository_discovery import RepositoryDiscoveryError, probe_git_repository

_MAX_CONTEXT_SOURCES = 32
MAX_REMOTES = 32
_DEFAULT_HOOK_LATENCY_BUDGET_MS = 250
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TMUX_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
_MEMORY_TOOL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class ConfigError(ValidationError):
    """Configuration syntax or semantics are invalid."""


class WorkingDirectoryPolicy(StrEnum):
    PROJECT_DEFAULT = "project_default"
    CURRENT = "current"
    REQUIRE_EXPLICIT = "require_explicit"


@dataclass(frozen=True, slots=True)
class HostConfig:
    host_id: HostId
    display_name: str


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider: ProviderId
    enabled: bool = True
    executable: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteConfig:
    alias: str
    ssh_target: str
    display_name: str


@dataclass(frozen=True, slots=True)
class DefaultsConfig:
    transport: Transport = Transport.TMUX
    refresh_interval_seconds: int = 30
    staleness_interval_seconds: int = 120
    recent_parked_limit: int = 100
    working_directory: WorkingDirectoryPolicy = WorkingDirectoryPolicy.PROJECT_DEFAULT


@dataclass(frozen=True, slots=True)
class TmuxConfig:
    naming_prefix: str = "as"
    launch_timeout_seconds: int = 30


@dataclass(frozen=True, slots=True)
class HooksConfig:
    timeout_seconds: int = 1
    latency_budget_ms: int = _DEFAULT_HOOK_LATENCY_BUDGET_MS


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    enabled: bool = False
    command: tuple[str, ...] = ()
    tool: str = "search"
    timeout_seconds: int = 5


@dataclass(frozen=True, slots=True)
class ProjectCatalog:
    projects: tuple[Project, ...]
    repositories: tuple[Repository, ...]
    project_repositories: tuple[ProjectRepository, ...]
    checkouts: tuple[Checkout, ...]


@dataclass(frozen=True, slots=True)
class SwitchboardConfig:
    host: HostConfig
    providers: tuple[ProviderConfig, ...]
    remotes: tuple[RemoteConfig, ...]
    catalog: ProjectCatalog
    defaults: DefaultsConfig
    tmux: TmuxConfig
    hooks: HooksConfig
    memory: MemoryConfig = MemoryConfig()

    @property
    def projects(self) -> tuple[Project, ...]:
        return self.catalog.projects

    @property
    def checkouts(self) -> tuple[Checkout, ...]:
        return self.catalog.checkouts

    @property
    def repositories(self) -> tuple[Repository, ...]:
        return self.catalog.repositories

    @property
    def project_repositories(self) -> tuple[ProjectRepository, ...]:
        return self.catalog.project_repositories


def _fail(path: str, message: str) -> Never:
    raise ConfigError(f"{path}: {message}")


def _table(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(path, "must be a TOML table")
    if not all(isinstance(key, str) for key in value):
        _fail(path, "contains a non-string key")
    return value


def _known(table: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        _fail(path, f"unknown key(s): {', '.join(unknown)}")


def _string(
    value: object,
    path: str,
    *,
    maximum: int = 1024,
    optional: bool = False,
) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        _fail(path, "must be a string")
    if any(unicodedata.category(char) == "Cc" for char in value):
        _fail(path, "contains control characters")
    value = value.strip()
    if not value:
        _fail(path, "must not be empty")
    if len(value) > maximum:
        _fail(path, f"must be at most {maximum} characters")
    return value


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, "must be a boolean")
    return value


def _integer(
    value: object,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = 86_400,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "must be an integer")
    if not minimum <= value <= maximum:
        _fail(path, f"must be between {minimum} and {maximum}")
    return value


def _provider(value: object, path: str) -> ProviderId:
    try:
        return ProviderId(_string(value, path))
    except ValueError:
        _fail(path, "must be 'codex' or 'claude'")


def _transport(value: object, path: str) -> Transport:
    try:
        return Transport(_string(value, path))
    except ValueError:
        _fail(path, "must be 'tmux'")


def _uuid[T: HostId | ProjectId | RepositoryId | CheckoutId](
    value: object, path: str, value_type: type[T]
) -> T:
    try:
        return value_type(_string(value, path))
    except ValidationError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _parse_host(raw: object, host_id: HostId) -> HostConfig:
    table = _table(raw, "host")
    _known(table, {"display_name"}, "host")
    display_name = _string(
        table.get("display_name", socket.gethostname()),
        "host.display_name",
        maximum=256,
    )
    assert display_name is not None
    return HostConfig(host_id=host_id, display_name=display_name)


def _parse_providers(raw: object) -> tuple[ProviderConfig, ...]:
    table = _table(raw, "providers")
    unknown = sorted(set(table) - {provider.value for provider in ProviderId})
    if unknown:
        _fail("providers", f"unsupported provider(s): {', '.join(unknown)}")
    providers: list[ProviderConfig] = []
    for provider in ProviderId:
        provider_table = _table(table.get(provider.value, {}), f"providers.{provider}")
        _known(provider_table, {"enabled", "executable"}, f"providers.{provider}")
        executable = _string(
            provider_table.get("executable"),
            f"providers.{provider}.executable",
            maximum=4096,
            optional=True,
        )
        if executable is not None and "\x00" in executable:
            _fail(f"providers.{provider}.executable", "contains NUL")
        providers.append(
            ProviderConfig(
                provider=provider,
                enabled=_boolean(
                    provider_table.get("enabled", True),
                    f"providers.{provider}.enabled",
                ),
                executable=executable,
            )
        )
    return tuple(providers)


def _parse_remotes(raw: object) -> tuple[RemoteConfig, ...]:
    table = _table(raw, "remotes")
    if len(table) > MAX_REMOTES:
        _fail("remotes", f"contains more than {MAX_REMOTES} entries")
    remotes: list[RemoteConfig] = []
    for alias in sorted(table):
        if not _ALIAS_RE.fullmatch(alias):
            _fail(f"remotes.{alias}", "invalid remote alias")
        remote = _table(table[alias], f"remotes.{alias}")
        _known(remote, {"ssh_target", "display_name"}, f"remotes.{alias}")
        if "ssh_target" not in remote:
            _fail(f"remotes.{alias}.ssh_target", "is required")
        ssh_target = _string(remote["ssh_target"], f"remotes.{alias}.ssh_target")
        assert ssh_target is not None
        if ssh_target.startswith("-") or any(char.isspace() for char in ssh_target):
            _fail(
                f"remotes.{alias}.ssh_target",
                "must be one SSH target without options or whitespace",
            )
        display_name = _string(
            remote.get("display_name", alias),
            f"remotes.{alias}.display_name",
            maximum=256,
        )
        assert display_name is not None
        remotes.append(RemoteConfig(alias, ssh_target, display_name))
    return tuple(remotes)


def _parse_context_sources(raw: object, path: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        _fail(path, "must be an array of project-relative paths")
    if len(raw) > _MAX_CONTEXT_SOURCES:
        _fail(path, f"must contain at most {_MAX_CONTEXT_SOURCES} paths")
    sources: list[str] = []
    for index, source in enumerate(raw):
        value = _string(source, f"{path}[{index}]")
        assert value is not None
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts or value == ".":
            _fail(f"{path}[{index}]", "must be a project-relative path")
        normalized = candidate.as_posix()
        if normalized not in sources:
            sources.append(normalized)
    return tuple(sources)


def _parse_projects(raw: object, host_id: HostId) -> ProjectCatalog:
    table = _table(raw, "projects")
    projects: list[Project] = []
    repositories: list[Repository] = []
    memberships: list[ProjectRepository] = []
    checkouts: list[Checkout] = []
    for project_key in sorted(table):
        project_path = f'projects."{project_key}"'
        project_id = _uuid(project_key, project_path, ProjectId)
        project = _table(table[project_key], project_path)
        _known(
            project,
            {
                "name",
                "aliases",
                "default_provider",
                "default_transport",
                "repositories",
            },
            project_path,
        )
        if "name" not in project:
            _fail(f"{project_path}.name", "is required")
        name = _string(project["name"], f"{project_path}.name", maximum=256)
        assert name is not None
        aliases_raw = project.get("aliases", [])
        if not isinstance(aliases_raw, list):
            _fail(f"{project_path}.aliases", "must be an array of strings")
        parsed_aliases: list[str] = []
        for index, value in enumerate(aliases_raw):
            alias = _string(value, f"{project_path}.aliases[{index}]", maximum=128)
            assert alias is not None
            parsed_aliases.append(alias)
        aliases = tuple(parsed_aliases)
        try:
            project_record = Project(
                project_id=project_id,
                name=name,
                aliases=aliases,
                default_provider=(
                    _provider(
                        project["default_provider"],
                        f"{project_path}.default_provider",
                    )
                    if "default_provider" in project
                    else None
                ),
                default_transport=_transport(
                    project.get("default_transport", "tmux"),
                    f"{project_path}.default_transport",
                ),
            )
        except ValidationError as exc:
            raise ConfigError(f"{project_path}: {exc}") from exc
        projects.append(project_record)

        repositories_raw = project.get("repositories", [])
        if not isinstance(repositories_raw, list):
            _fail(f"{project_path}.repositories", "must be an array of tables")
        for repository_index, repository_raw in enumerate(repositories_raw):
            repository_path = f"{project_path}.repositories[{repository_index}]"
            repository = _table(repository_raw, repository_path)
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
                repository_path,
            )
            for required in ("repository_id", "name"):
                if required not in repository:
                    _fail(f"{repository_path}.{required}", "is required")
            repository_id = _uuid(
                repository["repository_id"],
                f"{repository_path}.repository_id",
                RepositoryId,
            )
            repository_name = _string(
                repository["name"], f"{repository_path}.name", maximum=256
            )
            assert repository_name is not None
            try:
                repository_record = Repository(
                    repository_id=repository_id,
                    name=repository_name,
                    kind=RepositoryKind(
                        _string(
                            repository.get("kind", "git"),
                            f"{repository_path}.kind",
                        )
                    ),
                    context_sources=_parse_context_sources(
                        repository.get("context_sources"),
                        f"{repository_path}.context_sources",
                    ),
                )
                membership = ProjectRepository(
                    project_id=project_id,
                    repository_id=repository_id,
                    is_primary=_boolean(
                        repository.get("is_primary", False),
                        f"{repository_path}.is_primary",
                    ),
                )
            except (ValueError, ValidationError) as exc:
                raise ConfigError(f"{repository_path}: {exc}") from exc
            repositories.append(repository_record)
            memberships.append(membership)

            checkouts_raw = repository.get("checkouts", [])
            if not isinstance(checkouts_raw, list):
                _fail(f"{repository_path}.checkouts", "must be an array of tables")
            for checkout_index, checkout_raw in enumerate(checkouts_raw):
                checkout_path = f"{repository_path}.checkouts[{checkout_index}]"
                checkout = _table(checkout_raw, checkout_path)
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
                    checkout_path,
                )
                for required in ("checkout_id", "path"):
                    if required not in checkout:
                        _fail(f"{checkout_path}.{required}", "is required")
                checkout_id = _uuid(
                    checkout["checkout_id"],
                    f"{checkout_path}.checkout_id",
                    CheckoutId,
                )
                local_path = _string(
                    checkout["path"], f"{checkout_path}.path", maximum=4096
                )
                assert local_path is not None
                default_kind = (
                    "directory"
                    if repository_record.kind is RepositoryKind.DIRECTORY
                    else "main"
                )
                try:
                    checkouts.append(
                        Checkout(
                            checkout_id=checkout_id,
                            repository_id=repository_id,
                            host_id=host_id,
                            path=Path(local_path),
                            kind=CheckoutKind(
                                _string(
                                    checkout.get("kind", default_kind),
                                    f"{checkout_path}.kind",
                                )
                            ),
                            display_name=_string(
                                checkout.get("display_name"),
                                f"{checkout_path}.display_name",
                                maximum=256,
                                optional=True,
                            ),
                            provider_override=(
                                _provider(
                                    checkout["provider_override"],
                                    f"{checkout_path}.provider_override",
                                )
                                if "provider_override" in checkout
                                else None
                            ),
                            transport_override=(
                                _transport(
                                    checkout["transport_override"],
                                    f"{checkout_path}.transport_override",
                                )
                                if "transport_override" in checkout
                                else None
                            ),
                            is_default=_boolean(
                                checkout.get("is_default", False),
                                f"{checkout_path}.is_default",
                            ),
                        )
                    )
                except (ValueError, ValidationError) as exc:
                    raise ConfigError(f"{checkout_path}: {exc}") from exc
    try:
        return ProjectCatalog(
            merge_projects(projects),
            merge_repositories(repositories),
            merge_project_repositories(memberships),
            merge_checkouts(checkouts),
        )
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc


def _parse_defaults(raw: object) -> DefaultsConfig:
    table = _table(raw, "defaults")
    _known(
        table,
        {
            "transport",
            "refresh_interval_seconds",
            "staleness_interval_seconds",
            "recent_parked_limit",
            "working_directory",
        },
        "defaults",
    )
    working_directory = _string(
        table.get("working_directory", WorkingDirectoryPolicy.PROJECT_DEFAULT),
        "defaults.working_directory",
    )
    assert working_directory is not None
    try:
        working_directory_policy = WorkingDirectoryPolicy(working_directory)
    except ValueError:
        _fail(
            "defaults.working_directory",
            "must be project_default, current, or require_explicit",
        )
    refresh = _integer(
        table.get("refresh_interval_seconds", 30),
        "defaults.refresh_interval_seconds",
        minimum=1,
    )
    staleness = _integer(
        table.get("staleness_interval_seconds", 120),
        "defaults.staleness_interval_seconds",
        minimum=1,
    )
    if staleness < refresh:
        _fail(
            "defaults.staleness_interval_seconds",
            "must be at least refresh_interval_seconds",
        )
    return DefaultsConfig(
        transport=_transport(table.get("transport", "tmux"), "defaults.transport"),
        refresh_interval_seconds=refresh,
        staleness_interval_seconds=staleness,
        recent_parked_limit=_integer(
            table.get("recent_parked_limit", 100),
            "defaults.recent_parked_limit",
            maximum=100_000,
        ),
        working_directory=working_directory_policy,
    )


def _parse_tmux(raw: object) -> TmuxConfig:
    table = _table(raw, "tmux")
    _known(table, {"naming_prefix", "launch_timeout_seconds"}, "tmux")
    naming_prefix = _string(
        table.get("naming_prefix", "as"), "tmux.naming_prefix", maximum=32
    )
    assert naming_prefix is not None
    if not _TMUX_PREFIX_RE.fullmatch(naming_prefix):
        _fail("tmux.naming_prefix", "must be a safe tmux name prefix")
    return TmuxConfig(
        naming_prefix=naming_prefix,
        launch_timeout_seconds=_integer(
            table.get("launch_timeout_seconds", 30),
            "tmux.launch_timeout_seconds",
            minimum=1,
            maximum=3600,
        ),
    )


def _parse_hooks(raw: object) -> HooksConfig:
    table = _table(raw, "hooks")
    _known(table, {"timeout_seconds", "latency_budget_ms"}, "hooks")
    return HooksConfig(
        timeout_seconds=_integer(
            table.get("timeout_seconds", 1),
            "hooks.timeout_seconds",
            minimum=1,
            maximum=60,
        ),
        latency_budget_ms=_integer(
            table.get("latency_budget_ms", _DEFAULT_HOOK_LATENCY_BUDGET_MS),
            "hooks.latency_budget_ms",
            minimum=1,
            maximum=60_000,
        ),
    )


def _parse_memory(raw: object) -> MemoryConfig:
    table = _table(raw, "memory")
    _known(table, {"enabled", "command", "tool", "timeout_seconds"}, "memory")
    command_raw = table.get("command", [])
    if not isinstance(command_raw, list):
        _fail("memory.command", "must be an array of command arguments")
    if len(command_raw) > 32:
        _fail("memory.command", "must contain at most 32 arguments")
    command: list[str] = []
    for index, raw_argument in enumerate(command_raw):
        argument = _string(raw_argument, f"memory.command[{index}]", maximum=4096)
        assert argument is not None
        if "\x00" in argument:
            _fail(f"memory.command[{index}]", "contains NUL")
        command.append(argument)
    enabled = _boolean(table.get("enabled", False), "memory.enabled")
    if enabled and not command:
        _fail("memory.command", "is required when memory.enabled is true")
    if command and not Path(command[0]).is_absolute():
        _fail("memory.command[0]", "must be an absolute executable path")
    tool = _string(table.get("tool", "search"), "memory.tool", maximum=128)
    assert tool is not None
    if _MEMORY_TOOL_RE.fullmatch(tool) is None:
        _fail("memory.tool", "must be a safe MCP tool name")
    return MemoryConfig(
        enabled=enabled,
        command=tuple(command),
        tool=tool,
        timeout_seconds=_integer(
            table.get("timeout_seconds", 5),
            "memory.timeout_seconds",
            minimum=1,
            maximum=30,
        ),
    )


def parse_config(data: bytes | str, *, host_id: HostId) -> SwitchboardConfig:
    """Parse and validate a complete host-local TOML document."""

    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, bytes):
        raise ConfigError("configuration must be bytes or text")
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"invalid TOML value: {exc}") from exc
    if document:
        version = document.get("config_version")
        if version != 2:
            if "projects" in document:
                raise ConfigError(
                    "configuration: config_migration_required; run "
                    "'swbctl config migrate-v2 --print'"
                )
            raise ConfigError("configuration.config_version: must be 2")
    _known(
        document,
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
    if not isinstance(host_id, HostId):
        try:
            host_id = HostId(host_id)
        except ValidationError as exc:
            raise ConfigError(f"host_id: {exc}") from exc
    return SwitchboardConfig(
        host=_parse_host(document.get("host", {}), host_id),
        providers=_parse_providers(document.get("providers", {})),
        remotes=_parse_remotes(document.get("remotes", {})),
        catalog=_parse_projects(document.get("projects", {}), host_id),
        defaults=_parse_defaults(document.get("defaults", {})),
        tmux=_parse_tmux(document.get("tmux", {})),
        hooks=_parse_hooks(document.get("hooks", {})),
        memory=_parse_memory(document.get("memory", {})),
    )


def load_config(
    path: str | Path | None = None,
    *,
    host_id: HostId | None = None,
) -> SwitchboardConfig:
    """Load host-local config, defaulting only when its implicit path is absent."""

    source = Path(path) if path is not None else config_path()
    try:
        data = source.read_bytes()
    except FileNotFoundError as exc:
        if path is not None:
            raise ConfigError(f"cannot read configuration at {source}: {exc}") from exc
        data = b""
    except OSError as exc:
        raise ConfigError(f"cannot read configuration at {source}: {exc}") from exc
    return parse_config(data, host_id=host_id or load_or_create_host_id())


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: tuple[str, ...] | list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _legacy_projects(raw: object, host_id: HostId) -> ProjectCatalog:
    table = _table(raw, "projects")
    projects: list[Project] = []
    repositories: list[Repository] = []
    memberships: list[ProjectRepository] = []
    checkouts: list[Checkout] = []
    for project_key in sorted(table):
        path = f'projects."{project_key}"'
        project_id = _uuid(project_key, path, ProjectId)
        value = _table(table[project_key], path)
        _known(
            value,
            {
                "name",
                "aliases",
                "default_provider",
                "default_transport",
                "context_sources",
                "locations",
            },
            path,
        )
        if "name" not in value:
            _fail(f"{path}.name", "is required")
        name = _string(value["name"], f"{path}.name", maximum=256)
        assert name is not None
        aliases_raw = value.get("aliases", [])
        if not isinstance(aliases_raw, list):
            _fail(f"{path}.aliases", "must be an array of strings")
        aliases = tuple(
            str(_string(alias, f"{path}.aliases[{index}]", maximum=128))
            for index, alias in enumerate(aliases_raw)
        )
        try:
            project = Project(
                project_id=project_id,
                name=name,
                aliases=aliases,
                default_provider=(
                    _provider(value["default_provider"], f"{path}.default_provider")
                    if "default_provider" in value
                    else None
                ),
                default_transport=_transport(
                    value.get("default_transport", "tmux"),
                    f"{path}.default_transport",
                ),
            )
        except ValidationError as error:
            raise ConfigError(f"{path}: {error}") from error
        projects.append(project)
        locations_raw = value.get("locations", [])
        if not isinstance(locations_raw, list):
            _fail(f"{path}.locations", "must be an array of tables")
        parsed_locations: list[dict[str, object]] = []
        observations = []
        directory_location = False
        for index, raw_location in enumerate(locations_raw):
            location_path = f"{path}.locations[{index}]"
            location = _table(raw_location, location_path)
            _known(
                location,
                {
                    "location_id",
                    "path",
                    "display_name",
                    "repository_identity",
                    "provider_override",
                    "transport_override",
                    "is_default",
                },
                location_path,
            )
            for required in ("location_id", "path"):
                if required not in location:
                    _fail(f"{location_path}.{required}", "is required")
            checkout_id = _uuid(
                location["location_id"],
                f"{location_path}.location_id",
                CheckoutId,
            )
            raw_local_path = _string(
                location["path"], f"{location_path}.path", maximum=4096
            )
            assert raw_local_path is not None
            local_path = Path(raw_local_path).resolve(strict=False)
            if not local_path.is_dir():
                _fail(
                    location_path, "configured location is not an available directory"
                )
            try:
                observation = probe_git_repository(local_path)
            except RepositoryDiscoveryError as error:
                if error.code != "git_probe_failed":
                    raise ConfigError(f"{location_path}: {error}") from error
                directory_location = True
                observation = None
            if observation is not None:
                exact = next(
                    (item for item in observation.checkouts if item.path == local_path),
                    None,
                )
                if exact is None:
                    _fail(
                        location_path,
                        "path must be the exact Git worktree root before migration",
                    )
                observations.append(observation)
            parsed_locations.append(
                {
                    "checkout_id": checkout_id,
                    "path": local_path,
                    "display_name": _string(
                        location.get("display_name"),
                        f"{location_path}.display_name",
                        maximum=256,
                        optional=True,
                    ),
                    "provider_override": (
                        _provider(
                            location["provider_override"],
                            f"{location_path}.provider_override",
                        )
                        if "provider_override" in location
                        else None
                    ),
                    "transport_override": (
                        _transport(
                            location["transport_override"],
                            f"{location_path}.transport_override",
                        )
                        if "transport_override" in location
                        else None
                    ),
                    "is_default": _boolean(
                        location.get("is_default", False),
                        f"{location_path}.is_default",
                    ),
                }
            )
        if directory_location and (observations or len(parsed_locations) > 1):
            _fail(
                path,
                "legacy locations do not prove one repository; split them manually",
            )
        common_dirs = {observation.git_common_dir for observation in observations}
        if len(common_dirs) > 1:
            _fail(
                path,
                "legacy locations belong to different Git stores; split them manually",
            )
        repository_kind = (
            RepositoryKind.DIRECTORY if directory_location else RepositoryKind.GIT
        )
        repository = Repository(
            repository_id=RepositoryId(str(project_id)),
            name=name,
            kind=repository_kind,
            context_sources=_parse_context_sources(
                value.get("context_sources"), f"{path}.context_sources"
            ),
        )
        repositories.append(repository)
        memberships.append(
            ProjectRepository(project_id, repository.repository_id, True)
        )
        for location in parsed_locations:
            local_path = location["path"]
            assert isinstance(local_path, Path)
            kind = CheckoutKind.DIRECTORY
            if repository_kind is RepositoryKind.GIT:
                evidence = next(
                    item
                    for observation in observations
                    for item in observation.checkouts
                    if item.path == local_path
                )
                kind = CheckoutKind(evidence.kind)
            checkouts.append(
                Checkout(
                    checkout_id=location["checkout_id"],
                    repository_id=repository.repository_id,
                    host_id=host_id,
                    path=local_path,
                    kind=kind,
                    display_name=location["display_name"],
                    provider_override=location["provider_override"],
                    transport_override=location["transport_override"],
                    is_default=bool(location["is_default"]),
                )
            )
    try:
        return ProjectCatalog(
            merge_projects(projects),
            merge_repositories(repositories),
            merge_project_repositories(memberships),
            merge_checkouts(checkouts),
        )
    except ValidationError as error:
        raise ConfigError(str(error)) from error


def render_config(config: SwitchboardConfig) -> str:
    """Return the canonical, fully validated configuration-v2 representation."""

    lines = [
        "config_version = 2",
        "",
        "[host]",
        f"display_name = {_toml_string(config.host.display_name)}",
    ]
    for provider in config.providers:
        lines.extend(("", f"[providers.{provider.provider.value}]"))
        lines.append(f"enabled = {'true' if provider.enabled else 'false'}")
        if provider.executable is not None:
            lines.append(f"executable = {_toml_string(provider.executable)}")
    for remote in config.remotes:
        lines.extend(
            (
                "",
                f"[remotes.{remote.alias}]",
                f"ssh_target = {_toml_string(remote.ssh_target)}",
                f"display_name = {_toml_string(remote.display_name)}",
            )
        )
    repositories = {
        repository.repository_id: repository for repository in config.repositories
    }
    memberships = sorted(
        config.project_repositories,
        key=lambda item: (
            str(item.project_id),
            not item.is_primary,
            str(item.repository_id),
        ),
    )
    for project in config.projects:
        project_path = f'projects."{project.project_id}"'
        lines.extend(("", f"[{project_path}]", f"name = {_toml_string(project.name)}"))
        if project.aliases:
            lines.append(f"aliases = {_toml_array(list(project.aliases))}")
        if project.default_provider is not None:
            lines.append(
                f"default_provider = {_toml_string(project.default_provider.value)}"
            )
        lines.append(
            f"default_transport = {_toml_string(project.default_transport.value)}"
        )
        for membership in (
            item for item in memberships if item.project_id == project.project_id
        ):
            repository = repositories[membership.repository_id]
            lines.extend(
                (
                    "",
                    f"[[{project_path}.repositories]]",
                    f"repository_id = {_toml_string(str(repository.repository_id))}",
                    f"name = {_toml_string(repository.name)}",
                    f"kind = {_toml_string(repository.kind.value)}",
                    f"is_primary = {'true' if membership.is_primary else 'false'}",
                    "context_sources = "
                    f"{_toml_array(list(repository.context_sources))}",
                )
            )
            for checkout in sorted(
                (
                    item
                    for item in config.checkouts
                    if item.repository_id == repository.repository_id
                ),
                key=lambda item: str(item.checkout_id),
            ):
                lines.extend(
                    (
                        "",
                        f"[[{project_path}.repositories.checkouts]]",
                        f"checkout_id = {_toml_string(str(checkout.checkout_id))}",
                        f"path = {_toml_string(str(checkout.path))}",
                        f"kind = {_toml_string(checkout.kind.value)}",
                    )
                )
                if checkout.display_name is not None:
                    lines.append(
                        f"display_name = {_toml_string(checkout.display_name)}"
                    )
                if checkout.provider_override is not None:
                    lines.append(
                        "provider_override = "
                        f"{_toml_string(checkout.provider_override.value)}"
                    )
                if checkout.transport_override is not None:
                    lines.append(
                        "transport_override = "
                        f"{_toml_string(checkout.transport_override.value)}"
                    )
                lines.append(
                    f"is_default = {'true' if checkout.is_default else 'false'}"
                )
    defaults = config.defaults
    lines.extend(
        (
            "",
            "[defaults]",
            f"transport = {_toml_string(defaults.transport.value)}",
            f"refresh_interval_seconds = {defaults.refresh_interval_seconds}",
            f"staleness_interval_seconds = {defaults.staleness_interval_seconds}",
            f"recent_parked_limit = {defaults.recent_parked_limit}",
            f"working_directory = {_toml_string(defaults.working_directory.value)}",
            "",
            "[tmux]",
            f"naming_prefix = {_toml_string(config.tmux.naming_prefix)}",
            f"launch_timeout_seconds = {config.tmux.launch_timeout_seconds}",
            "",
            "[hooks]",
            f"timeout_seconds = {config.hooks.timeout_seconds}",
            f"latency_budget_ms = {config.hooks.latency_budget_ms}",
            "",
            "[memory]",
            f"enabled = {'true' if config.memory.enabled else 'false'}",
            f"command = {_toml_array(list(config.memory.command))}",
            f"tool = {_toml_string(config.memory.tool)}",
            f"timeout_seconds = {config.memory.timeout_seconds}",
        )
    )
    return "\n".join(lines) + "\n"


def migrate_legacy_config(data: bytes | str, *, host_id: HostId) -> str:
    """Validate one v1 document and return a non-mutating canonical v2 TOML form."""

    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"invalid legacy TOML: {error}") from error
    if document.get("config_version") not in {None, 1}:
        raise ConfigError("legacy configuration version must be absent or 1")
    _known(
        document,
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
    base = SwitchboardConfig(
        host=_parse_host(document.get("host", {}), host_id),
        providers=_parse_providers(document.get("providers", {})),
        remotes=_parse_remotes(document.get("remotes", {})),
        catalog=ProjectCatalog((), (), (), ()),
        defaults=_parse_defaults(document.get("defaults", {})),
        tmux=_parse_tmux(document.get("tmux", {})),
        hooks=_parse_hooks(document.get("hooks", {})),
        memory=_parse_memory(document.get("memory", {})),
    )
    catalog = _legacy_projects(document.get("projects", {}), host_id)
    migrated = SwitchboardConfig(
        host=base.host,
        providers=base.providers,
        remotes=base.remotes,
        catalog=catalog,
        defaults=base.defaults,
        tmux=base.tmux,
        hooks=base.hooks,
        memory=base.memory,
    )
    rendered = render_config(migrated)
    parse_config(rendered, host_id=host_id)
    return rendered.rstrip("\n")


def merge_project_catalogs(
    configs: list[SwitchboardConfig] | tuple[SwitchboardConfig, ...],
) -> ProjectCatalog:
    """Merge remote host declarations using stable IDs and conflict rules."""

    try:
        return ProjectCatalog(
            projects=merge_projects(
                [project for config in configs for project in config.projects]
            ),
            repositories=merge_repositories(
                [repository for config in configs for repository in config.repositories]
            ),
            project_repositories=merge_project_repositories(
                [
                    membership
                    for config in configs
                    for membership in config.project_repositories
                ]
            ),
            checkouts=merge_checkouts(
                [checkout for config in configs for checkout in config.checkouts]
            ),
        )
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
