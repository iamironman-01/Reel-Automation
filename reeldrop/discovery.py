"""Auto-discovery: keep the queue topped up from Explore, hashtags, creators and collections (Route B).

Pieces:
  * fetch_source()   one small function per source type (the only place that touches
                     instagrapi's media endpoints; fix signature quirks there)
  * refill()         fetch from every source, keep reels, add them to the queue
  * tick()           the 60-second scheduler job: decides whether a refill is due
  * top_up()         immediate refill when a rule finds the queue short
  * request_refill() dashboard "Refill now" button: sets a flag that tick() honours

A block/challenge from Instagram raises BlockedError from refill(); tick() and top_up()
turn that into "pause everything + alert", exactly like sending does. tick() and top_up()
never raise. Only stdlib is imported at module level; instagrapi is imported lazily
(through importer / the sender) so this works without it when a client is injected.
"""
import logging
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from . import config as config_mod
from . import db, queue, settings
from .importer import REEL_URL, _attr, _build_client, _fetch_medias, _find_collection_pk
from .importer import _is_block_error, _is_reel
from .notify import notify_me
from .senders.base import BlockedError, redact

log = logging.getLogger("reeldrop")

SETTING_REQUESTED = "discovery_refill_requested"
SETTING_LAST_RUN = "discovery_last_run"
SETTING_LAST_SUMMARY = "discovery_last_summary"

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"     # same format as db.now()
_MAX_ERROR_LINES = 5

# Held while a refill runs so tick() (scheduler job) and top_up() (rule job), which run
# in different scheduler threads, never refill at the same time.
_run_lock = threading.Lock()


def _sleep(seconds: float) -> None:
    """Pause between sources. Module-level so tests can replace it."""
    time.sleep(seconds)


@dataclass
class RefillResult:
    added: list = field(default_factory=list)       # reel URLs added (or that would be added)
    duplicates: int = 0                              # already queued/sent (or seen twice)
    skipped: int = 0                                 # not a reel, no code, too few likes, ...
    errors: list = field(default_factory=list)       # redacted "<source>: <error>" strings
    per_source: dict = field(default_factory=dict)   # "<type>:<value>" -> number added
    dry_run: bool = False


# --- one fetch function per source type ---------------------------------------
# If real instagrapi signatures differ from what is assumed here, fix them here only.

def _fetch_hashtag(client, source, amount):
    if source.mode == "recent":
        return client.hashtag_medias_recent(source.value, amount)
    return client.hashtag_medias_top(source.value, amount)


def _fetch_user(client, source, amount):
    uid = client.user_id_from_username(source.value)
    if hasattr(client, "user_clips"):
        return client.user_clips(uid, amount)
    # No clips endpoint: take the latest posts and over-fetch, since most are not reels.
    return client.user_medias(uid, amount * 3)


def _fetch_collection(client, source, amount):
    pk = _find_collection_pk(client, source.value)      # shared with importer.py
    return _fetch_medias(client, pk, amount)


def _fetch_explore(client, source, amount):
    # No search term: Instagram picks the reels. "reels" is the Explore reels tab and
    # "feed" is the account's own Reels feed. Seen working on a live account (2026-11-14,
    # instagrapi 3.2.12), when the hashtag endpoints returned nothing.
    if source.value == "feed":
        return client.reels(amount)
    return client.explore_reels(amount)


_FETCHERS = {
    "hashtag": _fetch_hashtag,
    "user": _fetch_user,
    "collection": _fetch_collection,
    "explore": _fetch_explore,
}


def fetch_source(client, source, amount: int) -> list:
    """Return the raw media list for one source (not yet filtered)."""
    fetcher = _FETCHERS.get(source.type)
    if fetcher is None:
        raise ValueError(f"Unknown discovery source type {source.type!r}")
    return list(fetcher(client, source, amount) or [])


# --- filtering ----------------------------------------------------------------

def is_reel(media) -> bool:
    """Same rule as the collection importer: video with product_type 'clips' (or unset)."""
    return _is_reel(media)


def passes_likes(media, min_likes: int) -> bool:
    """True if the media has at least min_likes likes. Missing counts as 0 only if min_likes > 0."""
    if min_likes <= 0:
        return True
    likes = _attr(media, "like_count")
    try:
        return int(likes or 0) >= min_likes
    except (TypeError, ValueError):
        return False


def caption_of(media) -> str:
    """The reel's caption as plain text ('' if there is none)."""
    text = _attr(media, "caption_text")
    if text is None:
        caption = _attr(media, "caption")           # raw API dicts nest it
        text = caption.get("text") if isinstance(caption, dict) else caption
    return str(text or "")


def passes_keywords(media, keywords, exclude_keywords) -> bool:
    """Topic filter on the caption, case-insensitive.

    A reel is dropped if any exclude keyword appears in its caption. If `keywords` is not
    empty the caption must also contain at least one of them, so a reel without a caption
    never passes an include filter.
    """
    if not keywords and not exclude_keywords:
        return True
    caption = caption_of(media).casefold()
    if any(word.casefold() in caption for word in exclude_keywords or ()):
        return False
    if keywords:
        return any(word.casefold() in caption for word in keywords)
    return True


KEYWORD_OVERFETCH = 3      # ask for more reels when a keyword filter will drop most of them
MAX_FETCH_AMOUNT = 50


def request_amount(discovery) -> int:
    """How many reels to ask each source for (more when a keyword filter is active)."""
    amount = discovery.fetch_per_source
    if getattr(discovery, "keywords", None):
        amount = min(amount * KEYWORD_OVERFETCH, max(MAX_FETCH_AMOUNT, amount))
    return amount


def source_key(source) -> str:
    return f"{source.type}:{source.value}"


def _safe(text) -> str:
    """Error text for logs/results/alerts: never contains the Instagram password."""
    return redact(text, os.getenv("IG_PASSWORD"))


# --- refill ---------------------------------------------------------------------

def refill(discovery, client, *, dry_run=False, limit=None) -> RefillResult:
    """Fetch reels from every source and add new ones to the queue.

    All Instagram calls happen before the first add_reel, so a block leaves the queue
    untouched. Sources are fetched in random order with `delay_seconds` between them and
    their reels are then added round-robin, so every source contributes before any
    source repeats. At most min(limit or max_per_refill, max_per_refill) reels are added.
    A source that fails with a normal error is recorded in `errors` and skipped; a
    block-type error raises BlockedError at once.
    """
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError(f"limit must be a positive integer, got {limit!r}")
    cap = min(limit or discovery.max_per_refill, discovery.max_per_refill)
    result = RefillResult(dry_run=dry_run)

    order = list(discovery.sources)
    random.shuffle(order)

    # Phase 1: fetch (Instagram calls only)
    pools = []   # (source, deque of (code, url))
    for i, source in enumerate(order):
        key = source_key(source)
        if i > 0 and discovery.delay_seconds > 0:
            _sleep(discovery.delay_seconds)
        try:
            medias = fetch_source(client, source, request_amount(discovery))
        except BlockedError:
            raise
        except Exception as e:
            if _is_block_error(e):
                raise BlockedError(f"{type(e).__name__}: {_safe(e)}") from e
            result.errors.append(_safe(f"{key}: {type(e).__name__}: {e}"))
            result.per_source[key] = 0
            continue
        result.per_source[key] = 0
        items = []
        for media in medias:
            if len(items) >= discovery.fetch_per_source:
                break
            code = _attr(media, "code")
            if not is_reel(media) or not code or not passes_likes(media, discovery.min_likes):
                result.skipped += 1
                continue
            if not passes_keywords(media, getattr(discovery, "keywords", None),
                                   getattr(discovery, "exclude_keywords", None)):
                result.skipped += 1
                continue
            items.append((str(code), REEL_URL.format(code=code)))
        pools.append((source, deque(items)))

    # Phase 2: add round-robin
    known = {r["shortcode"] for r in queue.list_reels()} if dry_run else set()
    while len(result.added) < cap and any(pool for _, pool in pools):
        for source, pool in pools:
            if len(result.added) >= cap:
                break
            if not pool:
                continue
            code, url = pool.popleft()
            key = source_key(source)
            if dry_run:
                if code in known:
                    result.duplicates += 1
                else:
                    known.add(code)
                    result.added.append(url)
                    result.per_source[key] += 1
                continue
            try:
                queue.add_reel(url, note="", recipient=source.to)
            except ValueError as e:
                if "already in the queue" in str(e):
                    result.duplicates += 1
                else:
                    result.skipped += 1
            except Exception as e:  # e.g. database error: record it and keep going
                result.errors.append(_safe(f"{url}: {type(e).__name__}: {e}"))
            else:
                result.added.append(url)
                result.per_source[key] += 1

    log.info("Discovery refill: %d added, %d duplicates, %d skipped, %d errors%s",
             len(result.added), result.duplicates, result.skipped, len(result.errors),
             " (dry run)" if dry_run else "")
    return result


def format_result(result: RefillResult) -> str:
    """One summary line, then up to 5 error lines."""
    verb = "Would add" if result.dry_run else "Added"
    line = (f"{verb} {len(result.added)} reel(s) from {len(result.per_source)} source(s); "
            f"{result.duplicates} already queued, {result.skipped} skipped, "
            f"{len(result.errors)} error(s).")
    details = [f"  error: {e}" for e in result.errors[:_MAX_ERROR_LINES]]
    return "\n".join([line] + details)


def request_refill() -> None:
    """Ask the scheduler's next tick() to refill now, ignoring the throttle."""
    settings.set_(SETTING_REQUESTED, "1")


# --- client provider --------------------------------------------------------------

def make_client_provider(sender=None):
    """Return a zero-arg function that gives an instagrapi client for discovery.

    If the sender has a `cl` attribute (SENDER=instagrapi) its logged-in client is reused,
    so there is still one Instagram login. Otherwise (e.g. SENDER=telegram) a client is
    built lazily on first use through importer._build_client (same IG_USERNAME /
    IG_PASSWORD / data/session.json handling) and cached for later calls. Thread-safe.
    A block during login is raised as BlockedError.
    """
    lock = threading.Lock()
    cache = {}

    def get_client():
        cl = getattr(sender, "cl", None)
        if cl is not None:
            return cl
        with lock:
            if "client" not in cache:
                try:
                    cache["client"] = _build_client()
                except BlockedError:
                    raise
                except Exception as e:
                    if _is_block_error(e):
                        raise BlockedError(f"{type(e).__name__}: {_safe(e)}") from e
                    raise
            return cache["client"]

    return get_client


# --- scheduler entry points ----------------------------------------------------------

def _now(now_fn) -> datetime:
    """Current time as a naive datetime on the db.now() clock (TZ_NAME)."""
    if now_fn is not None:
        return now_fn()
    return datetime.strptime(db.now(), _TIME_FORMAT)


def _load(loader):
    try:
        return (loader or config_mod.load_config)()
    except Exception as e:
        log.error("Discovery: could not load config: %s: %s", type(e).__name__, _safe(e))
        return None


def _minutes_since_last_run(now_fn):
    raw = settings.get(SETTING_LAST_RUN)
    if not raw:
        return None
    try:
        last = datetime.strptime(raw, _TIME_FORMAT)
    except ValueError:
        return None
    return (_now(now_fn) - last).total_seconds() / 60


def _record(summary: str, now_fn) -> None:
    settings.set_(SETTING_LAST_RUN, _now(now_fn).strftime(_TIME_FORMAT))
    settings.set_(SETTING_LAST_SUMMARY, summary)


def _run_refill(discovery, get_client, *, limit, now_fn):
    """Run one refill and record the outcome. Caller holds _run_lock. Never raises."""
    try:
        result = refill(discovery, get_client(), limit=limit)
    except BlockedError as e:
        err = _safe(e)
        log.error("Discovery blocked by Instagram: %s", err)
        settings.set_paused(True)
        notify_me("⛔ ReelDrop PAUSED: Instagram blocked/challenged the account "
                  f"during discovery.\n{err}")
        _record("BLOCKED", now_fn)
        return None
    except Exception as e:
        err = _safe(f"{type(e).__name__}: {e}")
        log.error("Discovery failed: %s", err)
        _record(f"error: {err}", now_fn)    # also sets last_run: no retry every minute
        return None
    _record(format_result(result), now_fn)
    return result


def tick(get_client, *, loader=None, now_fn=None):
    """Scheduler job (every minute): refill if discovery is enabled and due.

    Due when a manual refill was requested (runs even if throttled), or when fewer than
    min_pending reels are pending and at least check_every_minutes passed since the last
    run (or it never ran). Returns the RefillResult, or None if nothing ran (disabled,
    paused, not due, blocked, failed). A pending manual request is cleared when discovery
    is disabled, and kept while sending is paused. Never raises.
    """
    try:
        cfg = _load(loader)
        if cfg is None:
            return None
        discovery = cfg.discovery
        requested = settings.get(SETTING_REQUESTED) == "1"
        if not discovery.enabled:
            if requested:
                settings.set_(SETTING_REQUESTED, "0")
            return None
        if settings.is_paused():
            return None
        if not requested:
            if len(queue.list_reels("pending")) >= discovery.min_pending:
                return None
            minutes = _minutes_since_last_run(now_fn)
            if minutes is not None and 0 <= minutes < discovery.check_every_minutes:
                return None
        with _run_lock:
            if settings.is_paused():     # may have changed while waiting for the lock
                return None
            result = _run_refill(discovery, get_client, limit=None, now_fn=now_fn)
            settings.set_(SETTING_REQUESTED, "0")
            return result
    except Exception as e:  # last line of defence: a scheduler job must not raise
        log.error("Discovery tick failed: %s: %s", type(e).__name__, _safe(e))
        return None


def top_up(needed, get_client, *, loader=None) -> int:
    """Immediate refill when a rule finds the queue short. Returns the number of reels added.

    Does nothing (returns 0) if discovery is disabled or sending is paused. Adds at most
    max(needed, 1) reels, bounded by max_per_refill. Blocks and errors are handled like
    in tick(). Never raises.
    """
    try:
        cfg = _load(loader)
        if cfg is None or not cfg.discovery.enabled or settings.is_paused():
            return 0
        try:
            limit = max(int(needed), 1)
        except (TypeError, ValueError):
            limit = 1
        with _run_lock:
            if settings.is_paused():
                return 0
            result = _run_refill(cfg.discovery, get_client, limit=limit, now_fn=None)
        return len(result.added) if result else 0
    except Exception as e:
        log.error("Discovery top-up failed: %s: %s", type(e).__name__, _safe(e))
        return 0
