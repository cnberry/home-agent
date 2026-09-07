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
    assert (restored / "tasks.md").read_text(encoding="utf-8") == "# tasks.md\n"


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
    local_head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    remote_head = subprocess.check_output(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/state-backup"],
        text=True,
    ).strip()
    assert remote_head == local_head
