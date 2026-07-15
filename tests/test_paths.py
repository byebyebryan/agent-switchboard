from __future__ import annotations

import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_switchboard.domain import ValidationError
from agent_switchboard.paths import (
    config_path,
    database_path,
    host_id_path,
    load_or_create_host_id,
)


def test_xdg_paths_use_absolute_overrides(tmp_path: Path) -> None:
    environment = {
        "XDG_CONFIG_HOME": str(tmp_path / "configuration"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    assert config_path(environ=environment) == (
        tmp_path / "configuration/agent-switchboard/config.toml"
    )
    assert host_id_path(environ=environment) == (
        tmp_path / "state/agent-switchboard/host-id"
    )
    assert database_path(environ=environment) == (
        tmp_path / "state/agent-switchboard/switchboard.db"
    )


def test_relative_xdg_values_are_ignored(tmp_path: Path) -> None:
    environment = {"XDG_CONFIG_HOME": "relative", "XDG_STATE_HOME": "relative"}
    assert config_path(environ=environment, home=tmp_path) == (
        tmp_path / ".config/agent-switchboard/config.toml"
    )
    assert host_id_path(environ=environment, home=tmp_path) == (
        tmp_path / ".local/state/agent-switchboard/host-id"
    )
    assert database_path(environ=environment, home=tmp_path) == (
        tmp_path / ".local/state/agent-switchboard/switchboard.db"
    )


def test_host_id_is_stable_atomic_and_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "nested/host-id"
    with ThreadPoolExecutor(max_workers=16) as executor:
        ids = list(executor.map(lambda _: load_or_create_host_id(path), range(64)))
    assert len(set(ids)) == 1
    assert load_or_create_host_id(path) == ids[0]
    assert path.read_text(encoding="ascii") == f"{ids[0]}\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob("*.tmp"))


def test_existing_host_id_permissions_are_tightened(tmp_path: Path) -> None:
    path = tmp_path / "host-id"
    path.write_text("11111111-1111-4111-8111-111111111111\n", encoding="ascii")
    path.chmod(0o644)
    host_id = load_or_create_host_id(path)
    assert str(host_id) == "11111111-1111-4111-8111-111111111111"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_corrupt_host_id_is_not_silently_replaced(tmp_path: Path) -> None:
    path = tmp_path / "host-id"
    path.write_text("corrupt\n", encoding="ascii")
    with pytest.raises(ValidationError, match="invalid UUID"):
        load_or_create_host_id(path)
    assert path.read_text(encoding="ascii") == "corrupt\n"
