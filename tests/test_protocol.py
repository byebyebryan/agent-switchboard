from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_switchboard.domain import (
    HostId,
    PresentationContext,
    ProviderId,
    SessionKey,
    SurfaceId,
)
from agent_switchboard.protocol import (
    MAX_JSON_BYTES,
    Capability,
    CapabilityEnvelope,
    ErrorEnvelope,
    ErrorRecord,
    ErrorScope,
    IncompatibleProtocolError,
    IncompatibleSchemaError,
    PresentationPlan,
    PresentationPlanEnvelope,
    PresentationPlanKind,
    ProtocolError,
    SessionAction,
    SessionActionEnvelope,
    SessionActionStatus,
    SnapshotEnvelope,
)

FIXTURES = Path(__file__).parent / "fixtures/protocol/v1"
HOST = HostId("11111111-1111-4111-8111-111111111111")
PROJECT = "22222222-2222-4222-8222-222222222222"
SURFACE = SurfaceId("33333333-3333-4333-8333-333333333333")
LOCATION = "44444444-4444-4444-8444-444444444444"
SESSION_ID = "55555555-5555-4555-8555-555555555555"
LAUNCH = "66666666-6666-4666-8666-666666666666"
SESSION_KEY = f"{HOST}:codex:{SESSION_ID}"
CLAUDE_SESSION_KEY = SessionKey.parse(f"{HOST}:claude:{SESSION_ID}")


@pytest.mark.parametrize(
    ("name", "envelope_type"),
    [
        ("snapshot.json", SnapshotEnvelope),
        ("capability.json", CapabilityEnvelope),
        ("error.json", ErrorEnvelope),
        ("presentation-plan.json", PresentationPlanEnvelope),
    ],
)
def test_versioned_redacted_fixture_round_trip(name: str, envelope_type: type) -> None:
    raw = (FIXTURES / name).read_text(encoding="utf-8")
    parsed = envelope_type.from_json(raw)
    canonical = parsed.to_json()
    assert envelope_type.from_json(canonical) == parsed
    assert "futureEnvelopeField" not in canonical
    assert "futureField" not in canonical


def test_snapshot_contract_fields_and_capability_report() -> None:
    snapshot = SnapshotEnvelope.from_json((FIXTURES / "snapshot.json").read_bytes())
    assert snapshot.host.host_id == HOST
    assert snapshot.projects[0]["name"] == "example"
    capability = snapshot.capabilities[0]
    assert capability.provider_version == "0.144.4"
    assert capability.tested_contract_min == "0.144.4"
    assert capability.tested_contract_max == "0.144.4"
    assert capability.features == (
        "app_server_thread_list",
        "schema_fingerprint",
    )
    assert capability.schema_fingerprint == (
        "5d8251e1e2f713a3c567c927386f84f2f94692d4721b90d8ff36d0ff92877621"
    )


def test_claude_disabled_agent_view_profile_is_healthy() -> None:
    envelope = CapabilityEnvelope.from_json((FIXTURES / "capability.json").read_bytes())
    capability = envelope.capability
    assert capability.provider_version == "2.1.210"
    assert capability.available
    assert capability.features == ("hooks", "native_resume", "tmux_runtime")
    assert capability.degraded_reasons == ()


def test_unavailable_capability_requires_degraded_reason() -> None:
    data = json.loads((FIXTURES / "capability.json").read_text())
    data["capability"]["available"] = False
    data["capability"]["degradedReasons"] = []
    with pytest.raises(ProtocolError, match="must explain"):
        CapabilityEnvelope.from_dict(data)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("schemaVersion", 2, IncompatibleSchemaError),
        ("protocolVersion", 2, IncompatibleProtocolError),
        ("schemaVersion", True, ProtocolError),
    ],
)
def test_incompatible_or_malformed_versions_are_explicit(
    field: str, value: object, error: type[Exception]
) -> None:
    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data[field] = value
    with pytest.raises(error):
        SnapshotEnvelope.from_dict(data)


def test_snapshot_requires_every_known_collection() -> None:
    data = json.loads((FIXTURES / "snapshot.json").read_text())
    del data["sessions"]
    with pytest.raises(ProtocolError, match="sessions is required"):
        SnapshotEnvelope.from_dict(data)
    data["sessions"] = {}
    with pytest.raises(ProtocolError, match="must be an array"):
        SnapshotEnvelope.from_dict(data)


def test_snapshot_validates_known_records_and_strips_safe_additions() -> None:
    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data["projects"] = [
        {
            "projectId": PROJECT,
            "name": "example",
            "aliases": ["router"],
            "futureProjectField": {"safe": True},
        }
    ]
    data["locations"] = [
        {
            "locationId": LOCATION,
            "projectId": PROJECT,
            "hostId": str(HOST),
            "path": "/work/example",
            "isDefault": True,
            "futureLocationField": 1,
        }
    ]
    data["sessions"] = [
        {
            "sessionKey": SESSION_KEY,
            "hostId": str(HOST),
            "provider": "codex",
            "providerSessionId": SESSION_ID,
            "projectId": PROJECT,
            "locationId": LOCATION,
            "firstObservedAt": 10,
            "lastObservedAt": 20,
            "runtimePresence": "live",
            "resumability": "resumable",
            "activity": "working",
            "activityReason": "unknown",
            "attachment": "detached",
            "surfaceId": str(SURFACE),
            "metadataSource": "provider",
            "stateConfidence": "confirmed",
            "futureSessionField": ["safe"],
        }
    ]
    data["runtimes"] = [
        {
            "hostId": str(HOST),
            "provider": "codex",
            "sessionKey": SESSION_KEY,
            "runtimePresence": "live",
            "resumability": "resumable",
            "activity": "working",
            "activityReason": "unknown",
            "attachment": "detached",
            "observedAt": 20,
            "futureRuntimeField": False,
        }
    ]
    data["surfaces"] = [
        {
            "surfaceId": str(SURFACE),
            "hostId": str(HOST),
            "provider": "codex",
            "transport": "tmux",
            "transportLocator": "as-codex:@1.%1",
            "role": "session",
            "bindingConfidence": "confirmed",
            "currentSessionKey": SESSION_KEY,
            "launchId": LAUNCH,
            "createdAt": 10,
            "lastObservedAt": 20,
            "clientAttached": False,
            "futureSurfaceField": "safe",
        }
    ]

    parsed = SnapshotEnvelope.from_dict(data)
    canonical = parsed.to_dict()
    assert canonical["projects"][0]["aliases"] == ["router"]
    assert canonical["surfaces"][0]["provider"] == "codex"
    assert not any(
        key.startswith("future")
        for collection in ("projects", "locations", "sessions", "runtimes", "surfaces")
        for record in canonical[collection]
        for key in record
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("apiKey", "redacted-but-forbidden"),
        ("providerToken", "redacted-but-forbidden"),
        ("rawPayload", {"safe": True}),
        ("prompt", "redacted-but-forbidden"),
        ("transcript", []),
        ("argv", ["provider", "--resume"]),
    ],
)
def test_snapshot_rejects_sensitive_or_unsafe_generic_fields(
    field: str, value: object
) -> None:
    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data["futureEnvelopeField"] = {field: value}
    with pytest.raises(ProtocolError, match="forbidden"):
        SnapshotEnvelope.from_dict(data)


@pytest.mark.parametrize(
    "field",
    [
        "clientSecret",
        "client\uff33ecret",
        "accessKey",
        "chatHistory",
        "userInput",
        "history",
        "input",
        "sessionCookie",
        "commandArgv",
        "environmentVariables",
        "tokenValue",
        "modelResponse",
        "toolResult",
        "modelResult",
        "toolResponse",
        "commandOutput",
        "stdout",
        "chatMessages",
        "conversationId",
        "rawProviderEvent",
    ],
)
def test_retained_structured_details_reject_sensitive_key_variants(field: str) -> None:
    data = json.loads((FIXTURES / "error.json").read_text())
    data["error"]["details"][field] = "must not be retained"
    with pytest.raises(ProtocolError, match="forbidden"):
        ErrorEnvelope.from_dict(data)
    with pytest.raises(ProtocolError, match="forbidden"):
        ErrorRecord.from_dict(data["error"])


def test_mapping_input_obeys_the_same_total_byte_bound_as_json() -> None:
    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data["futureEnvelopeField"] = ["x" * (64 * 1024)] * 129
    assert len(json.dumps(data, ensure_ascii=False).encode()) > MAX_JSON_BYTES
    with pytest.raises(ProtocolError, match="byte limit"):
        SnapshotEnvelope.from_dict(data)


def test_retained_details_use_an_explicit_typed_safe_field_contract() -> None:
    data = json.loads((FIXTURES / "error.json").read_text())
    data["error"]["details"] = {"latencyMs": 12}
    with pytest.raises(ProtocolError, match="unsupported retained detail"):
        ErrorEnvelope.from_dict(data)

    data["error"]["details"] = {"payloadHash": "not-a-digest"}
    with pytest.raises(ProtocolError, match="lowercase SHA-256"):
        ErrorEnvelope.from_dict(data)

    data["error"]["details"] = {
        "capability": "provider.claude.agent_view",
        "emittedCount": 8,
        "latency": 12.5,
        "payloadHash": "a" * 64,
        "retainedCount": 10,
    }
    error = ErrorEnvelope.from_dict(data)
    assert error.error.details == data["error"]["details"]
    assert error.to_dict()["error"]["details"] == data["error"]["details"]

    data["error"]["details"]["emittedCount"] = 10.5
    with pytest.raises(ProtocolError, match="non-negative integer"):
        ErrorEnvelope.from_dict(data)
    data["error"]["details"]["emittedCount"] = 11
    with pytest.raises(ProtocolError, match="must not exceed"):
        ErrorEnvelope.from_dict(data)

    capability = json.loads((FIXTURES / "capability.json").read_text())
    capability["capability"]["degradedReasons"] = [
        {
            "code": "example_degradation",
            "message": "Structured example.",
            "retryable": False,
            "details": {"payloadHash": "arbitrary retained content"},
        }
    ]
    with pytest.raises(ProtocolError, match="lowercase SHA-256"):
        CapabilityEnvelope.from_dict(capability)


def test_typed_runtime_payload_hash_remains_supported() -> None:
    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data["runtimes"][0]["payloadHash"] = "b" * 64
    snapshot = SnapshotEnvelope.from_dict(data)
    assert snapshot.runtimes[0]["payloadHash"] == "b" * 64
    assert snapshot.to_dict()["runtimes"][0]["payloadHash"] == "b" * 64


def test_capability_schema_fingerprint_is_a_sha256_digest() -> None:
    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data["capabilities"][0]["schemaFingerprint"] = "not-a-digest"
    with pytest.raises(ProtocolError, match="lowercase SHA-256"):
        SnapshotEnvelope.from_dict(data)


@pytest.mark.parametrize("value", ["bad\x00value", "bad\x1b[31mvalue", "bad\nvalue"])
def test_protocol_rejects_terminal_control_characters(value: str) -> None:
    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data["host"]["displayName"] = value
    with pytest.raises(ProtocolError, match="control"):
        SnapshotEnvelope.from_dict(data)


def test_protocol_rejects_terminal_controls_in_unknown_keys() -> None:
    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data["future\x1bField"] = True
    with pytest.raises(ProtocolError, match="control"):
        SnapshotEnvelope.from_dict(data)


def test_snapshot_rejects_generic_records_and_cross_host_rows() -> None:
    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data["projects"] = [{"futureField": "not a project"}]
    with pytest.raises(ProtocolError, match="projectId is required"):
        SnapshotEnvelope.from_dict(data)

    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data["projects"] = [{"projectId": PROJECT, "name": "example"}]
    data["locations"] = [
        {
            "locationId": LOCATION,
            "projectId": PROJECT,
            "hostId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "path": "/work/example",
        }
    ]
    with pytest.raises(ProtocolError, match="does not match"):
        SnapshotEnvelope.from_dict(data)


def test_snapshot_rejects_inconsistent_session_identity_and_references() -> None:
    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data["sessions"] = [
        {
            "sessionKey": SESSION_KEY,
            "hostId": str(HOST),
            "provider": "claude",
            "providerSessionId": SESSION_ID,
            "firstObservedAt": 10,
            "lastObservedAt": 20,
            "runtimePresence": "unknown",
            "resumability": "unknown",
            "activity": "unknown",
            "activityReason": "unknown",
            "attachment": "unknown",
            "metadataSource": "provider",
            "stateConfidence": "unknown",
        }
    ]
    with pytest.raises(ProtocolError, match="identity fields disagree"):
        SnapshotEnvelope.from_dict(data)

    data["sessions"][0]["provider"] = "codex"
    data["sessions"][0]["projectId"] = "77777777-7777-4777-8777-777777777777"
    with pytest.raises(ProtocolError, match="not in projects"):
        SnapshotEnvelope.from_dict(data)


def test_snapshot_requires_symmetric_surface_binding_and_ordered_lifetime() -> None:
    data = json.loads((FIXTURES / "snapshot.json").read_text())
    del data["sessions"][0]["surfaceId"]
    with pytest.raises(ProtocolError, match="session binding is inconsistent"):
        SnapshotEnvelope.from_dict(data)

    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data["surfaces"][0]["currentSessionKey"] = None
    with pytest.raises(ProtocolError, match="requires currentSessionKey"):
        SnapshotEnvelope.from_dict(data)

    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data["surfaces"][0]["lastObservedAt"] = data["surfaces"][0]["createdAt"] - 1
    with pytest.raises(ProtocolError, match="timestamps are reversed"):
        SnapshotEnvelope.from_dict(data)

    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data["surfaces"][0]["retiredAt"] = data["surfaces"][0]["lastObservedAt"] + 1
    with pytest.raises(ProtocolError, match="outside the observation lifetime"):
        SnapshotEnvelope.from_dict(data)

    data = json.loads((FIXTURES / "snapshot.json").read_text())
    data["surfaces"][0]["retiredAt"] = data["surfaces"][0]["lastObservedAt"]
    with pytest.raises(ProtocolError, match="retired surface is still bound"):
        SnapshotEnvelope.from_dict(data)


def test_error_fixture_has_stable_routing_fields() -> None:
    error = ErrorEnvelope.from_json((FIXTURES / "error.json").read_bytes()).error
    assert error.code == "provider_unavailable"
    assert error.scope is ErrorScope.PROVIDER
    assert error.host_id == HOST
    assert error.retryable
    assert error.details == {"capability": "provider.claude.agent_view"}


def test_error_routing_identity_must_agree_with_session_key() -> None:
    data = json.loads((FIXTURES / "error.json").read_text())
    data["error"]["sessionKey"] = SESSION_KEY
    data["error"]["provider"] = "claude"
    with pytest.raises(ProtocolError, match="session/provider routing"):
        ErrorEnvelope.from_dict(data)

    data["error"]["provider"] = "codex"
    data["error"]["hostId"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    with pytest.raises(ProtocolError, match="session/host routing"):
        ErrorEnvelope.from_dict(data)


def test_direct_envelope_producers_cannot_emit_parser_invalid_records() -> None:
    unavailable = Capability(
        provider=ProviderId.CODEX,
        available=False,
        provider_version=None,
        tested_contract_min="1",
        tested_contract_max="1",
        features=(),
    )
    with pytest.raises(ProtocolError, match="must explain"):
        CapabilityEnvelope(unavailable).to_json()

    mismatched = ErrorRecord(
        "route_mismatch",
        "The routing fields disagree.",
        ErrorScope.SESSION,
        False,
        1,
        host_id=HostId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        provider=ProviderId.CLAUDE,
        session_key=SessionKey.parse(SESSION_KEY),
    )
    with pytest.raises(ProtocolError, match="routing fields disagree"):
        ErrorEnvelope(mismatched).to_json()

    unsafe_details = ErrorRecord(
        "unsafe_details",
        "The retained details are not safe.",
        ErrorScope.HOST,
        False,
        1,
        details={"accessKey": "must-not-serialize"},
    )
    with pytest.raises(ProtocolError, match="forbidden"):
        ErrorEnvelope(unsafe_details).to_json()

    invalid_fingerprint = Capability(
        provider=ProviderId.CODEX,
        available=True,
        provider_version=None,
        tested_contract_min="1",
        tested_contract_max="1",
        features=(),
        schema_fingerprint="not-a-digest",
    )
    with pytest.raises(ProtocolError, match="lowercase SHA-256"):
        CapabilityEnvelope(invalid_fingerprint).to_json()


def test_switch_plan_allows_waiting_surface_lease_and_context_validation() -> None:
    plan = PresentationPlanEnvelope.from_json(
        (FIXTURES / "presentation-plan.json").read_bytes()
    ).plan
    assert plan.kind is PresentationPlanKind.SWITCH
    assert plan.lease_expires_at == 1784142030000
    plan.validate_for_context(PresentationContext(True, "/dev/pts/7", False, False))
    with pytest.raises(ProtocolError, match="revalidate"):
        plan.validate_for_context(PresentationContext(True, "/dev/pts/8", False, False))
    plan.validate_for_context(PresentationContext(False, None, True, True))


def test_focus_attach_and_blocked_plan_shapes() -> None:
    focus = PresentationPlan(
        PresentationPlanKind.FOCUS,
        HOST,
        surface_id=SURFACE,
        desktop_token="switchboard:surface",
    )
    focus.validate_for_context(PresentationContext(False, None, True, False))
    with pytest.raises(ProtocolError, match="focus"):
        focus.validate_for_context(PresentationContext(True, None, False, False))

    attach = PresentationPlan(
        PresentationPlanKind.ATTACH,
        HOST,
        surface_id=SURFACE,
        workspace_id="workspace",
        tmux_target="workspace:@1.%1",
        desktop_token="switchboard:surface",
        lease_expires_at=123,
    )
    attach.validate_for_context(PresentationContext(True, None, False, False))
    with pytest.raises(ProtocolError, match="attach"):
        attach.validate_for_context(PresentationContext(False, None, False, False))

    error = ErrorRecord("unsafe_handoff", "No safe route.", ErrorScope.LAUNCH, False, 1)
    blocked = PresentationPlan(PresentationPlanKind.BLOCKED, HOST, error=error)
    blocked.validate_for_context(PresentationContext(False, None, False, False))


def test_session_stop_action_is_versioned_and_fail_closed() -> None:
    stopped = SessionActionEnvelope(
        SessionAction(
            SessionActionStatus.STOPPED,
            HOST,
            CLAUDE_SESSION_KEY,
        )
    )
    parsed = SessionActionEnvelope.from_json(stopped.to_json())
    assert parsed == stopped
    assert parsed.to_dict()["action"] == {
        "kind": "stop",
        "status": "stopped",
        "hostId": str(HOST),
        "sessionKey": str(CLAUDE_SESSION_KEY),
    }

    error = ErrorRecord(
        "surface_not_owned",
        "The surface is unmanaged.",
        ErrorScope.SESSION,
        False,
        1,
        host_id=HOST,
        provider=ProviderId.CLAUDE,
        session_key=CLAUDE_SESSION_KEY,
    )
    blocked = SessionAction(
        SessionActionStatus.BLOCKED,
        HOST,
        CLAUDE_SESSION_KEY,
        error,
    )
    assert (
        SessionActionEnvelope.from_json(
            SessionActionEnvelope(blocked).to_json()
        ).action.error
        == error
    )
    with pytest.raises(ProtocolError, match="requires an error"):
        SessionAction(
            SessionActionStatus.BLOCKED,
            HOST,
            CLAUDE_SESSION_KEY,
        )
    with pytest.raises(ProtocolError, match="routing disagrees"):
        SessionAction(
            SessionActionStatus.BLOCKED,
            HOST,
            CLAUDE_SESSION_KEY,
            ErrorRecord(
                "surface_not_owned",
                "The surface is unmanaged.",
                ErrorScope.SESSION,
                False,
                1,
                host_id=HostId("99999999-9999-4999-8999-999999999999"),
            ),
        )


def test_plan_rejects_non_applicable_fields() -> None:
    with pytest.raises(ProtocolError, match="non-applicable"):
        PresentationPlan(
            PresentationPlanKind.FOCUS,
            HOST,
            surface_id=SURFACE,
            desktop_token="desktop",
            tmux_target="not-applicable",
        )
    with pytest.raises(ProtocolError, match="cannot contain surface"):
        PresentationPlan(
            PresentationPlanKind.BLOCKED,
            HOST,
            surface_id=SURFACE,
            error=ErrorRecord("blocked", "Blocked.", ErrorScope.LAUNCH, False, 1),
        )
    with pytest.raises(ProtocolError, match="cannot contain surface"):
        PresentationPlan(
            PresentationPlanKind.BLOCKED,
            HOST,
            lease_expires_at=0,
            error=ErrorRecord("blocked", "Blocked.", ErrorScope.LAUNCH, False, 1),
        )


def test_non_json_or_non_finite_details_are_rejected() -> None:
    data = json.loads((FIXTURES / "error.json").read_text())
    data["error"]["details"] = {"latency": float("nan")}
    with pytest.raises(ProtocolError, match="non-finite"):
        ErrorEnvelope.from_dict(data)
