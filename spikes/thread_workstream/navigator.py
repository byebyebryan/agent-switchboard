"""Observed-state navigator model for the visibility/history spike."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


ALIAS = re.compile(r"^thread-[a-z][a-z0-9-]*$")


class NavigatorError(ValueError):
    pass


class TransitionVisibility(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"


@dataclass(frozen=True, slots=True)
class NavigatorState:
    previous: str
    current: str
    transition: TransitionVisibility
    active_tip: str

    def __post_init__(self) -> None:
        if not all(ALIAS.fullmatch(value) for value in (self.previous, self.current)):
            raise NavigatorError("navigator identities must be sanitized aliases")
        if self.active_tip != self.current:
            raise NavigatorError("historical inspection cannot move the active tip")

    def render(self) -> str:
        return (
            f"Previous: {self.previous}\n"
            f"Current: {self.current}\n"
            f"Transition: {self.transition.value}\n"
            f"Active: {self.active_tip}\n"
        )

    def activate_current(self) -> tuple[str, int]:
        """Return the exact target and one-action count."""

        return self.active_tip, 1


__all__ = [
    "NavigatorError",
    "NavigatorState",
    "TransitionVisibility",
]
