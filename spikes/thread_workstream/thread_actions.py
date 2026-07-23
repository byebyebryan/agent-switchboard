"""Spike-only policy for explicit thread and workstream management."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ThreadActionError(RuntimeError):
    """An explicit action cannot be applied to the observed state."""


class ActionSurface(StrEnum):
    NATIVE_PLAN = "native-plan"
    NAVIGATOR = "navigator"
    CONVERSATION = "conversation"


class ThreadAction(StrEnum):
    IMPLEMENT_HERE = "implement-here"
    CLEAR_AND_IMPLEMENT = "clear-and-implement"
    START_THREAD = "start-thread"
    INTERRUPT = "interrupt"
    START_WORKSTREAM = "start-workstream"
    FORK_WORKSTREAM = "fork-workstream"


class ActionRoute(StrEnum):
    STAY = "stay"
    INTERRUPT = "interrupt"
    THREAD_CUTOVER = "thread-cutover"
    NEW_WORKSTREAM = "new-workstream"
    FORK_WORKSTREAM = "fork-workstream"


@dataclass(frozen=True, slots=True)
class ActionObservation:
    surface: ActionSurface
    action: ThreadAction
    active_turn: bool
    has_completed_turn: bool
    has_exact_filesystem_checkpoint: bool
    has_transfer_artifact: bool


@dataclass(frozen=True, slots=True)
class ActionDecision:
    route: ActionRoute
    requires_prompt_boundary: bool
    preserves_source_turn: bool
    branches_from_last_completed_turn: bool
    creates_worktree: bool


_CONVERSATIONAL_ACTIONS = {
    "go ahead in a new thread": ThreadAction.CLEAR_AND_IMPLEMENT,
    "start a new thread": ThreadAction.START_THREAD,
    "start a new workstream": ThreadAction.START_WORKSTREAM,
    "fork the current task": ThreadAction.FORK_WORKSTREAM,
    "fork this task": ThreadAction.FORK_WORKSTREAM,
}


def conversational_action(text: str) -> ThreadAction | None:
    """Recognize only reserved, exact aliases; fuzzy intent is never authority."""

    normalized = " ".join(text.strip().casefold().split())
    if normalized.endswith((".", "!")):
        normalized = normalized[:-1].rstrip()
    return _CONVERSATIONAL_ACTIONS.get(normalized)


def decide_thread_action(observation: ActionObservation) -> ActionDecision:
    """Resolve explicit user intent without promoting capability into policy."""

    if observation.surface is ActionSurface.NATIVE_PLAN and observation.action not in {
        ThreadAction.IMPLEMENT_HERE,
        ThreadAction.CLEAR_AND_IMPLEMENT,
    }:
        raise ThreadActionError(
            "native Plan exposes only its two implementation choices"
        )
    if observation.surface is ActionSurface.CONVERSATION and observation.action not in {
        ThreadAction.CLEAR_AND_IMPLEMENT,
        ThreadAction.START_THREAD,
        ThreadAction.START_WORKSTREAM,
        ThreadAction.FORK_WORKSTREAM,
    }:
        raise ThreadActionError("action has no conversational alias")
    if observation.surface is ActionSurface.CONVERSATION and observation.active_turn:
        raise ThreadActionError(
            "conversational actions require the next user prompt boundary"
        )

    if observation.action is ThreadAction.IMPLEMENT_HERE:
        return ActionDecision(
            route=ActionRoute.STAY,
            requires_prompt_boundary=True,
            preserves_source_turn=True,
            branches_from_last_completed_turn=False,
            creates_worktree=False,
        )
    if observation.action in {
        ThreadAction.CLEAR_AND_IMPLEMENT,
        ThreadAction.START_THREAD,
    }:
        if observation.active_turn:
            raise ThreadActionError(
                "same-workstream thread cutover requires an idle source turn"
            )
        if (
            observation.action is ThreadAction.CLEAR_AND_IMPLEMENT
            and not observation.has_transfer_artifact
        ):
            raise ThreadActionError(
                "fresh implementation requires an exact selected artifact"
            )
        return ActionDecision(
            route=ActionRoute.THREAD_CUTOVER,
            requires_prompt_boundary=(
                observation.surface is ActionSurface.CONVERSATION
            ),
            preserves_source_turn=True,
            branches_from_last_completed_turn=False,
            creates_worktree=False,
        )
    if observation.action is ThreadAction.INTERRUPT:
        if not observation.active_turn:
            raise ThreadActionError("there is no active turn to interrupt")
        return ActionDecision(
            route=ActionRoute.INTERRUPT,
            requires_prompt_boundary=False,
            preserves_source_turn=False,
            branches_from_last_completed_turn=False,
            creates_worktree=False,
        )
    if observation.action is ThreadAction.START_WORKSTREAM:
        return ActionDecision(
            route=ActionRoute.NEW_WORKSTREAM,
            requires_prompt_boundary=(
                observation.surface is ActionSurface.CONVERSATION
            ),
            preserves_source_turn=True,
            branches_from_last_completed_turn=False,
            creates_worktree=True,
        )
    if observation.action is ThreadAction.FORK_WORKSTREAM:
        if not observation.has_completed_turn:
            raise ThreadActionError("fork requires a completed provider turn")
        if not observation.has_exact_filesystem_checkpoint:
            raise ThreadActionError("fork requires an exact filesystem checkpoint")
        return ActionDecision(
            route=ActionRoute.FORK_WORKSTREAM,
            requires_prompt_boundary=(
                observation.surface is ActionSurface.CONVERSATION
            ),
            preserves_source_turn=True,
            branches_from_last_completed_turn=True,
            creates_worktree=True,
        )
    raise ThreadActionError("unsupported thread action")


__all__ = [
    "ActionDecision",
    "ActionObservation",
    "ActionRoute",
    "ActionSurface",
    "ThreadAction",
    "ThreadActionError",
    "conversational_action",
    "decide_thread_action",
]
