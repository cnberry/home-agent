from __future__ import annotations

from pathlib import Path

import pytest

from home_agent.database import Database, QueueFullError


def make_database(path: Path, max_queue: int = 20) -> Database:
    database = Database(path, max_queue)
    database.initialize()
    return database


def test_update_deduplication_and_fifo(tmp_path: Path) -> None:
    database = make_database(tmp_path / "runtime.sqlite3")
    assert database.record_update(42)
    assert not database.record_update(42)

    first = database.enqueue("telegram", "first", telegram_update_id=42)
    second = database.enqueue("telegram", "second", telegram_update_id=43)
    assert first is not None and second is not None
    database = make_database(tmp_path / "runtime.sqlite3", max_queue=2)
    assert database.enqueue("telegram", "duplicate", telegram_update_id=42) is None
    assert not database.record_update(43)
    assert database.claim_next().id == first.id  # type: ignore[union-attr]
    database.finish(first.id, "completed", response="first reply")
    assert database.get_job(first.id).response == "first reply"  # type: ignore[union-attr]
    assert database.claim_next().id == second.id  # type: ignore[union-attr]


def test_queue_limit_and_heartbeat_deduplication(tmp_path: Path) -> None:
    database = make_database(tmp_path / "runtime.sqlite3", max_queue=2)
    assert database.enqueue("heartbeat", "check", deduplicate_kind=True)
    assert database.enqueue("heartbeat", "check", deduplicate_kind=True) is None
    assert database.enqueue("telegram", "work")
    with pytest.raises(QueueFullError):
        database.enqueue("telegram", "overflow")


def test_restart_recovery_and_explicit_retry(tmp_path: Path) -> None:
    database = make_database(tmp_path / "runtime.sqlite3")
    job = database.enqueue("telegram", "potentially mutating")
    assert job is not None
    running = database.claim_next()
    assert running is not None
    database.mark_codex_started(running.id)
    waiting = database.enqueue("telegram", "still waiting")
    assert waiting is not None

    database = make_database(tmp_path / "runtime.sqlite3")
    recovered = database.recover_interrupted()
    assert [item.id for item in recovered] == [job.id]
    assert recovered[0].status == "uncertain"
    assert database.get_job(job.id).status == "uncertain"  # type: ignore[union-attr]
    assert database.claim_next().id == waiting.id  # type: ignore[union-attr]
    database.finish(waiting.id, "completed")
    assert database.claim_next() is None

    retried = database.retry(job.id)
    assert retried is not None and retried.status == "queued"
    assert retried.attempts == 0
    assert not retried.codex_started
    assert database.claim_next().attempts == 1  # type: ignore[union-attr]


def test_retry_checks_eligibility_and_capacity(tmp_path: Path) -> None:
    database = make_database(tmp_path / "runtime.sqlite3", max_queue=1)
    failed = database.enqueue("telegram", "failed")
    assert failed is not None
    database.claim_next()
    database.finish(failed.id, "failed")
    waiting = database.enqueue("telegram", "waiting")
    assert waiting is not None

    assert database.retry(waiting.id) is None
    assert database.retry(999) is None
    with pytest.raises(QueueFullError):
        database.retry(failed.id)


def test_snapshot_reports_the_latest_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = make_database(tmp_path / "runtime.sqlite3")
    first = database.enqueue("heartbeat", "first")
    second = database.enqueue("heartbeat", "second")
    assert first is not None and second is not None
    database.claim_next()
    database.finish(first.id, "failed")
    database.claim_next()
    monkeypatch.setattr("home_agent.database.utc_now", lambda: "2026-09-07T10:00:00+00:00")
    database.finish(second.id, "completed")
    database.retry(first.id)
    database.claim_next()
    monkeypatch.setattr("home_agent.database.utc_now", lambda: "2026-09-07T11:00:00+00:00")
    database.finish(first.id, "completed")

    snapshot = database.snapshot()
    assert snapshot.last_completed_at == "2026-09-07T11:00:00+00:00"
    assert snapshot.last_heartbeat_at == snapshot.last_completed_at


def test_thread_and_metadata_persistence(tmp_path: Path) -> None:
    database = make_database(tmp_path / "runtime.sqlite3")
    database.set_thread("telegram", "thread-1")
    database.set_thread("heartbeat", "heartbeat-thread")
    database.set_metadata("authentication_degraded", True)

    reopened = make_database(tmp_path / "runtime.sqlite3")
    assert reopened.get_thread("telegram") == "thread-1"
    assert reopened.get_metadata("authentication_degraded") is True
    assert reopened.clear_thread("telegram") == "thread-1"
    assert reopened.get_thread("telegram") is None
    assert reopened.get_thread("heartbeat") == "heartbeat-thread"


def test_existing_database_preserves_jobs_and_can_store_responses(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY, kind TEXT, prompt TEXT, status TEXT, attempts INTEGER,
                telegram_update_id INTEGER, telegram_chat_id INTEGER,
                telegram_message_id INTEGER, ack_message_id INTEGER, codex_started INTEGER,
                created_at TEXT, available_at TEXT, started_at TEXT, completed_at TEXT, error TEXT
            )
            """
        )
        db.execute(
            "INSERT INTO jobs(id, kind, prompt, status, attempts, codex_started, "
            "created_at, available_at) VALUES "
            "(1, 'telegram', 'existing request', 'queued', 0, 0, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
    database = make_database(path)
    claimed = database.claim_next()
    assert claimed is not None and claimed.prompt == "existing request"
    database.finish(claimed.id, "completed", response="migrated reply")
    reopened = make_database(path)
    completed = reopened.get_job(claimed.id)
    assert completed is not None and completed.status == "completed"
    assert completed.response == "migrated reply"
