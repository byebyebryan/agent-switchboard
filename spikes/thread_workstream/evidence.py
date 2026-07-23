"""Sanitized evidence records for the thread/workstream studies."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final


UUID_PATTERN: Final = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
ABSOLUTE_PATH_PATTERN: Final = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[^/\s]+/)*[^/\s]*")
FORBIDDEN_KEY_PARTS: Final = {
    "credential",
    "cwd",
    "pane_id",
    "path",
    "pid",
    "prompt",
    "raw",
    "session_id",
    "socket",
    "thread_id",
    "transcript",
}
ALLOWED_STATUSES: Final = {"pass", "falsified", "blocked"}


class EvidenceError(ValueError):
    """A result cannot be retained as sanitized evidence."""


class StudyStatus(StrEnum):
    PASS = "pass"
    FALSIFIED = "falsified"
    BLOCKED = "blocked"


def _privacy_scan(value: object, *, key_path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise EvidenceError("evidence object keys must be text")
            normalized = key.lower()
            if key_path != ("privacyAudit",) and any(
                part in normalized for part in FORBIDDEN_KEY_PARTS
            ):
                raise EvidenceError(
                    f"evidence field {'.'.join((*key_path, key))!r} is private"
                )
            _privacy_scan(nested, key_path=(*key_path, key))
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            _privacy_scan(nested, key_path=key_path)
        return
    if isinstance(value, str):
        if UUID_PATTERN.search(value):
            raise EvidenceError("evidence contains a provider UUID")
        if ABSOLUTE_PATH_PATTERN.search(value):
            raise EvidenceError("evidence contains an absolute path")
        if "\n" in value or "\r" in value:
            raise EvidenceError("evidence strings must be single-line")


def audit_sanitized_evidence(value: object) -> None:
    """Fail if a retained evidence value contains a private field or value."""

    _privacy_scan(value)


def assert_private_file(path: Path) -> None:
    """Require a bounded regular file readable only by its owner."""

    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise EvidenceError("private evidence is not a regular file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise EvidenceError("private evidence mode must be 0600")


def write_private_json(path: Path, value: object) -> None:
    """Create one private JSON file without a permissive intermediate mode."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    assert_private_file(path)


@dataclass(frozen=True, slots=True)
class StudyResult:
    """One retained study result with privacy and passing gates enforced."""

    study: str
    provider: str
    installed_version: str
    contract_fingerprint: str
    status: StudyStatus
    assertions: Mapping[str, bool]
    event_order: Sequence[str]
    isolation: Mapping[str, bool]
    cleanup: Mapping[str, bool]
    timings_ms: Mapping[str, int]
    limitations: Sequence[str] = ()
    assisted: bool = False

    def as_dict(self) -> dict[str, Any]:
        if not self.study or not self.provider or not self.installed_version:
            raise EvidenceError("study identity is incomplete")
        if len(self.contract_fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.contract_fingerprint
        ):
            raise EvidenceError("contract fingerprint must be lowercase SHA-256")
        if self.status.value not in ALLOWED_STATUSES:
            raise EvidenceError("study status is unsupported")
        if not self.assertions:
            raise EvidenceError("study requires named assertions")
        if self.status is StudyStatus.PASS and (
            self.assisted
            or not all(self.assertions.values())
            or not all(self.isolation.values())
            or not all(self.cleanup.values())
        ):
            raise EvidenceError("assisted or failed checks cannot produce pass")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in self.timings_ms.values()
        ):
            raise EvidenceError("timings must be non-negative integer milliseconds")
        result: dict[str, Any] = {
            "schemaVersion": 1,
            "study": self.study,
            "provider": self.provider,
            "installedVersion": self.installed_version,
            "contractFingerprint": self.contract_fingerprint,
            "status": self.status.value,
            "assertions": dict(sorted(self.assertions.items())),
            "eventOrder": list(self.event_order),
            "isolation": dict(sorted(self.isolation.items())),
            "cleanup": dict(sorted(self.cleanup.items())),
            "timingsMs": dict(sorted(self.timings_ms.items())),
            "assisted": self.assisted,
            "limitations": list(self.limitations),
            "privacyAudit": {
                "credentialsExcluded": True,
                "providerIdentifiersExcluded": True,
                "providerInputExcluded": True,
                "providerOutputExcluded": True,
                "runtimeLocationsExcluded": True,
                "runtimeProcessIdentifiersExcluded": True,
            },
        }
        _privacy_scan(result)
        return result

    def write(self, path: Path) -> None:
        payload = self.as_dict()
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=True,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        path.write_text(encoded, encoding="utf-8")


def sanitize_hook_order(events: Sequence[Mapping[str, Any]]) -> list[str]:
    """Map private provider identities to encounter-order aliases."""

    aliases: dict[str, str] = {}
    result: list[str] = []
    for event in events:
        provider_identity = event.get("provider_identity")
        kind = event.get("event")
        if not isinstance(provider_identity, str) or not isinstance(kind, str):
            raise EvidenceError("raw hook event lacks identity or kind")
        alias = aliases.setdefault(
            provider_identity,
            f"thread-{chr(ord('a') + len(aliases))}",
        )
        source = event.get("source")
        suffix = f":{source}" if isinstance(source, str) and source else ""
        result.append(f"{alias}:{kind}{suffix}")
    _privacy_scan(result)
    return result


__all__ = [
    "EvidenceError",
    "StudyResult",
    "StudyStatus",
    "assert_private_file",
    "audit_sanitized_evidence",
    "sanitize_hook_order",
    "write_private_json",
]
