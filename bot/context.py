"""Сборка запроса к модели и подготовка длинной истории к сжатию.

Bionic сжимает контекст только в своём чате. По API длинный запрос молча обрезается,
поэтому следить за размером и пересказывать старые сообщения приходится боту.
"""
import math

from .config import CHARS_PER_TOKEN
from .db import Message

IMAGE_TOKENS = 600
MESSAGE_OVERHEAD = 6
# Фото пересылаем заново, пока разговор идёт о нём, а не на протяжении всего чата.
IMAGE_MEMORY_MESSAGES = 6
# Два последних сообщения всегда остаются дословно.
KEEP_TAIL = 2

SUMMARY_HEADER = "Краткое содержание предыдущей части этого диалога:\n"
SUMMARY_PROMPT = (
    "Сожми фрагмент диалога пользователя с ассистентом в краткую сводку. Сохрани факты, "
    "имена, числа, договорённости, требования пользователя и незавершённые задачи. "
    "Пиши на языке диалога, только сводку, без вступлений."
)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) + MESSAGE_OVERHEAD


def message_tokens(message: Message) -> int:
    return estimate_tokens(message.content) + len(message.images) * IMAGE_TOKENS


def total_tokens(summary: str, history: list[Message], text: str, images: list[str]) -> int:
    used = estimate_tokens(text) + len(images) * IMAGE_TOKENS
    if summary:
        used += estimate_tokens(SUMMARY_HEADER + summary)
    return used + sum(message_tokens(m) for m in history)


def messages_tokens(messages: list[dict]) -> int:
    """Оценка длины готового запроса к модели, чтобы отдать ответу всё оставшееся место."""
    used = 0
    for message in messages:
        used += MESSAGE_OVERHEAD
        content = message["content"]
        if isinstance(content, str):
            used += estimate_tokens(content)
            continue
        for part in content:
            if part.get("type") == "text":
                used += estimate_tokens(part["text"])
            else:
                used += IMAGE_TOKENS
    return used


def build_messages(
    summary: str, history: list[Message], text: str, images: list[str], vision: bool
) -> list[dict]:
    result: list[dict] = []
    if summary:
        result.append({"role": "system", "content": SUMMARY_HEADER + summary})

    remembered = _remembered_image_index(history) if vision and not images else None
    for index, message in enumerate(history):
        if index == remembered:
            content = _with_images(message.content, message.images)
        elif message.images:
            content = f"[Было приложено изображение]\n{message.content}"
        else:
            content = message.content
        result.append({"role": message.role, "content": content})

    result.append({"role": "user", "content": _with_images(text, images)})
    return result


def _remembered_image_index(history: list[Message]) -> int | None:
    start = max(0, len(history) - IMAGE_MEMORY_MESSAGES)
    for index in range(len(history) - 1, start - 1, -1):
        if history[index].images:
            return index
    return None


def _with_images(text: str, images: list[str]) -> str | list[dict]:
    if not images:
        return text
    parts: list[dict] = [{"type": "text", "text": text}]
    for image in images:
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}}
        )
    return parts


def take_oldest(history: list[Message], limit: int, keep: int = KEEP_TAIL) -> list[Message]:
    """Берёт сообщения для пересказа, не трогая хвост. Минимум одно, если есть что сжимать."""
    candidates = history[:-keep] if keep else history
    chunk: list[Message] = []
    used = 0
    for message in candidates:
        cost = message_tokens(message)
        if chunk and used + cost > limit:
            break
        chunk.append(message)
        used += cost
    return chunk


def compaction_request(summary: str, chunk: list[Message]) -> list[dict]:
    lines = [SUMMARY_PROMPT, ""]
    if summary:
        lines += ["Сводка более раннего диалога, включи её содержание:", summary, ""]
    lines.append("Фрагмент диалога:")
    for message in chunk:
        speaker = "Пользователь" if message.role == "user" else "Ассистент"
        lines.append(f"{speaker}: {message.content}")
    return [{"role": "user", "content": "\n".join(lines)}]
