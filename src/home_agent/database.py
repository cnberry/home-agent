from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from home_agent.redaction import redact

JobKind = Literal["telegram", "heartbeat"]
JobStatus = Literal["queued", "running", "completed", "failed", "cancelled", "uncertain"]


class QueueFullError(RuntimeError):
    """The bounded queue is full."""


@dataclass(frozen=True)
class Job:
    id: int
    kind: JobKind
    prompt: str
    status: JobStatus
    attempts: int
    telegram_update_id: int | None
    telegram_chat_id: int | None
    telegram_message_id: int | None
    ack_message_id: int | None
    codex_started: bool
    created_at: str
    available_at: str
    started_at: str | None
    completed_at: str | None
    error: str | None
    response: str | None


@dataclass(frozen=True)
class QueueSnapshot:
    queued: int
    active: Job | None
    last_completed_at: str | None
    last_heartbeat_at: str | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Database:
    def __init__(self, path: Path, max_queue: int = 20) -> None:
        self.path = path
        self.max_queue = max_queue

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS updates (
                    update_id INTEGER PRIMARY KEY,
                    received_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK (kind IN ('telegram', 'heartbeat')),
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'queued', 'running', 'completed', 'failed', 'cancelled', 'uncertain'
                        )
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    telegram_update_id INTEGER UNIQUE,
                    telegram_chat_id INTEGER,
                    telegram_message_id INTEGER,
                    ack_message_id INTEGER,
                    codex_started INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    error TEXT,
                    response TEXT
                );

                CREATE INDEX IF NOT EXISTS jobs_claim_idx
                    ON jobs(status, available_at, id);

                CREATE TABLE IF NOT EXISTS threads (
                    kind TEXT PRIMARY KEY CHECK (kind IN ('telegram', 'heartbeat')),
                    thread_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "response" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN response TEXT")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS interaction_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    event TEXT NOT NULL,
                    job_id INTEGER,
                    update_id INTEGER,
                    details TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS interaction_events_time
                    ON interaction_events(occurred_at);
                CREATE UNIQUE INDEX IF NOT EXISTS interaction_received_once
                    ON interaction_events(update_id) WHERE event = 'received';
            """)

    def _job_event(self, db: sqlite3.Connection, event: str, job_id: int) -> None:
        job = self.get_job(job_id, connection=db)
        if job is not None:
            db.execute(
                "INSERT INTO interaction_events"
                "(occurred_at,event,job_id,update_id,details) VALUES (?,?,?,?,?)",
                (utc_now(), event, job_id, job.telegram_update_id, redact(json.dumps(asdict(job)))),
            )

    def record_event(
        self,
        event: str,
        *,
        job_id: int | None = None,
        update_id: int | None = None,
        details: object = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO interaction_events"
                "(occurred_at, event, job_id, update_id, details) VALUES (?, ?, ?, ?, ?)",
                (utc_now(), event, job_id, update_id, redact(json.dumps(details))),
            )

    @staticmethod
    def _job(row: sqlite3.Row | None) -> Job | None:
        if row is None:
            return None
        return Job(
            id=row["id"],
            kind=row["kind"],
            prompt=row["prompt"],
            status=row["status"],
            attempts=row["attempts"],
            telegram_update_id=row["telegram_update_id"],
            telegram_chat_id=row["telegram_chat_id"],
            telegram_message_id=row["telegram_message_id"],
            ack_message_id=row["ack_message_id"],
            codex_started=bool(row["codex_started"]),
            created_at=row["created_at"],
            available_at=row["available_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error=row["error"],
            response=row["response"],
        )

    def record_update(self, update_id: int) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO updates(update_id, received_at) VALUES (?, ?)",
                (update_id, utc_now()),
            )
            return cursor.rowcount == 1

    def enqueue(
        self,
        kind: JobKind,
        prompt: str,
        *,
        telegram_update_id: int | None = None,
        telegram_chat_id: int | None = None,
        telegram_message_id: int | None = None,
        deduplicate_kind: bool = False,
    ) -> Job | None:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if telegram_update_id is not None:
                existing = db.execute(
                    "SELECT id FROM jobs WHERE telegram_update_id = ?", (telegram_update_id,)
                ).fetchone()
                if existing:
                    return None
            if deduplicate_kind:
                existing = db.execute(
                    """
                SELECT id FROM jobs
                WHERE kind = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                    (kind,),
                ).fetchone()
                if existing:
                    return None
            count = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
            if count >= self.max_queue:
                raise QueueFullError(f"queue contains {count} active jobs")
            cursor = db.execute(
                """
                INSERT INTO jobs(
                    kind, prompt, status, telegram_update_id, telegram_chat_id,
                    telegram_message_id, created_at, available_at
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    prompt,
                    telegram_update_id,
                    telegram_chat_id,
                    telegram_message_id,
                    now,
                    now,
                ),
            )
            job_id = cursor.lastrowid
            if job_id is None:  # pragma: no cover - SQLite always supplies a row ID here
                raise RuntimeError("SQLite did not return a job ID")
            if telegram_update_id is not None:
                db.execute(
                    "INSERT OR IGNORE INTO updates(update_id, received_at) VALUES (?, ?)",
                    (telegram_update_id, now),
                )
            self._job_event(db, "queued", job_id)
            return self.get_job(job_id, connection=db)

    def set_ack_message(self, job_id: int, message_id: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE jobs SET ack_message_id = ? WHERE id = ?", (message_id, job_id))
            self._job_event(db, "acknowledged", job_id)

    def claim_next(self) -> Job | None:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            paused = db.execute(
                "SELECT value FROM metadata WHERE key = 'deployment_paused'"
            ).fetchone()
            if paused and json.loads(paused[0]):
                return None
            row = db.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'queued' AND available_at <= ?
                ORDER BY id LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                """
                UPDATE jobs
                SET status = 'running', attempts = attempts + 1, started_at = ?,
                    completed_at = NULL, error = NULL, codex_started = 0
                WHERE id = ?
                """,
                (now, row["id"]),
            )
            self._job_event(db, "running", row["id"])
            return self.get_job(row["id"], connection=db)

    def mark_codex_started(self, job_id: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE jobs SET codex_started = 1 WHERE id = ?", (job_id,))
            self._job_event(db, "codex_started", job_id)

    def finish(
        self,
        job_id: int,
        status: JobStatus,
        error: str | None = None,
        *,
        response: str | None = None,
    ) -> None:
        if status not in {"completed", "failed", "cancelled", "uncertain"}:
            raise ValueError(f"invalid terminal job status {status}")
        with self.connect() as db:
            db.execute(
                """
                UPDATE jobs SET status = ?, completed_at = ?, error = ?, response = ?
                WHERE id = ?
                """,
                (status, utc_now(), error, response, job_id),
            )
            self._job_event(db, status, job_id)

    def requeue(self, job_id: int, delay_seconds: int, error: str) -> None:
        available = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat(
            timespec="microseconds"
        )
        with self.connect() as db:
            db.execute(
                """
                UPDATE jobs SET status = 'queued', available_at = ?, started_at = NULL,
                    completed_at = NULL, error = ?, response = NULL, codex_started = 0
                WHERE id = ?
                """,
                (available, error, job_id),
            )
            self._job_event(db, "requeued", job_id)

    def retry(self, job_id: int) -> Job | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            eligible = db.execute(
                "SELECT id FROM jobs WHERE id = ? "
                "AND status IN ('failed', 'uncertain', 'cancelled')",
                (job_id,),
            ).fetchone()
            if eligible is None:
                return None
            count = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
            if count >= self.max_queue:
                raise QueueFullError(f"queue contains {count} active jobs")
            cursor = db.execute(
                """
                UPDATE jobs SET status = 'queued', available_at = ?, started_at = NULL,
                    completed_at = NULL, error = NULL, response = NULL, codex_started = 0,
                    attempts = 0
                WHERE id = ? AND status IN ('failed', 'uncertain', 'cancelled')
                """,
                (utc_now(), job_id),
            )
            if cursor.rowcount != 1:
                return None
            self._job_event(db, "retry_requested", job_id)
            return self.get_job(job_id, connection=db)

    def recover_interrupted(self) -> list[Job]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM jobs WHERE status = 'running'").fetchall()
            if rows:
                db.execute(
                    """
                    UPDATE jobs SET status = 'uncertain', completed_at = ?,
                        error = 'service restarted while job was running'
                    WHERE status = 'running'
                    """,
                    (utc_now(),),
                )
            for row in rows:
                self._job_event(db, "uncertain", row["id"])
            return [
                job for row in rows if (job := self.get_job(row["id"], connection=db)) is not None
            ]

    def get_job(self, job_id: int, *, connection: sqlite3.Connection | None = None) -> Job | None:
        if connection is not None:
            return self._job(
                connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            )
        with self.connect() as db:
            return self._job(db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def active_job(self) -> Job | None:
        with self.connect() as db:
            return self._job(
                db.execute(
                    "SELECT * FROM jobs WHERE status = 'running' ORDER BY id LIMIT 1"
                ).fetchone()
            )

    def get_thread(self, kind: JobKind) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT thread_id FROM threads WHERE kind = ?", (kind,)).fetchone()
            return row[0] if row else None

    def set_thread(self, kind: JobKind, thread_id: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO threads(kind, thread_id, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(kind) DO UPDATE SET thread_id = excluded.thread_id,
                    updated_at = excluded.updated_at
                """,
                (kind, thread_id, utc_now()),
            )

    def clear_thread(self, kind: JobKind) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT thread_id FROM threads WHERE kind = ?", (kind,)).fetchone()
            db.execute("DELETE FROM threads WHERE kind = ?", (kind,))
            return row[0] if row else None

    def set_metadata(self, key: str, value: object) -> None:
        encoded = json.dumps(value, sort_keys=True)
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, encoded, utc_now()),
            )

    def get_metadata(self, key: str) -> object | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def snapshot(self) -> QueueSnapshot:
        with self.connect() as db:
            queued = db.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'").fetchone()[0]
            active = self._job(
                db.execute(
                    "SELECT * FROM jobs WHERE status = 'running' ORDER BY id LIMIT 1"
                ).fetchone()
            )
            last_completed = db.execute(
                "SELECT completed_at FROM jobs WHERE status = 'completed' "
                "ORDER BY completed_at DESC, id DESC LIMIT 1"
            ).fetchone()
            last_heartbeat = db.execute(
                """
                SELECT completed_at FROM jobs
                WHERE kind = 'heartbeat' AND status = 'completed'
                ORDER BY completed_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
            return QueueSnapshot(
                queued=queued,
                active=active,
                last_completed_at=last_completed[0] if last_completed else None,
                last_heartbeat_at=last_heartbeat[0] if last_heartbeat else None,
            )
