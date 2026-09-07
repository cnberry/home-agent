from __future__ import annotations

from pathlib import Path

import pytest

from home_agent.config import ConfigError, load_settings


def write_config(path: Path, agent_lines: str = "") -> None:
    path.write_text(
        "[telegram]\n"
        "owner_id = 123456789\n"
        "\n"
        "[agent]\n"
        f"{agent_lines}"
        "\n"
        "[heartbeat]\n"
        "\n"
        "[backup]\n",
        encoding="utf-8",
    )


def test_fast_cost_sensitive_defaults(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    write_config(config)

    settings = load_settings(config)

    assert settings.model == "gpt-5.6-luna"
    assert settings.reasoning_effort == "low"


def test_model_and_reasoning_effort_are_configurable(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    write_config(config, 'model = "gpt-5.6-terra"\nreasoning_effort = "medium"\n')

    settings = load_settings(config)

    assert settings.model == "gpt-5.6-terra"
    assert settings.reasoning_effort == "medium"


def test_invalid_reasoning_effort_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    write_config(config, 'reasoning_effort = "slow"\n')

    with pytest.raises(ConfigError, match="reasoning_effort"):
        load_settings(config)
