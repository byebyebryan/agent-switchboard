from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_switchboard.catalog import CatalogEditor, CatalogError
from agent_switchboard.catalog_management import CatalogManager
from agent_switchboard.domain import HostId
from agent_switchboard.storage import Registry

HOST_ID = HostId("11111111-1111-4111-8111-111111111111")
OTHER_HOST_ID = HostId("22222222-2222-4222-8222-222222222222")


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _git_repository(tmp_path: Path, name: str = "repository") -> tuple[Path, Path]:
    repository = tmp_path / name
    repository.mkdir()
    _git("init", "-b", "main", cwd=repository)
    _git("config", "user.email", "switchboard@example.invalid", cwd=repository)
    _git("config", "user.name", "Switchboard Test", cwd=repository)
    (repository / "README.md").write_text("test\n", encoding="utf-8")
    _git("add", "README.md", cwd=repository)
    _git("commit", "-m", "initial", cwd=repository)
    worktree = tmp_path / f"{name}-feature"
    _git("worktree", "add", "-b", "feature", str(worktree), cwd=repository)
    return repository, worktree


def _manager(
    root: Path, *, host_id: HostId = HOST_ID
) -> tuple[CatalogManager, Registry, Path]:
    config_path = root / "config" / "config.toml"
    registry = Registry(root / "state" / "switchboard.db")
    registry.upsert_host(str(host_id), "test-host", is_local=True, observed_at=1)
    editor = CatalogEditor(
        host_id=host_id,
        path=config_path,
        backup_root=root / "state" / "backups",
    )
    manager = CatalogManager(
        host_id=host_id,
        editor=editor,
        registry=registry,
        archive_root=root / "state" / "archives",
        now_ms=lambda: 123,
    )
    return manager, registry, config_path


def _project(document: dict[str, object], project_id: str) -> dict[str, object]:
    projects = document["projects"]
    assert isinstance(projects, list)
    return next(item for item in projects if item["projectId"] == project_id)


def test_directory_project_metadata_and_multi_repository_management(
    tmp_path: Path,
) -> None:
    manager, registry, config_path = _manager(tmp_path)
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    try:
        added = manager.add_project(primary, name="Atlas", kind="directory")
        project_id = added.identities["projectId"]
        primary_repository_id = added.identities["repositoryId"]
        assert config_path.exists()
        assert added.edit.backup_path is not None

        manager.update_project(
            project_id,
            name="Atlas Core",
            aliases=("atlas", "core"),
            default_provider="claude",
            default_transport="tmux",
        )
        linked = manager.add_repository(
            project_id, secondary, name="Notes", kind="directory"
        )
        secondary_repository_id = linked.identities["repositoryId"]
        manager.update_repository(
            secondary_repository_id,
            name="Atlas Notes",
            context_sources=("AGENTS.md", "docs"),
        )
        manager.set_primary_repository(project_id, secondary_repository_id)

        document = manager.document(include_archived=False)
        project = _project(document, project_id)
        assert project["name"] == "Atlas Core"
        assert project["aliases"] == ["atlas", "core"]
        assert project["defaultProvider"] == "claude"
        repositories = project["repositories"]
        assert isinstance(repositories, list)
        assert {item["repositoryId"] for item in repositories} == {
            primary_repository_id,
            secondary_repository_id,
        }
        secondary_record = next(
            item
            for item in repositories
            if item["repositoryId"] == secondary_repository_id
        )
        assert secondary_record["isPrimary"] is True
        assert secondary_record["contextSources"] == ["AGENTS.md", "docs"]
    finally:
        registry.close()


def test_git_worktree_add_and_checkout_path_identity_guard(tmp_path: Path) -> None:
    repository, worktree = _git_repository(tmp_path)
    unrelated, _ = _git_repository(tmp_path, "unrelated")
    manager, registry, _config_path = _manager(tmp_path / "private")
    try:
        added = manager.add_project(repository)
        repository_id = added.identities["repositoryId"]
        worktree_added = manager.add_checkout(
            repository_id,
            worktree,
            display_name="Feature",
            provider_override="claude",
            transport_override="tmux",
            is_default=True,
        )
        checkout_id = worktree_added.identities["checkoutId"]
        assert manager.inspect(worktree).checkout_id == checkout_id

        with pytest.raises(CatalogError) as failure:
            manager.update_checkout(checkout_id, path=unrelated)
        assert failure.value.code == "checkout_repository_mismatch"
    finally:
        registry.close()


def test_checkout_archive_restore_preserves_exact_metadata(tmp_path: Path) -> None:
    manager, registry, _config_path = _manager(tmp_path / "private")
    directory = tmp_path / "checkout"
    directory.mkdir()
    try:
        added = manager.add_project(directory, kind="directory")
        checkout_id = added.identities["checkoutId"]
        manager.update_checkout(
            checkout_id,
            display_name="Primary Checkout",
            provider_override="claude",
            transport_override="tmux",
            is_default=True,
        )
        manager.archive_checkout(checkout_id, confirmed=True)
        archived = manager.document(include_archived=True)
        project = _project(archived, added.identities["projectId"])
        assert project["repositories"][0]["checkouts"][0]["declared"] is False

        restored = manager.restore_checkout(checkout_id)
        assert restored.identities["checkoutId"] == checkout_id
        checkout = _project(
            manager.document(include_archived=False), added.identities["projectId"]
        )["repositories"][0]["checkouts"][0]
        assert checkout["checkoutId"] == checkout_id
        assert checkout["displayName"] == "Primary Checkout"
        assert checkout["providerOverride"] == "claude"
        assert checkout["transportOverride"] == "tmux"
        assert checkout["isDefault"] is True
    finally:
        registry.close()


def test_project_archive_restore_preserves_catalog_exactly(tmp_path: Path) -> None:
    manager, registry, _config_path = _manager(tmp_path / "private")
    directory = tmp_path / "checkout"
    directory.mkdir()
    try:
        added = manager.add_project(directory, name="Archive Me", kind="directory")
        project_id = added.identities["projectId"]
        before = _project(manager.document(include_archived=False), project_id)

        preview = manager.archive_project(project_id, confirmed=True, dry_run=True)
        assert preview.edit.changed and preview.edit.dry_run
        assert not (tmp_path / "private" / "state" / "archives").exists()

        manager.archive_project(project_id, confirmed=True)
        assert manager.document(include_archived=False)["projects"] == []
        archived = _project(manager.document(include_archived=True), project_id)
        assert archived["declared"] is False

        manager.restore_project(project_id)
        after = _project(manager.document(include_archived=False), project_id)
        # Observation fields may change; declarations and stable identities do not.
        assert after["projectId"] == before["projectId"]
        assert after["name"] == before["name"]
        assert after["aliases"] == before["aliases"]
        assert (
            after["repositories"][0]["repositoryId"]
            == (before["repositories"][0]["repositoryId"])
        )
        assert (
            after["repositories"][0]["checkouts"][0]["checkoutId"]
            == (before["repositories"][0]["checkouts"][0]["checkoutId"])
        )
        assert after["repositories"][0]["checkouts"][0]["isDefault"] is True
    finally:
        registry.close()


def test_active_archive_and_historical_membership_guards(tmp_path: Path) -> None:
    manager, registry, _config_path = _manager(tmp_path / "private")
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    try:
        added = manager.add_project(primary, kind="directory")
        linked = manager.add_repository(
            added.identities["projectId"], secondary, kind="directory"
        )
        task_id = "33333333-3333-4333-8333-333333333333"
        registry.create_task(
            task_id=task_id,
            host_id=str(HOST_ID),
            project_id=added.identities["projectId"],
            checkout_id=linked.identities["checkoutId"],
            title="Retained task",
            observed_at=10,
        )
        with pytest.raises(CatalogError) as failure:
            manager.archive_project(added.identities["projectId"], confirmed=True)
        assert failure.value.code == "project_active"

        registry.close_task(task_id, host_id=str(HOST_ID), observed_at=11)
        with pytest.raises(CatalogError) as failure:
            manager.unlink_repository(
                added.identities["projectId"],
                linked.identities["repositoryId"],
                confirmed=True,
            )
        assert failure.value.code == "repository_referenced"
    finally:
        registry.close()


def test_export_import_preserves_global_ids_and_creates_local_checkout(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source-checkout"
    destination_path = tmp_path / "destination-checkout"
    source_path.mkdir()
    destination_path.mkdir()
    source, source_registry, _ = _manager(tmp_path / "source")
    destination, destination_registry, _ = _manager(
        tmp_path / "destination", host_id=OTHER_HOST_ID
    )
    try:
        added = source.add_project(source_path, name="Portable", kind="directory")
        exported = source.export_project(added.identities["projectId"])
        imported = destination.import_project(
            exported,
            checkout_paths={added.identities["repositoryId"]: destination_path},
        )
        assert imported.identities["projectId"] == added.identities["projectId"]
        project = _project(
            destination.document(include_archived=False),
            added.identities["projectId"],
        )
        assert (
            project["repositories"][0]["repositoryId"]
            == (added.identities["repositoryId"])
        )
        imported_checkout_id = project["repositories"][0]["checkouts"][0]["checkoutId"]
        assert imported_checkout_id != added.identities["checkoutId"]
    finally:
        source_registry.close()
        destination_registry.close()
