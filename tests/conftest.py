import pytest

from reeldrop import db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point reeldrop at a fresh SQLite file for one test."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setenv("TZ_NAME", "Asia/Kolkata")
    db.init_db()
    return db.DB_PATH
