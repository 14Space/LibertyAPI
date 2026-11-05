"""Работа с Bionic (LM Studio): сервер, загрузка моделей и генерация ответов."""
import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

# Под этим именем загруженную ботом модель видно в Bionic. Модели, загруженные
# вручную, бот по таймеру простоя не выгружает.
BOT_INSTANCE = "liberty-bot"
LOAD_TIMEOUT = 300


class LLMError(Exception):
    """Сервер недоступен или вернул ошибку."""


@dataclass(frozen=True)
class ModelInfo:
    key: str
    name: str
    vision: bool
    instances: tuple[dict, ...]


@dataclass(frozen=True)
class Delta:
    """Часть потока: текст ответа, рассуждения либо итог генерации."""
    content: str = ""
    reasoning: str = ""
    finish_reason: str | None = None


class LMStudio:
    def __init__(
        self, base_url: str, lms_path: str, context_length: int, reasoning_effort: str = "none"
    ):
        self._base_url = base_url
        self._lms_path = lms_path
        self._context_length = context_length
        self._reasoning_effort = reasoning_effort
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(30, read=None)
        )
        self._load_lock = asyncio.Lock()
        self.owns_loaded_model = False

    async def close(self) -> None:
        await self._client.aclose()

    # --- Сервер и модели ---

    async def start_server(self) -> None:
        if await self._server_alive():
            return
        # Сервер мог умереть вместе с закрытым Bionic, а значит, и выгруженная им модель.
        self.owns_loaded_model = False
        port = urlsplit(self._base_url).port or 1234
        await self._lms("server", "start", "-p", str(port))
        for _ in range(30):
            if await self._server_alive():
                return
            await asyncio.sleep(1)
        raise LLMError("Сервер Bionic не запустился.")

    async def list_models(self) -> list[ModelInfo]:
        try:
            response = await self._client.get("/api/v1/models")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Сервер Bionic недоступен: {exc!r}") from exc
        return [
            ModelInfo(
                key=item["key"],
                name=item.get("display_name") or item["key"],
                vision=bool(item.get("capabilities", {}).get("vision")),
                instances=tuple(item.get("loaded_instances") or ()),
            )
            for item in response.json()["models"]
            if item.get("type") == "llm"
        ]

    def is_loaded(self, model: ModelInfo) -> bool:
        return self._usable_instance(model) is not None

    async def ensure_loaded(self, key: str) -> str:
        """Оставляет в памяти только нужную модель и возвращает её id для запросов.

        Запуск отделён от ожидания: если пользователь нажмёт /stop во время загрузки,
        lms дозагрузит модель, а следующий запрос дождётся этого на блокировке.
        """
        return await asyncio.shield(asyncio.ensure_future(self._ensure_loaded(key)))

    async def _ensure_loaded(self, key: str) -> str:
        async with self._load_lock:
            # Сервер мог быть выключен после старта бота: например, пользователь закрыл
            # и открыл Bionic, а тот не поднимает сервер сам (autoStartOnLaunch=false).
            await self.start_server()
            models = await self.list_models()
            target = next((m for m in models if m.key == key), None)
            if target is None:
                raise LLMError(f"Модель {key} не найдена в Bionic.")

            instance = self._usable_instance(target)
            for model in models:
                for loaded in model.instances:
                    if loaded["id"] != instance:
                        log.info("Выгружаю %s", loaded["id"])
                        await self._lms("unload", loaded["id"])
            if instance:
                self.owns_loaded_model = instance == BOT_INSTANCE
                return instance

            log.info("Загружаю %s, контекст %d", key, self._context_length)
            await self._lms(
                "load", key,
                "-c", str(self._context_length),
                "--parallel", "1",
                "--identifier", BOT_INSTANCE,
                "-y",
            )
            self.owns_loaded_model = True
            return BOT_INSTANCE

    async def unload_own_model(self) -> None:
        async with self._load_lock:
            if not self.owns_loaded_model:
                return
            self.owns_loaded_model = False
            log.info("Простой: выгружаю модель бота")
            try:
                await self._lms("unload", BOT_INSTANCE)
            except LLMError as exc:
                log.warning("Не удалось выгрузить модель: %s", exc)

    def _usable_instance(self, model: ModelInfo) -> str | None:
        """Подходит экземпляр с контекстом не меньше нашего: меньший обрежет историю."""
        for instance in model.instances:
            context = instance.get("config", {}).get("context_length", 0)
            if context >= self._context_length:
                return instance["id"]
        return None

    async def _server_alive(self) -> bool:
        try:
            response = await self._client.get("/v1/models", timeout=3)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def _lms(self, *args: str) -> None:
        process = await asyncio.create_subprocess_exec(
            self._lms_path, *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), LOAD_TIMEOUT)
        except TimeoutError as exc:
            process.kill()
            raise LLMError(f"lms {args[0]}: превышено время ожидания") from exc
        if process.returncode:
            text = stderr.decode("utf-8", "replace").strip()
            raise LLMError(f"lms {args[0]}: {text[-300:]}")

    # --- Генерация ---

    async def stream_chat(
        self, model: str, messages: list[dict], max_tokens: int
    ) -> AsyncIterator[Delta]:
        body = {"model": model, "messages": messages, "stream": True, "max_tokens": max_tokens}
        if self._reasoning_effort:
            body["reasoning_effort"] = self._reasoning_effort
        try:
            async with self._client.stream("POST", "/v1/chat/completions", json=body) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode("utf-8", "replace")[:300]
                    raise LLMError(f"HTTP {response.status_code}: {detail}")
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    event = json.loads(line[6:])
                    if "error" in event:
                        raise LLMError(str(event["error"]))
                    for choice in event.get("choices", ()):
                        delta = choice.get("delta", {})
                        yield Delta(
                            content=delta.get("content") or "",
                            reasoning=delta.get("reasoning_content") or "",
                            finish_reason=choice.get("finish_reason"),
                        )
        except httpx.HTTPError as exc:
            raise LLMError(f"Обрыв связи с Bionic: {exc!r}") from exc

    async def complete(self, model: str, messages: list[dict], max_tokens: int) -> str:
        parts = [
            delta.content async for delta in self.stream_chat(model, messages, max_tokens)
        ]
        return "".join(parts).strip()
