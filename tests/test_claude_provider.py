from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agent_switchboard.providers.claude import (
    CLAUDE_FEATURES,
    ClaudeProvider,
    ClaudeSettingsInspection,
    inspect_claude_settings,
)

FAKE_CLAUDE = Path(__file__).parent / "fakes" / "fake_claude.py"


def settings(path: Path, disabled: bool | None = True) -> ClaudeSettingsInspection:
    return ClaudeSettingsInspection(path, disabled, None, None)


def configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan: dict[str, object],
    **bounds: object,
) -> tuple[ClaudeProvider, Path]:
    plan_path = tmp_path / "plan.json"
    log_path = tmp_path / "claude.log"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_PLAN", str(plan_path))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log_path))
    values: dict[str, object] = {
        "command_timeout": 1.0,
        "cleanup_timeout": 0.2,
    }
    values.update(bounds)
    return ClaudeProvider(str(FAKE_CLAUDE), **values), log_path  # type: ignore[arg-type]


def test_supported_disabled_profile_reports_only_bounded_features(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, log = configured(tmp_path, monkeypatch, {})

    result = provider.inspect_capability(settings(tmp_path / "settings.json"))

    assert result.available
    assert result.provider_version == "2.1.210"
    assert result.features == CLAUDE_FEATURES
    assert result.degraded_reasons == ()
    assert json.loads(log.read_text(encoding="utf-8"))["argv"] == ["--version"]


@pytest.mark.parametrize("configured_value", [None, False])
def test_agent_view_enabled_or_unset_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_value: bool | None,
) -> None:
    provider, _ = configured(tmp_path, monkeypatch, {})

    result = provider.inspect_capability(
        settings(tmp_path / "settings.json", configured_value)
    )

    assert not result.available
    assert result.degraded_reasons[-1].code == "agent_view_enabled"


def test_unsupported_version_and_provider_failure_are_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = configured(tmp_path, monkeypatch, {"stdout": "9.9.9 (Claude Code)\n"})
    unsupported = provider.inspect_capability(settings(tmp_path / "settings.json"))
    assert not unsupported.available
    assert unsupported.provider_version == "9.9.9"
    assert unsupported.degraded_reasons[-1].code == "untested_provider_version"

    missing = ClaudeProvider(str(tmp_path / "missing")).inspect_capability(
        settings(tmp_path / "settings.json")
    )
    assert not missing.available
    assert missing.provider_version is None
    assert missing.degraded_reasons[-1].code == "provider_not_found"


def test_timeout_kills_complete_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, log = configured(
        tmp_path,
        monkeypatch,
        {"spawnChild": True, "sleep": 60, "ignoreTerm": True},
        command_timeout=0.15,
        cleanup_timeout=0.15,
    )

    result = provider.inspect_capability(settings(tmp_path / "settings.json"))

    assert result.degraded_reasons[-1].code == "provider_command_timeout"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    for pid in [
        entry["pid"] for entry in entries if entry["event"] in {"invoke", "child"}
    ]:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_failed_leader_still_kills_redirected_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, log = configured(
        tmp_path,
        monkeypatch,
        {"spawnChild": True, "detachChild": True, "returncode": 7},
    )

    result = provider.inspect_capability(settings(tmp_path / "settings.json"))

    assert result.degraded_reasons[-1].code == "provider_version_failed"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    child_pid = next(entry["pid"] for entry in entries if entry["event"] == "child")
    child_stat = Path("/proc") / str(child_pid) / "stat"
    deadline = time.monotonic() + 1.0
    while True:
        # An orphan can remain as a short-lived zombie until PID 1 reaps it;
        # it must not remain an executable process.
        try:
            state = child_stat.read_text(encoding="ascii").split(") ", 1)[1][0]
        except (FileNotFoundError, ProcessLookupError):
            break
        if state == "Z":
            break
        if time.monotonic() >= deadline:
            pytest.fail("redirected provider descendant remained executable")
        time.sleep(0.01)


def test_settings_inspection_is_bounded_and_boolean_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    directory = home / ".claude"
    directory.mkdir(parents=True)
    target = directory / "settings.json"
    target.write_text(
        json.dumps({"disableAgentView": True, "unrelated": "preserved"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    inspected = inspect_claude_settings()
    assert inspected.disable_agent_view is True
    assert inspected.issue is None

    target.write_text('{"disableAgentView":"yes"}', encoding="utf-8")
    invalid = inspect_claude_settings()
    assert invalid.issue is not None
    assert invalid.issue.code == "claude_settings_invalid"


def test_settings_inspection_rejects_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    directory = home / ".claude"
    directory.mkdir(parents=True)
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    (directory / "settings.json").symlink_to(real)
    monkeypatch.setenv("HOME", str(home))

    inspected = inspect_claude_settings()
    assert inspected.issue is not None
    assert inspected.issue.code == "claude_settings_unsafe"
