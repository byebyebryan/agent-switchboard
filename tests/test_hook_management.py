from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

import agent_switchboard.cli as cli_module
import agent_switchboard.hook_config as hook_config_module
from agent_switchboard.config import ConfigError, HooksConfig, parse_config
from agent_switchboard.doctor import DoctorResult, run_doctor
from agent_switchboard.domain import HostId
from agent_switchboard.hook_config import (
    HOOK_EVENTS,
    HOOK_STATUS_MESSAGE,
    HookConfigError,
    HookEditResult,
    edit_codex_hooks,
)
from agent_switchboard.providers.codex import CodexProvider

ROOT = Path(__file__).parents[1]
FAKE_CODEX = ROOT / "tests" / "fakes" / "fake_codex.py"
HOST_ID = HostId("11111111-1111-4111-8111-111111111111")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def hook_metadata(
    event_name: str,
    *,
    command: str,
    source_path: Path,
    matcher: str | None,
    trust: str = "trusted",
    enabled: bool = True,
    timeout: int = 1,
    status: str = HOOK_STATUS_MESSAGE,
) -> dict[str, Any]:
    return {
        "command": command,
        "currentHash": "safe-opaque-hash",
        "displayOrder": 1,
        "enabled": enabled,
        "eventName": event_name,
        "handlerType": "command",
        "isManaged": False,
        "key": f"key-{event_name}",
        "matcher": matcher,
        "pluginId": None,
        "source": "user",
        "sourcePath": str(source_path),
        "statusMessage": status,
        "timeoutSec": timeout,
        "trustStatus": trust,
    }


def test_hooks_config_defaults_custom_values_and_validation() -> None:
    default = parse_config("", host_id=HOST_ID)
    assert default.hooks == HooksConfig(timeout_seconds=1, latency_budget_ms=100)

    custom = parse_config(
        "[hooks]\ntimeout_seconds=3\nlatency_budget_ms=750\n",
        host_id=HOST_ID,
    )
    assert custom.hooks == HooksConfig(timeout_seconds=3, latency_budget_ms=750)

    for document in (
        "[hooks]\ntimeout_seconds=0\n",
        "[hooks]\nlatency_budget_ms=0\n",
        "[hooks]\nunknown=true\n",
    ):
        with pytest.raises(ConfigError):
            parse_config(document, host_id=HOST_ID)


def test_swbctl_resolution_prefers_the_invoked_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invoked = tmp_path / "current" / "swbctl"
    invoked.parent.mkdir()
    invoked.touch(mode=0o755)
    stale = tmp_path / "stale" / "swbctl"
    stale.parent.mkdir()
    stale.touch(mode=0o755)
    monkeypatch.setattr(hook_config_module.sys, "argv", [str(invoked)])
    monkeypatch.setenv("PATH", str(stale.parent))

    assert hook_config_module.resolve_swbctl_executable() == invoked


def test_install_preserves_unrelated_hooks_is_private_and_idempotent(
    tmp_path: Path,
) -> None:
    codex = tmp_path / "codex-home"
    destination = codex / "hooks.json"
    unrelated = {
        "type": "command",
        "command": "/opt/other-tool event",
        "commandWindows": "C:\\Tools\\other.exe event",
        "timeout": 42,
        "statusMessage": "Other tool",
    }
    write_json(
        destination,
        {
            "vendor": {"keep": True},
            "hooks": {
                "Stop": [{"matcher": "ignored", "hooks": [unrelated]}],
            },
        },
    )
    destination.chmod(0o644)
    environment = {"CODEX_HOME": str(codex), "HOME": str(tmp_path)}
    executable = tmp_path / "bin" / "swbctl"
    executable.parent.mkdir()
    executable.touch(mode=0o755)

    installed = edit_codex_hooks(
        "install",
        executable=executable,
        timeout_seconds=1,
        environ=environment,
    )
    assert installed.changed
    assert installed.installed_handlers == 5
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    transaction_lock = codex / ".hooks.json.agent-switchboard.lock"
    assert stat.S_ISREG(transaction_lock.stat().st_mode)
    assert stat.S_IMODE(transaction_lock.stat().st_mode) == 0o600
    document = json.loads(destination.read_text(encoding="utf-8"))
    assert document["vendor"] == {"keep": True}
    assert document["hooks"]["Stop"][0]["hooks"] == [unrelated]
    for event in HOOK_EVENTS:
        groups = document["hooks"][event]
        owned = [
            handler
            for group in groups
            for handler in group["hooks"]
            if handler.get("statusMessage") == HOOK_STATUS_MESSAGE
        ]
        assert len(owned) == 1
        assert owned[0]["command"] == f"{executable} event --provider codex"
        assert owned[0]["timeout"] == 1

    before = destination.read_bytes()
    again = edit_codex_hooks(
        "install",
        executable=executable,
        timeout_seconds=1,
        environ=environment,
    )
    assert not again.changed
    assert destination.read_bytes() == before

    destination.chmod(0o644)
    secured = edit_codex_hooks(
        "install",
        executable=executable,
        timeout_seconds=1,
        environ=environment,
    )
    assert secured.changed
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert destination.read_bytes() == before


def test_dry_run_never_creates_codex_home_and_uninstall_is_ownership_safe(
    tmp_path: Path,
) -> None:
    codex = tmp_path / "missing-codex-home"
    environment = {"CODEX_HOME": str(codex), "HOME": str(tmp_path)}
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)
    preview = edit_codex_hooks(
        "install",
        executable=executable,
        timeout_seconds=1,
        dry_run=True,
        environ=environment,
    )
    assert preview.changed
    assert not codex.exists()

    edit_codex_hooks(
        "install",
        executable=executable,
        timeout_seconds=1,
        environ=environment,
    )
    destination = codex / "hooks.json"
    document = json.loads(destination.read_text(encoding="utf-8"))
    document["hooks"]["Stop"][0]["hooks"].append(
        {"type": "command", "command": "/opt/keep stop"}
    )
    write_json(destination, document)

    removed = edit_codex_hooks(
        "uninstall",
        executable=executable,
        timeout_seconds=1,
        environ=environment,
    )
    assert removed.changed
    assert removed.removed_handlers == 5
    remaining = json.loads(destination.read_text(encoding="utf-8"))
    assert set(remaining["hooks"]) == {"Stop"}
    assert remaining["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "/opt/keep stop"}]}
    ]


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        '{"hooks":[]}',
        '{"hooks":null}',
        '{"hooks":{"Stop":{}}}',
        '{"hooks":{"Stop":[{"matcher":3,"hooks":[]}]}}',
        '{"hooks":{"Stop":[{"hooks":[{}]}]}}',
        '{"hooks":{"Stop":[{"hooks":[{"type":"command"}]}]}}',
        '{"hooks":{"Stop":[{"hooks":[{"type":"future"}]}]}}',
        (
            '{"hooks":{"Stop":[{"hooks":['
            '{"type":"command","command":"/bin/true","timeout":true}'
            "]}]}}"
        ),
        (
            '{"hooks":{"Stop":[{"hooks":['
            '{"type":"command","command":"/bin/true","async":"yes"}'
            "]}]}}"
        ),
        '{"hooks":{},"hooks":{}}',
        "{broken",
    ],
)
def test_malformed_hook_files_are_refused_without_rewrite(
    tmp_path: Path, value: str
) -> None:
    codex = tmp_path / "codex"
    destination = codex / "hooks.json"
    destination.parent.mkdir()
    destination.write_text(value, encoding="utf-8")
    before = destination.read_bytes()
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)

    with pytest.raises(HookConfigError):
        edit_codex_hooks(
            "install",
            executable=executable,
            timeout_seconds=1,
            environ={"CODEX_HOME": str(codex)},
        )
    assert destination.read_bytes() == before


def test_hook_file_symlink_and_failed_atomic_replace_are_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / "codex"
    codex.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    destination = codex / "hooks.json"
    destination.symlink_to(outside)
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)
    environment = {"CODEX_HOME": str(codex)}
    with pytest.raises(HookConfigError, match="regular file"):
        edit_codex_hooks(
            "install",
            executable=executable,
            timeout_seconds=1,
            environ=environment,
        )
    assert outside.read_text(encoding="utf-8") == "{}"

    destination.unlink()
    destination.write_text("{}", encoding="utf-8")
    before = destination.read_bytes()
    monkeypatch.setattr(
        hook_config_module.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(HookConfigError, match="cannot update"):
        edit_codex_hooks(
            "install",
            executable=executable,
            timeout_seconds=1,
            environ=environment,
        )
    assert destination.read_bytes() == before
    assert list(codex.glob("*.tmp")) == []


def test_fifo_hook_path_is_rejected_without_blocking(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    codex.mkdir()
    destination = codex / "hooks.json"
    os.mkfifo(destination)
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)

    with pytest.raises(HookConfigError, match="regular file"):
        edit_codex_hooks(
            "install",
            executable=executable,
            timeout_seconds=1,
            environ={"CODEX_HOME": str(codex)},
        )


def test_external_edit_observed_before_publish_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / "codex"
    destination = codex / "hooks.json"
    write_json(destination, {})
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)
    concurrent = {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/bin/true"}]}]}
    }
    original = hook_config_module._atomic_write

    def race(handle: object, path: Path, payload: bytes, expected: object) -> None:
        write_json(destination, concurrent)
        original(handle, path, payload, expected)

    monkeypatch.setattr(hook_config_module, "_atomic_write", race)
    with pytest.raises(HookConfigError, match="changed before"):
        edit_codex_hooks(
            "install",
            executable=executable,
            timeout_seconds=1,
            environ={"CODEX_HOME": str(codex)},
        )
    assert json.loads(destination.read_text(encoding="utf-8")) == concurrent


def test_destination_path_swap_is_not_followed_or_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / "codex"
    destination = codex / "hooks.json"
    write_json(destination, {})
    outside = tmp_path / "outside.json"
    outside.write_text('{"outside":true}', encoding="utf-8")
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)
    original = hook_config_module._atomic_write

    def swap(handle: object, path: Path, payload: bytes, expected: object) -> None:
        destination.unlink()
        destination.symlink_to(outside)
        original(handle, path, payload, expected)

    monkeypatch.setattr(hook_config_module, "_atomic_write", swap)
    with pytest.raises(HookConfigError, match="regular file"):
        edit_codex_hooks(
            "install",
            executable=executable,
            timeout_seconds=1,
            environ={"CODEX_HOME": str(codex)},
        )
    assert destination.is_symlink()
    assert outside.read_text(encoding="utf-8") == '{"outside":true}'


def test_switchboard_writers_serialize_and_reload_before_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / "codex"
    destination = codex / "hooks.json"
    unrelated = {"type": "command", "command": "/opt/keep stop"}
    write_json(destination, {"hooks": {"Stop": [{"hooks": [unrelated]}]}})
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)
    environment = {"CODEX_HOME": str(codex)}
    first_at_replace = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_done = threading.Event()
    replace_guard = threading.Lock()
    original_replace = hook_config_module.os.replace
    replace_calls = 0
    results: dict[str, HookEditResult] = {}
    errors: list[BaseException] = []

    def paused_replace(*args: object, **kwargs: object) -> None:
        nonlocal replace_calls
        with replace_guard:
            pause = replace_calls == 0
            replace_calls += 1
        if pause:
            first_at_replace.set()
            if not release_first.wait(5):
                raise AssertionError("timed out waiting to release the first writer")
        original_replace(*args, **kwargs)

    def run(action: str, name: str) -> None:
        if name == "second":
            second_started.set()
        try:
            results[name] = edit_codex_hooks(
                action,
                executable=executable,
                timeout_seconds=1,
                environ=environment,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            if name == "second":
                second_done.set()

    monkeypatch.setattr(hook_config_module.os, "replace", paused_replace)
    first = threading.Thread(target=run, args=("install", "first"))
    second = threading.Thread(target=run, args=("install", "second"))
    first.start()
    assert first_at_replace.wait(5)
    second.start()
    assert second_started.wait(5)
    try:
        assert not second_done.wait(0.2)
    finally:
        release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert results["first"].changed
    assert results["first"].installed_handlers == 5
    assert not results["second"].changed
    document = json.loads(destination.read_text(encoding="utf-8"))
    assert document["hooks"]["Stop"][0]["hooks"] == [unrelated]
    for event in HOOK_EVENTS:
        owned = [
            handler
            for group in document["hooks"][event]
            for handler in group["hooks"]
            if handler.get("statusMessage") == HOOK_STATUS_MESSAGE
        ]
        assert len(owned) == 1


def test_missing_codex_home_symlink_substitution_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / "codex"
    outside = tmp_path / "outside"
    outside.mkdir()
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)
    original_mkdir = hook_config_module.os.mkdir

    def substitute(
        name: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        original_mkdir(name, mode, dir_fd=dir_fd)
        if dir_fd is not None and os.fsdecode(name) == codex.name:
            os.rmdir(name, dir_fd=dir_fd)
            os.symlink(outside, name, dir_fd=dir_fd, target_is_directory=True)

    monkeypatch.setattr(hook_config_module.os, "mkdir", substitute)
    with pytest.raises(HookConfigError, match="CODEX_HOME"):
        edit_codex_hooks(
            "install",
            executable=executable,
            timeout_seconds=1,
            environ={"CODEX_HOME": str(codex)},
        )

    assert codex.is_symlink()
    assert list(outside.iterdir()) == []


def test_transaction_lock_symlink_is_refused_without_touching_target(
    tmp_path: Path,
) -> None:
    codex = tmp_path / "codex"
    codex.mkdir()
    outside = tmp_path / "outside.lock"
    outside.write_text("sentinel", encoding="utf-8")
    (codex / ".hooks.json.agent-switchboard.lock").symlink_to(outside)
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)

    with pytest.raises(HookConfigError, match="transaction lock"):
        edit_codex_hooks(
            "install",
            executable=executable,
            timeout_seconds=1,
            environ={"CODEX_HOME": str(codex)},
        )

    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert not (codex / "hooks.json").exists()


def test_oversized_transaction_lock_is_refused(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    codex.mkdir()
    transaction_lock = codex / ".hooks.json.agent-switchboard.lock"
    transaction_lock.write_bytes(b"x" * 4097)
    executable = tmp_path / "swbctl"
    executable.touch(mode=0o755)

    with pytest.raises(HookConfigError, match="private regular file"):
        edit_codex_hooks(
            "install",
            executable=executable,
            timeout_seconds=1,
            environ={"CODEX_HOME": str(codex)},
        )

    assert transaction_lock.read_bytes() == b"x" * 4097
    assert not (codex / "hooks.json").exists()


def test_explicit_environment_never_falls_back_to_real_home(tmp_path: Path) -> None:
    with pytest.raises(HookConfigError, match="explicit environment"):
        hook_config_module.codex_home(environ={})
    with pytest.raises(HookConfigError, match="HOME"):
        hook_config_module.codex_home(environ={"HOME": "relative"})
    assert (
        hook_config_module.codex_home(environ={"HOME": str(tmp_path)})
        == tmp_path / ".codex"
    )


def test_provider_hooks_list_contract_and_feature_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    log = tmp_path / "log.jsonl"
    source = tmp_path / "codex" / "hooks.json"
    command = f"{tmp_path / 'swbctl'} event --provider codex"
    entry = {
        "cwd": str(tmp_path),
        "errors": [],
        "hooks": [
            hook_metadata(
                "sessionStart",
                command=command,
                source_path=source,
                matcher="^(startup|resume|clear|compact)$",
            )
        ],
        "warnings": ["source warning"],
    }
    write_json(plan, {"app": {"hooks": [{"result": {"data": [entry]}}]}})
    environment = dict(os.environ)
    environment.update(
        {
            "FAKE_CODEX_PLAN": str(plan),
            "FAKE_CODEX_LOG": str(log),
            "SWITCHBOARD_TEST_ENV": "provider-environment",
            "CODEX_HOME": str(tmp_path / "isolated-codex"),
            "XDG_STATE_HOME": str(tmp_path / "isolated-state"),
        }
    )

    result = CodexProvider(str(FAKE_CODEX), environment=environment).inspect_hooks(
        cwds=(tmp_path,)
    )
    assert result.available
    assert result.provider_version == "0.144.4"
    assert result.issues == ()
    assert result.entries[0].hooks[0].event_name == "sessionStart"
    assert result.entries[0].warnings == ("source warning",)
    requests = [json.loads(line) for line in log.read_text().splitlines()]
    hooks_request = next(
        item["message"]
        for item in requests
        if item.get("event") == "request"
        and item["message"].get("method") == "hooks/list"
    )
    assert hooks_request["params"] == {"cwds": [str(tmp_path)]}
    invocations = [item for item in requests if item.get("event") == "invoke"]
    assert all(
        item["environmentMarker"] == "provider-environment"
        and item["codexHome"] == str(tmp_path / "isolated-codex")
        and item["xdgStateHome"] == str(tmp_path / "isolated-state")
        for item in invocations
    )

    write_json(
        plan,
        {"app": {"hooks": [{"result": {"data": [{"bad": True}]}}]}},
    )
    failed = CodexProvider(str(FAKE_CODEX), environment=environment).inspect_hooks(
        cwds=(tmp_path,)
    )
    assert not failed.available
    assert failed.issues[-1].feature == "hooks_list"
    assert failed.issues[-1].code == "invalid_hooks_list"


def test_doctor_reports_healthy_effective_hooks_and_isolated_latency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex"
    source = codex_home / "hooks.json"
    swbctl = tmp_path / "bin" / "swbctl"
    swbctl.parent.mkdir()
    swbctl.write_text(
        "#!/bin/sh\n"
        'test -z "$AGENT_SWITCHBOARD_LAUNCH_ID" || exit 9\n'
        'test -z "$AGENT_SWITCHBOARD_SURFACE_ID" || exit 9\n'
        'test -z "$AGENT_SWITCHBOARD_TEST_ATTACHMENT" || exit 9\n'
        'test -z "$TMUX" || exit 9\n'
        'test -z "$TMUX_PANE" || exit 9\n'
        "cat >/dev/null\n",
        encoding="utf-8",
    )
    swbctl.chmod(0o755)
    command = f"{swbctl} event --provider codex"
    event_names = {
        "SessionStart": ("sessionStart", "^(startup|resume|clear|compact)$"),
        "UserPromptSubmit": ("userPromptSubmit", None),
        "PermissionRequest": ("permissionRequest", ".*"),
        "PostToolUse": ("postToolUse", ".*"),
        "Stop": ("stop", None),
    }
    metadata = [
        hook_metadata(
            event_name,
            command=command,
            source_path=source,
            matcher=matcher,
        )
        for event_name, matcher in event_names.values()
    ]
    plan = tmp_path / "plan.json"
    write_json(
        plan,
        {
            "app": {
                "hooks": [
                    {
                        "result": {
                            "data": [
                                {
                                    "cwd": str(tmp_path),
                                    "errors": [],
                                    "hooks": metadata,
                                    "warnings": [],
                                }
                            ]
                        }
                    }
                ]
            }
        },
    )
    log = tmp_path / "doctor-codex.log"
    monkeypatch.setenv("FAKE_CODEX_PLAN", str(plan))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(codex_home)
    environment["HOME"] = str(tmp_path / "home")
    environment["XDG_STATE_HOME"] = str(tmp_path / "provider-state")
    environment["SWITCHBOARD_TEST_ENV"] = "doctor-environment"
    environment["AGENT_SWITCHBOARD_LAUNCH_ID"] = "11111111-1111-4111-8111-111111111111"
    environment["AGENT_SWITCHBOARD_SURFACE_ID"] = "22222222-2222-4222-8222-222222222222"
    environment["AGENT_SWITCHBOARD_TEST_ATTACHMENT"] = "must-not-leak"
    environment["TMUX"] = "/tmp/user-tmux,123,0"
    environment["TMUX_PANE"] = "%9"

    result = run_doctor(
        codex_executable=str(FAKE_CODEX),
        swbctl_executable=swbctl,
        hooks=HooksConfig(),
        cwd=tmp_path,
        environment=environment,
    )
    assert result.healthy
    assert result.diagnostics == ()
    assert result.cold_latency_ms is not None
    assert result.warm_p95_latency_ms is not None
    assert "Agent Switchboard doctor: healthy" in result.render()
    assert not (tmp_path / "state").exists()
    invocations = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event") == "invoke"
    ]
    assert invocations
    assert all(
        item["environmentMarker"] == "doctor-environment"
        and item["codexHome"] == str(codex_home)
        and item["xdgStateHome"] == str(tmp_path / "provider-state")
        for item in invocations
    )


def test_doctor_reports_trust_duplicates_source_errors_and_stale_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex"
    source = codex_home / "hooks.json"
    swbctl = tmp_path / "swbctl"
    swbctl.write_text("#!/bin/sh\ncat >/dev/null\n", encoding="utf-8")
    swbctl.chmod(0o755)
    stale = tmp_path / "old" / "swbctl"
    command = f"{stale} event --provider codex"
    metadata = [
        hook_metadata(
            "sessionStart",
            command=command,
            source_path=source,
            matcher="wrong",
            trust="untrusted",
            enabled=False,
        ),
        hook_metadata(
            "sessionStart",
            command=command,
            source_path=source,
            matcher="wrong",
        ),
    ]
    plan = tmp_path / "plan.json"
    write_json(
        plan,
        {
            "app": {
                "hooks": [
                    {
                        "result": {
                            "data": [
                                {
                                    "cwd": str(tmp_path),
                                    "errors": [
                                        {"path": str(source), "message": "bad source"}
                                    ],
                                    "hooks": metadata,
                                    "warnings": ["merged hook sources"],
                                }
                            ]
                        }
                    }
                ]
            }
        },
    )
    monkeypatch.setenv("FAKE_CODEX_PLAN", str(plan))
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(codex_home)
    result = run_doctor(
        codex_executable=str(FAKE_CODEX),
        swbctl_executable=swbctl,
        hooks=HooksConfig(timeout_seconds=1, latency_budget_ms=1_000),
        cwd=tmp_path,
        environment=environment,
    )
    codes = {diagnostic.code for diagnostic in result.diagnostics}
    assert not result.healthy
    assert {
        "hook_duplicate",
        "hook_untrusted",
        "hook_disabled",
        "hook_modified",
        "hook_command_stale",
        "hook_source_warning",
        "hook_source_error",
        "hook_missing",
    }.issubset(codes)


def test_doctor_health_retains_errors_after_diagnostic_render_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex"
    swbctl = tmp_path / "swbctl"
    swbctl.write_text("#!/bin/sh\ncat >/dev/null\n", encoding="utf-8")
    swbctl.chmod(0o755)
    plan = tmp_path / "plan.json"
    write_json(
        plan,
        {
            "app": {
                "hooks": [
                    {
                        "result": {
                            "data": [
                                {
                                    "cwd": str(tmp_path),
                                    "errors": [],
                                    "hooks": [],
                                    "warnings": [
                                        f"warning {index}" for index in range(300)
                                    ],
                                }
                            ]
                        }
                    }
                ]
            }
        },
    )
    monkeypatch.setenv("FAKE_CODEX_PLAN", str(plan))
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(codex_home)

    result = run_doctor(
        codex_executable=str(FAKE_CODEX),
        swbctl_executable=swbctl,
        hooks=HooksConfig(),
        cwd=tmp_path,
        environment=environment,
    )
    assert not result.healthy
    assert len(result.diagnostics) == 256
    assert all(item.level == "warning" for item in result.diagnostics)
    assert result.diagnostics[-1].code == "diagnostics_truncated"


def test_hook_cli_wires_dry_run_without_writing_real_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_home = tmp_path / "config"
    codex_home = tmp_path / "codex"
    swbctl = tmp_path / "swbctl"
    swbctl.touch(mode=0o755)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(cli_module, "resolve_swbctl_executable", lambda: swbctl)
    observed: list[tuple[str, bool]] = []

    def edit(action: str, **options: object) -> HookEditResult:
        observed.append((action, bool(options["dry_run"])))
        return HookEditResult(
            codex_home / "hooks.json",
            True,
            0,
            5,
            True,
        )

    monkeypatch.setattr(cli_module, "edit_codex_hooks", edit)
    assert (
        cli_module.main(["hooks", "install", "--provider", "codex", "--dry-run"]) == 0
    )
    captured = capsys.readouterr()
    assert observed == [("install", True)]
    assert "would update" in captured.out
    assert "Codex /hooks" in captured.out
    assert captured.err == ""
    assert not codex_home.exists()


def test_doctor_cli_returns_health_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    swbctl = tmp_path / "swbctl"
    swbctl.touch(mode=0o755)
    monkeypatch.setattr(
        cli_module,
        "_codex_executable",
        lambda: ("codex", HooksConfig()),
    )
    monkeypatch.setattr(cli_module, "resolve_swbctl_executable", lambda: swbctl)
    monkeypatch.setattr(
        cli_module,
        "run_doctor",
        lambda **_options: DoctorResult(True, "0.144.4", 1.0, 2.0, ()),
    )
    assert cli_module.main(["doctor"]) == 0
    assert "doctor: healthy" in capsys.readouterr().out

    monkeypatch.setattr(
        cli_module,
        "run_doctor",
        lambda **_options: DoctorResult(False, "0.144.4", None, None, ()),
    )
    assert cli_module.main(["doctor"]) == 1
    assert "doctor: unhealthy" in capsys.readouterr().out
