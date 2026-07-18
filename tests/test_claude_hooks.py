from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent_switchboard.config import HooksConfig
from agent_switchboard.doctor import run_claude_doctor
from agent_switchboard.hook_config import (
    CLAUDE_HOOK_EVENTS,
    CLAUDE_HOOK_STATUS_MESSAGE,
    HookConfigError,
    edit_claude_hooks,
    is_claude_switchboard_handler,
)


def _environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    settings = claude / "settings.json"
    executable = tmp_path / "bin" / "swbctl"
    executable.parent.mkdir()
    executable.touch(mode=0o755)
    return {"HOME": str(home)}, settings, executable


def _handlers(document: dict[str, object]) -> list[dict[str, object]]:
    hooks = document["hooks"]
    assert isinstance(hooks, dict)
    return [
        handler
        for groups in hooks.values()
        for group in groups
        for handler in group["hooks"]
    ]


def test_install_preserves_unrelated_settings_mode_and_is_idempotent(
    tmp_path: Path,
) -> None:
    environment, settings, executable = _environment(tmp_path)
    unrelated = {
        "disableAgentView": True,
        "theme": "dark",
        "hooks": {
            "Notification": [
                {
                    "matcher": "permission_prompt",
                    "hooks": [{"type": "command", "command": "/bin/true"}],
                }
            ]
        },
    }
    settings.write_text(json.dumps(unrelated), encoding="utf-8")
    settings.chmod(0o644)

    first = edit_claude_hooks(
        "install",
        executable=executable,
        timeout_seconds=1,
        environ=environment,
    )
    second = edit_claude_hooks(
        "install",
        executable=executable,
        timeout_seconds=1,
        environ=environment,
    )

    assert first.changed
    assert not second.changed
    assert stat.S_IMODE(settings.stat().st_mode) == 0o644
    document = json.loads(settings.read_text(encoding="utf-8"))
    assert document["disableAgentView"] is True
    assert document["theme"] == "dark"
    owned = [
        handler
        for handler in _handlers(document)
        if is_claude_switchboard_handler(handler)
    ]
    assert len(owned) == len(CLAUDE_HOOK_EVENTS)
    assert all(handler["command"] == str(executable) for handler in owned)
    assert all(
        handler["args"] == ["event", "--provider", "claude"] for handler in owned
    )
    assert all(
        handler["statusMessage"] == CLAUDE_HOOK_STATUS_MESSAGE for handler in owned
    )


def test_uninstall_removes_only_exact_owned_handlers(tmp_path: Path) -> None:
    environment, settings, executable = _environment(tmp_path)
    edit_claude_hooks(
        "install",
        executable=executable,
        timeout_seconds=1,
        environ=environment,
    )
    document = json.loads(settings.read_text(encoding="utf-8"))
    document["hooks"]["Stop"][0]["hooks"].append(
        {
            "type": "command",
            "command": str(executable),
            "args": ["event", "--provider", "claude"],
            "statusMessage": "unrelated status",
        }
    )
    settings.write_text(json.dumps(document), encoding="utf-8")

    result = edit_claude_hooks(
        "uninstall",
        executable=executable,
        timeout_seconds=1,
        environ=environment,
    )

    assert result.removed_handlers == len(CLAUDE_HOOK_EVENTS)
    retained = json.loads(settings.read_text(encoding="utf-8"))
    assert len(_handlers(retained)) == 1


def test_dry_run_does_not_create_or_modify_settings(tmp_path: Path) -> None:
    environment, settings, executable = _environment(tmp_path)

    result = edit_claude_hooks(
        "install",
        executable=executable,
        timeout_seconds=1,
        dry_run=True,
        environ=environment,
    )

    assert result.changed
    assert result.dry_run
    assert not settings.exists()


def test_symlink_settings_target_is_rejected(tmp_path: Path) -> None:
    environment, settings, executable = _environment(tmp_path)
    target = tmp_path / "private.json"
    target.write_text("{}", encoding="utf-8")
    settings.symlink_to(target)

    with pytest.raises(HookConfigError, match="regular file"):
        edit_claude_hooks(
            "install",
            executable=executable,
            timeout_seconds=1,
            environ=environment,
        )


def test_claude_doctor_checks_profile_handlers_latency_and_legacy_runtime(
    tmp_path: Path,
) -> None:
    environment, settings, executable = _environment(tmp_path)
    settings.write_text('{"disableAgentView":true}', encoding="utf-8")
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    edit_claude_hooks(
        "install",
        executable=executable,
        timeout_seconds=1,
        environ=environment,
    )
    fake_claude = Path(__file__).parent / "fakes" / "fake_claude.py"
    plan = tmp_path / "claude-plan.json"
    plan.write_text("{}", encoding="utf-8")
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    full_environment = dict(os.environ)
    full_environment.update(environment)
    full_environment["FAKE_CLAUDE_PLAN"] = str(plan)

    healthy = run_claude_doctor(
        claude_executable=str(fake_claude),
        swbctl_executable=executable,
        hooks=HooksConfig(latency_budget_ms=10_000),
        environment=full_environment,
        proc_root=proc_root,
        uid=os.getuid(),
    )

    assert healthy.healthy
    assert healthy.provider_version == "2.1.210"
    assert healthy.cold_latency_ms is not None

    agent = proc_root / "42"
    agent.mkdir()
    (agent / "cmdline").write_bytes(b"claude\0agents\0")
    degraded = run_claude_doctor(
        claude_executable=str(fake_claude),
        swbctl_executable=executable,
        hooks=HooksConfig(latency_budget_ms=10_000),
        environment=full_environment,
        proc_root=proc_root,
        uid=os.getuid(),
    )
    assert not degraded.healthy
    assert "legacy_agent_view_runtime" in {
        diagnostic.code for diagnostic in degraded.diagnostics
    }
