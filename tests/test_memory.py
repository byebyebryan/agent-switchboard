from __future__ import annotations

import sys
import time

from agent_switchboard.config import MemoryConfig
from agent_switchboard.memory import search_memory

FAKE_MCP = r"""
import json, os, sys
for raw in sys.stdin:
    request = json.loads(raw)
    if request.get("id") == 1:
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "serverInfo": {"name": "fake", "version": "1"},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}), flush=True)
    elif request.get("id") == 2:
        arguments = request["params"]["arguments"]
        leaked = any(key.startswith("AGENT_SWITCHBOARD_") for key in os.environ)
        text = json.dumps({"arguments": arguments, "capabilityLeaked": leaked})
        result = {"content": [{"type": "text", "text": text}], "isError": False}
        print(json.dumps({"jsonrpc": "2.0", "id": 2, "result": result}), flush=True)
"""


def test_memory_adapter_uses_stdio_mcp_and_strips_switchboard_capabilities() -> None:
    result = search_memory(
        MemoryConfig(
            enabled=True,
            command=(sys.executable, "-c", FAKE_MCP),
            timeout_seconds=2,
        ),
        query="alignment",
        project="Switchboard",
        limit=7,
        environment={
            "PATH": "/usr/bin",
            "AGENT_SWITCHBOARD_CAPABILITY": "do-not-forward",
            "AGENT_SWITCHBOARD_LAUNCH_ID": "do-not-forward",
        },
    )

    assert result.available is True
    assert '"capabilityLeaked": false' in result.text
    assert '"project": "Switchboard"' in result.text
    assert '"limit": 7' in result.text


def test_memory_adapter_fails_closed_without_exposing_process_details() -> None:
    result = search_memory(
        MemoryConfig(
            enabled=True,
            command=("/definitely/missing/agent-switchboard-memory",),
            timeout_seconds=1,
        ),
        query="alignment",
        project="Switchboard",
        limit=5,
    )

    assert result.available is False
    assert result.text == ""
    assert result.issues == (
        {
            "code": "memory_unavailable",
            "path": "memory",
            "message": "The optional memory adapter is unavailable.",
        },
    )
    assert "missing" not in str(result.issues)


def test_memory_adapter_bounds_text_and_fails_closed_on_timeout() -> None:
    large_server = FAKE_MCP.replace(
        'text = json.dumps({"arguments": arguments, "capabilityLeaked": leaked})',
        'text = "x" * 70000',
    )
    large = search_memory(
        MemoryConfig(
            enabled=True,
            command=(sys.executable, "-c", large_server),
            timeout_seconds=2,
        ),
        query="alignment",
        project="Switchboard",
        limit=5,
    )
    assert large.available is True
    assert large.truncated is True
    assert len(large.text.encode("utf-8")) == 64 * 1024

    unsafe_server = FAKE_MCP.replace(
        'text = json.dumps({"arguments": arguments, "capabilityLeaked": leaked})',
        'text = "unsafe\\x00text"',
    )
    unsafe = search_memory(
        MemoryConfig(
            enabled=True,
            command=(sys.executable, "-c", unsafe_server),
            timeout_seconds=2,
        ),
        query="alignment",
        project="Switchboard",
        limit=5,
    )
    assert unsafe.available is False
    assert unsafe.issues[0]["code"] == "memory_unavailable"

    started = time.monotonic()
    slow = search_memory(
        MemoryConfig(
            enabled=True,
            command=(sys.executable, "-c", "import time; time.sleep(5)"),
            timeout_seconds=1,
        ),
        query="alignment",
        project="Switchboard",
        limit=5,
    )
    assert slow.available is False
    assert slow.issues[0]["code"] == "memory_unavailable"
    assert time.monotonic() - started < 2.5
