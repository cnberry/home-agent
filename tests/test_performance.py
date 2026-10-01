from __future__ import annotations

from pathlib import Path

from home_agent.database import Database
from home_agent.performance import Performance


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
    assert performance.data["runtime_version"] == "0.4.7"
