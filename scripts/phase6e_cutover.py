#!/usr/bin/env python3
"""Resumable two-host Phase 6E clean-break executor.

The coordinator is launched from a plain shell.  Host workers use only fixed,
validated argv and emit one bounded JSON record.  Before the first core commit,
the coordinator rolls every staged generation back on failure.  Once snap
commits, the journal is forward-only.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

EXECUTOR_VERSION: Final = 1
CORE_VERSION: Final = "0.3.0"
DMS_VERSION: Final = "0.5.0"
ROLES: Final = ("desktop_primary", "remote_owner")
MAX_OUTPUT: Final = 4 * 1024 * 1024
COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}")
TOKEN_RE: Final = re.compile(r"[A-Za-z0-9._@:/+-]+")
REQUIRED_CHECKS: Final = (
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
LEGACY_SCHEMA_VERSION: Final = 10
LEGACY_ACTIVE_LAUNCH_STATES: Final = (
    "provider_started",
    "reserved",
    "surface_ready",
    "waiting_for_client",
)


class CutoverFailure(RuntimeError):
    pass


def canonical(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        + b"\n"
    )


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def exact_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise CutoverFailure(f"{label} must be a UUID")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise CutoverFailure(f"{label} must be a UUID") from error
    if parsed.int == 0 or str(parsed) != value:
        raise CutoverFailure(f"{label} must be a canonical non-nil UUID")
    return value


def safe_token(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode()) > 1024
        or TOKEN_RE.fullmatch(value) is None
    ):
        raise CutoverFailure(f"{label} is not a safe executor token")
    return value


def absolute_path(value: object, label: str) -> Path:
    token = safe_token(value, label)
    path = Path(token)
    if (
        not path.is_absolute()
        or path == path.parent
        or ".." in path.parts
        or ":" in token
    ):
        raise CutoverFailure(f"{label} must be an absolute non-root path")
    return path


def strict_object(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CutoverFailure(f"{label} fields are incompatible")
    return value


@dataclass(frozen=True, slots=True)
class HostSpec:
    role: str
    host_id: str
    generation_id: str
    ssh_target: str | None
    python: Path
    legacy_swbctl: Path
    legacy_database: Path
    legacy_config: Path
    config_root: Path
    state_root: Path
    release_root: Path
    bin_link: Path
    backup_root: Path
    provider_executables: dict[str, Path]
    hook_files: dict[str, Path]
    project_id: str
    stop_sessions: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> HostSpec:
        fields = {
            "role",
            "hostId",
            "generationId",
            "sshTarget",
            "python",
            "legacySwbctl",
            "legacyDatabase",
            "legacyConfig",
            "configRoot",
            "stateRoot",
            "releaseRoot",
            "binLink",
            "backupRoot",
            "providerExecutables",
            "hookFiles",
            "projectId",
            "stopSessions",
        }
        record = strict_object(value, fields, "host")
        role = safe_token(record["role"], "host.role")
        if role not in ROLES:
            raise CutoverFailure("host role is unsupported")
        ssh = record["sshTarget"]
        if ssh is not None:
            ssh = safe_token(ssh, "host.sshTarget")
        providers = record["providerExecutables"]
        if (
            not isinstance(providers, dict)
            or not providers
            or not set(providers) <= {"codex", "claude"}
        ):
            raise CutoverFailure("host provider executables are incompatible")
        hook_files = record["hookFiles"]
        if not isinstance(hook_files, dict) or set(hook_files) != set(providers):
            raise CutoverFailure("host hook files must match configured providers")
        stops = record["stopSessions"]
        if not isinstance(stops, list) or not all(
            isinstance(item, str) for item in stops
        ):
            raise CutoverFailure("host stopSessions must be an array")
        return cls(
            role,
            exact_uuid(record["hostId"], "host.hostId"),
            exact_uuid(record["generationId"], "host.generationId"),
            ssh,
            absolute_path(record["python"], "host.python"),
            absolute_path(record["legacySwbctl"], "host.legacySwbctl"),
            absolute_path(record["legacyDatabase"], "host.legacyDatabase"),
            absolute_path(record["legacyConfig"], "host.legacyConfig"),
            absolute_path(record["configRoot"], "host.configRoot"),
            absolute_path(record["stateRoot"], "host.stateRoot"),
            absolute_path(record["releaseRoot"], "host.releaseRoot"),
            absolute_path(record["binLink"], "host.binLink"),
            absolute_path(record["backupRoot"], "host.backupRoot"),
            {
                safe_token(name, "provider name"): absolute_path(
                    path, f"providerExecutables.{name}"
                )
                for name, path in providers.items()
            },
            {
                safe_token(name, "hook provider"): absolute_path(
                    path, f"hookFiles.{name}"
                )
                for name, path in hook_files.items()
            },
            exact_uuid(record["projectId"], "host.projectId"),
            tuple(safe_token(item, "stop session") for item in stops),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "hostId": self.host_id,
            "generationId": self.generation_id,
            "sshTarget": self.ssh_target,
            "python": str(self.python),
            "legacySwbctl": str(self.legacy_swbctl),
            "legacyDatabase": str(self.legacy_database),
            "legacyConfig": str(self.legacy_config),
            "configRoot": str(self.config_root),
            "stateRoot": str(self.state_root),
            "releaseRoot": str(self.release_root),
            "binLink": str(self.bin_link),
            "backupRoot": str(self.backup_root),
            "providerExecutables": {
                key: str(value) for key, value in self.provider_executables.items()
            },
            "hookFiles": {key: str(value) for key, value in self.hook_files.items()},
            "projectId": self.project_id,
            "stopSessions": list(self.stop_sessions),
        }


@dataclass(frozen=True, slots=True)
class DesktopSpec:
    dms_repo: Path
    plugin_dir: Path
    plugin_state: Path
    plugin_settings: Path
    service: str

    @classmethod
    def parse(cls, value: object) -> DesktopSpec:
        record = strict_object(
            value,
            {"dmsRepo", "pluginDir", "pluginState", "pluginSettings", "service"},
            "desktop",
        )
        return cls(
            absolute_path(record["dmsRepo"], "desktop.dmsRepo"),
            absolute_path(record["pluginDir"], "desktop.pluginDir"),
            absolute_path(record["pluginState"], "desktop.pluginState"),
            absolute_path(record["pluginSettings"], "desktop.pluginSettings"),
            safe_token(record["service"], "desktop.service"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "dmsRepo": str(self.dms_repo),
            "pluginDir": str(self.plugin_dir),
            "pluginState": str(self.plugin_state),
            "pluginSettings": str(self.plugin_settings),
            "service": self.service,
        }


@dataclass(frozen=True, slots=True)
class Spec:
    cutover_id: str
    core_commit: str
    dms_commit: str
    source_date_epoch: int
    workspace: Path
    core_repo: Path
    desktop: DesktopSpec
    hosts: tuple[HostSpec, HostSpec]
    current_session_key: str

    @classmethod
    def from_path(cls, path: Path) -> Spec:
        try:
            value = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CutoverFailure("cutover spec is invalid JSON") from error
        record = strict_object(
            value,
            {
                "executorVersion",
                "cutoverId",
                "coreCommit",
                "dmsCommit",
                "sourceDateEpoch",
                "workspace",
                "coreRepo",
                "desktop",
                "hosts",
                "currentSessionKey",
            },
            "spec",
        )
        if record["executorVersion"] != EXECUTOR_VERSION:
            raise CutoverFailure("executorVersion is incompatible")
        if not isinstance(record["sourceDateEpoch"], int) or isinstance(
            record["sourceDateEpoch"], bool
        ):
            raise CutoverFailure("sourceDateEpoch is invalid")
        if not COMMIT_RE.fullmatch(
            str(record["coreCommit"])
        ) or not COMMIT_RE.fullmatch(str(record["dmsCommit"])):
            raise CutoverFailure("paired commits must be exact Git object IDs")
        hosts_raw = record["hosts"]
        if not isinstance(hosts_raw, list) or len(hosts_raw) != 2:
            raise CutoverFailure("spec must contain exactly two hosts")
        hosts = tuple(HostSpec.parse(item) for item in hosts_raw)
        if {item.role for item in hosts} != set(ROLES):
            raise CutoverFailure("spec host roles are incomplete")
        if (
            len({item.host_id for item in hosts}) != 2
            or len({item.generation_id for item in hosts}) != 2
        ):
            raise CutoverFailure("host and generation identities must be distinct")
        local = next(item for item in hosts if item.role == "desktop_primary")
        remote = next(item for item in hosts if item.role == "remote_owner")
        if local.ssh_target is not None or remote.ssh_target is None:
            raise CutoverFailure("desktop must be local and remote_owner must use SSH")
        session = safe_token(record["currentSessionKey"], "currentSessionKey")
        session_parts = session.split(":")
        if (
            len(session_parts) != 3
            or session_parts[0] != local.host_id
            or session_parts[1] not in local.provider_executables
        ):
            raise CutoverFailure("currentSessionKey must belong to desktop_primary")
        exact_uuid(session_parts[2], "currentSessionKey provider session")
        return cls(
            exact_uuid(record["cutoverId"], "cutoverId"),
            str(record["coreCommit"]),
            str(record["dmsCommit"]),
            int(record["sourceDateEpoch"]),
            absolute_path(record["workspace"], "workspace"),
            absolute_path(record["coreRepo"], "coreRepo"),
            DesktopSpec.parse(record["desktop"]),
            hosts,  # type: ignore[arg-type]
            session,
        )

    def host(self, role: str) -> HostSpec:
        return next(item for item in self.hosts if item.role == role)

    def to_dict(self) -> dict[str, Any]:
        return {
            "executorVersion": EXECUTOR_VERSION,
            "cutoverId": self.cutover_id,
            "coreCommit": self.core_commit,
            "dmsCommit": self.dms_commit,
            "sourceDateEpoch": self.source_date_epoch,
            "workspace": str(self.workspace),
            "coreRepo": str(self.core_repo),
            "desktop": self.desktop.to_dict(),
            "hosts": [item.to_dict() for item in self.hosts],
            "currentSessionKey": self.current_session_key,
        }


def run(
    argv: list[str],
    *,
    timeout: int = 120,
    environment: dict[str, str] | None = None,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            env=environment,
            cwd=cwd,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CutoverFailure(f"command failed to execute: {argv[0]}") from error
    if len(result.stdout) > MAX_OUTPUT or len(result.stderr) > MAX_OUTPUT:
        raise CutoverFailure(f"command output exceeded bounds: {argv[0]}")
    if check and result.returncode != 0:
        raise CutoverFailure(f"command exited {result.returncode}: {argv[0]}")
    return result


def json_command(argv: list[str], *, timeout: int = 120, check: bool = True) -> Any:
    result = run(argv, timeout=timeout, check=check)
    try:
        return json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CutoverFailure(f"command did not return JSON: {argv[0]}") from error


def git_exact(repo: Path, commit: str) -> None:
    observed = (
        run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.decode().strip()
    )
    if observed != commit:
        raise CutoverFailure(f"repository is not at accepted commit: {repo}")
    if run(["git", "-C", str(repo), "status", "--porcelain"]).stdout:
        raise CutoverFailure(f"repository is dirty: {repo}")


def write_private(path: Path, value: object, *, mode: int = 0o400) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        payload = canonical(value)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise CutoverFailure("private journal write was incomplete")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def rehome_console_script(path: Path, interpreter: Path) -> None:
    """Atomically replace a relocated venv script's absolute shebang."""

    if not path.is_file() or path.is_symlink():
        raise CutoverFailure("release console script is missing or unsafe")
    if not interpreter.is_file():
        raise CutoverFailure("release interpreter is missing or unsafe")
    payload = path.read_bytes()
    newline = payload.find(b"\n")
    if newline < 0 or not payload.startswith(b"#!"):
        raise CutoverFailure("release console script has an invalid shebang")
    shebang = b"#!" + os.fsencode(interpreter) + b"\n"
    if payload[: newline + 1] == shebang:
        return
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700
    )
    try:
        view = memoryview(shebang + payload[newline + 1 :])
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise CutoverFailure("release console script write was incomplete")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(temporary, 0o755)
    os.replace(temporary, path)


def prepare(spec: Spec) -> dict[str, Any]:
    git_exact(spec.core_repo, spec.core_commit)
    git_exact(spec.desktop.dms_repo, spec.dms_commit)
    if spec.workspace.exists():
        raise CutoverFailure("cutover workspace already exists")
    spec.workspace.mkdir(mode=0o700, parents=True)
    artifacts = spec.workspace / "artifacts"
    build_dir = spec.workspace / "build"
    artifacts.mkdir(mode=0o700)
    build_dir.mkdir(mode=0o700)
    first_build = build_dir / "core-first"
    second_build = build_dir / "core-second"
    first_build.mkdir(mode=0o700)
    second_build.mkdir(mode=0o700)
    environment = dict(os.environ)
    environment["SOURCE_DATE_EPOCH"] = str(spec.source_date_epoch)
    for destination in (first_build, second_build):
        run(
            [sys.executable, "-m", "build", "--outdir", str(destination)],
            timeout=300,
            environment=environment,
            cwd=spec.core_repo,
        )
    run(
        [
            sys.executable,
            str(spec.core_repo / "scripts" / "verify_distributions.py"),
            str(first_build),
            str(second_build),
        ],
        timeout=180,
    )
    wheels = sorted(first_build.glob("agent_switchboard-0.3.0-*.whl"))
    if len(wheels) != 1:
        raise CutoverFailure("core build did not produce exactly one wheel")
    wheelhouse = artifacts / "wheelhouse"
    run(
        [
            sys.executable,
            str(spec.core_repo / "scripts" / "build_offline_bundle.py"),
            str(wheels[0]),
            str(wheelhouse),
            "--core-commit",
            spec.core_commit,
        ],
        timeout=300,
    )
    dms_artifact = artifacts / "switchboard-dms-0.5.0.zip"
    dms_second = build_dir / "switchboard-dms-second.zip"
    for destination in (dms_artifact, dms_second):
        run(
            [
                str(spec.desktop.dms_repo / "scripts" / "build-plugin"),
                "--output",
                str(destination),
            ],
            timeout=120,
            environment=environment,
        )
    if digest_file(dms_artifact) != digest_file(dms_second):
        raise CutoverFailure("DMS builds are not byte-identical")
    shutil.copy2(Path(__file__), spec.workspace / Path(__file__).name)
    write_private(spec.workspace / "spec.json", spec.to_dict(), mode=0o400)
    prepared = {
        "preparedVersion": 1,
        "cutoverId": spec.cutover_id,
        "coreCommit": spec.core_commit,
        "dmsCommit": spec.dms_commit,
        "coreArtifactSha256": digest_file(wheels[0]),
        "dmsArtifactSha256": digest_file(dms_artifact),
        "coreWheel": wheels[0].name,
        "wheelhouseManifestSha256": digest_file(
            wheelhouse / "wheelhouse-manifest.json"
        ),
        "preparedAt": int(time.time() * 1000),
    }
    write_private(spec.workspace / "prepared.json", prepared, mode=0o400)
    return prepared


def load_prepared(spec: Spec) -> dict[str, Any]:
    path = spec.workspace / "prepared.json"
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CutoverFailure("prepared manifest is missing or invalid") from error
    expected = {
        "preparedVersion",
        "cutoverId",
        "coreCommit",
        "dmsCommit",
        "coreArtifactSha256",
        "dmsArtifactSha256",
        "coreWheel",
        "wheelhouseManifestSha256",
        "preparedAt",
    }
    record = strict_object(value, expected, "prepared manifest")
    if (
        record["preparedVersion"] != 1
        or record["cutoverId"] != spec.cutover_id
        or record["coreCommit"] != spec.core_commit
        or record["dmsCommit"] != spec.dms_commit
    ):
        raise CutoverFailure("prepared manifest does not match cutover spec")
    dms = spec.workspace / "artifacts" / "switchboard-dms-0.5.0.zip"
    wheelhouse = spec.workspace / "artifacts" / "wheelhouse"
    wheel = wheelhouse / str(record["coreWheel"])
    if digest_file(wheel) != record["coreArtifactSha256"]:
        raise CutoverFailure("core artifact hash does not match prepared manifest")
    if digest_file(dms) != record["dmsArtifactSha256"]:
        raise CutoverFailure("DMS artifact hash does not match prepared manifest")
    if (
        digest_file(wheelhouse / "wheelhouse-manifest.json")
        != record["wheelhouseManifestSha256"]
    ):
        raise CutoverFailure("wheelhouse manifest hash does not match")
    return record


def core_argv(host: HostSpec, executable: Path, *arguments: str) -> list[str]:
    return [
        str(executable),
        "--config-root",
        str(host.config_root),
        "--state-root",
        str(host.state_root),
        *arguments,
    ]


def release_swbctl(spec: Spec, host: HostSpec, prepared: dict[str, Any]) -> Path:
    release_name = (
        f"core-{CORE_VERSION}-{spec.core_commit[:12]}-"
        f"{str(prepared['coreArtifactSha256'])[:12]}"
    )
    return host.release_root / release_name / "bin" / "swbctl"


def load_inventory(spec: Spec, host: HostSpec) -> dict[str, Any]:
    path = host.backup_root / spec.cutover_id / "inventory.json"
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CutoverFailure("host backup inventory is missing or invalid") from error
    if (
        not isinstance(value, dict)
        or value.get("role") != host.role
        or value.get("cutoverId") != spec.cutover_id
    ):
        raise CutoverFailure("host backup inventory belongs to another cutover")
    return value


def worker_stage(spec: Spec, role: str) -> dict[str, Any]:
    prepared = load_prepared(spec)
    host = spec.host(role)
    release = release_swbctl(spec, host, prepared).parent.parent
    if release.exists():
        if not release.is_dir() or release.is_symlink():
            raise CutoverFailure("inactive core release path is unsafe")
    else:
        release.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = release.with_name(f".{release.name}.{uuid4()}.tmp")
        run([str(host.python), "-m", "venv", str(temporary)], timeout=120)
        try:
            run(
                [
                    str(temporary / "bin" / "pip"),
                    "install",
                    "--no-index",
                    "--find-links",
                    str(spec.workspace / "artifacts" / "wheelhouse"),
                    f"agent-switchboard=={CORE_VERSION}",
                ],
                timeout=300,
            )
            temporary.rename(release)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    swbctl = release / "bin" / "swbctl"
    rehome_console_script(swbctl, release / "bin" / "python")
    version = run([str(swbctl), "--version"]).stdout.decode().strip()
    if version != f"swbctl {CORE_VERSION}":
        raise CutoverFailure("inactive core install has the wrong version")
    backup = host.backup_root / spec.cutover_id
    if backup.exists():
        result = load_inventory(spec, host)
        if result.get("releaseSwbctl") != str(swbctl):
            raise CutoverFailure("existing backup names another core release")
        return result
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_backup = backup.with_name(f".{backup.name}.{uuid4()}.tmp")
    temporary_backup.mkdir(mode=0o700)
    backed_up: list[dict[str, Any]] = []
    try:
        for source in (host.legacy_database, host.legacy_config):
            if not source.is_file() or source.is_symlink():
                raise CutoverFailure(f"legacy source is missing or unsafe: {source}")
            target = temporary_backup / source.name
            shutil.copy2(source, target, follow_symlinks=False)
            target.chmod(0o400)
            backed_up.append(
                {
                    "source": str(source),
                    "backupName": target.name,
                    "sha256": digest_file(target),
                }
            )
        hooks: dict[str, dict[str, Any]] = {}
        hook_backup = temporary_backup / "hooks"
        hook_backup.mkdir(mode=0o700)
        for provider, source in sorted(host.hook_files.items()):
            if source.is_symlink() or (source.exists() and not source.is_file()):
                raise CutoverFailure(f"hook configuration is unsafe: {source}")
            record: dict[str, Any] = {"path": str(source), "existed": source.exists()}
            if source.exists():
                target = hook_backup / f"{provider}.json"
                shutil.copy2(source, target, follow_symlinks=False)
                target.chmod(0o400)
                record.update(
                    {
                        "backupName": f"hooks/{provider}.json",
                        "sha256": digest_file(target),
                    }
                )
            hooks[provider] = record
        if host.bin_link.is_symlink():
            bin_link: dict[str, Any] = {
                "existed": True,
                "target": os.readlink(host.bin_link),
            }
        elif host.bin_link.exists():
            raise CutoverFailure("public swbctl path is not a symlink")
        else:
            bin_link = {"existed": False, "target": None}
        versions = {
            provider: run([str(executable), "--version"]).stdout.decode().strip()
            for provider, executable in sorted(host.provider_executables.items())
        }
        result = {
            "role": role,
            "cutoverId": spec.cutover_id,
            "releaseSwbctl": str(swbctl),
            "backup": str(backup),
            "sources": backed_up,
            "hooks": hooks,
            "binLink": bin_link,
            "legacyVersion": run([str(host.legacy_swbctl), "--version"])
            .stdout.decode()
            .strip(),
            "providerVersions": versions,
        }
        write_private(temporary_backup / "inventory.json", result, mode=0o400)
        temporary_backup.rename(backup)
        return result
    finally:
        if temporary_backup.exists():
            shutil.rmtree(temporary_backup)


def worker_import(spec: Spec, role: str) -> dict[str, Any]:
    prepared = load_prepared(spec)
    host = spec.host(role)
    swbctl = release_swbctl(spec, host, prepared)
    backup = host.backup_root / spec.cutover_id
    for session in host.stop_sessions:
        stopped = run(
            [
                str(host.legacy_swbctl),
                "stop-session",
                safe_token(session, "stop session"),
                "--host",
                host.host_id,
                "--json",
            ],
            timeout=60,
            check=False,
        )
        if stopped.returncode != 0:
            raise CutoverFailure("legacy session did not stop cleanly")
    for provider in sorted(host.provider_executables):
        run(
            [
                str(host.legacy_swbctl),
                "hooks",
                "uninstall",
                "--provider",
                provider,
            ],
            timeout=60,
        )
    quiesced = quiesce_legacy_registry(host)
    export_dir = backup / "export"
    bundle = export_dir / "cutover-bundle.json"
    if bundle.is_file() and not bundle.is_symlink():
        try:
            exported = {"bundleHash": json.loads(bundle.read_bytes())["bundleHash"]}
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CutoverFailure("existing cutover export is invalid") from error
    else:
        if export_dir.exists():
            raise CutoverFailure("existing cutover export is incomplete")
        exported = json_command(
            core_argv(
                host,
                swbctl,
                "cutover",
                "export",
                "--database",
                str(host.legacy_database),
                "--config",
                str(host.legacy_config),
                "--destination",
                str(export_dir),
            ),
            timeout=180,
        )
    imported = json_command(
        core_argv(
            host,
            swbctl,
            "cutover",
            "import",
            "--bundle",
            str(bundle),
            "--generation-id",
            host.generation_id,
        ),
        timeout=180,
    )
    if imported.get("activationState") != "cutover_staged":
        raise CutoverFailure("import did not produce a staged generation")
    return {
        "role": role,
        "bundleHash": exported["bundleHash"],
        "bundleSha256": digest_file(bundle),
        "generationId": host.generation_id,
        "legacyQuiescence": quiesced,
    }


def _legacy_snapshot(host: HostSpec) -> dict[str, Any]:
    value = json_command(
        [
            str(host.legacy_swbctl),
            "snapshot",
            "--reconcile",
            "full",
            "--json",
        ],
        timeout=180,
    )
    if (
        not isinstance(value, dict)
        or value.get("host", {}).get("hostId") != host.host_id
    ):
        raise CutoverFailure("legacy snapshot host identity is incompatible")
    sessions = value.get("sessions")
    surfaces = value.get("surfaces")
    if not isinstance(sessions, list) or not isinstance(surfaces, list):
        raise CutoverFailure("legacy snapshot collections are incompatible")
    live = [
        item
        for item in sessions
        if isinstance(item, dict) and item.get("runtimePresence") == "live"
    ]
    if live:
        raise CutoverFailure(
            f"legacy host still has {len(live)} live provider session(s)"
        )
    return value


def retire_legacy_surfaces(
    database: Path, host_id: str, *, observed_at: int | None = None
) -> dict[str, Any]:
    """Fence inactive v0.2 surfaces after backup and complete reconciliation."""

    if not database.is_file() or database.is_symlink():
        raise CutoverFailure("legacy database is missing or unsafe")
    timestamp = int(time.time() * 1000) if observed_at is None else observed_at
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise CutoverFailure("legacy quiescence timestamp is invalid")
    connection = sqlite3.connect(database, timeout=5)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        schema = connection.execute("PRAGMA user_version").fetchone()[0]
        if schema != LEGACY_SCHEMA_VERSION:
            raise CutoverFailure("legacy database is not schema v10")
        local_hosts = connection.execute(
            "SELECT host_id FROM hosts WHERE is_local = 1 ORDER BY host_id"
        ).fetchall()
        if local_hosts != [(host_id,)]:
            raise CutoverFailure("legacy database local host identity is incompatible")
        live = connection.execute(
            "SELECT count(*) FROM sessions "
            "WHERE host_id = ? AND runtime_presence = 'live'",
            (host_id,),
        ).fetchone()[0]
        placeholders = ",".join("?" for _ in LEGACY_ACTIVE_LAUNCH_STATES)
        active_launches = connection.execute(
            f"SELECT count(*) FROM launch_intents WHERE state IN ({placeholders})",
            LEGACY_ACTIVE_LAUNCH_STATES,
        ).fetchone()[0]
        if live or active_launches:
            raise CutoverFailure(
                "legacy runtimes or launch intents became active during quiescence"
            )
        rows = connection.execute(
            "SELECT surface_id, last_observed_at FROM surfaces "
            "WHERE host_id = ? AND retired_at IS NULL ORDER BY surface_id",
            (host_id,),
        ).fetchall()
        if rows:
            timestamp = max(timestamp, *(int(row[1]) for row in rows))
            connection.execute(
                "UPDATE sessions SET surface_id = NULL WHERE surface_id IN ("
                "SELECT surface_id FROM surfaces "
                "WHERE host_id = ? AND retired_at IS NULL)",
                (host_id,),
            )
            connection.execute(
                "UPDATE surfaces SET current_session_key = NULL, "
                "binding_confidence = 'unknown', client_attached = 0, "
                "retired_at = ?, last_observed_at = ? "
                "WHERE host_id = ? AND retired_at IS NULL",
                (timestamp, timestamp, host_id),
            )
        remaining = connection.execute(
            "SELECT count(*) FROM surfaces "
            "WHERE host_id = ? AND retired_at IS NULL",
            (host_id,),
        ).fetchone()[0]
        if remaining:
            raise CutoverFailure("legacy surface retirement was incomplete")
        connection.commit()
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.rollback()
        raise CutoverFailure("legacy surface retirement failed") from error
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    surface_ids = [str(row[0]) for row in rows]
    return {
        "retiredSurfaceCount": len(surface_ids),
        "retiredSurfaceIdsSha256": digest_bytes(canonical(surface_ids)),
    }


def quiesce_legacy_registry(host: HostSpec) -> dict[str, Any]:
    _legacy_snapshot(host)
    retired = retire_legacy_surfaces(host.legacy_database, host.host_id)
    verified = _legacy_snapshot(host)
    active_surfaces = [
        item
        for item in verified["surfaces"]
        if isinstance(item, dict) and item.get("retiredAt") is None
    ]
    if active_surfaces:
        raise CutoverFailure("legacy surfaces remained active after retirement")
    return retired


def expected_staged_failure(argv: list[str]) -> bytes:
    result = run(argv, check=False)
    if result.returncode == 0:
        raise CutoverFailure("staged mutation unexpectedly succeeded")
    try:
        value = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CutoverFailure(
            "staged mutation did not return a structured error"
        ) from error
    if value.get("error", {}).get("code") != "cutover_staged":
        raise CutoverFailure("staged mutation returned the wrong failure")
    return canonical(value)


def worker_validate(spec: Spec, role: str) -> dict[str, Any]:
    prepared = load_prepared(spec)
    host = spec.host(role)
    swbctl = release_swbctl(spec, host, prepared)
    doctor = canonical(json_command(core_argv(host, swbctl, "doctor", "--json")))
    reconcile = canonical(json_command(core_argv(host, swbctl, "reconcile", "--json")))
    host_state = run(core_argv(host, swbctl, "state", "host", "--json")).stdout
    navigator = run(
        core_argv(host, swbctl, "state", "navigator", "--refresh", "--json"),
        timeout=180,
    ).stdout
    for raw, name in ((host_state, "HostState"), (navigator, "NavigatorState")):
        value = json.loads(raw)
        if value.get("generationId") != host.generation_id:
            raise CutoverFailure(f"{name} generation is wrong")
    request = str(uuid4())
    blocked_view = expected_staged_failure(
        core_argv(
            host,
            swbctl,
            "view",
            "open",
            "--host",
            host.host_id,
            "--project",
            host.project_id,
            "--request-id",
            request,
            "--can-launch-terminal",
            "--json",
        )
    )
    provider = sorted(host.provider_executables)[0]
    blocked_hook = expected_staged_failure(
        core_argv(
            host,
            swbctl,
            "hooks",
            "install",
            "--provider",
            provider,
            "--executable",
            str(swbctl),
            "--dry-run",
        )
    )
    doctor_value = json.loads(doctor)
    versions = {
        item["provider"]: item["version"]
        for item in doctor_value["providers"]
        if item.get("enabled") and item.get("available")
    }
    return {
        "role": role,
        "hostId": host.host_id,
        "generationId": host.generation_id,
        "providerVersions": versions,
        "hostStateSha256": digest_bytes(host_state.rstrip(b"\n")),
        "navigatorStateSha256": digest_bytes(navigator.rstrip(b"\n")),
        "checks": {
            "coreDoctor": digest_bytes(doctor),
            "reconciliation": digest_bytes(reconcile),
            "stagedMutationBlock": digest_bytes(blocked_view + blocked_hook),
            "hostState": digest_bytes(host_state),
            "navigatorState": digest_bytes(navigator),
        },
    }


def replace_symlink(path: Path, target: str | Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    temporary.symlink_to(target)
    os.replace(temporary, path)


def worker_stage_core(spec: Spec, role: str) -> dict[str, Any]:
    prepared = load_prepared(spec)
    host = spec.host(role)
    load_inventory(spec, host)
    swbctl = release_swbctl(spec, host, prepared)
    if host.bin_link.exists() and not host.bin_link.is_symlink():
        raise CutoverFailure("public swbctl path is not a symlink")
    replace_symlink(host.bin_link, swbctl)
    return {
        "role": role,
        "activeSwbctl": str(host.bin_link),
        "activationState": "cutover_staged",
    }


def worker_hide_core(spec: Spec, role: str) -> dict[str, Any]:
    if role != "remote_owner":
        raise CutoverFailure("only the remote staged CLI may be hidden")
    prepared = load_prepared(spec)
    host = spec.host(role)
    expected = release_swbctl(spec, host, prepared)
    if not host.bin_link.is_symlink() or host.bin_link.resolve() != expected.resolve():
        raise CutoverFailure("remote staged swbctl identity changed")
    host.bin_link.unlink()
    return {"role": role, "activeSwbctl": None}


def restore_file(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise CutoverFailure("backup file is missing or unsafe")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4()}.tmp")
    shutil.copy2(source, temporary, follow_symlinks=False)
    os.replace(temporary, destination)


def worker_rollback(spec: Spec, role: str) -> dict[str, Any]:
    prepared = load_prepared(spec)
    host = spec.host(role)
    swbctl = release_swbctl(spec, host, prepared)
    rolled_back: Any = None
    result = run(core_argv(host, swbctl, "cutover", "rollback"), check=False)
    if result.returncode == 0:
        with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
            rolled_back = json.loads(result.stdout)
    inventory = load_inventory(spec, host)
    hooks = inventory.get("hooks")
    if not isinstance(hooks, dict) or set(hooks) != set(host.hook_files):
        raise CutoverFailure("host hook backup inventory is incompatible")
    backup = host.backup_root / spec.cutover_id
    for provider, destination in sorted(host.hook_files.items()):
        record = hooks[provider]
        if not isinstance(record, dict) or record.get("path") != str(destination):
            raise CutoverFailure("host hook backup path is incompatible")
        if record.get("existed") is True:
            source = backup / str(record.get("backupName"))
            if digest_file(source) != record.get("sha256"):
                raise CutoverFailure("host hook backup hash changed")
            restore_file(source, destination)
        elif record.get("existed") is False:
            if destination.is_symlink() or (
                destination.exists() and not destination.is_file()
            ):
                raise CutoverFailure("rollback hook destination is unsafe")
            destination.unlink(missing_ok=True)
        else:
            raise CutoverFailure("host hook backup existence is invalid")
    link = inventory.get("binLink")
    if not isinstance(link, dict) or set(link) != {"existed", "target"}:
        raise CutoverFailure("host swbctl backup inventory is incompatible")
    if host.bin_link.exists() and not host.bin_link.is_symlink():
        raise CutoverFailure("rollback swbctl destination is unsafe")
    if link["existed"] is True and isinstance(link["target"], str):
        replace_symlink(host.bin_link, link["target"])
    elif link == {"existed": False, "target": None}:
        host.bin_link.unlink(missing_ok=True)
    else:
        raise CutoverFailure("host swbctl backup identity is invalid")
    return {"role": role, "rollback": rolled_back, "legacyRestored": True}


def worker_commit(spec: Spec, role: str, evidence: Path) -> dict[str, Any]:
    prepared = load_prepared(spec)
    host = spec.host(role)
    swbctl = release_swbctl(spec, host, prepared)
    result = json_command(
        core_argv(
            host,
            swbctl,
            "cutover",
            "commit",
            "--evidence",
            str(evidence),
        )
    )
    if result.get("activationState") != "committed":
        raise CutoverFailure("core commit did not cross the activation boundary")
    return {"role": role, "status": result}


def worker_activate_core(spec: Spec, role: str) -> dict[str, Any]:
    prepared = load_prepared(spec)
    host = spec.host(role)
    swbctl = release_swbctl(spec, host, prepared)
    worker_stage_core(spec, role)
    installed: list[str] = []
    for provider in sorted(host.provider_executables):
        json_command(
            core_argv(
                host,
                swbctl,
                "hooks",
                "install",
                "--provider",
                provider,
                "--executable",
                str(host.bin_link),
            )
        )
        installed.append(provider)
    return {"role": role, "activeSwbctl": str(host.bin_link), "hooks": installed}


def navigator_reachability(raw: bytes, host_id: str) -> str:
    try:
        value = json.loads(raw)
        hosts = value["hosts"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CutoverFailure("navigator connectivity evidence is invalid") from error
    if not isinstance(hosts, list) or not all(isinstance(item, dict) for item in hosts):
        raise CutoverFailure("navigator host collection is invalid")
    matches = [item for item in hosts if item.get("hostId") == host_id]
    if len(matches) != 1 or not isinstance(matches[0].get("reachability"), str):
        raise CutoverFailure("navigator remote host identity is ambiguous")
    return str(matches[0]["reachability"])


def remote_connectivity_evidence(
    spec: Spec, prepared: dict[str, Any]
) -> dict[str, str]:
    local = spec.host("desktop_primary")
    remote = spec.host("remote_owner")
    swbctl = release_swbctl(spec, local, prepared)

    def refresh(expected: str) -> bytes:
        raw = run(
            core_argv(local, swbctl, "state", "navigator", "--refresh", "--json"),
            timeout=180,
        ).stdout
        if navigator_reachability(raw, remote.host_id) != expected:
            raise CutoverFailure(f"remote host did not become {expected}")
        return raw.rstrip(b"\n")

    online = refresh("online")
    worker_call(spec, "remote_owner", "hide-core")
    try:
        offline = refresh("offline")
    finally:
        worker_call(spec, "remote_owner", "stage-core")
    restored = refresh("online")
    return {
        "remoteOnline": digest_bytes(online + b"\n" + restored),
        "remoteOffline": digest_bytes(offline),
    }


def service_properties(service: str) -> dict[str, str]:
    result = run(
        [
            "systemctl",
            "--user",
            "show",
            service,
            "-p",
            "ActiveState",
            "-p",
            "InvocationID",
            "-p",
            "MainPID",
            "-p",
            "ExecMainStartTimestampMonotonic",
            "--no-pager",
        ]
    )
    values: dict[str, str] = {}
    for line in result.stdout.decode().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def dms_state_value(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CutoverFailure("DMS plugin state is missing or invalid") from error
    if not isinstance(value, dict):
        raise CutoverFailure("DMS plugin state is invalid")
    if (
        "last_good_model_v5_bridge4" in value
        and "last_good_switchboard_entry_model_v1" not in value
    ):
        raise CutoverFailure("new DMS cache key was not created")
    current = value.get("last_good_switchboard_entry_model_v1")
    if not isinstance(current, dict):
        raise CutoverFailure("DMS entry cache is missing")
    return canonical(current), value


def backup_optional_file(source: Path, target: Path) -> dict[str, Any]:
    if source.is_symlink() or (source.exists() and not source.is_file()):
        raise CutoverFailure(f"backup source is unsafe: {source}")
    if not source.exists():
        return {"path": str(source), "existed": False}
    shutil.copy2(source, target, follow_symlinks=False)
    target.chmod(0o400)
    return {
        "path": str(source),
        "existed": True,
        "backupName": target.name,
        "sha256": digest_file(target),
    }


def dms_enabled(settings: Path) -> bool:
    if not settings.exists():
        return False
    try:
        value = json.loads(settings.read_bytes())
        enabled = value.get("switchboard", {}).get("enabled", False)
    except (OSError, AttributeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CutoverFailure("DMS plugin settings are invalid") from error
    if not isinstance(enabled, bool):
        raise CutoverFailure("DMS Switchboard enablement is invalid")
    return enabled


def dms_backup(spec: Spec) -> dict[str, Any]:
    desktop = spec.desktop
    local = spec.host("desktop_primary")
    backup = local.backup_root / spec.cutover_id / "dms"
    manifest_path = backup / "inventory.json"
    if backup.exists():
        try:
            value = json.loads(manifest_path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CutoverFailure("DMS backup is incomplete") from error
        if not isinstance(value, dict) or value.get("cutoverId") != spec.cutover_id:
            raise CutoverFailure("DMS backup belongs to another cutover")
        return value
    temporary = backup.with_name(f".{backup.name}.{uuid4()}.tmp")
    temporary.mkdir(mode=0o700)
    active = desktop.plugin_dir / "switchboard"
    try:
        if active.is_symlink():
            active_plugin: dict[str, Any] = {
                "existed": True,
                "target": os.readlink(active),
            }
        elif active.exists():
            raise CutoverFailure("active Switchboard plugin is not a symlink")
        else:
            active_plugin = {"existed": False, "target": None}
        state = backup_optional_file(
            desktop.plugin_state, temporary / "switchboard_state.json"
        )
        settings = backup_optional_file(
            desktop.plugin_settings, temporary / "plugin_settings.json"
        )
        service = service_properties(desktop.service)
        result = {
            "cutoverId": spec.cutover_id,
            "activePlugin": active_plugin,
            "pluginState": state,
            "pluginSettings": settings,
            "pluginEnabled": dms_enabled(desktop.plugin_settings),
            "serviceWasActive": service.get("ActiveState") == "active",
            "serviceIdentity": service,
        }
        write_private(temporary / "inventory.json", result, mode=0o400)
        temporary.rename(backup)
        return result
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def restore_optional_file(record: object, backup: Path, destination: Path) -> None:
    if not isinstance(record, dict) or record.get("path") != str(destination):
        raise CutoverFailure("DMS backup file identity is incompatible")
    if record.get("existed") is True:
        source = backup / str(record.get("backupName"))
        if digest_file(source) != record.get("sha256"):
            raise CutoverFailure("DMS backup file hash changed")
        restore_file(source, destination)
    elif record.get("existed") is False:
        if destination.is_symlink() or (
            destination.exists() and not destination.is_file()
        ):
            raise CutoverFailure("DMS rollback destination is unsafe")
        destination.unlink(missing_ok=True)
    else:
        raise CutoverFailure("DMS backup file existence is invalid")


def dms_restore(spec: Spec) -> dict[str, Any]:
    desktop = spec.desktop
    local = spec.host("desktop_primary")
    backup = local.backup_root / spec.cutover_id / "dms"
    inventory = dms_backup(spec)
    run(["dms", "ipc", "call", "launcher", "close"], check=False)
    run(["dms", "ipc", "call", "plugins", "disable", "switchboard"], check=False)
    run(["systemctl", "--user", "stop", desktop.service], timeout=60)
    active = desktop.plugin_dir / "switchboard"
    if active.is_symlink():
        active.unlink()
    elif active.exists():
        raise CutoverFailure("DMS rollback plugin path is unsafe")
    original = inventory.get("activePlugin")
    if not isinstance(original, dict):
        raise CutoverFailure("DMS plugin backup identity is invalid")
    if original.get("existed") is True and isinstance(original.get("target"), str):
        replace_symlink(active, str(original["target"]))
    elif original != {"existed": False, "target": None}:
        raise CutoverFailure("DMS plugin backup target is invalid")
    restore_optional_file(inventory.get("pluginState"), backup, desktop.plugin_state)
    restore_optional_file(
        inventory.get("pluginSettings"), backup, desktop.plugin_settings
    )
    if inventory.get("serviceWasActive") is True:
        run(["systemctl", "--user", "start", desktop.service], timeout=60)
        run(["dms", "ipc", "call", "plugin-scan", "rescan"], check=False)
        if inventory.get("pluginEnabled") is True:
            run(
                ["dms", "ipc", "call", "plugins", "enable", "switchboard"],
                check=False,
            )
    return {"restored": True, "serviceActive": inventory.get("serviceWasActive")}


def wait_for_dms(*, minimum_generation: int = -1) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = run(
            ["dms", "ipc", "call", "switchboard-launcher", "status"],
            timeout=5,
            check=False,
        )
        try:
            candidate = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        if (
            candidate.get("adapterVersion") == DMS_VERSION
            and candidate.get("bridgeVersion") == 1
            and candidate.get("modelVersion") == 1
            and candidate.get("idle")
            and candidate.get("hasModel")
            and candidate.get("fresh")
            and candidate.get("runGeneration", -1) > minimum_generation
        ):
            return candidate
        time.sleep(0.1)
    raise CutoverFailure("DMS 0.5 did not produce a fresh entry model")


def dms_cold_start(spec: Spec, prepared: dict[str, Any]) -> dict[str, Any]:
    desktop = spec.desktop
    local = spec.host("desktop_primary")
    inventory = dms_backup(spec)
    active = desktop.plugin_dir / "switchboard"
    run(["dms", "ipc", "call", "launcher", "close"], check=False)
    run(["dms", "ipc", "call", "plugins", "disable", "switchboard"], check=False)
    run(["systemctl", "--user", "stop", desktop.service], timeout=60)
    before = service_properties(desktop.service)
    if before.get("ActiveState") != "inactive":
        raise CutoverFailure("DMS did not stop before plugin replacement")
    if active.is_symlink():
        active.unlink()
    installer = desktop.dms_repo / "scripts" / "install-plugin"
    staged = json_command(
        [
            str(installer),
            "--plugin-dir",
            str(desktop.plugin_dir),
            "stage",
            "--archive",
            str(spec.workspace / "artifacts" / "switchboard-dms-0.5.0.zip"),
        ]
    )
    if staged.get("sha256") != prepared["dmsArtifactSha256"]:
        raise CutoverFailure("staged DMS artifact hash changed")
    json_command(
        [
            str(installer),
            "--plugin-dir",
            str(desktop.plugin_dir),
            "activate",
            "--staged",
            staged["staged"],
        ]
    )
    run(["systemctl", "--user", "start", desktop.service], timeout=60)
    run(["dms", "ipc", "call", "plugin-scan", "rescan"], check=False)
    run(["dms", "ipc", "call", "plugins", "enable", "switchboard"], check=False)
    status = wait_for_dms()
    cold, _all_state = dms_state_value(desktop.plugin_state)
    active_target = Path(str(staged["staged"]))
    model_envelope = json_command(
        [
            str(active_target / "switchboard-bridge"),
            "--swbctl",
            str(local.bin_link),
            "--refresh",
        ],
        timeout=60,
    )
    if model_envelope.get("bridgeVersion") != 1 or model_envelope.get("ok") is not True:
        raise CutoverFailure("DMS bridge did not return entry model v1")
    model = canonical(model_envelope["model"])
    blocked = run(
        [
            str(active_target / "switchboard-open"),
            "--swbctl",
            str(local.bin_link),
            "--host",
            local.host_id,
            "--project",
            local.project_id,
            "--request-id",
            str(uuid4()),
        ],
        timeout=60,
        check=False,
    )
    try:
        blocked_value = json.loads(blocked.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CutoverFailure("DMS staged action did not return JSON") from error
    if (
        blocked.returncode == 0
        or blocked_value.get("actionVersion") != 1
        or blocked_value.get("ok") is not False
        or blocked_value.get("error", {}).get("code") != "cutover_staged"
    ):
        raise CutoverFailure("DMS action was not blocked by the staged generation")
    blocked_action = canonical(blocked_value)
    run(["dms", "ipc", "call", "switchboard-launcher", "refresh"])
    status = wait_for_dms(minimum_generation=int(status["runGeneration"]))
    warm, _all_state = dms_state_value(desktop.plugin_state)
    after = service_properties(desktop.service)
    process_id = ":".join(
        (
            Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            after.get("InvocationID", ""),
            after.get("MainPID", ""),
            after.get("ExecMainStartTimestampMonotonic", ""),
        )
    )
    if (
        after.get("ActiveState") != "active"
        or not after.get("InvocationID")
        or after.get("InvocationID")
        == inventory.get("serviceIdentity", {}).get("InvocationID")
    ):
        raise CutoverFailure("DMS cold start did not establish a fresh process")
    run(["dms", "ipc", "call", "plugins", "disable", "switchboard"], check=False)
    run(["systemctl", "--user", "stop", desktop.service], timeout=60)
    return {
        "hostId": local.host_id,
        "processStartId": process_id,
        "modelSha256": digest_bytes(model),
        "coldCacheSha256": digest_bytes(cold),
        "warmCacheSha256": digest_bytes(warm),
        "checks": {
            "dmsModel": digest_bytes(model + blocked_action),
            "dmsColdCache": digest_bytes(cold),
            "dmsWarmCache": digest_bytes(warm),
        },
    }


def evidence(
    spec: Spec,
    prepared: dict[str, Any],
    validations: dict[str, dict[str, Any]],
    dms: dict[str, Any],
    connectivity: dict[str, str],
) -> dict[str, Any]:
    if set(connectivity) != {"remoteOnline", "remoteOffline"}:
        raise CutoverFailure("remote connectivity evidence is incomplete")
    if connectivity["remoteOnline"] == connectivity["remoteOffline"]:
        raise CutoverFailure("online and offline evidence must be distinct")
    host_check_names = {
        "coreDoctor",
        "reconciliation",
        "stagedMutationBlock",
        "hostState",
        "navigatorState",
    }
    checks = {
        name: digest_bytes(
            canonical({role: validations[role]["checks"][name] for role in ROLES})
        )
        for name in host_check_names
    }
    checks.update(dms["checks"])
    checks.update(connectivity)
    if set(checks) != set(REQUIRED_CHECKS):
        raise CutoverFailure("acceptance check evidence is incomplete")
    return {
        "evidenceVersion": 1,
        "capturedAt": int(time.time() * 1000),
        "core": {
            "version": CORE_VERSION,
            "commit": spec.core_commit,
            "artifactSha256": prepared["coreArtifactSha256"],
        },
        "dms": {
            "version": DMS_VERSION,
            "commit": spec.dms_commit,
            "artifactSha256": prepared["dmsArtifactSha256"],
        },
        "hosts": [
            {
                "role": role,
                "hostId": validations[role]["hostId"],
                "generationId": validations[role]["generationId"],
                "providerVersions": validations[role]["providerVersions"],
                "stagedReads": {
                    "hostStateSha256": validations[role]["hostStateSha256"],
                    "navigatorStateSha256": validations[role]["navigatorStateSha256"],
                },
            }
            for role in ROLES
        ],
        "dmsColdStart": {
            key: dms[key]
            for key in (
                "hostId",
                "processStartId",
                "modelSha256",
                "coldCacheSha256",
                "warmCacheSha256",
            )
        },
        "checks": checks,
    }


def activate_dms(spec: Spec) -> dict[str, Any]:
    run(["systemctl", "--user", "start", spec.desktop.service], timeout=60)
    run(["dms", "ipc", "call", "plugin-scan", "rescan"], check=False)
    run(
        ["dms", "ipc", "call", "plugins", "enable", "switchboard"],
        check=False,
    )
    return wait_for_dms()


def resume_current_session(spec: Spec, prepared: dict[str, Any]) -> dict[str, Any]:
    local = spec.host("desktop_primary")
    swbctl = release_swbctl(spec, local, prepared)
    active = spec.desktop.plugin_dir / "switchboard"
    if not active.is_symlink():
        raise CutoverFailure("DMS replacement plugin is not active")
    opener = active.resolve() / "switchboard-open"
    request_id = str(uuid4())
    opened = json_command(
        [
            str(opener),
            "--swbctl",
            str(local.bin_link),
            "--host",
            local.host_id,
            "--project",
            local.project_id,
            "--request-id",
            request_id,
        ],
        timeout=60,
    )
    if (
        opened.get("actionVersion") != 1
        or opened.get("ok") is not True
        or opened.get("action", {}).get("requestId") != request_id
    ):
        raise CutoverFailure("first DMS project action was not accepted")
    view_id = exact_uuid(opened["action"].get("viewId"), "first view")
    json_command(
        core_argv(
            local,
            swbctl,
            "view",
            "mode",
            "--view",
            view_id,
            "--mode",
            "direct",
            "--request-id",
            str(uuid4()),
        )
    )
    view = json_command(core_argv(local, swbctl, "view", "show", "--view", view_id))
    frame_id = exact_uuid(view.get("activeFrameId"), "first workspace frame")
    resumed = json_command(
        core_argv(
            local,
            swbctl,
            "frame",
            "reopen",
            "--host",
            local.host_id,
            "--frame",
            frame_id,
            "--session",
            spec.current_session_key,
            "--request-id",
            str(uuid4()),
        ),
        timeout=180,
    )
    if (
        resumed.get("sessionKey") != spec.current_session_key
        or resumed.get("runtimePresence") != "live"
    ):
        raise CutoverFailure("exact imported provider UUID did not resume")
    before = wait_for_dms()
    run(["dms", "ipc", "call", "switchboard-launcher", "refresh"])
    dms = wait_for_dms(minimum_generation=int(before["runGeneration"]))
    navigator = run(
        core_argv(local, swbctl, "state", "navigator", "--refresh", "--json"),
        timeout=180,
    ).stdout.rstrip(b"\n")
    return {
        "viewId": view_id,
        "frameId": frame_id,
        "sessionKey": spec.current_session_key,
        "navigatorSha256": digest_bytes(navigator),
        "dmsStatusSha256": digest_bytes(canonical(dms)),
    }


def plain_shell_guard() -> None:
    forbidden = (
        "TMUX",
        "AGENT_SWITCHBOARD_CAPABILITY",
        "AGENT_SWITCHBOARD_LAUNCH_ID",
        "AGENT_SWITCHBOARD_SURFACE_ID",
        "SWB_V3_SESSION_KEY",
        "SWB_V3_CONFIG_ROOT",
        "SWB_V3_STATE_ROOT",
        "SWB_V3_MCP_COMMAND",
    )
    present = [name for name in forbidden if os.environ.get(name)]
    if present:
        raise CutoverFailure(
            "execute must start from a plain shell; managed variables are present: "
            + ", ".join(present)
        )


def journal(spec: Spec, phase: str, detail: object) -> None:
    path = spec.workspace / "journal.json"
    prior: list[Any] = []
    if path.exists():
        prior = json.loads(path.read_bytes())
    prior.append({"phase": phase, "at": int(time.time() * 1000), "detail": detail})
    write_private(path, prior, mode=0o400)


def worker_call(spec: Spec, role: str, action: str, *extra: str) -> dict[str, Any]:
    host = spec.host(role)
    script = spec.workspace / Path(__file__).name
    argv = [
        str(host.python),
        str(script),
        "worker",
        "--spec",
        str(spec.workspace / "spec.json"),
        "--role",
        role,
        "--action",
        action,
        *extra,
    ]
    if host.ssh_target is not None:
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            host.ssh_target,
            *argv,
        ]
    result = run(argv, timeout=600, check=False)
    try:
        value = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CutoverFailure(
            f"{role} {action} worker did not return JSON (exit {result.returncode})"
        ) from error
    if not isinstance(value, dict):
        raise CutoverFailure(f"{role} {action} worker returned incompatible JSON")
    if result.returncode != 0:
        failure = value.get("error")
        message = failure.get("message") if isinstance(failure, dict) else None
        detail = (
            str(message)[:512]
            if isinstance(message, str) and message
            else f"worker exited {result.returncode}"
        )
        raise CutoverFailure(f"{role} {action} failed: {detail}")
    return value


def sync_remote(spec: Spec) -> None:
    remote = spec.host("remote_owner")
    assert remote.ssh_target is not None
    parent = spec.workspace.parent
    common = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        remote.ssh_target,
    ]
    existing = run(
        [*common, "test", "-f", str(spec.workspace / "prepared.json")],
        check=False,
    )
    if existing.returncode == 0:
        return
    incomplete = run([*common, "test", "-e", str(spec.workspace)], check=False)
    if incomplete.returncode == 0:
        raise CutoverFailure("remote cutover workspace exists without a manifest")
    run([*common, "mkdir", "-p", str(parent)])
    run(
        [
            "scp",
            "-q",
            "-r",
            str(spec.workspace),
            f"{remote.ssh_target}:{parent}",
        ],
        timeout=600,
    )


def execute(spec: Spec, confirmation: str, *, sync: bool) -> dict[str, Any]:
    plain_shell_guard()
    if confirmation != spec.cutover_id:
        raise CutoverFailure("confirmation must equal cutoverId")
    git_exact(spec.core_repo, spec.core_commit)
    git_exact(spec.desktop.dms_repo, spec.dms_commit)
    prepared = load_prepared(spec)
    if sync:
        sync_remote(spec)
    journal(spec, "start", {"forwardOnly": False})
    prepared_roles: list[str] = []
    staged: list[str] = []
    committed: list[str] = []
    dms_touched = False
    try:
        inventories: dict[str, dict[str, Any]] = {}
        for role in reversed(ROLES):
            inventories[role] = worker_call(spec, role, "stage")
            prepared_roles.append(role)
        journal(spec, "inactive_installed", inventories)
        imports: dict[str, dict[str, Any]] = {}
        for role in reversed(ROLES):
            imports[role] = worker_call(spec, role, "import")
            staged.append(role)
        journal(spec, "staged", imports)
        staged_links = {
            role: worker_call(spec, role, "stage-core") for role in reversed(ROLES)
        }
        journal(spec, "staged_read_routes", staged_links)
        validations = {
            role: worker_call(spec, role, "validate") for role in reversed(ROLES)
        }
        connectivity = remote_connectivity_evidence(spec, prepared)
        dms_touched = True
        dms = dms_cold_start(spec, prepared)
        cutover_evidence = evidence(spec, prepared, validations, dms, connectivity)
        evidence_path = spec.workspace / "cutover-evidence.json"
        write_private(evidence_path, cutover_evidence, mode=0o400)
        if sync:
            remote = spec.host("remote_owner")
            assert remote.ssh_target is not None
            run(
                [
                    "scp",
                    "-q",
                    str(evidence_path),
                    f"{remote.ssh_target}:{evidence_path}",
                ]
            )
        journal(spec, "evidence", {"sha256": digest_file(evidence_path)})
        for role in ("remote_owner", "desktop_primary"):
            result = worker_call(spec, role, "commit", "--evidence", str(evidence_path))
            committed.append(role)
            journal(spec, "committed", result)
        activations = {
            role: worker_call(spec, role, "activate-core")
            for role in ("remote_owner", "desktop_primary")
        }
        dms_activation = activate_dms(spec)
        resumed = resume_current_session(spec, prepared)
        journal(
            spec,
            "activated",
            {"core": activations, "dms": dms_activation, "resume": resumed},
        )
        return {
            "cutoverId": spec.cutover_id,
            "evidenceSha256": digest_file(evidence_path),
            "committed": committed,
            "activation": activations,
            "resume": resumed,
        }
    except Exception:
        if not committed:
            rollback_errors: list[str] = []
            for role in reversed(prepared_roles):
                try:
                    worker_call(spec, role, "rollback")
                except Exception as error:
                    rollback_errors.append(f"{role}: {error}")
            if dms_touched:
                try:
                    dms_restore(spec)
                except Exception as error:
                    rollback_errors.append(f"dms: {error}")
            journal(
                spec,
                "rolled_back",
                {"staged": staged, "errors": rollback_errors},
            )
        else:
            journal(spec, "forward_recovery_required", {"committed": committed})
        raise


def worker(spec: Spec, role: str, action: str, evidence_path: Path | None) -> Any:
    if action == "stage":
        return worker_stage(spec, role)
    if action == "import":
        return worker_import(spec, role)
    if action == "validate":
        return worker_validate(spec, role)
    if action == "stage-core":
        return worker_stage_core(spec, role)
    if action == "hide-core":
        return worker_hide_core(spec, role)
    if action == "rollback":
        return worker_rollback(spec, role)
    if action == "commit":
        if evidence_path is None:
            raise CutoverFailure("worker commit requires evidence")
        return worker_commit(spec, role, evidence_path)
    if action == "activate-core":
        return worker_activate_core(spec, role)
    raise CutoverFailure("worker action is unsupported")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="phase6e-cutover")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("validate-spec", "prepare", "status"):
        command = commands.add_parser(name)
        command.add_argument("--spec", required=True, type=Path)
    execute_parser = commands.add_parser("execute")
    execute_parser.add_argument("--spec", required=True, type=Path)
    execute_parser.add_argument("--confirm", required=True)
    execute_parser.add_argument("--no-sync", action="store_true")
    worker_parser = commands.add_parser("worker")
    worker_parser.add_argument("--spec", required=True, type=Path)
    worker_parser.add_argument("--role", required=True, choices=ROLES)
    worker_parser.add_argument(
        "--action",
        required=True,
        choices=(
            "stage",
            "import",
            "stage-core",
            "hide-core",
            "validate",
            "rollback",
            "commit",
            "activate-core",
        ),
    )
    worker_parser.add_argument("--evidence", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        spec = Spec.from_path(arguments.spec)
        if arguments.command == "validate-spec":
            value: Any = {"valid": True, "cutoverId": spec.cutover_id}
        elif arguments.command == "prepare":
            value = prepare(spec)
        elif arguments.command == "status":
            prepared = load_prepared(spec)
            journal_path = spec.workspace / "journal.json"
            value = {
                "prepared": prepared,
                "journal": (
                    []
                    if not journal_path.exists()
                    else json.loads(journal_path.read_bytes())
                ),
            }
        elif arguments.command == "execute":
            value = execute(spec, arguments.confirm, sync=not arguments.no_sync)
        else:
            value = worker(spec, arguments.role, arguments.action, arguments.evidence)
        os.write(sys.stdout.fileno(), canonical(value))
        return 0
    except CutoverFailure as error:
        os.write(
            sys.stdout.fileno(),
            canonical(
                {
                    "ok": False,
                    "error": {
                        "code": "phase6e_cutover_failed",
                        "message": str(error)[:1024],
                    },
                }
            ),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
