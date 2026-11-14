"""Database access: SQLite by default, PostgreSQL (e.g. Supabase) when DATABASE_URL is set.

Callers use one API for both:

    with connect() as c:                 # commits (or rolls back) and releases on exit
        row = c.execute("SELECT ... WHERE id=?", (1,)).fetchone()
        row["status"], row[0]            # rows work by column name and by position

SQL is written once with `?` placeholders and portable syntax; the PostgreSQL wrapper
turns `?` into `%s`. Use `RETURNING id` instead of cursor.lastrowid, and
`begin_write(c)` before a read-then-write that must not interleave with other writers.

On a host without a disk (Render's free plan) DATABASE_URL keeps the queue, the send
history and the settings across restarts. Supabase: use the *Session pooler* connection
string (IPv4, port 5432) from Project Settings -> Database.
"""
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path("data/reeldrop.db")

BUSY_TIMEOUT_MS = 5000
DATABASE_URL_ENV = "DATABASE_URL"

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

PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS reels (
        id        BIGSERIAL PRIMARY KEY,
        url       TEXT NOT NULL,
        shortcode TEXT NOT NULL UNIQUE,
        note      TEXT DEFAULT '',
        status    TEXT NOT NULL DEFAULT 'pending',
        added_at  TEXT NOT NULL,
        position  BIGINT,
        recipient TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS send_log (
        id          BIGSERIAL PRIMARY KEY,
        reel_id     BIGINT,
        recipient   TEXT,
        rule_name   TEXT,
        sender_type TEXT,
        status      TEXT,
        error       TEXT,
        sent_at     TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    )""",
    "ALTER TABLE reels ADD COLUMN IF NOT EXISTS recipient TEXT",
    "ALTER TABLE reels ADD COLUMN IF NOT EXISTS position BIGINT",
    "UPDATE reels SET position = id WHERE position IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_reels_status_position ON reels (status, position)",
]


def now() -> str:
    tz = ZoneInfo(os.getenv("TZ_NAME", "Asia/Kolkata"))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


def database_url() -> str:
    """The PostgreSQL URL, or "" for the local SQLite file."""
    return os.environ.get(DATABASE_URL_ENV, "").strip()


def using_postgres() -> bool:
    return bool(database_url())


def keeps_state() -> bool:
    """True when the database outlives the container (DATABASE_URL), so the Instagram
    session and the schedule are kept in it too. Separate from using_postgres() so tests
    can switch it on while using a local SQLite file."""
    return using_postgres()


def _driver_errors():
    errors = [sqlite3.Error]
    try:
        import psycopg
        errors.append(psycopg.Error)
    except ImportError:
        pass
    return tuple(errors)


def _integrity_errors():
    errors = [sqlite3.IntegrityError]
    try:
        import psycopg
        errors.append(psycopg.errors.IntegrityError)
    except ImportError:
        pass
    return tuple(errors)


# Catch these instead of sqlite3.Error so code works with either database.
DB_ERRORS = _driver_errors()
INTEGRITY_ERRORS = _integrity_errors()


# --- SQLite -----------------------------------------------------------------------

class _Connection(sqlite3.Connection):
    """sqlite3.Connection whose `with` block also closes the connection.

    The stock context manager only commits (or rolls back on error) and leaves
    the connection open. Closing here keeps `with connect() as c:` leak-free.
    """

    is_postgres = False

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _connect_sqlite() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000, factory=_Connection
    )
    conn.row_factory = sqlite3.Row
    # The scheduler and dashboard share this file; wait instead of failing
    # immediately with "database is locked".
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


# --- PostgreSQL -------------------------------------------------------------------

def _qmarks_to_pyformat(sql: str) -> str:
    """Turn `?` placeholders into `%s` (and escape literal %), outside quoted strings."""
    out, quote = [], None
    for ch in sql:
        if quote:
            out.append("%%" if ch == "%" else ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    return "".join(out)


class Row(tuple):
    """A result row readable by position and by column name, like sqlite3.Row."""

    def __new__(cls, values, names):
        row = super().__new__(cls, values)
        row._names = names
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._names.index(key))
            except ValueError:
                raise IndexError(f"No column named {key!r}") from None
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)


def _row_factory(cursor):
    names = [d.name for d in (cursor.description or [])]
    return lambda values: Row(values, names)


class _PgCursor:
    def __init__(self, cursor):
        self._cur = cursor

    def execute(self, sql, params=()):
        self._cur.execute(_qmarks_to_pyformat(sql), tuple(params or ()))
        return self

    def executemany(self, sql, seq):
        self._cur.executemany(_qmarks_to_pyformat(sql), [tuple(p) for p in seq])
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur.fetchall())

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def close(self):
        self._cur.close()


class _PgConnection:
    """psycopg connection from the pool with the same small API as the SQLite one."""

    is_postgres = True

    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()

    def cursor(self):
        if self._conn is None:
            import psycopg
            raise psycopg.InterfaceError("the connection was already released")
        return _PgCursor(self._conn.cursor(row_factory=_row_factory))

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def executemany(self, sql, seq):
        return self.cursor().executemany(sql, seq)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._conn is not None:
            self._pool.putconn(self._conn)
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self.close()
        return False


_pool = None
_pool_url = None
_pool_lock = threading.Lock()


def _get_pool():
    """One small connection pool per process, re-created if DATABASE_URL changes."""
    global _pool, _pool_url
    url = database_url()
    with _pool_lock:
        if _pool is None or _pool_url != url:
            from psycopg_pool import ConnectionPool  # lazy: only needed with DATABASE_URL

            if _pool is not None:
                _pool.close()
            # prepare_threshold=None: Supabase's transaction pooler cannot use
            # prepared statements. max_size stays small for the free plan's limits.
            _pool = ConnectionPool(
                url, min_size=0, max_size=4, timeout=30, open=True,
                kwargs={"prepare_threshold": None, "connect_timeout": 15},
            )
            _pool_url = url
        return _pool


def close_pool() -> None:
    global _pool, _pool_url
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool, _pool_url = None, None


# --- shared ------------------------------------------------------------------------

def connect():
    """A connection to SQLite, or to PostgreSQL when DATABASE_URL is set."""
    if using_postgres():
        return _PgConnection(_get_pool())
    return _connect_sqlite()


def begin_write(c) -> None:
    """Start a transaction that blocks other writers until it ends (read-then-write)."""
    if getattr(c, "is_postgres", False):
        c.execute("LOCK TABLE reels IN SHARE ROW EXCLUSIVE MODE")
    else:
        c.execute("BEGIN IMMEDIATE")


def read_rows(c, sql, params=()):
    """(column_names, rows) for a SELECT, for tables in the dashboard."""
    cur = c.execute(sql, params)
    names = [d[0] for d in (cur.description or [])]
    return names, cur.fetchall()


def _migrate(c: sqlite3.Connection) -> None:
    """Upgrade a SQLite database created by an older schema, in place and idempotently.

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
    if using_postgres():
        with connect() as c:
            # Two processes (dashboard and scheduler) may start at once: serialise the DDL.
            c.execute("SELECT pg_advisory_xact_lock(7316)")
            for statement in PG_SCHEMA:
                c.execute(statement)
        return
    with connect() as c:
        # WAL lets the dashboard read while the scheduler writes. The mode is
        # stored in the database file, so setting it once here is enough.
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        _migrate(c)
