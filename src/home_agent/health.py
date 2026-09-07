from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from home_agent.config import Settings
from home_agent.database import Database, Job


@dataclass(frozen=True)
class DiskHealth:
    path: str
    used_percent: float


@dataclass(frozen=True)
class HealthSnapshot:
    captured_at: str
    failed_units: tuple[str, ...]
    disks: tuple[DiskHealth, ...]
    memory_available_percent: float | None
    load_per_cpu: float | None
    cpu_temperature_c: float | None
    alerts: tuple[str, ...]

    def as_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @property
    def fingerprint(self) -> str:
        keys: list[str] = []
        for alert in self.alerts:
            key = re.sub(r": [0-9.]+% used$", "", alert)
            key = re.sub(r"^memory low:.*$", "memory low", key)
            key = re.sub(r"^load high:.*$", "load high", key)
            key = re.sub(r"^CPU temperature high:.*$", "CPU temperature high", key)
            keys.append(key)
        if self.failed_units:
            keys.extend(f"failed-unit:{unit}" for unit in self.failed_units)
        payload = json.dumps(keys, sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()


def _failed_units() -> tuple[str, ...]:
    try:
        result = subprocess.run(
            ["systemctl", "--failed", "--no-legend", "--plain"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode not in {0, 1}:
        return ()
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _disk_health(paths: tuple[Path, ...]) -> tuple[DiskHealth, ...]:
    seen: set[int] = set()
    results: list[DiskHealth] = []
    for path in paths:
        candidate = path
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        try:
            stat = os.stat(candidate)
            values = os.statvfs(candidate)
        except OSError:
            continue
        if stat.st_dev in seen or values.f_blocks == 0:
            continue
        seen.add(stat.st_dev)
        available = values.f_bavail * values.f_frsize
        total = values.f_blocks * values.f_frsize
        used_percent = round((1 - available / total) * 100, 1)
        results.append(DiskHealth(path=str(candidate), used_percent=used_percent))
    return tuple(results)


def _memory_available_percent() -> float | None:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return None
    return round(available / total * 100, 1)


def _load_per_cpu() -> float | None:
    try:
        load_15m = os.getloadavg()[2]
    except (AttributeError, OSError):
        return None
    cpus = os.cpu_count() or 1
    return round(load_15m / cpus, 2)


def _cpu_temperature() -> float | None:
    readings: list[float] = []
    for path in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            raw = float(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        value = raw / 1000 if raw > 500 else raw
        if -20 <= value <= 150:
            readings.append(value)
    return round(max(readings), 1) if readings else None


def collect_health(settings: Settings) -> HealthSnapshot:
    failed_units = _failed_units()
    disks = _disk_health((Path("/"), settings.workspace, settings.data_dir))
    memory = _memory_available_percent()
    load = _load_per_cpu()
    temperature = _cpu_temperature()
    alerts: list[str] = []

    if failed_units:
        alerts.append(f"{len(failed_units)} failed systemd unit(s)")
    for disk in disks:
        if disk.used_percent >= settings.disk_critical_percent:
            alerts.append(f"disk critical at {disk.path}: {disk.used_percent}% used")
        elif disk.used_percent >= settings.disk_warning_percent:
            alerts.append(f"disk warning at {disk.path}: {disk.used_percent}% used")
    if memory is not None and memory < settings.memory_available_warning_percent:
        alerts.append(f"memory low: {memory}% available")
    if load is not None and load > settings.load_per_cpu_warning:
        alerts.append(f"load high: {load} per CPU over 15 minutes")
    if temperature is not None and temperature >= settings.cpu_temperature_warning_c:
        alerts.append(f"CPU temperature high: {temperature} C")

    return HealthSnapshot(
        captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        failed_units=failed_units,
        disks=disks,
        memory_available_percent=memory,
        load_per_cpu=load,
        cpu_temperature_c=temperature,
        alerts=tuple(alerts),
    )


def _read_durable(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _has_instructions(content: str) -> bool:
    in_comment = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("<!--"):
            in_comment = True
        if not in_comment and line and not line.startswith("#"):
            return True
        if line.endswith("-->"):
            in_comment = False
    return False


def _tasks_due(content: str, today: date | None = None) -> bool:
    current_date = today or datetime.now(timezone.utc).date()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line.startswith("- [ ]"):
            continue
        marker = "due:"
        if marker not in line.lower():
            return True
        due_value = line.lower().split(marker, 1)[1].strip().split()[0].rstrip(")]},;.")
        try:
            if date.fromisoformat(due_value) <= current_date:
                return True
        except ValueError:
            return True
    return False


def _recent_duplicate(database: Database, snapshot: HealthSnapshot, repeat_seconds: int) -> bool:
    previous = database.get_metadata("heartbeat_alert")
    if not isinstance(previous, dict):
        return False
    if previous.get("fingerprint") != snapshot.fingerprint:
        return False
    timestamp = previous.get("enqueued_at")
    if not isinstance(timestamp, str):
        return False
    try:
        elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    return elapsed.total_seconds() < repeat_seconds


def enqueue_heartbeat(settings: Settings, database: Database, *, force: bool = False) -> Job | None:
    snapshot = collect_health(settings)
    tasks = _read_durable(settings.tasks_file)
    instructions = _read_durable(settings.heartbeat_file)
    has_durable_work = _tasks_due(tasks) or _has_instructions(instructions)
    database.set_metadata("last_health_snapshot", asdict(snapshot))
    duplicate_alert = bool(
        snapshot.alerts and _recent_duplicate(database, snapshot, settings.heartbeat_repeat_seconds)
    )

    if not snapshot.alerts:
        database.set_metadata("heartbeat_alert", False)
    if not force and not snapshot.alerts and not has_durable_work:
        return None
    if not force and duplicate_alert and not has_durable_work:
        return None

    duplicate_note = (
        "These host alerts were already reported at the same severity. Do not repeat them unless "
        "your durable instructions reveal a new actionable finding."
        if duplicate_alert and not force
        else "These host alerts are new, recovered-and-recurred, or changed severity."
    )

    prompt = f"""Perform one Home Agent heartbeat.

Host health snapshot:
```json
{snapshot.as_json()}
```
Alert state: {duplicate_note}

Durable tasks from {settings.tasks_file}:
{tasks or "(none)"}

Heartbeat instructions from {settings.heartbeat_file}:
{instructions or "(none)"}

Investigate only what is needed, update durable state when appropriate, and report only
actionable findings. If nothing needs the owner's attention, respond with exactly NOOP.
"""
    job = database.enqueue("heartbeat", prompt, deduplicate_kind=True)
    if job is not None and snapshot.alerts and not duplicate_alert:
        database.set_metadata(
            "heartbeat_alert",
            {"fingerprint": snapshot.fingerprint, "enqueued_at": snapshot.captured_at},
        )
    return job
