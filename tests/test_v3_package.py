from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from agent_switchboard._v3 import __version__
from agent_switchboard._v3.cli import main

ROOT = Path(__file__).parents[1]


def test_clean_break_metadata_maps_only_replacement_runtime() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = metadata["project"]
    assert project["version"] == __version__ == "0.3.0"
    assert project["dependencies"] == ["textual>=8.2.8,<9"]
    assert "optional-dependencies" not in project
    assert project["scripts"] == {"swbctl": "agent_switchboard.cli:main"}
    wheel = metadata["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/agent_switchboard/_v3"]
    assert wheel["sources"] == {"src/agent_switchboard/_v3": "agent_switchboard"}
    sdist = metadata["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert "src/agent_switchboard/_v3" in sdist["only-include"]
    assert "src/agent_switchboard" not in sdist["only-include"]
    assert "docs/operations.md" in sdist["only-include"]
    assert "docs/phase-6e1-acceptance.md" in sdist["only-include"]
    assert "docs/usage-tracking-discovery.md" in sdist["only-include"]
    assert "docs/phase-6e-activation.md" not in sdist["only-include"]
    assert "scripts/phase6e_cutover.py" not in sdist["only-include"]
    requirements = (ROOT / "requirements-offline.txt").read_text().splitlines()
    assert requirements[0] == "textual==8.2.8"
    assert all("==" in line for line in requirements)
    builder = (ROOT / "scripts" / "build_offline_bundle.py").read_text()
    assert "wheelhouse-manifest.json" in builder
    assert '"sha256": digest(path)' in builder
    assert "venv.EnvBuilder(with_pip=True)" in builder


def test_replacement_has_no_runtime_import_of_old_active_modules() -> None:
    replacement = ROOT / "src" / "agent_switchboard" / "_v3"
    for source in replacement.rglob("*.py"):
        text = source.read_text()
        assert '"agent_switchboard._v3' not in text
        assert "from agent_switchboard.providers" not in text
        assert "from agent_switchboard.hooks" not in text
        assert "from agent_switchboard.state" not in text
        assert "from ..config" not in text
        assert "from ..storage" not in text


def test_public_help_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out.strip() == "swbctl 0.3.0"
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert "persistent project and task views" in output
    assert "init" in output
    assert "reset" in output
    assert "snapshot" not in output
    assert (
        "task"
        not in output.split("positional arguments:", 1)[-1]
        .split("options:", 1)[0]
        .split()
    )


def test_ci_executes_installed_navigator_module() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert '"$python" -m agent_switchboard.navigator --help' in workflow
    assert "Smoke fresh generation lifecycle" in workflow
