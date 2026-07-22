"""Generation-safe Phase 6 construction, resolution, and activation.

The state-home ``current`` symlink is the only activation coordinate.  Config
and registry files are written and validated under the same opaque generation
ID before that pointer can move.  Normal open never creates missing files.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from .config import SwitchboardConfig, parse_config, render_config
from .cutover import CutoverBundle, CutoverError
from .domain import ActivationState, GenerationId, HostId, canonical_json
from .storage import Registry

CORE_TARGET_VERSION: Final = "0.3.0"
DMS_TARGET_VERSION: Final = "0.5.0"
CUTOVER_MANIFEST_VERSION: Final = 1
FRESH_MANIFEST_VERSION: Final = 2
EVIDENCE_VERSION: Final = 1
_POINTER_TARGET_PARTS: Final = 2
_SHA256_LENGTH: Final = 64
_GIT_COMMIT_LENGTH: Final = 40
_HOST_ROLES: Final = frozenset({"desktop_primary", "remote_owner"})
_REQUIRED_ACCEPTANCE_CHECKS: Final = frozenset(
    {
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
    }
)


class GenerationError(RuntimeError):
    """Generation construction or activation failed closed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class GenerationPaths:
    """Explicit roots used by an isolated or installed Phase 6 runtime."""

    config_root: Path
    state_root: Path

    @classmethod
    def from_xdg(cls, config_home: Path, state_home: Path) -> GenerationPaths:
        return cls(
            Path(config_home) / "agent-switchboard",
            Path(state_home) / "agent-switchboard",
        )

    @property
    def current(self) -> Path:
        return self.state_root / "current"

    @property
    def lock(self) -> Path:
        return self.state_root / "cutover.lock"

    def config_generation(self, generation_id: GenerationId) -> Path:
        return self.config_root / "generations" / str(generation_id)

    def state_generation(self, generation_id: GenerationId) -> Path:
        return self.state_root / "generations" / str(generation_id)


@dataclass(frozen=True, slots=True)
class CutoverEvidence:
    """Strict, canonical evidence for one paired two-host activation."""

    value: Mapping[str, Any]

    @classmethod
    def from_json(cls, raw: bytes) -> CutoverEvidence:
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GenerationError("cutover_evidence_invalid", str(error)) from error
        return cls.from_dict(value)

    @classmethod
    def from_dict(cls, value: object) -> CutoverEvidence:
        if not isinstance(value, dict):
            raise GenerationError(
                "cutover_evidence_invalid", "evidence must be an object"
            )
        _exact_fields(
            value,
            {
                "evidenceVersion",
                "capturedAt",
                "core",
                "dms",
                "hosts",
                "dmsColdStart",
                "checks",
            },
            "evidence",
        )
        if value["evidenceVersion"] != EVIDENCE_VERSION:
            raise GenerationError(
                "cutover_evidence_invalid", "evidence version is incompatible"
            )
        _timestamp_value(value["capturedAt"], "capturedAt")
        core = _artifact_evidence(value["core"], "core", CORE_TARGET_VERSION)
        dms = _artifact_evidence(value["dms"], "dms", DMS_TARGET_VERSION)
        hosts_raw = value["hosts"]
        if not isinstance(hosts_raw, list) or len(hosts_raw) != 2:
            raise GenerationError(
                "cutover_evidence_invalid", "hosts must contain exactly two records"
            )
        hosts = [_host_evidence(item) for item in hosts_raw]
        roles = {item["role"] for item in hosts}
        if roles != _HOST_ROLES or len({item["hostId"] for item in hosts}) != 2:
            raise GenerationError(
                "cutover_evidence_invalid",
                "host roles and identities must be exact and distinct",
            )
        if len({item["generationId"] for item in hosts}) != 2:
            raise GenerationError(
                "cutover_evidence_invalid", "host generations must be distinct"
            )
        cold = _cold_start_evidence(value["dmsColdStart"])
        desktop = next(item for item in hosts if item["role"] == "desktop_primary")
        if cold["hostId"] != desktop["hostId"]:
            raise GenerationError(
                "cutover_evidence_invalid",
                "DMS cold start must belong to desktop_primary",
            )
        checks_raw = value["checks"]
        if (
            not isinstance(checks_raw, dict)
            or set(checks_raw) != _REQUIRED_ACCEPTANCE_CHECKS
        ):
            raise GenerationError(
                "cutover_evidence_invalid", "named acceptance checks are incomplete"
            )
        checks = {
            key: _sha256(value, f"checks.{key}") for key, value in checks_raw.items()
        }
        normalized = {
            "evidenceVersion": EVIDENCE_VERSION,
            "capturedAt": value["capturedAt"],
            "core": core,
            "dms": dms,
            "hosts": sorted(hosts, key=lambda item: item["role"]),
            "dmsColdStart": cold,
            "checks": dict(sorted(checks.items())),
        }
        return cls(normalized)

    def to_json(self) -> bytes:
        return canonical_json(dict(self.value)).encode()

    @property
    def captured_at(self) -> int:
        return int(self.value["capturedAt"])

    def includes_generation(self, generation_id: GenerationId) -> bool:
        return any(
            item["generationId"] == str(generation_id) for item in self.value["hosts"]
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json()).hexdigest()


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise GenerationError(
            "cutover_evidence_invalid", f"{label} fields are incompatible"
        )


def _timestamp_value(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GenerationError("cutover_evidence_invalid", f"{label} is invalid")
    return value


def _bounded_text(value: object, label: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode()) > maximum
        or "\x00" in value
    ):
        raise GenerationError("cutover_evidence_invalid", f"{label} is invalid")
    return value


def _sha256(value: object, label: str) -> str:
    text = _bounded_text(value, label, maximum=_SHA256_LENGTH)
    if len(text) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise GenerationError(
            "cutover_evidence_invalid", f"{label} is not a lowercase SHA-256"
        )
    return text


def _artifact_evidence(
    value: object, label: str, target_version: str
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise GenerationError("cutover_evidence_invalid", f"{label} must be an object")
    _exact_fields(value, {"version", "commit", "artifactSha256"}, label)
    version = _bounded_text(value["version"], f"{label}.version")
    commit_hash = _bounded_text(
        value["commit"], f"{label}.commit", maximum=_GIT_COMMIT_LENGTH
    )
    if version != target_version:
        raise GenerationError(
            "cutover_evidence_invalid", f"{label} version is incompatible"
        )
    if len(commit_hash) != _GIT_COMMIT_LENGTH or any(
        character not in "0123456789abcdef" for character in commit_hash
    ):
        raise GenerationError(
            "cutover_evidence_invalid", f"{label} commit is not an exact Git object"
        )
    return {
        "version": version,
        "commit": commit_hash,
        "artifactSha256": _sha256(value["artifactSha256"], f"{label}.artifactSha256"),
    }


def _host_evidence(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GenerationError(
            "cutover_evidence_invalid", "host evidence must be an object"
        )
    _exact_fields(
        value,
        {"role", "hostId", "generationId", "providerVersions", "stagedReads"},
        "host",
    )
    role = _bounded_text(value["role"], "host.role")
    if role not in _HOST_ROLES:
        raise GenerationError("cutover_evidence_invalid", "host role is incompatible")
    try:
        host_id = str(HostId(value["hostId"]))
        generation_id = str(GenerationId(value["generationId"]))
    except Exception as error:
        raise GenerationError(
            "cutover_evidence_invalid", "host identity is invalid"
        ) from error
    providers = value["providerVersions"]
    if (
        not isinstance(providers, dict)
        or not providers
        or not set(providers) <= {"codex", "claude"}
    ):
        raise GenerationError(
            "cutover_evidence_invalid", "provider observations are invalid"
        )
    provider_versions = {
        key: _bounded_text(item, f"providerVersions.{key}")
        for key, item in providers.items()
    }
    reads = value["stagedReads"]
    if not isinstance(reads, dict):
        raise GenerationError(
            "cutover_evidence_invalid", "staged reads must be an object"
        )
    _exact_fields(reads, {"hostStateSha256", "navigatorStateSha256"}, "stagedReads")
    return {
        "role": role,
        "hostId": host_id,
        "generationId": generation_id,
        "providerVersions": dict(sorted(provider_versions.items())),
        "stagedReads": {
            "hostStateSha256": _sha256(
                reads["hostStateSha256"], "stagedReads.hostStateSha256"
            ),
            "navigatorStateSha256": _sha256(
                reads["navigatorStateSha256"], "stagedReads.navigatorStateSha256"
            ),
        },
    }


def _cold_start_evidence(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise GenerationError(
            "cutover_evidence_invalid", "DMS cold start must be an object"
        )
    _exact_fields(
        value,
        {
            "hostId",
            "processStartId",
            "modelSha256",
            "coldCacheSha256",
            "warmCacheSha256",
        },
        "dmsColdStart",
    )
    try:
        host_id = str(HostId(value["hostId"]))
    except Exception as error:
        raise GenerationError(
            "cutover_evidence_invalid", "DMS cold-start host is invalid"
        ) from error
    return {
        "hostId": host_id,
        "processStartId": _bounded_text(
            value["processStartId"], "dmsColdStart.processStartId"
        ),
        "modelSha256": _sha256(value["modelSha256"], "dmsColdStart.modelSha256"),
        "coldCacheSha256": _sha256(
            value["coldCacheSha256"], "dmsColdStart.coldCacheSha256"
        ),
        "warmCacheSha256": _sha256(
            value["warmCacheSha256"], "dmsColdStart.warmCacheSha256"
        ),
    }


@dataclass(frozen=True, slots=True)
class GenerationStatus:
    generation_id: GenerationId
    activation_state: ActivationState
    previous_generation_id: GenerationId | None
    source_kind: str
    source_sha256: str
    created_at: int
    committed_at: int | None
    evidence_sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "generationId": str(self.generation_id),
            "activationState": self.activation_state.value,
            "previousGenerationId": (
                None
                if self.previous_generation_id is None
                else str(self.previous_generation_id)
            ),
            "sourceKind": self.source_kind,
            "sourceSha256": self.source_sha256,
            "createdAt": self.created_at,
            "committedAt": self.committed_at,
            "evidenceSha256": self.evidence_sha256,
        }
        if self.source_kind == "cutover":
            result["bundleHash"] = self.source_sha256
        return result


@dataclass(slots=True)
class OpenGeneration:
    generation_id: GenerationId
    config: SwitchboardConfig
    registry: Registry
    activation_state: ActivationState

    def close(self) -> None:
        self.registry.close()

    def __enter__(self) -> OpenGeneration:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def require_mutation(self, operation: str) -> None:
        if self.activation_state is ActivationState.CUTOVER_STAGED:
            raise GenerationError(
                "cutover_staged",
                f"{operation} is blocked until the paired cutover is committed",
            )


FaultInjector = Callable[[str], None]


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _write_file(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        mode,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - defensive OS boundary
                raise OSError("short generation write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _cutover_lock(paths: GenerationPaths) -> Iterator[None]:
    _secure_directory(paths.state_root)
    descriptor = os.open(
        paths.lock,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _fault(injector: FaultInjector | None, boundary: str) -> None:
    if injector is not None:
        injector(boundary)


def _read_pointer(paths: GenerationPaths, *, required: bool) -> GenerationId | None:
    try:
        target = os.readlink(paths.current)
    except FileNotFoundError:
        if required:
            if (paths.state_root / "switchboard.db").exists():
                raise GenerationError(
                    "cutover_required",
                    "legacy schema-v10 state is not a Phase 6 generation",
                ) from None
            raise GenerationError(
                "generation_missing", "current pointer is missing"
            ) from None
        return None
    except OSError as error:
        raise GenerationError("generation_pointer_invalid", str(error)) from error
    candidate = Path(target)
    if candidate.is_absolute() or candidate.parts[:1] != ("generations",):
        raise GenerationError(
            "generation_pointer_invalid", "current pointer has an unsafe target"
        )
    if len(candidate.parts) != _POINTER_TARGET_PARTS:
        raise GenerationError(
            "generation_pointer_invalid", "current pointer has an invalid target"
        )
    try:
        return GenerationId(candidate.parts[1])
    except Exception as error:
        raise GenerationError(
            "generation_pointer_invalid", "current pointer generation is invalid"
        ) from error


def resolve_current(paths: GenerationPaths) -> GenerationId:
    generation_id = _read_pointer(paths, required=True)
    assert generation_id is not None
    return generation_id


def _switch_pointer(paths: GenerationPaths, generation_id: GenerationId | None) -> None:
    _secure_directory(paths.state_root)
    temporary = paths.state_root / f".current-{uuid4()}"
    if generation_id is None:
        with suppress(FileNotFoundError):
            paths.current.unlink()
        _fsync_directory(paths.state_root)
        return
    os.symlink(f"generations/{generation_id}", temporary)
    try:
        os.replace(temporary, paths.current)
        _fsync_directory(paths.state_root)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _cutover_manifest_path(paths: GenerationPaths, generation_id: GenerationId) -> Path:
    return paths.state_generation(generation_id) / "cutover-manifest.json"


def _fresh_manifest_path(paths: GenerationPaths, generation_id: GenerationId) -> Path:
    return paths.state_generation(generation_id) / "generation-manifest.json"


def _evidence_path(paths: GenerationPaths, generation_id: GenerationId) -> Path:
    return paths.state_generation(generation_id) / "cutover-evidence.json"


def _read_manifest(
    paths: GenerationPaths, generation_id: GenerationId
) -> Mapping[str, Any]:
    fresh_path = _fresh_manifest_path(paths, generation_id)
    cutover_path = _cutover_manifest_path(paths, generation_id)
    if fresh_path.exists() == cutover_path.exists():
        raise GenerationError(
            "generation_manifest_invalid",
            "generation must contain exactly one manifest",
        )
    path = fresh_path if fresh_path.exists() else cutover_path
    _validate_regular_private_file(path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise GenerationError("generation_manifest_invalid", str(error)) from error
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GenerationError("generation_manifest_invalid", str(error)) from error
    if not isinstance(value, dict):
        raise GenerationError(
            "generation_manifest_invalid", "manifest fields are incompatible"
        )
    version = value.get("manifestVersion")
    expected = (
        {
            "manifestVersion",
            "generationId",
            "previousGenerationId",
            "bundleHash",
            "coreVersion",
            "dmsVersion",
            "createdAt",
        }
        if version == CUTOVER_MANIFEST_VERSION
        else {
            "manifestVersion",
            "generationId",
            "previousGenerationId",
            "sourceKind",
            "configSha256",
            "coreVersion",
            "dmsVersion",
            "createdAt",
        }
    )
    if version not in {CUTOVER_MANIFEST_VERSION, FRESH_MANIFEST_VERSION}:
        raise GenerationError(
            "generation_manifest_invalid", "manifest version is incompatible"
        )
    if set(value) != expected:
        raise GenerationError(
            "generation_manifest_invalid", "manifest fields are incompatible"
        )
    if value["generationId"] != str(generation_id):
        raise GenerationError(
            "generation_manifest_invalid", "manifest generation does not match"
        )
    previous = value["previousGenerationId"]
    if previous is not None:
        try:
            GenerationId(previous)
        except Exception as error:
            raise GenerationError(
                "generation_manifest_invalid",
                "previous generation is invalid",
            ) from error
    _timestamp_value(value["createdAt"], "createdAt")
    if version == FRESH_MANIFEST_VERSION:
        if value["sourceKind"] != "fresh":
            raise GenerationError(
                "generation_manifest_invalid", "generation source is incompatible"
            )
        _sha256(value["configSha256"], "configSha256")
    return value


def _validate_regular_private_file(path: Path) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise GenerationError("generation_incomplete", str(error)) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise GenerationError("generation_incomplete", f"{path.name} is not a file")
    if metadata.st_mode & 0o077:
        raise GenerationError("generation_permissions", f"{path.name} is not private")


def _open_exact(paths: GenerationPaths, generation_id: GenerationId) -> OpenGeneration:
    """Open final generation files without consulting the activation pointer."""

    config_path = paths.config_generation(generation_id) / "config.toml"
    database_path = paths.state_generation(generation_id) / "switchboard.db"
    _validate_regular_private_file(config_path)
    _validate_regular_private_file(database_path)
    try:
        config = parse_config(config_path.read_bytes())
    except Exception as error:
        raise GenerationError("generation_config_invalid", str(error)) from error
    if config.generation_id != generation_id:
        raise GenerationError(
            "generation_mismatch", "config generation does not match pointer"
        )
    registry: Registry | None = None
    try:
        registry = Registry(
            database_path,
            generation_id=generation_id,
            local_host_id=config.host.host_id,
            local_display_name=config.host.display_name,
        )
        metadata = registry.metadata()
        if metadata["generation_id"] != str(generation_id) or metadata[
            "local_host_id"
        ] != str(config.host.host_id):
            raise GenerationError(
                "generation_mismatch", "config and registry identity disagree"
            )
        try:
            activation_state = ActivationState(metadata["activation_state"])
        except ValueError as error:
            raise GenerationError(
                "generation_state_invalid", "registry activation state is invalid"
            ) from error
        return OpenGeneration(generation_id, config, registry, activation_state)
    except BaseException:
        if registry is not None:
            registry.close()
        raise


def open_generation(
    paths: GenerationPaths, generation_id: GenerationId | None = None
) -> OpenGeneration:
    """Open the exact current generation and reject a concurrent pointer change."""

    pointer_before = _read_pointer(paths, required=True)
    if generation_id is None:
        generation_id = pointer_before
    elif pointer_before != generation_id:
        raise GenerationError(
            "generation_not_active", "requested generation is not current"
        )
    assert generation_id is not None
    opened = _open_exact(paths, generation_id)
    try:
        pointer_after = _read_pointer(paths, required=True)
        if pointer_after != pointer_before:
            raise GenerationError(
                "generation_changed", "current generation changed during open"
            )
        return opened
    except BaseException:
        opened.close()
        raise


def _checkpoint_database(database: Path) -> None:
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA synchronous = FULL")
    finally:
        connection.close()
    _fsync_file(database)


def _publish_fresh_generation(
    config: SwitchboardConfig,
    paths: GenerationPaths,
    *,
    expected_current: GenerationId | None,
    created_at: int,
    fault_injector: FaultInjector | None = None,
) -> GenerationStatus:
    if not isinstance(config, SwitchboardConfig):
        raise GenerationError("generation_config_invalid", "init requires Config v3")
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, int)
        or created_at < 0
    ):
        raise GenerationError("generation_time_invalid", "init time is invalid")
    generation_id = config.generation_id
    config_payload = render_config(config).encode("utf-8")
    config_sha256 = hashlib.sha256(config_payload).hexdigest()
    with _cutover_lock(paths):
        current = _read_pointer(paths, required=False)
        if current != expected_current:
            if expected_current is None:
                raise GenerationError(
                    "generation_active", "a current generation already exists"
                )
            raise GenerationError(
                "generation_changed", "current generation does not match confirmation"
            )
        config_parent = paths.config_root / "generations"
        state_parent = paths.state_root / "generations"
        _secure_directory(config_parent)
        _secure_directory(state_parent)
        final_config = paths.config_generation(generation_id)
        final_state = paths.state_generation(generation_id)
        if final_config.exists() or final_state.exists():
            raise GenerationError(
                "generation_exists", "target generation already exists"
            )
        nonce = uuid4()
        temporary_config = config_parent / f".staging-{generation_id}-{nonce}"
        temporary_state = state_parent / f".staging-{generation_id}-{nonce}"
        temporary_config.mkdir(mode=0o700)
        temporary_state.mkdir(mode=0o700)
        try:
            config_file = temporary_config / "config.toml"
            database_file = temporary_state / "switchboard.db"
            manifest_file = temporary_state / "generation-manifest.json"
            _write_file(config_file, config_payload)
            with Registry(
                database_file,
                generation_id=generation_id,
                local_host_id=config.host.host_id,
                local_display_name=config.host.display_name,
                initial_activation_state=ActivationState.COMMITTED,
                now=created_at,
            ) as registry:
                registry.materialize_catalog(
                    config.host.host_id,
                    config.projects,
                    config.repositories,
                    config.project_repositories,
                    config.checkouts,
                    now=created_at,
                )
            _checkpoint_database(database_file)
            manifest = {
                "manifestVersion": FRESH_MANIFEST_VERSION,
                "generationId": str(generation_id),
                "previousGenerationId": (None if current is None else str(current)),
                "sourceKind": "fresh",
                "configSha256": config_sha256,
                "coreVersion": CORE_TARGET_VERSION,
                "dmsVersion": DMS_TARGET_VERSION,
                "createdAt": created_at,
            }
            _write_file(
                manifest_file,
                (canonical_json(manifest) + "\n").encode("utf-8"),
                mode=0o400,
            )
            _fsync_directory(temporary_config)
            _fsync_directory(temporary_state)
            _fault(fault_injector, "files_fsynced")
            os.replace(temporary_config, final_config)
            _fsync_directory(config_parent)
            _fault(fault_injector, "config_published")
            os.replace(temporary_state, final_state)
            _fsync_directory(state_parent)
            _fault(fault_injector, "state_published")
            with _open_exact(paths, generation_id) as opened:
                if opened.activation_state is not ActivationState.COMMITTED:
                    raise GenerationError(
                        "generation_state_invalid",
                        "fresh generation is not committed",
                    )
            _switch_pointer(paths, generation_id)
            _fault(fault_injector, "pointer_switched")
            return status(paths)
        finally:
            for temporary in (temporary_config, temporary_state):
                if temporary.exists():
                    shutil.rmtree(temporary)


def initialize(
    config: SwitchboardConfig,
    paths: GenerationPaths,
    *,
    created_at: int,
    fault_injector: FaultInjector | None = None,
) -> GenerationStatus:
    """Create the first committed generation without provider or tmux I/O."""

    return _publish_fresh_generation(
        config,
        paths,
        expected_current=None,
        created_at=created_at,
        fault_injector=fault_injector,
    )


def reset(
    config: SwitchboardConfig,
    paths: GenerationPaths,
    *,
    expected_current: GenerationId,
    created_at: int,
    fault_injector: FaultInjector | None = None,
) -> GenerationStatus:
    """Abandon exact current state by publishing a new empty generation."""

    if not isinstance(expected_current, GenerationId):
        raise GenerationError(
            "generation_confirmation_invalid", "reset confirmation is invalid"
        )
    return _publish_fresh_generation(
        config,
        paths,
        expected_current=expected_current,
        created_at=created_at,
        fault_injector=fault_injector,
    )


def import_bundle(
    bundle: CutoverBundle,
    paths: GenerationPaths,
    *,
    generation_id: GenerationId | None = None,
    fault_injector: FaultInjector | None = None,
) -> GenerationStatus:
    """Construct and activate one complete staged generation."""

    if not isinstance(bundle, CutoverBundle):
        raise GenerationError("bundle_invalid", "import requires CutoverBundle v1")
    generation_id = generation_id or GenerationId.new()
    config = bundle.target_config(generation_id)
    with _cutover_lock(paths):
        previous = _read_pointer(paths, required=False)
        config_parent = paths.config_root / "generations"
        state_parent = paths.state_root / "generations"
        _secure_directory(config_parent)
        _secure_directory(state_parent)
        final_config = paths.config_generation(generation_id)
        final_state = paths.state_generation(generation_id)
        if final_config.exists() or final_state.exists():
            raise GenerationError(
                "generation_exists", "target generation already exists"
            )
        nonce = uuid4()
        temporary_config = config_parent / f".staging-{generation_id}-{nonce}"
        temporary_state = state_parent / f".staging-{generation_id}-{nonce}"
        temporary_config.mkdir(mode=0o700)
        temporary_state.mkdir(mode=0o700)
        try:
            config_file = temporary_config / "config.toml"
            database_file = temporary_state / "switchboard.db"
            bundle_file = temporary_state / "cutover-bundle.json"
            manifest_file = temporary_state / "cutover-manifest.json"
            _write_file(config_file, render_config(config).encode("utf-8"))
            with Registry(
                database_file,
                generation_id=generation_id,
                local_host_id=bundle.host_id,
                local_display_name=config.host.display_name,
                initial_activation_state=ActivationState.CUTOVER_STAGED,
                now=bundle.exported_at,
            ) as registry:
                registry.materialize_catalog(
                    bundle.host_id,
                    config.projects,
                    config.repositories,
                    config.project_repositories,
                    config.checkouts,
                    now=bundle.exported_at,
                )
                for session in bundle.provider_sessions():
                    registry.upsert_provider_session(session)
                for handoff in bundle.handoffs():
                    registry.append_session_handoff(handoff)
            _checkpoint_database(database_file)
            _write_file(bundle_file, bundle.to_json().encode("utf-8"), mode=0o400)
            manifest = {
                "manifestVersion": CUTOVER_MANIFEST_VERSION,
                "generationId": str(generation_id),
                "previousGenerationId": None if previous is None else str(previous),
                "bundleHash": bundle.bundle_hash,
                "coreVersion": CORE_TARGET_VERSION,
                "dmsVersion": DMS_TARGET_VERSION,
                "createdAt": bundle.exported_at,
            }
            _write_file(
                manifest_file,
                (canonical_json(manifest) + "\n").encode("utf-8"),
                mode=0o400,
            )
            _fsync_directory(temporary_config)
            _fsync_directory(temporary_state)
            _fault(fault_injector, "files_fsynced")
            os.replace(temporary_config, final_config)
            _fsync_directory(config_parent)
            _fault(fault_injector, "config_published")
            os.replace(temporary_state, final_state)
            _fsync_directory(state_parent)
            _fault(fault_injector, "state_published")
            # Validate through final paths before exposing the generation.
            with _open_exact(paths, generation_id) as opened:
                if opened.activation_state is not ActivationState.CUTOVER_STAGED:
                    raise GenerationError(
                        "generation_state_invalid",
                        "imported generation is not staged",
                    )
            _switch_pointer(paths, generation_id)
            _fault(fault_injector, "pointer_switched")
            return status(paths)
        finally:
            for temporary in (temporary_config, temporary_state):
                if temporary.exists():
                    shutil.rmtree(temporary)


def status(paths: GenerationPaths) -> GenerationStatus:
    generation_id = resolve_current(paths)
    manifest = _read_manifest(paths, generation_id)
    cutover = manifest["manifestVersion"] == CUTOVER_MANIFEST_VERSION
    if cutover:
        bundle_path = paths.state_generation(generation_id) / "cutover-bundle.json"
        try:
            bundle = CutoverBundle.from_json(bundle_path.read_bytes())
        except (OSError, CutoverError) as error:
            raise GenerationError("generation_bundle_invalid", str(error)) from error
        if manifest["bundleHash"] != bundle.bundle_hash:
            raise GenerationError(
                "generation_manifest_invalid", "manifest bundle hash does not match"
            )
        source_sha256 = bundle.bundle_hash
    else:
        config_path = paths.config_generation(generation_id) / "config.toml"
        try:
            source_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest()
        except OSError as error:
            raise GenerationError("generation_config_invalid", str(error)) from error
        if manifest["configSha256"] != source_sha256:
            raise GenerationError(
                "generation_manifest_invalid", "manifest config hash does not match"
            )
    previous_raw = manifest["previousGenerationId"]
    previous = None if previous_raw is None else GenerationId(previous_raw)
    evidence_path = _evidence_path(paths, generation_id)
    evidence_sha256: str | None = None
    if evidence_path.exists():
        if not cutover:
            raise GenerationError(
                "generation_evidence_unexpected",
                "fresh generation cannot contain cutover evidence",
            )
        _validate_regular_private_file(evidence_path)
        try:
            evidence = CutoverEvidence.from_json(evidence_path.read_bytes())
        except OSError as error:
            raise GenerationError("cutover_evidence_invalid", str(error)) from error
        if not evidence.includes_generation(generation_id):
            raise GenerationError(
                "cutover_evidence_invalid",
                "stored evidence does not include the active generation",
            )
        evidence_sha256 = evidence.sha256
    with open_generation(paths, generation_id) as opened:
        metadata = opened.registry.metadata()
        committed_at = metadata["committed_at"]
        if (
            cutover
            and opened.activation_state is ActivationState.COMMITTED
            and evidence_sha256 is None
        ):
            raise GenerationError(
                "cutover_evidence_missing",
                "committed generation has no exact cutover evidence",
            )
        return GenerationStatus(
            generation_id,
            opened.activation_state,
            previous,
            "cutover" if cutover else "fresh",
            source_sha256,
            int(manifest["createdAt"]),
            None if committed_at is None else int(committed_at),
            evidence_sha256,
        )


def commit(
    paths: GenerationPaths,
    evidence: CutoverEvidence,
    *,
    committed_at: int,
) -> GenerationStatus:
    """Cross the irreversible boundary after exact paired cold-start evidence."""

    if not isinstance(evidence, CutoverEvidence):
        raise GenerationError(
            "cutover_evidence_invalid", "evidence has the wrong runtime type"
        )
    if isinstance(committed_at, bool) or committed_at < 0:
        raise GenerationError("cutover_time_invalid", "commit time is invalid")
    with _cutover_lock(paths):
        generation_id = resolve_current(paths)
        manifest = _read_manifest(paths, generation_id)
        if manifest["manifestVersion"] != CUTOVER_MANIFEST_VERSION:
            raise GenerationError(
                "cutover_not_applicable",
                "fresh generations do not cross the cutover commit boundary",
            )
        with open_generation(paths, generation_id) as opened:
            metadata = opened.registry.metadata()
            created_at = int(metadata["created_at"])
            if not evidence.includes_generation(generation_id):
                raise GenerationError(
                    "cutover_evidence_invalid",
                    "evidence does not include the active generation",
                )
            if evidence.captured_at < created_at or committed_at < evidence.captured_at:
                raise GenerationError(
                    "cutover_time_invalid",
                    "evidence or commit precedes generation creation",
                )
            evidence_path = _evidence_path(paths, generation_id)
            payload = evidence.to_json()
            try:
                existing = evidence_path.read_bytes()
            except FileNotFoundError:
                _write_file(evidence_path, payload, mode=0o400)
                _fsync_directory(evidence_path.parent)
            except OSError as error:
                raise GenerationError("cutover_evidence_invalid", str(error)) from error
            else:
                if existing != payload:
                    raise GenerationError(
                        "cutover_evidence_conflict",
                        "generation is already bound to different evidence",
                    )
            if opened.activation_state is ActivationState.COMMITTED:
                return status(paths)
            with opened.registry.transaction(immediate=True) as connection:
                changed = connection.execute(
                    "UPDATE registry_metadata "
                    "SET activation_state = 'committed', committed_at = ? "
                    "WHERE singleton = 1 AND activation_state = 'cutover_staged'",
                    (committed_at,),
                ).rowcount
                if changed != 1:
                    raise GenerationError(
                        "cutover_state_conflict", "generation is no longer staged"
                    )
        _checkpoint_database(paths.state_generation(generation_id) / "switchboard.db")
        return status(paths)


def rollback(paths: GenerationPaths) -> GenerationId | None:
    """Restore the previous pointer only while the current generation is staged."""

    with _cutover_lock(paths):
        current = resolve_current(paths)
        manifest = _read_manifest(paths, current)
        with open_generation(paths, current) as opened:
            if opened.activation_state is ActivationState.COMMITTED:
                raise GenerationError(
                    "cutover_committed", "committed generations cannot auto-rollback"
                )
        previous_raw = manifest["previousGenerationId"]
        previous = None if previous_raw is None else GenerationId(previous_raw)
        if previous is not None and (
            not paths.config_generation(previous).is_dir()
            or not paths.state_generation(previous).is_dir()
        ):
            raise GenerationError(
                "rollback_target_missing", "previous generation is incomplete"
            )
        _switch_pointer(paths, previous)
        return previous


def recover_incomplete(paths: GenerationPaths) -> tuple[str, ...]:
    """Remove inactive staging or staged generations left before publication."""

    removed: list[str] = []
    with _cutover_lock(paths):
        current = _read_pointer(paths, required=False)
        for parent in (
            paths.config_root / "generations",
            paths.state_root / "generations",
        ):
            if not parent.is_dir():
                continue
            for candidate in sorted(parent.iterdir(), key=lambda value: value.name):
                if not candidate.name.startswith(".staging-"):
                    continue
                shutil.rmtree(candidate)
                removed.append(str(candidate))
            _fsync_directory(parent)
        # A published one-sided generation is inactive and can never be opened.
        config_ids = {
            item.name
            for item in (paths.config_root / "generations").glob("*")
            if item.is_dir() and not item.name.startswith(".")
        }
        state_ids = {
            item.name
            for item in (paths.state_root / "generations").glob("*")
            if item.is_dir() and not item.name.startswith(".")
        }
        mismatched = sorted(config_ids ^ state_ids)
        if current is not None and str(current) in mismatched:
            raise GenerationError(
                "active_generation_torn", "current generation is one-sided"
            )
        for raw_id in mismatched:
            for candidate in (
                paths.config_root / "generations" / raw_id,
                paths.state_root / "generations" / raw_id,
            ):
                if candidate.exists():
                    shutil.rmtree(candidate)
                    removed.append(str(candidate))
        # A complete but inactive staged pair is a pre-pointer crash.  It has no
        # mutation authority and is safe to discard so import can be retried.
        for raw_id in sorted(config_ids & state_ids):
            if current is not None and raw_id == str(current):
                continue
            try:
                generation_id = GenerationId(raw_id)
                with _open_exact(paths, generation_id) as opened:
                    removable = (
                        opened.activation_state is ActivationState.CUTOVER_STAGED
                    )
                if not removable:
                    manifest = _read_manifest(paths, generation_id)
                    previous = manifest["previousGenerationId"]
                    removable = manifest[
                        "manifestVersion"
                    ] == FRESH_MANIFEST_VERSION and previous == (
                        None if current is None else str(current)
                    )
            except Exception:
                removable = True
            if removable:
                for candidate in (
                    paths.config_root / "generations" / raw_id,
                    paths.state_root / "generations" / raw_id,
                ):
                    shutil.rmtree(candidate)
                    removed.append(str(candidate))
        for parent in (
            paths.config_root / "generations",
            paths.state_root / "generations",
        ):
            if parent.is_dir():
                _fsync_directory(parent)
        return tuple(removed)


def bundle_file_hash(paths: GenerationPaths, generation_id: GenerationId) -> str:
    path = paths.state_generation(generation_id) / "cutover-bundle.json"
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "CORE_TARGET_VERSION",
    "DMS_TARGET_VERSION",
    "EVIDENCE_VERSION",
    "CutoverEvidence",
    "GenerationError",
    "GenerationPaths",
    "GenerationStatus",
    "OpenGeneration",
    "bundle_file_hash",
    "commit",
    "import_bundle",
    "initialize",
    "open_generation",
    "recover_incomplete",
    "reset",
    "resolve_current",
    "rollback",
    "status",
]
