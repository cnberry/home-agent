from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_routing_integration import fixture
from test_worker import Notifications, Runtime

from home_agent.database import Database
from home_agent.outbox import Outbox
from home_agent.routing import LocalRouter
from home_agent.supervision import Supervisor, enqueue_review
from home_agent.worker import Worker


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["Turn on the test lamps", "Turn on test lights"])
async def test_recognition_failure_reaches_codex_and_leaves_review_evidence(tmp_path, prompt):
    cfg, db = fixture(tmp_path)
    worker = Worker(db, Runtime("Handled by Codex"), Notifications(), routing_config=cfg)
    worker.router.classify = lambda *_: {"op": 0}
    job = db.enqueue("telegram", prompt)
    await worker._process(db.claim_next())
    assert db.get_job(job.id).response == "Handled by Codex"
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM local_reviews").fetchone()
    assert row is not None and row["status"] == "pending"
    assert worker.codex.requests


def make_review(tmp_path, *, prompt="Turn on test lights", verdict="incorrect"):
    cfg, db = fixture(tmp_path)
    job = db.enqueue("telegram", prompt, telegram_chat_id=123)
    job = db.claim_next()
    enqueue_review(db, job, {"outcome": "confirmed", "reply": "Done"}, {})
    db.finish(job.id, "completed", response="Done")
    reviewer = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                response=json.dumps(
                    {
                        "verdict": verdict,
                        "expected_capability": "switch:test:on",
                        "explanation": "The correct device action is on.",
                    }
                )
            )
        ),
        interrupt=AsyncMock(),
    )
    router = LocalRouter(cfg, db)
    return db, job, router, reviewer


@pytest.mark.asyncio
async def test_monitor_correction_is_durable_and_never_executes_devices(tmp_path):
    db, job, router, reviewer = make_review(tmp_path)
    router.classify = lambda *_: {"op": 1}
    router.execute = AsyncMock(side_effect=AssertionError("Review cannot execute"))
    supervisor = Supervisor(db, router, reviewer, lambda: None)
    with db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM local_reviews").fetchone())
    await supervisor.process(row)
    with db.connect() as conn:
        review = conn.execute("SELECT status,result FROM local_reviews").fetchone()
        reply = conn.execute("SELECT * FROM outbox").fetchone()
        lesson = conn.execute("SELECT * FROM routing_examples").fetchone()
    assert review["status"] == "completed"
    assert json.loads(review["result"])["learning"] == "promoted"
    assert lesson["active"] == 1
    assert "No additional device action" in reply["text"] and reply["revision"] == 1
    assert db.get_job(job.id).status == "completed"
    assert not router.execute.await_args_list


@pytest.mark.asyncio
async def test_failed_regression_never_activates_example(tmp_path):
    db, _, router, reviewer = make_review(tmp_path)
    router.classify = lambda *_: {"op": 0}
    supervisor = Supervisor(db, router, reviewer, lambda: None)
    with db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM local_reviews").fetchone())
    await supervisor.process(row)
    assert not router.examples(active=True)
    with db.connect() as conn:
        result = json.loads(conn.execute("SELECT result FROM local_reviews").fetchone()[0])
    assert result["learning"] == "rejected_regression"


@pytest.mark.asyncio
async def test_new_example_cannot_regress_previous_case(tmp_path):
    db, _, router, reviewer = make_review(tmp_path)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO routing_examples VALUES(999,1,?,?,?,0)",
            ("test", "Switch test lights on", "switch:test:on"),
        )
    router.classify = lambda prompt, *_: {"op": 0 if prompt.startswith("Switch") else 1}
    supervisor = Supervisor(db, router, reviewer, lambda: None)
    with db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM local_reviews").fetchone())
    await supervisor.process(row)
    assert not router.examples(active=True)


@pytest.mark.asyncio
async def test_review_outage_preserves_evidence_and_retries_after_restart(tmp_path):
    db, _, router, reviewer = make_review(tmp_path, verdict="correct")
    reviewer.run.side_effect = TimeoutError()
    first = Supervisor(db, router, reviewer, lambda: None)
    with db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM local_reviews").fetchone())
    await first.process(row)
    with db.connect() as conn:
        failed = conn.execute("SELECT * FROM local_reviews").fetchone()
        assert failed["status"] == "pending" and failed["attempts"] == 1 and failed["error"]
        conn.execute("UPDATE local_reviews SET status='running',due_at=0")
    reviewer.run.side_effect = None
    done = asyncio.Event()
    restarted = Supervisor(db, router, reviewer, done.set)
    task = asyncio.create_task(restarted.run())
    try:
        await asyncio.wait_for(done.wait(), 2)
    finally:
        await restarted.stop()
        await task
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM local_reviews").fetchone()[0] == "completed"


@pytest.mark.asyncio
async def test_delayed_original_delivery_cannot_lose_review_correction(tmp_path):
    db = Database(tmp_path / "db", durable_replies=True)
    db.initialize()
    job = db.enqueue("telegram", "test", telegram_chat_id=123)
    db.finish(job.id, "completed", response="Original")
    delivered = []
    done = asyncio.Event()

    async def send(_, text):
        delivered.append(text)
        if text == "Original":
            with db.connect() as conn:
                conn.execute("UPDATE outbox SET text='Correction',revision=revision+1,delivered=0")
        else:
            done.set()

    outbox = Outbox(db, send)
    task = asyncio.create_task(outbox.run())
    try:
        await asyncio.wait_for(done.wait(), 2)
    finally:
        await outbox.stop()
        await task
    assert delivered == ["Original", "Correction"]


@pytest.mark.asyncio
async def test_promoted_example_resolves_repeated_alias_but_still_runs_interpreter(tmp_path):
    db, job, router, reviewer = make_review(tmp_path, prompt="Turn on the test lamps")
    assert not router.candidates(job.prompt)
    router.classify = lambda *_: {"op": 1}
    supervisor = Supervisor(db, router, reviewer, lambda: None)
    with db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM local_reviews").fetchone())
    await supervisor.process(row)
    restarted = LocalRouter(tmp_path / "routing.json", db)
    assert [c["id"] for c in restarted.candidates(job.prompt)] == ["switch:test:on"]
    assert not restarted.candidates("Turn off the test lamps")
    restarted.catalog["version"] = "changed"
    assert not restarted.candidates(job.prompt)


@pytest.mark.asyncio
async def test_slow_reviewer_does_not_hold_foreground_queue(tmp_path):
    from home_agent.control import ControlServer, submit

    cfg, db = fixture(tmp_path)
    worker = Worker(db, Runtime("unused"), Notifications(), routing_config=cfg)
    worker.router.run = AsyncMock(return_value={"outcome": "confirmed", "reply": "Verified"})
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_review(*_, **__):
        entered.set()
        await release.wait()
        return SimpleNamespace(
            response=json.dumps(
                {"verdict": "correct", "expected_capability": None, "explanation": "Correct"}
            )
        )

    reviewer = SimpleNamespace(run=slow_review, interrupt=AsyncMock())
    supervisor = Supervisor(db, worker.router, reviewer, lambda: None)
    control = ControlServer(db, worker, tmp_path / "control.sock")
    worker.on_result = lambda: (supervisor.notify(), control.notify())
    await control.start()
    worker_task = asyncio.create_task(worker.run())
    review_task = asyncio.create_task(supervisor.run())
    try:
        first = await asyncio.wait_for(asyncio.to_thread(submit, control.path, "test lights on"), 2)
        await asyncio.wait_for(entered.wait(), 2)
        second = await asyncio.wait_for(
            asyncio.to_thread(submit, control.path, "test lights on"), 2
        )
        assert first["status"] == second["status"] == "completed"
        assert not release.is_set()
    finally:
        release.set()
        await supervisor.stop()
        await worker.stop()
        await asyncio.gather(worker_task, review_task)
        await control.close()


@pytest.mark.asyncio
async def test_failed_model_evaluation_does_not_lose_codex_correction(tmp_path):
    db, _, router, reviewer = make_review(tmp_path)

    def unavailable(*_):
        raise TimeoutError()

    router.classify = unavailable
    supervisor = Supervisor(db, router, reviewer, lambda: None)
    with db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM local_reviews").fetchone())
    await supervisor.process(row)
    with db.connect() as conn:
        review = conn.execute("SELECT status,result FROM local_reviews").fetchone()
        text = conn.execute("SELECT text FROM outbox").fetchone()[0]
    assert review["status"] == "completed"
    assert json.loads(review["result"])["learning"] == "pending_model_evaluation"
    assert "Codex review" in text
    assert not router.examples(active=True)


def test_monitor_detected_error_is_not_counted_as_verified_success(tmp_path):
    from home_agent.performance import Performance, report

    db, job, _, _ = make_review(tmp_path)
    perf = Performance(db, job)
    perf.data.update(engine="qwen", eligible=True, capability="switch:test:on", outcome="confirmed")
    perf.finish()
    with db.connect() as conn:
        conn.execute(
            "UPDATE local_reviews SET status='completed',result=?",
            (json.dumps({"verdict": "incorrect"}),),
        )
    stats = report(db)["groups"]["qwen/switch:test:on"]
    assert stats["successes"] == 0 and stats["review_incorrect"] == 1


@pytest.mark.asyncio
async def test_uncertain_review_never_becomes_training_label(tmp_path):
    db, _, router, reviewer = make_review(tmp_path, verdict="uncertain")
    supervisor = Supervisor(db, router, reviewer, lambda: None)
    with db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM local_reviews").fetchone())
    await supervisor.process(row)
    assert not router.examples()
    with db.connect() as conn:
        result = json.loads(conn.execute("SELECT result FROM local_reviews").fetchone()[0])
    assert result["learning"] == "uncertain_needs_review"
