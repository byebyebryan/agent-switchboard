"""Dependency-free, session-authorized stdio MCP server."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import BinaryIO, Final

from .agent_tools import AgentToolService

MCP_PROTOCOL_VERSION: Final = "2025-11-25"
SUPPORTED_MCP_PROTOCOL_VERSIONS: Final = (
    MCP_PROTOCOL_VERSION,
    "2025-06-18",
    "2025-03-26",
)
MAX_MCP_LINE_BYTES: Final = 1024 * 1024


class McpProtocolError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _schema(
    properties: Mapping[str, object] | None = None,
    *,
    required: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required),
        "additionalProperties": False,
    }


_STRING = {"type": "string", "minLength": 1}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 20}
_HANDOFF_LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}

TOOLS: Final = (
    ("project_get_current", "Read the exactly authorized current session.", _schema()),
    (
        "project_get_context",
        "Read bounded configured context and recent project state.",
        _schema(),
    ),
    (
        "project_list_tasks",
        "List bounded tasks in the current project.",
        _schema(),
    ),
    (
        "task_get",
        "Read the current task and its session history.",
        _schema(),
    ),
    (
        "task_get_handoff",
        "Read one exact handoff in the current project.",
        _schema({"handoffId": _STRING}, required=("handoffId",)),
    ),
    (
        "task_list_handoffs",
        "Read bounded handoffs across the current task history.",
        _schema({"limit": _HANDOFF_LIMIT}),
    ),
    (
        "task_search",
        "Search curated Switchboard metadata and handoffs in the current project.",
        _schema({"query": _STRING, "limit": _LIMIT}, required=("query",)),
    ),
    (
        "memory_search",
        "Search an explicitly configured optional memory MCP adapter.",
        _schema({"query": _STRING, "limit": _LIMIT}, required=("query",)),
    ),
    (
        "task_update",
        "Update only the current task title, purpose, or pin.",
        _schema(
            {
                "title": _STRING,
                "purpose": {"type": ["string", "null"]},
                "pinned": {"type": "boolean"},
            }
        ),
    ),
    (
        "task_set_handoff",
        "Append an agent-attributed handoff to the authorized current session.",
        _schema(
            {"summary": _STRING, "nextAction": _STRING, "handoffId": _STRING},
            required=("summary", "nextAction"),
        ),
    ),
    (
        "task_close",
        "Append a handoff, wrap the current session, and close its task.",
        _schema(
            {"summary": _STRING, "nextAction": _STRING, "handoffId": _STRING},
            required=("summary", "nextAction"),
        ),
    ),
)


def _tool_records() -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": schema,
            "annotations": {
                "readOnlyHint": name
                not in {"task_update", "task_set_handoff", "task_close"},
                "destructiveHint": False,
                "idempotentHint": name == "task_update",
                "openWorldHint": name == "memory_search",
            },
        }
        for name, description, schema in TOOLS
    ]


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise McpProtocolError(-32602, f"{name} must be an object")
    return value


def _arguments(
    params: object, allowed: set[str], required: set[str]
) -> dict[str, object]:
    table = _object(params, "params")
    if set(table) - {"name", "arguments"}:
        raise McpProtocolError(-32602, "params contains unknown fields")
    if table.get("name") is None:
        raise McpProtocolError(-32602, "params.name is required")
    arguments = _object(table.get("arguments", {}), "params.arguments")
    unknown = set(arguments) - allowed
    missing = required - set(arguments)
    if unknown or missing:
        raise McpProtocolError(-32602, "tool arguments do not match the schema")
    return arguments


def _text(
    arguments: Mapping[str, object], name: str, *, optional: bool = False
) -> str | None:
    value = arguments.get(name)
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value:
        raise McpProtocolError(-32602, f"{name} must be a non-empty string")
    return value


def _limit(
    arguments: Mapping[str, object], name: str, default: int, maximum: int
) -> int:
    value = arguments.get(name, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise McpProtocolError(-32602, f"{name} must be between 1 and {maximum}")
    return value


def _call(
    service: AgentToolService, name: str, raw_params: object
) -> dict[str, object]:
    if name in {"project_get_current", "project_get_context", "project_list_tasks"}:
        arguments = _arguments(raw_params, set(), set())
        assert not arguments
        if name == "project_get_current":
            envelope = service.current()
        elif name == "project_get_context":
            envelope = service.context()
        else:
            envelope = service.list_tasks()
    elif name == "task_get":
        arguments = _arguments(raw_params, set(), set())
        assert not arguments
        envelope = service.task()
    elif name == "task_list_handoffs":
        arguments = _arguments(raw_params, {"limit"}, set())
        envelope = service.list_task_handoffs(limit=_limit(arguments, "limit", 20, 100))
    elif name == "task_get_handoff":
        arguments = _arguments(raw_params, {"handoffId"}, {"handoffId"})
        envelope = service.handoff(str(_text(arguments, "handoffId")))
    elif name in {"task_search", "memory_search"}:
        arguments = _arguments(raw_params, {"query", "limit"}, {"query"})
        operation: Callable[..., object] = (
            service.search if name == "task_search" else service.memory_search
        )
        envelope = operation(
            str(_text(arguments, "query")), limit=_limit(arguments, "limit", 20, 20)
        )
    elif name == "task_update":
        arguments = _arguments(raw_params, {"title", "purpose", "pinned"}, set())
        if not arguments:
            raise McpProtocolError(-32602, "task_update requires one field")
        if "title" in arguments and not isinstance(arguments["title"], str):
            raise McpProtocolError(-32602, "title must be a string")
        if (
            "purpose" in arguments
            and arguments["purpose"] is not None
            and not isinstance(arguments["purpose"], str)
        ):
            raise McpProtocolError(-32602, "purpose must be a string or null")
        if "pinned" in arguments and not isinstance(arguments["pinned"], bool):
            raise McpProtocolError(-32602, "pinned must be boolean")
        envelope = service.update_task(arguments)
    elif name in {"task_set_handoff", "task_close"}:
        arguments = _arguments(
            raw_params,
            {"summary", "nextAction", "handoffId"},
            {"summary", "nextAction"},
        )
        envelope = service.append_handoff(
            summary=str(_text(arguments, "summary")),
            next_action=str(_text(arguments, "nextAction")),
            handoff_id=_text(arguments, "handoffId", optional=True),
            close=name == "task_close",
        )
    else:
        raise McpProtocolError(-32602, "unknown tool")
    if isinstance(envelope, Mapping):
        structured = dict(envelope)
        text = json.dumps(
            structured,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    else:
        structured = envelope.to_dict()  # type: ignore[attr-defined]
        text = envelope.to_json()  # type: ignore[attr-defined]
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": False,
    }


def _write(stream: BinaryIO, message: object) -> None:
    stream.write(
        json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
    )
    stream.flush()


def _response(identifier: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def _error(identifier: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "error": {"code": code, "message": message},
    }


def run_mcp_server(
    service: AgentToolService, input_stream: BinaryIO, output_stream: BinaryIO
) -> int:
    """Serve newline-delimited JSON-RPC until clean EOF or a framing violation."""

    initialized = False
    ready = False
    seen_ids: set[int | str] = set()
    while True:
        raw = input_stream.readline(MAX_MCP_LINE_BYTES + 1)
        if not raw:
            return 0
        if len(raw) > MAX_MCP_LINE_BYTES or not raw.endswith(b"\n"):
            _write(
                output_stream,
                _error(None, -32700, "invalid or oversized JSON-RPC line"),
            )
            return 1
        try:
            message = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            _write(output_stream, _error(None, -32700, "invalid JSON"))
            continue
        if isinstance(message, list) or not isinstance(message, dict):
            _write(
                output_stream, _error(None, -32600, "batch requests are not supported")
            )
            continue
        identifier = message.get("id")
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            _write(output_stream, _error(identifier, -32600, "invalid request"))
            continue
        is_notification = "id" not in message
        if not is_notification:
            if isinstance(identifier, bool) or not isinstance(identifier, (int, str)):
                _write(output_stream, _error(None, -32600, "request id is invalid"))
                continue
            if identifier in seen_ids:
                _write(
                    output_stream, _error(identifier, -32600, "request id was reused")
                )
                continue
            seen_ids.add(identifier)
        try:
            if method == "initialize":
                if is_notification or initialized:
                    raise McpProtocolError(-32600, "initialize request is invalid")
                params = _object(message.get("params", {}), "params")
                requested = params.get("protocolVersion")
                selected = (
                    requested
                    if requested in SUPPORTED_MCP_PROTOCOL_VERSIONS
                    else MCP_PROTOCOL_VERSION
                )
                initialized = True
                _write(
                    output_stream,
                    _response(
                        identifier,
                        {
                            "protocolVersion": selected,
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {
                                "name": "agent-switchboard",
                                "version": "0.2.0",
                            },
                        },
                    ),
                )
                continue
            if method == "notifications/initialized" and is_notification:
                if not initialized:
                    continue
                ready = True
                continue
            if is_notification:
                continue
            if not initialized or not ready:
                raise McpProtocolError(-32002, "server is not initialized")
            if method == "ping":
                result: object = {}
            elif method == "tools/list":
                result = {"tools": _tool_records()}
            elif method == "tools/call":
                params = _object(message.get("params", {}), "params")
                name = params.get("name")
                if not isinstance(name, str):
                    raise McpProtocolError(-32602, "params.name is required")
                try:
                    result = _call(service, name, params)
                except McpProtocolError:
                    raise
                except Exception:
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": "Agent Switchboard tool call failed.",
                            }
                        ],
                        "isError": True,
                    }
            else:
                raise McpProtocolError(-32601, "method not found")
            _write(output_stream, _response(identifier, result))
        except McpProtocolError as error:
            if not is_notification:
                _write(output_stream, _error(identifier, error.code, str(error)))


__all__ = [
    "MAX_MCP_LINE_BYTES",
    "MCP_PROTOCOL_VERSION",
    "SUPPORTED_MCP_PROTOCOL_VERSIONS",
    "TOOLS",
    "run_mcp_server",
]
