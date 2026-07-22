"""Atomic ownership-safe installation of the trusted provider hook."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

HOOK_EVENTS: Final = (
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
)
STATUS_MESSAGE: Final = "Agent Switchboard 0.3: trusted transition hook"
MAX_FILE_BYTES: Final = 8 * 1024 * 1024


class HookConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HookEditResult:
    path: Path
    changed: bool
    removed_handlers: int
    installed_handlers: int
    dry_run: bool


def _path(provider: str, environment: Mapping[str, str]) -> Path:
    home = environment.get("HOME")
    if not home or not Path(home).is_absolute():
        raise HookConfigError("HOME must be an absolute path")
    if provider == "codex":
        root = Path(environment.get("CODEX_HOME", Path(home) / ".codex"))
        filename = "hooks.json"
    elif provider == "claude":
        root = Path(environment.get("CLAUDE_CONFIG_DIR", Path(home) / ".claude"))
        filename = "settings.json"
    else:
        raise HookConfigError("provider is unsupported")
    if not root.is_absolute() or root == root.parent:
        raise HookConfigError(
            "provider configuration root must be an absolute directory"
        )
    return root / filename


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HookConfigError("hook configuration repeats an object key")
        result[key] = value
    return result


def _read_all(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _load(directory: int, filename: str) -> dict[str, Any]:
    try:
        descriptor = os.open(
            filename,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory,
        )
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise HookConfigError("hook configuration is unsafe") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE_BYTES:
            raise HookConfigError("hook configuration is not a bounded regular file")
        raw = _read_all(descriptor, metadata.st_size)
        if len(raw) != metadata.st_size:
            raise HookConfigError("hook configuration changed while it was read")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HookConfigError("hook configuration is invalid JSON") from error
    if not isinstance(value, dict):
        raise HookConfigError("hook configuration must be one object")
    hooks = value.get("hooks", {})
    if not isinstance(hooks, dict):
        raise HookConfigError("hook configuration hooks must be an object")
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            raise HookConfigError("hook groups are malformed")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise HookConfigError("hook group is malformed")
            if not all(isinstance(handler, dict) for handler in group["hooks"]):
                raise HookConfigError("hook handler is malformed")
    return value


def _owned(handler: object, provider: str) -> bool:
    if not isinstance(handler, dict) or handler.get("statusMessage") != STATUS_MESSAGE:
        return False
    if provider == "codex":
        command = handler.get("command")
        if not isinstance(command, str):
            return False
        try:
            argv = shlex.split(command)
        except ValueError:
            return False
        return (
            len(argv) == 4
            and Path(argv[0]).name == "swbctl"
            and argv[1:] == ["hook", "--provider", "codex"]
        )
    return (
        handler.get("type") == "command"
        and isinstance(handler.get("command"), str)
        and Path(handler["command"]).name == "swbctl"
        and handler.get("args") == ["hook", "--provider", "claude"]
    )


def _handler(provider: str, executable: Path, timeout: int) -> dict[str, Any]:
    if provider == "codex":
        return {
            "type": "command",
            "command": shlex.join((str(executable), "hook", "--provider", "codex")),
            "timeout": timeout,
            "statusMessage": STATUS_MESSAGE,
        }
    return {
        "type": "command",
        "command": str(executable),
        "args": ["hook", "--provider", "claude"],
        "timeout": timeout,
        "statusMessage": STATUS_MESSAGE,
    }


def _edit_document(
    document: dict[str, Any],
    *,
    operation: str,
    provider: str,
    executable: Path,
    timeout: int,
) -> tuple[dict[str, Any], int, int]:
    result = json.loads(json.dumps(document))
    hooks = result.setdefault("hooks", {})
    removed = 0
    for event in list(hooks):
        retained_groups: list[dict[str, Any]] = []
        for group in hooks[event]:
            retained = [item for item in group["hooks"] if not _owned(item, provider)]
            removed += len(group["hooks"]) - len(retained)
            if retained:
                copied = dict(group)
                copied["hooks"] = retained
                retained_groups.append(copied)
        if retained_groups:
            hooks[event] = retained_groups
        else:
            del hooks[event]
    installed = 0
    if operation == "install":
        handler = _handler(provider, executable, timeout)
        for event in HOOK_EVENTS:
            group: dict[str, Any] = {"hooks": [dict(handler)]}
            if event == "SessionStart":
                group["matcher"] = "^(startup|resume|clear|compact)$"
            elif event in {"PermissionRequest", "PostToolUse"}:
                group["matcher"] = ".*"
            hooks.setdefault(event, []).append(group)
            installed += 1
    if not hooks:
        result.pop("hooks", None)
    return result, removed, installed


def _write_atomic(directory: int, filename: str, document: dict[str, Any]) -> None:
    temporary = f".{filename}.{os.getpid()}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        payload = (
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - defensive OS boundary
                raise HookConfigError("hook configuration write was incomplete")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(
            temporary,
            filename,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.chmod(filename, 0o600, dir_fd=directory, follow_symlinks=False)
        os.fsync(directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def _open_directory(path: Path) -> int:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        return os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
    except OSError as error:
        raise HookConfigError("hook configuration directory is unsafe") from error


def edit_hooks(
    operation: str,
    provider: str,
    *,
    executable: Path,
    timeout_seconds: int,
    dry_run: bool = False,
    environment: Mapping[str, str] | None = None,
) -> HookEditResult:
    if operation not in {"install", "uninstall"}:
        raise HookConfigError("hook operation is unsupported")
    executable = Path(os.path.abspath(executable))
    try:
        target = executable.resolve(strict=True)
    except OSError as error:
        raise HookConfigError("swbctl executable is not runnable") from error
    if not target.is_file() or not os.access(executable, os.X_OK):
        raise HookConfigError("swbctl executable is not runnable")
    if not 1 <= timeout_seconds <= 30:
        raise HookConfigError("hook timeout is outside bounds")
    environment = os.environ if environment is None else environment
    path = _path(provider, environment)
    lock_name = f".{path.name}.agent-switchboard.lock"
    if dry_run and not path.parent.exists():
        current = {}
        updated, removed, installed = _edit_document(
            current,
            operation=operation,
            provider=provider,
            executable=executable,
            timeout=timeout_seconds,
        )
        return HookEditResult(path, updated != current, removed, installed, True)
    directory = _open_directory(path.parent)
    try:
        try:
            lock = os.open(
                lock_name,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
        except OSError as error:
            raise HookConfigError("hook configuration lock is unsafe") from error
        try:
            os.fchmod(lock, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = _load(directory, path.name)
            updated, removed, installed = _edit_document(
                current,
                operation=operation,
                provider=provider,
                executable=executable,
                timeout=timeout_seconds,
            )
            changed = updated != current
            if changed and not dry_run:
                _write_atomic(directory, path.name, updated)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
    finally:
        os.close(directory)
    return HookEditResult(path, changed, removed, installed, dry_run)


__all__ = ["HookConfigError", "HookEditResult", "edit_hooks"]
