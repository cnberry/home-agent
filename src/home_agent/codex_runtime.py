from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openai_codex import ApprovalMode, AsyncCodex, AsyncTurnHandle, CodexConfig, Sandbox
from openai_codex.types import ReasoningEffort, TurnStatus

LOGGER = logging.getLogger(__name__)

TRANSIENT_ERROR_MARKERS = (
    "connection",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "rate limit",
    "too many requests",
    "http 429",
    "http 502",
    "http 503",
    "http 504",
)


DEVELOPER_INSTRUCTIONS = """You are the persistent Codex agent on the Home Agent host.
The Telegram gateway has already authenticated the sole owner. Treat their message as the task.
Follow the repository AGENTS.md instruction chain and keep secrets out of responses and logs.
You run without Codex sandboxing or approval prompts. You start as the dedicated
home-agent Linux user and may use sudo -n to run as root when the owner's task requires it.
Use root only when needed and verify concrete work before reporting success.
"""


def is_transient_error(error: BaseException) -> bool:
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    message = str(error).lower()
    return any(marker in message for marker in TRANSIENT_ERROR_MARKERS)


class CodexRunError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        turn_started: bool,
        authentication: bool = False,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.turn_started = turn_started
        self.authentication = authentication
        self.transient = transient


class CodexInterrupted(CodexRunError):
    pass


@dataclass(frozen=True)
class CodexResult:
    thread_id: str
    response: str


class CodexRuntime:
    def __init__(
        self,
        workspace: Path,
        codex_home: Path,
        *,
        timeout_seconds: int,
        model: str,
        reasoning_effort: str,
    ) -> None:
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(codex_home)
        self.workspace = workspace
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.reasoning_effort = ReasoningEffort(reasoning_effort)
        self._codex = AsyncCodex(
            CodexConfig(
                cwd=str(workspace),
                env=environment,
                client_name="home_agent",
                client_title="Home Agent Telegram",
            )
        )
        self._active_handle: AsyncTurnHandle | None = None
        self._active_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._codex.close()

    async def authenticated(self, *, refresh: bool = False) -> bool:
        try:
            account = await self._codex.account(refresh_token=refresh)
        except Exception as exc:
            raise CodexRunError(
                f"Codex authentication check failed: {exc}",
                turn_started=False,
                transient=is_transient_error(exc),
            ) from exc
        return account.account is not None or not account.requires_openai_auth

    async def run(
        self,
        prompt: str,
        *,
        thread_id: str | None,
        on_thread: Callable[[str], None],
        on_turn_started: Callable[[], None],
    ) -> CodexResult:
        if not await self.authenticated(refresh=True):
            raise CodexRunError(
                "Codex is not authenticated; run agentctl auth on the Home Agent host",
                turn_started=False,
                authentication=True,
            )

        thread = None
        if thread_id:
            try:
                thread = await self._codex.thread_resume(
                    thread_id,
                    approval_mode=ApprovalMode.deny_all,
                    cwd=str(self.workspace),
                    developer_instructions=DEVELOPER_INSTRUCTIONS,
                    model=self.model,
                    sandbox=Sandbox.full_access,
                )
            except Exception:
                LOGGER.warning("stored Codex thread could not be resumed; starting a new thread")
        if thread is None:
            try:
                thread = await self._codex.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    cwd=str(self.workspace),
                    developer_instructions=DEVELOPER_INSTRUCTIONS,
                    model=self.model,
                    sandbox=Sandbox.full_access,
                    service_name="home-agent",
                )
            except Exception as exc:
                raise CodexRunError(
                    str(exc),
                    turn_started=False,
                    transient=is_transient_error(exc),
                ) from exc

        on_thread(thread.id)
        try:
            handle = await thread.turn(
                prompt,
                approval_mode=ApprovalMode.deny_all,
                cwd=str(self.workspace),
                effort=self.reasoning_effort,
                model=self.model,
                sandbox=Sandbox.full_access,
            )
        except Exception as exc:
            raise CodexRunError(
                str(exc),
                turn_started=False,
                transient=is_transient_error(exc),
            ) from exc

        async with self._active_lock:
            self._active_handle = handle

        try:
            on_turn_started()
        except Exception as exc:
            await handle.interrupt()
            async with self._active_lock:
                if self._active_handle is handle:
                    self._active_handle = None
            if isinstance(exc, CodexRunError):
                raise
            raise CodexRunError(str(exc), turn_started=True) from exc

        run_task = asyncio.create_task(handle.run())
        try:
            result = await asyncio.wait_for(asyncio.shield(run_task), self.timeout_seconds)
        except TimeoutError as exc:
            await handle.interrupt()
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            raise CodexInterrupted(
                f"Codex turn exceeded {self.timeout_seconds} seconds",
                turn_started=True,
            ) from exc
        except asyncio.CancelledError:
            await handle.interrupt()
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            raise
        except Exception as exc:
            raise CodexRunError(str(exc), turn_started=True) from exc
        finally:
            async with self._active_lock:
                if self._active_handle is handle:
                    self._active_handle = None

        if result.status == TurnStatus.interrupted:
            raise CodexInterrupted("Codex turn was interrupted", turn_started=True)
        if result.status != TurnStatus.completed:
            detail = result.error.message if result.error is not None else str(result.status.value)
            raise CodexRunError(detail, turn_started=True)
        response = (result.final_response or "").strip()
        if not response:
            response = "Codex completed the turn without a final text response."
        return CodexResult(thread_id=thread.id, response=response)

    async def interrupt(self) -> bool:
        async with self._active_lock:
            handle = self._active_handle
        if handle is None:
            return False
        await handle.interrupt()
        return True

    async def archive(self, thread_id: str) -> None:
        await self._codex.thread_archive(thread_id)
