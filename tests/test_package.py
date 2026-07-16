from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from agent_switchboard import __version__
from agent_switchboard.cli import main

ROOT = Path(__file__).parents[1]


def test_static_pep_621_metadata_and_stdlib_runtime() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    assert project["name"] == "agent-switchboard"
    assert project["version"] == __version__
    assert project["readme"] == "README.md"
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["requires-python"] == ">=3.12"
    assert project["dependencies"] == []
    assert project["scripts"]["swbctl"] == "agent_switchboard.cli:main"
    assert "dynamic" not in project
    assert metadata["build-system"] == {
        "requires": ["hatchling==1.31.0"],
        "build-backend": "hatchling.build",
    }
    build = metadata["tool"]["hatch"]["build"]
    assert build["reproducible"] is True
    assert build["targets"]["wheel"]["packages"] == ["src/agent_switchboard"]
    assert build["targets"]["sdist"]["only-include"] == [
        "src/agent_switchboard",
        "docs/design.md",
        "docs/phase-1-validation.md",
        "docs/phase-2-validation.md",
    ]
    assert metadata["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src"]


def test_readme_states_phase_and_license() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Phase 1 core plus the local Codex portion" in readme
    assert "Phase 2" in readme
    assert "docs/phase-2-validation.md" in readme
    assert "MIT License" in readme
    assert "SOURCE_DATE_EPOCH=1784073600" in readme


def test_ci_smokes_wheel_and_source_distribution_installations() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    verifier = (ROOT / "scripts/verify_distributions.py").read_text(encoding="utf-8")
    assert "/tmp/switchboard-wheel-smoke/bin/python" in workflow
    assert "/tmp/switchboard-sdist-smoke/bin/python" in workflow
    assert "--no-deps /tmp/switchboard-build-a/*.tar.gz" in workflow
    assert "/tmp/switchboard-sdist-smoke" in workflow
    for module in (
        "agent_switchboard.cli",
        "agent_switchboard.doctor",
        "agent_switchboard.hook_config",
        "agent_switchboard.hooks",
        "agent_switchboard.local",
        "agent_switchboard.local_events",
        "agent_switchboard.live",
        "agent_switchboard.paths",
        "agent_switchboard.providers.codex",
        "agent_switchboard.reconcile",
        "agent_switchboard.snapshot",
    ):
        assert f"import {module}" in workflow
    assert "migrations/v0003_name_provenance_runtime_index.py" in workflow
    assert "migrations/v0004_runtime_truth_ordering.py" in workflow
    assert 'snapshot --help | grep -F -- "--reconcile {none,live,full}"' in workflow
    assert 'list --help | grep -F -- "--refresh"' in workflow
    assert 'hooks install --help | grep -F -- "--dry-run"' in workflow
    assert 'hooks uninstall --help | grep -F -- "--dry-run"' in workflow
    assert 'doctor --help | grep -F "usage: swbctl doctor"' in workflow
    assert 'snapshot --json > "$smoke_root/snapshot.json"' in workflow
    assert 'list --json > "$smoke_root/list.json"' in workflow
    assert "Path(sys.argv[1]).read_bytes()" in workflow
    assert "SnapshotEnvelope.from_json" in workflow
    assert '"docs/phase-2-validation.md"' in verifier
    assert '"agent_switchboard/migrations/v0003_name_provenance_runtime_index.py"' in (
        verifier
    )
    assert '"agent_switchboard/migrations/v0004_runtime_truth_ordering.py"' in verifier


def test_cli_help_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    assert (
        "Inspect and route provider-native coding-agent sessions."
        in capsys.readouterr().out
    )

    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"swbctl {__version__}"
