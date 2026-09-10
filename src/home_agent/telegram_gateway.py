from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from telegram import Bot, Chat, Message, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from home_agent.codex_runtime import CodexRuntime
from home_agent.config import Settings
from home_agent.database import Database, Job, QueueFullError
from home_agent.health import enqueue_heartbeat
from home_agent.worker import AgentRuntime, Worker, safe_error

LOGGER = logging.getLogger(__name__)
TELEGRAM_CHUNK = 3_900


def split_text(text: str, limit: int = TELEGRAM_CHUNK) -> list[str]:
    value = text.strip() or "(empty response)"
    chunks: list[str] = []
    while len(value) > limit:
        split_at = value.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = value.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(value[:split_at].rstrip())
        value = value[split_at:].lstrip()
    chunks.append(value)
    return chunks


class TelegramNotifier:
    def __init__(self, bot: Bot, owner_id: int, database: Database) -> None:
        self.bot = bot
        self.database = database
        self.owner_id = owner_id

    async def _deliver(self, job: Job, method: str, **kwargs: Any) -> Any:
        self.database.record_event("delivery_attempt", job_id=job.id, details=kwargs)
        try:
            result = await getattr(self.bot, method)(**kwargs)
        except Exception as exc:
            self.database.record_event(
                "delivery_failed",
                job_id=job.id,
                details={"error": safe_error(exc), "method": method},
            )
            raise
        self.database.record_event("delivered", job_id=job.id, details={"method": method, **kwargs})
        return result

    async def _send_or_edit(self, job: Job, text: str) -> None:
        chunks = split_text(text)
        chat_id = job.telegram_chat_id or self.owner_id
        if job.ack_message_id and job.kind == "telegram":
            try:
                await self._deliver(
                    job,
                    "edit_message_text",
                    chat_id=chat_id,
                    message_id=job.ack_message_id,
                    text=chunks[0],
                )
            except Exception as exc:
                LOGGER.warning("could not edit acknowledgement: %s", safe_error(exc))
            else:
                chunks = chunks[1:]
        for chunk in chunks:
            await self._deliver(job, "send_message", chat_id=chat_id, text=chunk)

    async def working(self, job: Job) -> None:
        if job.kind == "telegram" and job.ack_message_id:
            await self._deliver(
                job,
                "edit_message_text",
                chat_id=job.telegram_chat_id or self.owner_id,
                message_id=job.ack_message_id,
                text=f"Working on #{job.id}…",
            )

    async def completed(self, job: Job, response: str) -> None:
        await self._send_or_edit(job, response)

    async def failed(self, job: Job, message: str) -> None:
        await self._send_or_edit(job, f"Job #{job.id}: {message}")

    async def retrying(self, job: Job, delay_seconds: int) -> None:
        if job.kind == "telegram" and job.ack_message_id:
            await self._deliver(
                job,
                "edit_message_text",
                chat_id=job.telegram_chat_id or self.owner_id,
                message_id=job.ack_message_id,
                text=f"Job #{job.id} hit a transient error; retrying in {delay_seconds}s.",
            )


class TelegramGateway:
    def __init__(
        self,
        settings: Settings,
        token: str,
        *,
        application: Application[Any, Any, Any, Any, Any, Any] | None = None,
        codex: AgentRuntime | None = None,
    ) -> None:
        self.settings = settings
        self.database = Database(settings.database_path, settings.max_queue)
        self.database.initialize()
        self.codex: AgentRuntime = codex or CodexRuntime(
            settings.workspace,
            settings.codex_home,
            timeout_seconds=settings.turn_timeout_seconds,
            model=settings.model,
            reasoning_effort=settings.reasoning_effort,
        )
        self.application: Application[Any, Any, Any, Any, Any, Any] = (
            application or ApplicationBuilder().token(token).build()
        )
        self.notifier = TelegramNotifier(
            self.application.bot, settings.telegram_owner_id, self.database
        )
        self.worker = Worker(
            self.database,
            self.codex,
            self.notifier,
            poll_seconds=settings.worker_poll_seconds,
        )
        self.worker_task: asyncio.Task[None] | None = None
        self._register_handlers()
        self.application.post_init = self._post_init
        self.application.post_stop = self._post_stop
        self.application.post_shutdown = self._post_shutdown

    def _register_handlers(self) -> None:
        for command in ("start", "help"):
            self.application.add_handler(CommandHandler(command, self.help_command))
        self.application.add_handler(CommandHandler("new", self.new_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("stop", self.stop_command))
        self.application.add_handler(CommandHandler("heartbeat", self.heartbeat_command))
        self.application.add_handler(CommandHandler("retry", self.retry_command))
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message)
        )
        self.application.add_handler(MessageHandler(filters.ALL, self.unsupported_message))
        self.application.add_error_handler(self.error_handler)

    def authorized(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        message = update.effective_message
        return bool(
            user
            and chat
            and message
            and update.message is not None
            and not user.is_bot
            and user.id == self.settings.telegram_owner_id
            and chat.type == Chat.PRIVATE
            and chat.id == self.settings.telegram_owner_id
            and update.edited_message is None
            and update.edited_channel_post is None
            and message.forward_origin is None
        )

    def _record_received(self, update: Update) -> None:
        message = update.effective_message
        self.database.record_event(
            "received",
            update_id=update.update_id,
            details={
                "text": message.text if message else None,
                "sent_at": message.date.isoformat() if message else None,
            },
        )

    def first_seen(self, update: Update) -> bool:
        self._record_received(update)
        return update.update_id is not None and self.database.record_update(update.update_id)

    async def _reply(self, update: Update, text: str) -> Message:
        message = update.effective_message
        assert message is not None
        self.database.record_event(
            "reply_attempt", update_id=update.update_id, details={"text": text}
        )
        try:
            result = await message.reply_text(text)
        except Exception as exc:
            self.database.record_event(
                "reply_failed", update_id=update.update_id, details={"error": safe_error(exc)}
            )
            raise
        self.database.record_event(
            "reply_delivered",
            update_id=update.update_id,
            details={"text": text, "message_id": result.message_id},
        )
        return result

    async def _post_init(self, _: Application[Any, Any, Any, Any, Any, Any]) -> None:
        interrupted = self.database.recover_interrupted()
        for job in interrupted:
            try:
                await self.notifier.failed(
                    job,
                    "Uncertain: Home Agent restarted while this job was running. "
                    "Use /retry if safe.",
                )
            except Exception as exc:
                LOGGER.warning("restart notification failed: %s", safe_error(exc))
        self.worker_task = asyncio.create_task(self.worker.run(), name="home-agent-worker")
        self.worker_task.add_done_callback(self._worker_done)

    def _worker_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            LOGGER.error("Home Agent worker stopped: %s", safe_error(error))
            self.application.stop_running()

    async def _post_stop(self, _: Application[Any, Any, Any, Any, Any, Any]) -> None:
        await self.worker.stop()
        if self.worker_task:
            await asyncio.gather(self.worker_task, return_exceptions=True)

    async def _post_shutdown(self, _: Application[Any, Any, Any, Any, Any, Any]) -> None:
        await self.codex.close()

    async def help_command(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if (
            not self.authorized(update)
            or not self.first_seen(update)
            or not update.effective_message
        ):
            return
        await self._reply(
            update,
            "Home Agent runs Codex with full system access and passwordless sudo.\n\n"
            "Send text to queue a task.\n"
            "/new — start a fresh Codex conversation\n"
            "/status — show queue and service state\n"
            "/stop — interrupt the active turn\n"
            "/heartbeat — queue an immediate heartbeat\n"
            "/retry <id> — retry a failed or uncertain job",
        )

    async def text_message(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update) or not update.effective_message:
            return
        self._record_received(update)
        text = update.effective_message.text or ""
        if len(text) > self.settings.max_input_chars:
            await self._reply(
                update, f"Message is too long; limit is {self.settings.max_input_chars} characters."
            )
            return
        try:
            job = self.database.enqueue(
                "telegram",
                text,
                telegram_update_id=update.update_id,
                telegram_chat_id=update.effective_chat.id if update.effective_chat else None,
                telegram_message_id=update.effective_message.message_id,
            )
        except QueueFullError:
            await self._reply(update, "Queue is full; use /status or /stop first.")
            return
        if job is None:
            return
        acknowledgement = await self._reply(update, f"Queued #{job.id}.")
        self.database.set_ack_message(job.id, acknowledgement.message_id)

    async def new_command(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if (
            not self.authorized(update)
            or not self.first_seen(update)
            or not update.effective_message
        ):
            return
        if await self.worker.archive_telegram_thread():
            await self._reply(update, "A fresh Codex conversation will start next.")
        else:
            await self._reply(update, "Work is queued or active; use /stop or wait before /new.")

    async def stop_command(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if (
            not self.authorized(update)
            or not self.first_seen(update)
            or not update.effective_message
        ):
            return
        if await self.worker.interrupt():
            await self._reply(update, "Interrupt requested for the active job.")
        else:
            await self._reply(update, "No active Codex turn.")

    async def heartbeat_command(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if (
            not self.authorized(update)
            or not self.first_seen(update)
            or not update.effective_message
        ):
            return
        try:
            job = enqueue_heartbeat(self.settings, self.database, force=True)
        except QueueFullError:
            await self._reply(update, "Queue is full; heartbeat was not queued.")
            return
        text = f"Heartbeat queued as #{job.id}." if job else "A heartbeat is already queued."
        await self._reply(update, text)

    async def retry_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if (
            not self.authorized(update)
            or not self.first_seen(update)
            or not update.effective_message
        ):
            return
        arguments = context.args or []
        if len(arguments) != 1 or not arguments[0].isdigit():
            await self._reply(update, "Usage: /retry <job-id>")
            return
        try:
            job = self.database.retry(int(arguments[0]))
        except QueueFullError:
            await self._reply(update, "Queue is full; retry was not queued.")
            return
        if job:
            acknowledgement = await self._reply(update, f"Requeued #{job.id}.")
            self.database.set_ack_message(job.id, acknowledgement.message_id)
        else:
            await self._reply(update, "Job is not retryable or does not exist.")

    async def status_command(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if (
            not self.authorized(update)
            or not self.first_seen(update)
            or not update.effective_message
        ):
            return
        snapshot = self.database.snapshot()
        active = "none"
        if snapshot.active:
            started = datetime.fromisoformat(
                snapshot.active.started_at or snapshot.active.created_at
            )
            elapsed = int((datetime.now(timezone.utc) - started).total_seconds())
            active = f"#{snapshot.active.id} ({snapshot.active.kind}, {elapsed}s)"
        degraded = bool(self.database.get_metadata("authentication_degraded"))
        await self._reply(
            update,
            f"Active: {active}\n"
            f"Queued: {snapshot.queued}\n"
            f"Last success: {snapshot.last_completed_at or 'never'}\n"
            f"Last heartbeat: {snapshot.last_heartbeat_at or 'never'}\n"
            f"Model: {self.settings.model} ({self.settings.reasoning_effort} reasoning)\n"
            f"Authentication: {'needs attention' if degraded else 'ok'}",
        )

    async def unsupported_message(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if (
            not self.authorized(update)
            or not self.first_seen(update)
            or not update.effective_message
        ):
            return
        await self._reply(update, "Version 1 accepts text messages and commands only.")

    async def error_handler(self, _: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOGGER.error("Telegram update failed: %s", safe_error(context.error or "unknown error"))

    def run(self) -> None:
        self.application.run_polling(drop_pending_updates=False)
