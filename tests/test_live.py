from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_switchboard.domain import (
    Activity,
    ActivityReason,
    Attachment,
    HostId,
    NormalizedRuntimeObservation,
    ProviderId,
    RuntimePresence,
    SessionKey,
)
from agent_switchboard.hooks import normalize_codex_event
from agent_switchboard.live import (
    LIVE_SOURCE_PRIORITY,
    MAX_TMUX_OUTPUT_BYTES,
    MAX_TMUX_SOCKETS,
    reconcile_live,
    scan_codex_processes,
    scan_tmux_panes,
)
from agent_switchboard.storage import IdentityConflict, Registry, StorageError
from agent_switchboard.tmux import TmuxLocator

HOST_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
SECOND_SESSION_ID = "33333333-3333-4333-8333-333333333333"
V7_SESSION_ID = "019a1234-5678-7abc-8def-0123456789ab"
SESSION_KEY = f"{HOST_ID}:codex:{SESSION_ID}"
BOOT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
LAUNCH_ID = "44444444-4444-4444-8444-444444444444"
SURFACE_ID = "55555555-5555-4555-8555-555555555555"
REQUEST_ID = "66666666-6666-4666-8666-666666666666"
PROJECT_ID = "77777777-7777-4777-8777-777777777777"
LOCATION_ID = "88888888-8888-4888-8888-888888888888"
HANDOFF_ID = "99999999-9999-4999-8999-999999999999"
TASK_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _proc_entry(
    root: Path,
    pid: int,
    *,
    ppid: int,
    argv: tuple[str, ...],
    start: int,
    session_ids: tuple[str, ...] = (),
) -> None:
    directory = root / str(pid)
    (directory / "fd").mkdir(parents=True)
    fields = ["S", str(ppid), *(["0"] * 17), str(start)]
    (directory / "stat").write_text(
        f"{pid} ({Path(argv[0]).name}) {' '.join(fields)}\n",
        encoding="ascii",
    )
    (directory / "cmdline").write_bytes(b"\0".join(os.fsencode(item) for item in argv))
    for number, session_id in enumerate(session_ids):
        os.symlink(
            "/home/test/.codex/sessions/2026/07/16/"
            f"rollout-2026-07-16T12-00-00-{session_id}.jsonl",
            directory / "fd" / str(number + 3),
        )


def _proc_root(tmp_path: Path) -> Path:
    root = tmp_path / "proc"
    boot = root / "sys/kernel/random"
    boot.mkdir(parents=True)
    (boot / "boot_id").write_text(f"{BOOT_ID}\n", encoding="ascii")
    return root


def _tmux_runner(stdout: bytes = b"", *, returncode: int = 0, stderr: bytes = b""):
    def run(
        argv: tuple[str, ...] | list[str], timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        assert argv[0] == "tmux"
        assert timeout < 1
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return run


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    value = Registry(tmp_path / "switchboard.db")
    value.upsert_host(HOST_ID, "local", is_local=True, observed_at=1)
    value.upsert_session(
        {
            "session_key": SESSION_KEY,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": SESSION_ID,
            "cwd": "/work/project",
            "runtime_presence": "unknown",
            "resumability": "resumable",
            "activity": "ready",
            "activity_reason": "turn_complete",
            "attachment": "detached",
            "metadata_source": "provider",
            "first_observed_at": 1,
            "last_observed_at": 1,
        }
    )
    yield value
    value.close()


def _observation(
    *,
    key: str,
    session_key: str = SESSION_KEY,
    entry_ns: int,
    presence: RuntimePresence,
    pid: int | None = None,
    birth: str | None = None,
    activity: Activity | None = None,
    reason: ActivityReason | None = None,
    attachment: Attachment | None = None,
    tmux_observed: bool = False,
    pane: str | None = None,
    source: str = "test",
    launch_id: str | None = None,
) -> NormalizedRuntimeObservation:
    parsed = SessionKey.parse(session_key)
    return NormalizedRuntimeObservation(
        key,
        HostId(HOST_ID),
        ProviderId.CODEX,
        parsed,
        source,
        LIVE_SOURCE_PRIORITY,
        entry_ns,
        entry_ns // 1_000_000,
        runtime_presence=presence,
        activity=activity,
        activity_reason=reason,
        attachment=attachment,
        pid=pid,
        process_birth_id=birth,
        tmux_observed=tmux_observed,
        tmux_socket="/tmp/fake-tmux" if pane is not None else None,
        tmux_session="work" if pane is not None else None,
        tmux_window="0" if pane is not None else None,
        tmux_pane=pane,
        launch_id=launch_id,
    )


def _prepare_pending_resume(registry: Registry, locator: TmuxLocator) -> None:
    registry.reserve_launch(
        {
            "host_id": HOST_ID,
            "provider": "codex",
            "action": "resume",
            "project_id": None,
            "checkout_id": None,
            "cwd": None,
            "source_handoff_id": None,
            "target_session_key": SESSION_KEY,
            "transport": "tmux",
        },
        request_id=REQUEST_ID,
        launch_id=LAUNCH_ID,
        lease_owner="bootstrap",
        capability_hash="a" * 64,
        expires_at=150,
        created_at=100,
    )
    registry.activate_launch_surface(
        LAUNCH_ID,
        {
            "surface_id": SURFACE_ID,
            "host_id": HOST_ID,
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": locator.to_storage(),
            "role": "session",
            "created_at": 110,
            "last_observed_at": 110,
        },
        lease_owner="bootstrap",
        observed_at=110,
    )
    registry.transition_launch(
        LAUNCH_ID,
        "provider_started",
        lease_owner="bootstrap",
        observed_at=120,
    )


def _prepare_pending_new(
    registry: Registry,
    locator: TmuxLocator,
    *,
    source_handoff_id: str | None = None,
) -> None:
    registry.materialize_projects(
        HOST_ID,
        [
            {
                "project_id": PROJECT_ID,
                "name": "project",
                "default_provider": "codex",
                "default_transport": "tmux",
                "checkouts": [
                    {
                        "checkout_id": LOCATION_ID,
                        "path": "/work/project",
                        "is_default": True,
                    }
                ],
            }
        ],
        observed_at=2,
    )
    registry.create_task(
        task_id=TASK_ID,
        host_id=HOST_ID,
        project_id=PROJECT_ID,
        checkout_id=LOCATION_ID,
        title="Pending launch",
        observed_at=3,
    )
    if source_handoff_id is not None:
        source = registry.get_handoff(source_handoff_id)
        assert source is not None
        source_session_key = str(source["session_key"])
        registry.adopt_session(
            task_id=TASK_ID, session_key=source_session_key, observed_at=4
        )
        registry.connection.execute(
            "UPDATE sessions SET wrapped_at = 4 WHERE session_key = ?",
            (source_session_key,),
        )
    registry.reserve_launch(
        {
            "host_id": HOST_ID,
            "provider": "codex",
            "action": "new",
            "project_id": PROJECT_ID,
            "task_id": TASK_ID,
            "checkout_id": LOCATION_ID,
            "cwd": "/work/project",
            "source_handoff_id": source_handoff_id,
            "target_session_key": None,
            "transport": "tmux",
        },
        request_id=REQUEST_ID,
        launch_id=LAUNCH_ID,
        lease_owner="bootstrap",
        capability_hash="b" * 64,
        expires_at=250,
        created_at=100,
    )
    registry.activate_launch_surface(
        LAUNCH_ID,
        {
            "surface_id": SURFACE_ID,
            "host_id": HOST_ID,
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": locator.to_storage(),
            "role": "session",
            "created_at": 110,
            "last_observed_at": 110,
        },
        lease_owner="bootstrap",
        observed_at=110,
    )
    registry.transition_launch(
        LAUNCH_ID,
        "provider_started",
        lease_owner="bootstrap",
        observed_at=120,
    )


def test_process_scan_is_bounded_to_interactive_codex_and_exact_rollout_ids(
    tmp_path: Path,
) -> None:
    root = _proc_root(tmp_path)
    _proc_entry(
        root,
        100,
        ppid=50,
        argv=("/usr/bin/codex",),
        start=1_000,
        session_ids=(SESSION_ID,),
    )
    _proc_entry(
        root,
        101,
        ppid=50,
        argv=("/usr/bin/codex", "app-server"),
        start=1_001,
        session_ids=(SECOND_SESSION_ID,),
    )
    _proc_entry(
        root,
        102,
        ppid=50,
        argv=("/usr/bin/codex", "exec", "task"),
        start=1_002,
        session_ids=(SECOND_SESSION_ID,),
    )

    scan = scan_codex_processes(proc_root=root, uid=os.getuid())

    assert scan.complete is True
    assert [process.pid for process in scan.processes] == [100]
    assert scan.processes[0].provider_session_ids == frozenset((SESSION_ID,))
    assert len(scan.processes[0].birth_id) == 64


@pytest.mark.parametrize(
    ("argv", "expected"),
    (
        (("/usr/bin/codex",), True),
        (("codex", "--strict-config", "resume"), True),
        (("codex", "--local-provider", "ollama"), True),
        (("codex", "--local-provider=lmstudio", "fork", "--last"), True),
        (("codex", "--remote", "ws://localhost:9000"), True),
        (("codex", "--dangerously-bypass-hook-trust"), True),
        (("codex", "--", "prompt text"), True),
        (("codex", "--profile", "work", "exec", "task"), False),
        (("codex", "-c", "key=value", "app-server"), False),
        (("codex", "--help"), False),
        (("codex", "write tests"), True),
        (("codex", "--strict-config", "write tests"), True),
        (("/usr/bin/CODEX",), False),
        (("/usr/bin/codex-code-mode-host",), False),
        (("/bin/bash", "-c", "codex"), False),
        (("/usr/bin/node", "/opt/codex/codex.js"), False),
    ),
)
def test_interactive_codex_parses_exact_executable_and_global_options(
    argv: tuple[str, ...], expected: bool
) -> None:
    import agent_switchboard.live as live_module

    assert live_module._interactive_codex(argv) is expected


@pytest.mark.parametrize(
    "subcommand",
    (
        "exec",
        "e",
        "review",
        "login",
        "logout",
        "mcp",
        "plugin",
        "mcp-server",
        "app-server",
        "remote-control",
        "completion",
        "update",
        "doctor",
        "sandbox",
        "debug",
        "apply",
        "a",
        "archive",
        "delete",
        "unarchive",
        "cloud",
        "exec-server",
        "features",
        "help",
    ),
)
def test_interactive_codex_rejects_every_noninteractive_subcommand(
    subcommand: str,
) -> None:
    import agent_switchboard.live as live_module

    assert live_module._interactive_codex(("codex", subcommand)) is False


def test_fd_fallback_accepts_uuidv7_only_in_canonical_rollout_targets(
    tmp_path: Path,
) -> None:
    root = _proc_root(tmp_path)
    _proc_entry(
        root,
        100,
        ppid=1,
        argv=("/usr/bin/codex",),
        start=100,
        session_ids=(V7_SESSION_ID,),
    )
    _proc_entry(root, 101, ppid=1, argv=("/usr/bin/codex",), start=101)
    os.symlink(
        f"/home/test/.codex/sessions/2026/07/16/{SESSION_ID}.jsonl",
        root / "101/fd/9",
    )
    _proc_entry(root, 102, ppid=1, argv=("/usr/bin/codex",), start=102)
    os.symlink(
        f"/home/test/.codex/sessions/x/rollout-2026-07-16T12-00-00-{SESSION_ID}.jsonl",
        root / "102/fd/9",
    )
    _proc_entry(root, 103, ppid=1, argv=("/usr/bin/codex",), start=103)
    os.symlink(
        f"/home/test/.codex/sessions/2026/07/16/rollout-loose-{SESSION_ID}.jsonl",
        root / "103/fd/9",
    )
    _proc_entry(root, 104, ppid=1, argv=("/usr/bin/codex",), start=104)
    os.symlink(
        "/home/test/.codex/sessions/2026/07/16/"
        f"rollout-2026-07-16T12-00-00-{SESSION_ID}.jsonl (deleted)",
        root / "104/fd/9",
    )

    scan = scan_codex_processes(proc_root=root, uid=os.getuid())
    by_pid = {process.pid: process for process in scan.processes}

    assert by_pid[100].provider_session_ids == frozenset((V7_SESSION_ID,))
    assert by_pid[101].provider_session_ids == frozenset()
    assert by_pid[102].provider_session_ids == frozenset()
    assert by_pid[103].provider_session_ids == frozenset()
    assert by_pid[104].provider_session_ids == frozenset()


def test_tmux_scan_uses_argv_and_rejects_malformed_output() -> None:
    output = b"/tmp/fake\t%7\t50\twork\t@1\t2\t1\n"
    scan = scan_tmux_panes((None,), runner=_tmux_runner(output))
    assert scan.complete is True
    assert scan.panes[0].pane_id == "%7"
    assert scan.panes[0].attached is True

    malformed = scan_tmux_panes((None,), runner=_tmux_runner(b"secret\n"))
    assert malformed.complete is False
    assert [issue.code for issue in malformed.issues] == ["tmux_probe_malformed"]

    absent = scan_tmux_panes(
        (None,),
        runner=_tmux_runner(
            returncode=1,
            stderr=b"error connecting to /tmp/tmux/default (No such file or directory)",
        ),
    )
    assert absent.complete is True
    assert absent.issues == ()

    denied = scan_tmux_panes(
        (None,),
        runner=_tmux_runner(
            returncode=1,
            stderr=b"failed to connect to /tmp/tmux/default (Permission denied)",
        ),
    )
    assert denied.complete is False
    assert [issue.code for issue in denied.issues] == ["tmux_probe_failed"]


def test_tmux_scan_caps_sockets_without_invoking_the_runner() -> None:
    def unexpected(_argv, _timeout):
        pytest.fail("an oversized socket set must not invoke tmux")

    sockets = tuple(f"/tmp/tmux-{index}" for index in range(MAX_TMUX_SOCKETS + 1))
    scan = scan_tmux_panes(sockets, runner=unexpected)
    assert scan.complete is False
    assert [issue.code for issue in scan.issues] == ["tmux_socket_limit_exceeded"]


def test_default_tmux_runner_kills_output_at_the_byte_bound() -> None:
    import agent_switchboard.live as live_module

    command = (
        sys.executable,
        "-c",
        "import os; os.write(1, b'x' * (1024 * 1024 + 1))",
    )
    with pytest.raises(RuntimeError):
        live_module._default_tmux_runner(command, 2.0)


def test_atomic_runtime_application_rolls_back_on_unknown_session(
    registry: Registry,
) -> None:
    valid = _observation(
        key="valid",
        entry_ns=1_000_000,
        presence=RuntimePresence.LIVE,
    )
    unknown = _observation(
        key="unknown",
        session_key=f"{HOST_ID}:codex:{SECOND_SESSION_ID}",
        entry_ns=1_000_001,
        presence=RuntimePresence.LIVE,
    )

    with pytest.raises(StorageError, match="unknown session"):
        registry.apply_runtime_observations((valid, unknown))

    assert (
        registry.connection.execute(
            "SELECT COUNT(*) FROM runtime_observations"
        ).fetchone()[0]
        == 0
    )


def test_reconcile_confirms_birth_maps_exact_pane_then_parks_dead_runtime(
    registry: Registry, tmp_path: Path
) -> None:
    root = _proc_root(tmp_path)
    _proc_entry(root, 50, ppid=1, argv=("/bin/sh",), start=500)
    _proc_entry(
        root,
        100,
        ppid=50,
        argv=("/usr/bin/codex",),
        start=1_000,
        session_ids=(SESSION_ID,),
    )
    process = scan_codex_processes(proc_root=root, uid=os.getuid()).processes[0]
    registry.apply_runtime_observations(
        (
            _observation(
                key="seed-live",
                entry_ns=1_000_000_000,
                presence=RuntimePresence.LIVE,
                pid=100,
                birth=process.birth_id,
                activity=Activity.READY,
                reason=ActivityReason.TURN_COMPLETE,
                attachment=Attachment.DETACHED,
                tmux_observed=True,
            ),
        )
    )
    pane = b"/tmp/fake\t%7\t50\twork\t@1\t2\t1\n"

    live = reconcile_live(
        registry,
        HOST_ID,
        proc_root=root,
        uid=os.getuid(),
        environment={},
        tmux_runner=_tmux_runner(pane),
        entry_ns=2_000_000_000,
    )

    assert live.errors == ()
    retained = registry.get_session(SESSION_KEY)
    assert retained is not None
    assert retained["runtime_presence"] == "live"
    assert retained["runtime_pid"] == 100
    assert retained["runtime_process_birth_id"] == process.birth_id
    assert retained["tmux_socket"] == "/tmp/fake"
    assert retained["tmux_session"] == "work"
    assert retained["tmux_window"] == "@1"
    assert retained["tmux_pane"] == "%7"
    assert retained["attachment"] == "attached"
    assert retained["activity"] == "ready"

    shutil.rmtree(root / "100")
    stopped = reconcile_live(
        registry,
        HOST_ID,
        proc_root=root,
        uid=os.getuid(),
        environment={},
        tmux_runner=_tmux_runner(pane),
        entry_ns=3_000_000_000,
    )

    assert stopped.application.applied_count == 1
    parked = registry.get_session(SESSION_KEY)
    assert parked is not None
    assert parked["runtime_presence"] == "stopped"
    assert parked["resumability"] == "resumable"
    assert parked["activity"] == "unknown"
    assert parked["activity_reason"] == "unknown"
    assert parked["attachment"] == "none"
    assert parked["runtime_pid"] is None
    assert parked["runtime_process_birth_id"] is None
    assert parked["tmux_socket"] is None
    assert parked["tmux_pane"] is None


def test_reconcile_binds_pending_resume_after_missed_hook(
    registry: Registry, tmp_path: Path
) -> None:
    locator = TmuxLocator("/tmp/fake", "work", "@1", "%7")
    _prepare_pending_resume(registry, locator)
    root = _proc_root(tmp_path)
    _proc_entry(root, 50, ppid=1, argv=("/bin/sh",), start=500)
    _proc_entry(
        root,
        100,
        ppid=50,
        argv=("/usr/bin/codex", "resume", SESSION_ID),
        start=1_000,
        session_ids=(SESSION_ID,),
    )

    result = reconcile_live(
        registry,
        HOST_ID,
        proc_root=root,
        uid=os.getuid(),
        environment={},
        tmux_runner=_tmux_runner(b"/tmp/fake\t%7\t50\twork\t@1\t2\t1\n"),
        entry_ns=200_000_000,
    )

    assert result.errors == ()
    launch = registry.get_launch(LAUNCH_ID)
    assert launch is not None
    assert launch["state"] == "bound"
    assert launch["lease_owner"] is None
    session = registry.get_session(SESSION_KEY)
    assert session is not None
    assert session["surface_id"] == SURFACE_ID
    assert session["runtime_presence"] == "live"
    assert session["attachment"] == "attached"
    surface = registry.get_surface(SURFACE_ID)
    assert surface is not None
    assert surface["current_session_key"] == SESSION_KEY
    assert surface["binding_confidence"] == "confirmed"
    assert surface["client_attached"] == 1
    observation = registry.connection.execute(
        "SELECT * FROM runtime_observations WHERE launch_id = ?", (LAUNCH_ID,)
    ).fetchone()
    assert observation is not None
    assert observation["session_key"] == SESSION_KEY


def test_reconcile_binds_pending_new_launch_after_missed_hook(
    registry: Registry, tmp_path: Path
) -> None:
    locator = TmuxLocator("/tmp/fake", "work", "@1", "%7")
    source_session_key = f"{HOST_ID}:codex:{SECOND_SESSION_ID}"
    registry.materialize_projects(
        HOST_ID,
        [
            {
                "project_id": PROJECT_ID,
                "name": "project",
                "default_provider": "codex",
                "default_transport": "tmux",
                "checkouts": [
                    {
                        "checkout_id": LOCATION_ID,
                        "path": "/work/project",
                        "is_default": True,
                    }
                ],
            }
        ],
        observed_at=2,
    )
    registry.upsert_session(
        {
            "session_key": source_session_key,
            "host_id": HOST_ID,
            "provider": "codex",
            "provider_session_id": SECOND_SESSION_ID,
            "project_id": PROJECT_ID,
            "checkout_id": LOCATION_ID,
            "cwd": "/work/project",
            "runtime_presence": "unknown",
            "resumability": "resumable",
            "activity": "ready",
            "activity_reason": "turn_complete",
            "attachment": "detached",
            "metadata_source": "provider",
            "first_observed_at": 1,
            "last_observed_at": 2,
        }
    )
    registry.append_handoff(
        session_key=source_session_key,
        handoff_id=HANDOFF_ID,
        summary="Continue the vertical slice.",
        next_action="Repair the missed runtime binding.",
        source="user",
        source_host_id=HOST_ID,
        created_at=3,
    )
    _prepare_pending_new(registry, locator, source_handoff_id=HANDOFF_ID)
    root = _proc_root(tmp_path)
    _proc_entry(root, 50, ppid=1, argv=("/bin/sh",), start=500)
    _proc_entry(
        root,
        100,
        ppid=50,
        argv=("/usr/bin/codex",),
        start=1_000,
        session_ids=(SESSION_ID,),
    )

    result = reconcile_live(
        registry,
        HOST_ID,
        proc_root=root,
        uid=os.getuid(),
        environment={},
        tmux_runner=_tmux_runner(b"/tmp/fake\t%7\t50\twork\t@1\t2\t1\n"),
        entry_ns=200_000_000,
    )

    assert result.errors == ()
    launch = registry.get_launch(LAUNCH_ID)
    assert launch is not None
    assert launch["state"] == "bound"
    assert launch["target_session_key"] == SESSION_KEY
    session = registry.get_session(SESSION_KEY)
    assert session is not None
    assert session["project_id"] == PROJECT_ID
    assert session["checkout_id"] == LOCATION_ID
    assert session["cwd"] == "/work/project"
    assert session["metadata_source"] == "launch"
    assert session["continued_from_handoff_id"] == HANDOFF_ID
    assert session["surface_id"] == SURFACE_ID
    surface = registry.get_surface(SURFACE_ID)
    assert surface is not None
    assert surface["current_session_key"] == SESSION_KEY
    assert surface["binding_confidence"] == "confirmed"


def test_complete_tmux_absence_retires_cancelled_pending_launch(
    registry: Registry, tmp_path: Path
) -> None:
    socket = tmp_path / "cancelled.sock"
    socket.touch()
    locator = TmuxLocator(str(socket), "picker", "@1", "%7")
    _prepare_pending_new(registry, locator)

    result = reconcile_live(
        registry,
        HOST_ID,
        proc_root=_proc_root(tmp_path),
        uid=os.getuid(),
        environment={},
        tmux_runner=_tmux_runner(returncode=1, stderr=b"no server running"),
        entry_ns=130_000_000,
    )

    assert result.errors == ()
    launch = registry.get_launch(LAUNCH_ID)
    assert launch is not None
    assert launch["state"] == "failed"
    assert launch["failure_code"] == "surface_terminated"
    surface = registry.get_surface(SURFACE_ID)
    assert surface is not None and surface["retired_at"] == 130


def test_incomplete_tmux_scan_preserves_pending_launch_surface(
    registry: Registry, tmp_path: Path
) -> None:
    socket = tmp_path / "inaccessible.sock"
    socket.touch()
    _prepare_pending_new(
        registry,
        TmuxLocator(str(socket), "picker", "@1", "%7"),
    )

    result = reconcile_live(
        registry,
        HOST_ID,
        proc_root=_proc_root(tmp_path),
        uid=os.getuid(),
        environment={},
        tmux_runner=_tmux_runner(returncode=2, stderr=b"permission denied"),
        entry_ns=130_000_000,
    )

    assert {error.code for error in result.errors} == {"tmux_probe_failed"}
    launch = registry.get_launch(LAUNCH_ID)
    assert launch is not None and launch["state"] == "provider_started"
    surface = registry.get_surface(SURFACE_ID)
    assert surface is not None and surface["retired_at"] is None


def test_runtime_launch_binding_rejects_a_different_tmux_locator(
    registry: Registry,
) -> None:
    _prepare_pending_resume(registry, TmuxLocator("/tmp/fake", "work", "@1", "%7"))
    observation = _observation(
        key="wrong-launch-pane",
        entry_ns=200_000_000,
        presence=RuntimePresence.LIVE,
        pid=100,
        birth="b" * 64,
        attachment=Attachment.ATTACHED,
        tmux_observed=True,
        pane="%8",
        source="liveness",
        launch_id=LAUNCH_ID,
    )

    with pytest.raises(IdentityConflict, match="launch surface locator"):
        registry.apply_runtime_observations((observation,))

    assert registry.get_launch(LAUNCH_ID)["state"] == "provider_started"  # type: ignore[index]
    assert registry.get_surface(SURFACE_ID)["current_session_key"] is None  # type: ignore[index]
    assert (
        registry.connection.execute(
            "SELECT COUNT(*) FROM runtime_observations"
        ).fetchone()[0]
        == 0
    )


def test_newer_hook_resurrects_after_higher_priority_liveness_stop(
    registry: Registry,
) -> None:
    registry.apply_runtime_observations(
        (
            _observation(
                key="liveness-stopped",
                entry_ns=2_000_000_000,
                presence=RuntimePresence.STOPPED,
                activity=Activity.UNKNOWN,
                reason=ActivityReason.UNKNOWN,
                attachment=Attachment.NONE,
                tmux_observed=True,
                source="liveness",
            ),
        )
    )
    stopped = registry.get_session(SESSION_KEY)
    assert stopped is not None
    assert stopped["runtime_source_priority"] == LIVE_SOURCE_PRIORITY
    assert stopped["runtime_presence"] == "stopped"

    tied = normalize_codex_event(
        {
            "session_id": SESSION_ID,
            "cwd": "/work/project",
            "hook_event_name": "SessionStart",
            "source": "resume",
        },
        {},
        entry_ns=2_000_000_000,
        process_birth_id="a" * 64,
    )
    tied_result = registry.ingest_hook_event(
        tied.storage_mapping(HostId(HOST_ID)), host_display_name="local"
    )
    assert tied_result.kind == "stale"
    assert tied_result.session["runtime_presence"] == "stopped"

    resumed = normalize_codex_event(
        {
            "session_id": SESSION_ID,
            "cwd": "/work/project",
            "hook_event_name": "SessionStart",
            "source": "resume",
        },
        {},
        entry_ns=3_000_000_000,
        process_birth_id="a" * 64,
    )
    result = registry.ingest_hook_event(
        resumed.storage_mapping(HostId(HOST_ID)), host_display_name="local"
    )

    assert result.kind == "applied"
    assert result.session["runtime_presence"] == "live"
    assert result.session["activity"] == "ready"
    assert result.session["runtime_source_priority"] < LIVE_SOURCE_PRIORITY
    assert result.session["runtime_order_ns"] == 3_000_000_000


def test_pid_reuse_does_not_confirm_the_stored_runtime(
    registry: Registry, tmp_path: Path
) -> None:
    root = _proc_root(tmp_path)
    _proc_entry(root, 100, ppid=1, argv=("/usr/bin/codex",), start=1_000)
    original = scan_codex_processes(proc_root=root, uid=os.getuid()).processes[0]
    registry.apply_runtime_observations(
        (
            _observation(
                key="original-process",
                entry_ns=1_000_000_000,
                presence=RuntimePresence.LIVE,
                pid=100,
                birth=original.birth_id,
                source="liveness",
            ),
        )
    )
    shutil.rmtree(root / "100")
    _proc_entry(root, 100, ppid=1, argv=("/usr/bin/codex",), start=2_000)

    reconcile_live(
        registry,
        HOST_ID,
        proc_root=root,
        uid=os.getuid(),
        environment={},
        tmux_runner=_tmux_runner(returncode=1, stderr=b"no server running"),
        entry_ns=2_000_000_000,
    )

    retained = registry.get_session(SESSION_KEY)
    assert retained is not None
    assert retained["runtime_presence"] == "stopped"
    assert retained["runtime_pid"] is None
    assert retained["runtime_process_birth_id"] is None


def test_exact_fd_fallback_correlates_one_unbound_session(
    registry: Registry, tmp_path: Path
) -> None:
    root = _proc_root(tmp_path)
    _proc_entry(
        root,
        100,
        ppid=1,
        argv=("/usr/bin/codex",),
        start=1_000,
        session_ids=(SESSION_ID,),
    )

    result = reconcile_live(
        registry,
        HOST_ID,
        proc_root=root,
        uid=os.getuid(),
        environment={},
        tmux_runner=_tmux_runner(returncode=1, stderr=b"no server running"),
        entry_ns=2_000_000_000,
    )

    assert result.errors == ()
    retained = registry.get_session(SESSION_KEY)
    assert retained is not None
    assert retained["runtime_presence"] == "live"
    assert retained["runtime_pid"] == 100


def test_repeated_live_evidence_is_deduplicated_and_bounded(
    registry: Registry, tmp_path: Path
) -> None:
    root = _proc_root(tmp_path)
    _proc_entry(
        root,
        100,
        ppid=1,
        argv=("/usr/bin/codex",),
        start=1_000,
        session_ids=(SESSION_ID,),
    )
    runner = _tmux_runner(returncode=1, stderr=b"no server running")
    for entry_ns in (2_000_000_000, 3_000_000_000):
        reconcile_live(
            registry,
            HOST_ID,
            proc_root=root,
            uid=os.getuid(),
            environment={},
            tmux_runner=runner,
            entry_ns=entry_ns,
        )
    assert (
        registry.connection.execute(
            "SELECT COUNT(*) FROM runtime_observations WHERE source = 'liveness'"
        ).fetchone()[0]
        == 1
    )

    for index in range(20):
        registry.apply_runtime_observations(
            (
                _observation(
                    key=f"changing-{index}",
                    entry_ns=4_000_000_000 + index,
                    presence=RuntimePresence.LIVE,
                    source="liveness",
                ),
            )
        )
    assert (
        registry.connection.execute(
            "SELECT COUNT(*) FROM runtime_observations WHERE source = 'liveness'"
        ).fetchone()[0]
        == 16
    )


def _bound_surface(registry: Registry, surface_id: str) -> None:
    registry.upsert_surface(
        {
            "surface_id": surface_id,
            "host_id": HOST_ID,
            "provider": "codex",
            "transport": "tmux",
            "transport_locator": f"tmux:{surface_id}",
            "role": "session",
            "created_at": 5,
            "last_observed_at": 5,
        }
    )
    registry.bind_surface(
        surface_id, SESSION_KEY, confidence="confirmed", observed_at=6
    )


def test_inaccessible_tmux_socket_preserves_attachment_locator_and_surface(
    registry: Registry, tmp_path: Path
) -> None:
    def inaccessible(_path: str) -> os.stat_result:
        raise PermissionError

    surface_id = "66666666-6666-4666-8666-666666666666"
    _bound_surface(registry, surface_id)
    root = _proc_root(tmp_path)
    _proc_entry(
        root,
        100,
        ppid=1,
        argv=("/usr/bin/codex",),
        start=1_000,
        session_ids=(SESSION_ID,),
    )
    process = scan_codex_processes(proc_root=root, uid=os.getuid()).processes[0]
    registry.apply_runtime_observations(
        (
            _observation(
                key="permission-retained-pane",
                entry_ns=10_000_000,
                presence=RuntimePresence.LIVE,
                pid=100,
                birth=process.birth_id,
                attachment=Attachment.DETACHED,
                tmux_observed=True,
                pane="%9",
                source="liveness",
            ),
        )
    )

    result = reconcile_live(
        registry,
        HOST_ID,
        proc_root=root,
        uid=os.getuid(),
        environment={},
        tmux_runner=_tmux_runner(returncode=1, stderr=b"no server running"),
        tmux_socket_lstat=inaccessible,
        entry_ns=20_000_000,
    )

    assert [error.code for error in result.errors] == ["tmux_probe_failed"]
    retained = registry.get_session(SESSION_KEY)
    assert retained is not None
    assert retained["attachment"] == "detached"
    assert retained["tmux_socket"] == "/tmp/fake-tmux"
    assert retained["tmux_pane"] == "%9"
    assert retained["surface_id"] == surface_id
    surface = registry.connection.execute(
        "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
    ).fetchone()
    assert surface["current_session_key"] == SESSION_KEY
    assert surface["binding_confidence"] == "confirmed"


def test_authoritative_stop_unbinds_tmux_surface(registry: Registry) -> None:
    surface_id = "44444444-4444-4444-8444-444444444444"
    _bound_surface(registry, surface_id)

    registry.apply_runtime_observations(
        (
            _observation(
                key="tmux-stopped",
                entry_ns=20_000_000,
                presence=RuntimePresence.STOPPED,
                activity=Activity.UNKNOWN,
                reason=ActivityReason.UNKNOWN,
                source="liveness",
            ),
        )
    )

    assert registry.get_session(SESSION_KEY)["surface_id"] is None  # type: ignore[index]
    surface = registry.connection.execute(
        "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
    ).fetchone()
    assert surface["current_session_key"] is None
    assert surface["binding_confidence"] == "unknown"


def test_authoritative_stop_preserves_non_tmux_surface(registry: Registry) -> None:
    surface_id = "55555555-5555-4555-8555-555555555555"
    _bound_surface(registry, surface_id)
    registry.connection.execute("PRAGMA ignore_check_constraints = ON")
    registry.connection.execute(
        "UPDATE surfaces SET transport = 'direct' WHERE surface_id = ?", (surface_id,)
    )

    registry.apply_runtime_observations(
        (
            _observation(
                key="direct-stopped",
                entry_ns=20_000_000,
                presence=RuntimePresence.STOPPED,
                activity=Activity.UNKNOWN,
                reason=ActivityReason.UNKNOWN,
                source="liveness",
            ),
        )
    )

    assert registry.get_session(SESSION_KEY)["surface_id"] == surface_id  # type: ignore[index]
    surface = registry.connection.execute(
        "SELECT * FROM surfaces WHERE surface_id = ?", (surface_id,)
    ).fetchone()
    assert surface["current_session_key"] == SESSION_KEY
    assert surface["binding_confidence"] == "confirmed"


def test_ambiguous_rollout_and_probe_failure_preserve_retained_state(
    registry: Registry, tmp_path: Path
) -> None:
    root = _proc_root(tmp_path)
    _proc_entry(
        root,
        100,
        ppid=1,
        argv=("/usr/bin/codex",),
        start=1_000,
        session_ids=(SESSION_ID, SECOND_SESSION_ID),
    )
    before = registry.get_session(SESSION_KEY)

    result = reconcile_live(
        registry,
        HOST_ID,
        proc_root=root,
        uid=os.getuid(),
        environment={},
        tmux_runner=_tmux_runner(returncode=2, stderr=b"permission denied"),
        entry_ns=2_000_000_000,
    )

    assert result.application.applied_count == 0
    assert {error.code for error in result.errors} == {
        "runtime_correlation_ambiguous",
        "tmux_probe_failed",
    }
    assert registry.get_session(SESSION_KEY) == before


def test_oversized_tmux_probe_preserves_retained_attachment_and_locator(
    registry: Registry, tmp_path: Path
) -> None:
    root = _proc_root(tmp_path)
    _proc_entry(
        root,
        100,
        ppid=1,
        argv=("/usr/bin/codex",),
        start=1_000,
        session_ids=(SESSION_ID,),
    )
    process = scan_codex_processes(proc_root=root, uid=os.getuid()).processes[0]
    registry.apply_runtime_observations(
        (
            _observation(
                key="retained-pane",
                entry_ns=1_000_000_000,
                presence=RuntimePresence.LIVE,
                pid=100,
                birth=process.birth_id,
                attachment=Attachment.DETACHED,
                tmux_observed=True,
                pane="%9",
                source="liveness",
            ),
        )
    )

    result = reconcile_live(
        registry,
        HOST_ID,
        proc_root=root,
        uid=os.getuid(),
        environment={},
        tmux_runner=_tmux_runner(b"x" * (MAX_TMUX_OUTPUT_BYTES + 1)),
        entry_ns=2_000_000_000,
    )

    assert [error.code for error in result.errors] == ["tmux_probe_oversized"]
    retained = registry.get_session(SESSION_KEY)
    assert retained is not None
    assert retained["runtime_presence"] == "live"
    assert retained["attachment"] == "detached"
    assert retained["tmux_socket"] == "/tmp/fake-tmux"
    assert retained["tmux_pane"] == "%9"
