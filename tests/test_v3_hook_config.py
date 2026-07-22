from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from agent_switchboard._v3.hook_config import (
    HOOK_EVENTS,
    STATUS_MESSAGE,
    HookConfigError,
    edit_hooks,
)


def executable(tmp_path: Path) -> Path:
    value = tmp_path / "bin" / "swbctl"
    value.parent.mkdir()
    value.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    value.chmod(0o755)
    return value


@pytest.mark.parametrize(
    ("provider", "root_variable", "filename"),
    [
        ("codex", "CODEX_HOME", "hooks.json"),
        ("claude", "CLAUDE_CONFIG_DIR", "settings.json"),
    ],
)
def test_install_is_owned_private_idempotent_and_reversible(
    tmp_path: Path,
    provider: str,
    root_variable: str,
    filename: str,
) -> None:
    root = tmp_path / provider
    root.mkdir()
    destination = root / filename
    unrelated = {
        "type": "command",
        "command": "/opt/other event",
        "statusMessage": "Other tool",
    }
    destination.write_text(
        json.dumps(
            {"vendor": {"keep": True}, "hooks": {"Stop": [{"hooks": [unrelated]}]}}
        ),
        encoding="utf-8",
    )
    environment = {"HOME": str(tmp_path), root_variable: str(root)}
    command = executable(tmp_path)

    installed = edit_hooks(
        "install",
        provider,
        executable=command,
        timeout_seconds=2,
        environment=environment,
    )
    assert installed.changed
    assert installed.installed_handlers == 5
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    document = json.loads(destination.read_text(encoding="utf-8"))
    assert document["vendor"] == {"keep": True}
    assert document["hooks"]["Stop"][0]["hooks"] == [unrelated]
    owned = [
        handler
        for event in HOOK_EVENTS
        for group in document["hooks"][event]
        for handler in group["hooks"]
        if handler.get("statusMessage") == STATUS_MESSAGE
    ]
    assert len(owned) == 5

    before = destination.read_bytes()
    repeated = edit_hooks(
        "install",
        provider,
        executable=command,
        timeout_seconds=2,
        environment=environment,
    )
    assert not repeated.changed
    assert destination.read_bytes() == before

    removed = edit_hooks(
        "uninstall",
        provider,
        executable=command,
        timeout_seconds=2,
        environment=environment,
    )
    assert removed.removed_handlers == 5
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "hooks": {"Stop": [{"hooks": [unrelated]}]},
        "vendor": {"keep": True},
    }


def test_dry_run_does_not_create_provider_directory(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    result = edit_hooks(
        "install",
        "codex",
        executable=executable(tmp_path),
        timeout_seconds=1,
        dry_run=True,
        environment={"HOME": str(tmp_path), "CODEX_HOME": str(root)},
    )
    assert result.changed and result.dry_run
    assert not root.exists()


@pytest.mark.parametrize("unsafe", ["document", "lock", "directory"])
def test_symlink_boundaries_fail_closed(tmp_path: Path, unsafe: str) -> None:
    root = tmp_path / "codex"
    outside = tmp_path / "outside"
    outside.mkdir()
    command = executable(tmp_path)
    if unsafe == "directory":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        name = (
            "hooks.json"
            if unsafe == "document"
            else ".hooks.json.agent-switchboard.lock"
        )
        (root / name).symlink_to(outside / "target")
    with pytest.raises(HookConfigError):
        edit_hooks(
            "install",
            "codex",
            executable=command,
            timeout_seconds=1,
            environment={"HOME": str(tmp_path), "CODEX_HOME": str(root)},
        )


def test_invalid_existing_configuration_is_never_replaced(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    destination = root / "hooks.json"
    destination.write_text('{"hooks": {}, "hooks": {}}', encoding="utf-8")
    before = destination.read_bytes()
    with pytest.raises(HookConfigError):
        edit_hooks(
            "install",
            "codex",
            executable=executable(tmp_path),
            timeout_seconds=1,
            environment={"HOME": str(tmp_path), "CODEX_HOME": str(root)},
        )
    assert destination.read_bytes() == before
