from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_switchboard._v3.domain import GenerationId
from agent_switchboard._v3.generation import CutoverEvidence

SCRIPT = Path(__file__).parents[1] / "scripts" / "phase6e_cutover.py"
SPEC = importlib.util.spec_from_file_location("phase6e_cutover", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
phase6e = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = phase6e
SPEC.loader.exec_module(phase6e)

LOCAL_HOST = "040f6a81-67b6-42ce-b7ca-2068bb190e88"
REMOTE_HOST = "140f6a81-67b6-42ce-b7ca-2068bb190e88"
LOCAL_GENERATION = "240f6a81-67b6-42ce-b7ca-2068bb190e88"
REMOTE_GENERATION = "340f6a81-67b6-42ce-b7ca-2068bb190e88"
SESSION = "019f6a67-a897-7661-97c5-41ca255d1284"
PROJECT = "e1405d26-89de-42a6-9d29-7514f8203b31"


def spec_value(tmp_path: Path) -> dict[str, object]:
    def host(
        role: str, host_id: str, generation_id: str, ssh_target: str | None
    ) -> dict[str, object]:
        root = tmp_path / role
        return {
            "role": role,
            "hostId": host_id,
            "generationId": generation_id,
            "sshTarget": ssh_target,
            "python": "/usr/bin/python",
            "legacySwbctl": str(root / "legacy-swbctl"),
            "legacyDatabase": str(root / "legacy.db"),
            "legacyConfig": str(root / "legacy.toml"),
            "configRoot": str(root / "config"),
            "stateRoot": str(root / "state"),
            "releaseRoot": str(root / "releases"),
            "binLink": str(root / "bin" / "swbctl"),
            "backupRoot": str(root / "backups"),
            "providerExecutables": {"codex": "/usr/bin/codex"},
            "hookFiles": {"codex": str(root / "codex" / "hooks.json")},
            "projectId": PROJECT,
            "stopSessions": [f"{host_id}:codex:{SESSION}"],
        }

    return {
        "executorVersion": 1,
        "cutoverId": "440f6a81-67b6-42ce-b7ca-2068bb190e88",
        "coreCommit": "a" * 40,
        "dmsCommit": "b" * 40,
        "sourceDateEpoch": 1_700_000_000,
        "workspace": str(tmp_path / "workspace"),
        "coreRepo": str(tmp_path / "core"),
        "desktop": {
            "dmsRepo": str(tmp_path / "dms"),
            "pluginDir": str(tmp_path / "plugins"),
            "pluginState": str(tmp_path / "switchboard_state.json"),
            "pluginSettings": str(tmp_path / "plugin_settings.json"),
            "service": "dms.service",
        },
        "hosts": [
            host("desktop_primary", LOCAL_HOST, LOCAL_GENERATION, None),
            host("remote_owner", REMOTE_HOST, REMOTE_GENERATION, "snap.lan"),
        ],
        "currentSessionKey": f"{LOCAL_HOST}:codex:{SESSION}",
    }


def parsed_spec(tmp_path: Path, value: dict[str, object] | None = None):
    path = tmp_path / "phase6e.json"
    path.write_text(json.dumps(value or spec_value(tmp_path)), encoding="utf-8")
    return phase6e.Spec.from_path(path)


def test_spec_is_strict_and_binds_the_exact_local_session(tmp_path: Path) -> None:
    parsed = parsed_spec(tmp_path)
    assert parsed.host("desktop_primary").hook_files["codex"].name == "hooks.json"
    assert parsed.current_session_key == f"{LOCAL_HOST}:codex:{SESSION}"

    wrong = spec_value(tmp_path)
    wrong["currentSessionKey"] = f"{REMOTE_HOST}:codex:{SESSION}"
    with pytest.raises(phase6e.CutoverFailure, match="desktop_primary"):
        parsed_spec(tmp_path, wrong)

    extra = spec_value(tmp_path)
    extra["compatibilityMode"] = True
    with pytest.raises(phase6e.CutoverFailure, match="fields are incompatible"):
        parsed_spec(tmp_path, extra)


def test_evidence_aggregates_both_hosts_and_passes_core_contract(
    tmp_path: Path,
) -> None:
    spec = parsed_spec(tmp_path)

    def sha(character: str) -> str:
        return character * 64

    host_checks = {
        "coreDoctor": sha("1"),
        "reconciliation": sha("2"),
        "stagedMutationBlock": sha("3"),
        "hostState": sha("4"),
        "navigatorState": sha("5"),
    }
    validations = {
        role: {
            "hostId": spec.host(role).host_id,
            "generationId": spec.host(role).generation_id,
            "providerVersions": {"codex": "codex-cli 1.2.3"},
            "hostStateSha256": sha("6" if role == "desktop_primary" else "7"),
            "navigatorStateSha256": sha("8" if role == "desktop_primary" else "9"),
            "checks": dict(host_checks),
        }
        for role in phase6e.ROLES
    }
    dms = {
        "hostId": LOCAL_HOST,
        "processStartId": "boot:invocation:123:456",
        "modelSha256": sha("a"),
        "coldCacheSha256": sha("b"),
        "warmCacheSha256": sha("c"),
        "checks": {
            "dmsModel": sha("d"),
            "dmsColdCache": sha("e"),
            "dmsWarmCache": sha("f"),
        },
    }
    prepared = {
        "coreArtifactSha256": sha("a"),
        "dmsArtifactSha256": sha("b"),
    }
    value = phase6e.evidence(
        spec,
        prepared,
        validations,
        dms,
        {"remoteOnline": sha("c"), "remoteOffline": sha("d")},
    )
    accepted = CutoverEvidence.from_dict(value)
    assert accepted.includes_generation(GenerationId(LOCAL_GENERATION))
    assert value["checks"]["coreDoctor"] != host_checks["coreDoctor"]


def test_evidence_rejects_substituted_offline_read(tmp_path: Path) -> None:
    spec = parsed_spec(tmp_path)
    with pytest.raises(phase6e.CutoverFailure, match="must be distinct"):
        phase6e.evidence(
            spec,
            {},
            {},
            {},
            {"remoteOnline": "1" * 64, "remoteOffline": "1" * 64},
        )


def test_navigator_reachability_is_exact() -> None:
    raw = phase6e.canonical(
        {
            "hosts": [
                {"hostId": LOCAL_HOST, "reachability": "local"},
                {"hostId": REMOTE_HOST, "reachability": "offline"},
            ]
        }
    )
    assert phase6e.navigator_reachability(raw, REMOTE_HOST) == "offline"
    with pytest.raises(phase6e.CutoverFailure, match="missing after refresh"):
        phase6e.navigator_reachability(raw, SESSION)


def test_execute_guard_rejects_managed_provider_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "TMUX",
        "AGENT_SWITCHBOARD_CAPABILITY",
        "AGENT_SWITCHBOARD_LAUNCH_ID",
        "AGENT_SWITCHBOARD_SURFACE_ID",
        "SWB_V3_SESSION_KEY",
        "SWB_V3_CONFIG_ROOT",
        "SWB_V3_STATE_ROOT",
        "SWB_V3_MCP_COMMAND",
    ):
        monkeypatch.delenv(name, raising=False)
    phase6e.plain_shell_guard()
    monkeypatch.setenv("TMUX", "/tmp/tmux,1,2")
    with pytest.raises(phase6e.CutoverFailure, match="plain shell"):
        phase6e.plain_shell_guard()


def test_atomic_symlink_replacement(tmp_path: Path) -> None:
    destination = tmp_path / "bin" / "swbctl"
    phase6e.replace_symlink(destination, "/opt/swbctl-0.3/bin/swbctl")
    assert destination.is_symlink()
    assert os.readlink(destination) == "/opt/swbctl-0.3/bin/swbctl"
    phase6e.replace_symlink(destination, "/opt/swbctl-next/bin/swbctl")
    assert os.readlink(destination) == "/opt/swbctl-next/bin/swbctl"


def test_restore_file_preserves_recorded_live_mode(tmp_path: Path) -> None:
    backup = tmp_path / "backup.json"
    backup.write_text("before\n", encoding="utf-8")
    backup.chmod(0o400)
    destination = tmp_path / "live.json"
    destination.write_text("after\n", encoding="utf-8")

    phase6e.restore_file(backup, destination, mode=0o640)

    assert destination.read_text(encoding="utf-8") == "before\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640


def test_restore_sqlite_replaces_mutation_and_stale_companions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE values_table(value TEXT NOT NULL)")
        connection.execute("INSERT INTO values_table VALUES ('before')")
    database.chmod(0o600)
    backup = tmp_path / "backup.db"
    phase6e.copy_sqlite(database, backup)
    backup.chmod(0o400)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE values_table SET value = 'after'")
    Path(f"{database}-wal").write_bytes(b"stale")
    Path(f"{database}-shm").write_bytes(b"stale")

    phase6e.restore_sqlite(backup, database, mode=0o600)

    with sqlite3.connect(database) as connection:
        value = connection.execute("SELECT value FROM values_table").fetchone()[0]
    assert value == "before"
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_restore_legacy_sources_restores_snapshot_contents_and_modes(
    tmp_path: Path,
) -> None:
    spec = parsed_spec(tmp_path)
    host = spec.host("desktop_primary")
    host.legacy_database.parent.mkdir(parents=True)
    with sqlite3.connect(host.legacy_database) as connection:
        connection.execute("CREATE TABLE values_table(value TEXT NOT NULL)")
        connection.execute("INSERT INTO values_table VALUES ('before')")
    host.legacy_database.chmod(0o600)
    host.legacy_config.write_text("state = 'before'\n", encoding="utf-8")
    host.legacy_config.chmod(0o640)

    backup = host.backup_root / spec.cutover_id
    backup.mkdir(parents=True)
    database_backup = backup / "legacy.db"
    phase6e.copy_sqlite(host.legacy_database, database_backup)
    database_backup.chmod(0o400)
    config_backup = backup / "legacy.toml"
    config_backup.write_text("state = 'before'\n", encoding="utf-8")
    config_backup.chmod(0o400)
    inventory = {
        "sources": [
            {
                "source": str(host.legacy_database),
                "backupName": database_backup.name,
                "sha256": phase6e.digest_file(database_backup),
                "mode": 0o600,
                "kind": "sqlite",
            },
            {
                "source": str(host.legacy_config),
                "backupName": config_backup.name,
                "sha256": phase6e.digest_file(config_backup),
                "mode": 0o640,
                "kind": "file",
            },
        ]
    }
    with sqlite3.connect(host.legacy_database) as connection:
        connection.execute("UPDATE values_table SET value = 'after'")
    host.legacy_database.chmod(0o400)
    host.legacy_config.write_text("state = 'after'\n", encoding="utf-8")
    host.legacy_config.chmod(0o400)
    Path(f"{host.legacy_database}-wal").write_bytes(b"stale")
    Path(f"{host.legacy_database}-shm").write_bytes(b"stale")

    phase6e.restore_legacy_sources(inventory, backup, host)

    with sqlite3.connect(host.legacy_database) as connection:
        value = connection.execute("SELECT value FROM values_table").fetchone()[0]
    assert value == "before"
    assert host.legacy_config.read_text(encoding="utf-8") == "state = 'before'\n"
    assert stat.S_IMODE(host.legacy_database.stat().st_mode) == 0o600
    assert stat.S_IMODE(host.legacy_config.stat().st_mode) == 0o640
    assert not Path(f"{host.legacy_database}-wal").exists()
    assert not Path(f"{host.legacy_database}-shm").exists()


def test_backup_optional_file_records_mode_without_weakening_backup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "settings.json"
    source.write_text("{}\n", encoding="utf-8")
    source.chmod(0o644)
    target = tmp_path / "backup.json"

    record = phase6e.backup_optional_file(source, target)

    assert record["mode"] == 0o644
    assert stat.S_IMODE(target.stat().st_mode) == 0o400


@pytest.mark.parametrize(
    ("active", "sub", "main", "control"),
    (("inactive", "dead", "0", "0"), ("failed", "failed", "0", "0")),
)
def test_dms_stopped_accepts_quiescent_systemd_states(
    active: str, sub: str, main: str, control: str
) -> None:
    assert phase6e.dms_service_stopped(
        {
            "ActiveState": active,
            "SubState": sub,
            "MainPID": main,
            "ControlPID": control,
        }
    )


@pytest.mark.parametrize(
    ("active", "sub", "main", "control"),
    (
        ("active", "running", "123", "0"),
        ("deactivating", "stop-sigterm", "123", "0"),
        ("failed", "failed", "123", "0"),
        ("inactive", "dead", "0", "123"),
    ),
)
def test_dms_stopped_rejects_live_or_transitioning_processes(
    active: str, sub: str, main: str, control: str
) -> None:
    assert not phase6e.dms_service_stopped(
        {
            "ActiveState": active,
            "SubState": sub,
            "MainPID": main,
            "ControlPID": control,
        }
    )


def test_wait_for_dms_state_observes_debounced_and_changed_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = iter(
        (
            phase6e.CutoverFailure("new DMS cache key was not created"),
            (b"cold", {"cache": "cold"}),
        )
    )

    def initial(_path: Path):
        value = next(first)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(phase6e, "dms_state_value", initial)
    monkeypatch.setattr(phase6e.time, "sleep", lambda _seconds: None)
    assert phase6e.wait_for_dms_state(tmp_path / "state.json") == (
        b"cold",
        {"cache": "cold"},
    )

    changed = iter(((b"cold", {}), (b"warm", {"cache": "warm"})))
    monkeypatch.setattr(phase6e, "dms_state_value", lambda _path: next(changed))
    assert phase6e.wait_for_dms_state(
        tmp_path / "state.json", different_from=b"cold"
    ) == (b"warm", {"cache": "warm"})


def test_wait_for_dms_retries_enable_until_plugin_is_discovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    statuses = iter(
        (
            {"adapterVersion": "missing"},
            {
                "adapterVersion": "0.5.0",
                "bridgeVersion": 1,
                "modelVersion": 1,
                "idle": True,
                "hasModel": True,
                "fresh": True,
                "runGeneration": 1,
            },
        )
    )

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        stdout = phase6e.canonical(next(statuses)) if argv[-1] == "status" else b""
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    monkeypatch.setattr(phase6e, "run", fake_run)
    monkeypatch.setattr(phase6e.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(phase6e.time, "sleep", lambda _seconds: None)

    result = phase6e.wait_for_dms(ensure_enabled=True)

    assert result["adapterVersion"] == "0.5.0"
    assert calls.count(("dms", "ipc", "call", "plugin-scan", "rescan")) == 1
    assert calls.count(("dms", "ipc", "call", "plugins", "enable", "switchboard")) == 2


def test_wait_for_dms_retries_rescan_until_ipc_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    scan_attempts = 0
    statuses = iter(
        (
            {},
            {
                "adapterVersion": "0.5.0",
                "bridgeVersion": 1,
                "modelVersion": 1,
                "idle": True,
                "hasModel": True,
                "fresh": True,
                "runGeneration": 1,
            },
        )
    )

    def fake_run(argv, **_kwargs):
        nonlocal scan_attempts
        calls.append(tuple(argv))
        if argv[-1] == "rescan":
            scan_attempts += 1
            return subprocess.CompletedProcess(
                argv, 1 if scan_attempts == 1 else 0, b"", b""
            )
        stdout = phase6e.canonical(next(statuses)) if argv[-1] == "status" else b""
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    monkeypatch.setattr(phase6e, "run", fake_run)
    monkeypatch.setattr(phase6e.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(phase6e.time, "sleep", lambda _seconds: None)

    result = phase6e.wait_for_dms(ensure_enabled=True)

    assert result["adapterVersion"] == "0.5.0"
    assert calls.count(("dms", "ipc", "call", "plugin-scan", "rescan")) == 2


def test_rehome_console_script_repairs_relocated_venv_entrypoint(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    binary = release / "bin"
    binary.mkdir(parents=True)
    interpreter = binary / "python"
    interpreter.write_bytes(b"python")
    interpreter.chmod(0o755)
    script = binary / "swbctl"
    body = b"from agent_switchboard.cli import main\nmain()\n"
    script.write_bytes(b"#!/tmp/build/bin/python\n" + body)
    script.chmod(0o755)

    phase6e.rehome_console_script(script, interpreter)

    assert script.read_bytes() == f"#!{interpreter}\n".encode() + body
    assert script.stat().st_mode & 0o777 == 0o755
    phase6e.rehome_console_script(script, interpreter)
    assert script.read_bytes() == f"#!{interpreter}\n".encode() + body


def test_worker_call_propagates_bounded_remote_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = parsed_spec(tmp_path)
    failure = phase6e.canonical(
        {
            "ok": False,
            "error": {
                "code": "phase6e_cutover_failed",
                "message": "release console script failed",
            },
        }
    )
    monkeypatch.setattr(
        phase6e,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, failure, b""),
    )
    with pytest.raises(
        phase6e.CutoverFailure,
        match=("remote_owner stage failed: release console script failed"),
    ):
        phase6e.worker_call(spec, "remote_owner", "stage")


def legacy_database(
    tmp_path: Path, *, live: bool = False, active: bool = False
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            PRAGMA user_version = 10;
            CREATE TABLE hosts(host_id TEXT PRIMARY KEY, is_local INTEGER NOT NULL);
            CREATE TABLE sessions(
                session_key TEXT PRIMARY KEY,
                host_id TEXT NOT NULL,
                runtime_presence TEXT NOT NULL,
                surface_id TEXT
            );
            CREATE TABLE launch_intents(
                launch_id TEXT PRIMARY KEY,
                state TEXT NOT NULL
            );
            CREATE TABLE surfaces(
                surface_id TEXT PRIMARY KEY,
                host_id TEXT NOT NULL,
                current_session_key TEXT,
                binding_confidence TEXT NOT NULL,
                client_attached INTEGER NOT NULL,
                last_observed_at INTEGER NOT NULL,
                retired_at INTEGER
            );
            """
        )
        connection.execute("INSERT INTO hosts VALUES (?, 1)", (LOCAL_HOST,))
        connection.execute(
            "INSERT INTO surfaces VALUES (?, ?, ?, 'confirmed', 1, 20, NULL)",
            ("540f6a81-67b6-42ce-b7ca-2068bb190e88", LOCAL_HOST, "session"),
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            (
                "session",
                LOCAL_HOST,
                "live" if live else "stopped",
                "540f6a81-67b6-42ce-b7ca-2068bb190e88",
            ),
        )
        if active:
            connection.execute(
                "INSERT INTO launch_intents VALUES (?, 'provider_started')",
                ("640f6a81-67b6-42ce-b7ca-2068bb190e88",),
            )
    return database


def test_legacy_surface_retirement_requires_complete_quiescence(tmp_path: Path) -> None:
    live = legacy_database(tmp_path / "live", live=True)
    with pytest.raises(phase6e.CutoverFailure, match="became active"):
        phase6e.retire_legacy_surfaces(live, LOCAL_HOST, observed_at=30)

    active = legacy_database(tmp_path / "active", active=True)
    with pytest.raises(phase6e.CutoverFailure, match="became active"):
        phase6e.retire_legacy_surfaces(active, LOCAL_HOST, observed_at=30)


def test_legacy_surface_retirement_clears_only_inactive_surfaces(
    tmp_path: Path,
) -> None:
    database = legacy_database(tmp_path)
    result = phase6e.retire_legacy_surfaces(database, LOCAL_HOST, observed_at=30)
    assert result["retiredSurfaceCount"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT surface_id FROM sessions WHERE session_key = 'session'"
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT current_session_key, binding_confidence, client_attached, "
            "last_observed_at, retired_at FROM surfaces"
        ).fetchone() == (None, "unknown", 0, 30, 30)
