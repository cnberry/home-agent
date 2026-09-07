from __future__ import annotations

from pathlib import Path

import pytest

from home_agent.config import ConfigError, Settings, load_settings


def test_configuration_preserves_explicit_choices(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[telegram]\nowner_id = 123\n[agent]\nmodel = "custom-model"\n'
        'reasoning_effort = "medium"\nworker_poll_seconds = 0.25\n'
        'workspace = "/srv/custom"\nmax_queue = 3\n',
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.telegram_owner_id == 123
    assert settings.model == "custom-model"
    assert settings.reasoning_effort == "medium"
    assert settings.worker_poll_seconds == 0.25
    assert settings.workspace == Path("/srv/custom")
    assert settings.max_queue == 3


@pytest.mark.parametrize(
    "content",
    [
        "[invalid",
        "",
        "telegram = 1",
        "[telegram]\nowner_id = true",
        "[telegram]\nowner_id = 0",
        '[telegram]\nowner_id = "123"',
        *[
            "[telegram]\nowner_id = 123\n[agent]\n" + setting
            for setting in (
                'max_queue = "2"',
                "max_queue = false",
                "max_queue = 1.5",
                "max_queue = 0",
                "max_input_chars = 0",
                "turn_timeout_seconds = 10",
                "worker_poll_seconds = 0",
                "worker_poll_seconds = nan",
                'workspace = "relative"',
                'model = " "',
                'reasoning_effort = "unsupported"',
            )
        ],
        *[
            "[telegram]\nowner_id = 123\n[heartbeat]\n" + setting
            for setting in (
                "disk_warning_percent = 99\ndisk_critical_percent = 90",
                "disk_critical_percent = 101",
                "memory_available_warning_percent = -1",
                "load_per_cpu_warning = inf",
                "repeat_alert_seconds = 0",
            )
        ],
    ],
)
def test_invalid_configuration_has_one_public_error_type(tmp_path: Path, content: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(path)


def test_systemd_credential_takes_precedence(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    settings.telegram_token_file.write_text("123:configured\n", encoding="utf-8")
    token = credentials / "telegram-token"
    token.write_text("456:systemd\n", encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    assert settings.read_telegram_token() == "456:systemd"
    token.unlink()
    with pytest.raises(ConfigError):
        settings.read_telegram_token()
    monkeypatch.delenv("CREDENTIALS_DIRECTORY")
    assert settings.read_telegram_token() == "123:configured"
