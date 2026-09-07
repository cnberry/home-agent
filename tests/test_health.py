from __future__ import annotations

from datetime import date, datetime, timezone

import home_agent.health as health_module
from home_agent.config import Settings
from home_agent.database import Database
from home_agent.health import HealthSnapshot, _tasks_due, enqueue_heartbeat


def snapshot(*alerts: str) -> HealthSnapshot:
    return HealthSnapshot(
        captured_at=datetime.now(timezone.utc).isoformat(),
        failed_units=(),
        disks=(),
        memory_available_percent=50,
        load_per_cpu=0.1,
        cpu_temperature_c=40,
        alerts=alerts,
    )


def test_due_task_detection() -> None:
    today = date(2026, 8, 15)
    assert _tasks_due("- [ ] do it", today)
    assert _tasks_due("- [ ] old (due: 2026-08-14)", today)
    assert not _tasks_due("- [ ] later (due: 2026-08-16)", today)
    assert not _tasks_due("- [x] finished (due: 2026-08-14)", today)


def test_healthy_noop_does_not_queue(monkeypatch: object, settings: Settings) -> None:
    monkeypatch.setattr(health_module, "collect_health", lambda _: snapshot())  # type: ignore[attr-defined]
    database = Database(settings.database_path)
    database.initialize()
    assert enqueue_heartbeat(settings, database) is None


def test_health_alert_deduplicates_and_recovers(monkeypatch: object, settings: Settings) -> None:
    current = snapshot("disk warning at /: 90.0% used")
    monkeypatch.setattr(health_module, "collect_health", lambda _: current)  # type: ignore[attr-defined]
    database = Database(settings.database_path)
    database.initialize()
    first = enqueue_heartbeat(settings, database)
    assert first is not None
    database.finish(first.id, "completed")
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


def test_forced_heartbeat_queues_when_healthy(monkeypatch: object, settings: Settings) -> None:
    monkeypatch.setattr(health_module, "collect_health", lambda _: snapshot())  # type: ignore[attr-defined]
    database = Database(settings.database_path)
    database.initialize()
    assert enqueue_heartbeat(settings, database, force=True) is not None
