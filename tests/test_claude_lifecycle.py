from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import agent_switchboard.hooks as hooks_module
from agent_switchboard.domain import HostId
from agent_switchboard.hooks import HookInputError, normalize_claude_event
from agent_switchboard.live import reconcile_live, scan_process_identities
from agent_switchboard.snapshot import build_host_snapshot_json
from agent_switchboard.storage import Registry

HOST_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
SESSION_KEY = f"{HOST_ID}:claude:{SESSION_ID}"
PROMPT_ID = "33333333-3333-4333-8333-333333333333"
SECOND_ID = "44444444-4444-4444-8444-444444444444"
PROJECT_ID = "55555555-5555-4555-8555-555555555555"
LOCATION_ID = "66666666-6666-4666-8666-666666666666"
LAUNCH_ID = "77777777-7777-4777-8777-777777777777"
SURFACE_ID = "88888888-8888-4888-8888-888888888888"
REQUEST_ID = "99999999-9999-4999-8999-999999999999"
TASK_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def payload(event: str, **extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "session_id": SESSION_ID,
        "cwd": "/work/switchboard",
        "hook_event_name": event,
        "prompt": "SECRET prompt",
        "last_assistant_message": "SECRET assistant",
        "tool_input": {"command": "SECRET command"},
        "tool_response": "SECRET response",
        "transcript_path": "/private/SECRET.jsonl",
        "task_description": "SECRET task",
    }
    value.update(extra)
    return value


def normalized(
    event: str,
    *,
    entry_ns: int,
    pid: int = 100,
    birth: str = "a" * 64,
    environment: dict[str, str] | None = None,
    **extra: object,
):
    return normalize_claude_event(
        payload(event, **extra),
        environment or {},
        entry_ns=entry_ns,
        process_pid=pid,
        process_birth_id=birth,
    )


def _proc_root(tmp_path: Path) -> Path:
    root = tmp_path / "proc"
    boot = root / "sys" / "kernel" / "random"
    boot.mkdir(parents=True)
    (boot / "boot_id").write_text(
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa\n", encoding="ascii"
    )
    return root


def _proc_entry(root: Path, pid: int, *, ppid: int, start: int) -> None:
    entry = root / str(pid)
    entry.mkdir()
    fields = ["S", str(ppid), *("0" for _ in range(17)), str(start)]
    (entry / "stat").write_text(
        f"{pid} (claude) {' '.join(fields)}\n", encoding="ascii"
    )
    (entry / "cmdline").write_bytes(b"/private/not-read\0SECRET\0")


def _tmux_runner(socket: str, *, attached: int = 0):
    output = f"{socket}\t%7\t50\tmanual\t@1\t0\t{attached}\n".encode()

    def run(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, output, b"")

    return run


def test_claude_normalization_uses_prompt_identity_and_drops_private_fields() -> None:
    first = normalized(
        "PermissionRequest",
        entry_ns=1_000_000_000,
        prompt_id=PROMPT_ID,
        tool_name="Bash",
    )
    changed = normalize_claude_event(
        payload(
            "PermissionRequest",
            prompt_id=PROMPT_ID,
            tool_name="Bash",
            prompt="different SECRET prompt",
            tool_input={"command": "different SECRET command"},
        ),
        {},
        entry_ns=2_000_000_000,
        process_pid=100,
        process_birth_id="a" * 64,
    )

    assert first.idempotency_key == changed.idempotency_key
    assert first.provider_turn_id == PROMPT_ID
    retained = json.dumps(changed.storage_mapping(HostId(HOST_ID)))
    assert "SECRET" not in retained
    assert "prompt" not in retained
    assert "transcript" not in retained
    assert changed.process_pid == 100

    with pytest.raises(HookInputError, match="UUID"):
        normalized(
            "UserPromptSubmit",
            entry_ns=3_000_000_000,
            prompt_id="not-a-canonical-prompt-id",
        )


def test_new_claude_session_start_binds_launch_project_and_surface(
    tmp_path: Path,
) -> None:
    environment = {
        "AGENT_SWITCHBOARD_LAUNCH_ID": LAUNCH_ID,
        "AGENT_SWITCHBOARD_SURFACE_ID": SURFACE_ID,
    }
    event = normalized(
        "SessionStart",
        entry_ns=200_000_000,
        environment=environment,
        source="startup",
    )
    with Registry(tmp_path / "registry.db") as registry:
        registry.upsert_host(HOST_ID, "starship", is_local=True, observed_at=10)
        registry.materialize_projects(
            HOST_ID,
            [
                {
                    "project_id": PROJECT_ID,
                    "name": "Switchboard",
                    "default_provider": "codex",
                    "default_transport": "tmux",
                    "checkouts": [
                        {
                            "checkout_id": LOCATION_ID,
                            "path": "/work/switchboard",
                            "is_default": True,
                        }
                    ],
                }
            ],
            observed_at=20,
        )
        registry.create_task(
            task_id=TASK_ID,
            host_id=HOST_ID,
            project_id=PROJECT_ID,
            checkout_id=LOCATION_ID,
            title="Claude lifecycle",
            observed_at=21,
        )
        registry.reserve_launch(
            {
                "host_id": HOST_ID,
                "provider": "claude",
                "action": "new",
                "project_id": PROJECT_ID,
                "task_id": TASK_ID,
                "checkout_id": LOCATION_ID,
                "cwd": "/work/switchboard",
                "source_handoff_id": None,
                "target_session_key": None,
                "transport": "tmux",
            },
            request_id=REQUEST_ID,
            launch_id=LAUNCH_ID,
            lease_owner=f"bootstrap:{LAUNCH_ID}",
            capability_hash="b" * 64,
            expires_at=10_000,
            created_at=100,
        )
        registry.activate_launch_surface(
            LAUNCH_ID,
            {
                "surface_id": SURFACE_ID,
                "host_id": HOST_ID,
                "provider": "claude",
                "transport": "tmux",
                "transport_locator": "tmux:test:0.0",
                "workspace_id": "test",
                "role": "session",
                "launch_id": LAUNCH_ID,
                "created_at": 110,
                "client_attached": True,
            },
            lease_owner=f"bootstrap:{LAUNCH_ID}",
            observed_at=110,
        )
        registry.transition_launch(
            LAUNCH_ID,
            "provider_started",
            lease_owner=f"bootstrap:{LAUNCH_ID}",
            observed_at=120,
        )

        result = registry.ingest_hook_event(
            event.storage_mapping(HostId(HOST_ID)), host_display_name="starship"
        )

        assert result.kind == "applied"
        assert result.launch is not None and result.launch["state"] == "bound"
        assert result.launch["target_session_key"] == SESSION_KEY
        assert result.session["project_id"] == PROJECT_ID
        assert result.session["checkout_id"] == LOCATION_ID
        assert result.session["surface_id"] == SURFACE_ID
        assert result.surface is not None
        assert result.surface["provider"] == "claude"
        assert result.surface["current_session_key"] == SESSION_KEY


def test_claude_process_identity_requires_exact_comm_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa\n"
    fields = ["S", "1", *("0" for _ in range(17)), "1000"]

    def proc_text(path: Path) -> str:
        if path.name == "boot_id":
            return boot
        return f"100 (notclaude) {' '.join(fields)}\n"

    monkeypatch.setattr(hooks_module, "_bounded_proc_ascii", proc_text)
    monkeypatch.setattr(hooks_module.os, "getppid", lambda: 100)

    assert hooks_module._linux_process_identity("claude") is None
    assert hooks_module._linux_process_identity("codex") is not None


def test_session_end_stops_runtime_without_overwriting_foreground_activity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.db"
    with Registry(database) as registry:
        for event in (
            normalized("SessionStart", entry_ns=1_000_000_000, source="startup"),
            normalized(
                "UserPromptSubmit",
                entry_ns=2_000_000_000,
                prompt_id=PROMPT_ID,
            ),
            normalized(
                "Stop",
                entry_ns=3_000_000_000,
                prompt_id=PROMPT_ID,
            ),
            normalized(
                "SessionEnd",
                entry_ns=4_000_000_000,
                prompt_id=PROMPT_ID,
                reason="prompt_input_exit",
            ),
        ):
            registry.ingest_hook_event(
                event.storage_mapping(HostId(HOST_ID)),
                host_display_name="starship",
            )
        session = registry.get_session(SESSION_KEY)
        assert session is not None
        assert session["runtime_presence"] == "stopped"
        assert session["activity"] == "ready"
        assert session["activity_reason"] == "turn_complete"
        assert session["runtime_pid"] == 100

    raw = database.read_bytes()
    assert b"SECRET" not in raw
    assert b"transcript" not in raw


def test_retained_exact_pid_birth_and_tmux_are_reconciled_without_argv_identity(
    tmp_path: Path,
) -> None:
    root = _proc_root(tmp_path)
    _proc_entry(root, 50, ppid=1, start=500)
    _proc_entry(root, 100, ppid=50, start=1000)
    birth = {
        process.pid: process
        for process in scan_process_identities(
            proc_root=root, uid=os.getuid()
        ).processes
    }[100].birth_id
    socket = str(tmp_path / "tmux.sock")
    Path(socket).touch()
    event = normalized(
        "SessionStart",
        entry_ns=1_000_000_000,
        pid=100,
        birth=birth,
        environment={"TMUX": f"{socket},1,0", "TMUX_PANE": "%7"},
        source="startup",
    )
    with Registry(tmp_path / "registry.db") as registry:
        registry.ingest_hook_event(
            event.storage_mapping(HostId(HOST_ID)), host_display_name="starship"
        )
        live = reconcile_live(
            registry,
            HOST_ID,
            proc_root=root,
            uid=os.getuid(),
            environment={},
            tmux_runner=_tmux_runner(socket),
            tmux_socket_lstat=os.lstat,
            entry_ns=2_000_000_000,
        )
        assert not [error for error in live.errors if error.provider.value == "claude"]
        session = registry.get_session(SESSION_KEY)
        assert session is not None
        assert session["runtime_presence"] == "live"
        assert session["resumability"] == "resumable"
        assert session["attachment"] == "detached"

        shutil.rmtree(root / "100")
        stopped = reconcile_live(
            registry,
            HOST_ID,
            proc_root=root,
            uid=os.getuid(),
            environment={},
            tmux_runner=_tmux_runner(socket),
            tmux_socket_lstat=os.lstat,
            entry_ns=3_000_000_000,
        )
        assert stopped.application.applied_count == 1
        session = registry.get_session(SESSION_KEY)
        assert session is not None
        assert session["runtime_presence"] == "stopped"
        assert session["resumability"] == "resumable"
        assert session["attachment"] == "none"

        snapshot = build_host_snapshot_json(registry, HOST_ID)
        assert "SECRET" not in snapshot
        assert "not-read" not in snapshot


def test_pid_reuse_does_not_resurrect_claude_session(tmp_path: Path) -> None:
    root = _proc_root(tmp_path)
    _proc_entry(root, 100, ppid=1, start=1000)
    old_birth = (
        scan_process_identities(proc_root=root, uid=os.getuid()).processes[0].birth_id
    )
    event = normalized(
        "SessionStart",
        entry_ns=1_000_000_000,
        pid=100,
        birth=old_birth,
        source="startup",
    )
    with Registry(tmp_path / "registry.db") as registry:
        registry.ingest_hook_event(
            event.storage_mapping(HostId(HOST_ID)), host_display_name="starship"
        )
        shutil.rmtree(root / "100")
        _proc_entry(root, 100, ppid=1, start=2000)

        reconcile_live(
            registry,
            HOST_ID,
            proc_root=root,
            uid=os.getuid(),
            environment={},
            tmux_runner=lambda argv, _timeout: subprocess.CompletedProcess(
                argv, 1, b"", b"no server running"
            ),
            entry_ns=2_000_000_000,
        )

        session = registry.get_session(SESSION_KEY)
        assert session is not None
        assert session["runtime_presence"] == "stopped"
        assert session["runtime_pid"] is None


def test_resume_switch_rebinds_one_process_to_only_the_newest_session(
    tmp_path: Path,
) -> None:
    root = _proc_root(tmp_path)
    _proc_entry(root, 100, ppid=1, start=1000)
    birth = (
        scan_process_identities(proc_root=root, uid=os.getuid()).processes[0].birth_id
    )
    first_start = normalized(
        "SessionStart",
        entry_ns=1_000_000_000,
        pid=100,
        birth=birth,
        source="startup",
    )
    first_end = normalized(
        "SessionEnd",
        entry_ns=2_000_000_000,
        pid=100,
        birth=birth,
        reason="resume",
    )
    second_payload = payload("SessionStart", session_id=SECOND_ID, source="resume")
    second_start = normalize_claude_event(
        second_payload,
        {},
        entry_ns=3_000_000_000,
        process_pid=100,
        process_birth_id=birth,
    )
    with Registry(tmp_path / "registry.db") as registry:
        for event in (first_start, first_end, second_start):
            registry.ingest_hook_event(
                event.storage_mapping(HostId(HOST_ID)),
                host_display_name="starship",
            )

        reconcile_live(
            registry,
            HOST_ID,
            proc_root=root,
            uid=os.getuid(),
            environment={},
            tmux_runner=lambda argv, _timeout: subprocess.CompletedProcess(
                argv, 1, b"", b"no server running"
            ),
            entry_ns=4_000_000_000,
        )

        first = registry.get_session(SESSION_KEY)
        second = registry.get_session(f"{HOST_ID}:claude:{SECOND_ID}")
        assert first is not None and second is not None
        assert first["runtime_presence"] == "stopped"
        assert first["resumability"] == "resumable"
        assert second["runtime_presence"] == "live"
        assert second["runtime_pid"] == 100
