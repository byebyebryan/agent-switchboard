from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_switchboard.domain import HostId, PresentationContext, SessionKey
from agent_switchboard.protocol import (
    PresentationPlanEnvelope,
    SessionAction,
    SessionActionEnvelope,
    SessionActionStatus,
    SnapshotEnvelope,
)
from agent_switchboard.tui_gateway import (
    MAX_STDOUT_BYTES,
    CommandOutput,
    GatewayError,
    SnapshotSource,
    SwbctlGateway,
    resolve_terminal_context,
    run_bounded_command,
)

ROOT = Path(__file__).parents[1]
SNAPSHOT_FIXTURE = ROOT / "tests/fixtures/protocol/v1/snapshot.json"
PLAN_FIXTURE = ROOT / "tests/fixtures/protocol/v1/presentation-plan.json"
HOST_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"
LOCATION_ID = "44444444-4444-4444-8444-444444444444"
CODEX_SESSION_KEY = f"{HOST_ID}:codex:55555555-5555-4555-8555-555555555555"
CLAUDE_SESSION_KEY = f"{HOST_ID}:claude:66666666-6666-4666-8666-666666666666"
REQUEST_ID = "77777777-7777-4777-8777-777777777777"
TMUX_CLIENT = "/dev/pts/7"


def _record(
    envelope: SnapshotEnvelope | PresentationPlanEnvelope | SessionActionEnvelope,
) -> bytes:
    return envelope.to_json().encode("utf-8") + b"\n"


SNAPSHOT_RECORD = _record(SnapshotEnvelope.from_json(SNAPSHOT_FIXTURE.read_bytes()))
PLAN_RECORD = _record(PresentationPlanEnvelope.from_json(PLAN_FIXTURE.read_bytes()))
ACTION_RECORD = _record(
    SessionActionEnvelope(
        SessionAction(
            SessionActionStatus.STOPPED,
            HostId(HOST_ID),
            SessionKey.parse(CLAUDE_SESSION_KEY),
        )
    )
)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], float]] = []

    async def __call__(
        self,
        argv: Sequence[str],
        timeout_seconds: float,
    ) -> CommandOutput:
        command = tuple(argv)
        self.calls.append((command, timeout_seconds))
        records = {
            "snapshot": SNAPSHOT_RECORD,
            "prepare-open": PLAN_RECORD,
            "prepare-new": PLAN_RECORD,
            "prepare-history": PLAN_RECORD,
            "stop-session": ACTION_RECORD,
            "select-surface": b"",
        }
        return CommandOutput(records[command[1]], b"", 0)


def test_terminal_context_is_plain_or_exactly_inherited_tmux() -> None:
    class FakeTmux:
        def __init__(self) -> None:
            self.environments: list[dict[str, str]] = []

        def current_client(self, environment: dict[str, str]) -> str | None:
            self.environments.append(environment)
            return environment.get("EXPECTED_CLIENT")

    tmux = FakeTmux()
    plain = resolve_terminal_context(environment={}, tmux=tmux)
    nested = resolve_terminal_context(
        environment={"EXPECTED_CLIENT": TMUX_CLIENT},
        tmux=tmux,
    )

    assert plain == PresentationContext(True, None, False, False)
    assert nested == PresentationContext(True, TMUX_CLIENT, False, False)
    assert tmux.environments == [{}, {"EXPECTED_CLIENT": TMUX_CLIENT}]


def test_gateway_uses_exact_public_argv_and_reuses_request_id(tmp_path: Path) -> None:
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)
    runner = RecordingRunner()
    gateway = SwbctlGateway(executable, timeout_seconds=3, runner=runner)
    context = PresentationContext(True, TMUX_CLIENT, False, False)

    async def exercise() -> None:
        snapshot = await gateway.snapshot(reconcile="full")
        first = await gateway.prepare_open(
            CODEX_SESSION_KEY,
            request_id=REQUEST_ID,
            context=context,
        )
        second = await gateway.prepare_open(
            CODEX_SESSION_KEY,
            request_id=REQUEST_ID,
            context=context,
        )
        created = await gateway.prepare_new(
            PROJECT_ID,
            location_id=LOCATION_ID,
            provider="claude",
            request_id=REQUEST_ID,
            context=context,
        )
        history = await gateway.prepare_history(
            PROJECT_ID,
            location_id=LOCATION_ID,
            request_id=REQUEST_ID,
            context=context,
        )
        stopped = await gateway.stop_session(CLAUDE_SESSION_KEY)
        await gateway.select_surface(
            "33333333-3333-4333-8333-333333333333",
            client=TMUX_CLIENT,
        )

        assert snapshot.host.host_id == HostId(HOST_ID)
        assert first == second == created == history
        assert stopped.action.status is SessionActionStatus.STOPPED

    asyncio.run(exercise())

    prefix = str(executable)
    assert runner.calls == [
        ((prefix, "snapshot", "--reconcile", "full", "--json"), 3.0),
        (
            (
                prefix,
                "prepare-open",
                CODEX_SESSION_KEY,
                "--request-id",
                REQUEST_ID,
                "--has-current-terminal",
                "--current-tmux-client",
                TMUX_CLIENT,
                "--json",
            ),
            3.0,
        ),
        (
            (
                prefix,
                "prepare-open",
                CODEX_SESSION_KEY,
                "--request-id",
                REQUEST_ID,
                "--has-current-terminal",
                "--current-tmux-client",
                TMUX_CLIENT,
                "--json",
            ),
            3.0,
        ),
        (
            (
                prefix,
                "prepare-new",
                "--project",
                PROJECT_ID,
                "--location",
                LOCATION_ID,
                "--provider",
                "claude",
                "--request-id",
                REQUEST_ID,
                "--has-current-terminal",
                "--current-tmux-client",
                TMUX_CLIENT,
                "--json",
            ),
            3.0,
        ),
        (
            (
                prefix,
                "prepare-history",
                "--project",
                PROJECT_ID,
                "--location",
                LOCATION_ID,
                "--request-id",
                REQUEST_ID,
                "--has-current-terminal",
                "--current-tmux-client",
                TMUX_CLIENT,
                "--json",
            ),
            3.0,
        ),
        ((prefix, "stop-session", CLAUDE_SESSION_KEY, "--json"), 3.0),
        (
            (
                prefix,
                "select-surface",
                "33333333-3333-4333-8333-333333333333",
                "--client",
                TMUX_CLIENT,
            ),
            3.0,
        ),
    ]
    assert gateway.attach_surface_command("33333333-3333-4333-8333-333333333333") == (
        prefix,
        "attach-surface",
        "33333333-3333-4333-8333-333333333333",
    )


@pytest.mark.parametrize(
    ("output", "code"),
    [
        (CommandOutput(b"{}", b"", 0), "response_invalid"),
        (CommandOutput(b"{}\n{}\n", b"", 0), "response_invalid"),
        (CommandOutput(b"not-json\n", b"", 0), "response_invalid"),
        (CommandOutput(SNAPSHOT_RECORD, b"private diagnostic", 0), "response_invalid"),
        (CommandOutput(b"", b"private diagnostic", 1), "command_failed"),
    ],
)
def test_gateway_failures_are_small_and_do_not_expose_command_output(
    tmp_path: Path,
    output: CommandOutput,
    code: str,
) -> None:
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)

    async def runner(_argv: Sequence[str], _timeout: float) -> CommandOutput:
        return output

    gateway = SwbctlGateway(executable, runner=runner)
    with pytest.raises(GatewayError) as failure:
        asyncio.run(gateway.snapshot(reconcile="none"))
    assert failure.value.code == code
    assert "private diagnostic" not in str(failure.value)
    assert "not-json" not in str(failure.value)


def test_gateway_rejects_invalid_arguments_before_execution(tmp_path: Path) -> None:
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)
    runner = RecordingRunner()
    gateway = SwbctlGateway(executable, runner=runner)

    with pytest.raises(GatewayError, match="reconciliation"):
        asyncio.run(gateway.snapshot(reconcile="everything"))
    with pytest.raises(GatewayError, match="Session key"):
        asyncio.run(
            gateway.prepare_open(
                "invalid",
                request_id=REQUEST_ID,
                context=PresentationContext(True, None, False, False),
            )
        )
    with pytest.raises(GatewayError, match="terminal-local"):
        asyncio.run(
            gateway.prepare_new(
                PROJECT_ID,
                location_id=None,
                provider="codex",
                request_id=REQUEST_ID,
                context=PresentationContext(False, None, False, False),
            )
        )
    with pytest.raises(GatewayError, match="surface ID"):
        asyncio.run(gateway.select_surface("invalid", client=TMUX_CLIENT))
    with pytest.raises(GatewayError, match="tmux client"):
        asyncio.run(
            gateway.select_surface(
                "33333333-3333-4333-8333-333333333333",
                client="bad\nclient",
            )
        )
    with pytest.raises(GatewayError, match="surface ID"):
        gateway.attach_surface_command("invalid")
    assert runner.calls == []


@pytest.mark.parametrize(
    "output",
    (
        CommandOutput(b"secret output", b"", 0),
        CommandOutput(b"", b"private diagnostic", 0),
        CommandOutput(b"", b"private diagnostic", 1),
    ),
)
def test_select_surface_requires_silent_success(
    tmp_path: Path,
    output: CommandOutput,
) -> None:
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)

    async def runner(_argv: Sequence[str], _timeout: float) -> CommandOutput:
        return output

    gateway = SwbctlGateway(executable, runner=runner)
    with pytest.raises(GatewayError) as failure:
        asyncio.run(
            gateway.select_surface(
                "33333333-3333-4333-8333-333333333333",
                client=TMUX_CLIENT,
            )
        )
    assert "private diagnostic" not in str(failure.value)
    assert "secret output" not in str(failure.value)


def test_gateway_rejects_plan_for_a_different_tmux_client(tmp_path: Path) -> None:
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)
    runner = RecordingRunner()
    gateway = SwbctlGateway(executable, runner=runner)

    with pytest.raises(GatewayError, match="incompatible") as failure:
        asyncio.run(
            gateway.prepare_open(
                CODEX_SESSION_KEY,
                request_id=REQUEST_ID,
                context=PresentationContext(True, "/dev/pts/8", False, False),
            )
        )
    assert failure.value.code == "response_invalid"
    assert len(runner.calls) == 1


def test_gateway_rejects_stop_response_for_a_different_session(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)
    other_key = f"{HOST_ID}:claude:88888888-8888-4888-8888-888888888888"
    response = _record(
        SessionActionEnvelope(
            SessionAction(
                SessionActionStatus.STOPPED,
                HostId(HOST_ID),
                SessionKey.parse(other_key),
            )
        )
    )

    async def runner(_argv: Sequence[str], _timeout: float) -> CommandOutput:
        return CommandOutput(response, b"", 0)

    gateway = SwbctlGateway(executable, runner=runner)
    with pytest.raises(GatewayError) as failure:
        asyncio.run(gateway.stop_session(CLAUDE_SESSION_KEY))
    assert failure.value.code == "response_invalid"


def test_snapshot_source_coalesces_refresh_and_preserves_last_good(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)

    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        outputs = [
            CommandOutput(SNAPSHOT_RECORD, b"", 0),
            CommandOutput(b"", b"private diagnostic", 1),
        ]
        calls = 0

        async def runner(_argv: Sequence[str], _timeout: float) -> CommandOutput:
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
            return outputs[calls - 1]

        source = SnapshotSource(SwbctlGateway(executable, runner=runner))
        first_waiter = asyncio.create_task(source.refresh())
        await started.wait()
        second_waiter = asyncio.create_task(source.refresh())
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        first, second = await asyncio.gather(first_waiter, second_waiter)
        assert first is second
        assert source.last_good is first
        assert source.last_error is None

        fallback = await source.refresh()
        assert fallback is first
        assert calls == 2
        assert source.last_error is not None
        assert source.last_error.code == "command_failed"

    asyncio.run(exercise())


def test_bounded_runner_captures_success_and_rejects_overflow() -> None:
    success = asyncio.run(
        run_bounded_command(
            (
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'ok'); "
                "sys.stderr.buffer.write(b'note')",
            ),
            2,
        )
    )
    assert success == CommandOutput(b"ok", b"note", 0)

    overflow_code = (
        f"import sys; sys.stdout.buffer.write(b'x' * {MAX_STDOUT_BYTES + 1})"
    )
    with pytest.raises(GatewayError) as failure:
        asyncio.run(
            run_bounded_command(
                (sys.executable, "-c", overflow_code),
                2,
            )
        )
    assert failure.value.code == "stdout_overflow"


def _wait_for_pid_file(path: Path, *, timeout: float = 2) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="ascii").strip():
            return int(path.read_text(encoding="ascii"))
        time.sleep(0.01)
    raise AssertionError("test child did not publish its PID")


def _assert_process_disappears(pid: int, *, timeout: float = 2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"test process {pid} survived gateway cleanup")


def test_timeout_kills_the_command_process_group(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    code = (
        "import pathlib, subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)']); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid)); "
        "time.sleep(30)"
    )

    with pytest.raises(GatewayError) as failure:
        asyncio.run(run_bounded_command((sys.executable, "-c", code), 0.5))
    assert failure.value.code == "command_timeout"
    _assert_process_disappears(_wait_for_pid_file(child_pid))


def test_cancellation_kills_the_command_process(tmp_path: Path) -> None:
    parent_pid = tmp_path / "parent.pid"
    code = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(parent_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )

    async def exercise() -> None:
        task = asyncio.create_task(
            run_bounded_command((sys.executable, "-c", code), 10)
        )
        deadline = asyncio.get_running_loop().time() + 2
        while not parent_pid.exists():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("test command did not publish its PID")
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    _assert_process_disappears(_wait_for_pid_file(parent_pid))
