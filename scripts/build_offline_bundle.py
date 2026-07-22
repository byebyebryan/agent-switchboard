#!/usr/bin/env python3
"""Build a pinned wheelhouse with a canonical SHA-256 manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("core_wheel", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--core-commit", required=True)
    arguments = parser.parse_args()
    if arguments.destination.exists():
        raise SystemExit("destination already exists")
    arguments.destination.mkdir(mode=0o700, parents=True)
    subprocess.run(
        (
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            str(arguments.destination),
            "--requirement",
            str(ROOT / "requirements-offline.txt"),
        ),
        check=True,
    )
    shutil.copy2(
        arguments.core_wheel,
        arguments.destination / arguments.core_wheel.name,
    )
    wheels = sorted(arguments.destination.glob("*.whl"))
    manifest = {
        "bundleVersion": 1,
        "coreCommit": arguments.core_commit,
        "files": [
            {
                "name": path.name,
                "sha256": digest(path),
                "size": path.stat().st_size,
            }
            for path in wheels
        ],
    }
    (arguments.destination / "wheelhouse-manifest.json").write_text(
        json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
