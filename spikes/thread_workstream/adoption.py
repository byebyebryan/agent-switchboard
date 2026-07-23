"""Spike-only trusted adoption and repeated authority rebinding."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum


class AdoptionError(RuntimeError):
    """A provider event cannot safely rotate the active binding."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TransitionClassification(StrEnum):
    PROVIDER_CLEAR = "provider-clear"
    TASK_TRANSITION = "task-transition"


@dataclass(frozen=True, slots=True)
class BindingRecord:
    version: int
    provider_identity: str
    capability: str
    launch: str
    surface: str
    server_generation: str
    pane: str
    process_birth: int
    working_directory: str


@dataclass(frozen=True, slots=True)
class ClearObservation:
    nonce: str
    order: int
    predecessor_identity: str
    destination_identity: str
    source: str
    launch: str
    surface: str
    server_generation: str
    pane: str
    process_birth: int
    working_directory: str
    provider_ancestor: bool
    provider_thread_exists: bool
    accepted_plan_digest: str | None


@dataclass(frozen=True, slots=True)
class InputObservation:
    nonce: str
    order: int
    provider_identity: str
    launch: str
    surface: str
    server_generation: str
    pane: str
    process_birth: int
    working_directory: str
    provider_ancestor: bool
    provider_thread_exists: bool
    carried_plan_digest: str | None


@dataclass(frozen=True, slots=True)
class AdoptionReceipt:
    source_identity: str
    destination_identity: str
    binding_version: int
    classification: TransitionClassification
    capability_rotated: bool


class AtomicBindingStore:
    """One in-memory compare-and-swap record for falsification tests."""

    def __init__(self, initial: BindingRecord) -> None:
        self._record = initial

    @property
    def record(self) -> BindingRecord:
        return self._record

    def rotate(
        self,
        *,
        expected_version: int,
        destination_identity: str,
        capability: str,
        fail_at: str | None = None,
    ) -> BindingRecord:
        if self._record.version != expected_version:
            raise AdoptionError("binding_version_changed")
        if fail_at in {"before", "partial"}:
            raise AdoptionError(f"binding_{fail_at}_commit_rejected")
        updated = replace(
            self._record,
            version=self._record.version + 1,
            provider_identity=destination_identity,
            capability=capability,
        )
        self._record = updated
        if fail_at == "after":
            raise AdoptionError("binding_commit_outcome_uncertain")
        if fail_at is not None:
            raise AdoptionError("binding_fault_mode_unknown")
        return updated


@dataclass(slots=True)
class _Tentative:
    clear: ClearObservation
    conflicted: bool = False


class AdoptionMachine:
    """Adopt only exact same-surface clear events, then rotate atomically."""

    def __init__(
        self,
        store: AtomicBindingStore,
        *,
        capability_factory: Callable[[], str],
    ) -> None:
        self.store = store
        self.capability_factory = capability_factory
        self._seen: set[str] = set()
        self._last_order = 0
        self._tentative: _Tentative | None = None

    @property
    def current(self) -> BindingRecord:
        return self.store.record

    def _validate_authority(
        self,
        *,
        launch: str,
        surface: str,
        server_generation: str,
        pane: str,
        process_birth: int,
        working_directory: str,
        provider_ancestor: bool,
        provider_thread_exists: bool,
    ) -> None:
        current = self.current
        checks = {
            "launch": launch == current.launch,
            "surface": surface == current.surface,
            "server_generation": server_generation == current.server_generation,
            "pane": pane == current.pane,
            "process_birth": process_birth == current.process_birth,
            "working_directory": working_directory == current.working_directory,
            "provider_ancestor": provider_ancestor,
            "provider_thread_exists": provider_thread_exists,
        }
        for name, valid in checks.items():
            if not valid:
                raise AdoptionError(f"authority_{name}_mismatch")

    def begin_clear(self, event: ClearObservation) -> None:
        if event.nonce in self._seen:
            raise AdoptionError("event_replayed")
        if event.order <= self._last_order:
            raise AdoptionError("event_stale")
        if self._tentative is not None:
            self._tentative.conflicted = True
            self._seen.add(event.nonce)
            raise AdoptionError("clear_concurrent")
        if event.source != "clear":
            raise AdoptionError("clear_source_invalid")
        if event.predecessor_identity != self.current.provider_identity:
            raise AdoptionError("clear_predecessor_unknown")
        if event.destination_identity == event.predecessor_identity:
            raise AdoptionError("clear_destination_unchanged")
        self._validate_authority(
            launch=event.launch,
            surface=event.surface,
            server_generation=event.server_generation,
            pane=event.pane,
            process_birth=event.process_birth,
            working_directory=event.working_directory,
            provider_ancestor=event.provider_ancestor,
            provider_thread_exists=event.provider_thread_exists,
        )
        self._seen.add(event.nonce)
        self._last_order = event.order
        self._tentative = _Tentative(event)

    def confirm_input(
        self,
        event: InputObservation,
        *,
        fail_at: str | None = None,
    ) -> AdoptionReceipt:
        tentative = self._tentative
        if tentative is None:
            raise AdoptionError("input_without_clear")
        if tentative.conflicted:
            self._tentative = None
            raise AdoptionError("clear_conflict_unresolved")
        if event.nonce in self._seen:
            raise AdoptionError("event_replayed")
        if event.order <= self._last_order:
            raise AdoptionError("event_stale")
        if event.provider_identity != tentative.clear.destination_identity:
            raise AdoptionError("input_destination_mismatch")
        self._validate_authority(
            launch=event.launch,
            surface=event.surface,
            server_generation=event.server_generation,
            pane=event.pane,
            process_birth=event.process_birth,
            working_directory=event.working_directory,
            provider_ancestor=event.provider_ancestor,
            provider_thread_exists=event.provider_thread_exists,
        )
        self._seen.add(event.nonce)
        self._last_order = event.order
        old = self.current
        capability = self.capability_factory()
        classification = (
            TransitionClassification.TASK_TRANSITION
            if tentative.clear.accepted_plan_digest is not None
            and tentative.clear.accepted_plan_digest == event.carried_plan_digest
            else TransitionClassification.PROVIDER_CLEAR
        )
        try:
            updated = self.store.rotate(
                expected_version=old.version,
                destination_identity=event.provider_identity,
                capability=capability,
                fail_at=fail_at,
            )
        except AdoptionError as error:
            self._tentative = None
            if (
                error.code == "binding_commit_outcome_uncertain"
                # The store record is complete and authoritative even though the
                # caller did not observe the commit response.
                and self.current.provider_identity != event.provider_identity
            ):
                raise AdoptionError("binding_partial_commit_detected") from error
            raise
        self._tentative = None
        return AdoptionReceipt(
            source_identity=old.provider_identity,
            destination_identity=updated.provider_identity,
            binding_version=updated.version,
            classification=classification,
            capability_rotated=updated.capability != old.capability,
        )


__all__ = [
    "AdoptionError",
    "AdoptionMachine",
    "AdoptionReceipt",
    "AtomicBindingStore",
    "BindingRecord",
    "ClearObservation",
    "InputObservation",
    "TransitionClassification",
]
