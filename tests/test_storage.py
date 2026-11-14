"""Shared-database storage (Supabase / PostgreSQL) pieces that work without a server.

The PostgreSQL code path itself runs in tests/test_postgres.py (needs a local server)
and, for the whole suite, with TEST_DATABASE_URL (see conftest.py).
"""
import base64
import importlib
import os
import logging
import sys

import pytest

from reeldrop import db, settings


# --- placeholder translation and rows ------------------------------------------------


# Tests that check SQLite itself; skipped when the suite runs on PostgreSQL.
SQLITE_ONLY = pytest.mark.skipif(bool(os.environ.get("TEST_DATABASE_URL")),
                                 reason="checks SQLite specifics")

@pytest.mark.parametrize("sql,expected", [
    ("SELECT * FROM t WHERE a=? AND b=?", "SELECT * FROM t WHERE a=%s AND b=%s"),
    ("SELECT '?' , ?", "SELECT '?' , %s"),
    ("SELECT 'it''s ?' , ?", "SELECT 'it''s ?' , %s"),
    ("SELECT 5 % 2, ?", "SELECT 5 %% 2, %s"),
    ("SELECT 'a%b'", "SELECT 'a%%b'"),
    ('SELECT "col?" FROM t', 'SELECT "col?" FROM t'),
    ("SELECT 1", "SELECT 1"),
])
def test_qmarks_to_pyformat(sql, expected):
    assert db._qmarks_to_pyformat(sql) == expected


def test_row_by_position_and_name():
    row = db.Row((1, "pending"), ["id", "status"])
    assert row[0] == 1 and row["status"] == "pending" and row[-1] == "pending"
    assert tuple(row) == (1, "pending") and row.keys() == ["id", "status"]
    with pytest.raises(IndexError):
        row["missing"]


@SQLITE_ONLY
def test_sqlite_is_used_without_database_url(temp_db):
    assert db.using_postgres() is False
    with db.connect() as c:
        assert c.is_postgres is False


def test_read_rows(temp_db):
    from reeldrop import queue
    queue.add_reel("https://www.instagram.com/reel/AAA/")
    with db.connect() as c:
        names, rows = db.read_rows(c, "SELECT id, status FROM reels WHERE id=?", (1,))
    assert names == ["id", "status"] and [tuple(r) for r in rows] == [(1, "pending")]


def test_add_reel_returns_the_new_id_and_rejects_duplicates(temp_db):
    from reeldrop import queue
    assert queue.add_reel("https://www.instagram.com/reel/AAA/") == 1
    assert queue.add_reel("https://www.instagram.com/reel/BBB/") == 2
    with pytest.raises(ValueError, match="already in the queue"):
        queue.add_reel("https://www.instagram.com/reel/AAA/")


def test_recipient_match_ignores_case(temp_db):
    from reeldrop import queue
    queue.add_reel("https://www.instagram.com/reel/AAA/", recipient="Pal")
    assert [r["shortcode"] for r in queue.next_pending(5, recipient="pal")] == ["AAA"]
    assert queue.next_pending(5, recipient="other") == []
    assert len(queue.next_pending(5, order="random", recipient="PAL")) == 1


# --- pretend the database is shared (DATABASE_URL) while using the SQLite test file ---

@pytest.fixture
def shared_db(temp_db, monkeypatch):
    monkeypatch.setattr(db, "keeps_state", lambda: True)
    return temp_db


GOOD_SESSION = '{"uuids": {"phone_id": "x"}, "cookies": {}, "authorization_data": {"a": 1}}'


@pytest.fixture
def sender_mod(monkeypatch):
    sys.modules.pop("reeldrop.senders.instagrapi_sender", None)
    mod = importlib.import_module("reeldrop.senders.instagrapi_sender")
    yield mod
    sys.modules.pop("reeldrop.senders.instagrapi_sender", None)


def test_session_round_trip_through_the_database(shared_db, tmp_path, sender_mod):
    first = tmp_path / "a" / "session.json"
    first.parent.mkdir()
    first.write_text(GOOD_SESSION, encoding="utf-8")
    assert sender_mod.store_session_in_db(first) is True
    assert settings.get(settings.SESSION_KEY) == GOOD_SESSION

    second = tmp_path / "b" / "session.json"            # a new, empty container
    assert sender_mod.seed_session_from_db(second) is True
    assert second.read_text(encoding="utf-8") == GOOD_SESSION
    assert sender_mod.seed_session_from_db(second) is False   # file exists: untouched


@SQLITE_ONLY
def test_session_is_not_stored_without_a_shared_database(temp_db, tmp_path, sender_mod):
    path = tmp_path / "session.json"
    path.write_text(GOOD_SESSION, encoding="utf-8")
    assert sender_mod.store_session_in_db(path) is False
    assert settings.get(settings.SESSION_KEY) is None
    assert sender_mod.seed_session_from_db(tmp_path / "x.json") is False


def test_bad_stored_session_is_ignored_quietly(shared_db, tmp_path, sender_mod, caplog):
    settings.set_(settings.SESSION_KEY, '{"sessionid": "SECRET-COOKIE"}')
    with caplog.at_level(logging.DEBUG):
        assert sender_mod.seed_session_from_db(tmp_path / "s.json") is False
    assert not (tmp_path / "s.json").exists()
    assert "SECRET-COOKIE" not in caplog.text


SCHEDULE = ('timezone: Asia/Kolkata\nrules:\n  - name: m\n    time: "09:00"\n'
            '    count: 2\n    recipient: db_friend\n')


def test_saved_schedule_is_kept_in_the_shared_database(shared_db, tmp_path):
    pytest.importorskip("yaml")
    from reeldrop import config
    from reeldrop.dashboard_helpers import save_schedules

    live = tmp_path / "live.yaml"
    live.write_text("rules: []\n", encoding="utf-8")
    save_schedules(str(live), SCHEDULE)
    assert settings.get(settings.SCHEDULES_KEY) == SCHEDULE
    assert config.schedules_from_db() == SCHEDULE


def test_missing_live_file_is_restored_from_the_database_first(shared_db, tmp_path,
                                                                monkeypatch):
    pytest.importorskip("yaml")
    from reeldrop import config

    settings.set_(settings.SCHEDULES_KEY, SCHEDULE)
    other = SCHEDULE.replace("db_friend", "env_friend")
    monkeypatch.setenv("SCHEDULES_B64", base64.b64encode(other.encode()).decode())
    default = tmp_path / "default.yaml"
    default.write_text("daily_cap: 3\n", encoding="utf-8")
    target = tmp_path / "disk" / "schedules.yaml"
    monkeypatch.setenv("SCHEDULES_PATH", str(target))
    config.ensure_schedules_file(str(default))
    assert "db_friend" in target.read_text(encoding="utf-8")


def test_invalid_schedule_in_the_database_falls_back(shared_db, tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    from reeldrop import config

    settings.set_(settings.SCHEDULES_KEY, "rules: [\n")
    monkeypatch.delenv("SCHEDULES_B64", raising=False)
    default = tmp_path / "default.yaml"
    default.write_text("daily_cap: 3\n", encoding="utf-8")
    target = tmp_path / "disk" / "schedules.yaml"
    monkeypatch.setenv("SCHEDULES_PATH", str(target))
    config.ensure_schedules_file(str(default))
    assert target.read_text(encoding="utf-8") == "daily_cap: 3\n"


@SQLITE_ONLY
def test_schedule_not_stored_without_a_shared_database(temp_db, tmp_path):
    pytest.importorskip("yaml")
    from reeldrop.dashboard_helpers import save_schedules

    live = tmp_path / "live.yaml"
    save_schedules(str(live), SCHEDULE)
    assert settings.get(settings.SCHEDULES_KEY) is None


# --- scheduler heartbeat -------------------------------------------------------------

def test_scheduler_status_from_the_heartbeat():
    from reeldrop.dashboard_helpers import scheduler_status, seconds_since

    assert seconds_since("2026-11-15 10:00:00", "2026-11-15 10:01:30") == 90
    assert seconds_since("bad", "2026-11-15 10:00:00") is None
    now = "2026-11-15 10:01:00"
    fresh = {"scheduler_heartbeat": "2026-11-15 10:00:30", "scheduler_owner": "srv-1"}
    old = {"scheduler_heartbeat": "2026-11-15 09:50:00"}
    assert scheduler_status(lambda k, d=None: fresh.get(k, d), now) == {
        "running": True, "last_seen": "2026-11-15 10:00:30", "owner": "srv-1"}
    assert scheduler_status(lambda k, d=None: old.get(k, d), now)["running"] is False
    assert scheduler_status(lambda k, d=None: d, now) == {
        "running": False, "last_seen": "", "owner": ""}
