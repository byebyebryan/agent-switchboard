from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from agent_switchboard._v3.domain import ProviderId
from agent_switchboard._v3.provider_runtime import (
    CODEX_SWITCHBOARD_MCP_ENV_VARS,
    CONTROL_PROMPT,
    ProviderContract,
    ProviderRuntimeError,
    build_fork_command,
    build_new_command,
    build_resume_command,
    probe_contract,
)

SESSION = UUID("11111111-1111-4111-8111-111111111111")
TARGET = UUID("22222222-2222-4222-8222-222222222222")


def test_exact_provider_argv_never_interpolates_semantic_content() -> None:
    cwd = Path("/tmp/project")
    codex = ProviderContract(ProviderId.CODEX, "/opt/bin/codex", "0.144.6")
    new = build_new_command(
        codex,
        cwd=cwd,
        session_id=SESSION,
        prompt=CONTROL_PROMPT,
        injected_environment={"AGENT_SWITCHBOARD_CAPABILITY": "opaque"},
    )
    assert new.argv == (
        "/opt/bin/codex",
        "resume",
        "-C",
        "/tmp/project",
        str(SESSION),
        CONTROL_PROMPT,
    )
    resumed = build_resume_command(
        codex,
        cwd=cwd,
        session_id=SESSION,
        prompt=None,
        injected_environment={},
    )
    assert resumed.argv[-1] == str(SESSION)
    assert CONTROL_PROMPT not in resumed.argv
    forked = build_fork_command(
        codex,
        cwd=cwd,
        source_session_id=SESSION,
        target_session_id=None,
        prompt=CONTROL_PROMPT,
        injected_environment={},
    )
    assert forked.argv[1] == "fork"
    assert forked.expected_session_id is None


def test_claude_new_resume_and_fork_preserve_exact_uuid_contract() -> None:
    claude = ProviderContract(ProviderId.CLAUDE, "/opt/bin/claude", "2.1.216")
    new = build_new_command(
        claude,
        cwd=Path("/tmp/project"),
        session_id=TARGET,
        prompt=CONTROL_PROMPT,
        injected_environment={},
    )
    assert new.argv[:3] == ("/opt/bin/claude", "--session-id", str(TARGET))
    resumed = build_resume_command(
        claude,
        cwd=Path("/tmp/project"),
        session_id=SESSION,
        prompt=None,
        injected_environment={},
    )
    assert resumed.argv == ("/opt/bin/claude", "--resume", str(SESSION))
    forked = build_fork_command(
        claude,
        cwd=Path("/tmp/project"),
        source_session_id=SESSION,
        target_session_id=TARGET,
        prompt=CONTROL_PROMPT,
        injected_environment={},
    )
    assert forked.expected_session_id == TARGET
    assert "--fork-session" in forked.argv


def test_version_probe_accepts_strict_newer_version_as_observed_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess([], 0, b"codex-cli 0.145.0\n", b"")

    monkeypatch.setattr(subprocess, "run", run)
    contract = probe_contract(ProviderId.CODEX, executable="codex")
    assert contract.version == "0.145.0"
    assert contract.known_good is False


def test_codex_rejects_claimed_preallocated_fork_target() -> None:
    contract = ProviderContract(ProviderId.CODEX, "codex", "0.144.6")
    with pytest.raises(ProviderRuntimeError) as caught:
        build_fork_command(
            contract,
            cwd=Path("/tmp/project"),
            source_session_id=SESSION,
            target_session_id=TARGET,
            prompt=None,
            injected_environment={},
        )
    assert caught.value.code == "provider_fork_identity_unsupported"


def test_contract_and_command_builders_reject_malformed_semantic_input() -> None:
    assert ProviderContract(ProviderId.CODEX, "codex", "0.145.0").known_good is False
    with pytest.raises(ProviderRuntimeError) as version:
        ProviderContract(ProviderId.CODEX, "codex", "latest")
    assert version.value.code == "provider_version_invalid"

    contract = ProviderContract(ProviderId.CODEX, "codex", "0.144.6")
    with pytest.raises(ProviderRuntimeError) as prompt:
        build_resume_command(
            contract,
            cwd=Path("/tmp/project"),
            session_id=SESSION,
            prompt="Summarize the child task.",
            injected_environment={},
        )
    assert prompt.value.code == "provider_prompt_forbidden"


def test_provider_commands_register_only_the_explicit_switchboard_mcp() -> None:
    command = ("/opt/swb-python", "-m", "agent_switchboard._v3", "agent-mcp")
    codex = build_resume_command(
        ProviderContract(ProviderId.CODEX, "codex", "0.144.6"),
        cwd=Path("/tmp/project"),
        session_id=SESSION,
        prompt=None,
        injected_environment={},
        mcp_command=command,
    )
    assert codex.argv[4:10] == (
        "-c",
        'mcp_servers.switchboard.command="/opt/swb-python"',
        "-c",
        'mcp_servers.switchboard.args=["-m","agent_switchboard._v3","agent-mcp"]',
        "-c",
        (
            "mcp_servers.switchboard.env_vars="
            '["AGENT_SWITCHBOARD_CAPABILITY","SWB_V3_CONFIG_ROOT",'
            '"SWB_V3_STATE_ROOT","SWB_V3_TMUX_SOCKET"]'
        ),
    )
    assert "AGENT_SWITCHBOARD_LAUNCH_ID" not in codex.argv
    assert "AGENT_SWITCHBOARD_SURFACE_ID" not in codex.argv
    assert "SWB_V3_SESSION_KEY" not in codex.argv
    assert CODEX_SWITCHBOARD_MCP_ENV_VARS == (
        "AGENT_SWITCHBOARD_CAPABILITY",
        "SWB_V3_CONFIG_ROOT",
        "SWB_V3_STATE_ROOT",
        "SWB_V3_TMUX_SOCKET",
    )
    claude = build_resume_command(
        ProviderContract(ProviderId.CLAUDE, "claude", "2.1.216"),
        cwd=Path("/tmp/project"),
        session_id=SESSION,
        prompt=None,
        injected_environment={},
        mcp_command=command,
    )
    assert claude.argv[3] == "--mcp-config"
    assert '"switchboard"' in claude.argv[4]
    assert "AGENT_SWITCHBOARD_CAPABILITY" not in claude.argv[4]
