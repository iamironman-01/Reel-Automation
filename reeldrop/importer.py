"""Auto-import: add reels from a saved Instagram collection to the queue (Route B).

Uses instagrapi's collection endpoints. Block-type errors are turned into
BlockedError and propagate; this module never pauses the system, the caller decides.
"""
import inspect
import logging
from dataclasses import dataclass, field

from . import queue
from .senders.base import BlockedError

log = logging.getLogger("reeldrop")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# instagrapi exception class names that mean "Instagram is challenging/throttling us".
# Matched by name (anywhere in the MRO) so we need no instagrapi import for injected clients.
BLOCK_ERROR_NAMES = frozenset(
    {"ChallengeRequired", "FeedbackRequired", "LoginRequired", "PleaseWaitFewMinutes"}
)

REEL_URL = "https://www.instagram.com/reel/{code}/"
MEDIA_TYPE_VIDEO = 2          # instagrapi: 1 photo, 2 video/reel/igtv, 8 album


@dataclass
class ImportResult:
    collection: str = ""
    dry_run: bool = False
    added: list = field(default_factory=list)       # URLs added (or that would be added)
    duplicates: list = field(default_factory=list)  # URLs already in the queue
    skipped: list = field(default_factory=list)     # "<what>: <reason>" for non-reels etc.
    errors: list = field(default_factory=list)      # "<what>: <error>" for unexpected failures


def format_result(result: ImportResult) -> str:
    verb = "Would add" if result.dry_run else "Added"
    line = (f"{verb} {len(result.added)} reel(s) from '{result.collection}'; "
            f"{len(result.duplicates)} already queued, "
            f"{len(result.skipped)} skipped, {len(result.errors)} error(s).")
    details = [f"  skipped: {s}" for s in result.skipped[:5]]
    details += [f"  error: {e}" for e in result.errors[:5]]
    return "\n".join([line] + details)


# --- helpers -----------------------------------------------------------------

def _is_block_error(exc: BaseException) -> bool:
    return any(c.__name__ in BLOCK_ERROR_NAMES for c in type(exc).__mro__)


def _blocked(exc: BaseException) -> BlockedError:
    return BlockedError(f"{type(exc).__name__}: {exc}")


def _build_client():
    """Build a logged-in instagrapi Client.

    Reuses InstagrapiSender (same IG_USERNAME / IG_PASSWORD env vars and
    data/session.json handling) and takes its `.cl`, so login code is not duplicated
    and instagrapi_sender.py stays untouched. Imported lazily so the importer works
    without instagrapi when a client is injected.
    """
    from .senders.instagrapi_sender import InstagrapiSender
    try:
        return InstagrapiSender().cl
    except KeyError as e:
        raise RuntimeError(f"Missing environment variable {e.args[0]} (needed to log in)") from None


def _attr(obj, *names):
    for n in names:
        value = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if value not in (None, ""):
            return value
    return None


def _find_collection_pk(client, name: str):
    wanted = name.strip().casefold()
    collections = list(client.collections() or [])
    names = []
    for col in collections:
        col_name = _attr(col, "name")
        if col_name is not None:
            names.append(str(col_name))
        if col_name is not None and str(col_name).strip().casefold() == wanted:
            pk = _attr(col, "id", "pk")
            if pk is None:
                raise RuntimeError(f"Collection '{col_name}' has no id")
            return pk
    available = ", ".join(sorted(names)) or "(none)"
    raise ValueError(f"Collection '{name}' not found. Available collections: {available}")


def _fetch_medias(client, collection_pk, amount: int):
    """The one place that calls collection_medias; adjust here if instagrapi changes.

    instagrapi 2.x: collection_medias(collection_pk, ...). Some versions accept an
    `amount` argument, others return a single page; pass it only if supported.
    """
    fn = client.collection_medias
    try:
        accepts_amount = "amount" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        accepts_amount = False
    medias = fn(collection_pk, amount=amount) if accepts_amount else fn(collection_pk)
    return list(medias or [])


def _is_reel(media) -> bool:
    if _attr(media, "media_type") != MEDIA_TYPE_VIDEO:
        return False
    product_type = _attr(media, "product_type")
    return product_type is None or product_type == "clips"


# --- public API --------------------------------------------------------------

def import_collection(collection_name, client=None, limit=DEFAULT_LIMIT,
                      dry_run=False) -> ImportResult:
    """Queue the reels in the saved collection `collection_name`.

    `limit` is the maximum number of reels added (or that would be added) in one run;
    duplicates and skipped items do not count against it. It is capped at MAX_LIMIT.
    Raises ValueError for an unknown collection or a bad limit, BlockedError when
    Instagram blocks us (nothing is queued in that case: all Instagram calls happen
    before the first add_reel).
    """
    if not isinstance(collection_name, str) or not collection_name.strip():
        raise ValueError("Collection name is required")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")
    limit = min(limit, MAX_LIMIT)

    result = ImportResult(collection=collection_name.strip(), dry_run=dry_run)
    try:
        if client is None:
            client = _build_client()
        pk = _find_collection_pk(client, collection_name)
        medias = _fetch_medias(client, pk, limit)
    except BlockedError:
        raise
    except Exception as e:
        if _is_block_error(e):
            raise _blocked(e) from e
        raise

    for media in medias:
        if len(result.added) >= limit:
            break
        code = _attr(media, "code")
        label = str(code or _attr(media, "pk", "id") or "?")
        if not _is_reel(media):
            result.skipped.append(f"{label}: not a reel")
            continue
        if not code:
            result.skipped.append(f"{label}: no media code")
            continue
        url = REEL_URL.format(code=code)
        if dry_run:
            result.added.append(url)
            continue
        try:
            queue.add_reel(url)
        except ValueError as e:
            if "already in the queue" in str(e):
                result.duplicates.append(url)
            else:
                result.skipped.append(f"{url}: {e}")
        except Exception as e:  # e.g. database error: record it, keep going
            result.errors.append(f"{url}: {type(e).__name__}: {e}")
        else:
            result.added.append(url)

    log.info("Import from '%s': %d added, %d duplicates, %d skipped, %d errors%s",
             result.collection, len(result.added), len(result.duplicates),
             len(result.skipped), len(result.errors), " (dry run)" if dry_run else "")
    return result
