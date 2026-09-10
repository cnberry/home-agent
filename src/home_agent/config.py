from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    optimization_checkout: Path = Path("/srv/home-agent/development/home-agent")
    optimization_repository: str = "https://github.com/cnberry/home-agent.git"
    optimization_model: str = DEFAULT_MODEL
    optimization_reasoning_effort: str = "high"
    optimization_timeout_seconds: int = 7_200

    @property
    def tasks_file(self) -> Path:
        return self.durable_dir / "tasks.md"

    @property
    def memory_file(self) -> Path:
        return self.durable_dir / "memory.md"

    @property
    def heartbeat_file(self) -> Path:
        return self.durable_dir / "heartbeat.md"

    @property
    def telegram_credential_path(self) -> Path:
        credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
        return (
            Path(credential_dir) / "telegram-token" if credential_dir else self.telegram_token_file
        )

    def read_telegram_token(self) -> str:
        token_path = self.telegram_credential_path
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
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
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise ConfigError(f"{name} must be an absolute path")
    return Path(value)


def _integer(table: dict[str, Any], key: str, default: int, section: str) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{section}.{key} must be an integer")
    return value


def _number(table: dict[str, Any], key: str, default: float, section: str) -> float:
    value = table.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{section}.{key} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ConfigError(f"{section}.{key} must be a finite number") from exc
    if not math.isfinite(number):
        raise ConfigError(f"{section}.{key} must be a finite number")
    return number


def load_settings(path: Path | None = None) -> Settings:
    configured_path = os.environ.get("HOME_AGENT_CONFIG") or os.environ.get("STRAWBERRY_CONFIG")
    config_path = path or Path(configured_path or DEFAULT_CONFIG_PATH)
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read configuration {config_path}: {exc}") from exc

    telegram = _table(data, "telegram")
    agent = _table(data, "agent")
    heartbeat = _table(data, "heartbeat")
    backup = _table(data, "backup")
    optimization = _table(data, "optimization")

    owner_id = telegram.get("owner_id")
    if not isinstance(owner_id, int) or isinstance(owner_id, bool) or owner_id <= 0:
        raise ConfigError("telegram.owner_id must be a positive numeric Telegram user ID")

    defaults = Settings(telegram_owner_id=owner_id)
    settings = Settings(
        telegram_owner_id=owner_id,
        optimization_checkout=_path(
            optimization.get("checkout"), defaults.optimization_checkout, "optimization.checkout"
        ),
        optimization_repository=optimization.get("repository", defaults.optimization_repository),
        optimization_model=optimization.get("model", defaults.optimization_model),
        optimization_reasoning_effort=optimization.get(
            "reasoning_effort", defaults.optimization_reasoning_effort
        ),
        optimization_timeout_seconds=_integer(
            optimization, "timeout_seconds", defaults.optimization_timeout_seconds, "optimization"
        ),
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
        max_input_chars=_integer(agent, "max_input_chars", defaults.max_input_chars, "agent"),
        max_queue=_integer(agent, "max_queue", defaults.max_queue, "agent"),
        turn_timeout_seconds=_integer(
            agent, "turn_timeout_seconds", defaults.turn_timeout_seconds, "agent"
        ),
        worker_poll_seconds=_number(
            agent, "worker_poll_seconds", defaults.worker_poll_seconds, "agent"
        ),
        model=agent.get("model", defaults.model),
        reasoning_effort=agent.get("reasoning_effort", defaults.reasoning_effort),
        disk_warning_percent=_number(
            heartbeat, "disk_warning_percent", defaults.disk_warning_percent, "heartbeat"
        ),
        disk_critical_percent=_number(
            heartbeat, "disk_critical_percent", defaults.disk_critical_percent, "heartbeat"
        ),
        memory_available_warning_percent=_number(
            heartbeat,
            "memory_available_warning_percent",
            defaults.memory_available_warning_percent,
            "heartbeat",
        ),
        load_per_cpu_warning=_number(
            heartbeat, "load_per_cpu_warning", defaults.load_per_cpu_warning, "heartbeat"
        ),
        cpu_temperature_warning_c=_number(
            heartbeat, "cpu_temperature_warning_c", defaults.cpu_temperature_warning_c, "heartbeat"
        ),
        heartbeat_repeat_seconds=_integer(
            heartbeat, "repeat_alert_seconds", defaults.heartbeat_repeat_seconds, "heartbeat"
        ),
    )
    if settings.max_queue < 1 or settings.max_input_chars < 1:
        raise ConfigError("agent queue and input limits must be positive")
    if settings.turn_timeout_seconds < 30:
        raise ConfigError("agent.turn_timeout_seconds must be at least 30")
    if settings.worker_poll_seconds <= 0:
        raise ConfigError("agent.worker_poll_seconds must be positive")
    if not 0 <= settings.disk_warning_percent < settings.disk_critical_percent <= 100:
        raise ConfigError("heartbeat disk thresholds must satisfy 0 <= warning < critical <= 100")
    if not 0 <= settings.memory_available_warning_percent <= 100:
        raise ConfigError("heartbeat.memory_available_warning_percent must be between 0 and 100")
    if (
        settings.load_per_cpu_warning <= 0
        or settings.cpu_temperature_warning_c <= 0
        or settings.heartbeat_repeat_seconds <= 0
    ):
        raise ConfigError("heartbeat load, temperature, and repeat thresholds must be positive")
    if not isinstance(settings.model, str) or not settings.model.strip():
        raise ConfigError("agent.model must be a non-empty string")
    if (
        not isinstance(settings.reasoning_effort, str)
        or settings.reasoning_effort not in REASONING_EFFORTS
    ):
        choices = ", ".join(sorted(REASONING_EFFORTS))
        raise ConfigError(f"agent.reasoning_effort must be one of: {choices}")
    if (
        not isinstance(settings.optimization_repository, str)
        or not settings.optimization_repository
    ):
        raise ConfigError("optimization.repository must be nonempty")
    if not isinstance(settings.optimization_model, str) or not settings.optimization_model.strip():
        raise ConfigError("optimization.model must be nonempty")
    if (
        not isinstance(settings.optimization_reasoning_effort, str)
        or settings.optimization_reasoning_effort not in REASONING_EFFORTS
    ):
        raise ConfigError("optimization.reasoning_effort is invalid")
    if not 30 <= settings.optimization_timeout_seconds <= 10_000:
        raise ConfigError("optimization.timeout_seconds must be between 30 and 10000")
    return settings
