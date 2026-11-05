"""Очередь запросов. Модель одна и считает медленно, поэтому запросы идут строго по одному."""
import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol

from . import context
from .bionic import BionicChats
from .config import CHARS_PER_TOKEN, Config
from .db import Database, Message
from .llm import LLMError, LMStudio, ModelInfo

log = logging.getLogger(__name__)

ACTIVE_MODEL_KEY = "active_model"
# После сжатия оставляем запас, иначе пересказ запускался бы на каждом сообщении.
COMPACTION_TARGET = 0.6
SUMMARY_SHARE = 0.25
# Запас на случай, когда оценка длины запроса чуть занижена: без него ответ иногда
# упирался бы в контекст и обрывался.
GENERATION_MARGIN = 256
BIONIC_FAILURE_NOTE = "Синхронизация с Bionic не удалась, чат сохранён только в боте"
IMAGE_MARK = "[Фото из Telegram]"

STATUS_THINKING = "Думаю..."
STATUS_LOADING = "Загружаю модель..."
STATUS_COMPACTING = "Сжимаю историю диалога…"
STATUS_STOPPED = "Остановлено"


class Reply(Protocol):
    """Куда воркер выводит ход работы. В боте это сообщение Telegram."""

    async def status(self, text: str) -> None: ...
    async def update(self, text: str) -> None: ...
    async def finish(self, text: str) -> None: ...


class Submit(Enum):
    QUEUED = auto()
    MERGED = auto()
    BUSY = auto()


@dataclass
class Job:
    user_id: int
    text: str
    images: list[str]
    reply: Reply
    answer: str = ""
    notes: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None
    stop_requested: bool = False
    discard: bool = False
    finished: asyncio.Event = field(default_factory=asyncio.Event)


class Worker:
    def __init__(
        self, llm: LMStudio, db: Database, config: Config, bionic: BionicChats | None = None
    ):
        self._llm = llm
        self._db = db
        self._config = config
        self._bionic = bionic
        self._pending: deque[Job] = deque()
        self._current: Job | None = None
        self._wake = asyncio.Event()
        self._submit_lock = asyncio.Lock()

    @property
    def queue_size(self) -> int:
        return len(self._pending) + (1 if self._current else 0)

    @property
    def current_user_id(self) -> int | None:
        return self._current.user_id if self._current else None

    async def submit(
        self, user_id: int, text: str, images: list[str], make_reply
    ) -> Submit:
        """Ставит запрос в очередь. На одного пользователя приходится один запрос."""
        async with self._submit_lock:
            if self.current_user_id == user_id:
                return Submit.BUSY
            for job in self._pending:
                if job.user_id == user_id:
                    job.text = f"{job.text}\n\n{text}"
                    job.images.extend(images)
                    return Submit.MERGED

            job = Job(user_id, text, images, make_reply())
            ahead = self.queue_size
            # Статус идёт первым: воркер должен получить уже созданное сообщение.
            await job.reply.status(_queue_status(ahead) if ahead else STATUS_THINKING)
            self._pending.append(job)
            self._wake.set()
            return Submit.QUEUED

    async def cancel(self, user_id: int, discard: bool = False) -> bool:
        """Отменяет запрос пользователя. С discard частичный ответ не сохраняется."""
        for job in list(self._pending):
            if job.user_id == user_id:
                self._pending.remove(job)
                await job.reply.finish(STATUS_STOPPED)
                await self._refresh_positions()
                return True

        job = self._current
        if job is None or job.user_id != user_id:
            return False
        job.stop_requested = True
        job.discard = discard
        if job.task:
            job.task.cancel()
        await job.finished.wait()
        return True

    async def run(self) -> None:
        while True:
            job = await self._next_job()
            await self._run_job(job)

    async def _next_job(self) -> Job:
        idle_limit = self._config.idle_unload_minutes * 60
        while not self._pending:
            self._wake.clear()
            wait_for_idle = idle_limit > 0 and self._llm.owns_loaded_model
            try:
                await asyncio.wait_for(self._wake.wait(), idle_limit if wait_for_idle else None)
            except TimeoutError:
                await self._llm.unload_own_model()
        return self._pending.popleft()

    async def _run_job(self, job: Job) -> None:
        self._current = job
        job.task = asyncio.create_task(self._process(job))
        try:
            await job.task
        except asyncio.CancelledError:
            if not job.stop_requested:
                job.task.cancel()
                raise
            await self._finish_stopped(job)
        except LLMError as exc:
            log.error("Ошибка модели: %s", exc)
            await self._report(job, "Модель сейчас недоступна, попробуй чуть позже")
        except Exception:
            log.exception("Сбой при обработке запроса")
            await self._report(job, "Что-то пошло не так, попробуй ещё раз")
        finally:
            self._current = None
            job.finished.set()
            await self._refresh_positions()

    async def _process(self, job: Job) -> None:
        # Сервер мог быть выключен после старта бота (например, пользователь перезапустил
        # Bionic). Поднимаем его прежде, чем запрашивать список моделей, иначе падаем здесь.
        await self._llm.start_server()
        model = await self.active_model()
        if job.images and not model.vision:
            await job.reply.finish(
                f"Текущая модель ({model.name}) не умеет смотреть изображения. "
                "Пришли текст или попроси админа сменить модель"
            )
            return

        if not self._llm.is_loaded(model):
            await job.reply.status(STATUS_LOADING)
        model_id = await self._llm.ensure_loaded(model.key)

        await self._pull_from_bionic(job)
        summary, history = await self._fit_context(job, model_id)
        messages = context.build_messages(summary, history, job.text, job.images, model.vision)

        await job.reply.status(STATUS_THINKING)
        await self._generate(job, model_id, messages)
        await self._save_and_finish(job)

    async def _generate(self, job: Job, model_id: str, messages: list[dict]) -> None:
        """Генерация идёт столько, сколько нужно: таймаута нет, остановить можно /stop.

        Модели отдаётся всё свободное место контекста, чтобы ответ не упирался в лимит.
        """
        max_tokens = max(
            self._config.reply_max_tokens,
            self._config.context_length - context.messages_tokens(messages) - GENERATION_MARGIN,
        )
        async for delta in self._llm.stream_chat(model_id, messages, max_tokens):
            if delta.content:
                job.answer += delta.content
                await job.reply.update(job.answer)

    async def _save_and_finish(self, job: Job) -> None:
        answer = job.answer.strip()
        if not answer:
            await job.reply.finish(
                "Модель не дала ответа, попробуй переформулировать вопрос"
            )
            return
        await self._store(job, answer)
        await job.reply.finish(_with_notes(answer, job.notes))

    async def _finish_stopped(self, job: Job) -> None:
        answer = job.answer.strip()
        if answer and not job.discard:
            await self._store(job, answer)
        await self._report(job, _with_notes(answer, [STATUS_STOPPED, *job.notes]))

    async def _store(self, job: Job, answer: str) -> None:
        await self._db.add_exchange(job.user_id, job.text, job.images, answer)
        await self._push_to_bionic(job, answer)

    async def _pull_from_bionic(self, job: Job) -> None:
        """Забирает сообщения, написанные в чате на ноутбуке после последней синхронизации."""
        if self._bionic is None:
            return
        is_admin, session_id, synced_at = await self._db.bionic_link(job.user_id)
        if not is_admin or session_id is None:
            return
        try:
            messages = await self._bionic.read_since(session_id, synced_at)
            for message in messages:
                await self._db.add_message(job.user_id, message.role, message.text)
            if messages:
                await self._db.set_bionic_link(job.user_id, session_id, messages[-1].created_at)
        except Exception:
            log.exception("Не удалось прочитать чат из Bionic")
            job.notes.append(BIONIC_FAILURE_NOTE)

    async def _push_to_bionic(self, job: Job, answer: str) -> None:
        """В Bionic показывается только чат админа: остальные чаты остаются в боте."""
        if self._bionic is None:
            return
        is_admin, session_id, _ = await self._db.bionic_link(job.user_id)
        if not is_admin:
            return
        question = f"{IMAGE_MARK}\n{job.text}" if job.images else job.text
        try:
            session_id, synced_at = await self._bionic.append_exchange(
                session_id, question, answer
            )
            await self._db.set_bionic_link(job.user_id, session_id, synced_at)
        except Exception:
            log.exception("Не удалось записать чат в Bionic")
            if BIONIC_FAILURE_NOTE not in job.notes:
                job.notes.append(BIONIC_FAILURE_NOTE)

    async def _fit_context(self, job: Job, model_id: str) -> tuple[str, list[Message]]:
        """Пересказывает старые сообщения, если вместе с новым они не влезают в контекст."""
        summary, history = await self._db.load_chat(job.user_id)
        budget = self._config.prompt_budget
        if context.total_tokens(summary, history, job.text, job.images) <= budget:
            return summary, history

        await job.reply.status(STATUS_COMPACTING)
        target = int(budget * COMPACTION_TARGET)
        keep = context.KEEP_TAIL
        while (used := context.total_tokens(summary, history, job.text, job.images)) > target:
            limit = budget - context.estimate_tokens(summary)
            chunk = context.take_oldest(history, limit, keep)
            if not chunk:
                if keep and used > budget:
                    # Хвост из пары огромных сообщений тоже придётся пересказать.
                    keep = 0
                    continue
                break
            summary = await self._summarize(job, model_id, summary, chunk)
            await self._db.apply_compaction(job.user_id, summary, chunk[-1].id)
            history = history[len(chunk):]
        return summary, history

    async def _summarize(
        self, job: Job, model_id: str, summary: str, chunk: list[Message]
    ) -> str:
        """Возвращает новую сводку. Если модель не справилась, начало просто забывается."""
        request = context.compaction_request(summary, chunk)
        result = await self._llm.complete(
            model_id, request, self._config.summary_max_tokens
        )
        if not result:
            log.warning("Пустая сводка у пользователя %s", job.user_id)
            note = "Начало диалога забыто, сжать его не получилось"
            if note not in job.notes:
                job.notes.append(note)
            return summary
        limit = int(self._config.prompt_budget * SUMMARY_SHARE) * CHARS_PER_TOKEN
        return result[:limit]

    async def active_model(self) -> ModelInfo:
        models = await self._llm.list_models()
        if not models:
            raise LLMError("В Bionic нет ни одной модели.")
        wanted = await self._db.get_setting(ACTIVE_MODEL_KEY) or self._config.default_model
        return next((m for m in models if m.key == wanted), models[0])

    async def _refresh_positions(self) -> None:
        ahead = 1 if self._current else 0
        for job in self._pending:
            # Тому, кто идёт следующим, статус сразу покажет сама обработка.
            if ahead:
                try:
                    await job.reply.status(_queue_status(ahead))
                except Exception:
                    log.exception("Не удалось обновить место в очереди")
            ahead += 1

    @staticmethod
    async def _report(job: Job, text: str) -> None:
        try:
            await job.reply.finish(text)
        except Exception:
            log.exception("Не удалось отправить сообщение пользователю")


def _queue_status(ahead: int) -> str:
    return f"Ожидай, перед тобой в очереди {ahead} человек"


def _with_notes(answer: str, notes: list[str]) -> str:
    return "\n\n".join(part for part in (answer, *notes) if part)
