"""Проверки без сети: оформление, доступ, сборка контекста, очередь."""
import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document

from bot import context
from bot.config import Config
from bot.db import Database, Message
from bot.documents import UnsupportedFile, extract_text
from bot.handlers import _parse_target
from bot.llm import Delta, LLMError, ModelInfo
from bot.render import split_markdown, to_html
from bot.worker import ACTIVE_MODEL_KEY, Submit, Worker


def make_config(tmp_path: Path, **overrides) -> Config:
    values = dict(
        bot_token="x", admin_username="boss", lm_base_url="http://x", lms_path="lms",
        default_model="", context_length=1000, reply_max_tokens=200, summary_max_tokens=100,
        idle_unload_minutes=15, db_path=tmp_path / "t.db",
    )
    return Config(**{**values, **overrides})


# --- Оформление ---

def test_html_escapes_and_formats():
    html = to_html("**жирно** и `a<b` и 2 < 3\n# Заголовок\n* пункт")
    assert "<b>жирно</b>" in html
    assert "<code>a&lt;b</code>" in html
    assert "2 &lt; 3" in html
    assert "<b>Заголовок</b>" in html
    assert "• пункт" in html


def test_html_code_block_keeps_markup_literal():
    html = to_html("```python\nprint('**x**' < 1)\n```")
    assert html == "<pre><code class=\"language-python\">print('**x**' &lt; 1)</code></pre>"


def test_html_unclosed_fence_while_streaming():
    assert to_html("```\nfoo").startswith("<pre>foo")


def test_html_rejects_bad_language_name():
    assert "class" not in to_html('```"><script>\nx\n```')


def test_split_reopens_code_block():
    text = "```python\n" + "\n".join(f"line {i}" for i in range(100)) + "\n```"
    parts = split_markdown(text, limit=200)
    assert len(parts) > 1
    assert all(part.count("```") % 2 == 0 for part in parts)
    assert all(part.startswith("```python") for part in parts)


def test_split_keeps_sentences_whole():
    sentences = [f"Предложение номер {i} с небольшим хвостом." for i in range(10)]
    text = " ".join(sentences)
    parts = split_markdown(text, limit=80)
    assert len(parts) > 1
    for part in parts[:-1]:
        assert part.endswith(".")
    for sentence in sentences:
        assert any(sentence in part for part in parts)


def test_single_overlong_sentence_is_split_by_words():
    text = "слово " * 400
    parts = split_markdown(text, limit=100)
    assert len(parts) > 1
    assert all(len(part) <= 100 for part in parts)
    assert "слово" in parts[0] and "слово" in parts[-1]


def test_split_is_stable_while_text_grows():
    text = "\n".join(f"строка {i}" for i in range(300))
    first = split_markdown(text[:2000], limit=500)
    second = split_markdown(text, limit=500)
    assert second[: len(first) - 1] == first[:-1]


# --- Доступ ---

async def test_stranger_has_no_access(db):
    assert await db.resolve(1, "stranger", "S") == (None, False)
    assert await db.resolve(2, None, "No tag") == (None, False)


async def test_grant_binds_on_first_contact_case_insensitive(db):
    await db.grant("friend")
    user, is_new = await db.resolve(10, "FrIend", "F")
    assert user.tg_id == 10 and is_new
    user, is_new = await db.resolve(10, "friend", "F")
    assert user is not None and not is_new


async def test_old_tag_does_not_pass_access_to_new_owner(db):
    await db.grant("friend")
    await db.resolve(10, "friend", "F")
    await db.resolve(10, "renamed", "F")
    assert (await db.resolve(11, "friend", "Other"))[0] is None
    assert (await db.resolve(10, "renamed", "F"))[0] is not None


async def test_tag_taken_after_binding_is_not_shared(db):
    await db.grant("friend")
    await db.resolve(10, "friend", "F")
    assert (await db.resolve(11, "friend", "Impostor"))[0] is None


async def test_admin_binds_by_username(db):
    await db.ensure_admin("boss")
    user, _ = await db.resolve(5, "Boss", "B")
    assert user.is_admin
    await db.grant("friend")
    friend, _ = await db.resolve(6, "friend", "F")
    assert not friend.is_admin


async def test_grant_by_numeric_id_and_repeat(db):
    assert await db.grant("777")
    assert not await db.grant("777")
    assert (await db.resolve(777, None, "N"))[0] is not None


async def test_revoke_deletes_history(db):
    await db.grant("friend")
    user, _ = await db.resolve(10, "friend", "F")
    await db.add_exchange(user.id, "q", [], "a")
    await db.delete_user(user.id)
    assert (await db.resolve(10, "friend", "F"))[0] is None
    assert await db.load_chat(user.id) == ("", [])


async def test_ensure_loaded_revives_dead_server():
    from bot.llm import BOT_INSTANCE, LMStudio, ModelInfo

    llm = LMStudio("http://127.0.0.1:1234", "lms", 16384)
    events: list[str] = []
    alive = {"value": False}

    async def fake_alive():
        return alive["value"]

    async def fake_lms(*args):
        events.append(" ".join(args))
        if args[0] == "server":
            alive["value"] = True

    async def fake_list():
        # После запуска сервера моделей ещё нет в памяти: бот должен её загрузить.
        return [ModelInfo("m", "M", False, ())]

    llm._server_alive = fake_alive
    llm._lms = fake_lms
    llm.list_models = fake_list
    try:
        instance = await llm.ensure_loaded("m")
    finally:
        await llm.close()

    assert instance == BOT_INSTANCE
    assert events[0] == "server start -p 1234"
    assert any(event.startswith("load m") for event in events)


def test_parse_target():
    assert _parse_target("@FriEnd") == "friend"
    assert _parse_target("https://t.me/friend") == "friend"
    assert _parse_target("123") == "123"
    assert _parse_target(None) is None


# --- Контекст ---

def test_chats_are_isolated_by_user_in_payload():
    history = [Message(1, "user", "вопрос"), Message(2, "assistant", "ответ")]
    payload = context.build_messages("", history, "ещё", [], False)
    assert [m["role"] for m in payload] == ["user", "assistant", "user"]


def test_no_system_prompt_without_summary():
    payload = context.build_messages("", [], "привет", [], False)
    assert payload == [{"role": "user", "content": "привет"}]


def test_summary_goes_first():
    payload = context.build_messages("сводка", [], "привет", [], False)
    assert payload[0]["role"] == "system" and "сводка" in payload[0]["content"]


def test_recent_image_is_remembered_only_with_vision():
    history = [Message(1, "user", "что тут?", ["AAA"]), Message(2, "assistant", "кот")]
    with_vision = context.build_messages("", history, "какого цвета?", [], True)
    assert isinstance(with_vision[0]["content"], list)
    without = context.build_messages("", history, "какого цвета?", [], False)
    assert isinstance(without[0]["content"], str)


def test_new_image_replaces_remembered_one():
    history = [Message(1, "user", "что тут?", ["AAA"]), Message(2, "assistant", "кот")]
    payload = context.build_messages("", history, "а тут?", ["BBB"], True)
    assert isinstance(payload[0]["content"], str)
    assert isinstance(payload[-1]["content"], list)


def test_take_oldest_keeps_tail():
    history = [Message(i, "user", "x" * 30) for i in range(5)]
    chunk = context.take_oldest(history, 10_000)
    assert [m.id for m in chunk] == [0, 1, 2]
    assert context.take_oldest(history[:2], 10_000) == []


# --- Файлы ---

def test_text_file_cp1251():
    assert extract_text("a.txt", "привет".encode("cp1251")) == "привет"


def test_pdf_text_is_extracted():
    stream = b"BT /F1 24 Tf 20 100 Td (Hello PDF) Tj ET"
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>stream\n%s\nendstream" % (len(stream), stream),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(pdf)
    pdf += b"xref\n0 6\n0000000000 65535 f \n"
    for offset in offsets:
        pdf += b"%010d 00000 n \n" % offset
    pdf += b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % xref

    assert "Hello PDF" in extract_text("a.pdf", bytes(pdf))


def test_docx_reads_paragraphs_and_tables():
    document = Document()
    document.add_paragraph("абзац")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "ячейка1"
    table.rows[0].cells[1].text = "ячейка2"
    buffer = BytesIO()
    document.save(buffer)
    text = extract_text("a.docx", buffer.getvalue())
    assert "абзац" in text and "ячейка1 | ячейка2" in text


@pytest.mark.parametrize("name,data", [
    ("a.doc", b"x"), ("a.bin", b"\x00\x01"), ("a.txt", b"   "), ("a.pdf", b"not a pdf"),
])
def test_unsupported_files(name, data):
    with pytest.raises(UnsupportedFile):
        extract_text(name, data)


# --- Очередь ---

class FakeReply:
    def __init__(self):
        self.statuses, self.updates, self.final = [], [], None

    async def status(self, text):
        self.statuses.append(text)

    async def update(self, text):
        self.updates.append(text)

    async def finish(self, text):
        self.final = text


class FakeLLM:
    owns_loaded_model = False

    def __init__(self, vision=False):
        self.models = [ModelInfo("a", "Model A", vision, ())]
        self.gate = asyncio.Event()
        self.gate.set()
        self.requests = []
        self.summaries = 0
        self.loaded = []
        self.error = None
        self.calls = []

    async def start_server(self):
        self.calls.append("start_server")

    async def list_models(self):
        self.calls.append("list_models")
        return self.models

    def is_loaded(self, model):
        return False

    async def ensure_loaded(self, key):
        self.loaded.append(key)
        return key

    async def stream_chat(self, model, messages, max_tokens):
        self.requests.append(messages)
        if self.error:
            raise self.error
        yield Delta(reasoning="скрытое рассуждение")
        yield Delta(content="ответ ")
        await self.gate.wait()
        yield Delta(content="готов", finish_reason="stop")

    async def complete(self, model, messages, max_tokens):
        self.summaries += 1
        return "сводка"


@pytest.fixture
async def rig(db, tmp_path):
    llm = FakeLLM()
    await db.grant("a")
    await db.grant("b")
    user_a, _ = await db.resolve(1, "a", "A")
    user_b, _ = await db.resolve(2, "b", "B")
    worker = Worker(llm, db, make_config(tmp_path))
    task = asyncio.create_task(worker.run())
    yield worker, llm, db, user_a, user_b
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def wait_until(condition):
    async with asyncio.timeout(3):
        while not condition():
            await asyncio.sleep(0.01)


async def test_server_is_started_before_models_are_queried(rig):
    worker, llm, db, a, _ = rig
    reply = FakeReply()
    await worker.submit(a.id, "вопрос", [], lambda: reply)
    await wait_until(lambda: reply.final)
    assert llm.calls[0] == "start_server"
    assert llm.calls.index("start_server") < llm.calls.index("list_models")


async def test_answer_saved_and_reasoning_hidden(rig):
    worker, llm, db, a, _ = rig
    reply = FakeReply()
    await worker.submit(a.id, "вопрос", [], lambda: reply)
    await wait_until(lambda: reply.final)
    assert reply.final == "ответ готов"
    assert "рассуждение" not in " ".join(reply.updates)
    _, history = await db.load_chat(a.id)
    assert [(m.role, m.content) for m in history] == [
        ("user", "вопрос"), ("assistant", "ответ готов"),
    ]


async def test_length_finish_adds_no_truncation_note(rig):
    worker, llm, db, a, _ = rig

    async def long_stream(model, messages, max_tokens):
        yield Delta(content="часть ответа", finish_reason="length")

    llm.stream_chat = long_stream
    reply = FakeReply()
    await worker.submit(a.id, "вопрос", [], lambda: reply)
    await wait_until(lambda: reply.final)
    assert reply.final == "часть ответа"
    assert "обрезан" not in reply.final


async def test_generation_gets_full_context_room(rig):
    worker, llm, db, a, _ = rig
    captured = {}
    original = llm.stream_chat

    async def capture(model, messages, max_tokens):
        captured["max"] = max_tokens
        async for delta in original(model, messages, max_tokens):
            yield delta

    llm.stream_chat = capture
    reply = FakeReply()
    await worker.submit(a.id, "короткий вопрос", [], lambda: reply)
    await wait_until(lambda: reply.final)
    # context_length=1000, запрос крошечный: модели достаётся куда больше floor-а 200.
    assert 200 < captured["max"] <= 1000


async def test_contexts_do_not_mix_between_users(rig):
    worker, llm, db, a, b = rig
    first, second = FakeReply(), FakeReply()
    await worker.submit(a.id, "секрет А", [], lambda: first)
    await wait_until(lambda: first.final)
    await worker.submit(b.id, "вопрос Б", [], lambda: second)
    await wait_until(lambda: second.final)
    assert "секрет А" not in str(llm.requests[-1])


async def test_queue_positions_and_order(rig):
    worker, llm, db, a, b = rig
    llm.gate.clear()
    first, second = FakeReply(), FakeReply()
    await worker.submit(a.id, "1", [], lambda: first)
    await wait_until(lambda: first.updates)
    await worker.submit(b.id, "2", [], lambda: second)
    assert second.statuses == ["Ожидай, перед тобой в очереди 1 человек"]
    llm.gate.set()
    await wait_until(lambda: second.final)
    assert first.final and llm.requests[0][-1]["content"] == "1"
    assert not any("очереди 0" in status for status in second.statuses)


async def test_one_active_request_per_user(rig):
    worker, llm, db, a, _ = rig
    llm.gate.clear()
    reply = FakeReply()
    await worker.submit(a.id, "1", [], lambda: reply)
    await wait_until(lambda: reply.updates)
    assert await worker.submit(a.id, "2", [], FakeReply) is Submit.BUSY
    llm.gate.set()


async def test_second_message_merges_into_queued_job(rig):
    worker, llm, db, a, b = rig
    llm.gate.clear()
    blocker, reply = FakeReply(), FakeReply()
    await worker.submit(b.id, "занят", [], lambda: blocker)
    await wait_until(lambda: blocker.updates)
    await worker.submit(a.id, "часть 1", [], lambda: reply)
    assert await worker.submit(a.id, "часть 2", [], FakeReply) is Submit.MERGED
    llm.gate.set()
    await wait_until(lambda: reply.final)
    assert llm.requests[-1][-1]["content"] == "часть 1\n\nчасть 2"


async def test_stop_saves_partial_answer(rig):
    worker, llm, db, a, _ = rig
    llm.gate.clear()
    reply = FakeReply()
    await worker.submit(a.id, "вопрос", [], lambda: reply)
    await wait_until(lambda: reply.updates)
    assert await worker.cancel(a.id)
    assert "Остановлено" in reply.final and "ответ" in reply.final
    assert len((await db.load_chat(a.id))[1]) == 2


async def test_cancel_with_discard_saves_nothing(rig):
    worker, llm, db, a, _ = rig
    llm.gate.clear()
    reply = FakeReply()
    await worker.submit(a.id, "вопрос", [], lambda: reply)
    await wait_until(lambda: reply.updates)
    await worker.cancel(a.id, discard=True)
    assert (await db.load_chat(a.id))[1] == []


async def test_cancel_queued_job_refreshes_others(rig):
    worker, llm, db, a, b = rig
    llm.gate.clear()
    blocker, reply = FakeReply(), FakeReply()
    await worker.submit(b.id, "занят", [], lambda: blocker)
    await wait_until(lambda: blocker.updates)
    await worker.submit(a.id, "в очереди", [], lambda: reply)
    assert await worker.cancel(a.id)
    assert reply.final == "Остановлено"
    llm.gate.set()


async def test_worker_survives_llm_error(rig):
    worker, llm, db, a, b = rig
    llm.error = LLMError("упал")
    reply = FakeReply()
    await worker.submit(a.id, "1", [], lambda: reply)
    await wait_until(lambda: reply.final)
    assert "недоступна" in reply.final
    assert (await db.load_chat(a.id))[1] == []

    llm.error = None
    second = FakeReply()
    await worker.submit(b.id, "2", [], lambda: second)
    await wait_until(lambda: second.final)
    assert second.final == "ответ готов"


async def test_image_rejected_without_vision(rig):
    worker, llm, db, a, _ = rig
    reply = FakeReply()
    await worker.submit(a.id, "что тут?", ["AAA"], lambda: reply)
    await wait_until(lambda: reply.final)
    assert "не умеет" in reply.final and llm.requests == []


async def test_model_switch_applies_to_next_request(rig):
    worker, llm, db, a, _ = rig
    llm.models.append(ModelInfo("b", "Model B", False, ()))
    await db.set_setting(ACTIVE_MODEL_KEY, "b")
    reply = FakeReply()
    await worker.submit(a.id, "1", [], lambda: reply)
    await wait_until(lambda: reply.final)
    assert llm.loaded == ["b"]


async def test_long_history_is_compacted(rig):
    worker, llm, db, a, _ = rig
    for index in range(20):
        await db.add_exchange(a.id, f"вопрос {index} " * 30, [], f"ответ {index} " * 30)
    reply = FakeReply()
    await worker.submit(a.id, "новый", [], lambda: reply)
    await wait_until(lambda: reply.final)

    assert llm.summaries >= 1
    assert any("Сжимаю" in status for status in reply.statuses)
    summary, history = await db.load_chat(a.id)
    assert summary == "сводка"
    assert history[-1].content == "ответ готов"
    assert llm.requests[-1][0]["role"] == "system"
    used = context.total_tokens(summary, history, "", [])
    assert used <= 800


async def test_history_survives_idle_unload_and_restart(tmp_path):
    path = tmp_path / "t.db"
    database = Database(path)
    await database.connect()
    await database.grant("a")
    user, _ = await database.resolve(1, "a", "A")

    llm = FakeLLM()
    llm.owns_loaded_model = True
    llm.unloads = 0

    async def unload():
        llm.owns_loaded_model = False
        llm.unloads += 1

    llm.unload_own_model = unload
    config = make_config(tmp_path, idle_unload_minutes=0.001)
    worker = Worker(llm, database, config)
    task = asyncio.create_task(worker.run())

    first = FakeReply()
    await worker.submit(user.id, "запомни 7", [], lambda: first)
    await wait_until(lambda: first.final)
    await wait_until(lambda: llm.unloads == 1)

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await database.close()

    database = Database(path)
    await database.connect()
    worker = Worker(llm, database, config)
    task = asyncio.create_task(worker.run())
    second = FakeReply()
    await worker.submit(user.id, "какое число?", [], lambda: second)
    await wait_until(lambda: second.final)
    assert "запомни 7" in str(llm.requests[-1])

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await database.close()


async def test_empty_summary_warns_and_still_answers(rig):
    worker, llm, db, a, _ = rig

    async def empty(model, messages, max_tokens):
        return ""

    llm.complete = empty
    for index in range(20):
        await db.add_exchange(a.id, f"вопрос {index} " * 30, [], f"ответ {index} " * 30)
    reply = FakeReply()
    await worker.submit(a.id, "новый", [], lambda: reply)
    await wait_until(lambda: reply.final)
    assert "забыто" in reply.final and "ответ готов" in reply.final
