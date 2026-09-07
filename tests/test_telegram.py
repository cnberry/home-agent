from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from telegram import Chat, Message, Update, User

from home_agent.config import Settings
from home_agent.database import Database
from home_agent.telegram_gateway import TelegramGateway, split_text


def gateway(settings: Settings) -> TelegramGateway:
    value = TelegramGateway.__new__(TelegramGateway)
    value.settings = settings
    return value


def update_for(
    user_id: int,
    *,
    chat_type: str = Chat.PRIVATE,
    edited: bool = False,
) -> Update:
    user = User(id=user_id, first_name="Owner", is_bot=False)
    chat_id = user_id if chat_type == Chat.PRIVATE else -1001
    chat = Chat(id=chat_id, type=chat_type)
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text="hello",
    )
    if edited:
        return Update(update_id=1, edited_message=message)
    return Update(update_id=1, message=message)


def test_authorization_requires_exact_owner_private_unedited(settings: Settings) -> None:
    value = gateway(settings)
    assert value.authorized(update_for(settings.telegram_owner_id))
    assert not value.authorized(update_for(settings.telegram_owner_id + 1))
    assert not value.authorized(update_for(settings.telegram_owner_id, chat_type=Chat.GROUP))
    assert not value.authorized(update_for(settings.telegram_owner_id, edited=True))


def test_authorization_rejects_bots_and_forwarded_messages(settings: Settings) -> None:
    value = gateway(settings)
    original = update_for(settings.telegram_owner_id)
    bot_user = User(id=settings.telegram_owner_id, first_name="Bot", is_bot=True)
    bot_message = Message(
        message_id=2,
        date=datetime.now(timezone.utc),
        chat=original.effective_chat,
        from_user=bot_user,
        text="hello",
    )
    assert not value.authorized(Update(update_id=2, message=bot_message))

    forwarded = SimpleNamespace(
        effective_user=original.effective_user,
        effective_chat=original.effective_chat,
        effective_message=SimpleNamespace(forward_origin=object()),
        edited_message=None,
        edited_channel_post=None,
    )
    assert not value.authorized(forwarded)  # type: ignore[arg-type]


def test_message_chunking_respects_limit_and_content() -> None:
    text = ("alpha beta gamma\n" * 100).strip()
    chunks = split_text(text, limit=80)
    assert all(0 < len(chunk) <= 80 for chunk in chunks)
    assert " ".join(" ".join(chunks).split()) == " ".join(text.split())


class FakeMessage:
    def __init__(self, text: str | None = None, message_id: int = 10) -> None:
        self.text = text
        self.message_id = message_id
        self.forward_origin = None
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> Any:
        self.replies.append(text)
        return SimpleNamespace(message_id=100 + len(self.replies))


class FakeUpdate:
    def __init__(self, settings: Settings, update_id: int, message: FakeMessage) -> None:
        self.update_id = update_id
        self.effective_user = SimpleNamespace(
            id=settings.telegram_owner_id,
            is_bot=False,
        )
        self.effective_chat = SimpleNamespace(
            id=settings.telegram_owner_id,
            type=Chat.PRIVATE,
        )
        self.effective_message = message
        self.edited_message = None
        self.edited_channel_post = None


class FakeWorker:
    def __init__(self) -> None:
        self.interrupt_result = True
        self.archive_result = True

    async def interrupt(self) -> bool:
        return self.interrupt_result

    async def archive_telegram_thread(self) -> bool:
        return self.archive_result


def command_gateway(settings: Settings) -> TelegramGateway:
    value = gateway(settings)
    value.database = Database(settings.database_path, settings.max_queue)
    value.database.initialize()
    value.worker = FakeWorker()  # type: ignore[assignment]
    return value


@pytest.mark.asyncio
async def test_text_input_acknowledgement_and_update_deduplication(settings: Settings) -> None:
    value = command_gateway(settings)
    message = FakeMessage("do something")
    update = FakeUpdate(settings, 50, message)
    await value.text_message(update, SimpleNamespace())  # type: ignore[arg-type]
    assert message.replies == ["Queued #1."]
    assert value.database.get_job(1).ack_message_id == 101  # type: ignore[union-attr]

    await value.text_message(update, SimpleNamespace())  # type: ignore[arg-type]
    assert message.replies == ["Queued #1."]


@pytest.mark.asyncio
async def test_input_limit_and_unsupported_content(settings: Settings) -> None:
    value = command_gateway(settings)
    oversized = FakeMessage("x" * (settings.max_input_chars + 1))
    await value.text_message(FakeUpdate(settings, 60, oversized), SimpleNamespace())  # type: ignore[arg-type]
    assert "too long" in oversized.replies[0]

    attachment = FakeMessage()
    await value.unsupported_message(
        FakeUpdate(settings, 61, attachment),
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert attachment.replies == ["Version 1 accepts text messages and commands only."]


@pytest.mark.asyncio
async def test_stop_new_and_retry_commands(settings: Settings) -> None:
    value = command_gateway(settings)

    stop_message = FakeMessage()
    await value.stop_command(
        FakeUpdate(settings, 70, stop_message),
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert stop_message.replies == ["Interrupt requested for the active job."]

    new_message = FakeMessage()
    await value.new_command(
        FakeUpdate(settings, 71, new_message),
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert new_message.replies == ["A fresh Codex conversation will start next."]

    failed = value.database.enqueue("telegram", "failed work")
    assert failed is not None
    value.database.finish(failed.id, "uncertain", "lost connection")
    retry_message = FakeMessage()
    await value.retry_command(
        FakeUpdate(settings, 72, retry_message),
        SimpleNamespace(args=[str(failed.id)]),  # type: ignore[arg-type]
    )
    assert retry_message.replies == [f"Requeued #{failed.id}."]
