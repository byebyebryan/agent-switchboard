#!/usr/bin/env python3
"""Compare and audit two Agent Switchboard PEP 517 build directories."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS = {
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "fixture",
    "fixtures",
    "test",
    "tests",
}
FORBIDDEN_SUFFIXES = {".db", ".pyc", ".pyo", ".sqlite", ".sqlite3"}


class DistributionError(RuntimeError):
    """A built distribution violates the release artifact contract."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise DistributionError(
            f"expected exactly one {pattern!r} in {directory}, got {matches}"
        )
    return matches[0]


def safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise DistributionError(f"unsafe archive member path: {name!r}")
    lowered_parts = {part.casefold() for part in path.parts}
    forbidden = lowered_parts & FORBIDDEN_PARTS
    if forbidden:
        raise DistributionError(
            f"forbidden archive path component {sorted(forbidden)!r}: {name}"
        )
    if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
        raise DistributionError(f"forbidden archive file type: {name}")
    return path


def wheel_files(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad is not None:
            raise DistributionError(f"wheel CRC check failed for {bad}")
        files: dict[str, bytes] = {}
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = safe_name(info.filename).as_posix()
            if name in files:
                raise DistributionError(f"duplicate wheel member: {name}")
            files[name] = archive.read(info)
        return files


def sdist_files(path: Path) -> tuple[str, dict[str, bytes]]:
    with tarfile.open(path, mode="r:gz") as archive:
        files: dict[str, bytes] = {}
        roots: set[str] = set()
        for member in archive.getmembers():
            name = safe_name(member.name).as_posix()
            roots.add(PurePosixPath(name).parts[0])
            if member.isdir():
                continue
            if not member.isfile():
                raise DistributionError(f"sdist contains a non-file member: {name}")
            if name in files:
                raise DistributionError(f"duplicate sdist member: {name}")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise DistributionError(f"cannot read sdist member: {name}")
            files[name] = extracted.read()
    if len(roots) != 1:
        raise DistributionError(f"sdist must have exactly one root: {sorted(roots)}")
    return roots.pop(), files


def expected_source_files() -> dict[str, bytes]:
    source_root = ROOT / "src" / "agent_switchboard"
    result: dict[str, bytes] = {}
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(ROOT / "src").as_posix()
        result[relative] = path.read_bytes()
    if not result:
        raise DistributionError("no package source files found")
    return result


def audit_contents(wheel: Path, sdist: Path) -> dict[str, object]:
    package_files = expected_source_files()
    project_files = {
        relative: (ROOT / relative).read_bytes()
        for relative in (
            ".gitignore",
            "LICENSE",
            "README.md",
            "docs/design.md",
            "docs/phase-1-validation.md",
            "docs/phase-2-validation.md",
            "docs/phase-2b-plan.md",
            "docs/phase-3a-validation.md",
            "docs/phase-3b-plan.md",
            "docs/phase-3c-plan.md",
            "docs/phase-4a-plan.md",
            "docs/phase-4b-plan.md",
            "docs/phase-4c-plan.md",
            "docs/phase-4d-plan.md",
            "pyproject.toml",
        )
    }
    wheel_content = wheel_files(wheel)
    sdist_root, sdist_content = sdist_files(sdist)
    dist_info = "agent_switchboard-0.2.0.dist-info"
    expected_wheel = set(package_files) | {
        f"{dist_info}/METADATA",
        f"{dist_info}/RECORD",
        f"{dist_info}/WHEEL",
        f"{dist_info}/entry_points.txt",
        f"{dist_info}/licenses/LICENSE",
    }
    if set(wheel_content) != expected_wheel:
        raise DistributionError(
            "wheel member mismatch: "
            f"missing={sorted(expected_wheel - set(wheel_content))}, "
            f"unexpected={sorted(set(wheel_content) - expected_wheel)}"
        )

    expected_sdist = {
        f"{sdist_root}/PKG-INFO",
        *(f"{sdist_root}/{relative}" for relative in project_files),
        *(f"{sdist_root}/src/{relative}" for relative in package_files),
    }
    if set(sdist_content) != expected_sdist:
        raise DistributionError(
            "sdist member mismatch: "
            f"missing={sorted(expected_sdist - set(sdist_content))}, "
            f"unexpected={sorted(set(sdist_content) - expected_sdist)}"
        )

    for relative, source in package_files.items():
        if wheel_content[relative] != source:
            raise DistributionError(f"wheel source differs from checkout: {relative}")
        sdist_name = f"{sdist_root}/src/{relative}"
        if sdist_content[sdist_name] != source:
            raise DistributionError(f"sdist source differs from checkout: {relative}")
    for relative, source in project_files.items():
        sdist_name = f"{sdist_root}/{relative}"
        if sdist_content[sdist_name] != source:
            raise DistributionError(
                f"sdist project file differs from checkout: {relative}"
            )
    if wheel_content[f"{dist_info}/licenses/LICENSE"] != project_files["LICENSE"]:
        raise DistributionError("wheel license differs from checkout")

    metadata = wheel_content[f"{dist_info}/METADATA"].decode("utf-8")
    for expected in (
        "Name: agent-switchboard",
        "Version: 0.2.0",
        "License-Expression: MIT",
        "License-File: LICENSE",
        "Requires-Python: >=3.12",
    ):
        if expected not in metadata:
            raise DistributionError(f"wheel metadata is missing {expected!r}")
    package_info = sdist_content[f"{sdist_root}/PKG-INFO"].decode("utf-8")
    for expected in ("License-Expression: MIT", "License-File: LICENSE"):
        if expected not in package_info:
            raise DistributionError(f"sdist metadata is missing {expected!r}")
    entry_points = wheel_content[f"{dist_info}/entry_points.txt"].decode("utf-8")
    if "swbctl = agent_switchboard.cli:main" not in entry_points:
        raise DistributionError("wheel is missing the swbctl console entry point")

    required_migrations = {
        "agent_switchboard/migrations/__init__.py",
        "agent_switchboard/migrations/v0001_initial.py",
        "agent_switchboard/migrations/v0002_remote_cache.py",
        "agent_switchboard/migrations/v0003_name_provenance_runtime_index.py",
        "agent_switchboard/migrations/v0004_runtime_truth_ordering.py",
        "agent_switchboard/migrations/v0005_history_launch.py",
        "agent_switchboard/migrations/v0006_agent_tools.py",
        "agent_switchboard/migrations/v0007_repository_checkouts.py",
        "agent_switchboard/migrations/v0008_tasks.py",
    }
    if not required_migrations <= set(wheel_content):
        raise DistributionError("wheel is missing migration modules")

    return {
        "wheelFiles": len(wheel_content),
        "sdistFiles": len(sdist_content),
        "packageFiles": len(package_files),
        "sdistRoot": sdist_root,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    args = parser.parse_args()

    first_wheel = artifact(args.first, "*.whl")
    second_wheel = artifact(args.second, "*.whl")
    first_sdist = artifact(args.first, "*.tar.gz")
    second_sdist = artifact(args.second, "*.tar.gz")
    if first_wheel.name != second_wheel.name or first_sdist.name != second_sdist.name:
        raise DistributionError("builds produced different artifact names")

    hashes = {
        "wheel": (sha256(first_wheel), sha256(second_wheel)),
        "sdist": (sha256(first_sdist), sha256(second_sdist)),
    }
    for kind, pair in hashes.items():
        if pair[0] != pair[1]:
            raise DistributionError(f"{kind} builds are not byte-identical: {pair}")

    content = audit_contents(first_wheel, first_sdist)
    audit_contents(second_wheel, second_sdist)
    print(
        json.dumps(
            {
                "passed": True,
                "wheelSha256": hashes["wheel"][0],
                "sdistSha256": hashes["sdist"][0],
                **content,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
