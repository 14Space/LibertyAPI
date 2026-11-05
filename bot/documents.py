"""Извлечение текста из присланных файлов."""
from io import BytesIO
from pathlib import PurePath

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

TEXT_SUFFIXES = {
    ".txt", ".md", ".log", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".ini", ".toml",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".sql", ".sh", ".ps1", ".bat",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".java", ".kt", ".go", ".rs", ".php", ".rb", ".lua",
}


class UnsupportedFile(Exception):
    """Текст файла получить нельзя. Сообщение показывается пользователю."""


def extract_text(filename: str, data: bytes) -> str:
    suffix = PurePath(filename).suffix.lower()
    if suffix == ".pdf":
        text = _pdf(data)
    elif suffix == ".docx":
        text = _docx(data)
    elif suffix == ".doc":
        raise UnsupportedFile("Старый формат .doc не читается, пересохрани файл как .docx")
    elif suffix in TEXT_SUFFIXES or _looks_like_text(data):
        text = _decode(data)
    else:
        raise UnsupportedFile(
            f"Формат {suffix or 'без расширения'} не поддерживается. "
            "Подойдут текстовые файлы, PDF и DOCX"
        )

    text = text.strip()
    if not text:
        raise UnsupportedFile(
            "В файле не нашлось текста. Возможно, это скан: такие документы бот не читает"
        )
    return text


def _pdf(data: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise UnsupportedFile("PDF защищён паролем")
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except UnsupportedFile:
        raise
    except Exception as exc:
        raise UnsupportedFile("Не удалось прочитать PDF") from exc


def _docx(data: bytes) -> str:
    try:
        document = Document(BytesIO(data))
    except Exception as exc:
        raise UnsupportedFile("Не удалось прочитать DOCX") from exc

    blocks: list[str] = []
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            blocks.append(block.text)
        elif isinstance(block, Table):
            for row in block.rows:
                blocks.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(blocks)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _looks_like_text(data: bytes) -> bool:
    return b"\x00" not in data[:4096]
