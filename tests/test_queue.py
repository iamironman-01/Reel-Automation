import os
import sqlite3

import pytest

from reeldrop import db
from reeldrop.queue import (
    add_reel,
    delete_reel,
    list_reels,
    mark_failed,
    mark_sent,
    next_pending,
    sent_today,
)



# Tests that check SQLite itself; skipped when the suite runs on PostgreSQL.
SQLITE_ONLY = pytest.mark.skipif(bool(os.environ.get("TEST_DATABASE_URL")),
                                 reason="checks SQLite specifics")

def _log_rows():
    with db.connect() as c:
        return c.execute("SELECT * FROM send_log ORDER BY id").fetchall()


def test_valid_link_stored_clean(temp_db):
    rid = add_reel("https://www.instagram.com/reel/ABC123xyz/?igsh=xxxx", "funny")
    (row,) = list_reels()
    assert row["id"] == rid
    assert row["url"] == "https://www.instagram.com/reel/ABC123xyz/"
    assert row["shortcode"] == "ABC123xyz"
    assert row["note"] == "funny"
    assert row["status"] == "pending"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/reels/AAA111/",
        "https://www.instagram.com/p/AAA111/",
        "https://instagram.com/reel/AAA111",
        "https://www.instagram.com/someuser/reel/AAA111/?igsh=1#x",
        "instagram.com/p/AAA111/",
    ],
)
def test_other_link_forms(temp_db, url):
    add_reel(url)
    assert list_reels()[0]["url"] == "https://www.instagram.com/reel/AAA111/"


def test_surrounding_whitespace_tolerated(temp_db):
    add_reel("  \nhttps://www.instagram.com/reel/AAA111/?igsh=1\t ")
    assert list_reels()[0]["shortcode"] == "AAA111"


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com/watch?v=1",
        "",
        "https://www.instagram.com/someuser/",
        "https://evil.example/?u=instagram.com/reel/AAA111/",
        "https://notinstagram.com/reel/AAA111/",
    ],
)
def test_invalid_rejected(temp_db, url):
    with pytest.raises(ValueError, match="Not an Instagram reel/post link"):
        add_reel(url)
    assert list_reels() == []


def test_duplicate_rejected_even_with_query(temp_db):
    add_reel("https://www.instagram.com/reel/AAA111/")
    with pytest.raises(ValueError, match="already in the queue"):
        add_reel("https://www.instagram.com/reel/AAA111/?igsh=xyz")
    with pytest.raises(ValueError, match="already in the queue"):
        add_reel("https://www.instagram.com/p/AAA111/")
    assert len(list_reels()) == 1


def test_next_pending_order_and_limit(temp_db):
    ids = [add_reel(f"https://www.instagram.com/reel/R{i}/") for i in range(4)]
    assert [r["shortcode"] for r in next_pending(10)] == ["R0", "R1", "R2", "R3"]
    assert [r["id"] for r in next_pending(2)] == ids[:2]
    mark_sent(ids[0], "bob", "rule", "dry_run")
    assert [r["id"] for r in next_pending(2)] == ids[1:3]
    assert next_pending(0) == []


def test_list_reels_status_filter(temp_db):
    a = add_reel("https://www.instagram.com/reel/AAA/")
    b = add_reel("https://www.instagram.com/reel/BBB/")
    mark_failed(b, "bob", "rule", "dry_run", "boom")
    assert [r["id"] for r in list_reels("pending")] == [a]
    assert [r["id"] for r in list_reels("failed")] == [b]
    assert len(list_reels()) == 2


def test_mark_sent_updates_status_and_logs(temp_db):
    rid = add_reel("https://www.instagram.com/reel/AAA/")
    mark_sent(rid, "bob", "morning", "dry_run")
    assert list_reels()[0]["status"] == "sent"
    (log,) = _log_rows()
    assert (log["reel_id"], log["recipient"], log["rule_name"]) == (rid, "bob", "morning")
    assert (log["sender_type"], log["status"], log["error"]) == ("dry_run", "sent", None)
    assert log["sent_at"]


def test_mark_failed_updates_status_and_logs(temp_db):
    rid = add_reel("https://www.instagram.com/reel/AAA/")
    mark_failed(rid, "bob", "morning", "dry_run", "no such user")
    assert list_reels()[0]["status"] == "failed"
    (log,) = _log_rows()
    assert (log["status"], log["error"]) == ("failed", "no such user")


def test_mark_sent_is_atomic(temp_db, monkeypatch):
    """If the log insert fails, the status update must roll back too."""
    rid = add_reel("https://www.instagram.com/reel/AAA/")
    with db.connect() as c:
        c.execute("DROP TABLE send_log")
    with pytest.raises(db.DB_ERRORS):
        mark_sent(rid, "bob", "r", "dry_run")
    assert list_reels()[0]["status"] == "pending"


def test_delete_reel(temp_db):
    a = add_reel("https://www.instagram.com/reel/AAA/")
    b = add_reel("https://www.instagram.com/reel/BBB/")
    assert delete_reel(a) is True
    assert [r["id"] for r in list_reels()] == [b]
    assert delete_reel(a) is False
    # can be re-added after deletion
    add_reel("https://www.instagram.com/reel/AAA/")


def test_sent_today_counts_only_today_and_sent(temp_db):
    a = add_reel("https://www.instagram.com/reel/AAA/")
    b = add_reel("https://www.instagram.com/reel/BBB/")
    c_ = add_reel("https://www.instagram.com/reel/CCC/")
    assert sent_today() == 0
    mark_sent(a, "bob", "r", "dry_run")
    mark_sent(b, "bob", "r", "dry_run")
    mark_failed(c_, "bob", "r", "dry_run", "err")
    with db.connect() as c:
        c.execute(
            "INSERT INTO send_log (reel_id, recipient, rule_name, sender_type, status, sent_at)"
            " VALUES (?, 'bob', 'r', 'dry_run', 'sent', '2020-01-01 10:00:00')",
            (a,),
        )
    assert sent_today() == 2


def test_sent_today_uses_tz_name(temp_db, monkeypatch):
    """sent_today follows TZ_NAME, the same clock as now()."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    rid = add_reel("https://www.instagram.com/reel/AAA/")
    monkeypatch.setenv("TZ_NAME", "Pacific/Kiritimati")  # UTC+14
    mark_sent(rid, "bob", "r", "dry_run")
    expected = datetime.now(ZoneInfo("Pacific/Kiritimati")).strftime("%Y-%m-%d")
    with db.connect() as c:
        assert c.execute("SELECT sent_at FROM send_log").fetchone()[0][:10] == expected
    assert sent_today() == 1


@SQLITE_ONLY
def test_connect_row_factory_and_closes(temp_db):
    with db.connect() as c:
        assert c.row_factory is sqlite3.Row
        assert isinstance(c, sqlite3.Connection)
        row = c.execute("SELECT 1 AS x").fetchone()
        assert row["x"] == 1
    with pytest.raises(sqlite3.ProgrammingError):  # closed on exit
        c.execute("SELECT 1")


def test_connect_closes_on_exception_and_rolls_back(temp_db):
    with pytest.raises(RuntimeError):
        with db.connect() as c:
            c.execute(
                "INSERT INTO reels (url, shortcode, added_at) VALUES ('u', 's', 'now')"
            )
            raise RuntimeError("boom")
    with pytest.raises(db.DB_ERRORS):              # released on exit
        c.execute("SELECT 1")
    assert list_reels() == []


@SQLITE_ONLY
def test_connect_pragmas(temp_db):
    with db.connect() as c:
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
