from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openai_codex import ApprovalMode, AsyncCodex, AsyncTurnHandle, CodexConfig, Sandbox
from openai_codex.errors import ServerBusyError, TransportClosedError
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
    if isinstance(error, (ConnectionError, TimeoutError, ServerBusyError, TransportClosedError)):
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
        developer_instructions: str = DEVELOPER_INSTRUCTIONS,
        client_title: str = "Home Agent Telegram",
    ) -> None:
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(codex_home)
        self.developer_instructions = developer_instructions
        self.workspace = workspace
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.reasoning_effort = ReasoningEffort(reasoning_effort)
        self._codex = AsyncCodex(
            CodexConfig(
                cwd=str(workspace),
                env=environment,
                client_name="home_agent",
                client_title=client_title,
            )
        )
        self._active_handle: AsyncTurnHandle | None = None
        self._running = False
        self._interrupt_requested = False

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
        turn_started = False

        def before_dispatch() -> None:
            nonlocal turn_started
            # A lost dispatch response does not prove that the server rejected
            # work. Persist the no-replay boundary before sending the turn.
            on_turn_started()
            turn_started = True

        self._running = True
        self._interrupt_requested = False
        try:
            return await asyncio.wait_for(
                self._run(prompt, thread_id, on_thread, before_dispatch),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            await self._interrupt_after_failure()
            raise CodexInterrupted(
                f"Codex turn exceeded {self.timeout_seconds} seconds",
                turn_started=turn_started,
            ) from exc
        except asyncio.CancelledError:
            await self._interrupt_after_failure()
            raise
        except CodexRunError:
            raise
        except Exception as exc:
            await self._interrupt_after_failure()
            raise CodexRunError(
                str(exc),
                turn_started=turn_started,
                transient=not turn_started and is_transient_error(exc),
            ) from exc
        finally:
            self._active_handle = None
            self._running = False

    async def _run(
        self,
        prompt: str,
        thread_id: str | None,
        on_thread: Callable[[str], None],
        before_dispatch: Callable[[], None],
    ) -> CodexResult:
        if not await self.authenticated(refresh=True):
            raise CodexRunError(
                "Codex is not authenticated; run agentctl auth on the Home Agent host",
                turn_started=False,
                authentication=True,
            )
        if thread_id:
            thread = await self._codex.thread_resume(
                thread_id,
                approval_mode=ApprovalMode.deny_all,
                cwd=str(self.workspace),
                developer_instructions=self.developer_instructions,
                model=self.model,
                sandbox=Sandbox.full_access,
            )
        else:
            thread = await self._codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=str(self.workspace),
                developer_instructions=self.developer_instructions,
                model=self.model,
                sandbox=Sandbox.full_access,
                service_name="home-agent",
            )

        on_thread(thread.id)
        if self._interrupt_requested:
            raise CodexInterrupted("Codex turn was interrupted", turn_started=False)
        before_dispatch()
        handle = await thread.turn(
            prompt,
            approval_mode=ApprovalMode.deny_all,
            cwd=str(self.workspace),
            effort=self.reasoning_effort,
            model=self.model,
            sandbox=Sandbox.full_access,
        )
        self._active_handle = handle
        if self._interrupt_requested:
            await handle.interrupt()
        result = await handle.run()
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
        if not self._running:
            return False
        self._interrupt_requested = True
        if self._active_handle is not None:
            await asyncio.wait_for(self._active_handle.interrupt(), timeout=5)
        return True

    async def _interrupt_after_failure(self) -> None:
        if self._active_handle is not None:
            try:
                await asyncio.wait_for(self._active_handle.interrupt(), timeout=5)
            except Exception:
                LOGGER.warning("could not confirm interruption of the failed Codex turn")

    async def archive(self, thread_id: str) -> None:
        await self._codex.thread_archive(thread_id)
