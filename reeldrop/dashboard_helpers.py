"""Pure helpers for dashboard.py.

Stdlib only at module level so they can be unit-tested without streamlit,
pandas or PyYAML installed. yaml / load_config are imported lazily.
"""
import hmac
import os
import shutil
import tempfile
from datetime import date, timedelta
from typing import Callable

PLACEHOLDER_PASSWORD = "change-me"
STATUS_FILTERS = ("all", "pending", "sent", "failed")


# --- password gate ---------------------------------------------------------

def password_config_error(expected: str | None) -> str | None:
    """Return an error message if the dashboard password is not safely set."""
    if not expected or not expected.strip():
        return "DASHBOARD_PASSWORD is not set. Set a real password in .env and restart the dashboard."
    if expected == PLACEHOLDER_PASSWORD:
        return (
            f'DASHBOARD_PASSWORD is still the placeholder "{PLACEHOLDER_PASSWORD}". '
            "Set a real password in .env and restart the dashboard."
        )
    return None


def password_matches(entered: str | None, expected: str | None) -> bool:
    """Constant-time password comparison. Never true for an empty password."""
    if not entered or not expected:
        return False
    return hmac.compare_digest(entered.encode("utf-8"), expected.encode("utf-8"))


# --- add reels -------------------------------------------------------------

def parse_links(text: str) -> list[str]:
    """Split pasted text into stripped, non-blank lines."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def add_links(
    text: str,
    note: str,
    add_fn: Callable[..., object],
    recipient: str | None = None,
) -> tuple[int, list[str]]:
    """Add each pasted link with add_fn(url, note), or add_fn(url, note,
    recipient=...) when a non-blank recipient is given.

    Returns (added_count, per_line_errors). A ValueError from add_fn is a
    per-line error; any other exception is reported for that line too so one
    bad line never aborts the rest.
    """
    added = 0
    errors: list[str] = []
    note = (note or "").strip()
    recipient = (recipient or "").strip()
    for url in parse_links(text):
        try:
            if recipient:
                add_fn(url, note, recipient=recipient)
            else:
                add_fn(url, note)
            added += 1
        except ValueError as e:
            errors.append(f"{url} — {e}")
        except Exception as e:  # noqa: BLE001 - keep processing remaining lines
            errors.append(f"{url} — unexpected error: {e}")
    return added, errors


# --- queue -----------------------------------------------------------------

def queue_query(status: str) -> tuple[str, tuple]:
    """Return (sql, params) for the queue table. Status is whitelisted."""
    if status not in STATUS_FILTERS:
        raise ValueError(f"Unknown status filter: {status!r}")
    sql = "SELECT id, url, note, status, recipient, position, added_at FROM reels"
    params: tuple = ()
    if status != "all":
        sql += " WHERE status = ?"
        params = (status,)
    if status == "pending":
        # the order reels will be sent in, so Up/Down matches what is shown
        return sql + " ORDER BY COALESCE(position, id), id", params
    return sql + " ORDER BY id DESC", params


def pending_ids(rows) -> list[int]:
    """Ids of the pending reels in rows (dicts with "id" and "status"), in order."""
    return [int(r["id"]) for r in rows if r["status"] == "pending"]


def move_message(reel_id: int, direction: str, moved: bool) -> str:
    """Flash/warning text for a Queue-tab move button press."""
    if moved:
        return f"Moved reel #{reel_id} {direction}"
    return f"Reel #{reel_id} was not moved: it is not pending or already at that end"


def recipient_message(reel_id: int, recipient: str | None, found: bool) -> str:
    """Flash/warning text after assigning a recipient on the Queue tab."""
    if not found:
        return f"No reel with id {reel_id}"
    who = (recipient or "").strip()
    return f"Reel #{reel_id} now goes to {who}" if who else f"Reel #{reel_id} is now unassigned (any rule)"


# --- stats -----------------------------------------------------------------

def _today() -> date:
    from reeldrop.db import now  # same TZ_NAME clock that stamps send_log.sent_at

    return date.fromisoformat(now()[:10])


def _sent_per_day(conn, first_day: date) -> dict[str, int]:
    rows = conn.execute(
        "SELECT substr(sent_at, 1, 10), COUNT(*) FROM send_log "
        "WHERE status='sent' AND substr(sent_at, 1, 10) >= ? "
        "GROUP BY substr(sent_at, 1, 10)",
        (first_day.isoformat(),),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def daily_counts(conn, days: int = 30, today: date | None = None) -> list[tuple[str, int]]:
    """Reels sent per day for the last `days` days, oldest first, zeros included.

    Returns [(YYYY-MM-DD, count), ...] ending with today.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    today = today or _today()
    first = today - timedelta(days=days - 1)
    per_day = _sent_per_day(conn, first)
    out = []
    for i in range(days):
        day = (first + timedelta(days=i)).isoformat()
        out.append((day, per_day.get(day, 0)))
    return out


def weekly_counts(conn, weeks: int = 12, today: date | None = None) -> list[tuple[str, int]]:
    """Reels sent per week (Monday-based) for the last `weeks` weeks, oldest first.

    Returns [(YYYY-MM-DD of the Monday, count), ...]; the last entry is the current week.
    """
    if weeks < 1:
        raise ValueError("weeks must be at least 1")
    today = today or _today()
    this_monday = today - timedelta(days=today.weekday())
    first_monday = this_monday - timedelta(weeks=weeks - 1)
    per_day = _sent_per_day(conn, first_monday)
    totals = {(first_monday + timedelta(weeks=i)).isoformat(): 0 for i in range(weeks)}
    for day, count in per_day.items():
        d = date.fromisoformat(day)
        monday = (d - timedelta(days=d.weekday())).isoformat()
        if monday in totals:
            totals[monday] += count
    return list(totals.items())


def failure_rate(conn) -> dict:
    """All-time {"sent": int, "failed": int, "rate": float} with rate = failed / (sent + failed).

    rate is 0.0 when nothing has been sent or failed yet.
    """
    counts = {"sent": 0, "failed": 0}
    for status, n in conn.execute(
        "SELECT status, COUNT(*) FROM send_log WHERE status IN ('sent', 'failed') GROUP BY status"
    ).fetchall():
        counts[status] = n
    total = counts["sent"] + counts["failed"]
    return {**counts, "rate": counts["failed"] / total if total else 0.0}


# --- schedules -------------------------------------------------------------

def validate_schedules_file(path: str) -> None:
    """Default validator: YAML syntax, then reeldrop rule validation."""
    import yaml  # lazy: not needed by tests that pass a fake validator

    from reeldrop.config import load_config

    with open(path, encoding="utf-8") as f:
        yaml.safe_load(f)
    load_config(path)


def _describe(e: Exception) -> str:
    if isinstance(e, KeyError):
        return f"missing required field {e}"
    return str(e) or e.__class__.__name__


def save_schedules(
    path: str,
    new_text: str,
    validator: Callable[[str], object] | None = None,
) -> None:
    """Validate new_text, then atomically replace the file at path.

    The text is written to a temp file in the same directory and validated
    via validator(temp_path), which must raise on invalid content. Only if
    it passes is the temp file moved over path (os.replace). On any failure
    the original file is untouched, the temp file is removed, and ValueError
    is raised with a readable message.
    """
    validator = validator or validate_schedules_file
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=".schedules-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(new_text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        try:
            validator(tmp_path)
        except Exception as e:  # noqa: BLE001 - any validation failure rejects the save
            raise ValueError(_describe(e)) from e
        if os.path.exists(path):
            shutil.copymode(path, tmp_path)  # keep original permissions
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
