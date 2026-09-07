from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from home_agent.backup import BackupError, perform_backup, restore_backup


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def state(directory: Path) -> None:
    directory.mkdir()
    for name in ("tasks.md", "memory.md", "heartbeat.md"):
        (directory / name).write_text(f"# {name}\n", encoding="utf-8")


def checkout(directory: Path) -> None:
    directory.mkdir()
    git(directory, "init", "-q")
    git(directory, "switch", "--orphan", "state-backup")
    git(directory, "config", "user.name", "test")
    git(directory, "config", "user.email", "test@example.com")


def test_change_only_backup_and_restore(tmp_path: Path) -> None:
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    restored = tmp_path / "restored"
    state(source)
    checkout(repo)

    first = perform_backup(source, repo, push=False)
    second = perform_backup(source, repo, push=False)
    assert first.changed and first.commit
    assert not second.changed and second.commit is None

    manifest = restore_backup(repo, restored)
    assert manifest["schema_version"] == 1
    for name in ("tasks.md", "memory.md", "heartbeat.md"):
        assert (restored / name).read_bytes() == (source / name).read_bytes()

    (source / "tasks.md").write_text("# updated\n", encoding="utf-8")
    assert perform_backup(source, repo, push=False).changed
    restore_backup(repo, restored)
    assert (restored / "tasks.md").read_text(encoding="utf-8") == "# updated\n"


def test_rejects_symlinks_unexpected_names_and_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    state(source)
    checkout(repo)
    (source / "extra.txt").write_text("no", encoding="utf-8")
    with pytest.raises(BackupError, match="unexpected"):
        perform_backup(source, repo, push=False)
    (source / "extra.txt").unlink()
    (source / "tasks.md").unlink()
    (source / "tasks.md").symlink_to(source / "memory.md")
    with pytest.raises(BackupError, match="non-symlink"):
        perform_backup(source, repo, push=False)

    (source / "tasks.md").unlink()
    (source / "tasks.md").write_text("# tasks\n", encoding="utf-8")
    perform_backup(source, repo, push=False)
    manifest_path = repo / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["tasks.md"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BackupError, match="mismatch"):
        restore_backup(repo, tmp_path / "restored")


@pytest.mark.parametrize("content", [b"[]", b"null", b"\xff"])
def test_invalid_manifest_does_not_change_existing_state(tmp_path: Path, content: bytes) -> None:
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    restored = tmp_path / "restored"
    state(source)
    checkout(repo)
    state(restored)
    (restored / "tasks.md").write_text("# keep local work\n", encoding="utf-8")
    perform_backup(source, repo, push=False)
    (repo / "backup-manifest.json").write_bytes(content)

    with pytest.raises(BackupError):
        restore_backup(repo, restored)
    assert (restored / "tasks.md").read_text(encoding="utf-8") == "# keep local work\n"


def test_unexpected_checkout_state_cannot_be_committed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    state(source)
    checkout(repo)
    perform_backup(source, repo, push=False)
    (repo / "state" / "credentials.txt").write_text("private data", encoding="utf-8")
    (source / "tasks.md").write_text("# changed\n", encoding="utf-8")

    with pytest.raises(BackupError, match="unexpected"):
        perform_backup(source, repo, push=False)


def test_unchanged_run_recovers_a_failed_commit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    state(source)
    checkout(repo)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o700)
    git(repo, "config", "core.hooksPath", str(hooks))

    with pytest.raises(BackupError, match="git commit"):
        perform_backup(source, repo, push=False)
    hook.unlink()
    assert perform_backup(source, repo, push=False).changed
    restored = tmp_path / "restored"
    restore_backup(repo, restored)
    assert (restored / "tasks.md").read_bytes() == (source / "tasks.md").read_bytes()
    assert not perform_backup(source, repo, push=False).changed


def test_unchanged_run_retries_an_unpushed_commit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    state(source)
    checkout(repo)
    remote.mkdir()
    git(remote, "init", "--bare", "-q")
    git(repo, "remote", "add", "origin", str(remote))
    perform_backup(source, repo, push=True)

    (source / "tasks.md").write_text("# changed\n", encoding="utf-8")
    git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    with pytest.raises(BackupError, match="git push"):
        perform_backup(source, repo, push=True)

    git(repo, "remote", "set-url", "origin", str(remote))
    result = perform_backup(source, repo, push=True)
    assert not result.changed
    fetched = tmp_path / "fetched"
    git(tmp_path, "clone", "--branch", "state-backup", str(remote), str(fetched))
    restored = tmp_path / "restored"
    restore_backup(fetched, restored)
    assert (restored / "tasks.md").read_text(encoding="utf-8") == "# changed\n"
