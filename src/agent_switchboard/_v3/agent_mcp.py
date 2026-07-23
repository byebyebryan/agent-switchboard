"""Capability-bound replacement MCP tools for Phase 6D frames."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from time import time
from typing import BinaryIO, Final

from . import __version__
from .domain import ProviderId, RequestId, TransitionId, ViewMode
from .views import ViewRuntime
from .workflow import WorkflowRuntime

MCP_PROTOCOL_VERSION: Final = "2025-11-25"
MCP_PROTOCOL_VERSIONS: Final = (MCP_PROTOCOL_VERSION, "2025-06-18")
MAX_MCP_LINE_BYTES: Final = 1024 * 1024


class McpError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(message)


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
_PARK_SAFE = {"type": "boolean", "const": True, "default": True}
TOOLS: Final = (
    ("switchboard_current", "Read exact current frame authority.", _schema()),
    ("switchboard_context", "Read current checkout ownership.", _schema()),
    ("switchboard_history", "Read bounded frame session history.", _schema()),
    (
        "switchboard_mode",
        "Switch the current managed view between navigator and direct mode.",
        _schema({"mode": {"enum": ["navigator", "direct"]}}, required=("mode",)),
    ),
    (
        "task_push",
        "Prepare one conservative child task after this turn stops.",
        _schema(
            {
                "title": _STRING,
                "brief": _STRING,
                "purpose": {"type": "string"},
                "provider": {"enum": ["codex", "claude"]},
                "park_safe": _PARK_SAFE,
                "request_id": _STRING,
            },
            required=("title", "brief"),
        ),
    ),
    (
        "task_back",
        "Prepare a model-free return to the exact parent frame.",
        _schema({"park_safe": _PARK_SAFE, "request_id": _STRING}),
    ),
    (
        "task_complete_return",
        "Prepare one immutable handoff and return to the parent.",
        _schema(
            {
                "summary": _STRING,
                "next_action": _STRING,
                "park_safe": _PARK_SAFE,
                "request_id": _STRING,
            },
            required=("summary", "next_action"),
        ),
    ),
    (
        "transition_claim",
        "Claim the exact pending brief or completion handoff.",
        _schema(),
    ),
    (
        "transition_status",
        "Read the current view transition without changing it.",
        _schema(),
    ),
    (
        "transition_cancel",
        "Cancel only the exact prepared zero-turn push.",
        _schema({"transition_id": _STRING}, required=("transition_id",)),
    ),
)


def _records() -> list[dict[str, object]]:
    mutating = {
        "task_push",
        "task_back",
        "task_complete_return",
        "transition_claim",
        "transition_cancel",
        "switchboard_mode",
    }
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": schema,
            "annotations": {
                "readOnlyHint": name not in mutating,
                "destructiveHint": name == "transition_cancel",
                "idempotentHint": name == "transition_claim",
                "openWorldHint": False,
            },
        }
        for name, description, schema in TOOLS
    ]


class AgentToolService:
    def __init__(
        self,
        workflow: WorkflowRuntime,
        raw_capability: str,
        *,
        now: int | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if now is not None and clock is not None:
            raise ValueError("now and clock are mutually exclusive")
        self.workflow = workflow
        self.raw_capability = raw_capability
        self._clock = (
            clock
            if clock is not None
            else (lambda: int(time() * 1_000))
            if now is None
            else (lambda: now)
        )

    @property
    def now(self) -> int:
        """Read time per tool call; an MCP server may outlive many transitions."""

        return self._clock()

    def _capability(self):
        return self.workflow.registry.validate_capability(
            self.raw_capability, now=self.now
        )

    def current(self) -> dict[str, object]:
        capability = self._capability()
        frame = self.workflow.registry.get_frame(capability.frame_id)
        return {
            "frameId": str(frame.frame_id),
            "role": frame.role.value,
            "parentFrameId": (
                None if frame.parent_frame_id is None else str(frame.parent_frame_id)
            ),
            "title": frame.title,
            "purpose": frame.purpose,
            "provider": capability.session_key.provider.value,
            "providerSessionId": str(capability.session_key.provider_session_id),
            "viewId": str(capability.view_id),
        }

    def context(self) -> dict[str, object]:
        capability = self._capability()
        frame = self.workflow.registry.get_frame(capability.frame_id)
        context = self.workflow.registry.get_work_context(frame.work_context_id)
        return {
            "projectId": str(frame.project_id),
            "checkoutId": str(context.checkout_id),
            "checkoutPath": str(
                self.workflow.registry.checkout_path(context.checkout_id)
            ),
            "claimGeneration": context.claim_generation,
            "foreground": context.foreground_frame_id == frame.frame_id,
            "backgroundState": context.background_state.value,
        }

    def history(self) -> dict[str, object]:
        capability = self._capability()
        rows = self.workflow.registry.connection.execute(
            "SELECT ordinal, session_key, membership_reason, joined_at "
            "FROM frame_sessions WHERE frame_id = ? ORDER BY ordinal DESC LIMIT 20",
            (str(capability.frame_id),),
        ).fetchall()
        return {
            "sessions": [
                {
                    "ordinal": int(row["ordinal"]),
                    "sessionKey": str(row["session_key"]),
                    "reason": str(row["membership_reason"]),
                    "joinedAt": int(row["joined_at"]),
                }
                for row in rows
            ]
        }

    def transition_status(self) -> dict[str, object]:
        capability = self._capability()
        transition = self.workflow.registry.nonterminal_transition_for_view(
            capability.view_id
        )
        if transition is None:
            return {"transition": None}
        control = self.workflow.registry.control_turn_for_transition(
            transition.transition_id
        )
        return {
            "transition": {
                "transitionId": str(transition.transition_id),
                "kind": transition.kind.value,
                "state": transition.state.value,
                "sourceFrameId": (
                    None
                    if transition.source_frame_id is None
                    else str(transition.source_frame_id)
                ),
                "targetFrameId": str(transition.target_frame_id),
                "transportPhase": transition.transport_phase.value,
                "controlState": None if control is None else control.state.value,
                "submissionCount": (
                    None if control is None else control.submission_count
                ),
            }
        }


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise McpError(-32602, f"{field} must be an object")
    return value


def _arguments(
    params: object, allowed: set[str], required: set[str]
) -> dict[str, object]:
    table = _object(params, "params")
    if set(table) - {"name", "arguments", "_meta"}:
        raise McpError(-32602, "params contains unknown fields")
    if "_meta" in table:
        _object(table["_meta"], "params._meta")
    arguments = _object(table.get("arguments", {}), "params.arguments")
    if set(arguments) - allowed or required - set(arguments):
        raise McpError(-32602, "tool arguments do not match schema")
    return arguments


def _text(arguments: Mapping[str, object], field: str) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or not value:
        raise McpError(-32602, f"{field} must be a non-empty string")
    return value


def _optional_text(arguments: Mapping[str, object], field: str) -> str | None:
    value = arguments.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise McpError(-32602, f"{field} must be a non-empty string")
    return value


def _request(arguments: Mapping[str, object]) -> RequestId | None:
    value = _optional_text(arguments, "request_id")
    if value is None:
        return None
    try:
        return RequestId(value)
    except ValueError as error:
        raise McpError(-32602, "request_id must be a UUID") from error


def _park_safe(arguments: Mapping[str, object]) -> bool:
    value = arguments.get("park_safe", True)
    if value is not True:
        raise McpError(-32602, "park_safe must be true")
    return True


def _call(
    service: AgentToolService, name: str, raw_params: object
) -> dict[str, object]:
    if name in {
        "switchboard_current",
        "switchboard_context",
        "switchboard_history",
        "transition_claim",
        "transition_status",
    }:
        assert not _arguments(raw_params, set(), set())
        if name == "switchboard_current":
            result = service.current()
        elif name == "switchboard_context":
            result = service.context()
        elif name == "switchboard_history":
            result = service.history()
        elif name == "transition_status":
            result = service.transition_status()
        else:
            claim = service.workflow.claim(service.raw_capability, now=service.now)
            result = {
                "kind": claim.kind,
                "transitionId": str(claim.transition_id),
                "targetFrameId": str(claim.target_frame_id),
                "brief": claim.brief,
                "summary": claim.summary,
                "nextAction": claim.next_action,
            }
    elif name == "task_push":
        args = _arguments(
            raw_params,
            {"title", "brief", "purpose", "provider", "park_safe", "request_id"},
            {"title", "brief"},
        )
        provider = _optional_text(args, "provider")
        try:
            selected = None if provider is None else ProviderId(provider)
        except ValueError as error:
            raise McpError(-32602, "provider is invalid") from error
        prepared = service.workflow.task_push(
            service.raw_capability,
            title=_text(args, "title"),
            brief=_text(args, "brief"),
            purpose=_optional_text(args, "purpose"),
            provider=selected,
            park_safe=_park_safe(args),
            request_id=_request(args),
            now=service.now,
        )
        result = _prepared_result(prepared)
    elif name == "switchboard_mode":
        args = _arguments(raw_params, {"mode"}, {"mode"})
        try:
            target_mode = ViewMode(_text(args, "mode"))
        except ValueError as error:
            raise McpError(-32602, "mode is invalid") from error
        capability = service._capability()
        view = ViewRuntime(
            service.workflow.opened,
            service.workflow.paths,
            tmux=service.workflow.tmux,
        ).set_mode(
            capability.view_id,
            target_mode,
            request_id=RequestId.new(),
            now=service.now,
        )
        result = {
            "viewId": str(view.view_id),
            "mode": view.mode.value,
            "instruction": (
                "The resident navigator is now visible."
                if view.mode is ViewMode.NAVIGATOR
                else "The active agent now fills the managed view."
            ),
        }
    elif name == "task_back":
        args = _arguments(raw_params, {"park_safe", "request_id"}, set())
        result = _prepared_result(
            service.workflow.task_back(
                service.raw_capability,
                park_safe=_park_safe(args),
                request_id=_request(args),
                now=service.now,
            )
        )
    elif name == "task_complete_return":
        args = _arguments(
            raw_params,
            {"summary", "next_action", "park_safe", "request_id"},
            {"summary", "next_action"},
        )
        result = _prepared_result(
            service.workflow.task_complete_return(
                service.raw_capability,
                summary=_text(args, "summary"),
                next_action=_text(args, "next_action"),
                park_safe=_park_safe(args),
                request_id=_request(args),
                now=service.now,
            )
        )
    elif name == "transition_cancel":
        args = _arguments(raw_params, {"transition_id"}, {"transition_id"})
        try:
            transition_id = TransitionId(_text(args, "transition_id"))
        except ValueError as error:
            raise McpError(-32602, "transition_id must be a UUID") from error
        service.workflow.cancel_push(
            service.raw_capability, transition_id, now=service.now
        )
        result = {"cancelled": True, "transitionId": str(transition_id)}
    else:
        raise McpError(-32602, "unknown tool")
    return result


def _prepared_result(prepared) -> dict[str, object]:
    return {
        "transitionId": str(prepared.transition_id),
        "sourceFrameId": str(prepared.source_frame_id),
        "targetFrameId": str(prepared.target_frame_id),
        "state": prepared.state.value,
        "instruction": "Finish this turn; Switchboard will transition after Stop.",
    }


def _write(stream: BinaryIO, value: object) -> None:
    stream.write(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    )
    stream.flush()


def _negotiate_protocol(params: object) -> str:
    initialize = _object(params, "params")
    requested = initialize.get("protocolVersion")
    if not isinstance(requested, str) or not requested:
        raise McpError(-32602, "params.protocolVersion is required")
    if requested in MCP_PROTOCOL_VERSIONS:
        return requested
    return MCP_PROTOCOL_VERSION


def run_mcp_server(
    service: AgentToolService, input_stream: BinaryIO, output_stream: BinaryIO
) -> int:
    initialized = False
    ready = False
    seen_ids: set[int | str] = set()
    while True:
        raw = input_stream.readline(MAX_MCP_LINE_BYTES + 1)
        if not raw:
            return 0
        if len(raw) > MAX_MCP_LINE_BYTES or not raw.endswith(b"\n"):
            _write(output_stream, _error(None, -32700, "invalid JSON-RPC line"))
            return 1
        try:
            message = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            _write(output_stream, _error(None, -32700, "invalid JSON"))
            continue
        if not isinstance(message, dict) or isinstance(message, list):
            _write(output_stream, _error(None, -32600, "invalid request"))
            continue
        identifier = message.get("id")
        method = message.get("method")
        notification = "id" not in message
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            _write(output_stream, _error(identifier, -32600, "invalid request"))
            continue
        if not notification:
            if isinstance(identifier, bool) or not isinstance(identifier, (int, str)):
                _write(output_stream, _error(None, -32600, "invalid request id"))
                continue
            if identifier in seen_ids:
                _write(output_stream, _error(identifier, -32600, "request id reused"))
                continue
            seen_ids.add(identifier)
        try:
            if method == "initialize":
                if notification or initialized:
                    raise McpError(-32600, "initialize request is invalid")
                protocol_version = _negotiate_protocol(message.get("params", {}))
                initialized = True
                _write(
                    output_stream,
                    _response(
                        identifier,
                        {
                            "protocolVersion": protocol_version,
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {
                                "name": "agent-switchboard-v3",
                                "version": __version__,
                            },
                        },
                    ),
                )
                continue
            if method == "notifications/initialized" and notification:
                ready = initialized
                continue
            if notification:
                continue
            if not ready:
                raise McpError(-32002, "server is not initialized")
            if method == "ping":
                result: object = {}
            elif method == "tools/list":
                result = {"tools": _records()}
            elif method == "tools/call":
                params = _object(message.get("params", {}), "params")
                name = params.get("name")
                if not isinstance(name, str):
                    raise McpError(-32602, "params.name is required")
                try:
                    structured = _call(service, name, params)
                    text = json.dumps(
                        structured,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    result = {
                        "content": [{"type": "text", "text": text}],
                        "structuredContent": structured,
                        "isError": False,
                    }
                except McpError:
                    raise
                except Exception:
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": "Switchboard tool call failed.",
                            }
                        ],
                        "isError": True,
                    }
            else:
                raise McpError(-32601, "method not found")
            _write(output_stream, _response(identifier, result))
        except McpError as error:
            if not notification:
                _write(output_stream, _error(identifier, error.code, str(error)))


def _response(identifier: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def _error(identifier: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "error": {"code": code, "message": message},
    }


__all__ = [
    "MAX_MCP_LINE_BYTES",
    "MCP_PROTOCOL_VERSION",
    "MCP_PROTOCOL_VERSIONS",
    "TOOLS",
    "AgentToolService",
    "run_mcp_server",
]
