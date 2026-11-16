import os
import tempfile

# Must be set before any `app` import so settings never touch real data.
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "conftest.db")

import pytest_asyncio  # noqa: E402


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    import app.database as database

    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_FILE", path)
    await database.init_db()
    yield path
