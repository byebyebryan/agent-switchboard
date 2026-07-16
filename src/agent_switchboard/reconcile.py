"""Provider discovery reconciliation without provider I/O or presentation policy."""

from __future__ import annotations

from dataclasses import dataclass

from .domain import HostId, ProviderId, ValidationError
from .protocol import (
    Capability,
    CapabilityDegradation,
    ErrorRecord,
    ErrorScope,
)
from .providers.codex import (
    CodexCapabilityReport,
    CodexDiscoveryResult,
    CodexProviderIssue,
)
from .storage import (
    ProviderSessionReconciliationResult,
    Registry,
    StorageError,
    now_ms,
)


@dataclass(frozen=True, slots=True)
class CodexReconciliationResult:
    """Ephemeral output of applying one Codex discovery result."""

    reconciliation: ProviderSessionReconciliationResult | None
    capability: Capability
    errors: tuple[ErrorRecord, ...]


def _canonical_host_id(value: str) -> HostId:
    try:
        host_id = HostId(value)
    except ValidationError as error:
        raise StorageError("host_id must be a non-nil UUID") from error
    if value != str(host_id):
        raise StorageError("host_id must use canonical lowercase UUID spelling")
    return host_id


def _observed_at(value: int | None) -> int:
    timestamp = now_ms() if value is None else value
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise StorageError("observed_at must be a non-negative integer")
    return timestamp


def _synthetic_incomplete_issue() -> CodexProviderIssue:
    return CodexProviderIssue(
        code="provider_discovery_incomplete",
        message="Codex session discovery did not produce a complete result.",
        retryable=True,
        stage="discovery",
        feature="app_server_thread_list",
    )


def _inconsistent_result_issue() -> CodexProviderIssue:
    return CodexProviderIssue(
        code="provider_result_inconsistent",
        message="Codex reported a complete result while the provider was unavailable.",
        retryable=False,
        stage="discovery",
        feature="app_server_thread_list",
    )


def _capability(
    report: CodexCapabilityReport,
    *,
    available: bool,
    issues: tuple[CodexProviderIssue, ...],
) -> Capability:
    capability = Capability(
        provider=ProviderId.CODEX,
        available=available,
        provider_version=report.provider_version,
        tested_contract_min=report.tested_contract_min,
        tested_contract_max=report.tested_contract_max,
        features=report.features,
        schema_fingerprint=report.schema_fingerprint,
        degraded_reasons=tuple(
            CapabilityDegradation(
                code=issue.code,
                message=issue.message,
                retryable=issue.retryable,
                feature=issue.feature,
            )
            for issue in issues
        ),
    )
    # Reparse through the public protocol contract so even locally constructed
    # provider results cannot bypass canonical limits or privacy validation.
    return Capability.from_dict(capability.to_dict())


def _unavailable_result(
    report: CodexCapabilityReport,
    issues: tuple[CodexProviderIssue, ...],
    *,
    host_id: HostId,
    observed_at: int,
) -> CodexReconciliationResult:
    capability = _capability(report, available=False, issues=issues)
    errors = tuple(
        ErrorRecord.from_dict(
            ErrorRecord(
                code=issue.code,
                message=issue.message,
                scope=ErrorScope.PROVIDER,
                retryable=issue.retryable,
                observed_at=observed_at,
                host_id=host_id,
                provider=ProviderId.CODEX,
            ).to_dict()
        )
        for issue in issues
    )
    return CodexReconciliationResult(None, capability, errors)


def reconcile_codex_discovery(
    registry: Registry,
    host_id: str,
    discovery: CodexDiscoveryResult,
    *,
    observed_at: int | None = None,
) -> CodexReconciliationResult:
    """Apply a complete Codex scan once, or return degradation without writes."""

    parsed_host_id = _canonical_host_id(host_id)
    timestamp = _observed_at(observed_at)
    issues = discovery.capability.degraded_reasons

    if discovery.complete and not discovery.capability.available:
        if not any(issue.code == "provider_result_inconsistent" for issue in issues):
            issues = (*issues, _inconsistent_result_issue())
        return _unavailable_result(
            discovery.capability,
            issues,
            host_id=parsed_host_id,
            observed_at=timestamp,
        )

    if not discovery.complete:
        if not issues:
            issues = (_synthetic_incomplete_issue(),)
        return _unavailable_result(
            discovery.capability,
            issues,
            host_id=parsed_host_id,
            observed_at=timestamp,
        )

    capability = _capability(
        discovery.capability,
        available=True,
        issues=issues,
    )
    records = tuple(
        session.storage_record(parsed_host_id, observed_at=timestamp)
        for session in discovery.sessions
    )
    reconciliation = registry.reconcile_provider_sessions(
        str(parsed_host_id),
        ProviderId.CODEX.value,
        records,
        observed_at=timestamp,
    )
    return CodexReconciliationResult(reconciliation, capability, ())


__all__ = ["CodexReconciliationResult", "reconcile_codex_discovery"]
