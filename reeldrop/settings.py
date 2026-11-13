from .db import connect


def get(key, default=None):
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_(key, value):
    with connect() as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def is_paused() -> bool:
    return get("paused", "0") == "1"


def set_paused(flag: bool):
    set_("paused", "1" if flag else "0")
