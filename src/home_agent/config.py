from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

DEFAULT_CONFIG_PATH = Path("/etc/home-agent/config.toml")
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "low"
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})


class ConfigError(ValueError):
    """Configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    telegram_owner_id: int
    workspace: Path = Path("/srv/home-agent/workspace")
    data_dir: Path = Path("/var/lib/home-agent")
    database_path: Path = Path("/var/lib/home-agent/runtime/runtime.sqlite3")
    durable_dir: Path = Path("/var/lib/home-agent/durable")
    codex_home: Path = Path("/var/lib/home-agent/codex-home")
    telegram_token_file: Path = Path("/etc/home-agent/credentials/telegram-token")
    backup_checkout: Path = Path("/var/lib/home-agent-backup/repo")
    max_input_chars: int = 12_000
    max_queue: int = 20
    turn_timeout_seconds: int = 2_700
    worker_poll_seconds: float = 1.0
    model: str = DEFAULT_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    disk_warning_percent: float = 85.0
    disk_critical_percent: float = 95.0
    memory_available_warning_percent: float = 10.0
    load_per_cpu_warning: float = 2.0
    cpu_temperature_warning_c: float = 85.0
    heartbeat_repeat_seconds: int = 21_600

    @property
    def tasks_file(self) -> Path:
        return self.durable_dir / "tasks.md"

    @property
    def memory_file(self) -> Path:
        return self.durable_dir / "memory.md"

    @property
    def heartbeat_file(self) -> Path:
        return self.durable_dir / "heartbeat.md"

    def read_telegram_token(self) -> str:
        credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
        token_path = (
            Path(credential_dir) / "telegram-token" if credential_dir else self.telegram_token_file
        )
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read Telegram token file {token_path}: {exc}") from exc
        if not token or ":" not in token:
            raise ConfigError(f"Telegram token file {token_path} is empty or malformed")
        return token


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _path(value: Any, default: Path, name: str) -> Path:
    if value is None:
        return default
    if not isinstance(value, str) or not value.startswith("/"):
        raise ConfigError(f"{name} must be an absolute path")
    return Path(value)


def load_settings(path: Path | None = None) -> Settings:
    configured_path = os.environ.get("HOME_AGENT_CONFIG") or os.environ.get("STRAWBERRY_CONFIG")
    config_path = path or Path(configured_path or DEFAULT_CONFIG_PATH)
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {config_path}: {exc}") from exc

    telegram = _table(data, "telegram")
    agent = _table(data, "agent")
    heartbeat = _table(data, "heartbeat")
    backup = _table(data, "backup")

    owner_id = telegram.get("owner_id")
    if not isinstance(owner_id, int) or owner_id <= 0:
        raise ConfigError("telegram.owner_id must be a positive numeric Telegram user ID")

    defaults = Settings(telegram_owner_id=owner_id)
    settings = Settings(
        telegram_owner_id=owner_id,
        workspace=_path(agent.get("workspace"), defaults.workspace, "agent.workspace"),
        data_dir=_path(agent.get("data_dir"), defaults.data_dir, "agent.data_dir"),
        database_path=_path(
            agent.get("database_path"), defaults.database_path, "agent.database_path"
        ),
        durable_dir=_path(agent.get("durable_dir"), defaults.durable_dir, "agent.durable_dir"),
        codex_home=_path(agent.get("codex_home"), defaults.codex_home, "agent.codex_home"),
        telegram_token_file=_path(
            telegram.get("token_file"), defaults.telegram_token_file, "telegram.token_file"
        ),
        backup_checkout=_path(backup.get("checkout"), defaults.backup_checkout, "backup.checkout"),
        max_input_chars=int(agent.get("max_input_chars", defaults.max_input_chars)),
        max_queue=int(agent.get("max_queue", defaults.max_queue)),
        turn_timeout_seconds=int(agent.get("turn_timeout_seconds", defaults.turn_timeout_seconds)),
        worker_poll_seconds=float(agent.get("worker_poll_seconds", defaults.worker_poll_seconds)),
        model=cast(str, agent.get("model") or defaults.model),
        reasoning_effort=cast(
            str, agent.get("reasoning_effort") or defaults.reasoning_effort
        ),
        disk_warning_percent=float(
            heartbeat.get("disk_warning_percent", defaults.disk_warning_percent)
        ),
        disk_critical_percent=float(
            heartbeat.get("disk_critical_percent", defaults.disk_critical_percent)
        ),
        memory_available_warning_percent=float(
            heartbeat.get(
                "memory_available_warning_percent", defaults.memory_available_warning_percent
            )
        ),
        load_per_cpu_warning=float(
            heartbeat.get("load_per_cpu_warning", defaults.load_per_cpu_warning)
        ),
        cpu_temperature_warning_c=float(
            heartbeat.get("cpu_temperature_warning_c", defaults.cpu_temperature_warning_c)
        ),
        heartbeat_repeat_seconds=int(
            heartbeat.get("repeat_alert_seconds", defaults.heartbeat_repeat_seconds)
        ),
    )
    if settings.max_queue < 1 or settings.max_input_chars < 1:
        raise ConfigError("agent queue and input limits must be positive")
    if settings.turn_timeout_seconds < 30:
        raise ConfigError("agent.turn_timeout_seconds must be at least 30")
    if not isinstance(settings.model, str) or not settings.model:
        raise ConfigError("agent.model must be a non-empty string")
    if (
        not isinstance(settings.reasoning_effort, str)
        or settings.reasoning_effort not in REASONING_EFFORTS
    ):
        choices = ", ".join(sorted(REASONING_EFFORTS))
        raise ConfigError(f"agent.reasoning_effort must be one of: {choices}")
    return settings
