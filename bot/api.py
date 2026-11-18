"""API для других ботов: вопрос модели в формате OpenAI (POST /v1/chat/completions).

Зачем свой API, а не прямой доступ к LM Studio. Бот сам управляет моделью: грузит
нужную и выгружает остальные. Чужая программа, идущая в LM Studio мимо него, начала бы
выгружать модели наперегонки с ботом. А так запрос по API встаёт в ту же очередь, что
и люди из Telegram, и отвечает та же модель, что выбрал админ.

Защита:
- слушаем только адрес из API_HOST (адрес ноутбука в Tailscale), так что из домашней
  сети и из интернета API не видно;
- у каждого бота свой ключ (API_KEYS), без него ответ 401;
- тексты вопросов и ответов в лог не пишутся, только кто спросил и сколько ждал.

Формат ответа повторяет OpenAI, поэтому подойдёт любая программа, которая умеет с ним
работать. Ответ отдаётся целиком, без потока.
"""
import asyncio
import hmac
import logging
import time
import uuid

from aiohttp import web

from . import context
from .config import Config
from .worker import GENERATION_MARGIN, Worker

log = logging.getLogger(__name__)

ROLES = {"system", "user", "assistant"}
# Меньше этого ответ не имеет смысла: разговор просто не помещается в контекст модели.
MIN_REPLY_TOKENS = 64
# Предел тела запроса. Текстовый разговор столько не занимает, а большой мусор не примем.
MAX_BODY_BYTES = 1_000_000
# Адрес Tailscale появляется не сразу после включения ноутбука: пробуем занять его снова.
BIND_RETRY_SECONDS = 30


def _error(status: int, text: str) -> web.Response:
    return web.json_response({"error": {"message": text}}, status=status)


def client_name(header: str, keys: dict[str, str]) -> str | None:
    """Имя бота по заголовку «Authorization: Bearer <ключ>», None для чужого ключа.

    compare_digest сравнивает за одинаковое время: по скорости отказа ключ не подобрать.
    """
    if not header.startswith("Bearer "):
        return None
    given = header[len("Bearer "):].strip().encode()
    found = None
    for name, key in keys.items():
        if hmac.compare_digest(given, key.encode()):
            found = name
    return found


def parse_messages(body: object) -> list[dict] | None:
    """Список сообщений {role, content} с текстом; None, если формат не тот."""
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list) or not messages:
        return None
    clean = []
    for message in messages:
        if (
            not isinstance(message, dict)
            or message.get("role") not in ROLES
            or not isinstance(message.get("content"), str)
        ):
            return None
        clean.append({"role": message["role"], "content": message["content"]})
    return clean


def reply_room(config: Config, messages: list[dict], requested: object) -> int:
    """Сколько токенов дать на ответ: сколько просят, но не больше лимита и свободного места."""
    if not isinstance(requested, int) or isinstance(requested, bool) or requested <= 0:
        requested = config.reply_max_tokens
    free = config.context_length - context.messages_tokens(messages) - GENERATION_MARGIN
    return min(requested, config.reply_max_tokens, free)


def build_app(worker: Worker, config: Config) -> web.Application:
    async def chat(request: web.Request) -> web.Response:
        name = client_name(request.headers.get("Authorization", ""), config.api_keys)
        if name is None:
            log.warning("API: отказ, неверный ключ (с адреса %s)", request.remote)
            return _error(401, "invalid api key")
        try:
            body = await request.json()
        except Exception:
            return _error(400, "body must be JSON")
        messages = parse_messages(body)
        if messages is None:
            return _error(400, "messages must be a list of {role, content} with text")
        max_tokens = reply_room(config, messages, body.get("max_tokens"))
        if max_tokens < MIN_REPLY_TOKENS:
            return _error(413, "conversation is too long for the model context")

        started = time.monotonic()
        job = await worker.submit_api(messages, max_tokens)
        try:
            await asyncio.wait_for(job.finished.wait(), config.api_timeout)
        except TimeoutError:
            log.warning("API: %s не дождался ответа за %dс, запрос снят", name, config.api_timeout)
            return _error(504, "model did not answer in time")
        finally:
            # Ответа больше никто не ждёт: снимаем запрос, чтобы модель не работала впустую.
            if not job.finished.is_set():
                await worker.drop(job)

        seconds = time.monotonic() - started
        answer = job.answer.strip()
        if job.failed:
            log.warning("API: %s, модель недоступна (%.1fс)", name, seconds)
            return _error(503, "model is unavailable")
        if not answer:
            log.warning("API: %s, пустой ответ модели (%.1fс)", name, seconds)
            return _error(502, "model returned an empty answer")
        log.info("API: %s, сообщений %d, ответ за %.1fс", name, len(messages), seconds)
        return web.json_response({
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": job.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }],
        })

    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app.router.add_post("/v1/chat/completions", chat)
    return app


async def serve_api(worker: Worker, config: Config) -> None:
    """Держит API запущенным, пока жив бот. Без API_HOST ничего не делает."""
    if not config.api_host:
        return
    # access_log=None: стандартный журнал aiohttp писал бы каждый запрос, нам хватает своего.
    runner = web.AppRunner(build_app(worker, config), access_log=None)
    await runner.setup()
    try:
        warned = False
        while True:
            try:
                await web.TCPSite(runner, config.api_host, config.api_port).start()
                break
            except OSError as exc:
                if not warned:
                    log.warning(
                        "API пока не запущен: адрес %s:%d недоступен (%s). Tailscale включён? "
                        "Пробую каждые %dс",
                        config.api_host, config.api_port, exc, BIND_RETRY_SECONDS,
                    )
                    warned = True
                await asyncio.sleep(BIND_RETRY_SECONDS)
        log.info(
            "API слушает %s:%d, ботов с ключом: %d",
            config.api_host, config.api_port, len(config.api_keys),
        )
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
