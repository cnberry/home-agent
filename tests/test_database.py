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

    recovered = database.recover_interrupted()
    assert [item.id for item in recovered] == [job.id]
    assert database.get_job(job.id).status == "uncertain"  # type: ignore[union-attr]

    retried = database.retry(job.id)
    assert retried is not None and retried.status == "queued"


def test_thread_and_metadata_persistence(tmp_path: Path) -> None:
    database = make_database(tmp_path / "runtime.sqlite3")
    database.set_thread("telegram", "thread-1")
    database.set_metadata("authentication_degraded", True)

    reopened = make_database(tmp_path / "runtime.sqlite3")
    assert reopened.get_thread("telegram") == "thread-1"
    assert reopened.get_metadata("authentication_degraded") is True
    assert reopened.clear_thread("telegram") == "thread-1"
    assert reopened.get_thread("telegram") is None


def test_existing_database_adds_response_column(tmp_path: Path) -> None:
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
    Database(path).initialize()
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
    assert "response" in columns
