"""Host-local orchestration for the zero-configuration JSON CLI."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from .config import SwitchboardConfig, load_config
from .domain import ProjectId, ProviderId
from .live import reconcile_live
from .paths import database_path, load_or_create_host_id
from .providers.codex import CodexProvider
from .reconcile import reconcile_codex_discovery
from .snapshot import build_host_snapshot_json
from .storage import Registry


def _timestamp_ms(value: datetime | None) -> int | None:
    return None if value is None else int(value.timestamp() * 1_000)


def _project_catalog(config: SwitchboardConfig) -> tuple[Mapping[str, Any], ...]:
    locations_by_project: dict[ProjectId, list[dict[str, Any]]] = {}
    for location in sorted(config.locations, key=lambda item: str(item.location_id)):
        locations_by_project.setdefault(location.project_id, []).append(
            {
                "location_id": str(location.location_id),
                "path": str(location.path),
                "display_name": location.display_name,
                "repository_identity": location.repository_identity,
                "provider_override": (
                    location.provider_override.value
                    if location.provider_override is not None
                    else None
                ),
                "transport_override": (
                    location.transport_override.value
                    if location.transport_override is not None
                    else None
                ),
                "is_default": location.is_default,
                "last_observed_at": _timestamp_ms(location.last_observed_at),
            }
        )

    return tuple(
        {
            "project_id": str(project.project_id),
            "name": project.name,
            "aliases": project.aliases,
            "default_provider": (
                project.default_provider.value
                if project.default_provider is not None
                else None
            ),
            "default_transport": project.default_transport.value,
            "context_sources": project.context_sources,
            "locations": tuple(locations_by_project.get(project.project_id, ())),
        }
        for project in sorted(config.projects, key=lambda item: str(item.project_id))
    )


def _codex_executable(config: SwitchboardConfig) -> str | None:
    for provider in config.providers:
        if provider.provider is ProviderId.CODEX:
            return (provider.executable or "codex") if provider.enabled else None
    return None


def materialize_configured_projects(
    registry: Registry,
    host_id: str,
    config: SwitchboardConfig,
) -> None:
    """Persist the validated host-local project catalog for launch resolution."""

    registry.upsert_host(
        host_id,
        config.host.display_name,
        is_local=True,
    )
    registry.materialize_projects(host_id, _project_catalog(config))


def build_local_snapshot_json(
    *, reconcile: Literal["none", "live", "full"] = "none"
) -> str:
    """Return one canonical host-local snapshot at the requested repair level."""

    if reconcile not in {"none", "live", "full"}:
        raise ValueError("reconcile must be none, live, or full")

    host_id = load_or_create_host_id()
    registry_path = database_path()
    config = load_config(host_id=host_id) if not registry_path.exists() else None

    with Registry(registry_path) as registry:
        existing_host = registry.get_host(str(host_id))
        needs_bootstrap = existing_host is None or not bool(existing_host["is_local"])

        # A fresh registry has no host row to snapshot. Bootstrap it once even on
        # a no-refresh request; after that, no-refresh is a read-only fast path.
        if reconcile == "none" and not needs_bootstrap:
            return build_host_snapshot_json(registry, str(host_id))

        if config is None and (needs_bootstrap or reconcile == "full"):
            config = load_config(host_id=host_id)
        if needs_bootstrap or reconcile == "full":
            assert config is not None
            materialize_configured_projects(registry, str(host_id), config)

        capabilities = ()
        errors: tuple = ()
        executable = (
            _codex_executable(config)
            if reconcile == "full" and config is not None
            else None
        )
        if executable is not None:
            result = reconcile_codex_discovery(
                registry,
                str(host_id),
                CodexProvider(executable=executable).discover_sessions(),
            )
            capabilities = (result.capability,)
            errors = result.errors
        if reconcile in {"live", "full"}:
            live = reconcile_live(registry, str(host_id))
            errors = (*errors, *live.errors)

        return build_host_snapshot_json(
            registry,
            str(host_id),
            capabilities=capabilities,
            errors=errors,
        )


__all__ = ["build_local_snapshot_json", "materialize_configured_projects"]
