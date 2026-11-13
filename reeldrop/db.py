import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path("data/reeldrop.db")

BUSY_TIMEOUT_MS = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS reels (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    url       TEXT NOT NULL,
    shortcode TEXT NOT NULL UNIQUE,
    note      TEXT DEFAULT '',
    status    TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | failed
    added_at  TEXT NOT NULL,
    position  INTEGER,                           -- queue order (lower is sent first)
    recipient TEXT                               -- NULL = unassigned (any rule may send it)
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


class _Connection(sqlite3.Connection):
    """sqlite3.Connection whose `with` block also closes the connection.

    The stock context manager only commits (or rolls back on error) and leaves
    the connection open. Closing here keeps `with connect() as c:` leak-free.
    It is still a real sqlite3.Connection, so pandas.read_sql_query works.
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000, factory=_Connection
    )
    conn.row_factory = sqlite3.Row
    # The scheduler and dashboard share this file; wait instead of failing
    # immediately with "database is locked".
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _migrate(c: sqlite3.Connection) -> None:
    """Upgrade a database created by an older schema, in place and idempotently.

    Adds reels.recipient and reels.position when missing, gives rows without a
    position position = id, and creates the (status, position) index. Safe to run
    on every start and on fresh databases (where it does nothing).
    """
    cols = {row["name"] for row in c.execute("PRAGMA table_info(reels)")}
    for name, ddl in (("recipient", "TEXT"), ("position", "INTEGER")):
        if name not in cols:
            try:
                c.execute(f"ALTER TABLE reels ADD COLUMN {name} {ddl}")
            except sqlite3.OperationalError as e:
                # another process (scheduler vs dashboard) migrated first
                if "duplicate column" not in str(e).lower():
                    raise
    c.execute("UPDATE reels SET position = id WHERE position IS NULL")
    c.execute("CREATE INDEX IF NOT EXISTS idx_reels_status_position ON reels (status, position)")
    c.commit()


def init_db() -> None:
    with connect() as c:
        # WAL lets the dashboard read while the scheduler writes. The mode is
        # stored in the database file, so setting it once here is enough.
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        _migrate(c)
