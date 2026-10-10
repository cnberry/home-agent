from __future__ import annotations

from pathlib import Path

from home_agent.database import Database
from home_agent.performance import Performance, _find_release_revision_file


def test_release_marker_is_found_above_installed_site_packages(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    module_file = release_root / "venv/lib/python3.14/site-packages/home_agent/performance.py"
    marker = release_root / ".release-revision"
    marker.parent.mkdir(parents=True)
    marker.write_text("installed-release-sha\n", encoding="utf-8")

    assert _find_release_revision_file(module_file) == marker


def test_performance_prefers_release_artifact_over_stale_service_environment(
    tmp_path: Path, monkeypatch
) -> None:
    release_file = tmp_path / ".release-revision"
    release_file.write_text("candidate-release-sha\n", encoding="utf-8")
    monkeypatch.setattr("home_agent.performance.RELEASE_REVISION_FILE", release_file)
    monkeypatch.setenv("HOME_AGENT_REVISION", "stale-systemd-revision")

    database = Database(tmp_path / "runtime.sqlite3")
    database.initialize()
    job = database.enqueue("telegram", "synthetic release attribution probe")
    assert job is not None

    performance = Performance(database, job)

    assert performance.data["release"] == "candidate-release-sha"
    assert performance.data["runtime_version"] == "0.4.11"
