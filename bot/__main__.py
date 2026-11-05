"""Запуск бота: python -m bot"""
import asyncio
import logging
import socket
import sys
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher

from .bionic import BionicChats
from .config import ROOT, Config, load_config
from .db import Database
from .handlers import AccessMiddleware, build_router, set_user_commands
from .llm import LMStudio
from .worker import Worker


def setup_logging() -> None:
    log_dir = ROOT / "data"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                log_dir / "bot.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8"
            ),
        ],
    )
    # Иначе в файл попадает каждый запрос httpx.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def ensure_single_instance() -> socket.socket:
    """Не даёт запустить второго бота: иначе обе копии конфликтуют за Telegram.

    Порт держится, пока жив процесс, и освобождается сам при его завершении.
    """
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 49677))
    except OSError:
        raise SystemExit(
            "Бот уже запущен в другом окне. Закрой его, прежде чем запускать заново."
        )
    return lock


async def connect_bionic(config: Config) -> BionicChats | None:
    """Без связи с Bionic бот продолжает работать, просто без общих чатов."""
    if not config.bionic_project:
        return None
    try:
        bionic = BionicChats(config.bionic_project)
        await bionic.backup(ROOT / "data" / "bionic-project-backup.sqlite")
    except Exception:
        logging.exception("Синхронизация с Bionic выключена")
        return None
    logging.info("Чат админа синхронизируется с проектом %s", config.bionic_project)
    return bionic


async def main() -> None:
    config = load_config()
    setup_logging()
    lock = ensure_single_instance()

    db = Database(config.db_path)
    llm = LMStudio(
        config.lm_base_url, config.lms_path, config.context_length, config.reasoning_effort
    )
    bot = Bot(config.bot_token)
    worker = Worker(llm, db, config, await connect_bionic(config))
    worker_task: asyncio.Task | None = None

    # Всё закрывается и при сбое на старте: незакрытая база держит процесс живым,
    # и start.bat не смог бы перезапустить бота.
    try:
        await db.connect()
        await db.ensure_admin(config.admin_username)
        await llm.start_server()

        # Команды видны только людям с доступом. Для всех остальных бот выглядит пустым.
        await bot.delete_my_commands()
        for user in await db.list_users():
            if user.tg_id:
                await set_user_commands(bot, user)

        dispatcher = Dispatcher()
        dispatcher.update.outer_middleware(AccessMiddleware(db, bot))
        dispatcher.include_router(build_router(bot, db, llm, worker, config))

        worker_task = asyncio.create_task(worker.run())
        me = await bot.get_me()
        logging.info("Бот @%s запущен, админ @%s", me.username, config.admin_username)
        await dispatcher.start_polling(bot, handle_signals=False)
    finally:
        if worker_task:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        await llm.unload_own_model()
        await llm.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
