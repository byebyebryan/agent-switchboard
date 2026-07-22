"""Strict, deterministic Config v3 parsing for the private replacement."""

from __future__ import annotations

import json
import re
import tomllib
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never

from .domain import (
    Checkout,
    CheckoutId,
    CheckoutKind,
    CompleteReturnPolicy,
    ControlTurnPolicy,
    GenerationId,
    HostId,
    Project,
    ProjectId,
    ProjectRepository,
    ProviderId,
    Repository,
    RepositoryId,
    RepositoryKind,
    TaskPushPolicy,
    Transport,
    ValidationError,
    ViewMode,
)

CONFIG_VERSION = 3
MAX_REMOTES = 32
MAX_CONTEXT_SOURCES = 32
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TMUX_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
_MEMORY_TOOL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class ConfigError(ValidationError):
    """Config v3 syntax or semantics are invalid."""


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
class ProjectCatalog:
    projects: tuple[Project, ...]
    repositories: tuple[Repository, ...]
    project_repositories: tuple[ProjectRepository, ...]
    checkouts: tuple[Checkout, ...]


@dataclass(frozen=True, slots=True)
class DefaultsConfig:
    transport: Transport = Transport.TMUX
    refresh_interval_seconds: int = 30
    staleness_interval_seconds: int = 120


@dataclass(frozen=True, slots=True)
class ViewsConfig:
    cli_default_mode: ViewMode = ViewMode.DIRECT
    desktop_default_mode: ViewMode = ViewMode.NAVIGATOR


@dataclass(frozen=True, slots=True)
class AutomationConfig:
    task_push: TaskPushPolicy = TaskPushPolicy.CONSERVATIVE
    complete_return: CompleteReturnPolicy = CompleteReturnPolicy.SYNTHESIZE
    initial_max_depth: int = 1


@dataclass(frozen=True, slots=True)
class ControlTurnsConfig:
    transport: ControlTurnPolicy = ControlTurnPolicy.LIVE_FIRST
    watchdog_timeout_seconds: int = 5


@dataclass(frozen=True, slots=True)
class TmuxConfig:
    naming_prefix: str = "as"
    launch_timeout_seconds: int = 30


@dataclass(frozen=True, slots=True)
class HooksConfig:
    timeout_seconds: int = 1
    latency_budget_ms: int = 250


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    enabled: bool = False
    command: tuple[str, ...] = ()
    tool: str = "search"
    timeout_seconds: int = 5


@dataclass(frozen=True, slots=True)
class SwitchboardConfig:
    generation_id: GenerationId
    host: HostConfig
    providers: tuple[ProviderConfig, ...]
    remotes: tuple[RemoteConfig, ...]
    catalog: ProjectCatalog
    defaults: DefaultsConfig
    views: ViewsConfig
    automation: AutomationConfig
    control_turns: ControlTurnsConfig
    tmux: TmuxConfig
    hooks: HooksConfig
    memory: MemoryConfig

    @property
    def projects(self) -> tuple[Project, ...]:
        return self.catalog.projects

    @property
    def repositories(self) -> tuple[Repository, ...]:
        return self.catalog.repositories

    @property
    def project_repositories(self) -> tuple[ProjectRepository, ...]:
        return self.catalog.project_repositories

    @property
    def checkouts(self) -> tuple[Checkout, ...]:
        return self.catalog.checkouts


def _fail(path: str, message: str) -> Never:
    raise ConfigError(f"{path}: {message}")


def _table(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        _fail(path, "must be a TOML table")
    return value


def _known(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail(path, f"unknown key(s): {', '.join(unknown)}")


def _string(
    value: object,
    path: str,
    *,
    maximum: int = 1_024,
    optional: bool = False,
) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        _fail(path, "must be a string")
    if any(unicodedata.category(character) == "Cc" for character in value):
        _fail(path, "contains a control character")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        _fail(path, "must not be empty")
    if len(normalized) > maximum or len(normalized.encode("utf-8")) > maximum:
        _fail(path, f"must be at most {maximum} bytes")
    return normalized


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, "must be boolean")
    return value


def _integer(value: object, path: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "must be an integer")
    if not minimum <= value <= maximum:
        _fail(path, f"must be between {minimum} and {maximum}")
    return value


def _enum[T](value: object, path: str, enum_type: type[T]) -> T:
    raw = _string(value, path, maximum=64)
    try:
        return enum_type(raw)  # type: ignore[call-arg,return-value]
    except ValueError:
        choices = ", ".join(repr(item.value) for item in enum_type)  # type: ignore[attr-defined]
        _fail(path, f"must be one of {choices}")


def _uuid[T: GenerationId | HostId | ProjectId | RepositoryId | CheckoutId](
    value: object,
    path: str,
    value_type: type[T],
) -> T:
    raw = _string(value, path, maximum=36)
    try:
        return value_type(raw)
    except ValidationError as error:
        raise ConfigError(f"{path}: {error}") from error


def _parse_host(raw: object) -> HostConfig:
    table = _table(raw, "host")
    _known(table, {"host_id", "display_name"}, "host")
    if "host_id" not in table or "display_name" not in table:
        _fail("host", "host_id and display_name are required")
    host_id = _uuid(table["host_id"], "host.host_id", HostId)
    display_name = _string(table["display_name"], "host.display_name", maximum=256)
    assert display_name is not None
    return HostConfig(host_id, display_name)


def _parse_providers(raw: object) -> tuple[ProviderConfig, ...]:
    table = _table(raw, "providers")
    unknown = sorted(set(table) - {provider.value for provider in ProviderId})
    if unknown:
        _fail("providers", f"unsupported provider(s): {', '.join(unknown)}")
    result: list[ProviderConfig] = []
    for provider in ProviderId:
        provider_table = _table(table.get(provider.value, {}), f"providers.{provider}")
        _known(provider_table, {"enabled", "executable"}, f"providers.{provider}")
        executable = _string(
            provider_table.get("executable"),
            f"providers.{provider}.executable",
            maximum=4_096,
            optional=True,
        )
        if executable is not None and "\x00" in executable:
            _fail(f"providers.{provider}.executable", "contains NUL")
        result.append(
            ProviderConfig(
                provider,
                _boolean(
                    provider_table.get("enabled", True),
                    f"providers.{provider}.enabled",
                ),
                executable,
            )
        )
    return tuple(result)


def _parse_remotes(raw: object) -> tuple[RemoteConfig, ...]:
    table = _table(raw, "remotes")
    if len(table) > MAX_REMOTES:
        _fail("remotes", f"contains more than {MAX_REMOTES} entries")
    result: list[RemoteConfig] = []
    for alias in sorted(table):
        if _ALIAS_RE.fullmatch(alias) is None:
            _fail(f"remotes.{alias}", "invalid alias")
        item = _table(table[alias], f"remotes.{alias}")
        _known(item, {"ssh_target", "display_name"}, f"remotes.{alias}")
        if "ssh_target" not in item:
            _fail(f"remotes.{alias}.ssh_target", "is required")
        target = _string(item["ssh_target"], f"remotes.{alias}.ssh_target")
        assert target is not None
        if target.startswith("-") or any(character.isspace() for character in target):
            _fail(f"remotes.{alias}.ssh_target", "must be one SSH target")
        display = _string(
            item.get("display_name", alias),
            f"remotes.{alias}.display_name",
            maximum=256,
        )
        assert display is not None
        result.append(RemoteConfig(alias, target, display))
    return tuple(result)


def _parse_context_sources(raw: object, path: str) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        _fail(path, "must be an array")
    if len(raw) > MAX_CONTEXT_SOURCES:
        _fail(path, f"must contain at most {MAX_CONTEXT_SOURCES} entries")
    result: list[str] = []
    for index, item in enumerate(raw):
        source = _string(item, f"{path}[{index}]", maximum=1_024)
        assert source is not None
        candidate = Path(source)
        if candidate.is_absolute() or ".." in candidate.parts or source == ".":
            _fail(f"{path}[{index}]", "must be project-relative")
        normalized = candidate.as_posix()
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _parse_catalog(raw: object, host_id: HostId) -> ProjectCatalog:
    table = _table(raw, "projects")
    projects: list[Project] = []
    repositories: dict[RepositoryId, Repository] = {}
    memberships: list[ProjectRepository] = []
    checkouts: dict[CheckoutId, Checkout] = {}
    for project_key in sorted(table):
        project_path = f'projects."{project_key}"'
        project_id = _uuid(project_key, project_path, ProjectId)
        item = _table(table[project_key], project_path)
        _known(
            item,
            {
                "name",
                "aliases",
                "default_provider",
                "default_transport",
                "task_push",
                "complete_return",
                "repositories",
            },
            project_path,
        )
        if "name" not in item:
            _fail(f"{project_path}.name", "is required")
        aliases_raw = item.get("aliases", [])
        if not isinstance(aliases_raw, list):
            _fail(f"{project_path}.aliases", "must be an array")
        aliases = tuple(
            " ".join(
                (
                    _string(value, f"{project_path}.aliases[{index}]", maximum=128)
                    or ""
                ).split()
            )
            for index, value in enumerate(aliases_raw)
        )
        name = _string(item["name"], f"{project_path}.name", maximum=256)
        assert name is not None
        project = Project(
            project_id,
            name,
            aliases,
            (
                _enum(
                    item["default_provider"],
                    f"{project_path}.default_provider",
                    ProviderId,
                )
                if "default_provider" in item
                else None
            ),
            _enum(
                item.get("default_transport", "tmux"),
                f"{project_path}.default_transport",
                Transport,
            ),
            (
                _enum(item["task_push"], f"{project_path}.task_push", TaskPushPolicy)
                if "task_push" in item
                else None
            ),
            (
                _enum(
                    item["complete_return"],
                    f"{project_path}.complete_return",
                    CompleteReturnPolicy,
                )
                if "complete_return" in item
                else None
            ),
        )
        projects.append(project)
        repositories_raw = item.get("repositories", [])
        if not isinstance(repositories_raw, list):
            _fail(f"{project_path}.repositories", "must be an array of tables")
        primary_count = 0
        for repository_index, repository_raw in enumerate(repositories_raw):
            repository_path = f"{project_path}.repositories[{repository_index}]"
            repository_table = _table(repository_raw, repository_path)
            _known(
                repository_table,
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
                if required not in repository_table:
                    _fail(f"{repository_path}.{required}", "is required")
            repository_id = _uuid(
                repository_table["repository_id"],
                f"{repository_path}.repository_id",
                RepositoryId,
            )
            repository_name = _string(
                repository_table["name"],
                f"{repository_path}.name",
                maximum=256,
            )
            assert repository_name is not None
            repository = Repository(
                repository_id,
                repository_name,
                _enum(
                    repository_table.get("kind", "git"),
                    f"{repository_path}.kind",
                    RepositoryKind,
                ),
                _parse_context_sources(
                    repository_table.get("context_sources", []),
                    f"{repository_path}.context_sources",
                ),
            )
            existing = repositories.get(repository_id)
            if existing is not None and existing != repository:
                _fail(repository_path, "stable repository ID has conflicting fields")
            repositories[repository_id] = repository
            is_primary = _boolean(
                repository_table.get("is_primary", False),
                f"{repository_path}.is_primary",
            )
            primary_count += int(is_primary)
            memberships.append(ProjectRepository(project_id, repository_id, is_primary))
            checkouts_raw = repository_table.get("checkouts", [])
            if not isinstance(checkouts_raw, list):
                _fail(f"{repository_path}.checkouts", "must be an array of tables")
            default_count = 0
            for checkout_index, checkout_raw in enumerate(checkouts_raw):
                checkout_path = f"{repository_path}.checkouts[{checkout_index}]"
                checkout_table = _table(checkout_raw, checkout_path)
                _known(
                    checkout_table,
                    {
                        "checkout_id",
                        "path",
                        "kind",
                        "display_name",
                        "provider_override",
                        "is_default",
                    },
                    checkout_path,
                )
                for required in ("checkout_id", "path"):
                    if required not in checkout_table:
                        _fail(f"{checkout_path}.{required}", "is required")
                checkout_id = _uuid(
                    checkout_table["checkout_id"],
                    f"{checkout_path}.checkout_id",
                    CheckoutId,
                )
                raw_path = _string(
                    checkout_table["path"],
                    f"{checkout_path}.path",
                    maximum=4_096,
                )
                assert raw_path is not None
                candidate = Path(raw_path).expanduser()
                if not candidate.is_absolute():
                    _fail(f"{checkout_path}.path", "must be absolute")
                is_default = _boolean(
                    checkout_table.get("is_default", False),
                    f"{checkout_path}.is_default",
                )
                default_count += int(is_default)
                checkout = Checkout(
                    checkout_id,
                    repository_id,
                    host_id,
                    candidate,
                    _enum(
                        checkout_table.get("kind", "main"),
                        f"{checkout_path}.kind",
                        CheckoutKind,
                    ),
                    _string(
                        checkout_table.get("display_name"),
                        f"{checkout_path}.display_name",
                        maximum=256,
                        optional=True,
                    ),
                    (
                        _enum(
                            checkout_table["provider_override"],
                            f"{checkout_path}.provider_override",
                            ProviderId,
                        )
                        if "provider_override" in checkout_table
                        else None
                    ),
                    is_default,
                )
                existing_checkout = checkouts.get(checkout_id)
                if existing_checkout is not None and existing_checkout != checkout:
                    _fail(checkout_path, "stable checkout ID has conflicting fields")
                checkouts[checkout_id] = checkout
            if default_count > 1:
                _fail(repository_path, "has more than one default checkout")
        if repositories_raw and primary_count != 1:
            _fail(project_path, "must have exactly one primary repository")
    paths = [str(checkout.path) for checkout in checkouts.values()]
    if len(paths) != len(set(paths)):
        _fail("projects", "checkout paths must be unique on the host")
    return ProjectCatalog(
        tuple(sorted(projects, key=lambda value: str(value.project_id))),
        tuple(
            sorted(repositories.values(), key=lambda value: str(value.repository_id))
        ),
        tuple(
            sorted(
                memberships,
                key=lambda value: (str(value.project_id), str(value.repository_id)),
            )
        ),
        tuple(sorted(checkouts.values(), key=lambda value: str(value.checkout_id))),
    )


def _parse_defaults(raw: object) -> DefaultsConfig:
    table = _table(raw, "defaults")
    _known(
        table,
        {"transport", "refresh_interval_seconds", "staleness_interval_seconds"},
        "defaults",
    )
    return DefaultsConfig(
        _enum(table.get("transport", "tmux"), "defaults.transport", Transport),
        _integer(
            table.get("refresh_interval_seconds", 30),
            "defaults.refresh_interval_seconds",
            minimum=1,
            maximum=86_400,
        ),
        _integer(
            table.get("staleness_interval_seconds", 120),
            "defaults.staleness_interval_seconds",
            minimum=1,
            maximum=604_800,
        ),
    )


def _parse_views(raw: object) -> ViewsConfig:
    table = _table(raw, "views")
    _known(table, {"cli_default_mode", "desktop_default_mode"}, "views")
    return ViewsConfig(
        _enum(
            table.get("cli_default_mode", "direct"), "views.cli_default_mode", ViewMode
        ),
        _enum(
            table.get("desktop_default_mode", "navigator"),
            "views.desktop_default_mode",
            ViewMode,
        ),
    )


def _parse_automation(raw: object) -> AutomationConfig:
    table = _table(raw, "automation")
    _known(table, {"task_push", "complete_return", "initial_max_depth"}, "automation")
    return AutomationConfig(
        _enum(
            table.get("task_push", "conservative"),
            "automation.task_push",
            TaskPushPolicy,
        ),
        _enum(
            table.get("complete_return", "synthesize"),
            "automation.complete_return",
            CompleteReturnPolicy,
        ),
        _integer(
            table.get("initial_max_depth", 1),
            "automation.initial_max_depth",
            minimum=1,
            maximum=1,
        ),
    )


def _parse_control_turns(raw: object) -> ControlTurnsConfig:
    table = _table(raw, "control_turns")
    _known(table, {"transport", "watchdog_timeout_seconds"}, "control_turns")
    return ControlTurnsConfig(
        _enum(
            table.get("transport", "live_first"),
            "control_turns.transport",
            ControlTurnPolicy,
        ),
        _integer(
            table.get("watchdog_timeout_seconds", 5),
            "control_turns.watchdog_timeout_seconds",
            minimum=1,
            maximum=60,
        ),
    )


def _parse_tmux(raw: object) -> TmuxConfig:
    table = _table(raw, "tmux")
    _known(table, {"naming_prefix", "launch_timeout_seconds"}, "tmux")
    prefix = _string(table.get("naming_prefix", "as"), "tmux.naming_prefix", maximum=32)
    assert prefix is not None
    if _TMUX_PREFIX_RE.fullmatch(prefix) is None:
        _fail("tmux.naming_prefix", "must be a safe tmux prefix")
    return TmuxConfig(
        prefix,
        _integer(
            table.get("launch_timeout_seconds", 30),
            "tmux.launch_timeout_seconds",
            minimum=1,
            maximum=300,
        ),
    )


def _parse_hooks(raw: object) -> HooksConfig:
    table = _table(raw, "hooks")
    _known(table, {"timeout_seconds", "latency_budget_ms"}, "hooks")
    return HooksConfig(
        _integer(
            table.get("timeout_seconds", 1),
            "hooks.timeout_seconds",
            minimum=1,
            maximum=30,
        ),
        _integer(
            table.get("latency_budget_ms", 250),
            "hooks.latency_budget_ms",
            minimum=10,
            maximum=5_000,
        ),
    )


def _parse_memory(raw: object) -> MemoryConfig:
    table = _table(raw, "memory")
    _known(table, {"enabled", "command", "tool", "timeout_seconds"}, "memory")
    enabled = _boolean(table.get("enabled", False), "memory.enabled")
    raw_command = table.get("command", [])
    if not isinstance(raw_command, list) or len(raw_command) > 32:
        _fail("memory.command", "must be an array with at most 32 tokens")
    command = tuple(
        _string(token, f"memory.command[{index}]", maximum=4_096) or ""
        for index, token in enumerate(raw_command)
    )
    if enabled and not command:
        _fail("memory.command", "is required when memory is enabled")
    tool = _string(table.get("tool", "search"), "memory.tool", maximum=128)
    assert tool is not None
    if _MEMORY_TOOL_RE.fullmatch(tool) is None:
        _fail("memory.tool", "must be a safe tool name")
    return MemoryConfig(
        enabled,
        command,
        tool,
        _integer(
            table.get("timeout_seconds", 5),
            "memory.timeout_seconds",
            minimum=1,
            maximum=30,
        ),
    )


def parse_config(data: bytes | str) -> SwitchboardConfig:
    """Parse a complete Config v3 document without filesystem side effects."""

    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, bytes):
        raise ConfigError("configuration must be bytes or text")
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as error:
        raise ConfigError(f"invalid TOML: {error}") from error
    _known(
        document,
        {
            "config_version",
            "generation_id",
            "host",
            "providers",
            "remotes",
            "projects",
            "defaults",
            "views",
            "automation",
            "control_turns",
            "tmux",
            "hooks",
            "memory",
        },
        "configuration",
    )
    if document.get("config_version") != CONFIG_VERSION:
        _fail("configuration.config_version", "must be 3")
    if "generation_id" not in document:
        _fail("configuration.generation_id", "is required")
    if "host" not in document:
        _fail("configuration.host", "is required")
    generation_id = _uuid(
        document["generation_id"],
        "configuration.generation_id",
        GenerationId,
    )
    host = _parse_host(document["host"])
    return SwitchboardConfig(
        generation_id,
        host,
        _parse_providers(document.get("providers", {})),
        _parse_remotes(document.get("remotes", {})),
        _parse_catalog(document.get("projects", {}), host.host_id),
        _parse_defaults(document.get("defaults", {})),
        _parse_views(document.get("views", {})),
        _parse_automation(document.get("automation", {})),
        _parse_control_turns(document.get("control_turns", {})),
        _parse_tmux(document.get("tmux", {})),
        _parse_hooks(document.get("hooks", {})),
        _parse_memory(document.get("memory", {})),
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: Sequence[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def render_config(config: SwitchboardConfig) -> str:
    """Return the canonical Config v3 TOML representation."""

    if not isinstance(config, SwitchboardConfig):
        raise ConfigError("config must be SwitchboardConfig")
    lines = [
        "config_version = 3",
        f"generation_id = {_toml_string(str(config.generation_id))}",
        "",
        "[host]",
        f"host_id = {_toml_string(str(config.host.host_id))}",
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
    checkouts = sorted(config.checkouts, key=lambda value: str(value.checkout_id))
    for project in config.projects:
        project_path = f'projects."{project.project_id}"'
        lines.extend(("", f"[{project_path}]", f"name = {_toml_string(project.name)}"))
        if project.aliases:
            lines.append(f"aliases = {_toml_array(project.aliases)}")
        if project.default_provider is not None:
            lines.append(
                f"default_provider = {_toml_string(project.default_provider.value)}"
            )
        lines.append(
            f"default_transport = {_toml_string(project.default_transport.value)}"
        )
        if project.task_push is not None:
            lines.append(f"task_push = {_toml_string(project.task_push.value)}")
        if project.complete_return is not None:
            lines.append(
                f"complete_return = {_toml_string(project.complete_return.value)}"
            )
        memberships = sorted(
            (
                membership
                for membership in config.project_repositories
                if membership.project_id == project.project_id
            ),
            key=lambda value: (not value.is_primary, str(value.repository_id)),
        )
        for membership in memberships:
            repository = repositories[membership.repository_id]
            lines.extend(
                (
                    "",
                    f"[[{project_path}.repositories]]",
                    f"repository_id = {_toml_string(str(repository.repository_id))}",
                    f"name = {_toml_string(repository.name)}",
                    f"kind = {_toml_string(repository.kind.value)}",
                    f"is_primary = {'true' if membership.is_primary else 'false'}",
                    f"context_sources = {_toml_array(repository.context_sources)}",
                )
            )
            for checkout in (
                value
                for value in checkouts
                if value.repository_id == repository.repository_id
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
                    provider_override = _toml_string(checkout.provider_override.value)
                    lines.append(f"provider_override = {provider_override}")
                lines.append(
                    f"is_default = {'true' if checkout.is_default else 'false'}"
                )
    staleness = config.defaults.staleness_interval_seconds
    desktop_mode = _toml_string(config.views.desktop_default_mode.value)
    complete_return = _toml_string(config.automation.complete_return.value)
    lines.extend(
        (
            "",
            "[defaults]",
            f"transport = {_toml_string(config.defaults.transport.value)}",
            f"refresh_interval_seconds = {config.defaults.refresh_interval_seconds}",
            f"staleness_interval_seconds = {staleness}",
            "",
            "[views]",
            f"cli_default_mode = {_toml_string(config.views.cli_default_mode.value)}",
            f"desktop_default_mode = {desktop_mode}",
            "",
            "[automation]",
            f"task_push = {_toml_string(config.automation.task_push.value)}",
            f"complete_return = {complete_return}",
            f"initial_max_depth = {config.automation.initial_max_depth}",
            "",
            "[control_turns]",
            f"transport = {_toml_string(config.control_turns.transport.value)}",
            "watchdog_timeout_seconds = "
            f"{config.control_turns.watchdog_timeout_seconds}",
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
            f"command = {_toml_array(config.memory.command)}",
            f"tool = {_toml_string(config.memory.tool)}",
            f"timeout_seconds = {config.memory.timeout_seconds}",
        )
    )
    rendered = "\n".join(lines) + "\n"
    if parse_config(rendered) != config:
        raise ConfigError("rendered configuration did not round-trip")
    return rendered


__all__ = [
    "CONFIG_VERSION",
    "AutomationConfig",
    "ConfigError",
    "ControlTurnsConfig",
    "DefaultsConfig",
    "HooksConfig",
    "HostConfig",
    "MemoryConfig",
    "ProjectCatalog",
    "ProviderConfig",
    "RemoteConfig",
    "SwitchboardConfig",
    "TmuxConfig",
    "ViewsConfig",
    "parse_config",
    "render_config",
]
