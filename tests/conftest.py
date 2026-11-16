import os
import tempfile

# Must be set before any `app` import so settings never touch real data.
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "conftest.db")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    import app.database as database

    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_FILE", path)
    await database.init_db()
    yield path


@pytest.fixture(autouse=True)
def _send_window_always_open(monkeypatch):
    """Tests don't depend on the wall clock; window tests set these explicitly."""
    from app.config import settings

    monkeypatch.setattr(settings, "send_window_start_hour", 0, raising=False)
    monkeypatch.setattr(settings, "send_window_end_hour", 24, raising=False)
    monkeypatch.setattr(settings, "send_weekdays_only", False, raising=False)
    # Sending requires a postal address (CAN-SPAM); tests that check the guard clear it.
    monkeypatch.setattr(settings, "postal_address", "1 Test St, Tampa, FL 33602", raising=False)
