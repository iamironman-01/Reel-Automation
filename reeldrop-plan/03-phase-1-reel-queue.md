# Phase 1 — Reel Queue

**Goal:** store reels and manage their order.
**Time:** 2 days

## Checklist

- [ ] `db.py` — connection, schema, `init_db()`, `now()`
- [ ] `queue.py` — `add_reel`, `list_reels`, `next_pending`, `mark_sent`, `mark_failed`, `delete_reel`, `sent_today`
- [ ] Link validation + duplicate rejection
- [ ] CLI: `python -m reeldrop add/list/remove`

## reeldrop/db.py

```python
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path("data/reeldrop.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reels (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    url       TEXT NOT NULL,
    shortcode TEXT NOT NULL UNIQUE,
    note      TEXT DEFAULT '',
    status    TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | failed
    added_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS send_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reel_id     INTEGER,
    recipient   TEXT,
    rule_name   TEXT,
    sender_type TEXT,
    status      TEXT,          -- sent | failed
    error       TEXT,
    sent_at     TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

def now() -> str:
    tz = ZoneInfo(os.getenv("TZ_NAME", "Asia/Kolkata"))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
```

## reeldrop/queue.py

```python
import re
import sqlite3
from .db import connect, now

REEL_RE = re.compile(r"instagram\.com/(?:reel|reels|p)/([A-Za-z0-9_-]+)")

def add_reel(url: str, note: str = "") -> int:
    m = REEL_RE.search(url)
    if not m:
        raise ValueError("Not an Instagram reel/post link")
    shortcode = m.group(1)
    clean_url = f"https://www.instagram.com/reel/{shortcode}/"
    try:
        with connect() as c:
            cur = c.execute(
                "INSERT INTO reels (url, shortcode, note, added_at) VALUES (?, ?, ?, ?)",
                (clean_url, shortcode, note, now()),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("This reel is already in the queue")

def list_reels(status: str | None = None):
    with connect() as c:
        if status:
            return c.execute("SELECT * FROM reels WHERE status=? ORDER BY id", (status,)).fetchall()
        return c.execute("SELECT * FROM reels ORDER BY id").fetchall()

def next_pending(n: int):
    with connect() as c:
        return c.execute(
            "SELECT * FROM reels WHERE status='pending' ORDER BY id LIMIT ?", (n,)
        ).fetchall()

def _log(c, reel_id, recipient, rule_name, sender_type, status, error=None):
    c.execute(
        "INSERT INTO send_log (reel_id, recipient, rule_name, sender_type, status, error, sent_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (reel_id, recipient, rule_name, sender_type, status, error, now()),
    )

def mark_sent(reel_id, recipient, rule_name, sender_type):
    with connect() as c:   # one transaction: status + log together
        c.execute("UPDATE reels SET status='sent' WHERE id=?", (reel_id,))
        _log(c, reel_id, recipient, rule_name, sender_type, "sent")

def mark_failed(reel_id, recipient, rule_name, sender_type, error: str):
    with connect() as c:
        c.execute("UPDATE reels SET status='failed' WHERE id=?", (reel_id,))
        _log(c, reel_id, recipient, rule_name, sender_type, "failed", error)

def delete_reel(reel_id: int):
    with connect() as c:
        c.execute("DELETE FROM reels WHERE id=?", (reel_id,))

def sent_today() -> int:
    today = now()[:10]
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM send_log WHERE status='sent' AND sent_at LIKE ?",
            (today + "%",),
        ).fetchone()
        return row[0]
```

## reeldrop/__main__.py  (CLI)

```python
import argparse
from .db import init_db
from .queue import add_reel, list_reels, delete_reel

def main():
    init_db()
    p = argparse.ArgumentParser(prog="reeldrop")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add");    a.add_argument("url"); a.add_argument("--note", default="")
    l = sub.add_parser("list");   l.add_argument("--status")
    r = sub.add_parser("remove"); r.add_argument("id", type=int)
    args = p.parse_args()

    if args.cmd == "add":
        try:
            print("Added reel id", add_reel(args.url, args.note))
        except ValueError as e:
            print("Error:", e)
    elif args.cmd == "list":
        for row in list_reels(args.status):
            print(f"{row['id']:>4}  {row['status']:<8} {row['url']}  {row['note']}")
    elif args.cmd == "remove":
        delete_reel(args.id); print("Removed", args.id)

if __name__ == "__main__":
    main()
```

## Try it

```bash
python -m reeldrop add "https://www.instagram.com/reel/ABC123xyz/?igsh=xxxx" --note "funny"
python -m reeldrop add "https://www.instagram.com/reel/ABC123xyz/"     # → duplicate error
python -m reeldrop add "https://youtube.com/watch?v=1"                 # → invalid error
python -m reeldrop list
```

✅ **Done when:** you can add, list and remove reels; invalid and duplicate links are rejected.
