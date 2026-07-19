"""Explicit, ownership-safe Codex hook configuration management."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import secrets
import shlex
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .config import ConfigError
from .executable import resolve_swbctl_executable
from .providers.claude import claude_settings_path

HOOK_STATUS_MESSAGE: Final = "Agent Switchboard: tracking session"
HOOK_EVENTS: Final = (
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
)
CLAUDE_HOOK_STATUS_MESSAGE: Final = "Agent Switchboard: tracking Claude session"
CLAUDE_HOOK_EVENTS: Final = (*HOOK_EVENTS, "SessionEnd")
APP_SERVER_EVENT_NAMES: Final = {
    "SessionStart": "sessionStart",
    "UserPromptSubmit": "userPromptSubmit",
    "PermissionRequest": "permissionRequest",
    "PostToolUse": "postToolUse",
    "Stop": "stop",
}
_MATCHERS: Final = {
    "SessionStart": "^(startup|resume|clear|compact)$",
    "PermissionRequest": ".*",
    "PostToolUse": ".*",
}
_MAX_HOOKS_FILE_BYTES: Final = 8 * 1024 * 1024
_MAX_JSON_NODES: Final = 100_000
_LOCK_FILENAME: Final = ".hooks.json.agent-switchboard.lock"
_MAX_LOCK_BYTES: Final = 4096


class HookConfigError(ConfigError):
    """Codex hook configuration cannot be managed safely."""


@dataclass(frozen=True, slots=True)
class HookEditResult:
    path: Path
    changed: bool
    removed_handlers: int
    installed_handlers: int
    dry_run: bool


@dataclass(frozen=True, slots=True)
class ClaudeHookCandidate:
    event: str
    handler_type: str | None
    command: str | None
    args: tuple[str, ...] | None
    matcher: str | None
    timeout_seconds: int | None
    status_message: str | None


@dataclass(frozen=True, slots=True)
class ClaudeHooksInspection:
    path: Path
    candidates: tuple[ClaudeHookCandidate, ...]


@dataclass(frozen=True, slots=True)
class _FileToken:
    directory_identity: tuple[int, int] | None
    file_identity: tuple[int, int, int, int, int, str] | None


@dataclass(frozen=True, slots=True)
class _LoadedDocument:
    document: dict[str, Any]
    token: _FileToken
    mode: int | None
    exists: bool


@dataclass(frozen=True, slots=True)
class _DirectoryHandle:
    parent: int
    directory: int
    name: str
    identity: tuple[int, int]


def codex_home(*, environ: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    configured = environment.get("CODEX_HOME")
    if configured:
        path = Path(configured)
    else:
        configured_home = environment.get("HOME")
        if configured_home is None:
            if environ is not None:
                raise HookConfigError(
                    "an explicit environment requires absolute HOME or CODEX_HOME"
                )
            configured_home = str(Path.home())
        home = Path(configured_home)
        if not home.is_absolute():
            raise HookConfigError("HOME must resolve to an absolute path")
        path = home / ".codex"
    if not path.is_absolute():
        raise HookConfigError("CODEX_HOME must resolve to an absolute path")
    if path == path.parent:
        raise HookConfigError("CODEX_HOME must not be the filesystem root")
    return path


def hook_command(executable: str | Path) -> str:
    path = Path(executable)
    if not path.is_absolute():
        raise HookConfigError("the swbctl hook command must use an absolute path")
    return shlex.join((str(path), "event", "--provider", "codex"))


def canonical_hook_groups(
    executable: str | Path,
    *,
    timeout_seconds: int,
) -> dict[str, dict[str, Any]]:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise HookConfigError("hook timeout must be between 1 and 60 seconds")
    command = hook_command(executable)
    result: dict[str, dict[str, Any]] = {}
    for event in HOOK_EVENTS:
        group: dict[str, Any] = {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": timeout_seconds,
                    "statusMessage": HOOK_STATUS_MESSAGE,
                }
            ]
        }
        matcher = _MATCHERS.get(event)
        if matcher is not None:
            group["matcher"] = matcher
        result[event] = group
    return result


def canonical_claude_hook_groups(
    executable: str | Path,
    *,
    timeout_seconds: int,
) -> dict[str, dict[str, Any]]:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise HookConfigError("hook timeout must be between 1 and 60 seconds")
    path = Path(executable)
    if not path.is_absolute():
        raise HookConfigError("the swbctl hook command must use an absolute path")
    result: dict[str, dict[str, Any]] = {}
    for event in CLAUDE_HOOK_EVENTS:
        result[event] = {
            "hooks": [
                {
                    "type": "command",
                    "command": str(path),
                    "args": ["event", "--provider", "claude"],
                    "timeout": timeout_seconds,
                    "statusMessage": CLAUDE_HOOK_STATUS_MESSAGE,
                }
            ]
        }
    return result


def command_argv(handler: Mapping[str, Any]) -> tuple[str, ...] | None:
    if handler.get("type") != "command" or not isinstance(handler.get("command"), str):
        return None
    try:
        argv = tuple(shlex.split(handler["command"]))
    except ValueError:
        return None
    return argv


def is_switchboard_handler(handler: Mapping[str, Any]) -> bool:
    """Recognize only definitions that Switchboard intentionally owns."""

    argv = command_argv(handler)
    return bool(
        argv is not None
        and len(argv) == 4
        and argv[1:] == ("event", "--provider", "codex")
        and Path(argv[0]).name == "swbctl"
        and handler.get("statusMessage") == HOOK_STATUS_MESSAGE
    )


def is_claude_switchboard_handler(handler: Mapping[str, Any]) -> bool:
    """Recognize only Switchboard's exact Claude exec-form handler."""

    command = handler.get("command")
    args = handler.get("args")
    return bool(
        handler.get("type") == "command"
        and isinstance(command, str)
        and Path(command).is_absolute()
        and Path(command).name == "swbctl"
        and args == ["event", "--provider", "claude"]
        and handler.get("statusMessage") == CLAUDE_HOOK_STATUS_MESSAGE
    )


def _validate_tree(value: object) -> None:
    stack = [value]
    count = 0
    while stack:
        current = stack.pop()
        count += 1
        if count > _MAX_JSON_NODES:
            raise HookConfigError("Codex hooks configuration is too complex")
        if current is None or isinstance(current, (bool, int, float, str)):
            continue
        if isinstance(current, list):
            stack.extend(current)
            continue
        if isinstance(current, dict):
            if not all(isinstance(key, str) for key in current):
                raise HookConfigError("Codex hooks configuration has a non-string key")
            stack.extend(current.values())
            continue
        raise HookConfigError("Codex hooks configuration is not valid JSON")


def _validate_document(value: object) -> dict[str, Any]:
    _validate_tree(value)
    if not isinstance(value, dict):
        raise HookConfigError("Codex hooks configuration must be a JSON object")
    if "hooks" not in value:
        return value
    hooks = value["hooks"]
    if not isinstance(hooks, dict):
        raise HookConfigError("Codex hooks configuration 'hooks' must be an object")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise HookConfigError(f"Codex hook event {event!r} must be an array")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise HookConfigError(
                    f"Codex hook event {event!r} contains an invalid matcher group"
                )
            matcher = group.get("matcher")
            if matcher is not None:
                _raw_hook_string(matcher, f"{event}.matcher", maximum=4096)
            for handler in group["hooks"]:
                _validate_raw_handler(handler, event)
    return value


def _raw_hook_string(
    value: object, field: str, *, maximum: int, nonempty: bool = False
) -> str:
    if (
        not isinstance(value, str)
        or (nonempty and not value)
        or len(value) > maximum
        or "\x00" in value
    ):
        raise HookConfigError(f"Codex hook field {field!r} must be a bounded string")
    return value


def _validate_raw_handler(value: object, event: str) -> None:
    if not isinstance(value, dict):
        raise HookConfigError(f"Codex hook event {event!r} has a non-object handler")
    handler_type = value.get("type")
    if handler_type not in {"command", "prompt", "agent"}:
        raise HookConfigError(
            f"Codex hook event {event!r} has an unsupported handler type"
        )
    if handler_type == "command":
        _raw_hook_string(
            value.get("command"),
            f"{event}.command",
            maximum=16_384,
            nonempty=True,
        )
        args = value.get("args")
        if args is not None:
            if not isinstance(args, list) or len(args) > 256:
                raise HookConfigError(
                    f"Codex hook field {event!r}.args must be a bounded array"
                )
            for index, argument in enumerate(args):
                _raw_hook_string(
                    argument,
                    f"{event}.args[{index}]",
                    maximum=16_384,
                )
    for field in ("commandWindows", "statusMessage"):
        field_value = value.get(field)
        if field_value is not None:
            _raw_hook_string(field_value, f"{event}.{field}", maximum=16_384)
    timeout = value.get("timeout")
    if timeout is not None and (type(timeout) is not int or not 0 <= timeout <= 86_400):
        raise HookConfigError(
            f"Codex hook field {event!r}.timeout must be an integer from 0 to 86400"
        )
    asynchronous = value.get("async")
    if asynchronous is not None and type(asynchronous) is not bool:
        raise HookConfigError(f"Codex hook field {event!r}.async must be a boolean")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HookConfigError(
                f"Codex hooks configuration contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _directory_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise HookConfigError("Codex hooks parent must be a directory")
    return metadata.st_dev, metadata.st_ino


def _assert_directory_binding(handle: _DirectoryHandle) -> None:
    try:
        metadata = os.stat(
            handle.name,
            dir_fd=handle.parent,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise HookConfigError("CODEX_HOME changed during hook management") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != handle.identity
    ):
        raise HookConfigError("CODEX_HOME changed during hook management")


def _open_codex_directory(path: Path, *, create: bool) -> _DirectoryHandle | None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        if create:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = os.open(path.parent, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HookConfigError("cannot safely open the CODEX_HOME parent") from exc

    directory = -1
    try:
        try:
            directory = os.open(path.name, flags, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                with suppress(OSError):
                    os.close(parent)
                return None
            # A concurrent Switchboard process may have created CODEX_HOME.
            # The no-follow open below still refuses path substitution.
            with suppress(FileExistsError):
                os.mkdir(path.name, 0o700, dir_fd=parent)
            directory = os.open(path.name, flags, dir_fd=parent)
        identity = _directory_identity(directory)
        handle = _DirectoryHandle(parent, directory, path.name, identity)
        _assert_directory_binding(handle)
        return handle
    except OSError as exc:
        if directory >= 0:
            with suppress(OSError):
                os.close(directory)
        with suppress(OSError):
            os.close(parent)
        if exc.errno == errno.ELOOP:
            raise HookConfigError("CODEX_HOME must not be a symbolic link") from exc
        raise HookConfigError("cannot safely open CODEX_HOME") from exc
    except HookConfigError:
        if directory >= 0:
            with suppress(OSError):
                os.close(directory)
        with suppress(OSError):
            os.close(parent)
        raise


def _close_codex_directory(handle: _DirectoryHandle) -> None:
    with suppress(OSError):
        os.close(handle.directory)
    with suppress(OSError):
        os.close(handle.parent)


def _assert_transaction_lock(directory: int, descriptor: int) -> None:
    try:
        metadata = os.fstat(descriptor)
        binding = os.stat(
            _LOCK_FILENAME,
            dir_fd=directory,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise HookConfigError("hook transaction lock changed while opening") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > _MAX_LOCK_BYTES
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not stat.S_ISREG(binding.st_mode)
        or (binding.st_dev, binding.st_ino) != (metadata.st_dev, metadata.st_ino)
    ):
        raise HookConfigError("hook transaction lock is not a private regular file")


def _open_transaction_lock(directory: int) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(_LOCK_FILENAME, flags, 0o600, dir_fd=directory)
    except OSError as exc:
        raise HookConfigError("cannot safely open the hook transaction lock") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > _MAX_LOCK_BYTES
        ):
            raise HookConfigError("hook transaction lock is not a private regular file")
        os.fchmod(descriptor, 0o600)
        _assert_transaction_lock(directory, descriptor)
        return descriptor
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise


@contextmanager
def _locked_codex_directory(path: Path) -> Iterator[_DirectoryHandle]:
    handle = _open_codex_directory(path, create=True)
    if handle is None:  # pragma: no cover - create=True either opens or raises
        raise HookConfigError("cannot create CODEX_HOME")
    lock = -1
    try:
        lock = _open_transaction_lock(handle.directory)
        fcntl.flock(lock, fcntl.LOCK_EX)
        _assert_transaction_lock(handle.directory, lock)
        _assert_directory_binding(handle)
        yield handle
    except OSError as exc:
        raise HookConfigError("cannot lock Codex hook configuration") from exc
    finally:
        if lock >= 0:
            with suppress(OSError):
                fcntl.flock(lock, fcntl.LOCK_UN)
            with suppress(OSError):
                os.close(lock)
        _close_codex_directory(handle)


def _read_from_directory(
    directory: int,
    name: str,
    *,
    directory_identity: tuple[int, int],
) -> _LoadedDocument:
    flags = os.O_RDONLY | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory)
    except FileNotFoundError:
        return _LoadedDocument({}, _FileToken(directory_identity, None), None, False)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise HookConfigError(
                "Codex hooks path must be a regular file, not a link"
            ) from exc
        raise HookConfigError("cannot safely open Codex hooks configuration") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise HookConfigError("Codex hooks path must be a regular file, not a link")
        if before.st_size > _MAX_HOOKS_FILE_BYTES:
            raise HookConfigError("Codex hooks configuration exceeds 8 MiB")
        content = bytearray()
        while len(content) <= _MAX_HOOKS_FILE_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _MAX_HOOKS_FILE_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > _MAX_HOOKS_FILE_BYTES:
            raise HookConfigError("Codex hooks configuration exceeds 8 MiB")
        after = os.fstat(descriptor)
        version_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        version_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if version_before != version_after or len(content) != after.st_size:
            raise HookConfigError("Codex hooks configuration changed while reading")
        raw = bytes(content)
        value = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except HookConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise HookConfigError("cannot parse Codex hooks configuration") from exc
    finally:
        with suppress(OSError):
            os.close(descriptor)
    document = _validate_document(value)
    identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        hashlib.sha256(raw).hexdigest(),
    )
    return _LoadedDocument(
        document,
        _FileToken(directory_identity, identity),
        stat.S_IMODE(after.st_mode),
        True,
    )


def _load_document(path: Path) -> _LoadedDocument:
    handle = _open_codex_directory(path.parent, create=False)
    if handle is None:
        return _LoadedDocument({}, _FileToken(None, None), None, False)
    try:
        return _read_from_directory(
            handle.directory,
            path.name,
            directory_identity=handle.identity,
        )
    finally:
        _close_codex_directory(handle)


def _remove_owned(
    document: dict[str, Any],
    owned: Callable[[Mapping[str, Any]], bool] = is_switchboard_handler,
) -> int:
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in tuple(hooks):
        groups = hooks[event]
        retained_groups: list[dict[str, Any]] = []
        for group in groups:
            handlers = group["hooks"]
            retained = [handler for handler in handlers if not owned(handler)]
            removed += len(handlers) - len(retained)
            if retained:
                group["hooks"] = retained
                retained_groups.append(group)
        if retained_groups:
            hooks[event] = retained_groups
        else:
            del hooks[event]
    return removed


def _encode(document: dict[str, Any]) -> bytes:
    try:
        payload = (
            json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise HookConfigError("Codex hooks configuration cannot be encoded") from exc
    if len(payload) > _MAX_HOOKS_FILE_BYTES:
        raise HookConfigError("Codex hooks configuration exceeds 8 MiB")
    return payload


def _same_source(expected: _FileToken, actual: _FileToken) -> bool:
    return bool(
        (
            expected.directory_identity is None
            or expected.directory_identity == actual.directory_identity
        )
        and expected.file_identity == actual.file_identity
    )


def _current_document(directory: int, path: Path) -> _LoadedDocument:
    return _read_from_directory(
        directory,
        path.name,
        directory_identity=_directory_identity(directory),
    )


def _temporary_file(directory: int, path: Path) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(128):
        name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        try:
            return os.open(name, flags, 0o600, dir_fd=directory), name
        except FileExistsError:
            continue
    raise HookConfigError("cannot reserve a private Codex hooks temporary file")


def _atomic_write(
    handle: _DirectoryHandle,
    path: Path,
    payload: bytes,
    expected: _FileToken,
    mode: int = 0o600,
) -> None:
    directory = handle.directory
    descriptor = -1
    temporary: str | None = None
    try:
        _assert_directory_binding(handle)
        if not _same_source(expected, _current_document(directory, path).token):
            raise HookConfigError(
                "Codex hooks configuration changed before it could be updated"
            )
        descriptor, temporary = _temporary_file(directory, path)
        os.fchmod(descriptor, mode)
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _assert_directory_binding(handle)
        if not _same_source(expected, _current_document(directory, path).token):
            raise HookConfigError(
                "Codex hooks configuration changed before it could be published"
            )
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        temporary = None
        os.fsync(directory)
    except HookConfigError:
        raise
    except OSError as exc:
        raise HookConfigError(f"cannot update Codex hooks at {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if temporary is not None and directory >= 0:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)


def _prepare_edit(
    action: str,
    *,
    path: Path,
    loaded: _LoadedDocument,
    executable: str | Path,
    timeout_seconds: int,
    dry_run: bool,
    groups: Callable[..., dict[str, dict[str, Any]]] = canonical_hook_groups,
    owned: Callable[[Mapping[str, Any]], bool] = is_switchboard_handler,
    require_private_mode: bool = True,
) -> tuple[HookEditResult, bytes]:
    document = loaded.document
    needs_private_mode = bool(
        require_private_mode and loaded.exists and loaded.mode != 0o600
    )
    before = _encode(document) if loaded.exists else None
    removed = _remove_owned(document, owned)
    installed = 0
    if action == "install":
        hooks = document.setdefault("hooks", {})
        assert isinstance(hooks, dict)
        for event, group in groups(executable, timeout_seconds=timeout_seconds).items():
            hooks.setdefault(event, []).append(group)
            installed += 1
    after = _encode(document)
    changed = (before != after and (action == "install" or removed > 0)) or (
        action == "install" and needs_private_mode
    )
    return HookEditResult(path, changed, removed, installed, dry_run), after


def edit_codex_hooks(
    action: str,
    *,
    executable: str | Path,
    timeout_seconds: int,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> HookEditResult:
    """Install or uninstall only Switchboard-owned user-level Codex hooks."""

    if action not in {"install", "uninstall"}:
        raise HookConfigError("hook action must be install or uninstall")
    path = codex_home(environ=environ) / "hooks.json"
    if dry_run:
        result, _payload = _prepare_edit(
            action,
            path=path,
            loaded=_load_document(path),
            executable=executable,
            timeout_seconds=timeout_seconds,
            dry_run=True,
        )
        return result

    with _locked_codex_directory(path.parent) as handle:
        loaded = _read_from_directory(
            handle.directory,
            path.name,
            directory_identity=handle.identity,
        )
        result, payload = _prepare_edit(
            action,
            path=path,
            loaded=loaded,
            executable=executable,
            timeout_seconds=timeout_seconds,
            dry_run=False,
        )
        if result.changed:
            _atomic_write(handle, path, payload, loaded.token)
        return result


def edit_claude_hooks(
    action: str,
    *,
    executable: str | Path,
    timeout_seconds: int,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> HookEditResult:
    """Install or uninstall only Switchboard-owned Claude user hooks."""

    if action not in {"install", "uninstall"}:
        raise HookConfigError("hook action must be install or uninstall")
    try:
        path = claude_settings_path(environ=environ)
    except ValueError as error:
        raise HookConfigError(str(error)) from error
    if dry_run:
        result, _payload = _prepare_edit(
            action,
            path=path,
            loaded=_load_document(path),
            executable=executable,
            timeout_seconds=timeout_seconds,
            dry_run=True,
            groups=canonical_claude_hook_groups,
            owned=is_claude_switchboard_handler,
            require_private_mode=False,
        )
        return result

    with _locked_codex_directory(path.parent) as handle:
        loaded = _read_from_directory(
            handle.directory,
            path.name,
            directory_identity=handle.identity,
        )
        result, payload = _prepare_edit(
            action,
            path=path,
            loaded=loaded,
            executable=executable,
            timeout_seconds=timeout_seconds,
            dry_run=False,
            groups=canonical_claude_hook_groups,
            owned=is_claude_switchboard_handler,
            require_private_mode=False,
        )
        if result.changed:
            _atomic_write(
                handle,
                path,
                payload,
                loaded.token,
                mode=loaded.mode if loaded.mode is not None else 0o600,
            )
        return result


def inspect_claude_hooks(
    *, environ: Mapping[str, str] | None = None
) -> ClaudeHooksInspection:
    """Return only bounded Switchboard-like Claude hook metadata."""

    try:
        path = claude_settings_path(environ=environ)
    except ValueError as error:
        raise HookConfigError(str(error)) from error
    document = _load_document(path).document
    hooks = document.get("hooks", {})
    assert isinstance(hooks, dict)
    candidates: list[ClaudeHookCandidate] = []
    for event, groups in hooks.items():
        for group in groups:
            for handler in group["hooks"]:
                command = handler.get("command")
                raw_args = handler.get("args")
                args = (
                    tuple(raw_args)
                    if isinstance(raw_args, list)
                    and all(isinstance(argument, str) for argument in raw_args)
                    else None
                )
                looks_owned = bool(
                    is_claude_switchboard_handler(handler)
                    or handler.get("statusMessage") == CLAUDE_HOOK_STATUS_MESSAGE
                    or args == ("event", "--provider", "claude")
                )
                if not looks_owned:
                    continue
                candidates.append(
                    ClaudeHookCandidate(
                        event,
                        handler.get("type")
                        if isinstance(handler.get("type"), str)
                        else None,
                        command if isinstance(command, str) else None,
                        args,
                        group.get("matcher")
                        if isinstance(group.get("matcher"), str)
                        else None,
                        handler.get("timeout")
                        if type(handler.get("timeout")) is int
                        else None,
                        handler.get("statusMessage")
                        if isinstance(handler.get("statusMessage"), str)
                        else None,
                    )
                )
    return ClaudeHooksInspection(path, tuple(candidates))


__all__ = [
    "APP_SERVER_EVENT_NAMES",
    "CLAUDE_HOOK_EVENTS",
    "CLAUDE_HOOK_STATUS_MESSAGE",
    "HOOK_EVENTS",
    "HOOK_STATUS_MESSAGE",
    "ClaudeHookCandidate",
    "ClaudeHooksInspection",
    "HookConfigError",
    "HookEditResult",
    "canonical_claude_hook_groups",
    "canonical_hook_groups",
    "codex_home",
    "command_argv",
    "edit_claude_hooks",
    "edit_codex_hooks",
    "hook_command",
    "inspect_claude_hooks",
    "is_claude_switchboard_handler",
    "is_switchboard_handler",
    "resolve_swbctl_executable",
]
