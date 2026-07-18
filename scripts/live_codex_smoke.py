#!/usr/bin/env python3
"""Exercise the production Codex read path without exposing live session data."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from agent_switchboard.domain import HostId
from agent_switchboard.protocol import SnapshotEnvelope
from agent_switchboard.providers.codex import CodexProvider
from agent_switchboard.reconcile import reconcile_codex_discovery
from agent_switchboard.snapshot import build_host_snapshot_json
from agent_switchboard.storage import Registry

DEFAULT_CODEX_EXECUTABLE = "codex"
DEFAULT_EXPECTED_VERSION = "0.144.6"
DEFAULT_EXPECTED_FINGERPRINT = (
    "5d8251e1e2f713a3c567c927386f84f2f94692d4721b90d8ff36d0ff92877621"
)
EXPECTED_FEATURES = ("app_server_thread_list", "schema_fingerprint")


class _SmokeFailure(RuntimeError):
    """Internal sentinel whose details are never printed."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the production Codex discovery pipeline and print only "
            "a sanitized summary."
        )
    )
    parser.add_argument(
        "--codex",
        default=DEFAULT_CODEX_EXECUTABLE,
        help=f"Codex executable (default: {DEFAULT_CODEX_EXECUTABLE})",
    )
    parser.add_argument(
        "--expect-version",
        default=DEFAULT_EXPECTED_VERSION,
        help=f"required provider version (default: {DEFAULT_EXPECTED_VERSION})",
    )
    parser.add_argument(
        "--expect-fingerprint",
        default=DEFAULT_EXPECTED_FINGERPRINT,
        help="required canonical schema SHA-256 fingerprint",
    )
    return parser


def _validated_fingerprint(value: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise _SmokeFailure
    return value


def _run_smoke(
    *,
    executable: str,
    expected_version: str,
    expected_fingerprint: str,
) -> dict[str, object]:
    if not executable or "\x00" in executable or not expected_version:
        raise _SmokeFailure
    expected_fingerprint = _validated_fingerprint(expected_fingerprint)
    started = time.monotonic()

    discovery = CodexProvider(executable=executable).discover_sessions()
    report = discovery.capability
    if (
        not discovery.complete
        or not report.available
        or report.provider_version != expected_version
        or report.schema_fingerprint != expected_fingerprint
        or report.features != EXPECTED_FEATURES
        or report.degraded_reasons
    ):
        raise _SmokeFailure

    host_id = HostId.new()
    with tempfile.TemporaryDirectory(prefix="switchboard-live-smoke-") as raw:
        database = Path(raw) / "switchboard.db"
        with Registry(database) as registry:
            registry.upsert_host(
                str(host_id),
                "live-smoke",
                is_local=True,
            )
            result = reconcile_codex_discovery(
                registry,
                str(host_id),
                discovery,
            )
            if (
                result.reconciliation is None
                or not result.capability.available
                or result.capability.features != EXPECTED_FEATURES
                or result.capability.degraded_reasons
                or result.errors
            ):
                raise _SmokeFailure
            snapshot_json = build_host_snapshot_json(
                registry,
                str(host_id),
                capabilities=(result.capability,),
                errors=result.errors,
            )

    snapshot = SnapshotEnvelope.from_json(snapshot_json)
    if (
        snapshot.to_json() != snapshot_json
        or len(snapshot.capabilities) != 1
        or snapshot.errors
    ):
        raise _SmokeFailure
    capability = snapshot.capabilities[0]
    if (
        capability.provider_version != expected_version
        or capability.schema_fingerprint != expected_fingerprint
        or not capability.available
        or capability.features != EXPECTED_FEATURES
        or capability.degraded_reasons
    ):
        raise _SmokeFailure

    elapsed_ms = int((time.monotonic() - started) * 1_000)
    return {
        "elapsedMs": elapsed_ms,
        "features": sorted(capability.features),
        "providerVersion": capability.provider_version,
        "schemaFingerprint": capability.schema_fingerprint,
        "sessionCount": len(snapshot.sessions),
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        summary = _run_smoke(
            executable=arguments.codex,
            expected_version=arguments.expect_version,
            expected_fingerprint=arguments.expect_fingerprint,
        )
    except Exception:
        print("live Codex smoke failed", file=sys.stderr)
        return 1
    print(
        json.dumps(
            summary,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
