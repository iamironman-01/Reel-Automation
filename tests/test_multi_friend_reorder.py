"""Phase 7: multiple friends (reels.recipient) and queue reorder (reels.position)."""
import sqlite3

import pytest

from reeldrop import db, job
from reeldrop.__main__ import main
from reeldrop.config import Config, Rule
from reeldrop.dashboard_helpers import (
    add_links,
    move_message,
    pending_ids,
    queue_query,
    recipient_message,
)
from reeldrop.queue import (
    add_reel,
    list_reels,
    mark_failed,
    mark_sent,
    move_reel,
    next_pending,
    reorder_ids,
    set_recipient,
)

OLD_SCHEMA = """
CREATE TABLE reels (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    url       TEXT NOT NULL,
    shortcode TEXT NOT NULL UNIQUE,
    note      TEXT DEFAULT '',
    status    TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | failed
    added_at  TEXT NOT NULL
);
CREATE TABLE send_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reel_id     INTEGER,
    recipient   TEXT,
    rule_name   TEXT,
    sender_type TEXT,
    status      TEXT,
    error       TEXT,
    sent_at     TEXT
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
"""


def url(i):
    return f"https://www.instagram.com/reel/R{i:03d}/"


def add(n, **kw):
    return [add_reel(url(i), **kw) for i in range(n)]


def queue_ids(recipient=None, n=100):
    return [r["id"] for r in next_pending(n, recipient=recipient)]


def columns():
    with db.connect() as c:
        return {r["name"]: r for r in c.execute("PRAGMA table_info(reels)")}


def positions():
    with db.connect() as c:
        return {r["id"]: r["position"] for r in c.execute("SELECT id, position FROM reels")}


# --- schema migration --------------------------------------------------------

@pytest.fixture
def old_db(tmp_path, monkeypatch):
    """A database created by the pre-Phase-7 schema, with three rows."""
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("TZ_NAME", "Asia/Kolkata")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    for i, status in enumerate(["sent", "pending", "pending"], start=1):
        conn.execute(
            "INSERT INTO reels (url, shortcode, note, status, added_at) VALUES (?,?,?,?,?)",
            (url(i), f"R{i:03d}", f"note{i}", status, "2025-01-0%d 10:00:00" % i),
        )
    conn.execute("INSERT INTO send_log (reel_id, recipient, status) VALUES (1, 'bob', 'sent')")
    conn.commit()
    conn.close()
    return path


def test_migration_adds_columns_and_keeps_data(old_db):
    db.init_db()
    cols = columns()
    assert "recipient" in cols and "position" in cols
    rows = list_reels()
    assert [(r["id"], r["status"], r["note"], r["recipient"], r["position"]) for r in rows] == [
        (1, "sent", "note1", None, 1),
        (2, "pending", "note2", None, 2),
        (3, "pending", "note3", None, 3),
    ]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM send_log").fetchone()[0] == 1
        idx = [r["name"] for r in c.execute("PRAGMA index_list(reels)")]
    assert "idx_reels_status_position" in idx


def test_migration_is_idempotent_and_does_not_touch_moved_positions(old_db):
    db.init_db()
    assert move_reel(3, "top")
    before = positions()
    db.init_db()
    db.init_db()
    assert positions() == before
    assert len(list_reels()) == 3


def test_migrated_db_accepts_new_reels_and_features(old_db):
    db.init_db()
    rid = add_reel(url(10), recipient="alice")
    assert positions()[rid] == 4
    assert queue_ids("alice") == [2, 3, rid]
    assert queue_ids("bob") == [2, 3]


def test_fresh_and_migrated_schemas_have_same_columns(old_db, tmp_path, monkeypatch):
    db.init_db()
    migrated = {k: (v["type"], v["notnull"]) for k, v in columns().items()}
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "fresh.db")
    db.init_db()
    fresh = {k: (v["type"], v["notnull"]) for k, v in columns().items()}
    assert migrated == fresh
    assert set(fresh) >= {"recipient", "position"}


def test_fresh_db_has_index(temp_db):
    with db.connect() as c:
        idx = [r["name"] for r in c.execute("PRAGMA index_list(reels)")]
    assert "idx_reels_status_position" in idx


# --- positions on add --------------------------------------------------------

def test_new_reels_get_increasing_positions(temp_db):
    ids = add(3)
    assert [positions()[i] for i in ids] == [1, 2, 3]


def test_position_continues_after_delete_and_never_reuses_max(temp_db):
    a, b, c = add(3)
    from reeldrop.queue import delete_reel
    delete_reel(c)
    d = add_reel(url(9))
    assert positions()[d] == 3   # max was 2 after the delete
    assert positions()[d] > positions()[b]


# --- recipient assignment / filtering ----------------------------------------

def test_add_reel_recipient_stripped_and_blank_is_none(temp_db):
    a = add_reel(url(1), recipient="  alice  ")
    b = add_reel(url(2), recipient="   ")
    c = add_reel(url(3), recipient=None)
    d = add_reel(url(4))
    got = {r["id"]: r["recipient"] for r in list_reels()}
    assert got == {a: "alice", b: None, c: None, d: None}


def test_existing_add_reel_signature_still_works(temp_db):
    rid = add_reel(url(1), "a note")
    (row,) = list_reels()
    assert row["id"] == rid and row["note"] == "a note" and row["recipient"] is None


def test_next_pending_without_recipient_returns_everything(temp_db):
    a = add_reel(url(1), recipient="alice")
    b = add_reel(url(2), recipient="bob")
    c = add_reel(url(3))
    assert queue_ids() == [a, b, c]


def test_recipient_filter_gets_own_and_unassigned_only(temp_db):
    a = add_reel(url(1), recipient="alice")
    b = add_reel(url(2), recipient="bob")
    u = add_reel(url(3))
    a2 = add_reel(url(4), recipient="alice")
    assert queue_ids("alice") == [a, u, a2]
    assert queue_ids("bob") == [b, u]
    assert queue_ids("carol") == [u]


@pytest.mark.parametrize("order", ["oldest", "random"])
def test_reel_for_one_friend_never_returned_for_another(temp_db, order):
    alice = [add_reel(url(i), recipient="alice") for i in range(5)]
    bob = [add_reel(url(10 + i), recipient="bob") for i in range(5)]
    for _ in range(20):
        got = {r["id"] for r in next_pending(50, order=order, recipient="bob")}
        assert got == set(bob)
        got = {r["id"] for r in next_pending(50, order=order, recipient="alice")}
        assert got == set(alice)


def test_recipient_match_ignores_case_and_surrounding_space(temp_db):
    a = add_reel(url(1), recipient="Alice")
    assert queue_ids("alice") == [a]
    assert queue_ids("  ALICE ") == [a]


def test_blank_recipient_filter_never_returns_assigned_reels(temp_db):
    add_reel(url(1), recipient="alice")
    u = add_reel(url(2))
    assert queue_ids("") == [u]


def test_recipient_filter_skips_non_pending_and_respects_limit(temp_db):
    a, b, c = (add_reel(url(i), recipient="alice") for i in range(3))
    mark_sent(a, "alice", "r", "s")
    mark_failed(b, "alice", "r", "s", "x")
    assert queue_ids("alice") == [c]
    d = add_reel(url(5))
    assert queue_ids("alice", n=1) == [c]
    assert queue_ids("alice", n=0) == []
    assert queue_ids("alice") == [c, d]


def test_random_with_recipient_fewer_than_n_sorted_by_id(temp_db):
    ids = [add_reel(url(i), recipient="alice") for i in range(4)]
    add_reel(url(9), recipient="bob")
    assert [r["id"] for r in next_pending(10, order="random", recipient="alice")] == ids
    for _ in range(10):
        got = [r["id"] for r in next_pending(2, order="random", recipient="alice")]
        assert len(got) == 2 and got == sorted(got)


def test_set_recipient_assign_change_clear(temp_db):
    rid = add_reel(url(1))
    assert set_recipient(rid, " bob ") is True
    assert list_reels()[0]["recipient"] == "bob"
    assert set_recipient(rid, "alice") is True
    assert list_reels()[0]["recipient"] == "alice"
    assert set_recipient(rid, None) is True
    assert list_reels()[0]["recipient"] is None
    assert set_recipient(rid, "  ") is True
    assert list_reels()[0]["recipient"] is None


def test_set_recipient_missing_reel_returns_false(temp_db):
    assert set_recipient(999, "bob") is False


def test_set_recipient_changes_who_receives_it(temp_db):
    rid = add_reel(url(1), recipient="alice")
    assert queue_ids("bob") == []
    set_recipient(rid, "bob")
    assert queue_ids("bob") == [rid] and queue_ids("alice") == []


def test_dedupe_is_global_across_recipients(temp_db):
    add_reel(url(1), recipient="alice")
    with pytest.raises(ValueError, match="already in the queue"):
        add_reel(url(1), recipient="bob")


# --- move_reel ---------------------------------------------------------------

def test_move_up_and_down_swap_neighbours(temp_db):
    a, b, c, d = add(4)
    assert move_reel(c, "up") is True
    assert queue_ids() == [a, c, b, d]
    assert move_reel(a, "down") is True
    assert queue_ids() == [c, a, b, d]


def test_move_top_and_bottom(temp_db):
    a, b, c, d = add(4)
    assert move_reel(c, "top") is True
    assert queue_ids() == [c, a, b, d]
    assert move_reel(c, "bottom") is True
    assert queue_ids() == [a, b, d, c]
    assert move_reel(b, "bottom") is True
    assert queue_ids() == [a, d, c, b]
    assert move_reel(b, "top") is True
    assert queue_ids() == [b, a, d, c]


def test_move_at_edge_returns_false_and_changes_nothing(temp_db):
    a, b, c = add(3)
    before = positions()
    assert move_reel(a, "up") is False
    assert move_reel(a, "top") is False
    assert move_reel(c, "down") is False
    assert move_reel(c, "bottom") is False
    assert positions() == before


def test_move_single_pending_reel_is_false(temp_db):
    (a,) = add(1)
    for d in ("up", "down", "top", "bottom"):
        assert move_reel(a, d) is False


def test_move_non_pending_or_missing_returns_false(temp_db):
    a, b, c = add(3)
    mark_sent(a, "x", "r", "s")
    mark_failed(b, "x", "r", "s", "err")
    before = positions()
    for d in ("up", "down", "top", "bottom"):
        assert move_reel(a, d) is False
        assert move_reel(b, d) is False
        assert move_reel(999, d) is False
    assert positions() == before


def test_move_rejects_bad_direction(temp_db):
    (a, _) = add(2)
    with pytest.raises(ValueError):
        move_reel(a, "sideways")


def test_move_ignores_sent_reels_between_pending_ones(temp_db):
    a, b, c, d = add(4)
    mark_sent(b, "x", "r", "s")           # b sits between a and c in position order
    assert move_reel(c, "up") is True     # swaps with a, the previous *pending* reel
    assert queue_ids() == [c, a, d]
    assert positions()[b] == 2            # sent reel untouched


def test_positions_stay_unique_after_many_moves(temp_db):
    ids = add(6)
    mark_sent(ids[2], "x", "r", "s")
    for rid, d in [(ids[5], "top"), (ids[0], "down"), (ids[4], "up"), (ids[1], "bottom"),
                   (ids[3], "top"), (ids[5], "bottom"), (ids[0], "up")]:
        move_reel(rid, d)
        vals = list(positions().values())
        assert len(vals) == len(set(vals))
    assert sorted(queue_ids()) == sorted(set(ids) - {ids[2]})


def test_move_survives_new_additions(temp_db):
    a, b = add(2)
    move_reel(b, "top")
    c = add_reel(url(7))
    assert queue_ids() == [b, a, c]


def test_move_repairs_duplicate_positions(temp_db):
    a, b, c = add(3)
    with db.connect() as conn:
        conn.execute("UPDATE reels SET position = 5")   # simulated damage
    assert move_reel(c, "top") is True
    assert queue_ids()[0] == c
    assert len(set(positions().values())) == 3


def test_move_treats_null_position_as_id(temp_db):
    a, b, c = add(3)
    with db.connect() as conn:
        conn.execute("UPDATE reels SET position = NULL WHERE id = ?", (b,))
    assert queue_ids() == [a, b, c]
    assert move_reel(b, "bottom") is True
    assert queue_ids() == [a, c, b]


def test_next_pending_respects_position_and_limit(temp_db):
    a, b, c, d = add(4)
    move_reel(d, "top")
    move_reel(c, "up")
    assert queue_ids() == [d, a, c, b]
    assert queue_ids(n=2) == [d, a]
    assert [r["id"] for r in list_reels("pending")] == [d, a, c, b]


def test_position_and_recipient_filter_combine(temp_db):
    a = add_reel(url(1), recipient="alice")
    b = add_reel(url(2), recipient="bob")
    u = add_reel(url(3))
    move_reel(u, "top")
    assert queue_ids("alice") == [u, a]
    assert queue_ids("bob") == [u, b]


def test_reorder_ids_pure():
    ids = [1, 2, 3, 4]
    assert reorder_ids(ids, 3, "up") == [1, 3, 2, 4]
    assert reorder_ids(ids, 2, "down") == [1, 3, 2, 4]
    assert reorder_ids(ids, 4, "top") == [4, 1, 2, 3]
    assert reorder_ids(ids, 1, "bottom") == [2, 3, 4, 1]
    assert reorder_ids(ids, 1, "up") == ids
    assert reorder_ids(ids, 4, "down") == ids
    assert reorder_ids(ids, 9, "top") == ids
    assert ids == [1, 2, 3, 4]    # input not mutated
    with pytest.raises(ValueError):
        reorder_ids(ids, 1, "nope")


# --- CLI ---------------------------------------------------------------------

def test_cli_add_to_sets_recipient(temp_db, capsys):
    assert main(["add", url(1), "--to", "alice"]) == 0
    assert "Added reel id 1" in capsys.readouterr().out
    assert list_reels()[0]["recipient"] == "alice"
    assert main(["add", url(2)]) == 0
    assert list_reels()[1]["recipient"] is None


def test_cli_list_shows_recipient_column(temp_db, capsys):
    main(["add", url(1), "--to", "alice", "--note", "hi"])
    main(["add", url(2)])
    capsys.readouterr()
    assert main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "alice" in lines[0] and url(1) in lines[0] and "hi" in lines[0]
    assert " - " in lines[1] and "alice" not in lines[1]


def test_cli_move(temp_db, capsys):
    main(["add", url(1)])
    main(["add", url(2)])
    main(["add", url(3)])
    capsys.readouterr()
    assert main(["move", "3", "top"]) == 0
    assert "Moved 3 top" in capsys.readouterr().out
    assert queue_ids() == [3, 1, 2]
    assert main(["list", "--status", "pending"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [int(line.split()[0]) for line in out] == [3, 1, 2]
    assert main(["move", "1", "down"]) == 0
    assert queue_ids() == [3, 2, 1]


def test_cli_move_failures_exit_nonzero(temp_db, capsys):
    main(["add", url(1)])
    capsys.readouterr()
    assert main(["move", "1", "up"]) == 1          # only reel: at the edge
    assert "not moved" in capsys.readouterr().out
    assert main(["move", "42", "up"]) == 1         # missing
    assert "not moved" in capsys.readouterr().out


def test_cli_move_rejects_bad_direction(temp_db):
    with pytest.raises(SystemExit) as e:
        main(["move", "1", "sideways"])
    assert e.value.code == 2


# --- job ---------------------------------------------------------------------

class Recorder:
    name = "rec"

    def __init__(self):
        self.sent = []

    def send(self, recipient, link):
        self.sent.append((recipient, link))

    def send_text(self, recipient, text):
        pass


@pytest.fixture
def jenv(temp_db, monkeypatch):
    monkeypatch.setattr(job, "notify_me", lambda m: None)
    monkeypatch.setattr(job.time, "sleep", lambda s: None)


def rule(name, recipient, count=10, pick="oldest"):
    return Rule(name=name, days="*", time="20:00", count=count, recipient=recipient, pick=pick)


def test_job_passes_rule_recipient_to_next_pending(jenv, monkeypatch):
    seen = []
    monkeypatch.setattr(
        job, "next_pending",
        lambda n, order="oldest", recipient=None: seen.append((n, order, recipient)) or [],
    )
    job.run_rule(rule("a", "alice", count=2), Config(), Recorder())
    assert seen == [(2, "oldest", "alice")]


@pytest.mark.parametrize("pick", ["oldest", "random"])
def test_two_rules_each_send_only_their_own_and_unassigned(jenv, pick):
    a1 = add_reel(url(1), recipient="alice")
    b1 = add_reel(url(2), recipient="bob")
    u1 = add_reel(url(3))
    a2 = add_reel(url(4), recipient="alice")
    cfg = Config(daily_cap=50, gap_seconds=0)
    s = Recorder()

    job.run_rule(rule("alice-rule", "alice", pick=pick), cfg, s)
    assert sorted(s.sent) == sorted([("alice", url(1)), ("alice", url(3)), ("alice", url(4))])

    s2 = Recorder()
    job.run_rule(rule("bob-rule", "bob", pick=pick), cfg, s2)
    assert s2.sent == [("bob", url(2))]            # u1 already went to alice

    status = {r["id"]: r["status"] for r in list_reels()}
    assert set(status.values()) == {"sent"}
    with db.connect() as c:
        log = {r["reel_id"]: r["recipient"] for r in c.execute("SELECT * FROM send_log")}
    assert log == {a1: "alice", a2: "alice", u1: "alice", b1: "bob"}


def test_job_never_sends_friends_reel_to_wrong_rule(jenv):
    add_reel(url(1), recipient="alice")
    s = Recorder()
    job.run_rule(rule("bob-rule", "bob"), Config(gap_seconds=0), s)
    assert s.sent == []
    assert list_reels()[0]["status"] == "pending"


def test_job_sends_in_queue_order(jenv):
    a, b, c = add(3)
    move_reel(c, "top")
    s = Recorder()
    job.run_rule(rule("r", "friend", count=2), Config(gap_seconds=0), s)
    assert s.sent == [("friend", url(2)), ("friend", url(0))]


# --- dashboard helpers -------------------------------------------------------

def test_add_links_without_recipient_keeps_two_arg_call():
    calls = []
    added, errors = add_links("a\nb", "n", lambda u, n: calls.append((u, n)))
    assert (added, errors) == (2, [])
    assert calls == [("a", "n"), ("b", "n")]
    calls.clear()
    add_links("a", "n", lambda u, n: calls.append((u, n)), recipient="   ")
    assert calls == [("a", "n")]


def test_add_links_passes_stripped_recipient():
    calls = []
    add_links("a\nb", "n", lambda u, n, recipient=None: calls.append((u, n, recipient)),
              recipient="  alice ")
    assert calls == [("a", "n", "alice"), ("b", "n", "alice")]


def test_add_links_with_real_queue_and_recipient(temp_db):
    added, errors = add_links(f"{url(1)}\nnope\n{url(2)}", "", add_reel, recipient="alice")
    assert added == 2 and len(errors) == 1
    assert [r["recipient"] for r in list_reels()] == ["alice", "alice"]


def test_queue_query_includes_recipient_and_position(temp_db):
    for status in ("all", "pending", "sent", "failed"):
        sql, params = queue_query(status)
        with db.connect() as c:
            cols = [d[0] for d in c.execute(sql, params).description]
        assert "recipient" in cols and "position" in cols


def test_queue_query_pending_is_in_send_order(temp_db):
    a, b, c = add(3)
    move_reel(c, "top")
    sql, params = queue_query("pending")
    with db.connect() as conn:
        assert [r["id"] for r in conn.execute(sql, params)] == [c, a, b]
    sql, params = queue_query("all")
    with db.connect() as conn:
        assert [r["id"] for r in conn.execute(sql, params)] == [c, b, a]   # id DESC as before


def test_pending_ids_filters_and_keeps_order():
    rows = [{"id": 5, "status": "pending"}, {"id": 4, "status": "sent"},
            {"id": 3, "status": "pending"}, {"id": 2, "status": "failed"}]
    assert pending_ids(rows) == [5, 3]
    assert pending_ids([]) == []


def test_messages():
    assert move_message(3, "up", True) == "Moved reel #3 up"
    assert "not moved" in move_message(3, "up", False)
    assert "alice" in recipient_message(3, " alice ", True)
    assert "unassigned" in recipient_message(3, "", True)
    assert "unassigned" in recipient_message(3, None, True)
    assert "No reel" in recipient_message(3, "alice", False)
