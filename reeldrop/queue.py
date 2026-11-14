import re

from .db import INTEGRITY_ERRORS, begin_write, connect, now

# Accepts instagram.com/{reel,reels,p}/<shortcode> with an optional scheme,
# www./m. prefix, optional username segment, and trailing path/query/fragment.
# Anchored at the start so a reel URL buried in another site's query string
# is not accepted.
REEL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?instagram\.com/"
    r"(?:[A-Za-z0-9_.]+/)?(?:reel|reels|p)/([A-Za-z0-9_-]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)


def _clean_recipient(recipient: str | None) -> str | None:
    """Strip a recipient; blank or None means unassigned (None)."""
    if recipient is None:
        return None
    return str(recipient).strip() or None


def add_reel(url: str, note: str = "", recipient: str | None = None) -> int:
    """Add a reel to the end of the queue. recipient=None leaves it unassigned."""
    m = REEL_RE.match(url.strip()) if isinstance(url, str) else None
    if not m:
        raise ValueError("Not an Instagram reel/post link")
    shortcode = m.group(1)
    clean_url = f"https://www.instagram.com/reel/{shortcode}/"
    try:
        with connect() as c:
            # position is computed inside the INSERT, so it is atomic with it
            row = c.execute(
                "INSERT INTO reels (url, shortcode, note, added_at, recipient, position) "
                "VALUES (?, ?, ?, ?, ?, (SELECT COALESCE(MAX(position), 0) + 1 FROM reels)) "
                "RETURNING id",
                (clean_url, shortcode, note, now(), _clean_recipient(recipient)),
            ).fetchone()
            return row[0]
    except INTEGRITY_ERRORS:
        raise ValueError("This reel is already in the queue") from None


def list_reels(status: str | None = None):
    """All reels (or one status) by id; pending reels are listed in queue order."""
    with connect() as c:
        if status == "pending":
            return c.execute(
                "SELECT * FROM reels WHERE status='pending' "
                "ORDER BY COALESCE(position, id), id"
            ).fetchall()
        if status:
            return c.execute(
                "SELECT * FROM reels WHERE status=? ORDER BY id", (status,)
            ).fetchall()
        return c.execute("SELECT * FROM reels ORDER BY id").fetchall()


PICK_ORDERS = ("oldest", "random")


def next_pending(n: int, order: str = "oldest", recipient: str | None = None):
    """Return up to n pending reels.

    order="oldest" takes the first n in queue order (position, then id) and returns
    them in that order; order="random" takes n random pending reels and returns them
    sorted by id.

    recipient=None considers every pending reel. With a recipient, only reels
    assigned to that recipient (case-insensitive) or unassigned (NULL) are
    considered, so a reel assigned to one friend is never returned for another.
    """
    if order not in PICK_ORDERS:
        raise ValueError(f"order must be one of {', '.join(PICK_ORDERS)}, got {order!r}")
    where = "status='pending'"
    params: list = []
    if recipient is not None:
        where += " AND (recipient IS NULL OR LOWER(recipient) = LOWER(?))"
        params.append(recipient.strip())
    if order == "oldest":
        sql = f"SELECT * FROM reels WHERE {where} ORDER BY COALESCE(position, id), id LIMIT ?"
    else:
        # pick at random, then return the picked rows in a stable (id) order
        sql = (f"SELECT * FROM (SELECT * FROM reels WHERE {where} "
               "ORDER BY RANDOM() LIMIT ?) AS picked ORDER BY id")
    params.append(n)
    with connect() as c:
        return c.execute(sql, params).fetchall()


def set_recipient(reel_id: int, recipient: str | None) -> bool:
    """Assign a reel to a recipient (None or blank = unassigned).

    Returns True if the reel exists, False otherwise.
    """
    with connect() as c:
        cur = c.execute(
            "UPDATE reels SET recipient=? WHERE id=?", (_clean_recipient(recipient), reel_id)
        )
        return cur.rowcount > 0


MOVE_DIRECTIONS = ("up", "down", "top", "bottom")


def reorder_ids(ids: list, reel_id, direction: str) -> list:
    """Pure helper: return ids (in queue order) with reel_id moved per direction.

    Returns the list unchanged if reel_id is absent or already at that edge.
    """
    if direction not in MOVE_DIRECTIONS:
        raise ValueError(f"direction must be one of {', '.join(MOVE_DIRECTIONS)}, got {direction!r}")
    if reel_id not in ids:
        return list(ids)
    i = ids.index(reel_id)
    out = list(ids)
    out.pop(i)
    if direction == "up":
        j = max(i - 1, 0)
    elif direction == "down":
        j = min(i + 1, len(ids) - 1)
    elif direction == "top":
        j = 0
    else:
        j = len(ids) - 1
    out.insert(j, reel_id)
    return out


def move_reel(reel_id: int, direction: str) -> bool:
    """Move a pending reel within the pending queue.

    direction is "up", "down", "top" or "bottom". Only pending reels take part;
    sent/failed reels keep their positions. Returns False if the reel is not
    pending (or missing) or already at that edge. Runs in one transaction and
    keeps positions unique: the pending reels reuse the same set of position
    values, in the new order.
    """
    if direction not in MOVE_DIRECTIONS:
        raise ValueError(f"direction must be one of {', '.join(MOVE_DIRECTIONS)}, got {direction!r}")
    with connect() as c:
        begin_write(c)                 # take the write lock before reading the order
        rows = c.execute(
            "SELECT id, COALESCE(position, id) AS pos FROM reels WHERE status='pending' "
            "ORDER BY COALESCE(position, id), id"
        ).fetchall()
        ids = [r["id"] for r in rows]
        new_ids = reorder_ids(ids, reel_id, direction)
        if new_ids == ids:
            return False
        slots = sorted(r["pos"] for r in rows)
        if len(set(slots)) != len(slots):   # damaged data: renumber after the current max
            top = c.execute("SELECT COALESCE(MAX(position), 0) FROM reels").fetchone()[0]
            slots = list(range(top + 1, top + 1 + len(ids)))
        c.executemany(
            "UPDATE reels SET position=? WHERE id=?", list(zip(slots, new_ids))
        )
        return True


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
