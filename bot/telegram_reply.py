"""Показ ответа в Telegram: статусы, печать на лету и итоговое оформление."""
import logging
import time

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter

from .render import split_markdown, to_html

log = logging.getLogger(__name__)

EDIT_INTERVAL = 1.5
CURSOR = " ▌"


class TelegramReply:
    def __init__(self, bot: Bot, chat_id: int, reply_to: int | None = None):
        self._bot = bot
        self._chat_id = chat_id
        self._reply_to = reply_to
        self._message_ids: list[int] = []
        self._shown: list[str] = []
        self._next_edit = 0.0

    async def status(self, text: str) -> None:
        await self._show([text])

    async def update(self, text: str) -> None:
        """Обновляет текст не чаще раза в EDIT_INTERVAL: у Telegram есть лимит на правки."""
        if time.monotonic() < self._next_edit:
            return
        self._next_edit = time.monotonic() + EDIT_INTERVAL
        parts = split_markdown(text)
        parts[-1] += CURSOR
        try:
            await self._show(parts)
        except TelegramRetryAfter as exc:
            self._next_edit = time.monotonic() + exc.retry_after
        except TelegramAPIError as exc:
            # Сбой показа не должен обрывать генерацию: следующая правка всё догонит.
            log.warning("Не удалось обновить сообщение: %s", exc)

    async def finish(self, text: str) -> None:
        parts = split_markdown(text)
        for index, part in enumerate(parts):
            if index < len(self._message_ids):
                await self._edit_html(self._message_ids[index], part)
            else:
                await self._send_html(part)

    async def _show(self, parts: list[str]) -> None:
        for index, part in enumerate(parts):
            if index >= len(self._message_ids):
                message = await self._send(part)
                self._message_ids.append(message.message_id)
                self._shown.append(part)
            elif self._shown[index] != part:
                await self._edit(self._message_ids[index], part)
                self._shown[index] = part

    async def _send(self, text: str, parse_mode: str | None = None):
        return await self._bot.send_message(
            self._chat_id,
            text,
            parse_mode=parse_mode,
            reply_to_message_id=None if self._message_ids else self._reply_to,
        )

    async def _edit(self, message_id: int, text: str, parse_mode: str | None = None) -> None:
        try:
            await self._bot.edit_message_text(
                text, chat_id=self._chat_id, message_id=message_id, parse_mode=parse_mode
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message:
                raise

    async def _edit_html(self, message_id: int, text: str) -> None:
        try:
            await self._edit(message_id, to_html(text), ParseMode.HTML)
        except TelegramBadRequest:
            log.info("Не вышло оформить HTML, отправляю простым текстом")
            await self._edit(message_id, text)

    async def _send_html(self, text: str) -> None:
        try:
            message = await self._send(to_html(text), ParseMode.HTML)
        except TelegramBadRequest:
            message = await self._send(text)
        self._message_ids.append(message.message_id)
        self._shown.append(text)
