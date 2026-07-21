"""Authoritative project-catalog inspection and atomic config mutation."""

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .config import SwitchboardConfig, parse_config, render_config
from .domain import CheckoutKind, HostId, RepositoryKind, ValidationError
from .paths import APP_DIR, config_path, state_home
from .repository_discovery import RepositoryDiscoveryError, probe_git_repository

MAX_CONFIG_BYTES: Final = 1024 * 1024
MAX_INSPECTED_REPOSITORIES: Final = 256


class CatalogError(ValidationError):
    """A catalog operation could not be completed safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PathClassification:
    kind: str
    path: Path
    checkout_kind: CheckoutKind
    suggested_name: str
    project_ids: tuple[str, ...] = ()
    repository_id: str | None = None
    checkout_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": str(self.path),
            "checkoutKind": self.checkout_kind.value,
            "suggestedName": self.suggested_name,
            "projectIds": list(self.project_ids),
            "repositoryId": self.repository_id,
            "checkoutId": self.checkout_id,
        }


@dataclass(frozen=True, slots=True)
class CatalogEditResult:
    operation: str
    changed: bool
    dry_run: bool
    before_hash: str
    after_hash: str
    backup_path: Path | None
    config: SwitchboardConfig


CatalogTransform = Callable[[SwitchboardConfig], SwitchboardConfig]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _project_ids_for_repository(
    config: SwitchboardConfig, repository_id: str
) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(membership.project_id)
            for membership in config.project_repositories
            if str(membership.repository_id) == repository_id
        )
    )


def inspect_path(config: SwitchboardConfig, value: str | Path) -> PathClassification:
    """Classify one existing directory without changing it or its repository."""

    try:
        requested = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CatalogError(
            "catalog_path_unavailable", "The selected path is unavailable."
        ) from error
    if not requested.is_dir():
        raise CatalogError(
            "catalog_path_not_directory", "The selected path is not a directory."
        )

    directory_matches = sorted(
        (
            checkout
            for checkout in config.checkouts
            if checkout.kind is CheckoutKind.DIRECTORY
            and (requested == checkout.path or checkout.path in requested.parents)
        ),
        key=lambda checkout: (-len(checkout.path.parts), str(checkout.checkout_id)),
    )
    if directory_matches:
        checkout = directory_matches[0]
        repository_id = str(checkout.repository_id)
        return PathClassification(
            "known_checkout",
            checkout.path,
            checkout.kind,
            checkout.display_name or checkout.path.name,
            _project_ids_for_repository(config, repository_id),
            repository_id,
            str(checkout.checkout_id),
        )

    try:
        observation = probe_git_repository(requested)
    except RepositoryDiscoveryError as error:
        if error.code != "git_probe_failed":
            raise CatalogError(error.code, str(error)) from error
        return PathClassification(
            "directory",
            requested,
            CheckoutKind.DIRECTORY,
            requested.name,
        )

    selected = next(
        (
            checkout
            for checkout in observation.checkouts
            if requested == checkout.path or checkout.path in requested.parents
        ),
        None,
    )
    if selected is None:
        raise CatalogError(
            "git_checkout_missing",
            "Git did not report the selected checkout in its worktree set.",
        )

    for checkout in config.checkouts:
        if checkout.path != selected.path:
            continue
        repository_id = str(checkout.repository_id)
        return PathClassification(
            "known_checkout",
            selected.path,
            CheckoutKind(selected.kind),
            checkout.display_name or selected.path.name,
            _project_ids_for_repository(config, repository_id),
            repository_id,
            str(checkout.checkout_id),
        )

    git_repositories = [
        repository
        for repository in config.repositories
        if repository.kind is RepositoryKind.GIT
    ]
    if len(git_repositories) > MAX_INSPECTED_REPOSITORIES:
        raise CatalogError(
            "catalog_repository_limit",
            "The configured repository count exceeds the path-inspection limit.",
        )
    configured_by_repository = {
        str(repository.repository_id): sorted(
            (
                checkout
                for checkout in config.checkouts
                if checkout.repository_id == repository.repository_id
            ),
            key=lambda checkout: (not checkout.is_default, str(checkout.checkout_id)),
        )
        for repository in git_repositories
    }
    for repository in git_repositories:
        repository_id = str(repository.repository_id)
        candidates = configured_by_repository[repository_id]
        if not candidates:
            continue
        try:
            configured = probe_git_repository(candidates[0].path)
        except RepositoryDiscoveryError:
            continue
        if configured.git_common_dir != observation.git_common_dir:
            continue
        return PathClassification(
            "new_checkout",
            selected.path,
            CheckoutKind(selected.kind),
            selected.path.name,
            _project_ids_for_repository(config, repository_id),
            repository_id,
        )

    return PathClassification(
        "new_git_repository",
        selected.path,
        CheckoutKind(selected.kind),
        selected.path.name,
    )


def _secure_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise CatalogError(
            "catalog_config_directory_invalid",
            "The configuration directory is unavailable.",
        ) from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise CatalogError(
            "catalog_config_directory_unsafe",
            "The configuration directory must be a user-owned non-writable directory.",
        )


def _read_source(path: Path) -> tuple[bytes, bool]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return b"", False
    except OSError as error:
        raise CatalogError(
            "catalog_config_unreadable", "The configuration file cannot be read."
        ) from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise CatalogError(
            "catalog_config_unsafe",
            "The configuration file must be a private user-owned regular file.",
        )
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise CatalogError(
            "catalog_config_unreadable", "The configuration file cannot be read."
        ) from error
    if len(payload) > MAX_CONFIG_BYTES:
        raise CatalogError(
            "catalog_config_too_large", "The configuration file is too large."
        )
    return payload, True


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


class CatalogEditor:
    """Serialize cooperating writers and atomically replace canonical config."""

    def __init__(
        self,
        *,
        host_id: HostId,
        path: str | Path | None = None,
        backup_root: str | Path | None = None,
        now_ns: Callable[[], int] = time.time_ns,
        token_hex: Callable[[int], str] = secrets.token_hex,
    ) -> None:
        self.host_id = host_id
        self.path = Path(path) if path is not None else config_path()
        self.backup_root = (
            Path(backup_root)
            if backup_root is not None
            else state_home() / APP_DIR / "config-backups"
        )
        self.now_ns = now_ns
        self.token_hex = token_hex

    def apply(
        self,
        operation: str,
        transform: CatalogTransform,
        *,
        dry_run: bool = False,
    ) -> CatalogEditResult:
        _secure_directory(self.path.parent)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        lock_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_fd = os.open(lock_path, lock_flags, 0o600)
        except OSError as error:
            raise CatalogError(
                "catalog_config_lock_failed",
                "The configuration lock could not be opened.",
            ) from error
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            original, existed = _read_source(self.path)
            before_hash = _sha256(original)
            try:
                current = parse_config(original, host_id=self.host_id)
                candidate = transform(current)
                if not isinstance(candidate, SwitchboardConfig):
                    raise TypeError("catalog transform returned an invalid config")
                rendered = render_config(candidate).encode("utf-8")
                validated = parse_config(rendered, host_id=self.host_id)
            except CatalogError:
                raise
            except (OSError, TypeError, ValidationError, ValueError) as error:
                raise CatalogError("catalog_candidate_invalid", str(error)) from error
            if len(rendered) > MAX_CONFIG_BYTES:
                raise CatalogError(
                    "catalog_config_too_large",
                    "The updated configuration exceeds the size limit.",
                )
            after_hash = _sha256(rendered)
            if validated == current:
                return CatalogEditResult(
                    operation,
                    False,
                    dry_run,
                    before_hash,
                    before_hash,
                    None,
                    current,
                )
            if dry_run:
                return CatalogEditResult(
                    operation,
                    True,
                    True,
                    before_hash,
                    after_hash,
                    None,
                    validated,
                )

            latest, latest_existed = _read_source(self.path)
            if latest_existed != existed or latest != original:
                raise CatalogError(
                    "catalog_config_changed",
                    "The configuration changed while the catalog edit was prepared.",
                )
            _secure_directory(self.backup_root)
            backup = self.backup_root / (f"{self.now_ns()}-{before_hash[:16]}.toml")
            try:
                _write_exclusive(backup, original)
            except OSError as error:
                raise CatalogError(
                    "catalog_backup_failed",
                    "The prior configuration could not be backed up.",
                ) from error

            temporary = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{self.token_hex(8)}.tmp"
            )
            try:
                _write_exclusive(temporary, rendered)
                latest, latest_existed = _read_source(self.path)
                if latest_existed != existed or latest != original:
                    raise CatalogError(
                        "catalog_config_changed",
                        "The configuration changed before publication.",
                    )
                os.replace(temporary, self.path)
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except CatalogError:
                raise
            except OSError as error:
                raise CatalogError(
                    "catalog_config_write_failed",
                    "The updated configuration could not be published.",
                ) from error
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            return CatalogEditResult(
                operation,
                True,
                False,
                before_hash,
                after_hash,
                backup,
                validated,
            )
        finally:
            os.close(lock_fd)


__all__ = [
    "CatalogEditResult",
    "CatalogEditor",
    "CatalogError",
    "PathClassification",
    "inspect_path",
]
