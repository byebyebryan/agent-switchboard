from __future__ import annotations

from pathlib import Path

import pytest

import agent_switchboard.config as config_module
from agent_switchboard.config import (
    ConfigError,
    WorkingDirectoryPolicy,
    load_config,
    merge_project_catalogs,
    parse_config,
)
from agent_switchboard.domain import HostId, ProviderId, Transport

HOST_A = HostId("11111111-1111-4111-8111-111111111111")
HOST_B = HostId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PROJECT = "22222222-2222-4222-8222-222222222222"
LOCATION_A = "33333333-3333-4333-8333-333333333333"
LOCATION_B = "44444444-4444-4444-8444-444444444444"


def full_config(path: Path) -> str:
    return f'''
[host]
display_name = "starship"

[providers.codex]
enabled = true
executable = "/opt/switchboard/bin/codex"

[providers.claude]
enabled = false

[defaults]
transport = "tmux"
refresh_interval_seconds = 15
staleness_interval_seconds = 90
recent_parked_limit = 42
working_directory = "require_explicit"

[tmux]
naming_prefix = "agent"
launch_timeout_seconds = 45

[remotes.snap]
ssh_target = "bryan@snap.lan"
display_name = "snap"

[projects."{PROJECT}"]
name = "Switchboard"
aliases = [" agent router ", "Agent   Router", "sessions"]
default_provider = "codex"
default_transport = "tmux"
context_sources = ["AGENTS.md", "README.md", "docs", "docs"]

[[projects."{PROJECT}".locations]]
location_id = "{LOCATION_A}"
display_name = "starship checkout"
path = "{path}"
repository_identity = "example/agent-switchboard"
provider_override = "claude"
transport_override = "tmux"
is_default = true
'''


def test_full_configuration_is_typed_and_normalized(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    config = parse_config(full_config(checkout), host_id=HOST_A)
    assert config.host.display_name == "starship"
    assert config.host.host_id == HOST_A
    assert config.providers[0].provider is ProviderId.CODEX
    assert config.providers[0].executable == "/opt/switchboard/bin/codex"
    assert not config.providers[1].enabled
    assert config.remotes[0].ssh_target == "bryan@snap.lan"
    assert config.defaults.transport is Transport.TMUX
    assert config.defaults.working_directory == WorkingDirectoryPolicy.REQUIRE_EXPLICIT
    assert config.tmux.naming_prefix == "agent"
    project = config.projects[0]
    assert project.aliases == ("agent router", "sessions")
    assert project.context_sources == ("AGENTS.md", "README.md", "docs")
    location = config.locations[0]
    assert location.path == checkout.resolve()
    assert location.provider_override is ProviderId.CLAUDE
    assert location.is_default


def test_minimal_configuration_has_documented_defaults() -> None:
    config = parse_config('[host]\ndisplay_name = "host"\n', host_id=HOST_A)
    assert [provider.provider for provider in config.providers] == [
        ProviderId.CODEX,
        ProviderId.CLAUDE,
    ]
    assert all(provider.enabled for provider in config.providers)
    assert config.defaults.transport is Transport.TMUX
    assert config.tmux.naming_prefix == "as"


def test_missing_implicit_configuration_uses_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "configuration" / "agent-switchboard" / "config.toml"
    monkeypatch.setattr(config_module, "config_path", lambda: source)

    config = load_config(host_id=HOST_A)

    assert config.host.host_id == HOST_A
    assert [provider.provider for provider in config.providers] == [
        ProviderId.CODEX,
        ProviderId.CLAUDE,
    ]
    assert config.remotes == ()
    assert config.projects == ()
    assert config.locations == ()
    assert config.defaults.transport is Transport.TMUX
    assert not source.exists()


def test_missing_explicit_configuration_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "missing.toml"

    with pytest.raises(
        ConfigError, match=r"cannot read configuration at .*missing.toml"
    ):
        load_config(source, host_id=HOST_A)


@pytest.mark.parametrize(
    "document",
    [
        "mystery = 1",
        "[host]\ndisplay_name='x'\nmystery=1",
        "[providers.codex]\nmystery=1",
        "[providers.future]\nenabled=true",
        "[remotes.snap]\nssh_target='snap'\nmystery=1",
        f"[projects.\"{PROJECT}\"]\nname='x'\nmystery=1",
        (
            f"[projects.\"{PROJECT}\"]\nname='x'\n"
            f'[[projects."{PROJECT}".locations]]\n'
            f"location_id='{LOCATION_A}'\npath='/tmp/x'\nmystery=1"
        ),
        "[defaults]\nmystery=1",
        "[tmux]\nmystery=1",
    ],
)
def test_unknown_configuration_keys_are_rejected(document: str) -> None:
    with pytest.raises(ConfigError, match=r"unknown|unsupported"):
        parse_config(document, host_id=HOST_A)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("[providers.codex]\nenabled='yes'", "boolean"),
        ("[remotes.snap]\nssh_target='-oProxy=x'", "without options"),
        ("[defaults]\nrefresh_interval_seconds=0", "between"),
        (
            "[defaults]\nrefresh_interval_seconds=60\nstaleness_interval_seconds=30",
            "at least",
        ),
        ("[defaults]\nworking_directory='guess'", "project_default"),
        ("[tmux]\nnaming_prefix='bad prefix'", "safe tmux"),
        ("[host]\ndisplay_name='bad\u009bvalue'", "control"),
        ('[host]\ndisplay_name="\\ttrimmed-looking"', "control"),
        (
            f"[projects.\"{PROJECT}\"]\nname='x'\ncontext_sources=['../secret']",
            "relative",
        ),
        ("[projects.not-a-uuid]\nname='x'", "invalid UUID"),
    ],
)
def test_invalid_configuration_values_are_rejected(document: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(document, host_id=HOST_A)


def host_project_config(
    *,
    host_name: str,
    location_id: str,
    path: Path,
    alias: str,
    project_name: str = "Switchboard",
) -> str:
    return f'''
[host]
display_name = "{host_name}"
[projects."{PROJECT}"]
name = "{project_name}"
aliases = ["{alias}"]
default_provider = "codex"
default_transport = "tmux"
context_sources = ["README.md"]
[[projects."{PROJECT}".locations]]
location_id = "{location_id}"
path = "{path}"
is_default = true
'''


def test_cross_host_projects_merge_locations_and_aliases(tmp_path: Path) -> None:
    first = parse_config(
        host_project_config(
            host_name="first",
            location_id=LOCATION_A,
            path=tmp_path / "first",
            alias="router",
        ),
        host_id=HOST_A,
    )
    second = parse_config(
        host_project_config(
            host_name="second",
            location_id=LOCATION_B,
            path=tmp_path / "second",
            alias="sessions",
        ),
        host_id=HOST_B,
    )
    merged = merge_project_catalogs([first, second])
    assert merged.projects[0].aliases == ("router", "sessions")
    assert {location.host_id for location in merged.locations} == {HOST_A, HOST_B}


def test_cross_host_global_conflict_is_visible(tmp_path: Path) -> None:
    first = parse_config(
        host_project_config(
            host_name="first",
            location_id=LOCATION_A,
            path=tmp_path / "first",
            alias="router",
        ),
        host_id=HOST_A,
    )
    second = parse_config(
        host_project_config(
            host_name="second",
            location_id=LOCATION_B,
            path=tmp_path / "second",
            alias="sessions",
            project_name="Different",
        ),
        host_id=HOST_B,
    )
    with pytest.raises(ConfigError, match="conflicting fields: name"):
        merge_project_catalogs([first, second])


def test_multiple_local_defaults_are_rejected(tmp_path: Path) -> None:
    document = host_project_config(
        host_name="first",
        location_id=LOCATION_A,
        path=tmp_path / "first",
        alias="router",
    )
    document += f'''
[[projects."{PROJECT}".locations]]
location_id = "{LOCATION_B}"
path = "{tmp_path / "second"}"
is_default = true
'''
    with pytest.raises(ConfigError, match="multiple defaults"):
        parse_config(document, host_id=HOST_A)
