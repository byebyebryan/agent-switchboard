"""Session-scoped authorization and bounded agent context operations."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from .config import SwitchboardConfig
from .curation import (
    CurationError,
    detail_envelope,
    read_session_detail,
    resolve_current_session_binding,
)
from .domain import (
    HostId,
    LaunchId,
    Project,
    ProjectLocation,
    ProviderId,
    SessionKey,
    SurfaceId,
    ValidationError,
)
from .memory import search_memory
from .protocol import (
    MAX_AGENT_CONTEXT_FILE_BYTES,
    MAX_AGENT_CONTEXT_FILES,
    MAX_AGENT_CONTEXT_ISSUES,
    MAX_AGENT_CONTEXT_SESSIONS,
    MAX_AGENT_CONTEXT_TOTAL_BYTES,
    AgentContextEnvelope,
    AgentHandoffEnvelope,
    AgentMemoryEnvelope,
    AgentSearchEnvelope,
    AgentSessionListEnvelope,
    SessionDetailEnvelope,
)
from .storage import (
    DEFAULT_AGENT_PROJECT_SESSION_LIMIT,
    DEFAULT_AGENT_SEARCH_LIMIT,
    Registry,
)
from .tmux import TmuxController, TmuxError

_CAPABILITY_RE: Final = re.compile(r"[A-Za-z0-9_-]{43,128}")
_AUTHORIZATION_MESSAGE: Final = "agent authorization failed"
_MAX_CONTEXT_TREE_DEPTH: Final = 16
_MAX_CONTEXT_TREE_ENTRIES: Final = 4_096


class AgentToolError(RuntimeError):
    """An agent tool request is unauthorized or cannot be constructed safely."""


@dataclass(frozen=True, slots=True)
class AuthorizedAgent:
    host_id: HostId
    provider: ProviderId
    session_key: SessionKey
    surface_id: SurfaceId
    launch_id: LaunchId


@dataclass(frozen=True, slots=True)
class _SourceRead:
    sources: tuple[dict[str, object], ...]
    truncated: bool
    issues: tuple[dict[str, str], ...]


def _authorization_error() -> AgentToolError:
    return AgentToolError(_AUTHORIZATION_MESSAGE)


def _canonical_environment_id(
    environment: Mapping[str, str], name: str, value_type: type[LaunchId | SurfaceId]
) -> LaunchId | SurfaceId:
    value = environment.get(name)
    if not isinstance(value, str):
        raise _authorization_error()
    try:
        parsed = value_type(value)
    except ValidationError as error:
        raise _authorization_error() from error
    if str(parsed) != value:
        raise _authorization_error()
    return parsed


def authorize_agent(
    registry: Registry,
    *,
    host_id: HostId | str,
    environment: Mapping[str, str] | None = None,
    tmux: TmuxController | None = None,
) -> AuthorizedAgent:
    """Authorize only the exact current managed provider surface and capability."""

    host = host_id if isinstance(host_id, HostId) else HostId(host_id)
    process_environment = os.environ if environment is None else environment
    capability = process_environment.get("AGENT_SWITCHBOARD_CAPABILITY")
    if not isinstance(capability, str) or _CAPABILITY_RE.fullmatch(capability) is None:
        raise _authorization_error()
    launch_id = _canonical_environment_id(
        process_environment, "AGENT_SWITCHBOARD_LAUNCH_ID", LaunchId
    )
    surface_id = _canonical_environment_id(
        process_environment, "AGENT_SWITCHBOARD_SURFACE_ID", SurfaceId
    )
    controller = TmuxController() if tmux is None else tmux
    try:
        binding = resolve_current_session_binding(
            registry,
            host_id=host,
            environment=process_environment,
            tmux=controller,
        )
    except (CurationError, TmuxError, ValidationError) as error:
        raise _authorization_error() from error
    if (
        binding.provider not in {ProviderId.CODEX, ProviderId.CLAUDE}
        or binding.surface_id != surface_id
        or binding.launch_id != str(launch_id)
    ):
        raise _authorization_error()
    launch = registry.get_launch(str(launch_id))
    surface = registry.get_surface(str(surface_id))
    session = registry.get_session(str(binding.session_key))
    if launch is None or surface is None or session is None:
        raise _authorization_error()
    stored_digest = launch.get("agent_capability_hash")
    candidate_digest = hashlib.sha256(capability.encode("ascii")).hexdigest()
    if (
        launch.get("host_id") != str(host)
        or launch.get("provider") != binding.provider.value
        or launch.get("action") not in {"new", "resume"}
        or launch.get("state") != "bound"
        or launch.get("surface_id") != str(surface_id)
        or launch.get("target_session_key") != str(binding.session_key)
        or not isinstance(stored_digest, str)
        or not hmac.compare_digest(stored_digest, candidate_digest)
        or surface.get("launch_id") != str(launch_id)
        or surface.get("current_session_key") != str(binding.session_key)
        or surface.get("binding_confidence") != "confirmed"
        or surface.get("retired_at") is not None
        or session.get("surface_id") != str(surface_id)
        or session.get("provider") != binding.provider.value
    ):
        raise _authorization_error()
    return AuthorizedAgent(
        host,
        binding.provider,
        binding.session_key,
        SurfaceId(str(surface_id)),
        LaunchId(str(launch_id)),
    )


def _issue(issues: list[dict[str, str]], *, code: str, path: str, message: str) -> None:
    record = {"code": code, "path": path, "message": message}
    if len(issues) < MAX_AGENT_CONTEXT_ISSUES:
        issues.append(record)
        return
    issues[-1] = {
        "code": "context_issues_truncated",
        "path": ".",
        "message": "Additional context source issues were omitted.",
    }


def _contains_text_controls(value: str) -> bool:
    return any(
        unicodedata.category(character) == "Cc" and character not in "\n\t"
        for character in value
    )


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _configured_files(
    root: Path,
    context_sources: tuple[str, ...],
    issues: list[dict[str, str]],
) -> tuple[list[tuple[str, Path]], bool]:
    files: list[tuple[str, Path]] = []
    seen: set[str] = set()
    truncated = len(context_sources) > MAX_AGENT_CONTEXT_FILES
    tree_entries = 0
    tree_issue_reported = False
    for configured in context_sources[:MAX_AGENT_CONTEXT_FILES]:
        relative = PurePosixPath(configured)
        candidate = root.joinpath(*relative.parts)
        try:
            candidate.lstat()
        except OSError:
            _issue(
                issues,
                code="context_source_unavailable",
                path=configured,
                message="The configured context source is unavailable.",
            )
            continue
        if candidate.is_symlink():
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                _issue(
                    issues,
                    code="context_source_unavailable",
                    path=configured,
                    message="The configured context source is unavailable.",
                )
                continue
            if not _inside(root, resolved):
                _issue(
                    issues,
                    code="context_source_escape",
                    path=configured,
                    message="The configured context source escapes its location.",
                )
                continue
            if resolved.is_dir():
                _issue(
                    issues,
                    code="context_directory_symlink",
                    path=configured,
                    message="Directory symlinks are not traversed.",
                )
                continue
            if not resolved.is_file():
                _issue(
                    issues,
                    code="context_source_not_regular",
                    path=configured,
                    message="The configured context source is not a regular file.",
                )
                continue
            files.append((configured, resolved))
            seen.add(configured)
            continue
        if candidate.is_file():
            files.append((configured, candidate))
            seen.add(configured)
            continue
        if not candidate.is_dir():
            _issue(
                issues,
                code="context_source_not_regular",
                path=configured,
                message="The configured context source is not a file or directory.",
            )
            continue
        for directory, directory_names, file_names in os.walk(
            candidate, followlinks=False
        ):
            directory_path = Path(directory)
            tree_entries += 1
            if tree_entries > _MAX_CONTEXT_TREE_ENTRIES:
                if not tree_issue_reported:
                    _issue(
                        issues,
                        code="context_tree_truncated",
                        path=configured,
                        message="Directory traversal exceeded its entry limit.",
                    )
                return files, True
            directory_names[:] = sorted(
                name
                for name in directory_names
                if not (directory_path / name).is_symlink()
            )
            depth = len(directory_path.relative_to(candidate).parts)
            if depth >= _MAX_CONTEXT_TREE_DEPTH and directory_names:
                directory_names.clear()
                truncated = True
                if not tree_issue_reported:
                    _issue(
                        issues,
                        code="context_tree_truncated",
                        path=configured,
                        message="Directory traversal exceeded its depth limit.",
                    )
                    tree_issue_reported = True
            for name in sorted(file_names):
                tree_entries += 1
                if tree_entries > _MAX_CONTEXT_TREE_ENTRIES:
                    if not tree_issue_reported:
                        _issue(
                            issues,
                            code="context_tree_truncated",
                            path=configured,
                            message="Directory traversal exceeded its entry limit.",
                        )
                    return files, True
                lexical_file = directory_path / name
                relative_name = lexical_file.relative_to(root).as_posix()
                if relative_name in seen:
                    continue
                try:
                    resolved_file = lexical_file.resolve(strict=True)
                except OSError:
                    _issue(
                        issues,
                        code="context_source_unavailable",
                        path=relative_name,
                        message="A context file is unavailable.",
                    )
                    continue
                if not _inside(root, resolved_file):
                    _issue(
                        issues,
                        code="context_source_escape",
                        path=relative_name,
                        message="A context file escapes its location.",
                    )
                    continue
                if not resolved_file.is_file():
                    continue
                files.append((relative_name, resolved_file))
                seen.add(relative_name)
                if len(files) >= MAX_AGENT_CONTEXT_FILES:
                    return files, True
    return files, truncated


def _decode_bounded_text(data: bytes, *, truncated: bool) -> str:
    candidate = data
    while True:
        try:
            text = candidate.decode("utf-8")
            break
        except UnicodeDecodeError as error:
            if truncated and error.reason == "unexpected end of data":
                candidate = candidate[: error.start]
                continue
            raise
    if _contains_text_controls(text):
        raise ValueError("context file contains terminal controls")
    return text


def _read_context_sources(
    *, root: Path, context_sources: tuple[str, ...]
) -> _SourceRead:
    issues: list[dict[str, str]] = []
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise AgentToolError(
            "the configured project location is unavailable"
        ) from error
    if not resolved_root.is_dir():
        raise AgentToolError("the configured project location is unavailable")
    files, truncated = _configured_files(resolved_root, context_sources, issues)
    sources: list[dict[str, object]] = []
    remaining = MAX_AGENT_CONTEXT_TOTAL_BYTES
    for relative, path in sorted(files, key=lambda item: item[0]):
        if remaining <= 0:
            truncated = True
            break
        limit = min(MAX_AGENT_CONTEXT_FILE_BYTES, remaining)
        try:
            with path.open("rb") as stream:
                data = stream.read(limit + 1)
            file_stat = path.stat()
        except OSError:
            _issue(
                issues,
                code="context_source_unreadable",
                path=relative,
                message="A context file could not be read.",
            )
            continue
        file_truncated = len(data) > limit
        bounded = data[:limit]
        try:
            text = _decode_bounded_text(bounded, truncated=file_truncated)
        except (UnicodeDecodeError, ValueError):
            _issue(
                issues,
                code="context_source_not_text",
                path=relative,
                message="A context file is not safe UTF-8 text.",
            )
            continue
        encoded = text.encode("utf-8")
        remaining -= len(encoded)
        truncated = truncated or file_truncated
        sources.append(
            {
                "sourceId": f"file:{relative}",
                "path": relative,
                "observedAt": max(0, file_stat.st_mtime_ns // 1_000_000),
                "text": text,
                "contentHash": hashlib.sha256(encoded).hexdigest(),
                "truncated": file_truncated,
                "stale": False,
            }
        )
    return _SourceRead(tuple(sources), truncated, tuple(issues))


def _configured_scope(
    config: SwitchboardConfig, current_session: Mapping[str, object]
) -> tuple[Project, ProjectLocation]:
    project_id = current_session.get("project_id")
    location_id = current_session.get("location_id")
    project = next(
        (item for item in config.projects if str(item.project_id) == project_id), None
    )
    location = next(
        (item for item in config.locations if str(item.location_id) == location_id),
        None,
    )
    if (
        project is None
        or location is None
        or location.project_id != project.project_id
        or str(location.host_id) != current_session.get("host_id")
    ):
        raise AgentToolError("the current project location is not configured")
    return project, location


def _handoff_record(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "handoffId": row["handoff_id"],
        "sessionKey": row["session_key"],
        "sequence": row["sequence"],
        "summary": row["summary"],
        "nextAction": row["next_action"],
        "source": row["source"],
        "sourceHostId": row["source_host_id"],
        "createdAt": row["created_at"],
        "contentHash": row["content_hash"],
    }


def build_agent_context(
    registry: Registry,
    authorized: AuthorizedAgent,
    *,
    config: SwitchboardConfig,
    generated_at: int | None = None,
) -> AgentContextEnvelope:
    """Build validated stable/live/recent context for the authorized project."""

    rows = registry.read_project_context(
        str(authorized.session_key),
        host_id=str(authorized.host_id),
        session_limit=MAX_AGENT_CONTEXT_SESSIONS,
    )
    project, location = _configured_scope(config, rows.current_session)
    source_read = _read_context_sources(
        root=location.path,
        context_sources=project.context_sources,
    )
    handoffs = {str(row["session_key"]): row for row in rows.latest_handoffs}
    sessions: list[dict[str, object]] = []
    for row in rows.sessions:
        record: dict[str, object] = {
            "sessionKey": row["session_key"],
            "projectId": row["project_id"],
            "provider": row["provider"],
            "runtimePresence": row["runtime_presence"],
            "activity": row["activity"],
            "attachment": row["attachment"],
            "lastObservedAt": row["last_observed_at"],
            "pinned": bool(row["pinned"]),
            "stale": False,
        }
        for field, target in (
            ("name", "name"),
            ("name_actor", "nameActor"),
            ("purpose", "purpose"),
            ("wrapped_at", "wrappedAt"),
        ):
            if row[field] is not None:
                record[target] = row[field]
        handoff = handoffs.get(str(row["session_key"]))
        if handoff is not None:
            record["latestHandoff"] = _handoff_record(handoff)
        sessions.append(record)
    timestamp = time.time_ns() // 1_000_000 if generated_at is None else generated_at
    return AgentContextEnvelope.from_dict(
        {
            "schemaVersion": 1,
            "protocolVersion": 1,
            "generatedAt": timestamp,
            "caller": {
                "hostId": str(authorized.host_id),
                "provider": authorized.provider.value,
                "sessionKey": str(authorized.session_key),
                "surfaceId": str(authorized.surface_id),
                "launchId": str(authorized.launch_id),
            },
            "project": {
                "projectId": str(project.project_id),
                "name": project.name,
                "locationId": str(location.location_id),
                "path": str(location.path),
                "contextSources": list(project.context_sources),
            },
            "stableSources": list(source_read.sources),
            "stableSourcesTruncated": source_read.truncated,
            "sessions": sessions,
            "sessionsTruncated": rows.retained_session_count > len(rows.sessions),
            "issues": list(source_read.issues),
        }
    )


def _caller_record(authorized: AuthorizedAgent) -> dict[str, object]:
    return {
        "hostId": str(authorized.host_id),
        "provider": authorized.provider.value,
        "sessionKey": str(authorized.session_key),
        "surfaceId": str(authorized.surface_id),
        "launchId": str(authorized.launch_id),
    }


def _project_record(project: Project, location: ProjectLocation) -> dict[str, object]:
    return {
        "projectId": str(project.project_id),
        "name": project.name,
        "locationId": str(location.location_id),
        "path": str(location.path),
        "contextSources": list(project.context_sources),
    }


def _agent_session_record(row: Mapping[str, object]) -> dict[str, object]:
    record: dict[str, object] = {
        "sessionKey": row["session_key"],
        "projectId": row["project_id"],
        "provider": row["provider"],
        "runtimePresence": row["runtime_presence"],
        "activity": row["activity"],
        "attachment": row["attachment"],
        "lastObservedAt": row["last_observed_at"],
        "pinned": bool(row["pinned"]),
        "stale": False,
    }
    for field, target in (
        ("name", "name"),
        ("name_actor", "nameActor"),
        ("purpose", "purpose"),
        ("wrapped_at", "wrappedAt"),
    ):
        if row[field] is not None:
            record[target] = row[field]
    return record


def _bounded_limit(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= DEFAULT_AGENT_SEARCH_LIMIT
    ):
        raise AgentToolError(
            f"limit must be between 1 and {DEFAULT_AGENT_SEARCH_LIMIT}"
        )
    return value


def _normalized_query(value: str) -> str:
    if not isinstance(value, str):
        raise AgentToolError("query must be a string")
    query = unicodedata.normalize("NFC", value).strip()
    if not query or len(query) > 256:
        raise AgentToolError("query must contain between 1 and 256 characters")
    if any(unicodedata.category(character) == "Cc" for character in query):
        raise AgentToolError("query contains control characters")
    return query


class AgentToolService:
    """Transport-neutral current-session agent operations."""

    def __init__(
        self,
        registry: Registry,
        *,
        host_id: HostId | str,
        config: SwitchboardConfig,
        environment: Mapping[str, str] | None = None,
        tmux: TmuxController | None = None,
    ) -> None:
        self.registry = registry
        self.config = config
        self.environment = os.environ if environment is None else environment
        self.authorized = authorize_agent(
            registry,
            host_id=host_id,
            environment=environment,
            tmux=tmux,
        )

    def current(self) -> SessionDetailEnvelope:
        return read_session_detail(
            self.registry,
            host_id=self.authorized.host_id,
            session_key=self.authorized.session_key,
        )

    def context(self) -> AgentContextEnvelope:
        return build_agent_context(
            self.registry,
            self.authorized,
            config=self.config,
        )

    def list_sessions(self) -> AgentSessionListEnvelope:
        rows = self.registry.read_project_context(
            str(self.authorized.session_key),
            host_id=str(self.authorized.host_id),
            session_limit=DEFAULT_AGENT_PROJECT_SESSION_LIMIT,
        )
        project, location = _configured_scope(self.config, rows.current_session)
        return AgentSessionListEnvelope.from_dict(
            {
                "schemaVersion": 1,
                "protocolVersion": 1,
                "generatedAt": time.time_ns() // 1_000_000,
                "caller": _caller_record(self.authorized),
                "project": _project_record(project, location),
                "sessions": [_agent_session_record(row) for row in rows.sessions],
                "sessionsTruncated": rows.retained_session_count > len(rows.sessions),
            }
        )

    def session_detail(
        self, session_key: str, *, handoff_limit: int = 20
    ) -> SessionDetailEnvelope:
        rows = self.registry.read_project_session_detail(
            str(self.authorized.session_key),
            str(SessionKey.parse(session_key)),
            host_id=str(self.authorized.host_id),
            handoff_limit=handoff_limit,
        )
        return detail_envelope(rows)

    def handoff(self, handoff_id: str) -> AgentHandoffEnvelope:
        caller, handoff = self.registry.read_project_handoff(
            str(self.authorized.session_key),
            handoff_id,
            host_id=str(self.authorized.host_id),
        )
        project, _ = _configured_scope(self.config, caller)
        return AgentHandoffEnvelope.from_dict(
            {
                "schemaVersion": 1,
                "protocolVersion": 1,
                "generatedAt": time.time_ns() // 1_000_000,
                "caller": _caller_record(self.authorized),
                "projectId": str(project.project_id),
                "handoff": _handoff_record(handoff),
            }
        )

    def search(self, query: str, *, limit: int = 20) -> AgentSearchEnvelope:
        bounded_limit = _bounded_limit(limit)
        rows = self.registry.search_project_context(
            str(self.authorized.session_key),
            query,
            host_id=str(self.authorized.host_id),
            limit=bounded_limit,
        )
        project, _ = _configured_scope(self.config, rows.current_session)
        results: list[dict[str, object]] = []
        for row in rows.results:
            record = {
                "kind": row["kind"],
                "sessionKey": row["session_key"],
                "observedAt": row["observed_at"],
            }
            if row["kind"] == "session":
                record["provider"] = row["provider"]
                for field in ("name", "purpose"):
                    if field in row:
                        record[field] = row[field]
            else:
                for source, target in (
                    ("handoff_id", "handoffId"),
                    ("sequence", "sequence"),
                    ("summary", "summary"),
                    ("next_action", "nextAction"),
                    ("source", "source"),
                ):
                    record[target] = row[source]
            results.append(record)
        return AgentSearchEnvelope.from_dict(
            {
                "schemaVersion": 1,
                "protocolVersion": 1,
                "generatedAt": time.time_ns() // 1_000_000,
                "caller": _caller_record(self.authorized),
                "projectId": str(project.project_id),
                "query": rows.query,
                "results": results,
                "resultsTruncated": rows.results_truncated,
            }
        )

    def memory_search(self, query: str, *, limit: int = 20) -> AgentMemoryEnvelope:
        normalized_query = _normalized_query(query)
        bounded_limit = _bounded_limit(limit)
        rows = self.registry.read_project_context(
            str(self.authorized.session_key),
            host_id=str(self.authorized.host_id),
            session_limit=1,
        )
        project, _ = _configured_scope(self.config, rows.current_session)
        result = search_memory(
            self.config.memory,
            query=normalized_query,
            project=project.name,
            limit=bounded_limit,
            environment=self.environment,
        )
        return AgentMemoryEnvelope.from_dict(
            {
                "schemaVersion": 1,
                "protocolVersion": 1,
                "generatedAt": time.time_ns() // 1_000_000,
                "caller": _caller_record(self.authorized),
                "projectId": str(project.project_id),
                "query": normalized_query,
                "adapter": "stdio-mcp",
                "available": result.available,
                "text": result.text,
                "truncated": result.truncated,
                "issues": list(result.issues),
            }
        )

    def set_name(self, value: str | None) -> SessionDetailEnvelope:
        self.registry.set_session_name(
            str(self.authorized.session_key),
            host_id=str(self.authorized.host_id),
            name=value,
            actor="agent",
        )
        return self.current()

    def append_handoff(
        self,
        *,
        summary: str,
        next_action: str,
        handoff_id: str | None,
        wrap: bool,
    ) -> SessionDetailEnvelope:
        self.registry.curate_session_handoff(
            str(self.authorized.session_key),
            host_id=str(self.authorized.host_id),
            summary=summary,
            next_action=next_action,
            handoff_id=handoff_id,
            wrap=wrap,
            source="agent",
        )
        rows = self.registry.read_session_detail(
            str(self.authorized.session_key),
            host_id=str(self.authorized.host_id),
        )
        return detail_envelope(rows)


__all__ = [
    "AgentContextEnvelope",
    "AgentToolError",
    "AgentToolService",
    "AuthorizedAgent",
    "authorize_agent",
    "build_agent_context",
]
