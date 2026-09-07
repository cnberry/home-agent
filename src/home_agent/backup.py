from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

ALLOWED_STATE_FILES = ("tasks.md", "memory.md", "heartbeat.md")
MAX_FILE_SIZE = 256 * 1024
MAX_TOTAL_SIZE = 1024 * 1024
SCHEMA_VERSION = 1


class BackupError(RuntimeError):
    """Durable state failed validation or could not be backed up."""


@dataclass(frozen=True)
class BackupResult:
    changed: bool
    commit: str | None


def _regular_file(path: Path) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise BackupError(f"cannot inspect {path}: {exc}") from exc
    if not stat.S_ISREG(value.st_mode) or path.is_symlink():
        raise BackupError(f"state path must be a regular non-symlink file: {path}")
    if value.st_size > MAX_FILE_SIZE:
        raise BackupError(f"state file exceeds {MAX_FILE_SIZE} bytes: {path}")
    return value


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            value.update(chunk)
    return value.hexdigest()


def _validate_file_set(directory: Path) -> None:
    try:
        names = {path.name for path in directory.iterdir()}
    except OSError as exc:
        raise BackupError(f"cannot inspect state directory {directory}: {exc}") from exc
    unexpected = names - set(ALLOWED_STATE_FILES)
    missing = set(ALLOWED_STATE_FILES) - names
    if unexpected:
        raise BackupError(f"unexpected durable state names: {', '.join(sorted(unexpected))}")
    if missing:
        raise BackupError(f"missing durable state files: {', '.join(sorted(missing))}")


def _validate_checkout_root(checkout: Path) -> None:
    allowed = {".git", "state", "backup-manifest.json"}
    try:
        unexpected = {path.name for path in checkout.iterdir()} - allowed
    except OSError as exc:
        raise BackupError(f"cannot inspect backup checkout {checkout}: {exc}") from exc
    if unexpected:
        raise BackupError(f"unexpected backup branch names: {', '.join(sorted(unexpected))}")


def build_manifest(source: Path) -> dict[str, Any]:
    _validate_file_set(source)
    files: dict[str, dict[str, Any]] = {}
    total = 0
    for name in ALLOWED_STATE_FILES:
        path = source / name
        value = _regular_file(path)
        total += value.st_size
        files[name] = {"sha256": _digest(path), "size": value.st_size}
    if total > MAX_TOTAL_SIZE:
        raise BackupError(f"durable state exceeds {MAX_TOTAL_SIZE} bytes")
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": files,
    }


def _run_git(checkout: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(checkout), *args],
            check=check,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise BackupError(f"git {' '.join(args)} failed: {detail}") from exc


def _write_atomic(path: Path, content: bytes, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _push_if_ahead(checkout: Path) -> None:
    upstream = _run_git(
        checkout,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
        check=False,
    )
    if upstream.returncode != 0:
        _run_git(checkout, "push", "--set-upstream", "origin", "state-backup")
        return
    ahead = _run_git(checkout, "rev-list", "--count", "@{upstream}..HEAD").stdout.strip()
    if ahead != "0":
        _run_git(checkout, "push", "origin", "state-backup")


def perform_backup(source: Path, checkout: Path, *, push: bool = True) -> BackupResult:
    if not (checkout / ".git").exists():
        raise BackupError(f"backup checkout is not initialized: {checkout}")
    branch = _run_git(checkout, "branch", "--show-current").stdout.strip()
    if branch != "state-backup":
        raise BackupError(f"backup checkout must be on state-backup, found {branch!r}")
    _validate_checkout_root(checkout)

    manifest = build_manifest(source)
    if (checkout / "backup-manifest.json").exists():
        try:
            current = validate_manifest(checkout)
        except BackupError:
            pass
        else:
            if current.get("files") == manifest["files"]:
                if push:
                    _push_if_ahead(checkout)
                return BackupResult(changed=False, commit=None)
    destination = checkout / "state"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ALLOWED_STATE_FILES:
        _write_atomic(destination / name, (source / name).read_bytes())
    _write_atomic(
        checkout / "backup-manifest.json",
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
    )

    _run_git(checkout, "add", "--", "state", "backup-manifest.json")
    status = _run_git(checkout, "status", "--porcelain", "--", "state", "backup-manifest.json")
    if not status.stdout.strip():
        return BackupResult(changed=False, commit=None)
    _run_git(checkout, "commit", "-m", f"Backup durable state {manifest['created_at']}")
    commit = _run_git(checkout, "rev-parse", "HEAD").stdout.strip()
    if push:
        _push_if_ahead(checkout)
    return BackupResult(changed=True, commit=commit)


def validate_manifest(checkout: Path) -> dict[str, Any]:
    _validate_checkout_root(checkout)
    manifest_path = checkout / "backup-manifest.json"
    _regular_file(manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"invalid backup manifest: {exc}") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BackupError("unsupported backup manifest schema")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(ALLOWED_STATE_FILES):
        raise BackupError("backup manifest contains an unexpected file set")
    _validate_file_set(checkout / "state")
    total = 0
    for name in ALLOWED_STATE_FILES:
        path = checkout / "state" / name
        value = _regular_file(path)
        total += value.st_size
        expected = files[name]
        if not isinstance(expected, dict):
            raise BackupError(f"invalid manifest entry for {name}")
        if value.st_size != expected.get("size") or _digest(path) != expected.get("sha256"):
            raise BackupError(f"backup hash or size mismatch for {name}")
    if total > MAX_TOTAL_SIZE:
        raise BackupError(f"backup exceeds {MAX_TOTAL_SIZE} bytes")
    return cast(dict[str, Any], manifest)


def restore_backup(checkout: Path, destination: Path) -> dict[str, Any]:
    manifest = validate_manifest(checkout)
    destination.mkdir(parents=True, exist_ok=True)
    for name in ALLOWED_STATE_FILES:
        source = checkout / "state" / name
        _write_atomic(destination / name, source.read_bytes())
    return manifest


def copy_default_state(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in ALLOWED_STATE_FILES:
        target = destination / name
        if not target.exists():
            shutil.copyfile(source / name, target)
            os.chmod(target, 0o640)
