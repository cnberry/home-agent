from __future__ import annotations

import asyncio
import json
import sys
import time
from unittest.mock import AsyncMock

import pytest
from test_worker import Notifications, Runtime

from home_agent.control import ControlServer, submit
from home_agent.database import Database
from home_agent.outbox import Outbox
from home_agent.performance import Performance, accepted, report
from home_agent.routing import LocalRouter
from home_agent.worker import Worker


def fixture(tmp_path, adapter_body=""):
    catalog = {
        "version": "test",
        "capabilities": [
            {
                "id": "switch:test:on",
                "service": "switch",
                "target": "test",
                "aliases": ["test lights"],
                "action": "on",
                "label": "on test lights",
                "mutation": True,
            }
        ],
    }
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "import json,sys,time\nrequest=json.loads(sys.stdin.readline())\n" + adapter_body
    )
    cfg = tmp_path / "routing.json"
    cfg.write_text(
        json.dumps(
            {"catalog": str(tmp_path / "catalog.json"), "executor": [sys.executable, str(adapter)]}
        )
    )
    db = Database(tmp_path / "db.sqlite3", durable_replies=True)
    db.initialize()
    return cfg, db


@pytest.mark.parametrize(
    ("prompt", "matched"),
    [
        ("Turn on firepit lights", True),
        ("Turn on fire pit lights", True),
        ("Turn on fire-pit lights", True),
        ("Turn on fireplace lights", False),
    ],
)
def test_catalog_aliases_accept_separator_variants_without_guessing_targets(
    tmp_path, prompt, matched
):
    cfg, _ = fixture(tmp_path)
    catalog_path = tmp_path / "catalog.json"
    catalog = json.loads(catalog_path.read_text())
    catalog["capabilities"][0]["aliases"] = ["firepit lights"]
    catalog_path.write_text(json.dumps(catalog))

    candidates = LocalRouter(cfg).candidates(prompt)

    assert ([candidate["id"] for candidate in candidates] == ["switch:test:on"]) is matched


@pytest.mark.asyncio
async def test_event_driven_submit_deduplicates_and_records_latency(tmp_path):
    cfg, db = fixture(tmp_path)
    runtime = Runtime("fallback")
    worker = Worker(db, runtime, Notifications(), routing_config=cfg)
    worker.router.run = AsyncMock(return_value={"outcome": "confirmed", "reply": "Verified."})
    control = ControlServer(db, worker, tmp_path / "control.sock")
    worker.on_result = control.notify
    await control.start()
    task = asyncio.create_task(worker.run())
    try:
        first = await asyncio.wait_for(
            asyncio.to_thread(
                submit, control.path, "turn on test lights", 5, "benchmark", "stable-key"
            ),
            2,
        )
        again = await asyncio.to_thread(
            submit, control.path, "turn on test lights", 5, "benchmark", "stable-key"
        )
        assert first == again and first["status"] == "completed"
        assert not runtime.requests
        assert worker.router.run.await_count == 1
        assert report(db, source="benchmark")["jobs"] == 1
        with db.connect() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM interaction_events WHERE event='performance_accepted'"
                ).fetchone()[0]
                == 1
            )
    finally:
        await worker.stop()
        await task
        await control.close()


@pytest.mark.asyncio
async def test_uncertain_dispatch_reads_only_and_does_not_fall_back(tmp_path):
    cfg, db = fixture(
        tmp_path,
        "if request.get('read_only'):\n"
        " print(json.dumps({'event':'result','outcome':'observed','reply':'Read only'}),"
        "flush=True)\n"
        "else:\n print(json.dumps({'event':'dispatch_ready'}),flush=True)\n"
        " assert sys.stdin.readline().strip()=='GO'\n"
        " print(json.dumps({'event':'driver_issued','monotonic_ns':time.monotonic_ns()}),"
        "flush=True)\n"
        " print(json.dumps({'event':'result','outcome':'uncertain','reply':'Uncertain'}),"
        "flush=True)\n",
    )
    worker = Worker(db, Runtime("must not run"), Notifications(), routing_config=cfg)
    worker.router.classify = lambda *_: {"op": 1}
    job = db.enqueue("telegram", "turn on test lights")
    accepted(db, job, time.monotonic_ns(), "benchmark")
    job = db.claim_next()
    await worker._process(job)
    assert db.get_job(job.id).status == "uncertain"
    assert not worker.codex.requests
    result = report(db, source="benchmark")["groups"]["qwen/switch:test:on"]
    assert (
        result["eligible_terminal"] == 1
        and result["successes"] == 0
        and result["issue_samples"] == 1
    )
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM interaction_events WHERE event='action_dispatch_started'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_model_failure_falls_back_before_device_execution(tmp_path):
    cfg, db = fixture(tmp_path)
    runtime = Runtime("Handled by Codex")
    worker = Worker(db, runtime, Notifications(), routing_config=cfg)

    def unavailable(*_):
        raise TimeoutError("model unavailable")

    worker.router.classify = unavailable
    job = db.enqueue("telegram", "turn on test lights")
    job = db.claim_next()
    await worker._process(job)
    assert db.get_job(job.id).status == "completed"
    assert len(runtime.requests) == 1
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM interaction_events WHERE event='action_dispatch_started'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_codex_auth_failure_does_not_block_local_job(tmp_path):
    cfg, db = fixture(tmp_path)
    db.set_metadata("authentication_degraded", True)
    worker = Worker(db, Runtime("unused"), Notifications(), routing_config=cfg)
    worker.router.run = AsyncMock(return_value={"outcome": "confirmed", "reply": "Done"})
    job = db.enqueue("telegram", "turn on test lights")
    done = asyncio.Event()
    worker.on_result = done.set
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(done.wait(), 2)
        assert db.get_job(job.id).status == "completed"
    finally:
        await worker.stop()
        await task


@pytest.mark.asyncio
async def test_committed_result_has_durable_reply_without_reexecution(tmp_path):
    _, db = fixture(tmp_path)
    job = db.enqueue("telegram", "sample", telegram_chat_id=123)
    db.finish(job.id, "completed", response="Done")
    sender = AsyncMock(side_effect=OSError("delivery down"))
    outbox = Outbox(db, sender)
    task = asyncio.create_task(outbox.run())
    try:
        for _ in range(30):
            if sender.await_count:
                break
            await asyncio.sleep(0.01)
        assert sender.await_count == 1
        assert db.get_job(job.id).status == "completed"
    finally:
        await outbox.stop()
        await task
    # Restarted delivery resumes persisted output; no device executor exists here.
    with db.connect() as conn:
        conn.execute("UPDATE outbox SET due_at=0")
    delivered = asyncio.Event()

    async def send(*_):
        delivered.set()

    restarted = Outbox(db, send)
    task = asyncio.create_task(restarted.run())
    try:
        await asyncio.wait_for(delivered.wait(), 2)
    finally:
        await restarted.stop()
        await task
    with db.connect() as conn:
        assert conn.execute("SELECT delivered FROM outbox").fetchone()[0] == 1


def test_metrics_do_not_count_unknown_or_missing_timing_as_success(tmp_path):
    _, db = fixture(tmp_path)
    for status, outcome in [
        ("completed", "confirmed"),
        ("uncertain", "uncertain"),
        ("completed", "clarify"),
    ]:
        job = db.enqueue("telegram", "sample")
        job = db.claim_next()
        accepted(db, job, time.monotonic_ns(), "benchmark")
        perf = Performance(db, job)
        perf.data.update(
            engine="qwen", capability="test", eligible=outcome != "clarify", outcome=outcome
        )
        db.finish(job.id, status)
        perf.finish()
    stats = report(db)["groups"]["qwen/test"]
    assert (
        stats["success_rate"] == 0.5
        and stats["issue_samples"] == 0
        and stats["clarifications"] == 1
    )


@pytest.mark.asyncio
async def test_idle_worker_waits_for_wake_and_crashed_job_stays_in_metrics(tmp_path):
    cfg, db = fixture(tmp_path)
    worker = Worker(db, Runtime("unused"), Notifications(), routing_config=cfg)
    waiting = asyncio.create_task(worker._wait_for_activity())
    await asyncio.sleep(0.03)
    assert not waiting.done()
    worker.wake()
    await asyncio.wait_for(waiting, 0.2)
    job = db.enqueue("telegram", "turn on test lights")
    accepted(db, job, time.monotonic_ns(), "benchmark")
    db.claim_next()
    db.recover_interrupted()
    stats = report(db, source="benchmark")
    assert stats["jobs"] == 1 and stats["missing_final_evidence"] == 1
    assert next(iter(stats["groups"].values()))["success_rate"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    [
        "Do not turn on test lights",
        "Turn on test lights later",
        "If it is dark, turn on test lights",
    ],
)
async def test_negated_and_conditional_writes_never_dispatch(tmp_path, prompt):
    cfg, db = fixture(tmp_path)
    worker = Worker(db, Runtime("unused"), Notifications(), routing_config=cfg)
    worker.router.classify = lambda *_: {"op": 1}
    job = db.enqueue("telegram", prompt)
    await worker._process(db.claim_next())
    assert db.get_job(job.id).status == "completed"
    assert not worker.codex.requests
    with db.connect() as conn:
        assert not conn.execute(
            "SELECT 1 FROM interaction_events WHERE event='action_dispatch_started'"
        ).fetchone()


@pytest.mark.asyncio
async def test_telegram_local_intake_does_not_wait_for_reply_and_deduplicates(tmp_path, settings):
    from dataclasses import replace

    from telegram.ext import ApplicationBuilder
    from test_telegram import Runtime as TelegramRuntime
    from test_telegram import TelegramAPI, telegram_update

    from home_agent.telegram_gateway import TelegramGateway

    cfg, _ = fixture(tmp_path)
    api = TelegramAPI()
    application = ApplicationBuilder().token("123456:fake-token").request(api).build()
    runtime = TelegramRuntime()
    gateway = TelegramGateway(
        replace(settings, routing_config=cfg), "unused", application=application, codex=runtime
    )
    gateway.worker.router.run = AsyncMock(
        return_value={"outcome": "confirmed", "reply": "Verified"}
    )
    async with application:
        update = telegram_update(gateway, "turn on test lights")
        await application.process_update(update)
        await application.process_update(update)
        assert gateway.database.snapshot().queued == 1 and not api.messages
        await gateway._post_init(application)
        try:
            for _ in range(100):
                if api.messages:
                    break
                await asyncio.sleep(0.01)
            assert len(api.messages) == 1
            assert api.messages[0][1]["text"] == "Verified"
            assert gateway.database.get_job(1).status == "completed"
            assert not runtime.entered.is_set()
        finally:
            await gateway._post_stop(application)
            await gateway._post_shutdown(application)


def test_heartbeat_outbox_preserves_alerts_but_not_noop(tmp_path):
    _, db = fixture(tmp_path)
    alert = db.enqueue("heartbeat", "health")
    db.finish(alert.id, "completed", response="Disk needs attention")
    noop = db.enqueue("heartbeat", "health")
    db.finish(noop.id, "completed", response="NOOP")
    with db.connect() as conn:
        rows = conn.execute("SELECT job_id,text FROM outbox").fetchall()
    assert [(r["job_id"], r["text"]) for r in rows] == [(alert.id, "Disk needs attention")]
