#!/usr/bin/env python3
"""Prove effective Claude hook loading without making a model request."""

from __future__ import annotations

import argparse
import json
import math
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path

from agent_switchboard.hook_config import canonical_claude_hook_groups
from agent_switchboard.paths import database_path
from agent_switchboard.providers.claude import (
    CLAUDE_FEATURES,
    CLAUDE_TESTED_CONTRACT_MIN,
    ClaudeProvider,
    ClaudeSettingsInspection,
)
from agent_switchboard.storage import Registry

DEFAULT_CLAUDE_EXECUTABLE = "claude"
DEFAULT_SWBCTL_EXECUTABLE = "swbctl"
EXPECTED_EVENTS = ("SessionStart", "UserPromptSubmit", "SessionEnd")
MAX_PROVIDER_OUTPUT_BYTES = 1024 * 1024
MAX_PROVIDER_ERROR_BYTES = 1024 * 1024
MAX_BLOCK_INPUT_BYTES = 1024 * 1024
_BLOCK_HOOK_CODE = (
    "import sys\n"
    f"payload = sys.stdin.buffer.read({MAX_BLOCK_INPUT_BYTES + 1})\n"
    f"if len(payload) > {MAX_BLOCK_INPUT_BYTES}:\n"
    "    raise SystemExit(2)\n"
    'print("{\\"decision\\":\\"block\\",\\"reason\\":'
    '\\"Switchboard no-model lifecycle probe.\\"}")\n'
)


class _SmokeFailure(RuntimeError):
    """Internal sentinel whose details are never printed."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate effective Claude lifecycle hooks with a no-model blocking "
            "probe and print only a sanitized summary."
        )
    )
    parser.add_argument(
        "--claude",
        default=DEFAULT_CLAUDE_EXECUTABLE,
        help=f"Claude executable (default: {DEFAULT_CLAUDE_EXECUTABLE})",
    )
    parser.add_argument(
        "--swbctl",
        default=DEFAULT_SWBCTL_EXECUTABLE,
        help=f"swbctl executable (default: {DEFAULT_SWBCTL_EXECUTABLE})",
    )
    parser.add_argument(
        "--timeout-seconds",
        default=20.0,
        type=float,
        help="bounded Claude probe deadline (default: 20)",
    )
    return parser


def _resolved_executable(value: str, *, require_name: str | None = None) -> Path:
    if not value or "\x00" in value:
        raise _SmokeFailure
    candidate = Path(value)
    if not candidate.is_absolute():
        from shutil import which

        found = which(value)
        if found is None:
            raise _SmokeFailure
        candidate = Path(found)
    candidate = candidate.absolute()
    if (
        (require_name is not None and candidate.name != require_name)
        or not candidate.is_file()
        or not os.access(candidate, os.X_OK)
    ):
        raise _SmokeFailure
    return candidate


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1.0)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def _run_bounded(
    argv: Sequence[str],
    *,
    cwd: Path,
    environ: Mapping[str, str],
    input_bytes: bytes,
    timeout_seconds: float,
) -> tuple[int, bytes]:
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(environ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )
    except OSError as error:
        raise _SmokeFailure from error
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr_seen = 0
    deadline = time.monotonic() + timeout_seconds
    try:
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
        except OSError as error:
            raise _SmokeFailure from error
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _SmokeFailure
            for key, _mask in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    if len(stdout) + len(chunk) > MAX_PROVIDER_OUTPUT_BYTES:
                        raise _SmokeFailure
                    stdout.extend(chunk)
                else:
                    stderr_seen += len(chunk)
                    if stderr_seen > MAX_PROVIDER_ERROR_BYTES:
                        raise _SmokeFailure
        try:
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise _SmokeFailure from error
        if returncode < 0:
            raise _SmokeFailure
        return returncode, bytes(stdout)
    finally:
        selector.close()
        _terminate_group(process)
        process.stdout.close()
        process.stderr.close()


def _settings_document(swbctl: Path) -> dict[str, object]:
    groups = canonical_claude_hook_groups(swbctl, timeout_seconds=2)
    hooks = {event: [group] for event, group in groups.items()}
    hooks["UserPromptSubmit"].append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": str(Path(sys.executable).absolute()),
                    "args": ["-c", _BLOCK_HOOK_CODE],
                    "timeout": 2,
                }
            ]
        }
    )
    return {
        "disableAgentView": True,
        "disableAllHooks": False,
        "hooks": hooks,
    }


def _number(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise _SmokeFailure
    return float(value)


def _run_smoke(
    *,
    claude: Path,
    swbctl: Path,
    timeout_seconds: float,
) -> dict[str, object]:
    if not math.isfinite(timeout_seconds) or not 1.0 <= timeout_seconds <= 120.0:
        raise _SmokeFailure
    started = time.monotonic()
    capability = ClaudeProvider(
        executable=str(claude),
        command_timeout=min(timeout_seconds, 5.0),
    ).inspect_capability(
        ClaudeSettingsInspection(
            path=Path("/isolated/claude-settings.json"),
            disable_agent_view=True,
            disable_all_hooks=False,
            allow_managed_hooks_only=None,
        )
    )
    if (
        not capability.available
        or capability.provider_version != CLAUDE_TESTED_CONTRACT_MIN
        or capability.features != CLAUDE_FEATURES
        or capability.degraded_reasons
    ):
        raise _SmokeFailure

    private_sentinel = f"switchboard-private-{uuid.uuid4()}"
    prompt = f"{private_sentinel}\n"
    with tempfile.TemporaryDirectory(prefix="switchboard-claude-smoke-") as raw:
        root = Path(raw)
        configuration = root / "configuration"
        state = root / "state"
        settings = root / "claude-settings.json"
        settings.write_text(
            json.dumps(
                _settings_document(swbctl),
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        settings.chmod(0o600)
        environment = os.environ.copy()
        for key in (
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_CODE_USE_VERTEX",
        ):
            environment.pop(key, None)
        environment.update(
            {
                "ANTHROPIC_API_KEY": "switchboard-intentionally-invalid",
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
                "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1",
                "XDG_CONFIG_HOME": str(configuration),
                "XDG_STATE_HOME": str(state),
            }
        )
        _returncode, output = _run_bounded(
            (
                str(claude),
                "--print",
                "--output-format",
                "json",
                "--no-session-persistence",
                "--setting-sources",
                "project",
                "--settings",
                str(settings),
            ),
            cwd=root,
            environ=environment,
            input_bytes=prompt.encode("utf-8"),
            timeout_seconds=timeout_seconds,
        )
        try:
            result = json.loads(output)
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise _SmokeFailure from error
        if not isinstance(result, dict):
            raise _SmokeFailure
        turns = result.get("num_turns")
        if isinstance(turns, bool) or not isinstance(turns, int) or turns != 0:
            raise _SmokeFailure
        cost = _number(result.get("total_cost_usd"))
        if cost != 0.0:
            raise _SmokeFailure

        database = database_path(environ=environment)
        with Registry(database) as registry:
            sessions = [
                session
                for session in registry.list_sessions()
                if session["provider"] == "claude"
            ]
            rows = registry.connection.execute(
                """
                SELECT event_kind, COUNT(*) AS event_count
                FROM events
                WHERE provider = 'claude'
                GROUP BY event_kind
                ORDER BY event_kind
                """
            ).fetchall()
        event_counts = {str(row["event_kind"]): int(row["event_count"]) for row in rows}
        if len(sessions) != 1 or event_counts != {
            event: 1 for event in EXPECTED_EVENTS
        }:
            raise _SmokeFailure
        if private_sentinel.encode("utf-8") in database.read_bytes():
            raise _SmokeFailure

    return {
        "elapsedMs": int((time.monotonic() - started) * 1_000),
        "eventCounts": event_counts,
        "features": sorted(capability.features),
        "providerVersion": capability.provider_version,
        "reportedCostUsd": cost,
        "reportedTurns": turns,
        "sessionCount": len(sessions),
    }


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    arguments = build_parser().parse_args(raw_arguments)
    try:
        claude = _resolved_executable(arguments.claude)
        swbctl = _resolved_executable(arguments.swbctl, require_name="swbctl")
        summary = _run_smoke(
            claude=claude,
            swbctl=swbctl,
            timeout_seconds=arguments.timeout_seconds,
        )
    except Exception:
        print("live Claude smoke failed", file=sys.stderr)
        return 1
    print(
        json.dumps(
            summary,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
