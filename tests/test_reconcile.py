from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from agent_switchboard.domain import ProviderId
from agent_switchboard.protocol import ErrorScope
from agent_switchboard.providers.claude import (
    CLAUDE_FEATURES,
    ClaudeCapabilityReport,
    ClaudeProviderIssue,
)
from agent_switchboard.providers.codex import (
    CodexCapabilityReport,
    CodexDiscoveryResult,
    CodexProviderIssue,
    NormalizedCodexSession,
)
from agent_switchboard.reconcile import (
    reconcile_claude_capability,
    reconcile_codex_discovery,
)
from agent_switchboard.storage import Registry

HOST_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
SESSION_KEY = f"{HOST_ID}:codex:{SESSION_ID}"


@pytest.fixture
def registry(tmp_path) -> Registry:
    value = Registry(tmp_path / "switchboard.db")
    value.upsert_host(HOST_ID, "local", is_local=True, observed_at=1)
    yield value
    value.close()


def session(name: str | None = "provider name") -> NormalizedCodexSession:
    return NormalizedCodexSession(
        provider_session_id=UUID(SESSION_ID),
        cwd=Path("/work/project"),
        name=name,
        created_at=10,
        provider_updated_at=20,
        last_activity_at=20,
    )


def capability(
    *,
    available: bool,
    issues: tuple[CodexProviderIssue, ...] = (),
    fingerprint: str | None = None,
) -> CodexCapabilityReport:
    return CodexCapabilityReport(
        available=available,
        provider_version="0.144.4",
        tested_contract_min="0.144.4",
        tested_contract_max="0.144.4",
        features=("app_server_thread_list",),
        schema_fingerprint=fingerprint,
        degraded_reasons=issues,
    )


def test_complete_discovery_reconciles_once_and_keeps_degradation_ephemeral(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = CodexProviderIssue(
        code="schema_fingerprint_mismatch",
        message="The app-server schema differs from the tested fingerprint.",
        retryable=False,
        stage="schema",
        feature="schema_fingerprint",
    )
    discovery = CodexDiscoveryResult(
        complete=True,
        sessions=(session(),),
        capability=capability(
            available=True,
            issues=(issue,),
            fingerprint="a" * 64,
        ),
    )
    original = registry.reconcile_provider_sessions
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "reconcile_provider_sessions", counted)
    result = reconcile_codex_discovery(registry, HOST_ID, discovery, observed_at=100)

    assert calls == 1
    assert result.reconciliation is not None
    assert result.reconciliation.inserted_count == 1
    assert result.capability.available is True
    assert result.capability.provider is ProviderId.CODEX
    assert result.capability.schema_fingerprint == "a" * 64
    assert [reason.code for reason in result.capability.degraded_reasons] == [
        "schema_fingerprint_mismatch"
    ]
    assert result.errors == ()
    assert registry.get_session(SESSION_KEY)["resumability"] == "resumable"  # type: ignore[index]


def test_incomplete_discovery_returns_provider_errors_without_mutation(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconcile_codex_discovery(
        registry,
        HOST_ID,
        CodexDiscoveryResult(True, (session(),), capability(available=True)),
        observed_at=100,
    )
    before = registry.get_session(SESSION_KEY)

    def unexpected_call(*_args, **_kwargs):
        pytest.fail("incomplete discovery must not call storage reconciliation")

    monkeypatch.setattr(registry, "reconcile_provider_sessions", unexpected_call)
    issue = CodexProviderIssue(
        code="provider_request_timeout",
        message="Codex discovery timed out.",
        retryable=True,
        stage="request",
        feature="app_server_thread_list",
    )
    result = reconcile_codex_discovery(
        registry,
        HOST_ID,
        CodexDiscoveryResult(False, (), capability(available=False, issues=(issue,))),
        observed_at=200,
    )

    assert result.reconciliation is None
    assert result.capability.available is False
    assert result.capability.degraded_reasons[0].code == issue.code
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.code == issue.code
    assert error.scope is ErrorScope.PROVIDER
    assert error.host_id is not None and str(error.host_id) == HOST_ID
    assert error.provider is ProviderId.CODEX
    assert error.retryable is True
    assert error.observed_at == 200
    assert registry.get_session(SESSION_KEY) == before

    retained = json.dumps(
        {
            "capability": result.capability.to_dict(),
            "errors": [item.to_dict() for item in result.errors],
        },
        sort_keys=True,
    )
    assert "raw_payload" not in retained
    assert "prompt" not in retained
    assert "transcript" not in retained


def test_incomplete_discovery_without_issue_gets_stable_degradation(
    registry: Registry,
) -> None:
    result = reconcile_codex_discovery(
        registry,
        HOST_ID,
        CodexDiscoveryResult(False, (), capability(available=True)),
        observed_at=100,
    )

    assert result.capability.available is False
    assert [reason.code for reason in result.capability.degraded_reasons] == [
        "provider_discovery_incomplete"
    ]
    assert [error.code for error in result.errors] == ["provider_discovery_incomplete"]
    assert registry.list_sessions(host_id=HOST_ID) == []


def test_complete_discovery_carries_explicit_null_name(registry: Registry) -> None:
    result = reconcile_codex_discovery(
        registry,
        HOST_ID,
        CodexDiscoveryResult(
            True,
            (session(name=None),),
            capability(available=True),
        ),
        observed_at=100,
    )

    assert result.reconciliation is not None
    stored = registry.get_session(SESSION_KEY)
    assert stored is not None
    assert stored["name"] is None
    assert stored["provider_name"] is None
    assert stored["name_source"] == "provider"
    assert stored["metadata_source"] == "provider"


def test_claude_version_drift_is_available_degradation_without_provider_error() -> None:
    warning = ClaudeProviderIssue(
        code="untested_provider_version",
        message="The installed Claude version is outside the tested contract range.",
        retryable=False,
        stage="version",
        blocking=False,
    )
    result = reconcile_claude_capability(
        HOST_ID,
        ClaudeCapabilityReport(
            available=True,
            provider_version="9.9.9",
            tested_contract_min="2.1.214",
            tested_contract_max="2.1.214",
            features=CLAUDE_FEATURES,
            degraded_reasons=(warning,),
        ),
        observed_at=100,
    )

    assert result.capability.available
    assert [item.code for item in result.capability.degraded_reasons] == [
        "untested_provider_version"
    ]
    assert result.errors == ()


def test_complete_unavailable_result_fails_closed_without_mutation(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_call(*_args, **_kwargs):
        pytest.fail("inconsistent discovery must not call storage reconciliation")

    monkeypatch.setattr(registry, "reconcile_provider_sessions", unexpected_call)
    result = reconcile_codex_discovery(
        registry,
        HOST_ID,
        CodexDiscoveryResult(
            True,
            (session(),),
            capability(available=False),
        ),
        observed_at=100,
    )

    assert result.reconciliation is None
    assert result.capability.available is False
    assert [reason.code for reason in result.capability.degraded_reasons] == [
        "provider_result_inconsistent"
    ]
    assert [error.code for error in result.errors] == ["provider_result_inconsistent"]
    assert result.errors[0].scope is ErrorScope.PROVIDER
    assert result.errors[0].details is None
    assert registry.list_sessions(host_id=HOST_ID) == []
