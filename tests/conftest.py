import os

import pytest

from reeldrop import db

# Set TEST_DATABASE_URL to a THROWAWAY PostgreSQL database to run every database test
# against PostgreSQL instead of SQLite. Its tables are emptied before each test.
TEST_PG_URL = os.environ.get("TEST_DATABASE_URL", "").strip()


@pytest.fixture(autouse=True)
def _no_shared_database(monkeypatch):
    """Never let a test reach a real database through DATABASE_URL.

    Set to "" rather than deleted: dashboard.py and main.py call load_dotenv(), which
    would otherwise fill it in from the project's real .env.
    """
    monkeypatch.setenv("DATABASE_URL", "")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point reeldrop at a fresh, empty database for one test."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setenv("TZ_NAME", "Asia/Kolkata")
    if TEST_PG_URL:
        monkeypatch.setenv("DATABASE_URL", TEST_PG_URL)
        db.init_db()
        with db.connect() as c:
            c.execute("TRUNCATE reels, send_log, settings RESTART IDENTITY")
    else:
        db.init_db()
    return db.DB_PATH
