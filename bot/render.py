"""Разбивка длинных ответов и перевод Markdown модели в HTML для Telegram."""
import re
from html import escape

# Лимит Telegram 4096 символов. Запас нужен, потому что теги HTML увеличивают текст.
MESSAGE_LIMIT = 3500

# Конец предложения: точка, «!», «?» или многоточие, за которыми пробел, кавычка,
# скобка или конец текста.
_SENTENCE_END = re.compile(r"[.!?…](?=[\s\"'»)\]]|$)")

_FENCE = re.compile(r"```([^\n`]*)\n?(.*?)(?:```|\Z)", re.S)
_LANGUAGE = re.compile(r"^[\w+#.-]+$")
_INLINE_CODE = re.compile(r"(`[^`\n]+`)")
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=\S)\*(?![\w*])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BULLET = re.compile(r"^(\s*)[*-]\s+", re.M)


def split_markdown(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Режет длинный ответ по границам предложений, целиком, в несколько сообщений.

    Предложение, которое не влезает рядом с предыдущими, уходит в следующий кусок.
    Только одно предложение длиннее лимита приходится разрывать. Блок кода на границе
    закрывается и открывается заново, чтобы оба куска оставались валидным Markdown.
    """
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = _cut_point(rest, limit)
        chunk, rest = rest[:cut].rstrip(), rest[cut:].lstrip("\n")

        opener = None
        for line in chunk.splitlines():
            if line.lstrip().startswith("```"):
                opener = None if opener else line.strip()
        if opener:
            chunk += "\n```"
            rest = f"{opener}\n{rest}"
        parts.append(chunk)
    parts.append(rest)
    return parts


def _cut_point(text: str, limit: int) -> int:
    """Самая поздняя граница в пределах лимита: конец абзаца, затем конец предложения."""
    window = text[:limit]

    paragraph = window.rfind("\n")
    sentence = -1
    for match in _SENTENCE_END.finditer(window):
        end = match.end()
        while end < len(text) and text[end] in " \t":
            end += 1
        sentence = end

    cut = max(paragraph + 1 if paragraph != -1 else -1, sentence)
    if cut <= 0:
        # Ни абзаца, ни предложения: одно слишком длинное предложение режем по пробелу.
        space = window.rfind(" ")
        cut = space + 1 if space > 0 else limit
    return cut


def to_html(markdown: str) -> str:
    output: list[str] = []
    position = 0
    for match in _FENCE.finditer(markdown):
        output.append(_inline(markdown[position:match.start()]))
        language = match[1].strip()
        code = escape(match[2].rstrip("\n"), quote=False)
        if _LANGUAGE.match(language):
            output.append(f'<pre><code class="language-{language}">{code}</code></pre>')
        else:
            output.append(f"<pre>{code}</pre>")
        position = match.end()
    output.append(_inline(markdown[position:]))
    return "".join(output)


def _inline(text: str) -> str:
    pieces = _INLINE_CODE.split(text)
    for index, piece in enumerate(pieces):
        if index % 2:
            pieces[index] = f"<code>{escape(piece[1:-1], quote=False)}</code>"
        else:
            pieces[index] = _format(escape(piece, quote=False))
    return "".join(pieces)


def _format(text: str) -> str:
    text = _HEADING.sub(lambda m: f"<b>{m[1].replace('**', '')}</b>", text)
    text = _BULLET.sub(r"\1• ", text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    return _LINK.sub(r'<a href="\2">\1</a>', text)
