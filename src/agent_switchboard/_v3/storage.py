"""Secure SQLite connection gate for the private Phase 6 registry."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from .domain import ActivationState, GenerationId, HostId, bounded_text
from .migrations import migrate

DEFAULT_BUSY_TIMEOUT_MS: Final = 5_000
MAX_BUSY_TIMEOUT_MS: Final = 30_000


class StorageError(RuntimeError):
    """Base Phase 6 registry error."""


class RegistryClosed(StorageError):
    """An operation was attempted after closing the registry."""


def _database_path(path: str | os.PathLike[str]) -> str:
    value = os.fspath(path)
    if value == ":memory:":
        return value
    if value.startswith("file:"):
        raise StorageError("SQLite URI database paths are not supported")
    return str(Path(value))


def _secure_database_file(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _secure_sidecars(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.chmod(0o600)
        except FileNotFoundError:
            continue


def connect_database(
    path: str | os.PathLike[str],
    *,
    generation_id: GenerationId,
    local_host_id: HostId,
    local_display_name: str,
    initial_activation_state: ActivationState = ActivationState.CUTOVER_STAGED,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    now: int | None = None,
) -> sqlite3.Connection:
    """Open an exact Phase 6 generation or initialize an empty database."""

    if not 1 <= busy_timeout_ms <= MAX_BUSY_TIMEOUT_MS:
        raise ValueError(f"busy_timeout_ms must be between 1 and {MAX_BUSY_TIMEOUT_MS}")
    if not isinstance(generation_id, GenerationId):
        generation_id = GenerationId(generation_id)
    if not isinstance(local_host_id, HostId):
        local_host_id = HostId(local_host_id)
    local_display_name = bounded_text(
        local_display_name,
        "local_display_name",
        maximum=256,
    )
    database = _database_path(path)
    file_database = database != ":memory:"
    if file_database:
        _secure_database_file(Path(database))
    connection = sqlite3.connect(
        database,
        timeout=busy_timeout_ms / 1_000,
        isolation_level=None,
        uri=False,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        migrate(
            connection,
            generation_id=generation_id,
            local_host_id=local_host_id,
            local_display_name=local_display_name,
            initial_activation_state=initial_activation_state,
            now=now,
        )
        if file_database:
            _secure_sidecars(Path(database))
        return connection
    except BaseException:
        connection.close()
        raise


class Registry:
    """One synchronous Phase 6 registry connection."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        generation_id: GenerationId,
        local_host_id: HostId,
        local_display_name: str,
        initial_activation_state: ActivationState = ActivationState.CUTOVER_STAGED,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        now: int | None = None,
    ) -> None:
        self._connection: sqlite3.Connection | None = connect_database(
            path,
            generation_id=generation_id,
            local_host_id=local_host_id,
            local_display_name=local_display_name,
            initial_activation_state=initial_activation_state,
            busy_timeout_ms=busy_timeout_ms,
            now=now,
        )

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RegistryClosed("registry is closed")
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Registry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connection
        if connection.in_transaction:
            raise StorageError("nested Registry transactions are not supported")
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def metadata(self) -> dict[str, object]:
        row = self.connection.execute(
            "SELECT * FROM registry_metadata WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise StorageError("registry metadata is missing")
        return dict(zip(row.keys(), row, strict=True))


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "MAX_BUSY_TIMEOUT_MS",
    "Registry",
    "RegistryClosed",
    "StorageError",
    "connect_database",
]
