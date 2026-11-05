"""Команды и сообщения Telegram."""
import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html import escape
from io import BytesIO
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart, Filter
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    Update,
)

from .config import Config
from .db import Database, User
from .documents import UnsupportedFile, extract_text
from .llm import LLMError, LMStudio
from .telegram_reply import TelegramReply
from .worker import ACTIVE_MODEL_KEY, Submit, Worker

log = logging.getLogger(__name__)

# Длинный текст Telegram режет на несколько сообщений, альбом тоже приходит по одному
# фото. Короткая пауза собирает такие части в один запрос.
COLLECT_DELAY = 1.2
TELEGRAM_FILE_LIMIT = 20 * 1024 * 1024
MAX_IMAGES = 4

DEFAULT_IMAGE_QUESTION = "Что на этом изображении?"
DEFAULT_FILE_QUESTION = "Изучи этот файл и кратко опиши, что в нём."

USER_COMMANDS = [
    BotCommand(command="new", description="Начать новый чат"),
    BotCommand(command="stop", description="Остановить ответ"),
    BotCommand(command="help", description="Как пользоваться"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="setmodel", description="Выбрать модель"),
    BotCommand(command="giveaccess", description="Выдать доступ: /giveaccess @тег"),
    BotCommand(command="revokeaccess", description="Забрать доступ: /revokeaccess @тег"),
    BotCommand(command="users", description="Кто имеет доступ"),
    BotCommand(command="status", description="Состояние бота"),
]

HELP_TEXT = (
    "/new - начать новый чат, бот забудет текущий разговор\n"
    "/stop - остановить ответ"
)
ADMIN_HELP_TEXT = (
    "\n\nКоманды админа:\n"
    "/setmodel - выбрать модель\n"
    "/giveaccess @тег - выдать доступ\n"
    "/revokeaccess @тег - забрать доступ и удалить историю\n"
    "/users - кто имеет доступ\n"
    "/status - состояние бота"
)


class IsAdmin(Filter):
    async def __call__(self, event: TelegramObject, user: User) -> bool:
        return user.is_admin


class AccessMiddleware(BaseMiddleware):
    """Пропускает только личные чаты людей с доступом. Остальным бот не отвечает."""

    def __init__(self, db: Database, bot: Bot):
        self._db = db
        self._bot = bot

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return None
        message = event.message or (event.callback_query.message if event.callback_query else None)
        sender = event.message.from_user if event.message else (
            event.callback_query.from_user if event.callback_query else None
        )
        if sender is None or message is None or message.chat.type != ChatType.PRIVATE:
            return None

        user, is_new = await self._db.resolve(sender.id, sender.username, sender.full_name)
        if user is None:
            return None
        if is_new:
            await set_user_commands(self._bot, user)
        data["user"] = user
        return await handler(event, data)


async def set_user_commands(bot: Bot, user: User) -> None:
    await bot.set_my_commands(
        ADMIN_COMMANDS if user.is_admin else USER_COMMANDS,
        scope=BotCommandScopeChat(chat_id=user.tg_id),
    )


@dataclass
class _Draft:
    parts: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    reply_to: int | None = None
    timer: asyncio.Task | None = None


class Inbox:
    """Собирает идущие подряд сообщения пользователя и передаёт их в очередь."""

    def __init__(self, bot: Bot, worker: Worker, config: Config):
        self._bot = bot
        self._worker = worker
        self._config = config
        self._drafts: dict[int, _Draft] = {}

    def add(
        self, user: User, text: str, images: list[str], message: Message
    ) -> None:
        draft = self._drafts.setdefault(user.id, _Draft(reply_to=message.message_id))
        if text:
            draft.parts.append(text)
        draft.images.extend(images)
        if draft.timer:
            draft.timer.cancel()
        draft.timer = asyncio.create_task(self._flush_later(user, message.chat.id))

    def drop(self, user_id: int) -> bool:
        draft = self._drafts.pop(user_id, None)
        if draft and draft.timer:
            draft.timer.cancel()
        return draft is not None

    async def _flush_later(self, user: User, chat_id: int) -> None:
        await asyncio.sleep(COLLECT_DELAY)
        draft = self._drafts.pop(user.id, None)
        if draft is None:
            return
        try:
            await self._submit(user, chat_id, draft)
        except Exception:
            log.exception("Не удалось поставить запрос в очередь")

    async def _submit(self, user: User, chat_id: int, draft: _Draft) -> None:
        text = "\n\n".join(draft.parts) or DEFAULT_IMAGE_QUESTION
        limit = self._config.max_input_chars
        notices: list[str] = []
        if len(text) > limit:
            text = text[:limit]
            notices.append(
                f"Текст слишком длинный: модель увидит только первые {limit} символов"
            )
        images = draft.images
        if len(images) > MAX_IMAGES:
            images = images[:MAX_IMAGES]
            notices.append(f"Обработаю только первые {MAX_IMAGES} изображения")

        def make_reply() -> TelegramReply:
            return TelegramReply(self._bot, chat_id, draft.reply_to)

        result = await self._worker.submit(user.id, text, images, make_reply)
        if result is Submit.BUSY:
            notices = ["Предыдущий ответ ещё готовится, дождись его или нажми /stop"]
        for notice in notices:
            await self._bot.send_message(chat_id, notice)


def build_router(
    bot: Bot, db: Database, llm: LMStudio, worker: Worker, config: Config
) -> Router:
    router = Router()
    inbox = Inbox(bot, worker, config)

    # --- Команды пользователя ---

    @router.message(CommandStart())
    async def start(message: Message, user: User) -> None:
        await set_user_commands(bot, user)
        await message.answer(_help_for(user))

    @router.message(Command("help"))
    async def help_command(message: Message, user: User) -> None:
        await message.answer(_help_for(user))

    @router.message(Command("new"))
    async def new_chat(message: Message, user: User) -> None:
        inbox.drop(user.id)
        await worker.cancel(user.id, discard=True)
        await db.reset_chat(user.id)
        await message.answer("Новый чат начат. Прошлый разговор забыт")

    @router.message(Command("stop"))
    async def stop(message: Message, user: User) -> None:
        dropped = inbox.drop(user.id)
        cancelled = await worker.cancel(user.id)
        if not dropped and not cancelled:
            await message.answer("Сейчас нечего останавливать")

    # --- Команды админа ---

    @router.message(Command("setmodel"), IsAdmin())
    async def set_model(message: Message) -> None:
        try:
            models = await llm.list_models()
            active = await worker.active_model()
        except LLMError:
            await message.answer("Сервер Bionic недоступен")
            return
        rows = [
            [InlineKeyboardButton(
                text=("✅ " if model.key == active.key else "") + model.name,
                callback_data=f"setmodel:{index}",
            )]
            for index, model in enumerate(models)
        ]
        await message.answer(
            "Какой моделью отвечать?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data.startswith("setmodel:"), IsAdmin())
    async def choose_model(callback: CallbackQuery) -> None:
        models = await llm.list_models()
        index = int(callback.data.split(":")[1])
        if index >= len(models):
            await callback.answer("Список моделей изменился, вызови /setmodel ещё раз")
            return
        model = models[index]
        await db.set_setting(ACTIVE_MODEL_KEY, model.key)
        await callback.answer()
        note = "Текущий ответ допишется старой моделью. " if worker.queue_size else ""
        await callback.message.edit_text(
            f"Выбрана модель: {model.name}\n"
            f"{note}Загрузится при следующем запросе"
        )

    @router.message(Command("giveaccess"), IsAdmin())
    async def give_access(message: Message, command: CommandObject) -> None:
        target = _parse_target(command.args)
        if target is None:
            await message.answer("Формат: /giveaccess @тег")
            return
        if await db.grant(target):
            await message.answer(
                f"Доступ выдан: {_label(target)}\n"
                "Написать первым бот не может, попроси человека отправить боту /start"
            )
        else:
            await message.answer(f"У {_label(target)} уже есть доступ")

    @router.message(Command("revokeaccess"), IsAdmin())
    async def revoke_access(message: Message, command: CommandObject, user: User) -> None:
        target = _parse_target(command.args)
        if target is None:
            await message.answer("Формат: /revokeaccess @тег")
            return
        found = await db.find(target)
        if found is None:
            await message.answer(f"У {_label(target)} нет доступа")
            return
        if found.id == user.id:
            await message.answer("Нельзя забрать доступ у самого себя")
            return
        inbox.drop(found.id)
        await worker.cancel(found.id, discard=True)
        await db.delete_user(found.id)
        await message.answer(f"Доступ забран, история удалена: {_label(target)}")

    @router.message(Command("users"), IsAdmin())
    async def list_users(message: Message) -> None:
        lines = ["<b>Доступ к боту:</b>"]
        for item in await db.list_users():
            label = f"@{item.username}" if item.username else str(item.tg_id)
            role = " (админ)" if item.is_admin else ""
            seen = f"был {item.last_seen} UTC" if item.last_seen else "ещё не заходил"
            lines.append(f"{escape(label)}{role}: {seen}")
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

    @router.message(Command("status"), IsAdmin())
    async def status(message: Message) -> None:
        try:
            model = await worker.active_model()
            state = "в памяти" if llm.is_loaded(model) else "выгружена"
            model_line = f"Модель: {model.name} ({state})"
        except LLMError:
            model_line = "Сервер Bionic недоступен"
        users = await db.list_users()
        await message.answer(
            f"{model_line}\n"
            f"Запросов в работе и в очереди: {worker.queue_size}\n"
            f"Пользователей с доступом: {len(users)}"
        )

    @router.message(F.text.startswith("/"))
    async def unknown_command(message: Message) -> None:
        # Молчим на неизвестные команды, но перехватываем их, чтобы они не ушли в модель.
        return

    # --- Сообщения для модели ---

    @router.message(F.text)
    async def on_text(message: Message, user: User) -> None:
        inbox.add(user, message.text, [], message)

    @router.message(F.photo)
    async def on_photo(message: Message, user: User) -> None:
        data = await _download(bot, message.photo[-1].file_id)
        image = base64.b64encode(data).decode()
        inbox.add(user, message.caption or "", [image], message)

    @router.message(F.document)
    async def on_document(message: Message, user: User) -> None:
        document = message.document
        if (document.file_size or 0) > TELEGRAM_FILE_LIMIT:
            await message.answer("Файл слишком большой: бот может скачать только до 20 МБ")
            return
        data = await _download(bot, document.file_id)

        if (document.mime_type or "").startswith("image/"):
            image = base64.b64encode(data).decode()
            inbox.add(user, message.caption or "", [image], message)
            return
        try:
            text = await asyncio.to_thread(extract_text, document.file_name or "", data)
        except UnsupportedFile as exc:
            await message.answer(str(exc))
            return

        # Вопрос идёт первым: слишком длинный файл обрежется с конца, а вопрос останется.
        question = message.caption or DEFAULT_FILE_QUESTION
        inbox.add(
            user, f"{question}\n\nФайл «{document.file_name}»:\n\n{text}", [], message
        )

    @router.message()
    async def on_unsupported(message: Message) -> None:
        await message.answer("Я понимаю текст, фото и файлы: текстовые, PDF и DOCX")

    return router


async def _download(bot: Bot, file_id: str) -> bytes:
    buffer = BytesIO()
    await bot.download(file_id, destination=buffer)
    return buffer.getvalue()


def _help_for(user: User) -> str:
    return HELP_TEXT + (ADMIN_HELP_TEXT if user.is_admin else "")


def _parse_target(args: str | None) -> str | None:
    """Принимает @тег, тег, ссылку t.me/тег или числовой id."""
    if not args:
        return None
    target = args.split()[0].removeprefix("https://").removeprefix("t.me/")
    return target.lstrip("@").lower() or None


def _label(target: str) -> str:
    return target if target.isdigit() else f"@{target}"
