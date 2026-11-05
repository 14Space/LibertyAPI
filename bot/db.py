"""Хранилище: пользователи, текущий чат каждого пользователя и настройки."""
import json
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id      INTEGER UNIQUE,
    username   TEXT UNIQUE,
    name       TEXT,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    summary    TEXT NOT NULL DEFAULT '',
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen  TEXT,
    bionic_session_id TEXT,
    bionic_synced_at  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role    TEXT NOT NULL,
    content TEXT NOT NULL,
    images  TEXT
);
CREATE INDEX IF NOT EXISTS messages_user ON messages(user_id, id);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class User:
    id: int
    tg_id: int | None
    username: str | None
    name: str | None
    is_admin: bool
    last_seen: str | None


@dataclass(frozen=True)
class Message:
    id: int
    role: str
    content: str
    images: list[str] = field(default_factory=list)


class Database:
    def __init__(self, path: Path):
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.executescript(SCHEMA)
        await self._add_missing_columns()
        await self._conn.commit()

    async def _add_missing_columns(self) -> None:
        """Обновляет базу, созданную прошлой версией бота."""
        rows = await self._conn.execute_fetchall("PRAGMA table_info(users)")
        existing = {row["name"] for row in rows}
        if "bionic_session_id" not in existing:
            await self._conn.execute("ALTER TABLE users ADD COLUMN bionic_session_id TEXT")
        if "bionic_synced_at" not in existing:
            await self._conn.execute(
                "ALTER TABLE users ADD COLUMN bionic_synced_at INTEGER NOT NULL DEFAULT 0"
            )

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    # --- Доступ ---

    async def ensure_admin(self, username: str) -> None:
        """Назначает админа по тегу. Числовой id привяжется при его первом сообщении."""
        await self._conn.execute(
            "INSERT INTO users (username, is_admin) VALUES (?, 1) "
            "ON CONFLICT(username) DO UPDATE SET is_admin = 1",
            (username,),
        )
        await self._conn.execute(
            "UPDATE users SET is_admin = 0 WHERE username IS NOT ? AND is_admin = 1",
            (username,),
        )
        await self._conn.commit()

    async def resolve(self, tg_id: int, username: str | None, name: str) -> tuple[User | None, bool]:
        """Находит пользователя с доступом. Второе значение: привязан ли он только что.

        Запись по тегу привязывается к id только один раз. Если человек сменит тег,
        а его старый займёт кто-то другой, доступ за новым владельцем тега не закрепится.
        """
        tag = username.lower() if username else None

        row = await self._fetch_one("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        if row:
            if tag != row["username"] and tag:
                await self._conn.execute(
                    "DELETE FROM users WHERE username = ? AND tg_id IS NULL AND id != ?",
                    (tag, row["id"]),
                )
                await self._conn.execute(
                    "UPDATE users SET username = NULL WHERE username = ? AND id != ?",
                    (tag, row["id"]),
                )
            await self._conn.execute(
                "UPDATE users SET username = ?, name = ?, last_seen = datetime('now') WHERE id = ?",
                (tag, name, row["id"]),
            )
            await self._conn.commit()
            return await self._user(row["id"]), False

        if not tag:
            return None, False
        row = await self._fetch_one(
            "SELECT * FROM users WHERE username = ? AND tg_id IS NULL", (tag,)
        )
        if not row:
            return None, False
        await self._conn.execute(
            "UPDATE users SET tg_id = ?, name = ?, last_seen = datetime('now') WHERE id = ?",
            (tg_id, name, row["id"]),
        )
        await self._conn.commit()
        return await self._user(row["id"]), True

    async def grant(self, target: str) -> bool:
        """Выдаёт доступ по тегу или числовому id. False, если доступ уже есть."""
        column = "tg_id" if target.isdigit() else "username"
        value = int(target) if target.isdigit() else target
        cursor = await self._conn.execute(
            f"INSERT OR IGNORE INTO users ({column}) VALUES (?)", (value,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def find(self, target: str) -> User | None:
        column = "tg_id" if target.isdigit() else "username"
        row = await self._fetch_one(
            f"SELECT * FROM users WHERE {column} = ?",
            (int(target) if target.isdigit() else target,),
        )
        return self._to_user(row) if row else None

    async def delete_user(self, user_id: int) -> None:
        """Забирает доступ и удаляет историю пользователя."""
        await self._conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await self._conn.commit()

    async def list_users(self) -> list[User]:
        rows = await self._conn.execute_fetchall(
            "SELECT * FROM users ORDER BY is_admin DESC, id"
        )
        return [self._to_user(row) for row in rows]

    # --- Чат ---

    async def load_chat(self, user_id: int) -> tuple[str, list[Message]]:
        row = await self._fetch_one("SELECT summary FROM users WHERE id = ?", (user_id,))
        rows = await self._conn.execute_fetchall(
            "SELECT * FROM messages WHERE user_id = ? ORDER BY id", (user_id,)
        )
        messages = [
            Message(r["id"], r["role"], r["content"], json.loads(r["images"] or "[]"))
            for r in rows
        ]
        return row["summary"] if row else "", messages

    async def add_exchange(
        self, user_id: int, question: str, images: list[str], answer: str
    ) -> None:
        await self._conn.execute(
            "INSERT INTO messages (user_id, role, content, images) VALUES (?, 'user', ?, ?)",
            (user_id, question, json.dumps(images) if images else None),
        )
        await self._conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, 'assistant', ?)",
            (user_id, answer),
        )
        await self._conn.commit()

    async def apply_compaction(self, user_id: int, summary: str, up_to_id: int) -> None:
        """Заменяет сообщения до up_to_id включительно их пересказом."""
        await self._conn.execute(
            "UPDATE users SET summary = ? WHERE id = ?", (summary, user_id)
        )
        await self._conn.execute(
            "DELETE FROM messages WHERE user_id = ? AND id <= ?", (user_id, up_to_id)
        )
        await self._conn.commit()

    async def reset_chat(self, user_id: int) -> None:
        """Забывает текущий разговор. Чат в Bionic остаётся, следующий начнётся отдельно."""
        await self._conn.execute(
            "UPDATE users SET summary = '', bionic_session_id = NULL, bionic_synced_at = 0 "
            "WHERE id = ?",
            (user_id,),
        )
        await self._conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        await self._conn.commit()

    # --- Связь с Bionic ---

    async def bionic_link(self, user_id: int) -> tuple[bool, str | None, int]:
        """Возвращает: админ ли это, id чата в Bionic и время последней синхронизации."""
        row = await self._fetch_one(
            "SELECT is_admin, bionic_session_id, bionic_synced_at FROM users WHERE id = ?",
            (user_id,),
        )
        if row is None:
            return False, None, 0
        return bool(row["is_admin"]), row["bionic_session_id"], row["bionic_synced_at"]

    async def set_bionic_link(self, user_id: int, session_id: str, synced_at: int) -> None:
        await self._conn.execute(
            "UPDATE users SET bionic_session_id = ?, bionic_synced_at = ? WHERE id = ?",
            (session_id, synced_at, user_id),
        )
        await self._conn.commit()

    async def add_message(self, user_id: int, role: str, content: str) -> None:
        await self._conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        await self._conn.commit()

    # --- Настройки ---

    async def get_setting(self, key: str) -> str | None:
        row = await self._fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()

    # --- Служебное ---

    async def _fetch_one(self, sql: str, params: tuple):
        async with self._conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def _user(self, user_id: int) -> User:
        row = await self._fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
        return self._to_user(row)

    @staticmethod
    def _to_user(row) -> User:
        return User(
            id=row["id"],
            tg_id=row["tg_id"],
            username=row["username"],
            name=row["name"],
            is_admin=bool(row["is_admin"]),
            last_seen=row["last_seen"],
        )
