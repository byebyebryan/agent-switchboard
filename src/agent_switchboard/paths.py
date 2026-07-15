"""XDG path resolution and stable local host identity."""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from .domain import HostId, ValidationError

APP_DIR = "agent-switchboard"


def _home(home: str | Path | None) -> Path:
    return Path(home).expanduser() if home is not None else Path.home()


def _xdg_dir(
    variable: str,
    fallback: str,
    *,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> Path:
    environ = os.environ if environ is None else environ
    configured = environ.get(variable)
    if configured:
        path = Path(configured)
        if path.is_absolute():
            return path
    return _home(home) / fallback


def config_home(
    *, environ: Mapping[str, str] | None = None, home: str | Path | None = None
) -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", ".config", environ=environ, home=home)


def state_home(
    *, environ: Mapping[str, str] | None = None, home: str | Path | None = None
) -> Path:
    return _xdg_dir("XDG_STATE_HOME", ".local/state", environ=environ, home=home)


def config_path(
    *, environ: Mapping[str, str] | None = None, home: str | Path | None = None
) -> Path:
    return config_home(environ=environ, home=home) / APP_DIR / "config.toml"


def host_id_path(
    *, environ: Mapping[str, str] | None = None, home: str | Path | None = None
) -> Path:
    return state_home(environ=environ, home=home) / APP_DIR / "host-id"


def database_path(
    *, environ: Mapping[str, str] | None = None, home: str | Path | None = None
) -> Path:
    """Return the documented host-local SQLite registry path."""

    return state_home(environ=environ, home=home) / APP_DIR / "switchboard.db"


def _read_host_id(path: Path) -> HostId:
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise ValidationError(f"cannot read host ID at {path}: {exc}") from exc
    host_id = HostId(value)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            path.chmod(0o600)
    except OSError as exc:
        raise ValidationError(f"cannot secure host ID at {path}: {exc}") from exc
    return host_id


def load_or_create_host_id(path: str | Path | None = None) -> HostId:
    """Read or atomically publish one stable host UUID with mode ``0600``."""

    destination = Path(path) if path is not None else host_id_path()
    try:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ValidationError(f"cannot create state directory: {exc}") from exc
    if destination.exists():
        return _read_host_id(destination)

    candidate = HostId.new()
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        payload = f"{candidate}\n".encode("ascii")
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        with suppress(FileExistsError):
            os.link(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise ValidationError(f"cannot create host ID at {destination}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()
    return _read_host_id(destination)
