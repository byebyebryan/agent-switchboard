from __future__ import annotations

import io
import json
from dataclasses import dataclass

from agent_switchboard.mcp_server import MAX_MCP_LINE_BYTES, TOOLS, run_mcp_server


@dataclass
class Envelope:
    value: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return self.value

    def to_json(self) -> str:
        return json.dumps(self.value, separators=(",", ":"), sort_keys=True)


class FakeService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[object, ...]] = []

    def current(self) -> Envelope:
        if self.fail:
            raise RuntimeError("private provider detail")
        self.calls.append(("current",))
        return Envelope({"kind": "current"})

    def context(self) -> Envelope:
        self.calls.append(("context",))
        return Envelope({"kind": "context"})

    def list_sessions(self) -> Envelope:
        self.calls.append(("sessions",))
        return Envelope({"kind": "sessions"})

    def session_detail(self, key: str, *, handoff_limit: int) -> Envelope:
        self.calls.append(("detail", key, handoff_limit))
        return Envelope({"kind": "detail"})

    def handoff(self, handoff_id: str) -> Envelope:
        self.calls.append(("handoff", handoff_id))
        return Envelope({"kind": "handoff"})

    def search(self, query: str, *, limit: int) -> Envelope:
        self.calls.append(("search", query, limit))
        return Envelope({"kind": "search"})

    def memory_search(self, query: str, *, limit: int) -> Envelope:
        self.calls.append(("memory", query, limit))
        return Envelope({"kind": "memory"})

    def set_name(self, value: str | None) -> Envelope:
        self.calls.append(("name", value))
        return Envelope({"kind": "name"})

    def append_handoff(
        self,
        *,
        summary: str,
        next_action: str,
        handoff_id: str | None,
        wrap: bool,
    ) -> Envelope:
        self.calls.append(("append", summary, next_action, handoff_id, wrap))
        return Envelope({"kind": "wrap" if wrap else "handoff"})


def frame(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def run(service: FakeService, *messages: object) -> tuple[int, list[dict[str, object]]]:
    source = io.BytesIO(b"".join(frame(message) for message in messages))
    output = io.BytesIO()
    status = run_mcp_server(service, source, output)  # type: ignore[arg-type]
    return status, [json.loads(line) for line in output.getvalue().splitlines()]


def initialized_messages(*after: object) -> tuple[object, ...]:
    return (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        *after,
    )


def test_mcp_lifecycle_lists_exact_tools_and_returns_structured_content() -> None:
    service = FakeService()
    status, responses = run(
        service,
        *initialized_messages(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "project_get_current", "arguments": {}},
            },
        ),
    )

    assert status == 0
    assert responses[0]["result"]["protocolVersion"] == "2025-11-25"  # type: ignore[index]
    listed = responses[1]["result"]["tools"]  # type: ignore[index]
    assert [tool["name"] for tool in listed] == [tool[0] for tool in TOOLS]
    result = responses[2]["result"]
    assert result["structuredContent"] == {"kind": "current"}  # type: ignore[index]
    assert result["content"] == [  # type: ignore[index]
        {"type": "text", "text": '{"kind":"current"}'}
    ]
    assert service.calls == [("current",)]


def test_mcp_rejects_preinit_reused_ids_batches_and_invalid_arguments() -> None:
    service = FakeService()
    status, responses = run(
        service,
        {"jsonrpc": "2.0", "id": 8, "method": "tools/list"},
        *initialized_messages(
            {"jsonrpc": "2.0", "id": 9, "method": "ping"},
            {"jsonrpc": "2.0", "id": 9, "method": "ping"},
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "session_search",
                    "arguments": {"query": "x", "limit": 21},
                },
            },
        ),
    )

    assert status == 0
    assert responses[0]["error"]["code"] == -32002  # type: ignore[index]
    assert responses[3]["error"]["code"] == -32600  # type: ignore[index]
    assert responses[4]["error"]["code"] == -32602  # type: ignore[index]

    status, batch = run(service, [])
    assert status == 0
    assert batch[0]["error"]["code"] == -32600  # type: ignore[index]


def test_mcp_tool_failure_is_safe_in_band_error() -> None:
    status, responses = run(
        FakeService(fail=True),
        *initialized_messages(
            {
                "jsonrpc": "2.0",
                "id": "call",
                "method": "tools/call",
                "params": {"name": "project_get_current", "arguments": {}},
            }
        ),
    )

    assert status == 0
    result = responses[1]["result"]
    assert result["isError"] is True  # type: ignore[index]
    assert "private provider detail" not in json.dumps(responses)


def test_mcp_oversized_line_fails_and_stops() -> None:
    output = io.BytesIO()
    status = run_mcp_server(  # type: ignore[arg-type]
        FakeService(),
        io.BytesIO(b"x" * (MAX_MCP_LINE_BYTES + 1) + b"\n"),
        output,
    )

    assert status == 1
    response = json.loads(output.getvalue())
    assert response["error"]["code"] == -32700
