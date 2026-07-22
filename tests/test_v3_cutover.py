from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from agent_switchboard._v3.cutover import (
    CutoverBundle,
    CutoverError,
    export_artifacts,
    export_legacy,
)
from agent_switchboard._v3.domain import (
    ActivationState,
    FrameId,
    GenerationId,
    WorkContextId,
)
from agent_switchboard._v3.generation import (
    CutoverEvidence,
    GenerationError,
    GenerationPaths,
    commit,
    import_bundle,
    initialize,
    open_generation,
    recover_incomplete,
    reset,
    resolve_current,
    rollback,
    status,
)
from agent_switchboard.config import parse_config as parse_legacy_config
from agent_switchboard.domain import HostId as LegacyHostId
from agent_switchboard.local import _project_catalog
from agent_switchboard.storage import Registry as LegacyRegistry

HOST = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
REPOSITORY = "33333333-3333-4333-8333-333333333333"
CHECKOUT = "44444444-4444-4444-8444-444444444444"
SESSION_ID = "55555555-5555-4555-8555-555555555555"
SESSION_KEY = f"{HOST}:codex:{SESSION_ID}"
HANDOFF = "66666666-6666-4666-8666-666666666666"
GENERATION = GenerationId("77777777-7777-4777-8777-777777777777")
GENERATION_2 = GenerationId("88888888-8888-4888-8888-888888888888")
REMOTE_HOST = "99999999-9999-4999-8999-999999999999"
REMOTE_GENERATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def cutover_evidence(
    generation: GenerationId = GENERATION_2, *, captured_at: int = 109
) -> CutoverEvidence:
    digest = "a" * 64
    checks = {
        name: digest
        for name in (
            "coreDoctor",
            "reconciliation",
            "stagedMutationBlock",
            "hostState",
            "navigatorState",
            "dmsModel",
            "dmsColdCache",
            "dmsWarmCache",
            "remoteOnline",
            "remoteOffline",
        )
    }
    return CutoverEvidence.from_dict(
        {
            "evidenceVersion": 1,
            "capturedAt": captured_at,
            "core": {
                "version": "0.3.0",
                "commit": "b" * 40,
                "artifactSha256": "c" * 64,
            },
            "dms": {
                "version": "0.5.0",
                "commit": "d" * 40,
                "artifactSha256": "e" * 64,
            },
            "hosts": [
                {
                    "role": "desktop_primary",
                    "hostId": HOST,
                    "generationId": str(generation),
                    "providerVersions": {"codex": "codex-cli 99.0.0"},
                    "stagedReads": {
                        "hostStateSha256": "f" * 64,
                        "navigatorStateSha256": "1" * 64,
                    },
                },
                {
                    "role": "remote_owner",
                    "hostId": REMOTE_HOST,
                    "generationId": REMOTE_GENERATION,
                    "providerVersions": {"claude": "2.1.216"},
                    "stagedReads": {
                        "hostStateSha256": "2" * 64,
                        "navigatorStateSha256": "3" * 64,
                    },
                },
            ],
            "dmsColdStart": {
                "hostId": HOST,
                "processStartId": "boot-id:1234:5678",
                "modelSha256": "4" * 64,
                "coldCacheSha256": "5" * 64,
                "warmCacheSha256": "6" * 64,
            },
            "checks": checks,
        }
    )


def legacy_config(checkout: Path) -> str:
    return f'''
config_version = 2

[host]
display_name = "starship"

[providers.codex]
enabled = true
executable = "/usr/bin/codex"

[providers.claude]
enabled = false

[remotes.snap]
ssh_target = "snap.lan"
display_name = "snap"

[projects."{PROJECT}"]
name = "Switchboard"
aliases = ["agent router"]
default_provider = "codex"
default_transport = "tmux"

[[projects."{PROJECT}".repositories]]
repository_id = "{REPOSITORY}"
name = "agent-switchboard"
kind = "git"
is_primary = true
context_sources = ["AGENTS.md", "docs"]

[[projects."{PROJECT}".repositories.checkouts]]
checkout_id = "{CHECKOUT}"
path = "{checkout}"
kind = "main"
display_name = "primary"
is_default = true

[defaults]
refresh_interval_seconds = 15
staleness_interval_seconds = 90

[tmux]
naming_prefix = "agent"
launch_timeout_seconds = 45

[hooks]
timeout_seconds = 2
latency_budget_ms = 300
'''


def seeded_legacy(
    tmp_path: Path, *, runtime_presence: str = "stopped"
) -> tuple[Path, str]:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    config_text = legacy_config(checkout)
    parsed = parse_legacy_config(config_text, host_id=LegacyHostId(HOST))
    database = tmp_path / "legacy.db"
    with LegacyRegistry(database) as registry:
        registry.upsert_host(HOST, "starship", is_local=True, observed_at=10)
        registry.materialize_projects(HOST, _project_catalog(parsed), observed_at=20)
        registry.upsert_session(
            {
                "session_key": SESSION_KEY,
                "host_id": HOST,
                "provider": "codex",
                "provider_session_id": SESSION_ID,
                "project_id": PROJECT,
                "checkout_id": CHECKOUT,
                "cwd": str(checkout),
                "name": "Phase 6",
                "name_source": "curated",
                "purpose": "Cut over safely",
                "runtime_presence": runtime_presence,
                "resumability": "resumable",
                "activity": "ready",
                "activity_reason": "turn_complete",
                "created_at": 30,
                "provider_updated_at": 35,
                "last_observed_at": 40,
            }
        )
        registry.append_handoff(
            session_key=SESSION_KEY,
            summary="Foundation complete",
            next_action="Build the view shell",
            source="agent",
            source_host_id=HOST,
            handoff_id=HANDOFF,
            created_at=45,
        )
        registry.create_task(
            task_id="99999999-9999-4999-8999-999999999999",
            host_id=HOST,
            project_id=PROJECT,
            checkout_id=CHECKOUT,
            title="Historical task",
            observed_at=50,
        )
    return database, config_text


def roots(tmp_path: Path) -> GenerationPaths:
    return GenerationPaths.from_xdg(tmp_path / "config", tmp_path / "state")


def test_exact_legacy_export_is_deterministic_strict_and_source_immutable(
    tmp_path: Path,
) -> None:
    database, config = seeded_legacy(tmp_path)
    before = database.read_bytes()
    first = export_legacy(database, config, exported_at=100)
    second = export_legacy(database, config, exported_at=100)
    assert first.to_json() == second.to_json()
    assert database.read_bytes() == before
    assert first.body["source"] == {
        "schemaVersion": 10,
        "protocolVersion": 2,
        "configVersion": 2,
        "hostId": HOST,
        "exportedAt": 100,
        "quiescent": True,
    }
    assert len(first.body["providerSessions"]) == 1
    assert len(first.body["handoffs"]) == 1
    assert len(first.body["historicalTasks"]) == 1
    assert CutoverBundle.from_json(first.to_json()) == first

    changed = json.loads(first.to_json())
    changed["providerSessions"][0]["name"] = "tampered"
    with pytest.raises(CutoverError, match="bundle_hash_mismatch"):
        CutoverBundle.from_json(json.dumps(changed))
    changed = json.loads(first.to_json())
    changed["unknown"] = True
    with pytest.raises(CutoverError, match="unknown fields"):
        CutoverBundle.from_json(json.dumps(changed))


def test_export_rejects_nonquiescent_and_incompatible_sources(tmp_path: Path) -> None:
    database, config = seeded_legacy(tmp_path, runtime_presence="live")
    with pytest.raises(CutoverError) as caught:
        export_legacy(database, config, exported_at=100)
    assert caught.value.code == "source_not_quiescent"
    with LegacyRegistry(database) as registry:
        registry.connection.execute("PRAGMA user_version = 9")
    with pytest.raises(CutoverError) as caught:
        export_legacy(database, config, exported_at=100)
    assert caught.value.code == "source_incompatible"


def test_export_artifacts_retain_exact_private_bundle_config_and_database(
    tmp_path: Path,
) -> None:
    database, config = seeded_legacy(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text(config)
    destination = tmp_path / "backup"
    bundle = export_artifacts(database, config_path, destination, exported_at=100)
    assert (
        CutoverBundle.from_json((destination / "cutover-bundle.json").read_bytes())
        == bundle
    )
    assert (destination / "legacy-config.toml").read_text() == config
    assert (destination.stat().st_mode & 0o777) == 0o500
    assert all((path.stat().st_mode & 0o777) == 0o400 for path in destination.iterdir())
    with pytest.raises(CutoverError) as caught:
        export_artifacts(database, config_path, destination, exported_at=100)
    assert caught.value.code == "export_destination_exists"


def test_import_builds_one_private_staged_generation_without_frames(
    tmp_path: Path,
) -> None:
    database, config = seeded_legacy(tmp_path)
    bundle = export_legacy(database, config, exported_at=100)
    paths = roots(tmp_path)
    imported = import_bundle(bundle, paths, generation_id=GENERATION)
    assert imported.activation_state is ActivationState.CUTOVER_STAGED
    assert resolve_current(paths) == GENERATION
    assert os.readlink(paths.current) == f"generations/{GENERATION}"
    with open_generation(paths) as opened:
        assert opened.config.generation_id == GENERATION
        assert opened.registry.metadata()["generation_id"] == str(GENERATION)
        assert (
            opened.registry.connection.execute(
                "SELECT count(*) FROM provider_sessions"
            ).fetchone()[0]
            == 1
        )
        assert (
            opened.registry.connection.execute(
                "SELECT count(*) FROM session_handoffs"
            ).fetchone()[0]
            == 1
        )
        assert (
            opened.registry.connection.execute(
                "SELECT count(*) FROM frames"
            ).fetchone()[0]
            == 0
        )
        with pytest.raises(GenerationError) as caught:
            opened.require_mutation("view open")
        assert caught.value.code == "cutover_staged"
    assert (
        paths.state_generation(GENERATION) / "cutover-bundle.json"
    ).stat().st_mode & 0o777 == 0o400


def test_fresh_init_and_confirmed_reset_publish_empty_committed_generations(
    tmp_path: Path,
) -> None:
    database, config = seeded_legacy(tmp_path)
    template = export_legacy(database, config, exported_at=100).target_config(
        GENERATION
    )
    paths = roots(tmp_path)

    initialized = initialize(template, paths, created_at=101)
    assert initialized.activation_state is ActivationState.COMMITTED
    assert initialized.source_kind == "fresh"
    assert initialized.previous_generation_id is None
    assert initialized.evidence_sha256 is None
    with open_generation(paths) as opened:
        assert opened.registry.metadata()["committed_at"] == 101
        assert (
            opened.registry.connection.execute(
                "SELECT count(*) FROM provider_sessions"
            ).fetchone()[0]
            == 0
        )
        opened.registry.ensure_workspace(
            WorkContextId("aaaaaaaa-1111-4111-8111-111111111111"),
            FrameId("bbbbbbbb-1111-4111-8111-111111111111"),
            template.host.host_id,
            template.projects[0].project_id,
            template.checkouts[0].checkout_id,
            "Disposable workspace",
            now=102,
        )

    with pytest.raises(GenerationError) as caught:
        initialize(replace(template, generation_id=GENERATION_2), paths, created_at=103)
    assert caught.value.code == "generation_active"
    with pytest.raises(GenerationError) as caught:
        commit(paths, cutover_evidence(GENERATION, captured_at=102), committed_at=103)
    assert caught.value.code == "cutover_not_applicable"
    assert not (paths.state_generation(GENERATION) / "cutover-evidence.json").exists()

    replacement = replace(template, generation_id=GENERATION_2)
    replaced = reset(
        replacement,
        paths,
        expected_current=GENERATION,
        created_at=104,
    )
    assert replaced.generation_id == GENERATION_2
    assert replaced.previous_generation_id == GENERATION
    with open_generation(paths) as opened:
        assert (
            opened.registry.connection.execute(
                "SELECT count(*) FROM frames"
            ).fetchone()[0]
            == 0
        )
    old_database = paths.state_generation(GENERATION) / "switchboard.db"
    with sqlite3.connect(old_database) as old:
        assert old.execute("SELECT count(*) FROM frames").fetchone()[0] == 1

    with pytest.raises(GenerationError) as caught:
        reset(
            replace(template, generation_id=GenerationId.new()),
            paths,
            expected_current=GENERATION,
            created_at=105,
        )
    assert caught.value.code == "generation_changed"
    assert resolve_current(paths) == GENERATION_2


@pytest.mark.parametrize(
    "boundary",
    ["files_fsynced", "config_published", "state_published", "pointer_switched"],
)
def test_fresh_init_crash_recovery_never_exposes_torn_state(
    tmp_path: Path, boundary: str
) -> None:
    database, config = seeded_legacy(tmp_path)
    template = export_legacy(database, config, exported_at=100).target_config(
        GENERATION
    )
    paths = roots(tmp_path)

    def fail(current: str) -> None:
        if current == boundary:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        initialize(template, paths, created_at=101, fault_injector=fail)
    if boundary == "pointer_switched":
        assert status(paths).source_kind == "fresh"
    else:
        assert not paths.current.exists()
    recover_incomplete(paths)
    if boundary != "pointer_switched":
        assert not paths.config_generation(GENERATION).exists()
        assert not paths.state_generation(GENERATION).exists()


def test_precommit_rollback_and_commit_are_exact_boundaries(tmp_path: Path) -> None:
    database, config = seeded_legacy(tmp_path)
    bundle = export_legacy(database, config, exported_at=100)
    paths = roots(tmp_path)
    import_bundle(bundle, paths, generation_id=GENERATION)
    assert rollback(paths) is None
    assert not paths.current.exists()

    import_bundle(bundle, paths, generation_id=GENERATION_2)
    with pytest.raises(GenerationError) as caught:
        commit(
            paths,
            cutover_evidence(GENERATION),
            committed_at=110,
        )
    assert caught.value.code == "cutover_evidence_invalid"
    committed = commit(
        paths,
        cutover_evidence(),
        committed_at=110,
    )
    assert committed.activation_state is ActivationState.COMMITTED
    assert committed.evidence_sha256 == cutover_evidence().sha256
    evidence_path = paths.state_generation(GENERATION_2) / "cutover-evidence.json"
    assert evidence_path.stat().st_mode & 0o777 == 0o400
    assert CutoverEvidence.from_json(evidence_path.read_bytes()) == cutover_evidence()
    with open_generation(paths) as opened:
        opened.require_mutation("view open")
    with pytest.raises(GenerationError) as caught:
        rollback(paths)
    assert caught.value.code == "cutover_committed"


@pytest.mark.parametrize(
    "boundary",
    ["files_fsynced", "config_published", "state_published", "pointer_switched"],
)
def test_activation_crash_boundaries_never_expose_a_torn_generation(
    tmp_path: Path, boundary: str
) -> None:
    database, config = seeded_legacy(tmp_path)
    bundle = export_legacy(database, config, exported_at=100)
    paths = roots(tmp_path)

    def fail(current: str) -> None:
        if current == boundary:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        import_bundle(
            bundle,
            paths,
            generation_id=GENERATION,
            fault_injector=fail,
        )
    if boundary == "pointer_switched":
        assert status(paths).activation_state is ActivationState.CUTOVER_STAGED
    else:
        assert not paths.current.exists()
    recover_incomplete(paths)
    if boundary != "pointer_switched":
        assert not paths.config_generation(GENERATION).exists()
        assert not paths.state_generation(GENERATION).exists()
        retried = import_bundle(bundle, paths, generation_id=GENERATION)
        assert retried.activation_state is ActivationState.CUTOVER_STAGED


def test_open_rejects_torn_pointer_and_generation_mismatch(tmp_path: Path) -> None:
    paths = roots(tmp_path)
    paths.state_root.mkdir(parents=True)
    os.symlink("../../escape", paths.current)
    with pytest.raises(GenerationError) as caught:
        resolve_current(paths)
    assert caught.value.code == "generation_pointer_invalid"
