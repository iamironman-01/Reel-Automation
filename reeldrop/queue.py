import re
import sqlite3

from .db import connect, now

# Accepts instagram.com/{reel,reels,p}/<shortcode> with an optional scheme,
# www./m. prefix, optional username segment, and trailing path/query/fragment.
# Anchored at the start so a reel URL buried in another site's query string
# is not accepted.
REEL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?instagram\.com/"
    r"(?:[A-Za-z0-9_.]+/)?(?:reel|reels|p)/([A-Za-z0-9_-]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)


def add_reel(url: str, note: str = "") -> int:
    m = REEL_RE.match(url.strip()) if isinstance(url, str) else None
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
        raise ValueError("This reel is already in the queue") from None


def list_reels(status: str | None = None):
    with connect() as c:
        if status:
            return c.execute(
                "SELECT * FROM reels WHERE status=? ORDER BY id", (status,)
            ).fetchall()
        return c.execute("SELECT * FROM reels ORDER BY id").fetchall()


PICK_ORDERS = ("oldest", "random")


def next_pending(n: int, order: str = "oldest"):
    """Return up to n pending reels, sorted by id.

    order="oldest" takes the n lowest ids; order="random" takes n random pending reels.
    """
    if order == "oldest":
        sql = "SELECT * FROM reels WHERE status='pending' ORDER BY id LIMIT ?"
    elif order == "random":
        # pick at random, then return the picked rows in a stable (id) order
        sql = ("SELECT * FROM (SELECT * FROM reels WHERE status='pending' "
               "ORDER BY RANDOM() LIMIT ?) ORDER BY id")
    else:
        raise ValueError(f"order must be one of {', '.join(PICK_ORDERS)}, got {order!r}")
    with connect() as c:
        return c.execute(sql, (n,)).fetchall()


def _log(c, reel_id, recipient, rule_name, sender_type, status, error=None):
    c.execute(
        "INSERT INTO send_log (reel_id, recipient, rule_name, sender_type, status, error, sent_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (reel_id, recipient, rule_name, sender_type, status, error, now()),
    )


def mark_sent(reel_id, recipient, rule_name, sender_type):
    with connect() as c:  # one transaction: status + log together
        c.execute("UPDATE reels SET status='sent' WHERE id=?", (reel_id,))
        _log(c, reel_id, recipient, rule_name, sender_type, "sent")


def mark_failed(reel_id, recipient, rule_name, sender_type, error: str):
    with connect() as c:
        c.execute("UPDATE reels SET status='failed' WHERE id=?", (reel_id,))
        _log(c, reel_id, recipient, rule_name, sender_type, "failed", error)


def delete_reel(reel_id: int) -> bool:
    """Delete a reel. Returns True if a row was removed, False if it didn't exist."""
    with connect() as c:
        cur = c.execute("DELETE FROM reels WHERE id=?", (reel_id,))
        return cur.rowcount > 0


def sent_today() -> int:
    today = now()[:10]  # same TZ_NAME clock that stamps send_log.sent_at
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM send_log WHERE status='sent' AND substr(sent_at, 1, 10) = ?",
            (today,),
        ).fetchone()
        return row[0]
