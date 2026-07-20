from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from agent_switchboard import __version__
from agent_switchboard.domain import HostId
from agent_switchboard.providers.codex import (
    CODEX_0144_SCHEMA_FINGERPRINT,
    CodexDiscoveryResult,
    CodexProvider,
    canonical_json_fingerprint,
)

FAKE_CODEX = Path(__file__).parent / "fakes" / "fake_codex.py"
CODEX_EXPERIMENTAL_FIXTURE = (
    Path(__file__).parents[1]
    / "spikes"
    / "fixtures"
    / "codex"
    / "0.144.4"
    / "codex_app_server_protocol.v2.schemas.json"
)
CODEX_NONEXPERIMENTAL_FIXTURE = (
    CODEX_EXPERIMENTAL_FIXTURE.parent
    / "nonexperimental"
    / "codex_app_server_protocol.v2.schemas.json"
)
HOST_ID = HostId("11111111-1111-4111-8111-111111111111")


def thread(
    number: int,
    *,
    source: str = "cli",
    ephemeral: bool = False,
    cwd: str | None = None,
    name: object = "A safe title",
    preview: object = "SECRET PREVIEW",
    turns: object = None,
) -> dict[str, Any]:
    return {
        "id": f"00000000-0000-4000-8000-{number:012d}",
        "cwd": cwd or f"/work/session-{number}",
        "cliVersion": "0.144.4",
        "createdAt": 100 + number,
        "updatedAt": 200 + number,
        "recencyAt": 300 + number,
        "ephemeral": ephemeral,
        "modelProvider": "openai",
        "preview": preview,
        "sessionId": f"10000000-0000-4000-8000-{number:012d}",
        "source": source,
        "status": {"type": "idle"},
        "turns": [] if turns is None else turns,
        "name": name,
        "path": f"/private/provider/history-{number}.jsonl",
        "gitInfo": {"originUrl": "https://credential.invalid/private"},
        "extra": {"raw": "provider-private"},
    }


def configured_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan: dict[str, Any],
    **bounds: Any,
) -> tuple[CodexProvider, Path]:
    plan_path = tmp_path / "plan.json"
    log_path = tmp_path / "fake.log"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("FAKE_CODEX_PLAN", str(plan_path))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    defaults: dict[str, Any] = {
        "request_timeout": 1.0,
        "total_timeout": 3.0,
        "command_timeout": 1.0,
        "cleanup_timeout": 0.2,
    }
    defaults.update(bounds)
    return CodexProvider(str(FAKE_CODEX), **defaults), log_path


def log_entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def requests(path: Path) -> list[dict[str, Any]]:
    return [
        entry["message"]
        for entry in log_entries(path)
        if entry.get("event") == "request"
    ]


def result_code(result: CodexDiscoveryResult) -> str:
    return result.capability.degraded_reasons[-1].code


def minimal_v2_schema(*, semantic_marker: object = None) -> dict[str, Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "CodexAppServerProtocol",
        "type": "object",
        "definitions": {
            "Thread": {"type": "object"},
            "ThreadListParams": {"type": "object"},
            "ThreadListResponse": {"type": "object"},
        },
        "semanticMarker": semantic_marker,
    }


def test_three_page_scan_uses_exact_contract_and_normalizes_safe_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, third = thread(1), thread(2), thread(3)
    provider, log = configured_provider(
        tmp_path,
        monkeypatch,
        {
            "app": {
                "pages": [
                    [{"result": {"data": [first], "nextCursor": "opaque-a"}}],
                    [{"result": {"data": [second], "nextCursor": "opaque-b"}}],
                    [
                        {
                            "result": {
                                "data": [third],
                                "nextCursor": None,
                                "backwardsCursor": "ignored-backwards",
                            }
                        }
                    ],
                ]
            }
        },
    )

    result = provider.discover_sessions()

    assert result.complete
    assert result.capability.available
    assert result.capability.provider_version == "0.144.6"
    assert result.capability.schema_fingerprint
    assert [str(item.provider_session_id) for item in result.sessions] == [
        first["id"],
        second["id"],
        third["id"],
    ]
    normalized = result.sessions[0]
    assert normalized.created_at == 101_000
    assert normalized.provider_updated_at == 201_000
    assert normalized.last_activity_at == 301_000
    assert normalized.cwd == Path("/work/session-1")
    storage = normalized.storage_record(HOST_ID, observed_at=999_000)
    assert storage["metadata_source"] == "provider"
    assert storage["name"] == "A safe title"
    assert (
        not {
            "preview",
            "turns",
            "status",
            "path",
            "gitInfo",
            "extra",
            "runtime_presence",
            "activity",
        }
        & storage.keys()
    )

    sent = requests(log)
    assert sent[:2] == [
        {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "agent_switchboard",
                    "title": "Switchboard",
                    "version": __version__,
                }
            },
        },
        {"method": "initialized", "params": {}},
    ]
    page_params = [message["params"] for message in sent[2:]]
    assert page_params == [
        {
            "limit": 100,
            "sourceKinds": ["cli"],
            "archived": False,
            "useStateDbOnly": False,
        },
        {
            "limit": 100,
            "sourceKinds": ["cli"],
            "archived": False,
            "useStateDbOnly": False,
            "cursor": "opaque-a",
        },
        {
            "limit": 100,
            "sourceKinds": ["cli"],
            "archived": False,
            "useStateDbOnly": False,
            "cursor": "opaque-b",
        },
    ]


def test_notifications_wrong_ids_and_bounded_stderr_are_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {
            "app": {
                "initializeActions": [
                    {"json": {"method": "server/notice", "params": {}}},
                    {"wrongResult": {"not": "ours"}},
                    {
                        "result": {
                            "userAgent": "fake",
                            "codexHome": "/fake/home",
                            "platformFamily": "unix",
                            "platformOs": "linux",
                        }
                    },
                ],
                "pages": [
                    [
                        {"json": {"method": "thread/status/changed", "params": {}}},
                        {"wrongResult": {"data": [thread(99)]}},
                        {"result": {"data": [thread(1)]}},
                    ]
                ],
            }
        },
        max_stderr_bytes=128_000,
    )

    result = provider.discover_sessions()

    assert result.complete
    assert len(result.sessions) == 1
    assert str(result.sessions[0].provider_session_id) == thread(1)["id"]


def test_missing_executable_is_structured_and_incomplete(tmp_path: Path) -> None:
    provider = CodexProvider(
        str(tmp_path / "not-installed"),
        command_timeout=0.1,
        cleanup_timeout=0.1,
    )

    result = provider.discover_sessions()

    assert not result.complete
    assert not result.capability.available
    assert result.sessions == ()
    assert result.capability.provider_version is None
    assert result_code(result) == "provider_not_found"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ({"returncode": 7}, "provider_version_failed"),
        ({"stdout": "not a version\n"}, "provider_version_invalid"),
    ],
)
def test_version_failures_prevent_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: dict[str, Any],
    expected: str,
) -> None:
    provider, log = configured_provider(tmp_path, monkeypatch, {"version": version})

    result = provider.discover_sessions()

    assert not result.complete
    assert result_code(result) == expected
    assert not any(message.get("method") == "initialize" for message in requests(log))


@pytest.mark.parametrize(
    ("schema", "expected_code", "retryable"),
    [
        ({"returncode": 9}, "schema_generation_failed", True),
        ({"raw": "not-json"}, "schema_invalid", False),
        ({"omit": True}, "schema_output_missing", True),
        ({"value": []}, "schema_invalid", False),
    ],
)
def test_schema_failure_is_distinct_and_does_not_discard_a_complete_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema: dict[str, Any],
    expected_code: str,
    retryable: bool,
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"schema": schema, "app": {"pages": [[{"result": {"data": [thread(1)]}}]]}},
    )

    result = provider.discover_sessions()

    assert result.complete
    assert result.capability.available
    assert result.capability.schema_fingerprint is None
    assert result.capability.features == ("app_server_thread_list",)
    degradation = result.capability.degraded_reasons[0]
    assert degradation.stage == "schema"
    assert degradation.code == expected_code
    assert degradation.retryable is retryable


def test_unknown_version_is_shape_probed_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {
            "version": {"stdout": "codex-cli 9.9.9\n"},
            "app": {"pages": [[{"result": {"data": [thread(1)]}}]]},
        },
    )

    result = provider.discover_sessions()

    assert result.complete
    assert result.capability.available
    assert result.capability.provider_version == "9.9.9"
    assert result.capability.degraded_reasons[-1].code == "untested_provider_version"
    assert not result.capability.degraded_reasons[-1].blocking


def test_schema_fingerprint_is_canonical_and_tracks_semantic_changes() -> None:
    left = {"definitions": {"B": 2, "A": [1, {"é": True}]}, "type": "object"}
    reordered = {"type": "object", "definitions": {"A": [1, {"é": True}], "B": 2}}
    changed = {"type": "object", "definitions": {"A": [1, {"é": False}], "B": 2}}

    assert canonical_json_fingerprint(left) == canonical_json_fingerprint(reordered)
    assert canonical_json_fingerprint(left) != canonical_json_fingerprint(changed)


def test_retained_0144_nonexperimental_fixture_has_contract_fingerprint() -> None:
    parsed = json.loads(CODEX_NONEXPERIMENTAL_FIXTURE.read_bytes())

    assert canonical_json_fingerprint(parsed) == CODEX_0144_SCHEMA_FINGERPRINT


def test_retained_0144_experimental_fixture_is_explicitly_distinct() -> None:
    parsed = json.loads(CODEX_EXPERIMENTAL_FIXTURE.read_bytes())

    assert canonical_json_fingerprint(parsed) == (
        "f5e8d20f3a8f9bb5e5b23ab0c5aa6bde7b12e7e0713606c5d0132651a4959d37"
    )
    assert canonical_json_fingerprint(parsed) != CODEX_0144_SCHEMA_FINGERPRINT


def test_schema_probe_uses_private_directory_and_no_experimental_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, log = configured_provider(tmp_path, monkeypatch, {})

    assert provider.discover_sessions().complete
    schema_events = [entry for entry in log_entries(log) if entry["event"] == "schema"]
    assert schema_events[0]["mode"] == 0o700
    assert "--experimental" not in schema_events[0]["argv"]


def test_initialize_timeout_is_bounded_and_child_is_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, log = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"initializeActions": [{"sleep": 10}]}},
        request_timeout=0.25,
        total_timeout=0.5,
    )

    result = provider.discover_sessions()

    assert not result.complete
    assert result_code(result) == "app_server_timeout"
    starts = [entry for entry in log_entries(log) if entry["event"] == "start"]
    assert len(starts) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(starts[0]["pid"], 0)


def test_mid_pagination_timeout_returns_no_partial_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {
            "app": {
                "pages": [
                    [{"result": {"data": [thread(1)], "nextCursor": "next"}}],
                    [{"sleep": 10}],
                ]
            }
        },
        request_timeout=0.05,
        total_timeout=0.2,
    )

    result = provider.discover_sessions()

    assert not result.complete
    assert result.sessions == ()
    assert result_code(result) == "app_server_timeout"


@pytest.mark.parametrize(
    ("actions", "expected"),
    [
        ([{"raw": "{broken json}\n"}], "app_server_malformed_json"),
        (
            [
                {
                    "raw": (
                        '{"id":1,"result":{"data":[],"oversized":'
                        + "1" * 5_000
                        + "}}\n"
                    )
                }
            ],
            "app_server_malformed_json",
        ),
        ([{"json": ["not", "an", "object"]}], "app_server_invalid_message"),
        (
            [{"error": {"code": -32000, "message": "SECRET provider payload"}}],
            "app_server_rpc_error",
        ),
        ([{"result": []}], "app_server_invalid_result"),
        ([{"result": {}}], "invalid_thread_list"),
        ([{"exit": True}], "app_server_closed"),
    ],
)
def test_malformed_or_failed_page_is_structured_without_payload_leakage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    actions: list[dict[str, Any]],
    expected: str,
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"pages": [actions]}},
    )

    result = provider.discover_sessions()

    assert not result.complete
    assert result_code(result) == expected
    assert "SECRET" not in repr(result)
    assert "SECRET" not in result.capability.degraded_reasons[-1].message


def test_repeated_cursor_and_page_limit_are_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repeated, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {
            "app": {
                "pages": [
                    [{"result": {"data": [], "nextCursor": "same"}}],
                    [{"result": {"data": [], "nextCursor": "same"}}],
                ]
            }
        },
    )
    assert result_code(repeated.discover_sessions()) == "repeated_pagination_cursor"

    limited_dir = tmp_path / "limited"
    limited_dir.mkdir()
    limited, _ = configured_provider(
        limited_dir,
        monkeypatch,
        {
            "app": {
                "pages": [
                    [
                        {
                            "result": {
                                "data": [thread(1, ephemeral=True)],
                                "nextCursor": "one",
                            }
                        }
                    ],
                    [
                        {
                            "result": {
                                "data": [thread(2, ephemeral=True)],
                                "nextCursor": "two",
                            }
                        }
                    ],
                ]
            }
        },
        max_pages=2,
    )
    assert result_code(limited.discover_sessions()) == "pagination_page_limit"


@pytest.mark.parametrize(
    ("result", "bounds", "expected"),
    [
        (
            {"data": [], "nextCursor": "x" * 100},
            {"max_cursor_bytes": 16},
            "invalid_pagination_cursor",
        ),
        (
            {"data": [], "backwardsCursor": 42},
            {},
            "invalid_pagination_cursor",
        ),
    ],
)
def test_cursor_shapes_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, Any],
    bounds: dict[str, Any],
    expected: str,
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"pages": [[{"result": result}]]}},
        **bounds,
    )

    assert result_code(provider.discover_sessions()) == expected


def test_line_limit_is_enforced_before_json_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"pages": [[{"lineBytes": 1000}]]}},
        max_line_bytes=128,
    )

    assert result_code(provider.discover_sessions()) == "app_server_line_too_large"


def test_ephemeral_and_non_cli_rows_are_filtered_before_content_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ephemeral = thread(1, ephemeral=True, preview={"unexpected": "shape"})
    non_cli = thread(2, source="vscode", turns="not-a-list")
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"pages": [[{"result": {"data": [ephemeral, non_cli]}}]]}},
    )

    result = provider.discover_sessions()

    assert result.complete
    assert result.sessions == ()


def test_identical_duplicates_dedupe_and_conflicting_duplicates_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    duplicate = thread(1)
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"pages": [[{"result": {"data": [duplicate, duplicate]}}]]}},
    )
    result = provider.discover_sessions()
    assert result.complete
    assert len(result.sessions) == 1

    conflict_dir = tmp_path / "conflict"
    conflict_dir.mkdir()
    conflicting = dict(duplicate, updatedAt=999)
    provider, _ = configured_provider(
        conflict_dir,
        monkeypatch,
        {"app": {"pages": [[{"result": {"data": [duplicate, conflicting]}}]]}},
    )
    result = provider.discover_sessions()
    assert not result.complete
    assert result_code(result) == "conflicting_thread_duplicate"


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (lambda value: value.pop("updatedAt"), "invalid_thread_shape"),
        (lambda value: value.update(id="not-a-uuid"), "invalid_thread_identity"),
        (lambda value: value.update(cwd="relative/path"), "invalid_thread_cwd"),
        (lambda value: value.update(createdAt=-1), "invalid_thread_shape"),
        (lambda value: value.update(status={"type": "future"}), "invalid_thread_shape"),
        (lambda value: value.update(turns={}), "invalid_thread_shape"),
    ],
)
def test_unsafe_or_incomplete_durable_thread_aborts_complete_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutator: Any,
    expected: str,
) -> None:
    invalid = thread(1)
    mutator(invalid)
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"pages": [[{"result": {"data": [invalid]}}]]}},
    )

    result = provider.discover_sessions()

    assert not result.complete
    assert result.sessions == ()
    assert result_code(result) == expected


def test_thread_content_and_status_never_enter_normalized_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = thread(
        1,
        preview="TOP SECRET PROMPT",
        turns=[{"items": [{"text": "TOP SECRET RESPONSE"}]}],
    )
    raw["status"] = {"type": "active", "activeFlags": ["waitingOnApproval"]}
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"pages": [[{"result": {"data": [raw]}}]]}},
    )

    result = provider.discover_sessions()

    assert result.complete
    assert "TOP SECRET" not in repr(result)
    storage = result.sessions[0].storage_record(HOST_ID, observed_at=500_000)
    assert "runtime_presence" not in storage
    assert "activity" not in storage


def test_unsafe_optional_name_is_omitted_without_losing_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {
            "app": {
                "pages": [[{"result": {"data": [thread(1, name="unsafe\nterminal")]}}]]
            }
        },
    )

    result = provider.discover_sessions()

    assert result.complete
    assert result.sessions[0].name is None
    assert result.sessions[0].storage_record(HOST_ID, observed_at=1)["name"] is None


def test_recency_falls_back_to_updated_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = thread(1)
    raw["recencyAt"] = None
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"pages": [[{"result": {"data": [raw]}}]]}},
    )

    result = provider.discover_sessions()

    assert result.complete
    assert result.sessions[0].last_activity_at == result.sessions[0].provider_updated_at


@pytest.mark.parametrize(
    "missing", ["userAgent", "codexHome", "platformFamily", "platformOs"]
)
def test_initialize_requires_the_verified_0144_string_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    initialize = {
        "userAgent": "fake",
        "codexHome": "/fake/home",
        "platformFamily": "unix",
        "platformOs": "linux",
    }
    del initialize[missing]
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"initializeActions": [{"result": initialize}]}},
    )

    result = provider.discover_sessions()

    assert not result.complete
    assert result_code(result) == "invalid_initialize_result"
    assert result.capability.schema_fingerprint == CODEX_0144_SCHEMA_FINGERPRINT


def test_nonreading_app_server_cannot_block_a_large_cursor_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = "c" * (2 * 1024 * 1024)
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {
            "app": {
                "pages": [
                    [
                        {"result": {"data": [thread(1)], "nextCursor": cursor}},
                        {"sleep": 10},
                    ]
                ]
            }
        },
        request_timeout=0.1,
        total_timeout=0.4,
        cleanup_timeout=0.05,
        max_cursor_bytes=3 * 1024 * 1024,
        max_line_bytes=3 * 1024 * 1024,
        max_stdout_bytes=3 * 1024 * 1024,
    )

    started = time.monotonic()
    result = provider.discover_sessions()
    elapsed = time.monotonic() - started

    assert not result.complete
    assert result_code(result) == "app_server_timeout"
    assert elapsed < 1.5


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_schema_special_files_are_rejected_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"schema": {"kind": kind}},
    )

    result = provider.discover_sessions()

    assert result.complete
    degradation = result.capability.degraded_reasons[0]
    assert degradation.code == "schema_output_unsafe"
    assert not degradation.retryable


def test_schema_size_limit_reads_no_more_than_the_configured_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {},
        max_schema_bytes=1024,
    )

    result = provider.discover_sessions()

    assert result.complete
    assert result.capability.degraded_reasons[0].code == "schema_too_large"


def test_known_version_schema_mismatch_is_nonfatal_and_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"schema": {"value": minimal_v2_schema(semantic_marker="changed")}},
    )

    result = provider.discover_sessions()

    assert result.complete
    assert result.capability.available
    assert result.capability.schema_fingerprint != CODEX_0144_SCHEMA_FINGERPRINT
    mismatch = result.capability.degraded_reasons[0]
    assert mismatch.code == "schema_contract_mismatch"
    assert not mismatch.retryable


def test_successful_version_and_schema_evidence_survives_discovery_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {
            "app": {
                "pages": [[{"error": {"code": -32000, "message": "private failure"}}]]
            }
        },
    )

    result = provider.discover_sessions()

    assert not result.complete
    assert result.capability.provider_version == "0.144.6"
    assert result.capability.schema_fingerprint == CODEX_0144_SCHEMA_FINGERPRINT
    assert result.capability.features == ("schema_fingerprint",)
    assert result_code(result) == "app_server_rpc_error"


@pytest.mark.parametrize("rpc_code", [-32700, -32600, -32601, -32602])
def test_contract_json_rpc_errors_are_nonretryable_without_message_leakage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rpc_code: int,
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {
            "app": {
                "pages": [
                    [
                        {
                            "error": {
                                "code": rpc_code,
                                "message": "SECRET contract payload",
                            }
                        }
                    ]
                ]
            }
        },
    )

    result = provider.discover_sessions()

    issue = result.capability.degraded_reasons[-1]
    assert issue.code == "app_server_incompatible_rpc"
    assert not issue.retryable
    assert "SECRET" not in issue.message


def test_graceful_app_server_shutdown_uses_eof_without_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, log = configured_provider(tmp_path, monkeypatch, {})

    assert provider.discover_sessions().complete
    events = [entry["event"] for entry in log_entries(log)]
    assert "eof" in events
    assert "terminate" not in events


def test_sigterm_resistant_app_server_is_killed_and_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, log = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"ignoreTerm": True, "lingerOnEof": 10}},
        cleanup_timeout=0.05,
    )

    started = time.monotonic()
    result = provider.discover_sessions()
    elapsed = time.monotonic() - started

    assert result.complete
    entries = log_entries(log)
    process_id = next(entry["pid"] for entry in entries if entry["event"] == "start")
    assert any(entry["event"] == "eof" for entry in entries)
    assert any(entry["event"] == "terminate_ignored" for entry in entries)
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)
    assert elapsed < 1.5


@pytest.mark.parametrize(
    "actions",
    [
        [{"rawHex": "ff0a"}],
        [{"result": {"data": [thread(1, name="\ud800")]}}],
        [{"result": {"data": [], "nextCursor": "\ud800"}}],
        [{"raw": '{"id":1,"result":{"data":[],"nextCursor":NaN}}\n'}],
    ],
)
def test_invalid_utf8_surrogates_and_nonfinite_page_values_are_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    actions: list[dict[str, Any]],
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"pages": [actions]}},
    )

    result = provider.discover_sessions()

    assert not result.complete
    assert result_code(result) == "app_server_malformed_json"


def test_recursive_page_json_is_a_structured_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = "[" * 10_000 + "]" * 10_000
    raw = f'{{"id":1,"result":{{"data":{nested}}}}}\n'
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"pages": [[{"raw": raw}]]}},
    )

    result = provider.discover_sessions()

    assert not result.complete
    assert result_code(result) in {
        "app_server_malformed_json",
        "invalid_thread_shape",
    }


@pytest.mark.parametrize(
    "raw",
    [
        '{"$schema":"x","title":"\\ud800","type":"object",'
        '"definitions":{"Thread":{},"ThreadListParams":{},'
        '"ThreadListResponse":{}}}',
        '{"$schema":"x","title":"x","type":"object",'
        '"definitions":{"Thread":{},"ThreadListParams":{},'
        '"ThreadListResponse":{}},"invalid":NaN}',
    ],
)
def test_invalid_schema_scalars_are_specific_nonfatal_degradations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"schema": {"raw": raw}},
    )

    result = provider.discover_sessions()

    assert result.complete
    issue = result.capability.degraded_reasons[0]
    assert issue.code == "schema_invalid"
    assert not issue.retryable


def test_identical_empty_pages_may_advance_through_distinct_opaque_cursors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {
            "app": {
                "pages": [
                    [{"result": {"data": [], "nextCursor": "one"}}],
                    [{"result": {"data": [], "nextCursor": "two"}}],
                    [{"result": {"data": [], "nextCursor": None}}],
                ]
            }
        },
    )

    result = provider.discover_sessions()

    assert result.complete
    assert result.sessions == ()


def test_page_item_and_total_normalized_session_limits_are_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    over_page, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {
            "app": {
                "pages": [[{"result": {"data": [thread(i) for i in range(1, 102)]}}]]
            }
        },
    )
    assert result_code(over_page.discover_sessions()) == "thread_list_page_limit"

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    over_sessions, _ = configured_provider(
        session_dir,
        monkeypatch,
        {"app": {"pages": [[{"result": {"data": [thread(1), thread(2)]}}]]}},
        max_sessions=1,
    )
    assert result_code(over_sessions.discover_sessions()) == "normalized_session_limit"


@pytest.mark.parametrize(
    ("actions", "bounds", "expected"),
    [
        (
            [{"lineBytes": 2000}],
            {"max_stdout_bytes": 500, "max_line_bytes": 3000},
            "app_server_stdout_limit",
        ),
        (
            [{"stderrBytes": 2000}, {"result": {"data": []}}],
            {"max_stderr_bytes": 500},
            "app_server_stderr_limit",
        ),
    ],
)
def test_cumulative_provider_output_limits_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    actions: list[dict[str, Any]],
    bounds: dict[str, int],
    expected: str,
) -> None:
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"pages": [actions]}},
        **bounds,
    )

    assert result_code(provider.discover_sessions()) == expected


@pytest.mark.parametrize(
    "bounds",
    [
        {"request_timeout": float("nan")},
        {"total_timeout": float("inf")},
        {"command_timeout": 0},
        {"cleanup_timeout": True},
        {"max_pages": 1.5},
        {"max_sessions": True},
        {"max_stdout_bytes": 0},
    ],
)
def test_provider_bounds_are_finite_positive_and_strictly_typed(
    bounds: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        CodexProvider(**bounds)


@pytest.mark.parametrize(
    "updates",
    [
        {"createdAt": 300, "updatedAt": 200, "recencyAt": 400},
        {"createdAt": 300, "updatedAt": 400, "recencyAt": 200},
    ],
)
def test_impossible_timestamp_ordering_rejects_the_complete_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, int],
) -> None:
    invalid = thread(1)
    invalid.update(updates)
    provider, _ = configured_provider(
        tmp_path,
        monkeypatch,
        {"app": {"pages": [[{"result": {"data": [invalid]}}]]}},
    )

    assert result_code(provider.discover_sessions()) == "invalid_thread_timestamps"
