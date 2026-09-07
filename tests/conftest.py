from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from home_agent.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    from home_agent.config import Settings

    workspace = tmp_path / "workspace"
    data = tmp_path / "data"
    durable = data / "durable"
    codex_home = data / "codex-home"
    for directory in (workspace, durable, codex_home):
        directory.mkdir(parents=True)
    (durable / "tasks.md").write_text("# Tasks\n", encoding="utf-8")
    (durable / "memory.md").write_text("# Memory\n", encoding="utf-8")
    (durable / "heartbeat.md").write_text("# Heartbeat\n", encoding="utf-8")
    return Settings(
        telegram_owner_id=123456789,
        workspace=workspace,
        data_dir=data,
        database_path=data / "runtime" / "runtime.sqlite3",
        durable_dir=durable,
        codex_home=codex_home,
        telegram_token_file=tmp_path / "telegram-token",
        backup_checkout=tmp_path / "backup",
        worker_poll_seconds=0.01,
    )
