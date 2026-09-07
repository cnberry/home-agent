from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo

import pytest

import home_agent.health as health_module
from home_agent.config import Settings
from home_agent.database import Database
from home_agent.health import HealthSnapshot, enqueue_heartbeat


def snapshot(*alerts: str, failed_units: tuple[str, ...] = ()) -> HealthSnapshot:
    return HealthSnapshot(
        captured_at=datetime.now(timezone.utc).isoformat(),
        failed_units=failed_units,
        disks=(),
        memory_available_percent=50,
        load_per_cpu=0.1,
        cpu_temperature_c=40,
        alerts=alerts,
    )


@pytest.mark.parametrize(
    ("tasks", "instructions", "force", "queued"),
    [
        ("", "", False, False),
        ("- [ ] do it", "", False, True),
        ("- [ ] old (due: 2000-01-01)", "", False, True),
        ("- [ ] later (due: 2999-01-01)", "", False, False),
        ("- [x] finished", "", False, False),
        ("- [ ] malformed due:", "", False, True),
        ("- [ ] malformed (due: nonsense)", "", False, True),
        ("<!--\n- [ ] example\n-->", "", False, False),
        ("", "# Heading\n<!-- example only -->", False, False),
        ("", "<!-- comment -->Check pending maintenance.", False, True),
        ("", "", True, True),
    ],
)
def test_heartbeat_runs_only_for_actionable_work(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tasks: str,
    instructions: str,
    force: bool,
    queued: bool,
) -> None:
    monkeypatch.setattr(health_module, "collect_health", lambda _: snapshot())
    settings.tasks_file.write_text(tasks, encoding="utf-8")
    settings.heartbeat_file.write_text(instructions, encoding="utf-8")
    database = Database(settings.database_path)
    database.initialize()
    job = enqueue_heartbeat(settings, database, force=force)
    assert (job is not None) == queued
    if job:
        assert job.kind == "heartbeat"
        assert tasks in job.prompt
        assert instructions in job.prompt


def test_alerts_repeat_after_failure_severity_change_or_recovery(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = snapshot("disk warning at /: 90.0% used")
    monkeypatch.setattr(health_module, "collect_health", lambda _: current)
    database = Database(settings.database_path)
    database.initialize()
    first = enqueue_heartbeat(settings, database)
    assert first is not None
    assert enqueue_heartbeat(settings, database) is None
    database.finish(first.id, "failed", error="connection unavailable")
    retry = enqueue_heartbeat(settings, database)
    assert retry is not None
    database.finish(retry.id, "completed")
    current = snapshot("disk warning at /: 91.0% used")
    assert enqueue_heartbeat(settings, database) is None
    current = snapshot("disk critical at /: 96.0% used")
    critical = enqueue_heartbeat(settings, database)
    assert critical is not None
    database.finish(critical.id, "completed")
    current = snapshot()
    assert enqueue_heartbeat(settings, database) is None
    current = snapshot("disk warning at /: 90.0% used")
    assert enqueue_heartbeat(settings, database) is not None


def test_alert_order_does_not_trigger_another_notification(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = snapshot("load high: 3 per CPU over 15 minutes", "2 failed systemd unit(s)",
                       failed_units=("a.service", "b.service"))
    monkeypatch.setattr(health_module, "collect_health", lambda _: current)
    database = Database(settings.database_path)
    database.initialize()
    first = enqueue_heartbeat(settings, database)
    assert first is not None
    database.finish(first.id, "completed")
    current = snapshot(
        *reversed(current.alerts), failed_units=tuple(reversed(current.failed_units))
    )
    assert enqueue_heartbeat(settings, database) is None


def test_alert_repeat_window_expires_without_real_sleep(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return now if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(health_module, "datetime", Clock)
    monkeypatch.setattr(health_module, "collect_health", lambda _: snapshot("load high: 3"))
    database = Database(settings.database_path)
    database.initialize()
    first = enqueue_heartbeat(settings, database)
    assert first is not None
    database.finish(first.id, "completed")
    now += timedelta(seconds=settings.heartbeat_repeat_seconds - 1)
    assert enqueue_heartbeat(settings, database) is None
    settings.tasks_file.write_text("- [ ] unrelated task\n", encoding="utf-8")
    task_job = enqueue_heartbeat(settings, database)
    assert task_job is not None
    database.finish(task_job.id, "completed")
    settings.tasks_file.write_text("", encoding="utf-8")
    now += timedelta(seconds=2)
    assert enqueue_heartbeat(settings, database) is not None
