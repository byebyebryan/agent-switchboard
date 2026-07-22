from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from agent_switchboard.catalog import CatalogEditor, CatalogError, inspect_path
from agent_switchboard.config import ProjectCatalog, parse_config
from agent_switchboard.domain import (
    Checkout,
    CheckoutId,
    CheckoutKind,
    HostId,
    Project,
    ProjectId,
    ProjectRepository,
    ProviderId,
    Repository,
    RepositoryId,
)

HOST_ID = HostId("11111111-1111-4111-8111-111111111111")
PROJECT_ID = ProjectId("22222222-2222-4222-8222-222222222222")
REPOSITORY_ID = RepositoryId("33333333-3333-4333-8333-333333333333")
CHECKOUT_ID = CheckoutId("44444444-4444-4444-8444-444444444444")


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", "-b", "main", cwd=repository)
    _git("config", "user.email", "switchboard@example.invalid", cwd=repository)
    _git("config", "user.name", "Switchboard Test", cwd=repository)
    (repository / "README.md").write_text("test\n", encoding="utf-8")
    _git("add", "README.md", cwd=repository)
    _git("commit", "-m", "initial", cwd=repository)
    worktree = tmp_path / "feature"
    _git("worktree", "add", "-b", "feature", str(worktree), cwd=repository)
    return repository, worktree


def _configured(repository: Path):
    base = parse_config("config_version = 2\n", host_id=HOST_ID)
    catalog = ProjectCatalog(
        (Project(PROJECT_ID, "Switchboard", default_provider=ProviderId.CODEX),),
        (Repository(REPOSITORY_ID, "Switchboard"),),
        (ProjectRepository(PROJECT_ID, REPOSITORY_ID, True),),
        (
            Checkout(
                CHECKOUT_ID,
                REPOSITORY_ID,
                HOST_ID,
                repository,
                kind=CheckoutKind.MAIN,
                is_default=True,
            ),
        ),
    )
    return replace(base, catalog=catalog)


def test_path_inspection_classifies_git_directory_and_known_worktree(
    tmp_path: Path,
) -> None:
    repository, worktree = _repository(tmp_path)
    empty = parse_config("config_version = 2\n", host_id=HOST_ID)
    nested = repository / "src"
    nested.mkdir()

    fresh = inspect_path(empty, nested)
    assert fresh.kind == "new_git_repository"
    assert fresh.path == repository.resolve()
    assert fresh.checkout_kind is CheckoutKind.MAIN

    configured = _configured(repository)
    known = inspect_path(configured, repository)
    assert known.kind == "known_checkout"
    assert known.repository_id == str(REPOSITORY_ID)
    assert known.checkout_id == str(CHECKOUT_ID)
    assert known.project_ids == (str(PROJECT_ID),)

    linked = inspect_path(configured, worktree)
    assert linked.kind == "new_checkout"
    assert linked.repository_id == str(REPOSITORY_ID)
    assert linked.checkout_kind is CheckoutKind.WORKTREE

    directory = tmp_path / "notes"
    directory.mkdir()
    plain = inspect_path(empty, directory)
    assert plain.kind == "directory"
    assert plain.checkout_kind is CheckoutKind.DIRECTORY


def test_editor_canonicalizes_with_exact_backup_and_secure_modes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "config" / "config.toml"
    source.parent.mkdir(mode=0o700)
    original = (
        b'# retained in backup\nconfig_version = 2\n\n[host]\ndisplay_name = "before"\n'
    )
    source.write_bytes(original)
    source.chmod(0o600)
    backups = tmp_path / "state" / "backups"
    editor = CatalogEditor(
        host_id=HOST_ID,
        path=source,
        backup_root=backups,
        now_ns=lambda: 123,
        token_hex=lambda _size: "token",
    )

    result = editor.apply(
        "project-add",
        lambda config: replace(
            config,
            host=replace(config.host, display_name="starship"),
        ),
    )

    assert result.changed and not result.dry_run
    assert result.backup_path == backups / f"123-{result.before_hash[:16]}.toml"
    assert result.backup_path.read_bytes() == original
    assert "# retained" not in source.read_text(encoding="utf-8")
    assert parse_config(source.read_bytes(), host_id=HOST_ID).host.display_name == (
        "starship"
    )
    assert source.stat().st_mode & 0o777 == 0o600
    assert result.backup_path.stat().st_mode & 0o777 == 0o600


def test_editor_dry_run_and_semantic_noop_do_not_write_or_backup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "config" / "config.toml"
    backups = tmp_path / "backups"
    editor = CatalogEditor(host_id=HOST_ID, path=source, backup_root=backups)

    dry_run = editor.apply(
        "project-add",
        lambda config: replace(
            config,
            host=replace(config.host, display_name="preview"),
        ),
        dry_run=True,
    )
    assert dry_run.changed and dry_run.dry_run
    assert not source.exists()
    assert not backups.exists()

    noop = editor.apply("project-list", lambda config: config)
    assert not noop.changed
    assert not source.exists()
    assert not backups.exists()


def test_editor_rejects_symlinks_and_concurrent_source_changes(tmp_path: Path) -> None:
    directory = tmp_path / "config"
    directory.mkdir(mode=0o700)
    target = directory / "target.toml"
    target.write_text("config_version = 2\n", encoding="utf-8")
    target.chmod(0o600)
    source = directory / "config.toml"
    source.symlink_to(target)
    editor = CatalogEditor(host_id=HOST_ID, path=source, backup_root=tmp_path / "b")
    with pytest.raises(CatalogError) as failure:
        editor.apply("project-add", lambda config: config)
    assert failure.value.code == "catalog_config_unsafe"

    source.unlink()
    source.write_text("config_version = 2\n", encoding="utf-8")
    source.chmod(0o600)

    def concurrent(config):
        source.write_text(
            "config_version = 2\n[host]\ndisplay_name = 'foreign'\n",
            encoding="utf-8",
        )
        os.chmod(source, 0o600)
        return replace(config, host=replace(config.host, display_name="candidate"))

    with pytest.raises(CatalogError) as failure:
        editor.apply("project-add", concurrent)
    assert failure.value.code == "catalog_config_changed"
    assert "foreign" in source.read_text(encoding="utf-8")
