"""Human-readable provider hook and runtime-profile diagnostics."""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import tempfile
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import uuid4

from .config import HooksConfig, SwitchboardConfig
from .domain import ProviderId
from .hook_config import (
    APP_SERVER_EVENT_NAMES,
    CLAUDE_HOOK_EVENTS,
    CLAUDE_HOOK_STATUS_MESSAGE,
    HOOK_EVENTS,
    HOOK_STATUS_MESSAGE,
    HookConfigError,
    canonical_claude_hook_groups,
    canonical_hook_groups,
    codex_home,
    hook_command,
    inspect_claude_hooks,
)
from .providers.claude import ClaudeProvider, inspect_claude_settings
from .providers.codex import CodexHookMetadata, CodexProvider

_MAX_DIAGNOSTICS: Final = 256
_MAX_MESSAGE_LENGTH: Final = 512
_WARM_RUNS: Final = 10


@dataclass(frozen=True, slots=True)
class DoctorDiagnostic:
    level: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class DoctorResult:
    healthy: bool
    provider_version: str | None
    cold_latency_ms: float | None
    warm_p95_latency_ms: float | None
    diagnostics: tuple[DoctorDiagnostic, ...]

    def render(self) -> str:
        status = "healthy" if self.healthy else "unhealthy"
        lines = [f"Agent Switchboard doctor: {status}"]
        if self.provider_version is not None:
            lines.append(f"Codex: {self.provider_version}")
        if self.cold_latency_ms is not None:
            lines.append(f"Hook cold start: {self.cold_latency_ms:.1f} ms")
        if self.warm_p95_latency_ms is not None:
            lines.append(f"Hook warm p95: {self.warm_p95_latency_ms:.1f} ms")
        lines.extend(
            f"{diagnostic.level.upper()} {diagnostic.code}: {diagnostic.message}"
            for diagnostic in self.diagnostics
        )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class CombinedDoctorResult:
    healthy: bool
    providers: tuple[tuple[str, DoctorResult], ...]

    def render(self) -> str:
        status = "healthy" if self.healthy else "unhealthy"
        lines = [f"Agent Switchboard doctor: {status}"]
        for provider, result in self.providers:
            provider_status = "healthy" if result.healthy else "unhealthy"
            lines.append(f"{provider}: {provider_status}")
            if result.provider_version is not None:
                lines.append(f"{provider} version: {result.provider_version}")
            if result.cold_latency_ms is not None:
                lines.append(
                    f"{provider} hook cold start: {result.cold_latency_ms:.1f} ms"
                )
            if result.warm_p95_latency_ms is not None:
                lines.append(
                    f"{provider} hook warm p95: {result.warm_p95_latency_ms:.1f} ms"
                )
            lines.extend(
                f"{diagnostic.level.upper()} {provider.lower()}."
                f"{diagnostic.code}: {diagnostic.message}"
                for diagnostic in result.diagnostics
            )
        return "\n".join(lines)


def _bounded_message(value: object) -> str:
    printable = "".join(
        character if character.isprintable() else " " for character in str(value)
    )
    return " ".join(printable.split())[:_MAX_MESSAGE_LENGTH] or "unknown issue"


def _candidate(metadata: CodexHookMetadata) -> bool:
    command_matches = False
    if metadata.command is not None:
        try:
            argv = tuple(shlex.split(metadata.command))
        except ValueError:
            argv = ()
        command_matches = bool(
            len(argv) == 4
            and argv[1:] == ("event", "--provider", "codex")
            and Path(argv[0]).name == "swbctl"
        )
    return command_matches or metadata.status_message == HOOK_STATUS_MESSAGE


def _command_path(command: str | None) -> Path | None:
    if command is None:
        return None
    try:
        argv = tuple(shlex.split(command))
    except ValueError:
        return None
    if len(argv) != 4 or argv[1:] != ("event", "--provider", "codex"):
        return None
    return Path(argv[0])


def _latency_probe(
    executable: Path,
    *,
    timeout_seconds: int,
    environment: Mapping[str, str] | None,
    provider: str = "codex",
) -> tuple[float, float]:
    base_environment = dict(os.environ if environment is None else environment)
    for key in tuple(base_environment):
        if key.startswith("AGENT_SWITCHBOARD_") or key in {"TMUX", "TMUX_PANE"}:
            del base_environment[key]
    with tempfile.TemporaryDirectory(prefix="agent-switchboard-doctor-") as raw:
        root = Path(raw)
        isolated = dict(base_environment)
        isolated.update(
            {
                "HOME": str(root / "home"),
                "CODEX_HOME": str(root / "codex"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
            }
        )

        def invoke() -> float:
            payload = json.dumps(
                {
                    "session_id": str(uuid4()),
                    "cwd": str(root),
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                },
                separators=(",", ":"),
            ).encode()
            started = time.perf_counter_ns()
            deadline = time.monotonic() + timeout_seconds
            process = subprocess.Popen(
                [str(executable), "event", "--provider", provider],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=isolated,
            )
            assert process.stdin is not None
            try:
                with suppress(BrokenPipeError):
                    process.stdin.write(payload)
                with suppress(BrokenPipeError):
                    process.stdin.close()
                while process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        process.kill()
                        process.wait()
                        raise subprocess.TimeoutExpired(
                            [str(executable), "event", "--provider", provider],
                            timeout_seconds,
                        )
                    time.sleep(min(0.001, remaining))
            except BaseException:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                raise
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
            if process.returncode != 0:
                raise RuntimeError("isolated swbctl event probe failed")
            return elapsed

        cold = invoke()
        warm = sorted(invoke() for _ in range(_WARM_RUNS))
        rank = max(0, math.ceil(0.95 * len(warm)) - 1)
        return cold, warm[rank]


def run_doctor(
    *,
    codex_executable: str,
    swbctl_executable: str | Path,
    hooks: HooksConfig,
    cwd: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> DoctorResult:
    """Inspect effective hooks and benchmark event ingestion in isolated state."""

    current_swbctl = Path(swbctl_executable)
    diagnostics: list[DoctorDiagnostic] = []
    has_error = False
    omitted_diagnostics = 0

    def add(level: str, code: str, message: object) -> None:
        nonlocal has_error, omitted_diagnostics
        has_error = has_error or level == "error"
        if len(diagnostics) < _MAX_DIAGNOSTICS - 1:
            diagnostics.append(DoctorDiagnostic(level, code, _bounded_message(message)))
        else:
            omitted_diagnostics += 1

    if not current_swbctl.is_absolute():
        add("error", "swbctl_nonabsolute", "swbctl does not resolve absolutely")
    if not current_swbctl.is_file():
        add("error", "swbctl_missing", f"{current_swbctl} does not exist")
    elif not os.access(current_swbctl, os.X_OK):
        add("error", "swbctl_not_executable", f"{current_swbctl} is not executable")

    inspected_cwd = Path.cwd() if cwd is None else Path(cwd)
    inspected_cwd = inspected_cwd.absolute()
    inspection = CodexProvider(
        executable=codex_executable,
        environment=environment,
    ).inspect_hooks(cwds=(inspected_cwd,))
    for issue in inspection.issues:
        add("error" if issue.blocking else "warning", issue.code, issue.message)
    expected_path = codex_home(environ=environment) / "hooks.json"
    expected_command = hook_command(current_swbctl)
    canonical = canonical_hook_groups(
        current_swbctl, timeout_seconds=hooks.timeout_seconds
    )
    observed = [
        metadata
        for entry in inspection.entries
        for metadata in entry.hooks
        if _candidate(metadata)
    ]
    for entry in inspection.entries:
        for warning in entry.warnings:
            add("warning", "hook_source_warning", warning)
        for path, message in entry.errors:
            add("error", "hook_source_error", f"{path}: {message}")

    for config_event in HOOK_EVENTS:
        event_name = APP_SERVER_EVENT_NAMES[config_event]
        matches = [item for item in observed if item.event_name == event_name]
        if not matches:
            add("error", "hook_missing", f"{config_event} hook is missing")
            continue
        if len(matches) > 1:
            add(
                "error",
                "hook_duplicate",
                f"{config_event} has {len(matches)} Switchboard handlers",
            )
        expected_group = canonical[config_event]
        expected_matcher = expected_group.get("matcher")
        for metadata in matches:
            if metadata.handler_type != "command":
                add("error", "hook_modified", f"{config_event} is not a command hook")
            if metadata.command != expected_command:
                add("error", "hook_modified", f"{config_event} command differs")
            if metadata.matcher != expected_matcher:
                add("error", "hook_modified", f"{config_event} matcher differs")
            if metadata.timeout_seconds != hooks.timeout_seconds:
                add("error", "hook_modified", f"{config_event} timeout differs")
            if metadata.status_message != HOOK_STATUS_MESSAGE:
                add("error", "hook_modified", f"{config_event} status marker differs")
            if metadata.source_path != expected_path:
                add(
                    "error",
                    "hook_wrong_source",
                    f"{config_event} loaded from {metadata.source_path}",
                )
            if not metadata.enabled:
                add("error", "hook_disabled", f"{config_event} is disabled")
            if metadata.trust_status == "untrusted":
                add("error", "hook_untrusted", f"{config_event} needs review in /hooks")
            elif metadata.trust_status == "modified":
                add("error", "hook_modified", f"{config_event} trust hash is stale")
            command_path = _command_path(metadata.command)
            if command_path is None or not command_path.is_absolute():
                add(
                    "error",
                    "hook_command_nonabsolute",
                    f"{config_event} command path is not absolute",
                )
            elif command_path != current_swbctl:
                add(
                    "error",
                    "hook_command_stale",
                    f"{config_event} uses {command_path}",
                )
            elif not command_path.is_file():
                add("error", "hook_command_missing", f"{command_path} does not exist")
            elif not os.access(command_path, os.X_OK):
                add(
                    "error",
                    "hook_command_not_executable",
                    f"{command_path} is not executable",
                )

    cold_latency: float | None = None
    warm_p95: float | None = None
    if (
        current_swbctl.is_absolute()
        and current_swbctl.is_file()
        and os.access(current_swbctl, os.X_OK)
    ):
        try:
            cold_latency, warm_p95 = _latency_probe(
                current_swbctl,
                timeout_seconds=hooks.timeout_seconds,
                environment=environment,
            )
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
            add("error", "hook_latency_failed", error)
        else:
            if warm_p95 > hooks.latency_budget_ms:
                add(
                    "error",
                    "hook_latency_budget",
                    f"warm p95 {warm_p95:.1f} ms exceeds {hooks.latency_budget_ms} ms",
                )

    if omitted_diagnostics:
        diagnostics.append(
            DoctorDiagnostic(
                "warning",
                "diagnostics_truncated",
                f"{omitted_diagnostics} additional diagnostics were omitted",
            )
        )
    healthy = inspection.available and not has_error
    return DoctorResult(
        healthy,
        inspection.provider_version,
        cold_latency,
        warm_p95,
        tuple(diagnostics),
    )


def _legacy_agent_view_counts(
    *, proc_root: Path = Path("/proc"), uid: int | None = None
) -> tuple[int, int, bool]:
    """Count same-user Agent View clients/supervisors without exposing argv."""

    expected_uid = os.getuid() if uid is None else uid
    agents = 0
    daemons = 0
    complete = True
    try:
        entries = tuple(
            entry for entry in proc_root.iterdir() if entry.name.isdecimal()
        )
    except OSError:
        return 0, 0, False
    if len(entries) > 32_768:
        return 0, 0, False
    for entry in entries:
        try:
            if entry.stat().st_uid != expected_uid:
                continue
            with (entry / "cmdline").open("rb") as stream:
                raw = stream.read(64 * 1024 + 1)
        except FileNotFoundError:
            continue
        except OSError:
            complete = False
            continue
        if len(raw) > 64 * 1024:
            complete = False
            continue
        argv = tuple(
            os.fsdecode(value) for value in raw.rstrip(b"\0").split(b"\0") if value
        )
        if not argv or Path(argv[0]).name != "claude":
            continue
        if len(argv) >= 2 and argv[1] == "agents":
            agents += 1
        elif len(argv) >= 2 and argv[1] == "daemon":
            daemons += 1
    return agents, daemons, complete


def run_claude_doctor(
    *,
    claude_executable: str,
    swbctl_executable: str | Path,
    hooks: HooksConfig,
    environment: Mapping[str, str] | None = None,
    proc_root: Path = Path("/proc"),
    uid: int | None = None,
) -> DoctorResult:
    """Inspect configured Claude hooks and the Agent-View-disabled profile."""

    current_swbctl = Path(swbctl_executable)
    diagnostics: list[DoctorDiagnostic] = []
    has_error = False

    def add(level: str, code: str, message: object) -> None:
        nonlocal has_error
        has_error = has_error or level == "error"
        if len(diagnostics) < _MAX_DIAGNOSTICS:
            diagnostics.append(DoctorDiagnostic(level, code, _bounded_message(message)))

    if not current_swbctl.is_absolute():
        add("error", "swbctl_nonabsolute", "swbctl does not resolve absolutely")
    if not current_swbctl.is_file():
        add("error", "swbctl_missing", f"{current_swbctl} does not exist")
    elif not os.access(current_swbctl, os.X_OK):
        add("error", "swbctl_not_executable", f"{current_swbctl} is not executable")

    settings = inspect_claude_settings(environ=environment)
    capability = ClaudeProvider(
        executable=claude_executable, environment=environment
    ).inspect_capability(settings)
    for issue in capability.degraded_reasons:
        add("error" if issue.blocking else "warning", issue.code, issue.message)
    if settings.disable_all_hooks is True:
        add("error", "hooks_disabled", "Claude disableAllHooks is enabled")
    if settings.allow_managed_hooks_only is True:
        add(
            "warning",
            "managed_hook_policy_unknown",
            "Static inspection cannot prove that user hooks are allowed by policy.",
        )

    try:
        inspection = inspect_claude_hooks(environ=environment)
    except HookConfigError as error:
        add("error", "hook_source_error", error)
        observed = ()
    else:
        observed = inspection.candidates
    canonical = canonical_claude_hook_groups(
        current_swbctl, timeout_seconds=hooks.timeout_seconds
    )
    expected_args = ("event", "--provider", "claude")
    for event in CLAUDE_HOOK_EVENTS:
        matches = [candidate for candidate in observed if candidate.event == event]
        if not matches:
            add("error", "hook_missing", f"{event} hook is missing")
            continue
        if len(matches) > 1:
            add(
                "error",
                "hook_duplicate",
                f"{event} has {len(matches)} Switchboard handlers",
            )
        expected = canonical[event]["hooks"][0]
        for candidate in matches:
            if candidate.handler_type != "command":
                add("error", "hook_modified", f"{event} is not a command hook")
            if candidate.command != str(current_swbctl):
                add("error", "hook_command_stale", f"{event} command differs")
            if candidate.args != expected_args:
                add("error", "hook_modified", f"{event} arguments differ")
            if candidate.matcher is not None:
                add("error", "hook_modified", f"{event} matcher differs")
            if candidate.timeout_seconds != expected["timeout"]:
                add("error", "hook_modified", f"{event} timeout differs")
            if candidate.status_message != CLAUDE_HOOK_STATUS_MESSAGE:
                add("error", "hook_modified", f"{event} status marker differs")

    agents, daemons, process_complete = _legacy_agent_view_counts(
        proc_root=proc_root, uid=uid
    )
    if not process_complete:
        add(
            "warning",
            "legacy_runtime_probe_incomplete",
            "Some Agent View process evidence could not be inspected.",
        )
    if agents or daemons:
        add(
            "error",
            "legacy_agent_view_runtime",
            f"Detected {agents} Agent View client(s) and {daemons} supervisor(s).",
        )

    cold_latency: float | None = None
    warm_p95: float | None = None
    if (
        current_swbctl.is_absolute()
        and current_swbctl.is_file()
        and os.access(current_swbctl, os.X_OK)
    ):
        try:
            cold_latency, warm_p95 = _latency_probe(
                current_swbctl,
                timeout_seconds=hooks.timeout_seconds,
                environment=environment,
                provider="claude",
            )
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
            add("error", "hook_latency_failed", error)
        else:
            if warm_p95 > hooks.latency_budget_ms:
                add(
                    "error",
                    "hook_latency_budget",
                    f"warm p95 {warm_p95:.1f} ms exceeds {hooks.latency_budget_ms} ms",
                )

    return DoctorResult(
        capability.available and not has_error,
        capability.provider_version,
        cold_latency,
        warm_p95,
        tuple(diagnostics),
    )


def run_all_doctors(
    *,
    config: SwitchboardConfig,
    swbctl_executable: str | Path,
    cwd: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> CombinedDoctorResult:
    """Run independent diagnostics for every enabled configured provider."""

    results: list[tuple[str, DoctorResult]] = []
    for provider in config.providers:
        if not provider.enabled:
            continue
        if provider.provider is ProviderId.CODEX:
            result = run_doctor(
                codex_executable=provider.executable or "codex",
                swbctl_executable=swbctl_executable,
                hooks=config.hooks,
                cwd=cwd,
                environment=environment,
            )
            name = "Codex"
        else:
            result = run_claude_doctor(
                claude_executable=provider.executable or "claude",
                swbctl_executable=swbctl_executable,
                hooks=config.hooks,
                environment=environment,
            )
            name = "Claude"
        results.append((name, result))
    return CombinedDoctorResult(
        bool(results) and all(result.healthy for _name, result in results),
        tuple(results),
    )


__all__ = [
    "CombinedDoctorResult",
    "DoctorDiagnostic",
    "DoctorResult",
    "run_all_doctors",
    "run_claude_doctor",
    "run_doctor",
]
