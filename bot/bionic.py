"""Общие чаты с Bionic: запись диалога в проект и чтение того, что дописано на ноутбуке.

У Bionic нет API для чатов, поэтому бот работает напрямую с базой проекта. Формат внутренний:
обновление Bionic может его изменить, поэтому любой сбой здесь не должен мешать ответам бота.
Чат хранится цепочкой записей: каждая ссылается на предыдущую, а сессия хранит последнюю.
Счётчики ссылок ведут триггеры самой базы.
"""
import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

DEFAULT_BASE = Path.home() / ".lmstudio" / "apps" / "bionic"
TITLE_LENGTH = 48
BUSY_TIMEOUT_SECONDS = 5


class BionicError(Exception):
    """Проект не найден или база имеет неожиданный вид."""


@dataclass(frozen=True)
class BionicMessage:
    role: str
    text: str
    created_at: int


class BionicChats:
    def __init__(self, project_name: str, base_dir: Path = DEFAULT_BASE):
        self._base_dir = base_dir
        projects = self._read_registry()
        project = projects.get(project_name)
        if project is None:
            raise BionicError(f"В Bionic нет проекта «{project_name}».")
        self._db_path = Path(project["path"]) / ".internal" / "ng-sessions.sqlite"
        self._working_directory = project["projectWorkingDirectoryPath"]
        self._other_databases = [
            Path(other["path"]) / ".internal" / "ng-sessions.sqlite"
            for name, other in projects.items()
            if name != project_name
        ]

    async def append_exchange(
        self, session_id: str | None, question: str, answer: str
    ) -> tuple[str, int]:
        """Дописывает вопрос и ответ. Возвращает id чата и отметку времени записи."""
        return await asyncio.to_thread(self._append_exchange, session_id, question, answer)

    async def read_since(self, session_id: str, since: int) -> list[BionicMessage]:
        """Сообщения чата новее отметки: то, что написано уже в самом Bionic."""
        return await asyncio.to_thread(self._read_since, session_id, since)

    async def backup(self, destination: Path) -> None:
        await asyncio.to_thread(self._backup, destination)

    # --- Запись ---

    def _append_exchange(
        self, session_id: str | None, question: str, answer: str
    ) -> tuple[str, int]:
        connection = self._connect(self._db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            head = self._current_head(connection, session_id)
            if head is False:
                session_id = self._create_session(connection, question)
                head = None

            now = int(time.time() * 1000)
            head = self._insert_entry(connection, head, _user_entry(question, now))
            head = self._insert_entry(connection, head, _assistant_entry(answer, now + 1))
            head = self._insert_entry(
                connection, head, {"type": "forkPoint", "forkPoint": "assistantTurnEnd"}
            )
            connection.execute(
                "UPDATE sessions SET committed_head_entry_id = ?, updated_timestamp = ?, "
                "has_unread = 1 WHERE session_id = ?",
                (head, now + 1, session_id),
            )
            connection.commit()
            return session_id, now + 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _current_head(connection: sqlite3.Connection, session_id: str | None):
        """Последняя запись чата. False значит, что чата нет: новый или удалён в Bionic."""
        if session_id is None:
            return False
        row = connection.execute(
            "SELECT committed_head_entry_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row[0] if row else False

    def _create_session(self, connection: sqlite3.Connection, question: str) -> str:
        session_id = str(uuid4())
        connection.execute(
            "INSERT INTO sessions (session_id, session_json, updated_timestamp, "
            "user_draft_json, suggested_session_name) VALUES (?, ?, ?, ?, ?)",
            (
                session_id,
                json.dumps(self._session_template(connection), ensure_ascii=False),
                int(time.time() * 1000),
                json.dumps({"type": "composerDrafts", "drafts": [{"id": "main", "draft": None}]}),
                _title(question),
            ),
        )
        return session_id

    def _session_template(self, connection: sqlite3.Connection) -> dict:
        """Настройки нового чата берём у существующего: их набор зависит от версии Bionic."""
        raw = self._latest_session_json(connection)
        for path in self._other_databases:
            if raw or not path.exists():
                break
            other = self._connect(path)
            try:
                raw = self._latest_session_json(other)
            finally:
                other.close()
        if raw is None:
            raise BionicError("Нет чата, с которого можно взять настройки.")

        template = json.loads(raw)
        template["sessionParams"] = {"working_directory": self._working_directory}
        template["queuedUserTurns"] = []
        template["contextEstimation"] = {"before": 0, "self": 0, "total": 0}
        return template

    @staticmethod
    def _latest_session_json(connection: sqlite3.Connection) -> str | None:
        row = connection.execute(
            "SELECT session_json FROM sessions WHERE is_transient = 0 "
            "AND parent_session_id IS NULL ORDER BY updated_timestamp DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _insert_entry(connection: sqlite3.Connection, previous: str | None, entry: dict) -> str:
        entry_id = str(uuid4())
        full = {"id": entry_id, **({"previousId": previous} if previous else {}), **entry}
        connection.execute(
            "INSERT INTO chat_entries (id, previous_id, entry_json) VALUES (?, ?, ?)",
            (entry_id, previous, json.dumps(full, ensure_ascii=False)),
        )
        return entry_id

    # --- Чтение ---

    def _read_since(self, session_id: str, since: int) -> list[BionicMessage]:
        connection = self._connect(self._db_path)
        try:
            row = connection.execute(
                "SELECT committed_head_entry_id FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            entry_id = row[0] if row else None
            messages: list[BionicMessage] = []
            while entry_id:
                row = connection.execute(
                    "SELECT previous_id, entry_json FROM chat_entries WHERE id = ?", (entry_id,)
                ).fetchone()
                if row is None:
                    break
                entry_id = row[0]
                entry = json.loads(row[1])
                created_at = entry.get("createdTimestamp")
                if created_at is not None and created_at <= since:
                    break
                message = _visible_message(entry)
                if message:
                    messages.append(message)
            messages.reverse()
            return messages
        finally:
            connection.close()

    # --- Служебное ---

    def _backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = self._connect(self._db_path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def _read_registry(self) -> dict[str, dict]:
        path = self._base_dir / ".internal" / "projects-registry.json"
        try:
            registry = json.loads(path.read_text(encoding="utf-8"))
            entries = registry["json"]["projects"]
        except (OSError, ValueError, KeyError) as exc:
            raise BionicError(f"Не удалось прочитать список проектов: {exc!r}") from exc

        projects: dict[str, dict] = {}
        for _, info in entries:
            meta_path = Path(info["path"]) / "project.json"
            if meta_path.exists():
                name = json.loads(meta_path.read_text(encoding="utf-8")).get("name")
                if name:
                    projects[name] = info
        return projects

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        if not path.exists():
            raise BionicError(f"Нет базы проекта: {path}")
        connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _user_entry(text: str, created_at: int) -> dict:
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        if line else {"type": "paragraph"}
        for line in text.split("\n")
    ]
    return {
        "type": "message",
        "message": {"role": "user", "parts": [{"type": "text", "text": text}]},
        "createdTimestamp": created_at,
        "rawMessage": {"type": "doc", "content": paragraphs},
    }


def _assistant_entry(text: str, created_at: int) -> dict:
    return {
        "type": "message",
        "message": {"role": "assistant", "parts": [{"type": "text", "text": text}]},
        "createdTimestamp": created_at,
    }


def _visible_message(entry: dict) -> BionicMessage | None:
    """Только то, что видит человек: без служебных вставок, рассуждений и вызовов инструментов."""
    if entry.get("type") != "message" or entry.get("hidden"):
        return None
    message = entry["message"]
    if message.get("role") not in ("user", "assistant"):
        return None
    text = "".join(
        part.get("text", "") for part in message.get("parts", ()) if part.get("type") == "text"
    ).strip()
    if not text:
        return None
    return BionicMessage(message["role"], text, entry.get("createdTimestamp", 0))


def _title(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= TITLE_LENGTH else f"{line[:TITLE_LENGTH - 1]}…"
