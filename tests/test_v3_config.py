from __future__ import annotations

from pathlib import Path

import pytest

from agent_switchboard._v3.config import (
    ConfigError,
    parse_config,
    parse_config_template,
    render_config,
)
from agent_switchboard._v3.domain import (
    CompleteReturnPolicy,
    ControlTurnPolicy,
    GenerationId,
    ProviderId,
    TaskPushPolicy,
    ViewMode,
)

GENERATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
HOST = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
REPOSITORY = "33333333-3333-4333-8333-333333333333"
CHECKOUT = "44444444-4444-4444-8444-444444444444"


def minimal_config() -> str:
    return f'''
config_version = 3
generation_id = "{GENERATION}"

[host]
host_id = "{HOST}"
display_name = "starship"
'''


def full_config(path: Path) -> str:
    return f'''
config_version = 3
generation_id = "{GENERATION}"

[host]
host_id = "{HOST}"
display_name = "starship"

[providers.codex]
enabled = true
executable = "/opt/switchboard/bin/codex"

[providers.claude]
enabled = false

[remotes.snap]
ssh_target = "bryan@snap.lan"
display_name = "snap"

[projects."{PROJECT}"]
name = "Switchboard"
aliases = [" Agent   Router ", "agent router", "Sessions"]
default_provider = "codex"
task_push = "off"
complete_return = "handoff"

[[projects."{PROJECT}".repositories]]
repository_id = "{REPOSITORY}"
name = "agent-switchboard"
kind = "git"
is_primary = true
context_sources = ["AGENTS.md", "docs", "docs"]

[[projects."{PROJECT}".repositories.checkouts]]
checkout_id = "{CHECKOUT}"
path = "{path}"
kind = "main"
display_name = "primary"
provider_override = "claude"
is_default = true

[defaults]
refresh_interval_seconds = 15
staleness_interval_seconds = 90

[views]
cli_default_mode = "navigator"
desktop_default_mode = "direct"

[automation]
task_push = "off"
complete_return = "handoff"
initial_max_depth = 1

[control_turns]
transport = "resume_only"
watchdog_timeout_seconds = 9

[tmux]
naming_prefix = "agent"
launch_timeout_seconds = 45

[hooks]
timeout_seconds = 2
latency_budget_ms = 300

[memory]
enabled = true
command = ["memory-bridge", "serve"]
tool = "search"
timeout_seconds = 6
'''


def test_minimal_v3_configuration_has_phase6_defaults() -> None:
    config = parse_config(minimal_config())
    assert str(config.generation_id) == GENERATION
    assert str(config.host.host_id) == HOST
    assert [provider.provider for provider in config.providers] == [
        ProviderId.CODEX,
        ProviderId.CLAUDE,
    ]
    assert all(provider.enabled for provider in config.providers)
    assert config.views.cli_default_mode is ViewMode.NAVIGATOR
    assert config.views.desktop_default_mode is ViewMode.NAVIGATOR
    assert config.automation.task_push is TaskPushPolicy.CONSERVATIVE
    assert config.automation.complete_return is CompleteReturnPolicy.SYNTHESIZE
    assert config.control_turns.transport is ControlTurnPolicy.LIVE_FIRST
    assert config.control_turns.watchdog_timeout_seconds == 5
    assert config.hooks.timeout_seconds == 10
    assert not config.memory.enabled


def test_full_v3_configuration_is_typed_canonical_and_round_trips(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    config = parse_config(full_config(checkout))
    assert config.projects[0].aliases == ("Agent Router", "Sessions")
    assert config.projects[0].task_push is TaskPushPolicy.OFF
    assert config.projects[0].complete_return is CompleteReturnPolicy.HANDOFF
    assert config.repositories[0].context_sources == ("AGENTS.md", "docs")
    assert config.checkouts[0].path == checkout
    assert config.checkouts[0].provider_override is ProviderId.CLAUDE
    assert config.views.cli_default_mode is ViewMode.NAVIGATOR
    assert config.control_turns.transport is ControlTurnPolicy.RESUME_ONLY
    assert config.control_turns.watchdog_timeout_seconds == 9
    assert render_config(config).startswith("config_version = 3\n")
    assert parse_config(render_config(config)) == config


def test_init_template_may_omit_or_rebind_generation() -> None:
    allocated = GenerationId("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    without = minimal_config().replace(f'generation_id = "{GENERATION}"\n', "")
    assert parse_config_template(without, allocated).generation_id == allocated
    assert parse_config_template(minimal_config(), allocated).generation_id == allocated


@pytest.mark.parametrize(
    "document",
    [
        minimal_config().replace("config_version = 3", "config_version = 2"),
        minimal_config().replace(f'generation_id = "{GENERATION}"\n', ""),
        minimal_config().replace(f'host_id = "{HOST}"\n', ""),
        minimal_config() + "\n[unknown]\nvalue = true\n",
        minimal_config() + '\n[defaults]\nworking_directory = "current"\n',
        minimal_config() + "\n[defaults]\nrecent_parked_limit = 10\n",
    ],
)
def test_v3_rejects_old_missing_unknown_and_task_first_configuration(
    document: str,
) -> None:
    with pytest.raises(ConfigError):
        parse_config(document)


def test_memory_is_disabled_by_default_and_requires_explicit_command() -> None:
    with pytest.raises(ConfigError, match="command"):
        parse_config(minimal_config() + "\n[memory]\nenabled = true\n")
