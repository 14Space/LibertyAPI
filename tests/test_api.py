"""API для других ботов (bot/api.py): ключ, формат OpenAI, общая очередь, срок ответа."""
import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.api import build_app, client_name, serve_api
from bot.config import parse_api_keys
from bot.llm import LLMError
from bot.worker import Worker
from test_units import FakeLLM, FakeReply, make_config, wait_until

KEY = "k" * 32
AUTH = {"Authorization": f"Bearer {KEY}"}
QUESTION = {"messages": [
    {"role": "system", "content": "будь краток"},
    {"role": "user", "content": "привет"},
]}


@pytest.fixture
async def api(db, tmp_path):
    llm = FakeLLM()
    config = make_config(tmp_path, api_host="127.0.0.1", api_keys={"viakitbot": KEY}, api_timeout=1)
    worker = Worker(llm, db, config)
    task = asyncio.create_task(worker.run())
    client = TestClient(TestServer(build_app(worker, config)))
    await client.start_server()
    yield client, worker, llm, db
    await client.close()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_wrong_key_is_rejected_and_nothing_queued(api):
    client, worker, llm, _ = api
    for headers in ({}, {"Authorization": "Bearer чужой"}, {"Authorization": KEY}):
        response = await client.post("/v1/chat/completions", json=QUESTION, headers=headers)
        assert response.status == 401
    assert llm.requests == [] and worker.queue_size == 0


async def test_answer_in_openai_format_and_nothing_stored(api):
    client, _, llm, db = api
    response = await client.post("/v1/chat/completions", json=QUESTION, headers=AUTH)
    assert response.status == 200
    data = await response.json()
    assert data["choices"][0]["message"] == {"role": "assistant", "content": "ответ готов"}
    assert data["model"] == "a"
    # Разговор уходит модели как есть: своя история у другого бота уже внутри.
    assert llm.requests == [QUESTION["messages"]]
    assert await db.list_users() == []


@pytest.mark.parametrize("body", [
    {},
    {"messages": []},
    {"messages": [{"role": "user"}]},
    {"messages": [{"role": "tool", "content": "x"}]},
    {"messages": [{"role": "user", "content": [{"type": "image_url"}]}]},
])
async def test_bad_body_is_400(api, body):
    client, *_ = api
    response = await client.post("/v1/chat/completions", json=body, headers=AUTH)
    assert response.status == 400


async def test_too_long_conversation_is_413(api):
    client, _, llm, _ = api
    body = {"messages": [{"role": "user", "content": "слово " * 2000}]}
    response = await client.post("/v1/chat/completions", json=body, headers=AUTH)
    assert response.status == 413
    assert llm.requests == []


async def test_reply_length_is_capped(api):
    client, _, llm, _ = api
    captured = {}
    original = llm.stream_chat

    async def capture(model, messages, max_tokens):
        captured["max"] = max_tokens
        async for delta in original(model, messages, max_tokens):
            yield delta

    llm.stream_chat = capture
    body = {**QUESTION, "max_tokens": 10_000}
    assert (await client.post("/v1/chat/completions", json=body, headers=AUTH)).status == 200
    assert captured["max"] == 200          # не больше REPLY_MAX_TOKENS из настроек

    # Короткий ответ – законная просьба, а не «слишком длинный разговор» (было 413).
    body = {**QUESTION, "max_tokens": 20}
    assert (await client.post("/v1/chat/completions", json=body, headers=AUTH)).status == 200
    assert captured["max"] == 20


async def test_model_error_is_503_and_worker_survives(api):
    client, _, llm, _ = api
    llm.error = LLMError("упал")
    response = await client.post("/v1/chat/completions", json=QUESTION, headers=AUTH)
    assert response.status == 503
    llm.error = None
    response = await client.post("/v1/chat/completions", json=QUESTION, headers=AUTH)
    assert response.status == 200


async def test_api_waits_in_the_same_queue_as_people(api):
    client, worker, llm, db = api
    await db.grant("a")
    user, _ = await db.resolve(1, "a", "A")
    llm.gate.clear()
    person = FakeReply()
    await worker.submit(user.id, "вопрос человека", [], lambda: person)
    await wait_until(lambda: person.updates)

    request = asyncio.create_task(
        client.post("/v1/chat/completions", json=QUESTION, headers=AUTH)
    )
    await wait_until(lambda: worker.queue_size == 2)
    assert len(llm.requests) == 1          # модель занята человеком, API ждёт своей очереди
    llm.gate.set()
    response = await request
    assert response.status == 200
    assert person.final == "ответ готов"


async def test_timeout_drops_the_job(api):
    client, worker, llm, _ = api
    llm.gate.clear()                        # модель «думает» дольше api_timeout=1с
    response = await client.post("/v1/chat/completions", json=QUESTION, headers=AUTH)
    assert response.status == 504
    assert worker.queue_size == 0           # запрос снят, модель не работает впустую

    llm.gate.set()
    response = await client.post("/v1/chat/completions", json=QUESTION, headers=AUTH)
    assert response.status == 200


async def test_queued_job_is_dropped_on_timeout(api):
    client, worker, llm, db = api
    await db.grant("a")
    user, _ = await db.resolve(1, "a", "A")
    llm.gate.clear()
    person = FakeReply()
    await worker.submit(user.id, "долгий вопрос", [], lambda: person)
    await wait_until(lambda: person.updates)

    response = await client.post("/v1/chat/completions", json=QUESTION, headers=AUTH)
    assert response.status == 504
    assert worker.queue_size == 1           # остался только человек
    llm.gate.set()
    await wait_until(lambda: person.final)
    assert len(llm.requests) == 1           # снятый запрос модель так и не получила


def test_client_name_by_key():
    keys = {"viakitbot": KEY, "other": "o" * 32}
    assert client_name(f"Bearer {KEY}", keys) == "viakitbot"
    assert client_name(f"Bearer {'o' * 32}", keys) == "other"
    assert client_name("Bearer ", keys) is None
    assert client_name(KEY, keys) is None


def test_parse_api_keys():
    assert parse_api_keys("") == {}
    assert parse_api_keys(f" viakitbot:{KEY} , other:{'o' * 30} ") == {
        "viakitbot": KEY, "other": "o" * 30,
    }


@pytest.mark.parametrize("raw", ["короткий:abc", "без_двоеточия" + "x" * 30, f":{KEY}"])
def test_bad_api_keys_stop_the_bot_without_printing_the_key(raw):
    with pytest.raises(SystemExit) as error:
        parse_api_keys(raw)
    assert "x" * 30 not in str(error.value) and KEY not in str(error.value)


async def test_api_is_off_without_host(tmp_path):
    # Без API_HOST сервер не поднимается и задача сразу завершается.
    await asyncio.wait_for(serve_api(None, make_config(tmp_path)), 1)
