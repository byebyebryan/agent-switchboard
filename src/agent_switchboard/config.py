"""Strict standard-library TOML configuration loading."""

from __future__ import annotations

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
    HostId,
    LocationId,
    Project,
    ProjectId,
    ProjectLocation,
    ProviderId,
    Transport,
    ValidationError,
    merge_locations,
    merge_projects,
)
from .paths import config_path, load_or_create_host_id

_MAX_CONTEXT_SOURCES = 32
_DEFAULT_HOOK_LATENCY_BUDGET_MS = 125
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
    locations: tuple[ProjectLocation, ...]


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
    def locations(self) -> tuple[ProjectLocation, ...]:
        return self.catalog.locations


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


def _uuid[T: HostId | ProjectId | LocationId](
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
    locations: list[ProjectLocation] = []
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
                "context_sources",
                "locations",
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
        context_sources = _parse_context_sources(
            project.get("context_sources"), f"{project_path}.context_sources"
        )
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
                context_sources=context_sources,
            )
        except ValidationError as exc:
            raise ConfigError(f"{project_path}: {exc}") from exc
        projects.append(project_record)

        locations_raw = project.get("locations", [])
        if not isinstance(locations_raw, list):
            _fail(f"{project_path}.locations", "must be an array of tables")
        for index, location_raw in enumerate(locations_raw):
            location_path = f"{project_path}.locations[{index}]"
            location = _table(location_raw, location_path)
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
            location_id = _uuid(
                location["location_id"],
                f"{location_path}.location_id",
                LocationId,
            )
            local_path = _string(
                location["path"], f"{location_path}.path", maximum=4096
            )
            assert local_path is not None
            try:
                locations.append(
                    ProjectLocation(
                        location_id=location_id,
                        project_id=project_id,
                        host_id=host_id,
                        path=Path(local_path),
                        display_name=_string(
                            location.get("display_name"),
                            f"{location_path}.display_name",
                            maximum=256,
                            optional=True,
                        ),
                        repository_identity=_string(
                            location.get("repository_identity"),
                            f"{location_path}.repository_identity",
                            maximum=1024,
                            optional=True,
                        ),
                        provider_override=(
                            _provider(
                                location["provider_override"],
                                f"{location_path}.provider_override",
                            )
                            if "provider_override" in location
                            else None
                        ),
                        transport_override=(
                            _transport(
                                location["transport_override"],
                                f"{location_path}.transport_override",
                            )
                            if "transport_override" in location
                            else None
                        ),
                        is_default=_boolean(
                            location.get("is_default", False),
                            f"{location_path}.is_default",
                        ),
                    )
                )
            except ValidationError as exc:
                raise ConfigError(f"{location_path}: {exc}") from exc
    try:
        return ProjectCatalog(merge_projects(projects), merge_locations(locations))
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
    _known(
        document,
        {
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


def merge_project_catalogs(
    configs: list[SwitchboardConfig] | tuple[SwitchboardConfig, ...],
) -> ProjectCatalog:
    """Merge remote host declarations using stable IDs and conflict rules."""

    try:
        return ProjectCatalog(
            projects=merge_projects(
                [project for config in configs for project in config.projects]
            ),
            locations=merge_locations(
                [location for config in configs for location in config.locations]
            ),
        )
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
