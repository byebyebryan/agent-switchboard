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
    assert project["requires-python"] == ">=3.12"
    assert project["dependencies"] == []
    assert project["scripts"]["agentctl"] == "agent_switchboard.cli:main"
    assert "dynamic" not in project
    assert "license" not in project
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
    ]
    assert metadata["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src"]


def test_readme_states_phase_and_license_boundaries() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Phase 1 core implementation" in readme
    assert "not implemented yet" in readme
    assert "No license has been selected" in readme
    assert "SOURCE_DATE_EPOCH=1784073600" in readme


def test_ci_smokes_wheel_and_source_distribution_installations() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "/tmp/switchboard-wheel-smoke/bin/python" in workflow
    assert "/tmp/switchboard-sdist-smoke/bin/python" in workflow
    assert "--no-deps /tmp/switchboard-build-a/*.tar.gz" in workflow
    assert "/tmp/switchboard-sdist-smoke/bin/agentctl" in workflow


def test_provisional_cli_help_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"agentctl {__version__}"
