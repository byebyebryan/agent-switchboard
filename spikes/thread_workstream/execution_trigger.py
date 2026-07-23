"""Spike-only execution-intent classification and cutover transaction model."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class TriggerError(RuntimeError):
    """An execution-intent observation cannot authorize a safe cutover."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PlanProvenance(StrEnum):
    CODEX_PLAN_ITEM = "codex-plan-item"
    EXPLICIT_SELECTION = "explicit-selection"
    CONVERSATIONAL_UNSELECTED = "conversational-unselected"


class ExecutionSignal(StrEnum):
    NATIVE_CLEAR_IMPLEMENT = "native-clear-implement"
    CODEX_ORDINARY_IMPLEMENT = "codex-ordinary-implement"
    EXPLICIT_FRESH_IMPLEMENT = "explicit-fresh-implement"
    NATURAL_LANGUAGE_ACCEPTANCE = "natural-language-acceptance"
    DISCUSSION = "discussion"
    PLAN_REVISION = "plan-revision"
    GENERIC_CLEAR = "generic-clear"


class TriggerDecision(StrEnum):
    NATIVE_ADOPT = "native-adopt"
    CUTOVER = "cutover"
    PROVIDER_ONLY = "provider-only"
    ADVISORY = "advisory"
    STAY = "stay"


@dataclass(frozen=True, slots=True)
class PlanCandidate:
    source_identity: str
    source_revision: int
    digest: str
    provenance: PlanProvenance
    accepted: bool
    consumed: bool = False


@dataclass(frozen=True, slots=True)
class TriggerObservation:
    nonce: str
    order: int
    source_identity: str
    source_revision: int
    signal: ExecutionSignal
    before_source_commit: bool
    source_sampled: bool
    referenced_plan_digest: str | None = None
    ordinary_coding_input: bool = False
    mode_before: str | None = None
    mode_at_submit: str | None = None


@dataclass(frozen=True, slots=True)
class TriggerReceipt:
    decision: TriggerDecision
    authoritative: bool
    reason: str


def _candidate_matches(
    candidate: PlanCandidate | None,
    observation: TriggerObservation,
    *,
    provenances: set[PlanProvenance],
) -> bool:
    return bool(
        candidate is not None
        and candidate.accepted
        and not candidate.consumed
        and candidate.provenance in provenances
        and candidate.source_identity == observation.source_identity
        and candidate.source_revision == observation.source_revision
        and candidate.digest == observation.referenced_plan_digest
    )


def classify_execution_trigger(
    candidate: PlanCandidate | None,
    observation: TriggerObservation,
) -> TriggerReceipt:
    """Classify only pre-submit observations with exact plan provenance."""

    if not observation.before_source_commit or observation.source_sampled:
        return TriggerReceipt(
            TriggerDecision.STAY,
            False,
            "source-already-committed",
        )

    if observation.signal is ExecutionSignal.GENERIC_CLEAR:
        return TriggerReceipt(
            TriggerDecision.PROVIDER_ONLY,
            False,
            "generic-clear-is-not-task-authority",
        )

    if observation.signal is ExecutionSignal.NATIVE_CLEAR_IMPLEMENT:
        matched = _candidate_matches(
            candidate,
            observation,
            provenances={PlanProvenance.CODEX_PLAN_ITEM},
        )
        return TriggerReceipt(
            TriggerDecision.NATIVE_ADOPT if matched else TriggerDecision.PROVIDER_ONLY,
            matched,
            "native-plan-provenance" if matched else "native-plan-unconfirmed",
        )

    if observation.signal is ExecutionSignal.CODEX_ORDINARY_IMPLEMENT:
        matched = observation.ordinary_coding_input and _candidate_matches(
            candidate,
            observation,
            provenances={PlanProvenance.CODEX_PLAN_ITEM},
        )
        return TriggerReceipt(
            TriggerDecision.CUTOVER if matched else TriggerDecision.STAY,
            matched,
            "structured-plan-implementation"
            if matched
            else "ordinary-implementation-unconfirmed",
        )

    if observation.signal is ExecutionSignal.EXPLICIT_FRESH_IMPLEMENT:
        matched = _candidate_matches(
            candidate,
            observation,
            provenances={
                PlanProvenance.CODEX_PLAN_ITEM,
                PlanProvenance.EXPLICIT_SELECTION,
            },
        )
        return TriggerReceipt(
            TriggerDecision.CUTOVER if matched else TriggerDecision.STAY,
            matched,
            "explicit-plan-selection" if matched else "explicit-plan-mismatch",
        )

    if observation.signal is ExecutionSignal.NATURAL_LANGUAGE_ACCEPTANCE:
        relevant = bool(
            candidate is not None
            and candidate.accepted
            and not candidate.consumed
            and candidate.source_identity == observation.source_identity
        )
        return TriggerReceipt(
            TriggerDecision.ADVISORY if relevant else TriggerDecision.STAY,
            False,
            "language-is-advisory" if relevant else "no-current-plan",
        )

    return TriggerReceipt(
        TriggerDecision.STAY,
        False,
        "non-execution-intent",
    )


class ExecutionTriggerGate:
    """Reject replay, stale ordering, and concurrent authoritative triggers."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._last_order = 0
        self._pending: str | None = None

    def observe(
        self,
        candidate: PlanCandidate | None,
        observation: TriggerObservation,
    ) -> TriggerReceipt:
        if observation.nonce in self._seen:
            raise TriggerError("trigger-replayed")
        if observation.order <= self._last_order:
            raise TriggerError("trigger-stale")
        receipt = classify_execution_trigger(candidate, observation)
        if receipt.authoritative and self._pending is not None:
            self._seen.add(observation.nonce)
            self._last_order = observation.order
            raise TriggerError("trigger-concurrent")
        self._seen.add(observation.nonce)
        self._last_order = observation.order
        if receipt.authoritative:
            self._pending = observation.nonce
        return receipt

    def settle(self, nonce: str) -> None:
        if self._pending != nonce:
            raise TriggerError("trigger-settlement-mismatch")
        self._pending = None


class CutoverState(StrEnum):
    HELD = "held"
    DESTINATION_READY = "destination-ready"
    DELIVERY_UNCERTAIN = "delivery-uncertain"
    DELIVERED = "delivered"
    COMMIT_UNCERTAIN = "commit-uncertain"
    COMMITTED = "committed"
    RESTORED = "restored"


@dataclass(frozen=True, slots=True)
class CutoverBinding:
    version: int
    active_identity: str
    plan_consumed: bool = False


class AtomicCutoverStore:
    """Minimal compare-and-swap record standing in for proven transition storage."""

    def __init__(self, binding: CutoverBinding) -> None:
        self._binding = binding

    @property
    def binding(self) -> CutoverBinding:
        return self._binding

    def commit(
        self,
        *,
        expected_version: int,
        destination_identity: str,
        fail_at: str | None = None,
    ) -> CutoverBinding:
        if self._binding.version != expected_version:
            raise TriggerError("binding-version-changed")
        if fail_at == "before":
            raise TriggerError("binding-commit-rejected")
        updated = CutoverBinding(
            version=self._binding.version + 1,
            active_identity=destination_identity,
            plan_consumed=True,
        )
        self._binding = updated
        if fail_at == "after":
            raise TriggerError("binding-commit-outcome-uncertain")
        if fail_at is not None:
            raise TriggerError("binding-fault-mode-unknown")
        return updated


class DeliveryLedger:
    """Idempotent destination-delivery evidence for failure replay."""

    def __init__(self) -> None:
        self._keys: set[str] = set()
        self.attempts = 0

    def submit(self, key: str) -> bool:
        self.attempts += 1
        before = len(self._keys)
        self._keys.add(key)
        return len(self._keys) > before

    def observed(self, key: str) -> bool:
        return key in self._keys

    @property
    def deliveries(self) -> int:
        return len(self._keys)


@dataclass(slots=True)
class CutoverTransaction:
    """Hold-before-submit orchestration around reusable cutover mechanics."""

    nonce: str
    source_identity: str
    plan_digest: str
    input_digest: str
    binding_version: int
    state: CutoverState = CutoverState.HELD
    destination_identity: str | None = None
    delivery_key: str | None = None
    source_input_restored: bool = False

    @classmethod
    def prepare(
        cls,
        candidate: PlanCandidate,
        observation: TriggerObservation,
        receipt: TriggerReceipt,
        store: AtomicCutoverStore,
    ) -> CutoverTransaction:
        if receipt.decision is not TriggerDecision.CUTOVER or not receipt.authoritative:
            raise TriggerError("trigger-not-authoritative")
        if observation.referenced_plan_digest != candidate.digest:
            raise TriggerError("plan-reference-changed")
        if store.binding.active_identity != observation.source_identity:
            raise TriggerError("source-binding-changed")
        return cls(
            nonce=observation.nonce,
            source_identity=observation.source_identity,
            plan_digest=candidate.digest,
            input_digest=f"input-for-{observation.nonce}",
            binding_version=store.binding.version,
        )

    def set_destination(self, destination_identity: str) -> None:
        if self.state is not CutoverState.HELD:
            raise TriggerError("destination-state-invalid")
        if not destination_identity or destination_identity == self.source_identity:
            raise TriggerError("destination-identity-invalid")
        self.destination_identity = destination_identity
        self.state = CutoverState.DESTINATION_READY

    def deliver(
        self,
        ledger: DeliveryLedger,
        *,
        fail_at: str | None = None,
    ) -> None:
        if self.state not in {
            CutoverState.DESTINATION_READY,
            CutoverState.DELIVERY_UNCERTAIN,
        }:
            raise TriggerError("delivery-state-invalid")
        key = self.delivery_key or f"delivery-{self.nonce}"
        self.delivery_key = key
        if fail_at == "before":
            raise TriggerError("delivery-rejected")
        ledger.submit(key)
        if fail_at == "after":
            self.state = CutoverState.DELIVERY_UNCERTAIN
            raise TriggerError("delivery-outcome-uncertain")
        if fail_at is not None:
            raise TriggerError("delivery-fault-mode-unknown")
        self.state = CutoverState.DELIVERED

    def recover_delivery(self, ledger: DeliveryLedger) -> None:
        if self.state is not CutoverState.DELIVERY_UNCERTAIN:
            raise TriggerError("delivery-recovery-state-invalid")
        if self.delivery_key is not None and ledger.observed(self.delivery_key):
            self.state = CutoverState.DELIVERED
        else:
            self.source_input_restored = True
            self.state = CutoverState.RESTORED

    def commit(
        self,
        store: AtomicCutoverStore,
        *,
        fail_at: str | None = None,
    ) -> None:
        if self.state not in {CutoverState.DELIVERED, CutoverState.COMMIT_UNCERTAIN}:
            raise TriggerError("commit-state-invalid")
        if self.destination_identity is None:
            raise TriggerError("destination-missing")
        try:
            store.commit(
                expected_version=self.binding_version,
                destination_identity=self.destination_identity,
                fail_at=fail_at,
            )
        except TriggerError as error:
            if error.code == "binding-commit-outcome-uncertain":
                self.state = CutoverState.COMMIT_UNCERTAIN
            raise
        self.state = CutoverState.COMMITTED

    def recover_commit(self, store: AtomicCutoverStore) -> None:
        if self.state is not CutoverState.COMMIT_UNCERTAIN:
            raise TriggerError("commit-recovery-state-invalid")
        binding = store.binding
        if (
            binding.version == self.binding_version + 1
            and binding.active_identity == self.destination_identity
            and binding.plan_consumed
        ):
            self.state = CutoverState.COMMITTED
            return
        self.state = CutoverState.DELIVERED

    def restore_source(self) -> None:
        if self.state in {
            CutoverState.DELIVERY_UNCERTAIN,
            CutoverState.DELIVERED,
            CutoverState.COMMIT_UNCERTAIN,
            CutoverState.COMMITTED,
        }:
            raise TriggerError("delivered-input-cannot-return-to-source")
        self.source_input_restored = True
        self.state = CutoverState.RESTORED

    def consume_candidate(self, candidate: PlanCandidate) -> PlanCandidate:
        if self.state is not CutoverState.COMMITTED:
            raise TriggerError("plan-consumption-before-commit")
        if candidate.digest != self.plan_digest:
            raise TriggerError("plan-consumption-mismatch")
        return replace(candidate, consumed=True)


__all__ = [
    "AtomicCutoverStore",
    "CutoverBinding",
    "CutoverState",
    "CutoverTransaction",
    "DeliveryLedger",
    "ExecutionSignal",
    "ExecutionTriggerGate",
    "PlanCandidate",
    "PlanProvenance",
    "TriggerDecision",
    "TriggerError",
    "TriggerObservation",
    "TriggerReceipt",
    "classify_execution_trigger",
]
