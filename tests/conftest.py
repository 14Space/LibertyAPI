import pytest

from bot.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "t.db")
    await database.connect()
    yield database
    await database.close()
