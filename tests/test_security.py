from __future__ import annotations

import logging

import pytest

from home_agent.cli import configure_logging
from home_agent.worker import safe_error


@pytest.mark.parametrize(
    "secret",
    [
        "123456789:" + "a" * 35,
        "sk-" + "a" * 40,
        "sk-proj-" + "a" * 80,
        "ghp_" + "a" * 36,
        "github_pat_" + "a" * 80,
    ],
)
def test_diagnostics_preserve_context_without_credentials(secret: str) -> None:
    message = safe_error(RuntimeError(f"Connection failed using {secret}; retry later"))
    assert secret not in message
    assert "Connection failed" in message
    assert "retry later" in message


def test_verbose_logging_does_not_emit_credential_bearing_transport_details(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = ("httpx", "httpcore", "telegram.ext.ExtBot")
    # Restore process-global logger configuration after this scenario.
    for name in names:
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "level", logger.level)
    with caplog.at_level(logging.DEBUG):
        configure_logging(verbose=True)
        for name in names:
            logging.getLogger(name).info("request includes synthetic-credential")
        logging.getLogger("home_agent").info("application remains visible")
    assert "synthetic-credential" not in caplog.text
    assert "application remains visible" in caplog.text
