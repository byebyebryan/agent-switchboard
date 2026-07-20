"""Host-local orchestration for the zero-configuration JSON CLI."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from .config import SwitchboardConfig, load_config
from .domain import HostId, ProjectId, ProviderId, RepositoryId
from .live import reconcile_live
from .paths import database_path, load_or_create_host_id
from .protocol import ErrorRecord, ErrorScope
from .providers.claude import ClaudeProvider, inspect_claude_settings
from .providers.codex import CodexProvider
from .reconcile import reconcile_claude_capability, reconcile_codex_discovery
from .repository_discovery import RepositoryDiscoveryError, probe_git_repository
from .snapshot import build_host_snapshot_json
from .storage import Registry, StorageError


def _timestamp_ms(value: datetime | None) -> int | None:
    return None if value is None else int(value.timestamp() * 1_000)


def _project_catalog(config: SwitchboardConfig) -> tuple[Mapping[str, Any], ...]:
    checkouts_by_repository: dict[RepositoryId, list[dict[str, Any]]] = {}
    for checkout in sorted(config.checkouts, key=lambda item: str(item.checkout_id)):
        checkouts_by_repository.setdefault(checkout.repository_id, []).append(
            {
                "checkout_id": str(checkout.checkout_id),
                "path": str(checkout.path),
                "kind": checkout.kind.value,
                "display_name": checkout.display_name,
                "provider_override": (
                    checkout.provider_override.value
                    if checkout.provider_override is not None
                    else None
                ),
                "transport_override": (
                    checkout.transport_override.value
                    if checkout.transport_override is not None
                    else None
                ),
                "is_default": checkout.is_default,
                "declared": checkout.declared,
                "present": checkout.path.is_dir(),
                "last_observed_at": _timestamp_ms(checkout.last_observed_at),
            }
        )

    repositories = {
        repository.repository_id: repository for repository in config.repositories
    }
    memberships_by_project: dict[ProjectId, list[dict[str, Any]]] = {}
    for membership in config.project_repositories:
        repository = repositories[membership.repository_id]
        memberships_by_project.setdefault(membership.project_id, []).append(
            {
                "repository_id": str(repository.repository_id),
                "name": repository.name,
                "kind": repository.kind.value,
                "context_sources": repository.context_sources,
                "is_primary": membership.is_primary,
                "checkouts": tuple(
                    checkouts_by_repository.get(repository.repository_id, ())
                ),
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
            "repositories": tuple(memberships_by_project.get(project.project_id, ())),
        }
        for project in sorted(config.projects, key=lambda item: str(item.project_id))
    )


def materialize_configured_projects(
    registry: Registry,
    host_id: str,
    config: SwitchboardConfig,
) -> tuple[ErrorRecord, ...]:
    """Persist the validated host-local project catalog for launch resolution."""

    registry.upsert_host(
        host_id,
        config.host.display_name,
        is_local=True,
    )
    registry.materialize_projects(host_id, _project_catalog(config))
    errors: list[ErrorRecord] = []
    project_by_repository = {
        membership.repository_id: membership.project_id
        for membership in config.project_repositories
        if membership.is_primary
    }
    for repository in config.repositories:
        if repository.kind.value != "git":
            continue
        declared = sorted(
            (
                checkout
                for checkout in config.checkouts
                if checkout.repository_id == repository.repository_id
            ),
            key=lambda checkout: (not checkout.is_default, str(checkout.checkout_id)),
        )
        if not declared:
            continue
        try:
            observation = probe_git_repository(declared[0].path)
            roots = {checkout.path for checkout in observation.checkouts}
            if any(checkout.path not in roots for checkout in declared):
                raise RepositoryDiscoveryError(
                    "git_configured_checkout_missing",
                    "A configured checkout is absent from the repository worktree set.",
                )
            registry.reconcile_repository_checkouts(
                host_id=host_id,
                repository_id=str(repository.repository_id),
                observations=tuple(
                    {
                        "path": str(checkout.path),
                        "kind": checkout.kind,
                        "branch": checkout.branch,
                        "head_oid": checkout.head_oid,
                        "git_common_dir": str(checkout.git_common_dir),
                        "git_dir": str(checkout.git_dir),
                    }
                    for checkout in observation.checkouts
                ),
            )
        except (RepositoryDiscoveryError, StorageError) as error:
            code = getattr(error, "code", "repository_discovery_failed")
            errors.append(
                ErrorRecord(
                    code=str(code),
                    message=str(error),
                    scope=ErrorScope.PROJECT,
                    retryable=True,
                    observed_at=int(datetime.now().timestamp() * 1_000),
                    host_id=HostId(host_id),
                    details={
                        "projectId": str(
                            project_by_repository.get(
                                repository.repository_id, repository.repository_id
                            )
                        ),
                        "repositoryId": str(repository.repository_id),
                    },
                )
            )
    return tuple(errors)


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
        errors = []
        if needs_bootstrap or reconcile == "full":
            assert config is not None
            errors.extend(
                materialize_configured_projects(registry, str(host_id), config)
            )

        capabilities = []
        if reconcile == "full" and config is not None:
            for provider in config.providers:
                if not provider.enabled:
                    continue
                if provider.provider is ProviderId.CODEX:
                    result = reconcile_codex_discovery(
                        registry,
                        str(host_id),
                        CodexProvider(
                            executable=provider.executable or "codex"
                        ).discover_sessions(),
                    )
                else:
                    result = reconcile_claude_capability(
                        str(host_id),
                        ClaudeProvider(
                            executable=provider.executable or "claude"
                        ).inspect_capability(inspect_claude_settings()),
                    )
                capabilities.append(result.capability)
                errors.extend(result.errors)
        if reconcile in {"live", "full"}:
            live = reconcile_live(registry, str(host_id))
            errors.extend(live.errors)

        return build_host_snapshot_json(
            registry,
            str(host_id),
            capabilities=tuple(
                sorted(capabilities, key=lambda capability: capability.provider.value)
            ),
            errors=tuple(errors),
        )


__all__ = ["build_local_snapshot_json", "materialize_configured_projects"]
