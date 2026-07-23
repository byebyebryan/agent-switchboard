from __future__ import annotations

import io
import json

import pytest

from agent_switchboard._v3 import __version__
from agent_switchboard._v3.agent_mcp import (
    MCP_PROTOCOL_VERSION,
    run_mcp_server,
)


def _initialize(protocol_version: object) -> dict[str, object]:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": protocol_version},
    }
    source = io.BytesIO(json.dumps(request).encode() + b"\n")
    output = io.BytesIO()
    assert run_mcp_server(object(), source, output) == 0  # type: ignore[arg-type]
    return json.loads(output.getvalue())


@pytest.mark.parametrize("requested", ["2025-06-18", "2025-11-25"])
def test_initialize_echoes_supported_client_protocol(requested: str) -> None:
    response = _initialize(requested)
    result = response["result"]
    assert isinstance(result, dict)
    assert result["protocolVersion"] == requested
    assert result["serverInfo"] == {
        "name": "agent-switchboard-v3",
        "version": __version__,
    }


def test_initialize_offers_latest_version_when_client_protocol_is_unknown() -> None:
    response = _initialize("2099-01-01")
    result = response["result"]
    assert isinstance(result, dict)
    assert result["protocolVersion"] == MCP_PROTOCOL_VERSION


@pytest.mark.parametrize("requested", [None, "", 20250618])
def test_initialize_rejects_missing_or_invalid_protocol(requested: object) -> None:
    response = _initialize(requested)
    assert response["error"] == {
        "code": -32602,
        "message": "params.protocolVersion is required",
    }
