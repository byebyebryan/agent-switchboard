"""Contract-gated native provider command construction for Phase 6.

This module does not start a provider.  It turns an already-authorized durable
launch into one fixed argv and environment contract.  The tmux executor owns
the final ``exec`` boundary so no Switchboard wrapper remains in the pane.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import UUID

from .domain import ProviderId

CONTROL_PROMPT: Final = (
    "Call transition_claim() and follow the returned transition instructions."
)
CODEX_KNOWN_GOOD_VERSIONS: Final = frozenset({"0.144.6"})
CLAUDE_KNOWN_GOOD_VERSIONS: Final = frozenset({"2.1.216"})
_VERSION_VALUE_RE: Final = re.compile(r"\d+\.\d+\.\d+(?:[-+][^\s()]+)?$")
_VERSION_RE: Final = re.compile(
    r"(?:^|\s)(?:codex(?:-cli)?\s+)?"
    r"(?P<version>\d+\.\d+\.\d+(?:[-+][^\s()]+)?)"
    r"(?:\s+\(Claude Code\))?\s*$"
)
_PROBE_TIMEOUT_SECONDS: Final = 5.0
_MAX_PROBE_BYTES: Final = 4096


class ProviderRuntimeError(RuntimeError):
    """A native provider command cannot be authorized safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProviderContract:
    provider: ProviderId
    executable: str
    version: str

    def __post_init__(self) -> None:
        _safe_executable(self.executable)
        if (
            not isinstance(self.version, str)
            or _VERSION_VALUE_RE.fullmatch(self.version) is None
        ):
            raise ProviderRuntimeError(
                "provider_version_invalid", "provider version is malformed"
            )

    @property
    def known_good(self) -> bool:
        observed = (
            CODEX_KNOWN_GOOD_VERSIONS
            if self.provider is ProviderId.CODEX
            else CLAUDE_KNOWN_GOOD_VERSIONS
        )
        return self.version in observed


@dataclass(frozen=True, slots=True)
class ProviderCommand:
    provider: ProviderId
    version: str
    action: str
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    expected_session_id: UUID | None


def _safe_executable(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProviderRuntimeError(
            "provider_executable_invalid", "provider executable is invalid"
        )
    return value


def _safe_cwd(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or "\x00" in str(path):
        raise ProviderRuntimeError(
            "provider_cwd_invalid", "provider cwd must be an absolute path"
        )
    return path


def _safe_session_id(value: UUID, field: str) -> UUID:
    if not isinstance(value, UUID) or value.int == 0:
        raise ProviderRuntimeError(
            "provider_session_invalid", f"{field} must be a non-nil UUID"
        )
    return value


def _safe_prompt(value: str | None) -> str | None:
    if value is not None and value != CONTROL_PROMPT:
        raise ProviderRuntimeError(
            "provider_prompt_forbidden",
            "provider bootstrap accepts only the fixed control prompt",
        )
    return value


def probe_contract(
    provider: ProviderId,
    *,
    executable: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> ProviderContract:
    """Return a strictly parsed provider observation or fail closed.

    Version identity is telemetry. Behavioral command, UUID, and lifecycle
    checks remain the launch authority.
    """

    binary = _safe_executable(executable or provider.value)
    try:
        result = subprocess.run(
            [binary, "--version"],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            env=None if environment is None else dict(environment),
        )
    except FileNotFoundError as error:
        raise ProviderRuntimeError(
            "provider_not_found", f"{provider.value} executable was not found"
        ) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProviderRuntimeError(
            "provider_probe_failed", f"{provider.value} version probe failed"
        ) from error
    if (
        result.returncode != 0
        or len(result.stdout) > _MAX_PROBE_BYTES
        or len(result.stderr) > _MAX_PROBE_BYTES
    ):
        raise ProviderRuntimeError(
            "provider_probe_failed", f"{provider.value} version probe failed"
        )
    try:
        output = result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ProviderRuntimeError(
            "provider_version_invalid", "provider version output is invalid"
        ) from error
    match = _VERSION_RE.fullmatch(output)
    if match is None:
        raise ProviderRuntimeError(
            "provider_version_invalid", "provider version output is unrecognized"
        )
    version = match.group("version")
    return ProviderContract(provider, binary, version)


def _environment(
    base: Mapping[str, str] | None, injected: Mapping[str, str]
) -> dict[str, str]:
    result = {} if base is None else dict(base)
    for key, value in injected.items():
        if (
            not key
            or "=" in key
            or "\x00" in key
            or not isinstance(value, str)
            or "\x00" in value
        ):
            raise ProviderRuntimeError(
                "provider_environment_invalid", "provider environment is invalid"
            )
        result[key] = value
    return result


def _mcp_options(
    provider: ProviderId, command: tuple[str, ...] | None
) -> tuple[str, ...]:
    if command is None:
        return ()
    if not command or any(not value or "\x00" in value for value in command):
        raise ProviderRuntimeError(
            "provider_mcp_invalid", "provider MCP command is invalid"
        )
    if provider is ProviderId.CODEX:
        executable = json.dumps(command[0], ensure_ascii=False)
        arguments = json.dumps(
            list(command[1:]), ensure_ascii=False, separators=(",", ":")
        )
        return (
            "-c",
            f"mcp_servers.switchboard.command={executable}",
            "-c",
            f"mcp_servers.switchboard.args={arguments}",
        )
    document = json.dumps(
        {
            "mcpServers": {
                "switchboard": {
                    "type": "stdio",
                    "command": command[0],
                    "args": list(command[1:]),
                }
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ("--mcp-config", document)


def build_new_command(
    contract: ProviderContract,
    *,
    cwd: Path,
    session_id: UUID,
    prompt: str | None,
    injected_environment: Mapping[str, str],
    base_environment: Mapping[str, str] | None = None,
    mcp_command: tuple[str, ...] | None = None,
) -> ProviderCommand:
    """Build an exact new-session command.

    Codex requires the UUID to have been precreated through its accepted App
    Server contract, so its native CLI enters that zero-turn session via
    ``resume``.  Claude can reserve the UUID directly with ``--session-id``.
    """

    path = _safe_cwd(cwd)
    session_id = _safe_session_id(session_id, "session_id")
    prompt = _safe_prompt(prompt)
    options = _mcp_options(contract.provider, mcp_command)
    if contract.provider is ProviderId.CODEX:
        argv = [
            contract.executable,
            "resume",
            "-C",
            str(path),
            *options,
            str(session_id),
        ]
    else:
        argv = [contract.executable, "--session-id", str(session_id), *options]
    if prompt is not None:
        argv.append(prompt)
    return ProviderCommand(
        contract.provider,
        contract.version,
        "new",
        tuple(argv),
        path,
        _environment(base_environment, injected_environment),
        session_id,
    )


def build_resume_command(
    contract: ProviderContract,
    *,
    cwd: Path,
    session_id: UUID,
    prompt: str | None,
    injected_environment: Mapping[str, str],
    base_environment: Mapping[str, str] | None = None,
    mcp_command: tuple[str, ...] | None = None,
) -> ProviderCommand:
    path = _safe_cwd(cwd)
    session_id = _safe_session_id(session_id, "session_id")
    prompt = _safe_prompt(prompt)
    options = _mcp_options(contract.provider, mcp_command)
    if contract.provider is ProviderId.CODEX:
        argv = [
            contract.executable,
            "resume",
            "-C",
            str(path),
            *options,
            str(session_id),
        ]
    else:
        argv = [contract.executable, "--resume", str(session_id), *options]
    if prompt is not None:
        argv.append(prompt)
    return ProviderCommand(
        contract.provider,
        contract.version,
        "resume",
        tuple(argv),
        path,
        _environment(base_environment, injected_environment),
        session_id,
    )


def build_fork_command(
    contract: ProviderContract,
    *,
    cwd: Path,
    source_session_id: UUID,
    target_session_id: UUID | None,
    prompt: str | None,
    injected_environment: Mapping[str, str],
    base_environment: Mapping[str, str] | None = None,
    mcp_command: tuple[str, ...] | None = None,
) -> ProviderCommand:
    """Build a guarded provider-native fork command.

    Codex allocates the fork UUID itself, while Claude's accepted contract can
    bind an explicitly reserved target UUID.
    """

    path = _safe_cwd(cwd)
    source_session_id = _safe_session_id(source_session_id, "source_session_id")
    target_session_id = (
        None
        if target_session_id is None
        else _safe_session_id(target_session_id, "target_session_id")
    )
    prompt = _safe_prompt(prompt)
    options = _mcp_options(contract.provider, mcp_command)
    if contract.provider is ProviderId.CODEX:
        if target_session_id is not None:
            raise ProviderRuntimeError(
                "provider_fork_identity_unsupported",
                "accepted Codex fork contract cannot reserve the target UUID",
            )
        argv = [
            contract.executable,
            "fork",
            "-C",
            str(path),
            *options,
            str(source_session_id),
        ]
    else:
        if target_session_id is None:
            raise ProviderRuntimeError(
                "provider_fork_identity_required",
                "Claude fork requires an exact target UUID",
            )
        argv = [
            contract.executable,
            "--resume",
            str(source_session_id),
            "--fork-session",
            "--session-id",
            str(target_session_id),
            *options,
        ]
    if prompt is not None:
        argv.append(prompt)
    return ProviderCommand(
        contract.provider,
        contract.version,
        "fork",
        tuple(argv),
        path,
        _environment(base_environment, injected_environment),
        target_session_id,
    )


__all__ = [
    "CLAUDE_KNOWN_GOOD_VERSIONS",
    "CODEX_KNOWN_GOOD_VERSIONS",
    "CONTROL_PROMPT",
    "ProviderCommand",
    "ProviderContract",
    "ProviderRuntimeError",
    "build_fork_command",
    "build_new_command",
    "build_resume_command",
    "probe_contract",
]
