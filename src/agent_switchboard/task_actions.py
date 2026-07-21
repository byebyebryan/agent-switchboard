"""State-first task lifecycle actions with best-effort runtime cleanup."""

from __future__ import annotations

import time
from collections.abc import Callable

from .domain import HostId, RuntimePresence, SessionKey, TaskId, ValidationError
from .protocol import (
    ErrorRecord,
    ErrorScope,
    RuntimeDisposition,
    SessionAction,
    SessionActionStatus,
    TaskCloseAction,
    TaskCloseStatus,
)
from .storage import Registry, StorageError, TaskConflict

Clock = Callable[[], int]
ReconcileRuntime = Callable[[], object]
StopSession = Callable[[str], SessionAction]


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class TaskCloseController:
    """Close first, then stop only through the safe managed-session boundary."""

    def __init__(
        self,
        registry: Registry,
        *,
        host_id: HostId | str,
        reconcile_runtime: ReconcileRuntime,
        stop_session: StopSession,
        clock: Clock = _now_ms,
    ) -> None:
        self.registry = registry
        self.host_id = host_id if isinstance(host_id, HostId) else HostId(host_id)
        self.reconcile_runtime = reconcile_runtime
        self.stop_session = stop_session
        self.clock = clock

    def close(self, task_id: str) -> TaskCloseAction:
        parsed_task_id = TaskId(task_id)
        if str(parsed_task_id) != task_id:
            raise ValidationError("task ID must use canonical UUID spelling")

        reconciliation_warning: ErrorRecord | None = None
        try:
            self.reconcile_runtime()
        except (StorageError, OSError):
            reconciliation_warning = self._warning(
                "runtime_reconciliation_failed",
                "Runtime state could not be refreshed before cleanup.",
                retryable=True,
            )

        task = self.registry.get_task(task_id)
        if task is None or task["host_id"] != str(self.host_id):
            return self._blocked(
                parsed_task_id,
                "task_not_found",
                "The task is not retained on this host.",
            )
        was_closed = task["status"] == "closed"
        current = task.get("current_session_key")
        current_key = SessionKey.parse(current) if isinstance(current, str) else None

        try:
            self.registry.close_task(task_id, host_id=str(self.host_id))
        except TaskConflict as error:
            return self._blocked(parsed_task_id, error.code, str(error))

        status = (
            TaskCloseStatus.ALREADY_CLOSED if was_closed else TaskCloseStatus.CLOSED
        )
        if current_key is None:
            return TaskCloseAction(
                status,
                self.host_id,
                parsed_task_id,
                RuntimeDisposition.NO_SESSION,
            )

        session = self.registry.get_session(str(current_key))
        if (
            session is not None
            and session["runtime_presence"] == RuntimePresence.STOPPED
        ):
            return TaskCloseAction(
                status,
                self.host_id,
                parsed_task_id,
                RuntimeDisposition.ALREADY_STOPPED,
                current_key,
            )

        try:
            stopped = self.stop_session(str(current_key))
        except (StorageError, OSError):
            return TaskCloseAction(
                status,
                self.host_id,
                parsed_task_id,
                RuntimeDisposition.UNKNOWN,
                current_key,
                warning=reconciliation_warning
                or self._warning(
                    "runtime_cleanup_failed",
                    "The task closed, but runtime cleanup could not be completed.",
                    session_key=current_key,
                    retryable=True,
                ),
            )

        if stopped.status is SessionActionStatus.STOPPED:
            return TaskCloseAction(
                status,
                self.host_id,
                parsed_task_id,
                RuntimeDisposition.STOPPED,
                current_key,
            )
        if stopped.status is SessionActionStatus.ALREADY_STOPPED:
            return TaskCloseAction(
                status,
                self.host_id,
                parsed_task_id,
                RuntimeDisposition.ALREADY_STOPPED,
                current_key,
            )

        latest = self.registry.get_session(str(current_key))
        disposition = (
            RuntimeDisposition.RETAINED
            if latest is not None
            and latest["runtime_presence"] == RuntimePresence.LIVE.value
            else RuntimeDisposition.UNKNOWN
        )
        warning = (
            stopped.error
            or reconciliation_warning
            or self._warning(
                "runtime_cleanup_blocked",
                "The task closed, but its runtime could not be stopped safely.",
                session_key=current_key,
            )
        )
        return TaskCloseAction(
            status,
            self.host_id,
            parsed_task_id,
            disposition,
            current_key,
            warning=warning,
        )

    def _blocked(self, task_id: TaskId, code: str, message: str) -> TaskCloseAction:
        return TaskCloseAction(
            TaskCloseStatus.BLOCKED,
            self.host_id,
            task_id,
            RuntimeDisposition.UNKNOWN,
            error=ErrorRecord(
                code,
                message,
                ErrorScope.TASK,
                False,
                self.clock(),
                host_id=self.host_id,
            ),
        )

    def _warning(
        self,
        code: str,
        message: str,
        *,
        session_key: SessionKey | None = None,
        retryable: bool = False,
    ) -> ErrorRecord:
        return ErrorRecord(
            code,
            message,
            ErrorScope.SESSION if session_key is not None else ErrorScope.HOST,
            retryable,
            self.clock(),
            host_id=self.host_id,
            provider=None if session_key is None else session_key.provider,
            session_key=session_key,
        )


__all__ = ["TaskCloseController"]
