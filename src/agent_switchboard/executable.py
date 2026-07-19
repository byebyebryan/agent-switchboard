"""Neutral installed-executable resolution shared by core frontends."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from .domain import ValidationError

ExecutableSearch = Callable[[str], str | None]


class ExecutableError(ValidationError):
    """The installed Switchboard command cannot be resolved safely."""


def resolve_swbctl_executable(
    *,
    invoked_as: str | None = None,
    search: ExecutableSearch = shutil.which,
) -> Path:
    """Resolve one absolute executable path for recursive fixed-argv calls."""

    invoked_value = sys.argv[0] if invoked_as is None else invoked_as
    invoked = Path(invoked_value)
    found: str | None = None
    if invoked.name == "swbctl":
        found = (
            str(invoked.absolute()) if invoked.is_absolute() else search(invoked_value)
        )
    if found is None:
        found = search("swbctl")
    if found is None:
        raise ExecutableError("swbctl is not available on PATH")
    path = Path(found).absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ExecutableError("the resolved swbctl path is not executable")
    return path
