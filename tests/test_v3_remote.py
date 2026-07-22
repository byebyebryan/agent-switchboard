from __future__ import annotations

import asyncio
from hashlib import sha256

import pytest

from agent_switchboard._v3.config import parse_config
from agent_switchboard._v3.domain import (
    GenerationId,
    HostId,
    HostStateCache,
    Reachability,
)
from agent_switchboard._v3.process import CommandOutput
from agent_switchboard._v3.protocol import (
    DirectiveKind,
    PresentationDirective,
    build_host_state,
)
from agent_switchboard._v3.remote import (
    RemoteError,
    RemoteRuntime,
    action_argv,
    host_state_argv,
)
from agent_switchboard._v3.storage import Registry

GENERATION = GenerationId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
LOCAL = HostId("11111111-1111-4111-8111-111111111111")
REMOTE = HostId("22222222-2222-4222-8222-222222222222")
VIEW = "33333333-3333-4333-8333-333333333333"
REQUEST = "44444444-4444-4444-8444-444444444444"


def config():
    return parse_config(
        f'''config_version = 3
generation_id = "{GENERATION}"
[host]
host_id = "{LOCAL}"
display_name = "local"
[remotes.snap]
ssh_target = "snap.lan"
display_name = "snap"
'''
    )


def host_state(host_id: HostId, display: str) -> bytes:
    with Registry(
        ":memory:",
        generation_id=GENERATION,
        local_host_id=host_id,
        local_display_name=display,
        now=1,
    ) as registry:
        return (build_host_state(registry, generated_at=10).to_json() + "\n").encode()


def test_fixed_ssh_argv_has_no_shell_or_ui_derived_endpoint() -> None:
    remote = config().remotes[0]
    assert host_state_argv(remote) == (
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "--",
        "snap.lan",
        "swbctl",
        "state",
        "host",
        "--json",
    )
    assert action_argv(remote, ("view", "open", "--host", str(REMOTE)))[:8] == (
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "--",
        "snap.lan",
    )
    with pytest.raises(RemoteError, match="unsafe"):
        action_argv(remote, ("view", "open", "--bad-option"))


def test_refresh_pins_validated_host_and_retains_last_good_on_failure() -> None:
    calls = 0

    async def runner(_argv):
        nonlocal calls
        calls += 1
        if calls == 1:
            return CommandOutput(host_state(REMOTE, "snap"), b"", 0)
        return CommandOutput(b"", b"offline", 255)

    with Registry(
        ":memory:",
        generation_id=GENERATION,
        local_host_id=LOCAL,
        local_display_name="local",
        now=1,
    ) as registry:
        runtime = RemoteRuntime(config(), registry, runner=runner)
        first = asyncio.run(runtime.refresh(now=20))[0]
        assert first.host_id == REMOTE
        assert first.reachability is Reachability.ONLINE
        second = asyncio.run(runtime.refresh(now=30))[0]
        assert second.host_id == REMOTE
        assert second.reachability is Reachability.OFFLINE
        cached = registry.cached_host_states()[0]
        assert cached.host_id == REMOTE
        assert cached.received_at == 20
        assert cached.last_attempt_at == 30
        assert cached.state_json == host_state(REMOTE, "snap").decode().strip()


def test_remote_directive_routes_by_pinned_host_and_revalidates_identity() -> None:
    directive = PresentationDirective(
        REQUEST,
        str(REMOTE),
        DirectiveKind.FOCUS,
        VIEW,
        2,
        "opaque-desktop-token",
    )

    async def runner(argv):
        assert argv[7] == "snap.lan"
        return CommandOutput((directive.to_json() + "\n").encode(), b"", 0)

    with Registry(
        ":memory:",
        generation_id=GENERATION,
        local_host_id=LOCAL,
        local_display_name="local",
        now=1,
    ) as registry:
        payload = host_state(REMOTE, "snap").decode().strip()
        registry.cache_host_state(
            HostStateCache(
                "snap",
                REMOTE,
                payload,
                sha256(payload.encode()).hexdigest(),
                10,
                20,
                20,
                Reachability.ONLINE,
                None,
            )
        )
        received = asyncio.run(
            RemoteRuntime(config(), registry, runner=runner).directive(
                REMOTE, ("view", "open", "--host", str(REMOTE))
            )
        )
        assert received == directive

        other = HostId("55555555-5555-4555-8555-555555555555")
        with pytest.raises(RemoteError, match="pinned"):
            asyncio.run(
                RemoteRuntime(config(), registry, runner=runner).directive(
                    other, ("view", "open", "--host", str(other))
                )
            )
