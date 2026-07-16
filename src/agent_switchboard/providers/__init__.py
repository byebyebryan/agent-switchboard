"""Provider-native discovery adapters.

Adapters in this package return validated, privacy-safe values.  They do not
write the registry or decide how frontends present provider state.
"""

from .codex import (
    CODEX_0144_SCHEMA_FINGERPRINT,
    CODEX_TESTED_CONTRACT_MAX,
    CODEX_TESTED_CONTRACT_MIN,
    CodexCapabilityReport,
    CodexDiscoveryResult,
    CodexProvider,
    CodexProviderIssue,
    NormalizedCodexSession,
    canonical_json_fingerprint,
)

__all__ = [
    "CODEX_0144_SCHEMA_FINGERPRINT",
    "CODEX_TESTED_CONTRACT_MAX",
    "CODEX_TESTED_CONTRACT_MIN",
    "CodexCapabilityReport",
    "CodexDiscoveryResult",
    "CodexProvider",
    "CodexProviderIssue",
    "NormalizedCodexSession",
    "canonical_json_fingerprint",
]
