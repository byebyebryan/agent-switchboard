from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_switchboard.config import RemoteConfig, parse_config
from agent_switchboard.domain import HostId
from agent_switchboard.protocol import (
    FleetEnvelope,
    PresentationPlanEnvelope,
    SnapshotEnvelope,
)
from agent_switchboard.remote import (
    MAX_CONCURRENT_SSH,
    REMOTE_SNAPSHOT_TIMEOUT_SECONDS,
    RemoteError,
    action_ssh_argv,
    attach_ssh_argv,
    build_fleet_envelope,
    fetch_remote_snapshot,
    fetch_remote_snapshots,
    invoke_remote_empty,
    invoke_remote_json,
    refresh_remote_cache,
    resolve_remote_host,
    snapshot_ssh_argv,
)
from agent_switchboard.storage import Registry
from agent_switchboard.tui_gateway import CommandOutput

FIXTURE = Path(__file__).parent / "fixtures/protocol/v2/snapshot.json"
PLAN_FIXTURE = Path(__file__).parent / "fixtures/protocol/v2/presentation-plan.json"
LOCAL_HOST = HostId("11111111-1111-4111-8111-111111111111")
REMOTE_HOST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SECOND_REMOTE_HOST = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
REMOTE_CHECKOUT = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def snapshot_bytes(host_id: str, display_name: str, generated_at: int) -> bytes:
    value = json.loads(FIXTURE.read_text())
    original = value["host"]["hostId"]
    value = json.loads(json.dumps(value).replace(original, host_id))
    if host_id != str(LOCAL_HOST):
        original_checkout = value["checkouts"][0]["checkoutId"]
        value = json.loads(
            json.dumps(value).replace(original_checkout, REMOTE_CHECKOUT)
        )
    value["host"]["displayName"] = display_name
    value["generatedAt"] = generated_at
    return json.dumps(value, separators=(",", ":")).encode()


def test_snapshot_ssh_argv_is_exact_and_shell_free() -> None:
    remote = RemoteConfig("snap", "bryan@snap.lan", "snap")
    assert snapshot_ssh_argv(remote, refresh=True) == (
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "--",
        "bryan@snap.lan",
        "swbctl",
        "snapshot",
        "--reconcile",
        "full",
        "--json",
    )


def test_remote_action_argv_is_exact_and_rejects_unsafe_tokens() -> None:
    remote = RemoteConfig("snap", "bryan@snap.lan", "snap")
    arguments = (
        "prepare-open",
        f"{REMOTE_HOST}:codex:55555555-5555-4555-8555-555555555555",
        "--request-id",
        "77777777-7777-4777-8777-777777777777",
        "--has-current-terminal",
        "--json",
    )
    assert action_ssh_argv(remote, arguments) == (
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "--",
        "bryan@snap.lan",
        "swbctl",
        *arguments,
    )
    reopen_arguments = (
        "prepare-task",
        "66666666-6666-4666-8666-666666666666",
        "--reopen",
        "--request-id",
        "77777777-7777-4777-8777-777777777777",
        "--json",
    )
    assert action_ssh_argv(remote, reopen_arguments) == (
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "--",
        "bryan@snap.lan",
        "swbctl",
        *reopen_arguments,
    )
    assert attach_ssh_argv(
        remote,
        "33333333-3333-4333-8333-333333333333",
    ) == (
        "ssh",
        "-tt",
        "--",
        "bryan@snap.lan",
        "swbctl",
        "attach-surface",
        "33333333-3333-4333-8333-333333333333",
    )
    with pytest.raises(RemoteError, match="safe bounded token"):
        action_ssh_argv(remote, ("prepare-open", "bad; touch /tmp/injected"))
    with pytest.raises(RemoteError, match="safe bounded token"):
        action_ssh_argv(remote, ("select-surface", "id", "--evil"))


def test_remote_snapshot_success_and_nonzero_are_bounded() -> None:
    calls: list[tuple[tuple[str, ...], float, bytes | None]] = []

    async def successful(argv, timeout, stdin):
        calls.append((tuple(argv), timeout, stdin))
        return CommandOutput(snapshot_bytes(REMOTE_HOST, "snap", 100), b"", 0)

    result = asyncio.run(
        fetch_remote_snapshot(
            RemoteConfig("snap", "snap.lan", "snap"),
            refresh=False,
            runner=successful,
            clock=lambda: 110,
        )
    )
    assert result.snapshot is not None
    assert result.snapshot.host.host_id == HostId(REMOTE_HOST)
    assert result.error is None
    assert calls[0][1:] == (REMOTE_SNAPSHOT_TIMEOUT_SECONDS, None)

    async def failed(argv, timeout, stdin):
        return CommandOutput(b"", b"private ssh detail", 255)

    failure = asyncio.run(
        fetch_remote_snapshot(
            RemoteConfig("snap", "snap.lan", "snap"),
            refresh=True,
            runner=failed,
            clock=lambda: 120,
        )
    )
    assert failure.snapshot is None
    assert failure.error is not None
    assert failure.error.code == "ssh_failed"
    assert "private ssh detail" not in failure.error.message


def test_remote_action_validates_json_and_empty_responses() -> None:
    remote = RemoteConfig("snap", "snap.lan", "snap")
    plan_value = json.loads(PLAN_FIXTURE.read_text())
    plan_value = json.loads(
        json.dumps(plan_value).replace(str(LOCAL_HOST), REMOTE_HOST)
    )
    plan_record = (
        PresentationPlanEnvelope.from_dict(plan_value).to_json().encode() + b"\n"
    )
    calls: list[tuple[tuple[str, ...], bytes | None]] = []

    async def runner(argv, timeout, stdin):
        calls.append((tuple(argv), stdin))
        output = plan_record if argv[9] == "prepare-open" else b""
        return CommandOutput(output, b"", 0)

    async def exercise() -> None:
        envelope = await invoke_remote_json(
            remote,
            (
                "prepare-open",
                f"{REMOTE_HOST}:codex:55555555-5555-4555-8555-555555555555",
                "--request-id",
                "77777777-7777-4777-8777-777777777777",
                "--json",
            ),
            PresentationPlanEnvelope.from_json,
            stdin=b'{"bounded":true}',
            runner=runner,
        )
        assert str(envelope.plan.host_id) == REMOTE_HOST
        await invoke_remote_empty(
            remote,
            (
                "select-surface",
                "33333333-3333-4333-8333-333333333333",
                "--client",
                "/dev/pts/7",
            ),
            runner=runner,
        )

    asyncio.run(exercise())
    assert calls[0][1] == b'{"bounded":true}'
    assert calls[1][1] is None


def test_resolve_remote_host_requires_one_declared_pinned_endpoint(tmp_path) -> None:
    config = parse_config(
        """
config_version = 2
[remotes.snap]
ssh_target = "snap.lan"
display_name = "snap"
""",
        host_id=LOCAL_HOST,
    )
    with Registry(tmp_path / "switchboard.db") as registry:
        registry.upsert_remote("snap", "snap.lan", "snap", observed_at=1)
        with pytest.raises(RemoteError) as unknown:
            resolve_remote_host(registry, config, HostId(REMOTE_HOST))
        assert unknown.value.code == "remote_host_unknown"
        registry.store_remote_snapshot(
            "snap",
            json.loads(snapshot_bytes(REMOTE_HOST, "snap", 10)),
            remote_host_id=REMOTE_HOST,
            schema_version=2,
            protocol_version=2,
            observed_at=10,
            received_at=11,
        )
        resolved = resolve_remote_host(registry, config, HostId(REMOTE_HOST))
    assert resolved.ssh_target == "snap.lan"


def test_remote_snapshot_concurrency_is_bounded() -> None:
    active = 0
    peak = 0

    async def runner(argv, timeout, stdin):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return CommandOutput(snapshot_bytes(REMOTE_HOST, "snap", 100), b"", 0)

    remotes = tuple(
        RemoteConfig(f"remote-{index}", f"remote-{index}.lan", f"remote-{index}")
        for index in range(MAX_CONCURRENT_SSH + 3)
    )
    results = asyncio.run(
        fetch_remote_snapshots(
            remotes,
            refresh=False,
            runner=runner,
            clock=lambda: 110,
        )
    )
    assert len(results) == len(remotes)
    assert peak == MAX_CONCURRENT_SSH


def test_refresh_cache_retains_failure_and_builds_canonical_fleet(tmp_path) -> None:
    config = parse_config(
        """
config_version = 2
[remotes.down]
ssh_target = "down.lan"
display_name = "down"
[remotes.snap]
ssh_target = "snap.lan"
display_name = "snap"
""",
        host_id=LOCAL_HOST,
    )

    async def runner(argv, timeout, stdin):
        target = argv[7]
        if target == "down.lan":
            return CommandOutput(b"", b"connection refused", 255)
        return CommandOutput(snapshot_bytes(REMOTE_HOST, "snap", 100), b"", 0)

    timestamps = iter((90, 120, 121))
    with Registry(tmp_path / "switchboard.db") as registry:
        refresh_remote_cache(
            registry,
            config,
            local_host_id=LOCAL_HOST,
            runner=runner,
            clock=lambda: next(timestamps),
        )
        down = registry.get_remote("down")
        snap = registry.get_remote("snap")
        assert down is not None and down["reachability"] == "offline"
        assert down["snapshot_json"] is None
        assert snap is not None and snap["reachability"] == "online"

        local = SnapshotEnvelope.from_json(
            snapshot_bytes(str(LOCAL_HOST), "local", 125)
        )
        fleet = build_fleet_envelope(
            local,
            registry.list_remotes(declared_only=True),
            generated_at=300_000,
            staleness_interval_seconds=120,
        )
    assert FleetEnvelope.from_json(fleet.to_json()) == fleet
    assert [host.remote_name for host in fleet.hosts] == [None, "down", "snap"]
    assert fleet.hosts[1].snapshot is None
    assert fleet.hosts[2].snapshot is not None
    assert fleet.hosts[2].stale is True


def test_fleet_excludes_conflicting_remote_catalog_without_erasing_cache(
    tmp_path,
) -> None:
    local = SnapshotEnvelope.from_json(snapshot_bytes(str(LOCAL_HOST), "local", 100))
    remote_value = json.loads(snapshot_bytes(REMOTE_HOST, "snap", 110))
    remote_value["projects"][0]["name"] = "conflicting project"
    with Registry(tmp_path / "switchboard.db") as registry:
        registry.upsert_remote("snap", "snap.lan", "snap", observed_at=100)
        registry.store_remote_snapshot(
            "snap",
            remote_value,
            remote_host_id=REMOTE_HOST,
            schema_version=2,
            protocol_version=2,
            observed_at=110,
            received_at=120,
        )
        fleet = build_fleet_envelope(
            local,
            registry.list_remotes(declared_only=True),
            generated_at=130,
            staleness_interval_seconds=120,
        )
        retained = registry.get_remote("snap")
    assert retained is not None and retained["snapshot_json"] is not None
    assert fleet.hosts[1].snapshot is None
    assert fleet.hosts[1].host_id is None
    assert fleet.hosts[1].error is not None
    assert fleet.hosts[1].error.code == "remote_catalog_conflict"


def test_fleet_accepts_matching_catalog_with_host_local_observation_fields(
    tmp_path,
) -> None:
    local = SnapshotEnvelope.from_json(snapshot_bytes(str(LOCAL_HOST), "local", 100))
    remote_value = json.loads(snapshot_bytes(REMOTE_HOST, "snap", 110))
    for collection in ("projects", "repositories"):
        remote_value[collection][0]["createdAt"] = 101
        remote_value[collection][0]["updatedAt"] = 102
        remote_value[collection][0]["declared"] = False
    with Registry(tmp_path / "switchboard.db") as registry:
        registry.upsert_remote("snap", "snap.lan", "snap", observed_at=100)
        registry.store_remote_snapshot(
            "snap",
            remote_value,
            remote_host_id=REMOTE_HOST,
            schema_version=2,
            protocol_version=2,
            observed_at=110,
            received_at=120,
        )
        fleet = build_fleet_envelope(
            local,
            registry.list_remotes(declared_only=True),
            generated_at=130,
            staleness_interval_seconds=120,
        )
    assert fleet.hosts[1].host_id == HostId(REMOTE_HOST)
    assert fleet.hosts[1].snapshot is not None
    assert fleet.hosts[1].error is None
