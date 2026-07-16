"""Bounded Linux process and tmux reconciliation for host-local runtimes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import UUID

from .domain import (
    Activity,
    ActivityReason,
    Attachment,
    HostId,
    NormalizedRuntimeObservation,
    ProviderId,
    RuntimePresence,
    SessionKey,
    ValidationError,
)
from .protocol import ErrorRecord, ErrorScope
from .storage import Registry, RuntimeObservationApplyResult, StorageError

LIVE_SOURCE_PRIORITY: Final = 200
MAX_PROC_PIDS: Final = 32_768
MAX_PROC_FDS: Final = 1_024
MAX_PROC_BYTES: Final = 64 * 1024
MAX_ANCESTRY_DEPTH: Final = 64
MAX_TMUX_OUTPUT_BYTES: Final = 1024 * 1024
MAX_TMUX_STDERR_BYTES: Final = 4096
MAX_TMUX_SOCKETS: Final = 64
TMUX_TIMEOUT_SECONDS: Final = 0.75
_ROLLOUT_RE: Final = re.compile(
    r"(?:^|/)sessions/[0-9]{4}/(?:0[1-9]|1[0-2])/(?:0[1-9]|[12][0-9]|3[01])/"
    r"rollout-[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3])-[0-5][0-9]-[0-5][0-9]-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12})\.jsonl$",
)
_CODEX_GLOBAL_VALUE_OPTIONS: Final = frozenset(
    {
        "--add-dir",
        "--ask-for-approval",
        "--cd",
        "--config",
        "--disable",
        "--enable",
        "--image",
        "--local-provider",
        "--model",
        "--profile",
        "--remote",
        "--remote-auth-token-env",
        "--sandbox",
        "-C",
        "-a",
        "-c",
        "-i",
        "-m",
        "-p",
        "-s",
    }
)
_CODEX_GLOBAL_FLAG_OPTIONS: Final = frozenset(
    {
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--no-alt-screen",
        "--oss",
        "--search",
        "--strict-config",
    }
)
_CODEX_EXIT_GLOBAL_FLAGS: Final = frozenset({"--help", "--version", "-h", "-V"})
_INTERACTIVE_CODEX_SUBCOMMANDS: Final = frozenset({"fork", "resume"})
_NONINTERACTIVE_CODEX_SUBCOMMANDS: Final = frozenset(
    {
        "a",
        "app-server",
        "apply",
        "archive",
        "cloud",
        "code-mode-host",
        "completion",
        "debug",
        "delete",
        "doctor",
        "e",
        "exec",
        "exec-server",
        "features",
        "help",
        "login",
        "logout",
        "mcp",
        "mcp-server",
        "plugin",
        "remote-control",
        "review",
        "sandbox",
        "unarchive",
        "update",
    }
)
_TMUX_AUTHORITATIVE_ABSENCE_RE: Final = re.compile(
    r"(?:no server running(?: on .+)?|"
    r"failed to connect to server: no such file or directory|"
    r"error connecting to .+ \(no such file or directory\))"
)


class _TmuxOutputLimitExceeded(RuntimeError):
    pass


def _tmux_authoritative_absence(stderr: bytes) -> bool:
    diagnostic = stderr.decode("utf-8", "replace").strip().casefold()
    return bool(_TMUX_AUTHORITATIVE_ABSENCE_RE.fullmatch(diagnostic))


@dataclass(frozen=True, slots=True)
class RuntimeProbeIssue:
    code: str
    message: str
    retryable: bool = True
    session_key: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessEvidence:
    pid: int
    parent_pid: int
    start_ticks: str
    birth_id: str
    argv: tuple[str, ...]
    provider_session_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class ProcessScan:
    processes: tuple[ProcessEvidence, ...]
    parents: Mapping[int, int]
    complete: bool
    issues: tuple[RuntimeProbeIssue, ...]


@dataclass(frozen=True, slots=True)
class TmuxPaneEvidence:
    socket: str
    pane_id: str
    pane_pid: int
    session: str
    window: str
    pane: str
    attached: bool


@dataclass(frozen=True, slots=True)
class TmuxScan:
    panes: tuple[TmuxPaneEvidence, ...]
    complete: bool
    issues: tuple[RuntimeProbeIssue, ...]


@dataclass(frozen=True, slots=True)
class LiveReconciliationResult:
    application: RuntimeObservationApplyResult
    errors: tuple[ErrorRecord, ...]


TmuxRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[bytes]]
TmuxSocketStat = Callable[[str], os.stat_result]


def _digest(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _bounded_bytes(path: Path, maximum: int = MAX_PROC_BYTES) -> bytes:
    with path.open("rb") as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError(f"bounded proc read exceeded {maximum} bytes")
    return value


def _process_stat(path: Path) -> tuple[int, str]:
    raw = _bounded_bytes(path).decode("ascii")
    close = raw.rfind(")")
    if close < 2:
        raise ValueError("malformed process stat")
    fields = raw[close + 2 :].split()
    if len(fields) < 20:
        raise ValueError("malformed process stat")
    parent_pid = int(fields[1])
    start_ticks = fields[19]
    if parent_pid < 0 or not start_ticks.isdecimal():
        raise ValueError("malformed process stat")
    return parent_pid, start_ticks


def _argv(path: Path) -> tuple[str, ...]:
    raw = _bounded_bytes(path)
    return tuple(os.fsdecode(part) for part in raw.rstrip(b"\0").split(b"\0") if part)


def _interactive_codex(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    executable = Path(argv[0]).name
    if executable == "codex":
        tail = argv[1:]
    else:
        return False

    index = 0
    subcommand: str | None = None
    while index < len(tail):
        argument = tail[index]
        if argument == "--":
            break
        option = argument.split("=", 1)[0]
        if option in _CODEX_GLOBAL_VALUE_OPTIONS:
            if "=" not in argument:
                index += 1
                if index >= len(tail):
                    return False
            index += 1
            continue
        if option in _CODEX_GLOBAL_FLAG_OPTIONS:
            index += 1
            continue
        if option in _CODEX_EXIT_GLOBAL_FLAGS:
            return False
        if argument.startswith("-"):
            # An unknown option has unknown arity. Failing closed avoids
            # mistaking its value (for example ``exec``) for an interactive
            # prompt or subcommand.
            return False
        subcommand = argument
        break
    return (
        subcommand is None
        or subcommand in _INTERACTIVE_CODEX_SUBCOMMANDS
        or subcommand not in _NONINTERACTIVE_CODEX_SUBCOMMANDS
    )


def _fd_session_ids(directory: Path) -> frozenset[str]:
    values: set[str] = set()
    entries = sorted(
        (entry for entry in directory.iterdir() if entry.name.isdecimal()),
        key=lambda entry: int(entry.name),
    )
    if len(entries) > MAX_PROC_FDS:
        raise ValueError("process has too many file descriptors to inspect safely")
    for entry in entries:
        try:
            target = os.readlink(entry)
        except FileNotFoundError:
            continue
        if len(target) > MAX_PROC_BYTES:
            raise ValueError("file descriptor target exceeds the safe size")
        match = _ROLLOUT_RE.search(target)
        if match is None:
            continue
        candidate = match.group(1).lower()
        try:
            if str(UUID(candidate)) == candidate:
                values.add(candidate)
        except ValueError:
            continue
    return frozenset(values)


def scan_codex_processes(
    *,
    proc_root: Path = Path("/proc"),
    uid: int | None = None,
) -> ProcessScan:
    """Return bounded same-UID interactive Codex process evidence."""

    expected_uid = os.getuid() if uid is None else uid
    issues: list[RuntimeProbeIssue] = []
    try:
        boot_id = (
            _bounded_bytes(proc_root / "sys/kernel/random/boot_id", 256)
            .decode("ascii")
            .strip()
        )
        UUID(boot_id)
        entries = sorted(
            (entry for entry in proc_root.iterdir() if entry.name.isdecimal()),
            key=lambda entry: int(entry.name),
        )
    except (OSError, UnicodeError, ValueError) as error:
        return ProcessScan(
            (),
            {},
            False,
            (
                RuntimeProbeIssue(
                    "process_probe_unavailable",
                    f"Linux process liveness is unavailable: {type(error).__name__}.",
                ),
            ),
        )
    if len(entries) > MAX_PROC_PIDS:
        return ProcessScan(
            (),
            {},
            False,
            (
                RuntimeProbeIssue(
                    "process_probe_truncated",
                    "Linux process enumeration exceeded the safe PID bound.",
                ),
            ),
        )

    complete = True
    parents: dict[int, int] = {}
    processes: list[ProcessEvidence] = []
    for entry in entries:
        pid = int(entry.name)
        try:
            if entry.stat().st_uid != expected_uid:
                continue
            parent_pid, start_ticks = _process_stat(entry / "stat")
            parents[pid] = parent_pid
            argv = _argv(entry / "cmdline")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError):
            complete = False
            continue
        if not _interactive_codex(argv):
            continue
        try:
            provider_session_ids = _fd_session_ids(entry / "fd")
        except FileNotFoundError:
            provider_session_ids = frozenset()
        except (OSError, ValueError):
            complete = False
            provider_session_ids = frozenset()
        birth_id = _digest({"boot": boot_id, "pid": pid, "start": start_ticks})
        processes.append(
            ProcessEvidence(
                pid,
                parent_pid,
                start_ticks,
                birth_id,
                argv,
                provider_session_ids,
            )
        )
    if not complete:
        issues.append(
            RuntimeProbeIssue(
                "process_probe_incomplete",
                "Some same-user process evidence could not be read; retained "
                "runtime state was not destructively changed.",
            )
        )
    return ProcessScan(tuple(processes), parents, complete, tuple(issues))


def _default_tmux_runner(
    argv: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[bytes]:
    command = list(argv)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    buffers = {
        process.stdout.fileno(): (process.stdout, MAX_TMUX_OUTPUT_BYTES, bytearray()),
        process.stderr.fileno(): (process.stderr, MAX_TMUX_STDERR_BYTES, bytearray()),
    }
    for descriptor, (stream, _maximum, _buffer) in buffers.items():
        os.set_blocking(descriptor, False)
        selector.register(stream, selectors.EVENT_READ, descriptor)
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(command, timeout)
            for key, _mask in events:
                descriptor = key.data
                stream, maximum, buffer = buffers[descriptor]
                chunk = os.read(descriptor, min(65_536, maximum - len(buffer) + 1))
                if not chunk:
                    selector.unregister(stream)
                    continue
                buffer.extend(chunk)
                if len(buffer) > maximum:
                    raise _TmuxOutputLimitExceeded
        returncode = process.wait(max(0.0, deadline - time.monotonic()))
        return subprocess.CompletedProcess(
            command,
            returncode,
            bytes(buffers[process.stdout.fileno()][2]),
            bytes(buffers[process.stderr.fileno()][2]),
        )
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _current_tmux_socket(environment: Mapping[str, str]) -> str | None:
    value = environment.get("TMUX")
    if not value:
        return None
    pieces = value.rsplit(",", 2)
    if len(pieces) != 3 or not Path(pieces[0]).is_absolute():
        return None
    return pieces[0]


def scan_tmux_panes(
    sockets: Sequence[str | None],
    *,
    runner: TmuxRunner = _default_tmux_runner,
    socket_lstat: TmuxSocketStat = os.lstat,
) -> TmuxScan:
    """Inspect only explicitly selected tmux sockets with bounded commands."""

    panes: list[TmuxPaneEvidence] = []
    issues: list[RuntimeProbeIssue] = []
    complete = True
    selected = tuple(dict.fromkeys(sockets))
    if len(selected) > MAX_TMUX_SOCKETS:
        return TmuxScan(
            (),
            False,
            (
                RuntimeProbeIssue(
                    "tmux_socket_limit_exceeded",
                    "tmux socket discovery exceeded the safe bound; retained "
                    "attachment state was preserved.",
                ),
            ),
        )
    fields = (
        "#{socket_path}\t#{pane_id}\t#{pane_pid}\t#{session_name}\t"
        "#{window_index}\t#{pane_index}\t#{session_attached}"
    )
    for socket in selected:
        if socket is not None:
            try:
                socket_lstat(socket)
            except FileNotFoundError:
                continue
            except OSError:
                complete = False
                issues.append(
                    RuntimeProbeIssue(
                        "tmux_probe_failed",
                        "tmux socket accessibility could not be established; "
                        "retained attachment state was preserved.",
                    )
                )
                continue
        argv = ["tmux"]
        if socket is not None:
            argv.extend(("-S", socket))
        argv.extend(("list-panes", "-a", "-F", fields))
        try:
            result = runner(argv, TMUX_TIMEOUT_SECONDS)
        except FileNotFoundError:
            complete = False
            issues.append(
                RuntimeProbeIssue(
                    "tmux_not_found",
                    "tmux is unavailable; retained attachment state was preserved.",
                )
            )
            break
        except subprocess.TimeoutExpired:
            complete = False
            issues.append(
                RuntimeProbeIssue(
                    "tmux_probe_timeout",
                    "tmux liveness inspection exceeded its timeout.",
                )
            )
            continue
        except _TmuxOutputLimitExceeded:
            complete = False
            issues.append(
                RuntimeProbeIssue(
                    "tmux_probe_oversized",
                    "tmux liveness output exceeded the safe bound.",
                )
            )
            continue
        except OSError as error:
            complete = False
            issues.append(
                RuntimeProbeIssue(
                    "tmux_probe_unavailable",
                    f"tmux liveness inspection failed: {type(error).__name__}.",
                )
            )
            continue
        if (
            len(result.stdout) > MAX_TMUX_OUTPUT_BYTES
            or len(result.stderr) > MAX_TMUX_STDERR_BYTES
        ):
            complete = False
            issues.append(
                RuntimeProbeIssue(
                    "tmux_probe_oversized",
                    "tmux liveness output exceeded the safe bound.",
                )
            )
            continue
        if result.returncode != 0:
            if _tmux_authoritative_absence(result.stderr):
                continue
            complete = False
            issues.append(
                RuntimeProbeIssue(
                    "tmux_probe_failed",
                    "tmux liveness inspection returned an unexpected failure.",
                )
            )
            continue
        try:
            for raw_line in result.stdout.splitlines():
                columns = raw_line.decode("utf-8").split("\t")
                if len(columns) != 7:
                    raise ValueError("unexpected tmux field count")
                socket_path, pane_id, pane_pid, session, window, pane, attached = (
                    columns
                )
                if (
                    not Path(socket_path).is_absolute()
                    or (socket is not None and socket_path != socket)
                    or not pane_id.startswith("%")
                    or not pane_id[1:].isdecimal()
                    or not pane_pid.isdecimal()
                    or int(pane_pid) <= 0
                    or not attached.isdecimal()
                    or any(len(value) > 256 for value in columns[1:])
                    or any("\x00" in value or "\n" in value for value in columns)
                ):
                    raise ValueError("invalid tmux field")
                panes.append(
                    TmuxPaneEvidence(
                        socket_path,
                        pane_id,
                        int(pane_pid),
                        session,
                        window,
                        pane,
                        int(attached) > 0,
                    )
                )
        except (UnicodeError, ValueError):
            complete = False
            issues.append(
                RuntimeProbeIssue(
                    "tmux_probe_malformed",
                    "tmux liveness output did not match the expected schema.",
                )
            )
    return TmuxScan(tuple(panes), complete, tuple(issues))


def _pane_for_process(
    process: ProcessEvidence,
    panes: Sequence[TmuxPaneEvidence],
    parents: Mapping[int, int],
) -> TmuxPaneEvidence | None:
    distances: dict[int, int] = {}
    current = process.pid
    for distance in range(MAX_ANCESTRY_DEPTH + 1):
        if current <= 0 or current in distances:
            break
        distances[current] = distance
        current = parents.get(current, 0)
    candidates = [pane for pane in panes if pane.pane_pid in distances]
    if not candidates:
        return None
    return min(candidates, key=lambda pane: (distances[pane.pane_pid], pane.socket))


def _error_record(
    issue: RuntimeProbeIssue,
    host_id: HostId,
    observed_at: int,
) -> ErrorRecord:
    session_key = (
        None if issue.session_key is None else SessionKey.parse(issue.session_key)
    )
    scope = ErrorScope.HOST if session_key is None else ErrorScope.SESSION
    return ErrorRecord.from_dict(
        ErrorRecord(
            issue.code,
            issue.message,
            scope,
            issue.retryable,
            observed_at,
            host_id=host_id,
            provider=ProviderId.CODEX,
            session_key=session_key,
        ).to_dict()
    )


def reconcile_live(
    registry: Registry,
    host_id: str,
    *,
    proc_root: Path = Path("/proc"),
    uid: int | None = None,
    environment: Mapping[str, str] | None = None,
    tmux_runner: TmuxRunner = _default_tmux_runner,
    tmux_socket_lstat: TmuxSocketStat = os.lstat,
    entry_ns: int | None = None,
) -> LiveReconciliationResult:
    """Repair Codex runtime truth from one bounded process/tmux observation."""

    try:
        parsed_host = HostId(host_id)
    except ValidationError as error:
        raise StorageError("host_id must be a non-nil UUID") from error
    if str(parsed_host) != host_id:
        raise StorageError("host_id must use canonical lowercase UUID spelling")
    timestamp_ns = time.time_ns() if entry_ns is None else entry_ns
    if (
        isinstance(timestamp_ns, bool)
        or not isinstance(timestamp_ns, int)
        or timestamp_ns < 0
    ):
        raise StorageError("entry_ns must be a non-negative integer")
    observed_at = timestamp_ns // 1_000_000
    sessions = tuple(
        row
        for row in registry.list_sessions(host_id=host_id)
        if row["provider"] == ProviderId.CODEX.value
    )

    process_scan = scan_codex_processes(proc_root=proc_root, uid=uid)
    env = os.environ if environment is None else environment
    sockets: list[str | None] = [None]
    current_socket = _current_tmux_socket(env)
    if current_socket is not None:
        sockets.append(current_socket)
    sockets.extend(
        str(row["tmux_socket"]) for row in sessions if row["tmux_socket"] is not None
    )
    tmux_scan = scan_tmux_panes(
        sockets,
        runner=tmux_runner,
        socket_lstat=tmux_socket_lstat,
    )
    issues = [*process_scan.issues, *tmux_scan.issues]

    by_pid = {process.pid: process for process in process_scan.processes}
    by_birth: dict[str, list[ProcessEvidence]] = {}
    for process in process_scan.processes:
        by_birth.setdefault(process.birth_id, []).append(process)
    claimed_pids: set[int] = set()
    observations: list[NormalizedRuntimeObservation] = []
    for row in sessions:
        key = SessionKey.parse(str(row["session_key"]))
        process: ProcessEvidence | None = None
        stored_pid = row["runtime_pid"]
        stored_birth = row["runtime_process_birth_id"]
        if stored_pid is not None and stored_birth is not None:
            candidate = by_pid.get(int(stored_pid))
            if candidate is not None and candidate.birth_id == stored_birth:
                process = candidate
        if process is None and stored_birth is not None:
            birth_matches = by_birth.get(str(stored_birth), ())
            if len(birth_matches) == 1:
                process = birth_matches[0]
        if process is None:
            ambiguous_fallback = [
                candidate
                for candidate in process_scan.processes
                if str(key.provider_session_id) in candidate.provider_session_ids
                and len(candidate.provider_session_ids) != 1
            ]
            if ambiguous_fallback:
                issues.append(
                    RuntimeProbeIssue(
                        "runtime_correlation_ambiguous",
                        "A Codex process referenced more than one durable session; "
                        "state was preserved.",
                        session_key=str(key),
                    )
                )
                continue
            fallback = [
                candidate
                for candidate in process_scan.processes
                if candidate.provider_session_ids
                == frozenset((str(key.provider_session_id),))
            ]
            if len(fallback) == 1:
                process = fallback[0]
            elif len(fallback) > 1:
                issues.append(
                    RuntimeProbeIssue(
                        "runtime_correlation_ambiguous",
                        "More than one Codex process matched the retained session; "
                        "state was preserved.",
                        session_key=str(key),
                    )
                )
                continue
        if process is not None and process.pid in claimed_pids:
            issues.append(
                RuntimeProbeIssue(
                    "runtime_correlation_ambiguous",
                    "One Codex process matched more than one retained session; "
                    "state was preserved.",
                    session_key=str(key),
                )
            )
            continue

        if process is None:
            has_confirmable_locator = stored_pid is not None or stored_birth is not None
            if (
                process_scan.complete
                and has_confirmable_locator
                and row["runtime_presence"] == RuntimePresence.LIVE.value
            ):
                attachment = Attachment.NONE if tmux_scan.complete else None
                values = {
                    "session": str(key),
                    "presence": RuntimePresence.STOPPED.value,
                    "tmux_observed": tmux_scan.complete,
                    "attachment": (None if attachment is None else attachment.value),
                }
                observations.append(
                    NormalizedRuntimeObservation(
                        f"live:{_digest(values)}",
                        parsed_host,
                        ProviderId.CODEX,
                        key,
                        "liveness",
                        LIVE_SOURCE_PRIORITY,
                        timestamp_ns,
                        observed_at,
                        runtime_presence=RuntimePresence.STOPPED,
                        activity=Activity.UNKNOWN,
                        activity_reason=ActivityReason.UNKNOWN,
                        attachment=attachment,
                        tmux_observed=tmux_scan.complete,
                    )
                )
            continue

        claimed_pids.add(process.pid)
        pane = _pane_for_process(process, tmux_scan.panes, process_scan.parents)
        tmux_observed = pane is not None or tmux_scan.complete
        attachment = None
        if pane is not None:
            attachment = Attachment.ATTACHED if pane.attached else Attachment.DETACHED
        elif tmux_scan.complete:
            attachment = Attachment.NONE
        values = {
            "session": str(key),
            "pid": process.pid,
            "birth": process.birth_id,
            "socket": None if pane is None else pane.socket,
            "pane": None if pane is None else pane.pane_id,
            "attached": None if attachment is None else attachment.value,
        }
        observations.append(
            NormalizedRuntimeObservation(
                f"live:{_digest(values)}",
                parsed_host,
                ProviderId.CODEX,
                key,
                "liveness",
                LIVE_SOURCE_PRIORITY,
                timestamp_ns,
                observed_at,
                runtime_presence=RuntimePresence.LIVE,
                attachment=attachment,
                pid=process.pid,
                process_birth_id=process.birth_id,
                tmux_observed=tmux_observed,
                tmux_socket=None if pane is None else pane.socket,
                tmux_session=None if pane is None else pane.session,
                tmux_window=None if pane is None else pane.window,
                tmux_pane=None if pane is None else pane.pane_id,
            )
        )

    application = registry.apply_runtime_observations(tuple(observations))
    errors = tuple(
        _error_record(issue, parsed_host, observed_at)
        for issue in dict.fromkeys(issues)
    )
    return LiveReconciliationResult(application, errors)


__all__ = [
    "LIVE_SOURCE_PRIORITY",
    "LiveReconciliationResult",
    "ProcessEvidence",
    "ProcessScan",
    "RuntimeProbeIssue",
    "TmuxPaneEvidence",
    "TmuxScan",
    "reconcile_live",
    "scan_codex_processes",
    "scan_tmux_panes",
]
