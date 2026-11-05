"""Общие чаты: запись и чтение на копии устройства базы Bionic, без настоящего приложения."""
import asyncio
import json
import sqlite3

import pytest

from bot.bionic import BionicChats, BionicError
from bot.db import Database
from bot.worker import BIONIC_FAILURE_NOTE, IMAGE_MARK, Worker
from test_units import FakeLLM, FakeReply, make_config, wait_until

SCHEMA = """
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY, session_name TEXT, committed_head_entry_id TEXT,
    session_json TEXT NOT NULL, updated_timestamp INTEGER NOT NULL, user_draft_json TEXT,
    is_transient INTEGER NOT NULL DEFAULT 0, suggested_session_name TEXT,
    has_unread INTEGER NOT NULL DEFAULT 0, is_temporary INTEGER NOT NULL DEFAULT 0,
    ref_count INTEGER NOT NULL DEFAULT 0, parent_session_id TEXT);
CREATE TABLE chat_entries (
    id TEXT PRIMARY KEY, previous_id TEXT, entry_json TEXT NOT NULL,
    ref_count INTEGER NOT NULL DEFAULT 0);
"""
TEMPLATE = {
    "baseSystemPrompt": "агент", "ngModuleSpecifiers": [{"identifier": "x"}],
    "sessionParams": {"working_directory": "чужая папка"},
    "queuedUserTurns": ["старый"], "contextEstimation": {"before": 5, "self": 5, "total": 10},
}


@pytest.fixture
def base(tmp_path):
    root = tmp_path / "bionic"
    (root / ".internal").mkdir(parents=True)
    entries = []
    for name, identifier in (("LibertyAPI", "p1"), ("Другой", "p2")):
        project = root / "projects" / identifier
        (project / ".internal").mkdir(parents=True)
        (project / "project.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        database = sqlite3.connect(project / ".internal" / "ng-sessions.sqlite")
        database.executescript(SCHEMA)
        database.close()
        entries.append([identifier, {
            "path": str(project), "projectWorkingDirectoryPath": rf"C:\work\{identifier}",
        }])
    registry = {"json": {"projects": entries}}
    (root / ".internal" / "projects-registry.json").write_text(
        json.dumps(registry), encoding="utf-8"
    )
    return root


def project_db(base, identifier="p1"):
    connection = sqlite3.connect(base / "projects" / identifier / ".internal" / "ng-sessions.sqlite")
    connection.row_factory = sqlite3.Row
    return connection


def add_template(base, identifier="p1"):
    connection = project_db(base, identifier)
    connection.execute(
        "INSERT INTO sessions (session_id, session_json, updated_timestamp) VALUES ('t', ?, 1)",
        (json.dumps(TEMPLATE),),
    )
    connection.commit()
    connection.close()


def chain(base, session_id):
    connection = project_db(base)
    head = connection.execute(
        "SELECT committed_head_entry_id FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    entries = []
    while head:
        row = connection.execute(
            "SELECT previous_id, entry_json FROM chat_entries WHERE id = ?", (head,)
        ).fetchone()
        entries.append(json.loads(row[1]))
        head = row[0]
    connection.close()
    return list(reversed(entries))


def write_as_bionic(base, session_id, role, text, created_at):
    """Запись так, как её делает само приложение: новая голова поверх текущей."""
    connection = project_db(base)
    head = connection.execute(
        "SELECT committed_head_entry_id FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    entry_id = f"bionic-{created_at}"
    entry = {
        "id": entry_id, "previousId": head, "type": "message", "createdTimestamp": created_at,
        "message": {"role": role, "parts": [
            {"type": "reasoning", "text": "скрытое"}, {"type": "text", "text": text},
        ]},
    }
    connection.execute(
        "INSERT INTO chat_entries (id, previous_id, entry_json) VALUES (?, ?, ?)",
        (entry_id, head, json.dumps(entry, ensure_ascii=False)),
    )
    connection.execute(
        "UPDATE sessions SET committed_head_entry_id = ? WHERE session_id = ?",
        (entry_id, session_id),
    )
    connection.commit()
    connection.close()


async def test_unknown_project_is_reported(base):
    with pytest.raises(BionicError):
        BionicChats("Нет такого", base)


async def test_new_chat_uses_project_folder_and_clean_template(base):
    add_template(base)
    session_id, _ = await BionicChats("LibertyAPI", base).append_exchange(None, "вопрос", "ответ")

    row = project_db(base).execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    config = json.loads(row["session_json"])
    assert config["sessionParams"]["working_directory"] == r"C:\work\p1"
    assert config["baseSystemPrompt"] == "агент"
    assert config["queuedUserTurns"] == []
    assert row["suggested_session_name"] == "вопрос"
    assert row["is_temporary"] == 0 and row["is_transient"] == 0 and row["has_unread"] == 1


async def test_template_is_taken_from_other_project_when_own_is_empty(base):
    add_template(base, "p2")
    session_id, _ = await BionicChats("LibertyAPI", base).append_exchange(None, "в", "о")
    assert session_id


async def test_no_template_rolls_back_cleanly(base):
    with pytest.raises(BionicError):
        await BionicChats("LibertyAPI", base).append_exchange(None, "в", "о")
    connection = project_db(base)
    assert connection.execute("SELECT count(*) FROM chat_entries").fetchone()[0] == 0


async def test_entries_form_a_chain_in_bionic_format(base):
    add_template(base)
    session_id, _ = await BionicChats("LibertyAPI", base).append_exchange(
        None, "строка 1\n\nстрока 3", "ответ"
    )
    user, assistant, fork = chain(base, session_id)

    assert "previousId" not in user
    assert assistant["previousId"] == user["id"] and fork["previousId"] == assistant["id"]
    assert user["message"] == {
        "role": "user", "parts": [{"type": "text", "text": "строка 1\n\nстрока 3"}],
    }
    assert user["rawMessage"]["content"][1] == {"type": "paragraph"}
    assert assistant["message"]["parts"] == [{"type": "text", "text": "ответ"}]
    assert fork == {
        "id": fork["id"], "previousId": assistant["id"],
        "type": "forkPoint", "forkPoint": "assistantTurnEnd",
    }


async def test_second_exchange_continues_after_bionic_entries(base):
    add_template(base)
    chats = BionicChats("LibertyAPI", base)
    session_id, _ = await chats.append_exchange(None, "1", "2")
    write_as_bionic(base, session_id, "user", "с ноутбука", 9_999_999_999_999)
    await chats.append_exchange(session_id, "3", "4")

    texts = [e["message"]["parts"][-1]["text"] for e in chain(base, session_id) if "message" in e]
    assert texts == ["1", "2", "с ноутбука", "3", "4"]


async def test_chat_deleted_in_bionic_starts_a_new_one(base):
    add_template(base)
    chats = BionicChats("LibertyAPI", base)
    session_id, _ = await chats.append_exchange(None, "1", "2")
    connection = project_db(base)
    connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    connection.commit()
    connection.close()

    new_id, _ = await chats.append_exchange(session_id, "3", "4")
    assert new_id != session_id


async def test_read_since_returns_only_new_visible_text(base):
    add_template(base)
    chats = BionicChats("LibertyAPI", base)
    session_id, synced_at = await chats.append_exchange(None, "из телефона", "ответ")
    write_as_bionic(base, session_id, "user", "с ноутбука", synced_at + 10)
    write_as_bionic(base, session_id, "assistant", "ответ на ноутбуке", synced_at + 20)
    write_as_bionic(base, session_id, "tool", "результат", synced_at + 30)

    messages = await chats.read_since(session_id, synced_at)
    assert [(m.role, m.text) for m in messages] == [
        ("user", "с ноутбука"), ("assistant", "ответ на ноутбуке"),
    ]


async def test_backup_is_a_readable_copy(base, tmp_path):
    add_template(base)
    target = tmp_path / "backup" / "copy.sqlite"
    await BionicChats("LibertyAPI", base).backup(target)
    assert sqlite3.connect(target).execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


# --- Вместе с очередью ---

@pytest.fixture
async def rig(db, base, tmp_path):
    add_template(base)
    await db.ensure_admin("boss")
    await db.grant("friend")
    admin, _ = await db.resolve(1, "boss", "B")
    friend, _ = await db.resolve(2, "friend", "F")
    llm = FakeLLM()
    worker = Worker(llm, db, make_config(tmp_path), BionicChats("LibertyAPI", base))
    task = asyncio.create_task(worker.run())
    yield worker, llm, db, base, admin, friend
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def ask(worker, user, text, images=()):
    reply = FakeReply()
    await worker.submit(user.id, text, list(images), lambda: reply)
    await wait_until(lambda: reply.final and worker.current_user_id is None)
    return reply


async def test_laptop_messages_reach_model_on_next_phone_request(rig):
    worker, llm, db, base, admin, _ = rig
    await ask(worker, admin, "с телефона")
    _, session_id, synced_at = await db.bionic_link(admin.id)
    write_as_bionic(base, session_id, "user", "дописал на ноутбуке", synced_at + 10)
    write_as_bionic(base, session_id, "assistant", "ответ на ноутбуке", synced_at + 20)

    await ask(worker, admin, "снова с телефона")
    payload = str(llm.requests[-1])
    assert "дописал на ноутбуке" in payload and "ответ на ноутбуке" in payload

    await ask(worker, admin, "ещё раз")
    assert str(llm.requests[-1]).count("дописал на ноутбуке") == 1


async def test_friend_chat_stays_out_of_bionic(rig):
    worker, llm, db, base, _, friend = rig
    await ask(worker, friend, "личное")
    assert await db.bionic_link(friend.id) == (False, None, 0)
    connection = project_db(base)
    assert connection.execute("SELECT count(*) FROM chat_entries").fetchone()[0] == 0


async def test_new_starts_separate_chat_and_keeps_old_one(rig):
    worker, llm, db, base, admin, _ = rig
    await ask(worker, admin, "первый чат")
    _, first_id, _ = await db.bionic_link(admin.id)
    await db.reset_chat(admin.id)
    await ask(worker, admin, "второй чат")
    _, second_id, _ = await db.bionic_link(admin.id)

    assert second_id != first_id
    assert "первый чат" not in str(llm.requests[-1])
    assert chain(base, first_id)[0]["message"]["parts"][0]["text"] == "первый чат"


async def test_photo_is_marked_in_bionic(rig):
    worker, llm, db, base, admin, _ = rig
    llm.models[0] = type(llm.models[0])("a", "A", True, ())
    await ask(worker, admin, "что тут?", ["AAA"])
    _, session_id, _ = await db.bionic_link(admin.id)
    assert chain(base, session_id)[0]["message"]["parts"][0]["text"] == f"{IMAGE_MARK}\nчто тут?"


async def test_bionic_failure_does_not_lose_answer(rig):
    worker, llm, db, base, admin, _ = rig
    (base / "projects" / "p1" / ".internal" / "ng-sessions.sqlite").unlink()
    reply = await ask(worker, admin, "вопрос")
    assert "ответ готов" in reply.final and BIONIC_FAILURE_NOTE in reply.final
    assert len((await db.load_chat(admin.id))[1]) == 2


async def test_old_bot_database_is_upgraded(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER UNIQUE, "
        "username TEXT UNIQUE, name TEXT, is_admin INTEGER NOT NULL DEFAULT 0, "
        "summary TEXT NOT NULL DEFAULT '', granted_at TEXT, last_seen TEXT)"
    )
    old.execute("INSERT INTO users (tg_id, username, is_admin) VALUES (1, 'boss', 1)")
    old.commit()
    old.close()

    database = Database(path)
    await database.connect()
    assert await database.bionic_link(1) == (True, None, 0)
    await database.close()
