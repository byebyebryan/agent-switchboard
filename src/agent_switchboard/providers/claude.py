"""Bounded Claude Code capability probing for the Agent-View-disabled profile."""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

CLAUDE_TESTED_CONTRACT_MIN: Final = "2.1.210"
CLAUDE_TESTED_CONTRACT_MAX: Final = "2.1.210"
CLAUDE_FEATURES: Final = ("hooks", "native_resume", "tmux_runtime")
MAX_CLAUDE_SETTINGS_BYTES: Final = 8 * 1024 * 1024

_VERSION_RE: Final = re.compile(
    r"^\s*(?P<version>\d+\.\d+\.\d+(?:[-+][^\s()]+)?)"
    r"(?:\s+\(Claude Code\))?\s*$"
)


@dataclass(frozen=True, slots=True)
class ClaudeProviderIssue:
    """Stable, payload-free Claude capability degradation."""

    code: str
    message: str
    retryable: bool
    stage: str
    feature: str | None = None


@dataclass(frozen=True, slots=True)
class ClaudeCapabilityReport:
    """One bounded observation of the supported Claude runtime profile."""

    available: bool
    provider_version: str | None
    tested_contract_min: str
    tested_contract_max: str
    features: tuple[str, ...]
    degraded_reasons: tuple[ClaudeProviderIssue, ...]


@dataclass(frozen=True, slots=True)
class ClaudeSettingsInspection:
    """Configured user-level Agent View state without claiming policy effect."""

    path: Path
    disable_agent_view: bool | None
    disable_all_hooks: bool | None
    allow_managed_hooks_only: bool | None
    issue: ClaudeProviderIssue | None = None


class _ClaudeProbeFailure(RuntimeError):
    def __init__(self, issue: ClaudeProviderIssue) -> None:
        super().__init__(issue.message)
        self.issue = issue


def _issue(
    code: str,
    message: str,
    *,
    retryable: bool,
    stage: str,
    feature: str | None = None,
) -> ClaudeProviderIssue:
    return ClaudeProviderIssue(code, message, retryable, stage, feature)


def _failure(
    code: str,
    message: str,
    *,
    retryable: bool,
    stage: str,
    feature: str | None = None,
) -> _ClaudeProbeFailure:
    return _ClaudeProbeFailure(
        _issue(
            code,
            message,
            retryable=retryable,
            stage=stage,
            feature=feature,
        )
    )


def claude_settings_path(*, environ: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    home = environment.get("HOME")
    if not isinstance(home, str) or not home or not Path(home).is_absolute():
        raise ValueError("HOME must be a non-empty absolute path")
    if "\x00" in home:
        raise ValueError("HOME contains NUL")
    return Path(home) / ".claude" / "settings.json"


def inspect_claude_settings(
    *, environ: Mapping[str, str] | None = None
) -> ClaudeSettingsInspection:
    """Read bounded user settings without following a special-file target."""

    try:
        path = claude_settings_path(environ=environ)
    except ValueError:
        return ClaudeSettingsInspection(
            Path("/unavailable/claude-settings.json"),
            None,
            None,
            None,
            _issue(
                "claude_settings_unavailable",
                "Claude user settings location is unavailable.",
                retryable=False,
                stage="configuration",
            ),
        )
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return ClaudeSettingsInspection(path, None, None, None)
    except OSError:
        return ClaudeSettingsInspection(
            path,
            None,
            None,
            None,
            _issue(
                "claude_settings_unavailable",
                "Claude user settings could not be inspected safely.",
                retryable=True,
                stage="configuration",
            ),
        )
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return ClaudeSettingsInspection(
            path,
            None,
            None,
            None,
            _issue(
                "claude_settings_unsafe",
                "Claude user settings are not a regular file.",
                retryable=False,
                stage="configuration",
            ),
        )
    if metadata.st_size > MAX_CLAUDE_SETTINGS_BYTES:
        return ClaudeSettingsInspection(
            path,
            None,
            None,
            None,
            _issue(
                "claude_settings_oversized",
                "Claude user settings exceed the safe size limit.",
                retryable=False,
                stage="configuration",
            ),
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("settings target is not regular")
        raw = bytearray()
        while len(raw) <= MAX_CLAUDE_SETTINGS_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_CLAUDE_SETTINGS_BYTES + 1 - len(raw)),
            )
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > MAX_CLAUDE_SETTINGS_BYTES:
            raise ValueError("oversized settings")
        value = json.loads(raw)
    except (OSError, UnicodeError, ValueError, RecursionError, json.JSONDecodeError):
        return ClaudeSettingsInspection(
            path,
            None,
            None,
            None,
            _issue(
                "claude_settings_invalid",
                "Claude user settings are not valid bounded JSON.",
                retryable=False,
                stage="configuration",
            ),
        )
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
    if not isinstance(value, dict):
        return ClaudeSettingsInspection(
            path,
            None,
            None,
            None,
            _issue(
                "claude_settings_invalid",
                "Claude user settings must contain one JSON object.",
                retryable=False,
                stage="configuration",
            ),
        )

    def optional_boolean(field: str) -> bool | None:
        field_value = value.get(field)
        if field_value is None:
            return None
        if not isinstance(field_value, bool):
            raise TypeError(field)
        return field_value

    try:
        return ClaudeSettingsInspection(
            path,
            optional_boolean("disableAgentView"),
            optional_boolean("disableAllHooks"),
            optional_boolean("allowManagedHooksOnly"),
        )
    except TypeError:
        return ClaudeSettingsInspection(
            path,
            None,
            None,
            None,
            _issue(
                "claude_settings_invalid",
                "Claude lifecycle settings must use boolean values.",
                retryable=False,
                stage="configuration",
            ),
        )


def _stop_process_group(process: subprocess.Popen[bytes], timeout: float) -> None:
    """Terminate and reap the complete isolated probe process group."""

    # The leader may already have exited while a descendant kept running with
    # redirected stdio, so process.poll() alone cannot prove group cleanup.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=timeout)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


class ClaudeProvider:
    """Production Claude adapter limited to a bounded version probe."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        command_timeout: float = 5.0,
        cleanup_timeout: float = 1.0,
        max_stdout_bytes: int = 4096,
        max_stderr_bytes: int = 64 * 1024,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if executable is not None and (
            not isinstance(executable, str) or not executable or "\x00" in executable
        ):
            raise ValueError("Claude executable must be a non-empty string")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in (command_timeout, cleanup_timeout)
        ):
            raise ValueError("Claude provider timeouts must be finite and positive")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (max_stdout_bytes, max_stderr_bytes)
        ):
            raise ValueError("Claude provider byte bounds must be positive integers")
        if environment is not None and any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "=" in key
            or "\x00" in key
            or "\x00" in value
            for key, value in environment.items()
        ):
            raise ValueError("Claude provider environment must contain safe strings")
        self.executable = executable or "claude"
        self.command_timeout = float(command_timeout)
        self.cleanup_timeout = float(cleanup_timeout)
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.environment = None if environment is None else dict(environment)

    def _provider_version(self) -> str:
        try:
            process = subprocess.Popen(
                [self.executable, "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=self.environment,
                start_new_session=True,
            )
        except FileNotFoundError as error:
            raise _failure(
                "provider_not_found",
                "The configured Claude executable was not found.",
                retryable=False,
                stage="spawn",
            ) from error
        except OSError as error:
            raise _failure(
                "provider_start_failed",
                "The configured Claude executable could not be started.",
                retryable=False,
                stage="spawn",
            ) from error
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        stdout = bytearray()
        stderr_seen = 0
        deadline = time.monotonic() + self.command_timeout
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _failure(
                        "provider_command_timeout",
                        "A Claude capability probe exceeded its deadline.",
                        retryable=True,
                        stage="version",
                    )
                for key, _mask in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stdout":
                        if len(stdout) + len(chunk) > self.max_stdout_bytes:
                            raise _failure(
                                "provider_output_too_large",
                                "A Claude capability probe exceeded its output limit.",
                                retryable=False,
                                stage="version",
                            )
                        stdout.extend(chunk)
                    else:
                        stderr_seen += len(chunk)
                        if stderr_seen > self.max_stderr_bytes:
                            raise _failure(
                                "provider_stderr_limit",
                                "A Claude capability probe exceeded its stderr limit.",
                                retryable=False,
                                stage="version",
                            )
            try:
                returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise _failure(
                    "provider_command_timeout",
                    "A Claude capability probe exceeded its deadline.",
                    retryable=True,
                    stage="version",
                ) from error
            if returncode != 0:
                raise _failure(
                    "provider_version_failed",
                    "Claude version probing returned an unsuccessful status.",
                    retryable=True,
                    stage="version",
                )
            try:
                text = stdout.decode("utf-8")
            except UnicodeDecodeError as error:
                raise _failure(
                    "provider_version_invalid",
                    "Claude returned invalid version output.",
                    retryable=False,
                    stage="version",
                ) from error
            match = _VERSION_RE.fullmatch(text)
            if match is None:
                raise _failure(
                    "provider_version_invalid",
                    "Claude returned unrecognized version output.",
                    retryable=False,
                    stage="version",
                )
            return match.group("version")
        finally:
            selector.close()
            _stop_process_group(process, self.cleanup_timeout)
            process.stdout.close()
            process.stderr.close()

    def inspect_capability(
        self, settings: ClaudeSettingsInspection
    ) -> ClaudeCapabilityReport:
        """Probe version and evaluate the configured Agent View profile."""

        try:
            version = self._provider_version()
        except _ClaudeProbeFailure as failure:
            return ClaudeCapabilityReport(
                False,
                None,
                CLAUDE_TESTED_CONTRACT_MIN,
                CLAUDE_TESTED_CONTRACT_MAX,
                CLAUDE_FEATURES,
                (failure.issue,),
            )
        issues: list[ClaudeProviderIssue] = []
        if version != CLAUDE_TESTED_CONTRACT_MIN:
            issues.append(
                _issue(
                    "untested_provider_version",
                    "The installed Claude version is outside the tested "
                    "contract range.",
                    retryable=False,
                    stage="version",
                )
            )
        if settings.issue is not None:
            issues.append(settings.issue)
        elif settings.disable_agent_view is not True:
            issues.append(
                _issue(
                    "agent_view_enabled",
                    "Claude Agent View must be disabled for the Switchboard profile.",
                    retryable=False,
                    stage="configuration",
                    feature="tmux_runtime",
                )
            )
        return ClaudeCapabilityReport(
            not issues,
            version,
            CLAUDE_TESTED_CONTRACT_MIN,
            CLAUDE_TESTED_CONTRACT_MAX,
            CLAUDE_FEATURES,
            tuple(issues),
        )


__all__ = [
    "CLAUDE_FEATURES",
    "CLAUDE_TESTED_CONTRACT_MAX",
    "CLAUDE_TESTED_CONTRACT_MIN",
    "ClaudeCapabilityReport",
    "ClaudeProvider",
    "ClaudeProviderIssue",
    "ClaudeSettingsInspection",
    "claude_settings_path",
    "inspect_claude_settings",
]
