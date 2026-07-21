"""High-level project, repository, and checkout catalog operations."""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from .catalog import (
    CatalogEditor,
    CatalogEditResult,
    CatalogError,
    PathClassification,
    inspect_path,
)
from .config import ProjectCatalog, SwitchboardConfig
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
from .local import materialize_configured_projects
from .paths import APP_DIR, state_home
from .storage import Registry

CATALOG_VERSION: Final = 1
PROJECT_EXPORT_VERSION: Final = 1
MAX_CATALOG_ITEMS: Final = 10_000
MAX_ARCHIVE_BYTES: Final = 2 * 1024 * 1024
_UNSET = object()


@dataclass(frozen=True, slots=True)
class CatalogMutation:
    edit: CatalogEditResult
    identities: dict[str, str]


def _catalog(
    config: SwitchboardConfig,
    *,
    projects: tuple[Project, ...] | None = None,
    repositories: tuple[Repository, ...] | None = None,
    memberships: tuple[ProjectRepository, ...] | None = None,
    checkouts: tuple[Checkout, ...] | None = None,
) -> SwitchboardConfig:
    value = ProjectCatalog(
        merge_projects(projects if projects is not None else config.projects),
        merge_repositories(
            repositories if repositories is not None else config.repositories
        ),
        merge_project_repositories(
            memberships if memberships is not None else config.project_repositories
        ),
        merge_checkouts(checkouts if checkouts is not None else config.checkouts),
    )
    return replace(config, catalog=value)


def _project(config: SwitchboardConfig, project_id: str) -> Project:
    parsed = ProjectId(project_id)
    for project in config.projects:
        if project.project_id == parsed:
            return project
    raise CatalogError("project_not_found", "The selected project is not declared.")


def _repository(config: SwitchboardConfig, repository_id: str) -> Repository:
    parsed = RepositoryId(repository_id)
    for repository in config.repositories:
        if repository.repository_id == parsed:
            return repository
    raise CatalogError(
        "repository_not_found", "The selected repository is not declared."
    )


def _checkout(config: SwitchboardConfig, checkout_id: str) -> Checkout:
    parsed = CheckoutId(checkout_id)
    for checkout in config.checkouts:
        if checkout.checkout_id == parsed:
            return checkout
    raise CatalogError("checkout_not_found", "The selected checkout is not declared.")


def _memberships(
    config: SwitchboardConfig, project_id: ProjectId
) -> tuple[ProjectRepository, ...]:
    return tuple(
        membership
        for membership in config.project_repositories
        if membership.project_id == project_id
    )


def _pruned(
    config: SwitchboardConfig,
    *,
    projects: tuple[Project, ...],
    memberships: tuple[ProjectRepository, ...],
) -> SwitchboardConfig:
    repository_ids = {membership.repository_id for membership in memberships}
    repositories = tuple(
        repository
        for repository in config.repositories
        if repository.repository_id in repository_ids
    )
    checkouts = tuple(
        checkout
        for checkout in config.checkouts
        if checkout.repository_id in repository_ids
    )
    return _catalog(
        config,
        projects=projects,
        repositories=repositories,
        memberships=memberships,
        checkouts=checkouts,
    )


def _provider(value: object) -> ProviderId | None:
    if value is None or isinstance(value, ProviderId):
        return value
    if not isinstance(value, str):
        raise CatalogError(
            "provider_invalid", "The selected default provider is invalid."
        )
    try:
        return ProviderId(value)
    except ValueError as error:
        raise CatalogError(
            "provider_invalid", "The selected default provider is invalid."
        ) from error


def _transport(value: object, *, optional: bool) -> Transport | None:
    if value is None and optional:
        return None
    if isinstance(value, Transport):
        return value
    if not isinstance(value, str):
        raise CatalogError("transport_invalid", "The selected transport is invalid.")
    try:
        return Transport(value)
    except ValueError as error:
        raise CatalogError(
            "transport_invalid", "The selected transport is invalid."
        ) from error


def _required_transport(value: object) -> Transport:
    selected = _transport(value, optional=False)
    assert selected is not None
    return selected


def _checkout_record(checkout: Checkout) -> dict[str, object]:
    return {
        "checkoutId": str(checkout.checkout_id),
        "repositoryId": str(checkout.repository_id),
        "hostId": str(checkout.host_id),
        "path": str(checkout.path),
        "kind": checkout.kind.value,
        "displayName": checkout.display_name,
        "providerOverride": (
            None
            if checkout.provider_override is None
            else checkout.provider_override.value
        ),
        "transportOverride": (
            None
            if checkout.transport_override is None
            else checkout.transport_override.value
        ),
        "isDefault": checkout.is_default,
    }


def _checkout_from_record(raw: object, *, host_id: HostId) -> Checkout:
    if not isinstance(raw, dict) or set(raw) != {
        "checkoutId",
        "repositoryId",
        "hostId",
        "path",
        "kind",
        "displayName",
        "providerOverride",
        "transportOverride",
        "isDefault",
    }:
        raise CatalogError("catalog_archive_invalid", "The catalog archive is invalid.")
    if raw["hostId"] != str(host_id):
        raise CatalogError(
            "catalog_archive_host_mismatch",
            "The archived checkout belongs to another host.",
        )
    try:
        return Checkout(
            CheckoutId(raw["checkoutId"]),
            RepositoryId(raw["repositoryId"]),
            host_id,
            Path(raw["path"]),
            kind=raw["kind"],
            display_name=raw["displayName"],
            provider_override=raw["providerOverride"],
            transport_override=raw["transportOverride"],
            is_default=raw["isDefault"],
        )
    except (TypeError, ValidationError, ValueError) as error:
        raise CatalogError(
            "catalog_archive_invalid", "The catalog archive is invalid."
        ) from error


def _append_identity[Identity](
    existing: list[Identity],
    candidate: Identity,
    *,
    identity: Callable[[Identity], object],
    label: str,
) -> None:
    for current in existing:
        if identity(current) != identity(candidate):
            continue
        if current != candidate:
            raise CatalogError(
                "catalog_identity_conflict",
                f"The archived {label} identity conflicts with current metadata.",
            )
        return
    existing.append(candidate)


def _resolved_directory(value: str | Path) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CatalogError(
            "catalog_path_unavailable", "The selected path is unavailable."
        ) from error
    if not path.is_dir():
        raise CatalogError(
            "catalog_path_not_directory", "The selected path is not a directory."
        )
    return path


def _classification(
    config: SwitchboardConfig, value: str | Path, kind: str
) -> PathClassification:
    if kind not in {"auto", "git", "directory"}:
        raise CatalogError("repository_kind_invalid", "Repository kind is invalid.")
    if kind == "directory":
        path = _resolved_directory(value)
        for checkout in config.checkouts:
            if checkout.path == path:
                repository_id = str(checkout.repository_id)
                return PathClassification(
                    "known_checkout",
                    path,
                    checkout.kind,
                    checkout.display_name or path.name,
                    tuple(
                        sorted(
                            str(item.project_id)
                            for item in config.project_repositories
                            if item.repository_id == checkout.repository_id
                        )
                    ),
                    repository_id,
                    str(checkout.checkout_id),
                )
        return PathClassification("directory", path, CheckoutKind.DIRECTORY, path.name)
    result = inspect_path(config, value)
    if kind == "git" and result.kind == "directory":
        raise CatalogError(
            "git_probe_failed", "The selected path is not a readable Git checkout."
        )
    return result


class CatalogManager:
    """Apply catalog operations through one authoritative editor and registry."""

    def __init__(
        self,
        *,
        host_id: HostId,
        editor: CatalogEditor,
        registry: Registry,
        archive_root: str | Path | None = None,
        now_ms: Callable[[], int] | None = None,
        project_id_factory: Callable[[], ProjectId] = ProjectId.new,
        repository_id_factory: Callable[[], RepositoryId] = RepositoryId.new,
        checkout_id_factory: Callable[[], CheckoutId] = CheckoutId.new,
    ) -> None:
        self.host_id = host_id
        self.editor = editor
        self.registry = registry
        self.archive_root = (
            Path(archive_root)
            if archive_root is not None
            else state_home() / APP_DIR / "catalog-archives"
        )
        self.now_ms = (
            (lambda: time.time_ns() // 1_000_000) if now_ms is None else now_ms
        )
        self.project_id_factory = project_id_factory
        self.repository_id_factory = repository_id_factory
        self.checkout_id_factory = checkout_id_factory

    def _read(self) -> SwitchboardConfig:
        config = self.editor.read()
        materialize_configured_projects(self.registry, str(self.host_id), config)
        return config

    def inspect(self, value: str | Path, *, kind: str = "auto") -> PathClassification:
        return _classification(self._read(), value, kind)

    def _apply(
        self,
        operation: str,
        transform: Callable[[SwitchboardConfig], SwitchboardConfig],
        *,
        identities: dict[str, str],
        dry_run: bool,
    ) -> CatalogMutation:
        result = self.editor.apply(operation, transform, dry_run=dry_run)
        if result.changed and not result.dry_run:
            try:
                materialize_configured_projects(
                    self.registry, str(self.host_id), result.config
                )
            except (OSError, ValidationError, ValueError) as error:
                backup = (
                    ""
                    if result.backup_path is None
                    else f" Backup: {result.backup_path}."
                )
                raise CatalogError(
                    "catalog_materialization_failed",
                    "The config was updated but catalog materialization "
                    f"failed.{backup}",
                ) from error
        return CatalogMutation(result, identities)

    def _references(self) -> dict[str, dict[str, dict[str, int]]]:
        return self.registry.catalog_reference_counts(str(self.host_id))

    @staticmethod
    def _active(values: dict[str, int]) -> bool:
        return any(
            values.get(field, 0) > 0
            for field in ("openTasks", "liveSessions", "pendingLaunches")
        )

    def add_project(
        self,
        value: str | Path,
        *,
        name: str | None = None,
        kind: str = "auto",
        default_provider: str | ProviderId | None = ProviderId.CODEX,
        default_transport: str | Transport = Transport.TMUX,
        dry_run: bool = False,
    ) -> CatalogMutation:
        current = self._read()
        classified = _classification(current, value, kind)
        if classified.kind == "known_checkout":
            if not classified.project_ids:
                raise CatalogError(
                    "catalog_membership_missing",
                    "The configured checkout has no project membership.",
                )
            return self._apply(
                "project-add",
                lambda config: config,
                identities={
                    "projectId": classified.project_ids[0],
                    "repositoryId": str(classified.repository_id),
                    "checkoutId": str(classified.checkout_id),
                },
                dry_run=dry_run,
            )
        if classified.kind == "new_checkout":
            raise CatalogError(
                "repository_already_configured",
                "The path is a new checkout of an existing repository; add the "
                "checkout instead.",
            )
        project_id = self.project_id_factory()
        repository_id = self.repository_id_factory()
        checkout_id = self.checkout_id_factory()
        project_name = name or classified.suggested_name
        repository_kind = (
            RepositoryKind.DIRECTORY
            if classified.kind == "directory"
            else RepositoryKind.GIT
        )
        project = Project(
            project_id,
            project_name,
            default_provider=_provider(default_provider),
            default_transport=_required_transport(default_transport),
        )
        repository = Repository(repository_id, project_name, repository_kind)
        membership = ProjectRepository(project_id, repository_id, True)
        checkout = Checkout(
            checkout_id,
            repository_id,
            self.host_id,
            classified.path,
            kind=classified.checkout_kind,
            display_name=classified.path.name,
            is_default=True,
        )

        def transform(config: SwitchboardConfig) -> SwitchboardConfig:
            return _catalog(
                config,
                projects=(*config.projects, project),
                repositories=(*config.repositories, repository),
                memberships=(*config.project_repositories, membership),
                checkouts=(*config.checkouts, checkout),
            )

        return self._apply(
            "project-add",
            transform,
            identities={
                "projectId": str(project_id),
                "repositoryId": str(repository_id),
                "checkoutId": str(checkout_id),
            },
            dry_run=dry_run,
        )

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        aliases: tuple[str, ...] | None = None,
        default_provider: object = _UNSET,
        default_transport: object = _UNSET,
        dry_run: bool = False,
    ) -> CatalogMutation:
        parsed = ProjectId(project_id)

        def transform(config: SwitchboardConfig) -> SwitchboardConfig:
            current = _project(config, project_id)
            updated = replace(
                current,
                name=current.name if name is None else name,
                aliases=current.aliases if aliases is None else aliases,
                default_provider=(
                    current.default_provider
                    if default_provider is _UNSET
                    else _provider(default_provider)
                ),
                default_transport=(
                    current.default_transport
                    if default_transport is _UNSET
                    else _required_transport(default_transport)
                ),
            )
            return _catalog(
                config,
                projects=tuple(
                    updated if project.project_id == parsed else project
                    for project in config.projects
                ),
            )

        return self._apply(
            "project-update",
            transform,
            identities={"projectId": str(parsed)},
            dry_run=dry_run,
        )

    def _archive_path(self, kind: str, identity: str) -> Path:
        return self.archive_root / f"{kind}-{identity}.json"

    def _secure_archive_root(self) -> None:
        try:
            self.archive_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = self.archive_root.lstat()
        except OSError as error:
            raise CatalogError(
                "catalog_archive_unavailable", "The catalog archive is unavailable."
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise CatalogError(
                "catalog_archive_unsafe",
                "The catalog archive must be a private user-owned directory.",
            )

    def _write_archive(self, kind: str, identity: str, payload: object) -> None:
        self._secure_archive_root()
        destination = self._archive_path(kind, identity)
        if destination.exists() or destination.is_symlink():
            try:
                metadata = destination.lstat()
            except OSError as error:
                raise CatalogError(
                    "catalog_archive_unavailable",
                    "The catalog archive is unavailable.",
                ) from error
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise CatalogError(
                    "catalog_archive_unsafe",
                    "The catalog archive entry is unsafe.",
                )
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_ARCHIVE_BYTES:
            raise CatalogError(
                "catalog_archive_too_large", "The catalog archive is too large."
            )
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError("short write")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, destination)
            directory_fd = os.open(self.archive_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise CatalogError(
                "catalog_archive_write_failed",
                "The catalog archive could not be written.",
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _read_archive(self, kind: str, identity: str) -> object:
        self._secure_archive_root()
        source = self._archive_path(kind, identity)
        try:
            metadata = source.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_size > MAX_ARCHIVE_BYTES
            ):
                raise CatalogError(
                    "catalog_archive_unsafe",
                    "The catalog archive entry is unsafe.",
                )
            return json.loads(source.read_bytes())
        except CatalogError:
            raise
        except (OSError, ValueError) as error:
            raise CatalogError(
                "catalog_archive_missing", "The catalog archive is unavailable."
            ) from error

    def _project_archive(
        self, config: SwitchboardConfig, project_id: str
    ) -> dict[str, object]:
        project = _project(config, project_id)
        repositories = {item.repository_id: item for item in config.repositories}
        records: list[dict[str, object]] = []
        for membership in _memberships(config, project.project_id):
            repository = repositories[membership.repository_id]
            records.append(
                {
                    "repositoryId": str(repository.repository_id),
                    "name": repository.name,
                    "kind": repository.kind.value,
                    "contextSources": list(repository.context_sources),
                    "isPrimary": membership.is_primary,
                    "checkouts": [
                        _checkout_record(checkout)
                        for checkout in config.checkouts
                        if checkout.repository_id == repository.repository_id
                        and checkout.host_id == self.host_id
                    ],
                }
            )
        return {
            "archiveVersion": 1,
            "kind": "project",
            "project": {
                "projectId": str(project.project_id),
                "name": project.name,
                "aliases": list(project.aliases),
                "defaultProvider": (
                    None
                    if project.default_provider is None
                    else project.default_provider.value
                ),
                "defaultTransport": project.default_transport.value,
                "repositories": records,
            },
        }

    def _decode_project_archive(
        self, payload: object, project_id: str
    ) -> tuple[
        Project,
        tuple[Repository, ...],
        tuple[ProjectRepository, ...],
        tuple[Checkout, ...],
    ]:
        if (
            not isinstance(payload, dict)
            or set(payload) != {"archiveVersion", "kind", "project"}
            or payload["archiveVersion"] != 1
            or payload["kind"] != "project"
            or not isinstance(payload["project"], dict)
        ):
            raise CatalogError(
                "catalog_archive_invalid", "The catalog archive is invalid."
            )
        raw = payload["project"]
        if (
            set(raw)
            != {
                "projectId",
                "name",
                "aliases",
                "defaultProvider",
                "defaultTransport",
                "repositories",
            }
            or raw["projectId"] != project_id
        ):
            raise CatalogError(
                "catalog_archive_invalid", "The catalog archive is invalid."
            )
        try:
            project = Project(
                ProjectId(raw["projectId"]),
                raw["name"],
                aliases=tuple(raw["aliases"]),
                default_provider=raw["defaultProvider"],
                default_transport=raw["defaultTransport"],
            )
            repositories: list[Repository] = []
            memberships: list[ProjectRepository] = []
            checkouts: list[Checkout] = []
            if not isinstance(raw["repositories"], list):
                raise TypeError
            for record in raw["repositories"]:
                if not isinstance(record, dict) or set(record) != {
                    "repositoryId",
                    "name",
                    "kind",
                    "contextSources",
                    "isPrimary",
                    "checkouts",
                }:
                    raise TypeError
                repository = Repository(
                    RepositoryId(record["repositoryId"]),
                    record["name"],
                    record["kind"],
                    tuple(record["contextSources"]),
                )
                repositories.append(repository)
                memberships.append(
                    ProjectRepository(
                        project.project_id,
                        repository.repository_id,
                        record["isPrimary"],
                    )
                )
                if not isinstance(record["checkouts"], list):
                    raise TypeError
                for checkout_raw in record["checkouts"]:
                    checkout = _checkout_from_record(checkout_raw, host_id=self.host_id)
                    if checkout.repository_id != repository.repository_id:
                        raise TypeError
                    checkouts.append(checkout)
            # Domain merge helpers enforce uniqueness and one primary/default.
            return (
                project,
                merge_repositories(repositories),
                merge_project_repositories(memberships),
                merge_checkouts(checkouts),
            )
        except CatalogError:
            raise
        except (KeyError, TypeError, ValidationError, ValueError) as error:
            raise CatalogError(
                "catalog_archive_invalid", "The catalog archive is invalid."
            ) from error

    def archive_project(
        self, project_id: str, *, confirmed: bool, dry_run: bool = False
    ) -> CatalogMutation:
        if not confirmed:
            raise CatalogError(
                "confirmation_required", "Project archive requires confirmation."
            )
        config = self._read()
        project = _project(config, project_id)
        references = self._references()["projects"].get(str(project.project_id), {})
        if self._active(references):
            raise CatalogError(
                "project_active",
                "Close open tasks and stop live or pending project work before "
                "archive.",
            )
        if not dry_run:
            self._write_archive(
                "project",
                str(project.project_id),
                self._project_archive(config, str(project.project_id)),
            )

        def transform(current: SwitchboardConfig) -> SwitchboardConfig:
            _project(current, project_id)
            projects = tuple(
                item
                for item in current.projects
                if item.project_id != project.project_id
            )
            memberships = tuple(
                item
                for item in current.project_repositories
                if item.project_id != project.project_id
            )
            return _pruned(current, projects=projects, memberships=memberships)

        return self._apply(
            "project-archive",
            transform,
            identities={"projectId": str(project.project_id)},
            dry_run=dry_run,
        )

    def restore_project(
        self, project_id: str, *, dry_run: bool = False
    ) -> CatalogMutation:
        parsed_id = str(ProjectId(project_id))
        current = self._read()
        if any(str(item.project_id) == parsed_id for item in current.projects):
            raise CatalogError("project_not_archived", "The project is not archived.")
        project, archived_repositories, archived_memberships, archived_checkouts = (
            self._decode_project_archive(
                self._read_archive("project", parsed_id), parsed_id
            )
        )

        def transform(config: SwitchboardConfig) -> SwitchboardConfig:
            repositories: list[Repository] = list(config.repositories)
            memberships: list[ProjectRepository] = list(config.project_repositories)
            checkouts: list[Checkout] = list(config.checkouts)
            for repository in archived_repositories:
                _append_identity(
                    repositories,
                    repository,
                    identity=lambda item: item.repository_id,
                    label="repository",
                )
            for membership in archived_memberships:
                _append_identity(
                    memberships,
                    membership,
                    identity=lambda item: (item.project_id, item.repository_id),
                    label="repository membership",
                )
            for checkout in archived_checkouts:
                _append_identity(
                    checkouts,
                    checkout,
                    identity=lambda item: item.checkout_id,
                    label="checkout",
                )
            return _catalog(
                config,
                projects=(*config.projects, project),
                repositories=tuple(repositories),
                memberships=tuple(memberships),
                checkouts=tuple(checkouts),
            )

        return self._apply(
            "project-restore",
            transform,
            identities={"projectId": parsed_id},
            dry_run=dry_run,
        )

    def add_repository(
        self,
        project_id: str,
        value: str | Path,
        *,
        name: str | None = None,
        kind: str = "auto",
        primary: bool = False,
        dry_run: bool = False,
    ) -> CatalogMutation:
        current = self._read()
        project = _project(current, project_id)
        classified = _classification(current, value, kind)
        if classified.kind in {"known_checkout", "new_checkout"}:
            raise CatalogError(
                "repository_already_configured",
                "The repository already exists; link it or add its checkout instead.",
            )
        repository_id = self.repository_id_factory()
        checkout_id = self.checkout_id_factory()
        repository = Repository(
            repository_id,
            name or classified.suggested_name,
            RepositoryKind.DIRECTORY
            if classified.kind == "directory"
            else RepositoryKind.GIT,
        )
        current_memberships = _memberships(current, project.project_id)
        make_primary = primary or not current_memberships
        membership = ProjectRepository(project.project_id, repository_id, make_primary)
        checkout = Checkout(
            checkout_id,
            repository_id,
            self.host_id,
            classified.path,
            kind=classified.checkout_kind,
            display_name=classified.path.name,
            is_default=True,
        )

        def transform(config: SwitchboardConfig) -> SwitchboardConfig:
            _project(config, project_id)
            memberships = list(config.project_repositories)
            if make_primary:
                memberships = [
                    replace(item, is_primary=False)
                    if item.project_id == project.project_id
                    else item
                    for item in memberships
                ]
            memberships.append(membership)
            return _catalog(
                config,
                repositories=(*config.repositories, repository),
                memberships=tuple(memberships),
                checkouts=(*config.checkouts, checkout),
            )

        return self._apply(
            "repository-add",
            transform,
            identities={
                "projectId": project_id,
                "repositoryId": str(repository_id),
                "checkoutId": str(checkout_id),
            },
            dry_run=dry_run,
        )

    def link_repository(
        self,
        project_id: str,
        repository_id: str,
        *,
        primary: bool = False,
        dry_run: bool = False,
    ) -> CatalogMutation:
        parsed_project = ProjectId(project_id)
        parsed_repository = RepositoryId(repository_id)

        def transform(config: SwitchboardConfig) -> SwitchboardConfig:
            _project(config, project_id)
            _repository(config, repository_id)
            existing = next(
                (
                    item
                    for item in config.project_repositories
                    if item.project_id == parsed_project
                    and item.repository_id == parsed_repository
                ),
                None,
            )
            memberships = list(config.project_repositories)
            if existing is not None and (not primary or existing.is_primary):
                return config
            make_primary = primary or not _memberships(config, parsed_project)
            if make_primary:
                memberships = [
                    replace(item, is_primary=False)
                    if item.project_id == parsed_project
                    else item
                    for item in memberships
                ]
            if existing is None:
                memberships.append(
                    ProjectRepository(parsed_project, parsed_repository, make_primary)
                )
            else:
                memberships = [
                    replace(item, is_primary=make_primary) if item == existing else item
                    for item in memberships
                ]
            return _catalog(config, memberships=tuple(memberships))

        return self._apply(
            "repository-link",
            transform,
            identities={"projectId": project_id, "repositoryId": repository_id},
            dry_run=dry_run,
        )

    def update_repository(
        self,
        repository_id: str,
        *,
        name: str | None = None,
        context_sources: tuple[str, ...] | None = None,
        dry_run: bool = False,
    ) -> CatalogMutation:
        parsed = RepositoryId(repository_id)

        def transform(config: SwitchboardConfig) -> SwitchboardConfig:
            current = _repository(config, repository_id)
            updated = replace(
                current,
                name=current.name if name is None else name,
                context_sources=(
                    current.context_sources
                    if context_sources is None
                    else context_sources
                ),
            )
            return _catalog(
                config,
                repositories=tuple(
                    updated if item.repository_id == parsed else item
                    for item in config.repositories
                ),
            )

        return self._apply(
            "repository-update",
            transform,
            identities={"repositoryId": repository_id},
            dry_run=dry_run,
        )

    def set_primary_repository(
        self,
        project_id: str,
        repository_id: str,
        *,
        dry_run: bool = False,
    ) -> CatalogMutation:
        parsed_project = ProjectId(project_id)
        parsed_repository = RepositoryId(repository_id)

        def transform(config: SwitchboardConfig) -> SwitchboardConfig:
            _project(config, project_id)
            if not any(
                item.project_id == parsed_project
                and item.repository_id == parsed_repository
                for item in config.project_repositories
            ):
                raise CatalogError(
                    "repository_not_linked",
                    "The repository is not linked to the project.",
                )
            return _catalog(
                config,
                memberships=tuple(
                    replace(
                        item,
                        is_primary=item.repository_id == parsed_repository,
                    )
                    if item.project_id == parsed_project
                    else item
                    for item in config.project_repositories
                ),
            )

        return self._apply(
            "repository-primary",
            transform,
            identities={"projectId": project_id, "repositoryId": repository_id},
            dry_run=dry_run,
        )

    def unlink_repository(
        self,
        project_id: str,
        repository_id: str,
        *,
        confirmed: bool,
        dry_run: bool = False,
    ) -> CatalogMutation:
        if not confirmed:
            raise CatalogError(
                "confirmation_required", "Repository unlink requires confirmation."
            )
        config = self._read()
        project = _project(config, project_id)
        repository = _repository(config, repository_id)
        target = next(
            (
                item
                for item in config.project_repositories
                if item.project_id == project.project_id
                and item.repository_id == repository.repository_id
            ),
            None,
        )
        if target is None:
            raise CatalogError(
                "repository_not_linked", "The repository is not linked to the project."
            )
        references = self.registry.catalog_membership_reference_counts(
            str(self.host_id), project_id, repository_id
        )
        if any(references.values()):
            raise CatalogError(
                "repository_referenced",
                "A retained task, session, or launch still references the repository.",
            )
        remaining = [
            item
            for item in _memberships(config, project.project_id)
            if item.repository_id != repository.repository_id
        ]
        if not remaining:
            raise CatalogError(
                "project_repository_required",
                "Archive the project instead of unlinking its last repository.",
            )
        if target.is_primary and remaining:
            raise CatalogError(
                "primary_repository_required",
                "Choose another primary repository before unlinking this one.",
            )

        def transform(current: SwitchboardConfig) -> SwitchboardConfig:
            memberships = tuple(
                item
                for item in current.project_repositories
                if not (
                    item.project_id == project.project_id
                    and item.repository_id == repository.repository_id
                )
            )
            return _pruned(current, projects=current.projects, memberships=memberships)

        return self._apply(
            "repository-unlink",
            transform,
            identities={"projectId": project_id, "repositoryId": repository_id},
            dry_run=dry_run,
        )

    def add_checkout(
        self,
        repository_id: str,
        value: str | Path,
        *,
        display_name: str | None = None,
        kind: str = "auto",
        provider_override: str | ProviderId | None = None,
        transport_override: str | Transport | None = None,
        is_default: bool = False,
        dry_run: bool = False,
    ) -> CatalogMutation:
        config = self._read()
        repository = _repository(config, repository_id)
        classified = _classification(
            config,
            value,
            "directory" if repository.kind is RepositoryKind.DIRECTORY else kind,
        )
        if classified.kind == "known_checkout":
            if classified.repository_id != repository_id:
                raise CatalogError(
                    "checkout_repository_mismatch",
                    "The checkout belongs to another configured repository.",
                )
            return self._apply(
                "checkout-add",
                lambda current: current,
                identities={
                    "repositoryId": repository_id,
                    "checkoutId": str(classified.checkout_id),
                },
                dry_run=dry_run,
            )
        if repository.kind is RepositoryKind.GIT and (
            classified.kind != "new_checkout"
            or classified.repository_id != repository_id
        ):
            raise CatalogError(
                "checkout_repository_mismatch",
                "The Git checkout does not belong to the selected repository.",
            )
        checkout_id = self._promotable_checkout_id(repository_id, classified.path)
        checkout_id = checkout_id or self.checkout_id_factory()
        checkout = Checkout(
            checkout_id,
            repository.repository_id,
            self.host_id,
            classified.path,
            kind=classified.checkout_kind,
            display_name=display_name or classified.suggested_name,
            provider_override=_provider(provider_override),
            transport_override=_transport(transport_override, optional=True),
            is_default=is_default,
        )

        def transform(current: SwitchboardConfig) -> SwitchboardConfig:
            _repository(current, repository_id)
            checkouts = [
                replace(item, is_default=False)
                if is_default and item.repository_id == repository.repository_id
                else item
                for item in current.checkouts
            ]
            checkouts.append(checkout)
            return _catalog(current, checkouts=tuple(checkouts))

        return self._apply(
            "checkout-add",
            transform,
            identities={
                "repositoryId": repository_id,
                "checkoutId": str(checkout_id),
            },
            dry_run=dry_run,
        )

    def _promotable_checkout_id(
        self, repository_id: str, path: Path
    ) -> CheckoutId | None:
        checkout = self.registry.checkout_at_path(str(self.host_id), path)
        if checkout is None:
            return None
        if checkout["repository_id"] != repository_id:
            raise CatalogError(
                "checkout_repository_mismatch",
                "The checkout path is retained under another repository identity.",
            )
        if bool(checkout["declared"]):
            raise CatalogError(
                "checkout_path_conflict",
                "The checkout path is already declared.",
            )
        checkout_id = CheckoutId(checkout["checkout_id"])
        if self._archive_path("checkout", str(checkout_id)).exists():
            raise CatalogError(
                "checkout_archived",
                "Restore the archived checkout instead of adding it again.",
            )
        return checkout_id

    def update_checkout(
        self,
        checkout_id: str,
        *,
        path: str | Path | None = None,
        display_name: object = _UNSET,
        provider_override: object = _UNSET,
        transport_override: object = _UNSET,
        is_default: object = _UNSET,
        dry_run: bool = False,
    ) -> CatalogMutation:
        config = self._read()
        current = _checkout(config, checkout_id)
        selected_path = current.path
        selected_kind = current.kind
        if path is not None:
            references = self._references()["checkouts"].get(checkout_id, {})
            if self._active(references):
                raise CatalogError(
                    "checkout_active",
                    "Stop active checkout work before changing its path.",
                )
            repository = _repository(config, str(current.repository_id))
            classified = _classification(
                config,
                path,
                "directory" if repository.kind is RepositoryKind.DIRECTORY else "git",
            )
            known_current = (
                classified.kind == "known_checkout"
                and classified.checkout_id == checkout_id
            )
            known_repository = (
                classified.kind == "new_checkout"
                and classified.repository_id == str(repository.repository_id)
            )
            if repository.kind is RepositoryKind.GIT and not (
                known_current or known_repository
            ):
                raise CatalogError(
                    "checkout_repository_mismatch",
                    "The new path does not belong to the selected repository.",
                )
            selected_path = classified.path
            selected_kind = classified.checkout_kind
        default_value = current.is_default if is_default is _UNSET else bool(is_default)
        updated = replace(
            current,
            path=selected_path,
            kind=selected_kind,
            display_name=(
                current.display_name if display_name is _UNSET else display_name
            ),
            provider_override=(
                current.provider_override
                if provider_override is _UNSET
                else _provider(provider_override)
            ),
            transport_override=(
                current.transport_override
                if transport_override is _UNSET
                else _transport(transport_override, optional=True)
            ),
            is_default=default_value,
        )

        def transform(candidate: SwitchboardConfig) -> SwitchboardConfig:
            _checkout(candidate, checkout_id)
            checkouts = tuple(
                replace(item, is_default=False)
                if default_value
                and item.repository_id == updated.repository_id
                and item.checkout_id != updated.checkout_id
                else updated
                if item.checkout_id == updated.checkout_id
                else item
                for item in candidate.checkouts
            )
            return _catalog(candidate, checkouts=checkouts)

        return self._apply(
            "checkout-update",
            transform,
            identities={
                "repositoryId": str(current.repository_id),
                "checkoutId": checkout_id,
            },
            dry_run=dry_run,
        )

    def set_default_checkout(
        self, checkout_id: str, *, dry_run: bool = False
    ) -> CatalogMutation:
        return self.update_checkout(checkout_id, is_default=True, dry_run=dry_run)

    def archive_checkout(
        self, checkout_id: str, *, confirmed: bool, dry_run: bool = False
    ) -> CatalogMutation:
        if not confirmed:
            raise CatalogError(
                "confirmation_required", "Checkout archive requires confirmation."
            )
        config = self._read()
        checkout = _checkout(config, checkout_id)
        references = self._references()["checkouts"].get(checkout_id, {})
        if self._active(references):
            raise CatalogError(
                "checkout_active",
                "Close open tasks and stop live or pending checkout work before "
                "archive.",
            )
        if not dry_run:
            self._write_archive(
                "checkout",
                checkout_id,
                {
                    "archiveVersion": 1,
                    "kind": "checkout",
                    "checkout": _checkout_record(checkout),
                },
            )

        def transform(current: SwitchboardConfig) -> SwitchboardConfig:
            _checkout(current, checkout_id)
            return _catalog(
                current,
                checkouts=tuple(
                    item
                    for item in current.checkouts
                    if item.checkout_id != checkout.checkout_id
                ),
            )

        return self._apply(
            "checkout-archive",
            transform,
            identities={
                "repositoryId": str(checkout.repository_id),
                "checkoutId": checkout_id,
            },
            dry_run=dry_run,
        )

    def restore_checkout(
        self, checkout_id: str, *, dry_run: bool = False
    ) -> CatalogMutation:
        parsed_id = str(CheckoutId(checkout_id))
        current = self._read()
        if any(str(item.checkout_id) == parsed_id for item in current.checkouts):
            raise CatalogError("checkout_not_archived", "The checkout is not archived.")
        payload = self._read_archive("checkout", parsed_id)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"archiveVersion", "kind", "checkout"}
            or payload["archiveVersion"] != 1
            or payload["kind"] != "checkout"
        ):
            raise CatalogError(
                "catalog_archive_invalid", "The catalog archive is invalid."
            )
        checkout = _checkout_from_record(payload["checkout"], host_id=self.host_id)
        if str(checkout.checkout_id) != parsed_id:
            raise CatalogError(
                "catalog_archive_invalid", "The catalog archive is invalid."
            )
        repository_id = str(checkout.repository_id)

        def transform(config: SwitchboardConfig) -> SwitchboardConfig:
            repository = _repository(config, repository_id)
            if repository.repository_id != checkout.repository_id:
                raise CatalogError(
                    "catalog_identity_conflict",
                    "The archived checkout repository identity conflicts.",
                )
            checkouts = [
                replace(item, is_default=False)
                if checkout.is_default and item.repository_id == checkout.repository_id
                else item
                for item in config.checkouts
            ]
            checkouts.append(checkout)
            return _catalog(
                config,
                checkouts=tuple(checkouts),
            )

        return self._apply(
            "checkout-restore",
            transform,
            identities={"repositoryId": repository_id, "checkoutId": parsed_id},
            dry_run=dry_run,
        )

    def export_project(self, project_id: str) -> dict[str, object]:
        config = self._read()
        project = _project(config, project_id)
        repositories = {item.repository_id: item for item in config.repositories}
        records = []
        for membership in _memberships(config, project.project_id):
            repository = repositories[membership.repository_id]
            records.append(
                {
                    "repositoryId": str(repository.repository_id),
                    "name": repository.name,
                    "kind": repository.kind.value,
                    "isPrimary": membership.is_primary,
                    "contextSources": list(repository.context_sources),
                }
            )
        return {
            "schemaVersion": 2,
            "protocolVersion": 2,
            "projectExportVersion": PROJECT_EXPORT_VERSION,
            "generatedAt": self.now_ms(),
            "project": {
                "projectId": str(project.project_id),
                "name": project.name,
                "aliases": list(project.aliases),
                "defaultProvider": (
                    None
                    if project.default_provider is None
                    else project.default_provider.value
                ),
                "defaultTransport": project.default_transport.value,
                "repositories": records,
            },
        }

    def import_project(
        self,
        envelope: object,
        *,
        checkout_paths: dict[str, str | Path],
        dry_run: bool = False,
    ) -> CatalogMutation:
        current = self._read()
        if not isinstance(envelope, dict) or set(envelope) != {
            "schemaVersion",
            "protocolVersion",
            "projectExportVersion",
            "generatedAt",
            "project",
        }:
            raise CatalogError("project_export_invalid", "Project export is invalid.")
        if (
            envelope["schemaVersion"] != 2
            or envelope["protocolVersion"] != 2
            or envelope["projectExportVersion"] != PROJECT_EXPORT_VERSION
        ):
            raise CatalogError(
                "project_export_incompatible", "Project export is incompatible."
            )
        if not isinstance(envelope["generatedAt"], int):
            raise CatalogError("project_export_invalid", "Project export is invalid.")
        raw = envelope["project"]
        if (
            not isinstance(raw, dict)
            or set(raw)
            != {
                "projectId",
                "name",
                "aliases",
                "defaultProvider",
                "defaultTransport",
                "repositories",
            }
            or not isinstance(raw["repositories"], list)
            or len(raw["repositories"]) > MAX_CATALOG_ITEMS
        ):
            raise CatalogError("project_export_invalid", "Project export is invalid.")
        try:
            project = Project(
                ProjectId(raw["projectId"]),
                raw["name"],
                aliases=tuple(raw["aliases"]),
                default_provider=raw.get("defaultProvider"),
                default_transport=raw["defaultTransport"],
            )
            repositories: list[Repository] = []
            memberships: list[ProjectRepository] = []
            checkouts: list[Checkout] = []
            primary_id: str | None = None
            for item in raw["repositories"]:
                if not isinstance(item, dict) or set(item) != {
                    "repositoryId",
                    "name",
                    "kind",
                    "isPrimary",
                    "contextSources",
                }:
                    raise TypeError
                repository = Repository(
                    RepositoryId(item["repositoryId"]),
                    item["name"],
                    item["kind"],
                    tuple(item["contextSources"]),
                )
                if not isinstance(item["isPrimary"], bool):
                    raise TypeError
                is_primary = item["isPrimary"]
                if is_primary:
                    if primary_id is not None:
                        raise TypeError
                    primary_id = str(repository.repository_id)
                repositories.append(repository)
                memberships.append(
                    ProjectRepository(
                        project.project_id, repository.repository_id, is_primary
                    )
                )
                mapped = checkout_paths.get(str(repository.repository_id))
                if mapped is None:
                    continue
                classified = _classification(
                    current,
                    mapped,
                    "directory"
                    if repository.kind is RepositoryKind.DIRECTORY
                    else "git",
                )
                if classified.kind == "known_checkout":
                    existing = _checkout(current, str(classified.checkout_id))
                    if existing.repository_id != repository.repository_id:
                        raise CatalogError(
                            "project_import_identity_conflict",
                            "A mapped checkout belongs to another repository identity.",
                        )
                    continue
                if repository.kind is RepositoryKind.GIT:
                    valid_new = classified.kind == "new_git_repository" or (
                        classified.kind == "new_checkout"
                        and classified.repository_id == str(repository.repository_id)
                    )
                    if not valid_new:
                        raise CatalogError(
                            "project_import_checkout_invalid",
                            "A mapped Git checkout is invalid.",
                        )
                checkouts.append(
                    Checkout(
                        self.checkout_id_factory(),
                        repository.repository_id,
                        self.host_id,
                        classified.path,
                        kind=classified.checkout_kind,
                        display_name=classified.suggested_name,
                        is_default=True,
                    )
                )
        except CatalogError:
            raise
        except (KeyError, TypeError, ValidationError, ValueError) as error:
            raise CatalogError(
                "project_export_invalid", "Project export is invalid."
            ) from error
        repository_ids = {str(item.repository_id) for item in repositories}
        if set(checkout_paths) - repository_ids:
            raise CatalogError(
                "project_import_mapping_invalid",
                "A checkout path maps an unknown repository identity.",
            )
        if primary_id is None:
            raise CatalogError(
                "project_export_invalid", "Project export has no primary repository."
            )
        if primary_id not in checkout_paths:
            raise CatalogError(
                "project_import_primary_checkout_required",
                "Import requires a local checkout for the primary repository.",
            )

        def transform(config: SwitchboardConfig) -> SwitchboardConfig:
            projects = list(config.projects)
            local_repositories = list(config.repositories)
            local_memberships = list(config.project_repositories)
            local_checkouts = [
                replace(item, is_default=False)
                if any(
                    candidate.is_default
                    and candidate.repository_id == item.repository_id
                    for candidate in checkouts
                )
                else item
                for item in config.checkouts
            ]
            _append_identity(
                projects,
                project,
                identity=lambda item: item.project_id,
                label="project",
            )
            for repository in repositories:
                _append_identity(
                    local_repositories,
                    repository,
                    identity=lambda item: item.repository_id,
                    label="repository",
                )
            for membership in memberships:
                _append_identity(
                    local_memberships,
                    membership,
                    identity=lambda item: (item.project_id, item.repository_id),
                    label="repository membership",
                )
            for checkout in checkouts:
                _append_identity(
                    local_checkouts,
                    checkout,
                    identity=lambda item: item.checkout_id,
                    label="checkout",
                )
            return _catalog(
                config,
                projects=tuple(projects),
                repositories=tuple(local_repositories),
                memberships=tuple(local_memberships),
                checkouts=tuple(local_checkouts),
            )

        identities = {"projectId": str(project.project_id)}
        return self._apply(
            "project-import",
            transform,
            identities=identities,
            dry_run=dry_run,
        )

    def document(
        self,
        *,
        include_archived: bool,
        mutation: CatalogMutation | None = None,
    ) -> dict[str, object]:
        self._read()
        references = self._references()
        projects = []
        for project in self.registry.list_projects(include_undeclared=include_archived):
            if not include_archived and not bool(project["declared"]):
                continue
            repositories = []
            for repository in project["repositories"]:
                checkouts = []
                for checkout in repository["checkouts"]:
                    if checkout["host_id"] != str(self.host_id):
                        continue
                    if not include_archived and not bool(checkout["declared"]):
                        continue
                    checkouts.append(
                        {
                            "checkoutId": checkout["checkout_id"],
                            "path": checkout["path"],
                            "kind": checkout["kind"],
                            "displayName": checkout["display_name"],
                            "providerOverride": checkout["provider_override"],
                            "transportOverride": checkout["transport_override"],
                            "isDefault": bool(checkout["is_default"]),
                            "declared": bool(checkout["declared"]),
                            "present": bool(checkout["present"]),
                            "branch": checkout["branch"],
                            "headOid": checkout["head_oid"],
                            "references": references["checkouts"].get(
                                checkout["checkout_id"], {}
                            ),
                        }
                    )
                repositories.append(
                    {
                        "repositoryId": repository["repository_id"],
                        "name": repository["name"],
                        "kind": repository["kind"],
                        "isPrimary": bool(repository["is_primary"]),
                        "declared": bool(repository["declared"]),
                        "contextSources": repository["context_sources"],
                        "references": references["repositories"].get(
                            repository["repository_id"], {}
                        ),
                        "checkouts": checkouts,
                    }
                )
            projects.append(
                {
                    "projectId": project["project_id"],
                    "name": project["name"],
                    "aliases": project["aliases"],
                    "defaultProvider": project["default_provider"],
                    "defaultTransport": project["default_transport"],
                    "declared": bool(project["declared"]),
                    "references": references["projects"].get(project["project_id"], {}),
                    "repositories": repositories,
                }
            )
        if len(projects) > MAX_CATALOG_ITEMS:
            raise CatalogError(
                "catalog_record_limit", "The catalog exceeds the record limit."
            )
        operation: dict[str, object] | None = None
        if mutation is not None:
            operation = {
                "name": mutation.edit.operation,
                "changed": mutation.edit.changed,
                "dryRun": mutation.edit.dry_run,
                "beforeHash": mutation.edit.before_hash,
                "afterHash": mutation.edit.after_hash,
                "backupPath": (
                    None
                    if mutation.edit.backup_path is None
                    else str(mutation.edit.backup_path)
                ),
                "identities": mutation.identities,
            }
        document = {
            "schemaVersion": 2,
            "protocolVersion": 2,
            "catalogVersion": CATALOG_VERSION,
            "generatedAt": self.now_ms(),
            "hostId": str(self.host_id),
            "operation": operation,
            "projects": projects,
        }
        encoded = json.dumps(
            document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if len(encoded) > 8 * 1024 * 1024:
            raise CatalogError(
                "catalog_size_limit", "The catalog exceeds the size limit."
            )
        return document


def catalog_json(document: dict[str, object]) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "CATALOG_VERSION",
    "PROJECT_EXPORT_VERSION",
    "CatalogManager",
    "CatalogMutation",
    "catalog_json",
]
