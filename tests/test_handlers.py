"""Весь путь через aiogram на поддельной сессии Telegram, без сети."""
import asyncio
from datetime import datetime, timezone
from itertools import count

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMyCommands,
    EditMessageText,
    GetMe,
    SendMessage,
    SetMyCommands,
)
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

import bot.handlers as handlers
from bot.handlers import AccessMiddleware, build_router
from bot.worker import Worker
from test_units import FakeLLM, make_config

ADMIN = (1, "Boss")
FRIEND = (2, "friend")
STRANGER = (3, "stranger")


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self._ids = count(1000)
        self.reject_html = False

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return TgUser(id=99, is_bot=True, first_name="Bot", username="test_bot")
        if isinstance(method, (SetMyCommands, DeleteMyCommands, AnswerCallbackQuery)):
            return True
        if isinstance(method, (SendMessage, EditMessageText)):
            if self.reject_html and method.parse_mode == "HTML":
                raise TelegramBadRequest(method, "can't parse entities")
            return Message(
                message_id=getattr(method, "message_id", None) or next(self._ids),
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        raise AssertionError(f"Неожиданный вызов {method!r}")

    def texts(self, chat_id):
        return [
            call.text for call in self.calls
            if isinstance(call, (SendMessage, EditMessageText)) and call.chat_id == chat_id
        ]


class Harness:
    def __init__(self, db, tmp_path):
        self.session = FakeSession()
        self.bot = Bot("1:test", session=self.session)
        self.llm = FakeLLM()
        self.db = db
        self.worker = Worker(self.llm, db, make_config(tmp_path))
        self.dispatcher = Dispatcher()
        self.dispatcher.update.outer_middleware(AccessMiddleware(db, self.bot))
        self.dispatcher.include_router(
            build_router(self.bot, db, self.llm, self.worker, make_config(tmp_path))
        )
        self._updates = count(1)
        self._messages = count(1)

    def _message(self, sender, chat_type="private", **fields):
        user_id, username = sender
        return {
            "message_id": next(self._messages),
            "date": 1_700_000_000,
            "chat": {"id": user_id if chat_type == "private" else -500, "type": chat_type},
            "from": {"id": user_id, "is_bot": False, "first_name": "N", "username": username},
            **fields,
        }

    async def send(self, sender, text, chat_type="private"):
        data = {"update_id": next(self._updates),
                "message": self._message(sender, chat_type, text=text)}
        await self.dispatcher.feed_update(
            self.bot, Update.model_validate(data, context={"bot": self.bot})
        )

    async def click(self, sender, data):
        user_id, username = sender
        payload = {
            "update_id": next(self._updates),
            "callback_query": {
                "id": "1",
                "from": {"id": user_id, "is_bot": False, "first_name": "N", "username": username},
                "chat_instance": "x",
                "data": data,
                "message": self._message((99, "test_bot"), text="выбор") | {
                    "chat": {"id": user_id, "type": "private"}
                },
            },
        }
        await self.dispatcher.feed_update(
            self.bot, Update.model_validate(payload, context={"bot": self.bot})
        )


@pytest.fixture
async def rig(db, tmp_path, monkeypatch):
    monkeypatch.setattr(handlers, "COLLECT_DELAY", 0.01)
    await db.ensure_admin("boss")
    harness = Harness(db, tmp_path)
    task = asyncio.create_task(harness.worker.run())
    yield harness
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await harness.bot.session.close()


async def wait_for_text(harness, chat_id, fragment):
    async with asyncio.timeout(3):
        while not any(fragment in text for text in harness.session.texts(chat_id)):
            await asyncio.sleep(0.01)


async def test_stranger_gets_complete_silence(rig):
    for text in ("/start", "привет", "/setmodel", "/giveaccess @me"):
        await rig.send(STRANGER, text)
    await asyncio.sleep(0.05)
    assert rig.session.calls == []
    assert rig.llm.requests == []


async def test_group_chats_are_ignored_even_for_admin(rig):
    await rig.send(ADMIN, "/start", chat_type="group")
    assert rig.session.calls == []


async def test_giveaccess_then_user_gets_answer(rig):
    await rig.send(FRIEND, "привет")
    assert rig.session.texts(FRIEND[0]) == []

    await rig.send(ADMIN, "/giveaccess @Friend")
    assert any("Доступ выдан: @friend" in t for t in rig.session.texts(ADMIN[0]))

    await rig.send(FRIEND, "как дела?")
    await wait_for_text(rig, FRIEND[0], "ответ готов")
    menu = [c for c in rig.session.calls if isinstance(c, SetMyCommands)]
    assert {c.command for c in menu[-1].commands} == {"new", "stop", "help"}


async def test_admin_commands_are_closed_for_users(rig):
    await rig.send(ADMIN, "/giveaccess friend")
    await rig.send(FRIEND, "/setmodel")
    await rig.send(FRIEND, "/giveaccess @stranger")
    await rig.send(FRIEND, "/users")
    assert (await rig.db.find("stranger")) is None
    # Админские команды для обычного пользователя — неизвестные, бот на них молчит.
    assert rig.session.texts(FRIEND[0]) == []
    assert rig.llm.requests == []


async def test_setmodel_callback_rejected_for_user(rig):
    await rig.send(ADMIN, "/giveaccess friend")
    await rig.send(FRIEND, "/start")
    await rig.click(FRIEND, "setmodel:0")
    assert await rig.db.get_setting("active_model") is None


async def test_setmodel_by_button(rig):
    await rig.send(ADMIN, "/setmodel")
    keyboard = next(
        c for c in rig.session.calls if isinstance(c, SendMessage) and c.reply_markup
    ).reply_markup
    assert keyboard.inline_keyboard[0][0].text == "✅ Model A"

    await rig.click(ADMIN, "setmodel:0")
    assert await rig.db.get_setting("active_model") == "a"
    await wait_for_text(rig, ADMIN[0], "Выбрана модель: Model A")


async def test_revoke_returns_silence(rig):
    await rig.send(ADMIN, "/giveaccess friend")
    await rig.send(FRIEND, "/start")
    await rig.send(ADMIN, "/revokeaccess @friend")
    before = len(rig.session.calls)
    await rig.send(FRIEND, "эй")
    await asyncio.sleep(0.05)
    assert len(rig.session.calls) == before


async def test_new_chat_forgets_context(rig):
    await rig.send(ADMIN, "запомни число 7")
    await wait_for_text(rig, ADMIN[0], "ответ готов")
    await rig.send(ADMIN, "/new")
    await wait_for_text(rig, ADMIN[0], "Новый чат начат")
    await rig.send(ADMIN, "какое число?")
    async with asyncio.timeout(3):
        while len(rig.llm.requests) < 2:
            await asyncio.sleep(0.01)
    assert "запомни" not in str(rig.llm.requests[-1])


async def test_split_messages_are_joined_into_one_request(rig, monkeypatch):
    monkeypatch.setattr(handlers, "COLLECT_DELAY", 0.2)
    await rig.send(ADMIN, "первая часть")
    await rig.send(ADMIN, "вторая часть")
    await wait_for_text(rig, ADMIN[0], "ответ готов")
    assert len(rig.llm.requests) == 1
    assert rig.llm.requests[0][-1]["content"] == "первая часть\n\nвторая часть"


async def test_html_failure_falls_back_to_plain_text(rig):
    rig.session.reject_html = True
    await rig.send(ADMIN, "вопрос")
    await wait_for_text(rig, ADMIN[0], "ответ готов")
    last = rig.session.calls[-1]
    assert isinstance(last, EditMessageText) and last.parse_mode is None


async def test_unknown_command_is_silent_and_does_not_reach_model(rig):
    await rig.send(ADMIN, "/whatever")
    await asyncio.sleep(0.05)
    assert rig.llm.requests == []
    assert rig.session.texts(ADMIN[0]) == []
