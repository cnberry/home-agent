from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any

import pytest
import pytest_asyncio
from telegram import Update
from telegram.error import NetworkError
from telegram.ext import Application, ApplicationBuilder
from telegram.request import BaseRequest, RequestData

from home_agent.codex_runtime import CodexInterrupted, CodexResult
from home_agent.config import Settings
from home_agent.telegram_gateway import TelegramGateway


class TelegramAPI(BaseRequest):
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []
        self.fail_edits = False

    @property
    def read_timeout(self) -> float:
        return 5.0

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        **kwargs: Any,
    ) -> tuple[int, bytes]:
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        if endpoint == "getMe":
            result = {"id": 42, "is_bot": True, "first_name": "Home", "username": "home_bot"}
        elif endpoint == "deleteWebhook":
            result = True
        elif endpoint == "getUpdates":
            await asyncio.sleep(0.01)
            result = []
        elif endpoint in {"sendMessage", "editMessageText"}:
            if endpoint == "editMessageText" and self.fail_edits:
                raise NetworkError("acknowledgement no longer exists")
            self.messages.append((endpoint, params))
            result = {
                "message_id": params.get("message_id", len(self.messages)),
                "date": 0,
                "chat": {"id": int(params["chat_id"]), "type": "private"},
                "text": params["text"],
            }
        else:
            raise AssertionError(f"Unexpected Telegram request: {endpoint}")
        return 200, json.dumps({"ok": True, "result": result}).encode()


class Runtime:
    def __init__(self) -> None:
        self.response = "done"
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.archived: list[str] = []
        self.interrupted = False
        self.closed = False

    async def run(
        self,
        prompt: str,
        *,
        thread_id: str | None,
        on_thread: Callable[[str], None],
        on_turn_started: Callable[[], None],
    ) -> CodexResult:
        on_thread(thread_id or "thread-1")
        on_turn_started()
        self.entered.set()
        if self.release:
            await self.release.wait()
        if self.interrupted:
            raise CodexInterrupted("cancelled", turn_started=True)
        return CodexResult(thread_id or "thread-1", self.response)

    async def interrupt(self) -> bool:
        self.interrupted = True
        if self.release:
            self.release.set()
        return True

    async def archive(self, thread_id: str) -> None:
        self.archived.append(thread_id)

    async def close(self) -> None:
        self.closed = True


@pytest_asyncio.fixture
async def gateway(
    settings: Settings,
) -> AsyncIterator[tuple[TelegramGateway, TelegramAPI, Runtime]]:
    api = TelegramAPI()
    application: Application[Any, Any, Any, Any, Any, Any] = (
        ApplicationBuilder().token("123456:fake-token").request(api).build()
    )
    runtime = Runtime()
    value = TelegramGateway(settings, "unused", application=application, codex=runtime)
    async with application:
        yield value, api, runtime


def telegram_update(
    gateway: TelegramGateway,
    text: str | None = "do something",
    *,
    update_id: int = 1,
    rejection: str | None = None,
) -> Update:
    owner_id = gateway.settings.telegram_owner_id
    message: dict[str, Any] = {
        "message_id": update_id,
        "date": 0,
        "chat": {"id": owner_id, "type": "private"},
        "from": {"id": owner_id, "first_name": "Owner", "is_bot": False},
    }
    if text is not None:
        message["text"] = text
        if text.startswith("/"):
            message["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ]
    else:
        message["photo"] = [
            {"file_id": "photo", "file_unique_id": "photo", "width": 1, "height": 1}
        ]
    if rejection == "stranger":
        message["from"]["id"] += 1
    elif rejection == "bot":
        message["from"]["is_bot"] = True
    elif rejection == "group":
        message["chat"] = {"id": -1001, "type": "group", "title": "Group"}
    elif rejection == "wrong_chat":
        message["chat"]["id"] += 1
    elif rejection == "forward":
        message["forward_origin"] = {"type": "hidden_user", "sender_user_name": "Other", "date": 0}
    key = "edited_message" if rejection == "edit" else "message"
    return Update.de_json({"update_id": update_id, key: message}, gateway.application.bot)


async def wait_for_job(gateway: TelegramGateway, job_id: int, status: str) -> None:
    async def wait() -> None:
        while True:
            job = gateway.database.get_job(job_id)
            if job is not None and job.status == status:
                return
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["stranger", "bot", "group", "wrong_chat", "forward", "edit"])
async def test_untrusted_updates_have_no_effect_or_reply(
    gateway: tuple[TelegramGateway, TelegramAPI, Runtime], rejection: str
) -> None:
    value, api, runtime = gateway
    for update_id, text in enumerate(["do something", "/stop", "/new", "/retry 1"], 1):
        await value.application.process_update(
            telegram_update(value, text, update_id=update_id, rejection=rejection)
        )
    assert value.database.snapshot().queued == 0
    assert not api.messages and not runtime.archived and not runtime.interrupted


@pytest.mark.asyncio
async def test_owner_messages_are_queued_once_even_when_the_queue_is_full(
    gateway: tuple[TelegramGateway, TelegramAPI, Runtime],
) -> None:
    value, api, _ = gateway
    update = telegram_update(value)
    await value.application.process_update(update)
    for index in range(value.settings.max_queue - 1):
        value.database.enqueue("telegram", f"other {index}")
    await value.application.process_update(update)
    assert len(api.messages) == 1
    assert api.messages[0][1]["text"] == "Queued #1."
    await value.application.process_update(telegram_update(value, update_id=2))
    assert "Queue is full" in api.messages[-1][1]["text"]
    job = value.database.get_job(1)
    assert job is not None and job.prompt == "do something"


@pytest.mark.asyncio
async def test_oversized_messages_and_attachments_do_not_create_jobs(
    gateway: tuple[TelegramGateway, TelegramAPI, Runtime],
) -> None:
    value, api, _ = gateway
    value.settings = replace(value.settings, max_input_chars=10)
    await value.application.process_update(telegram_update(value, "x" * 11))
    await value.application.process_update(telegram_update(value, None, update_id=2))
    assert value.database.snapshot().queued == 0
    assert "too long" in api.messages[0][1]["text"]
    assert "text messages" in api.messages[1][1]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted_ack", [False, True])
async def test_long_results_reach_owner_even_when_acknowledgement_cannot_be_edited(
    gateway: tuple[TelegramGateway, TelegramAPI, Runtime], deleted_ack: bool
) -> None:
    value, api, runtime = gateway
    runtime.response = "alpha beta gamma\n" * 600
    await value.application.process_update(telegram_update(value))
    api.messages.clear()
    api.fail_edits = deleted_ack
    task = asyncio.create_task(value.worker.run())
    try:
        await wait_for_job(value, 1, "completed")
    finally:
        await value.worker.stop()
        await asyncio.wait_for(task, 2)
    responses = [
        params for _, params in api.messages if not params["text"].startswith("Working on")
    ]
    assert responses
    assert all(params["chat_id"] == value.settings.telegram_owner_id for params in responses)
    assert all(0 < len(params["text"]) <= 4096 for params in responses)
    assert " ".join(" ".join(params["text"] for params in responses).split()) == " ".join(
        runtime.response.split()
    )


@pytest.mark.asyncio
async def test_stop_interrupts_active_work_and_new_waits_for_queue_to_clear(
    gateway: tuple[TelegramGateway, TelegramAPI, Runtime],
) -> None:
    value, api, runtime = gateway
    runtime.release = asyncio.Event()
    await value.application.process_update(telegram_update(value))
    task = asyncio.create_task(value.worker.run())
    try:
        await asyncio.wait_for(runtime.entered.wait(), 2)
        await value.application.process_update(telegram_update(value, "/new", update_id=2))
        assert "queued or active" in api.messages[-1][1]["text"]
        await value.application.process_update(telegram_update(value, "/stop", update_id=3))
        await wait_for_job(value, 1, "cancelled")
        await value.application.process_update(telegram_update(value, "/new", update_id=4))
        assert value.database.get_thread("telegram") is None
        assert runtime.archived == ["thread-1"]
    finally:
        await value.worker.stop()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_retry_requeues_uncertain_work_but_duplicate_command_does_not_repeat_it(
    gateway: tuple[TelegramGateway, TelegramAPI, Runtime],
) -> None:
    value, api, _ = gateway
    job = value.database.enqueue("telegram", "uncertain work")
    assert job is not None
    value.database.finish(job.id, "uncertain", "connection lost")
    update = telegram_update(value, f"/retry {job.id}")
    await value.application.process_update(update)
    await value.application.process_update(update)
    stored = value.database.get_job(job.id)
    assert stored is not None and stored.status == "queued"
    assert len(api.messages) == 1 and "Requeued" in api.messages[0][1]["text"]
    task = asyncio.create_task(value.worker.run())
    try:
        await wait_for_job(value, job.id, "completed")
    finally:
        await value.worker.stop()
        await asyncio.wait_for(task, 2)
    assert api.messages[-1][1]["text"] == "done"


def test_worker_failure_shuts_down_service(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    api = TelegramAPI()
    application: Application[Any, Any, Any, Any, Any, Any] = (
        ApplicationBuilder()
        .token("123456:fake-token")
        .request(api)
        .get_updates_request(api)
        .build()
    )
    runtime = Runtime()
    value = TelegramGateway(settings, "unused", application=application, codex=runtime)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    timed_out = []

    def break_queue() -> None:
        with sqlite3.connect(settings.database_path) as connection:
            connection.execute("DROP TABLE jobs")

    def watchdog() -> None:
        timed_out.append(True)
        application.stop_running()

    loop.call_later(0.05, break_queue)
    timeout = loop.call_later(2, watchdog)
    try:
        value.run()
    finally:
        timeout.cancel()
        asyncio.set_event_loop(None)
        if not loop.is_closed():
            loop.close()
    assert not timed_out, "A dead worker left the service running without processing jobs"
    assert runtime.closed
    assert "Home Agent worker stopped" in caplog.text
