import importlib
import time
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from .queue import next_pending, mark_sent, mark_failed, sent_today
from .settings import is_paused, set_paused
from .senders import BlockedError
from .notify import notify_me

log = logging.getLogger("reeldrop")

RETRY_WAIT_SECONDS = 60   # pause before the single retry; tests set this to 0

# Rules that fire at the same minute run in separate scheduler threads. Serialising
# them keeps two runs from picking the same pending reels or overshooting the daily cap.
_RUN_LOCK = threading.Lock()


def _call_with_retry(fn, wait=None):
    """Call fn(); on a non-block error wait and try exactly one more time.

    BlockedError is never retried, whether it comes from the first or second attempt.
    Any other error from the second attempt propagates to the caller.
    """
    if wait is None:
        wait = RETRY_WAIT_SECONDS
    try:
        fn()
    except BlockedError:
        raise                       # never retry a block
    except Exception as e:
        log.warning("Send failed (%s), retrying once in %ss", e, wait)
        time.sleep(wait)
        fn()


def _send_with_retry(sender, recipient, url, wait=None):
    _call_with_retry(lambda: sender.send(recipient, url), wait)


def _send_text_with_retry(sender, recipient, text, wait=None):
    _call_with_retry(lambda: sender.send_text(recipient, text), wait)


def _today(cfg) -> str:
    """Today's date (YYYY-MM-DD) in the configured timezone."""
    return datetime.now(ZoneInfo(cfg.timezone)).date().isoformat()


# Callable returning an instagrapi client for auto-discovery; set once by main().
# While it is None, a short queue is never topped up.
_client_provider = None


def set_client_provider(fn):
    """Register (or clear, with None) the callable discovery uses to get a client."""
    global _client_provider
    _client_provider = fn


def _discovery_enabled(cfg) -> bool:
    return bool(getattr(getattr(cfg, "discovery", None), "enabled", False))


def _top_up(needed: int) -> int:
    """Ask discovery for `needed` more reels. Never raises; returns how many were added."""
    try:
        discovery = importlib.import_module("reeldrop.discovery")
        added = discovery.top_up(needed, _client_provider)
        return added if isinstance(added, int) else 0
    except Exception as e:      # discovery is optional and must never break sending
        log.warning("Auto-discovery top-up failed: %s: %s", type(e).__name__, e)
        return 0


def run_rule(rule, cfg, sender):
    with _RUN_LOCK:
        _run_rule(rule, cfg, sender)


def _run_rule(rule, cfg, sender):
    if is_paused():
        log.warning("[%s] paused — skipping", rule.name)
        return
    today = _today(cfg)
    if today in cfg.quiet_days:
        log.info("[%s] quiet day (%s) — skipping", rule.name, today)
        return
    remaining = cfg.daily_cap - sent_today()
    if remaining <= 0:
        log.info("[%s] daily cap reached", rule.name)
        return
    wanted = min(rule.count, remaining)
    reels = next_pending(wanted, order=rule.pick, recipient=rule.recipient)
    if len(reels) < wanted and _client_provider is not None and _discovery_enabled(cfg):
        # Short queue: try one immediate top-up, then look again.
        log.info("[%s] queue short (%d/%d) — trying auto-discovery", rule.name, len(reels), wanted)
        _top_up(wanted - len(reels))
        if is_paused():                 # a block during discovery pauses everything
            log.warning("[%s] paused by auto-discovery — skipping", rule.name)
            return
        reels = next_pending(wanted, order=rule.pick, recipient=rule.recipient)
    if not reels:
        notify_me(f"ReelDrop: queue is empty for {rule.name} — add more reels")
        return

    sent = 0
    blocked = False
    for i, reel in enumerate(reels):
        if i > 0 and is_paused():       # kill switch pressed while the run is in progress
            log.warning("[%s] paused during run — stopping", rule.name)
            break
        try:
            note = (reel["note"] or "").strip()
            if rule.send_note and note:
                # The note goes first, through the same retry/block handling as the reel.
                # If it fails the reel is not sent. Notes do not count towards daily_cap.
                try:
                    _send_text_with_retry(sender, rule.recipient, note)
                except BlockedError:
                    raise
                except Exception as e:
                    raise RuntimeError(f"note not sent: {e}") from e
            _send_with_retry(sender, rule.recipient, reel["url"])
        except BlockedError as e:
            set_paused(True)
            mark_failed(reel["id"], rule.recipient, rule.name, sender.name, f"BLOCKED: {e}")
            notify_me(f"⛔ ReelDrop PAUSED: Instagram blocked/challenged the account.\n{e}")
            blocked = True
            break                       # later reels stay 'pending'
        except Exception as e:
            mark_failed(reel["id"], rule.recipient, rule.name, sender.name, str(e))
            log.error("[%s] reel %s failed: %s", rule.name, reel["id"], e)
        else:
            # Delivered. Record it immediately; a failure here must not be recorded as
            # a failed send, and continuing would risk re-sending this reel next run.
            try:
                mark_sent(reel["id"], rule.recipient, rule.name, sender.name)
            except Exception as e:
                log.critical("[%s] reel %s was SENT but could not be recorded: %s",
                             rule.name, reel["id"], e)
                try:
                    set_paused(True)
                except Exception:
                    pass
                notify_me(f"⛔ ReelDrop: reel {reel['id']} was sent but the database "
                          f"update failed ({e}). Paused to avoid a duplicate send.")
                blocked = True
                break
            sent += 1
        if i < len(reels) - 1:
            time.sleep(cfg.gap_seconds)

    log.info("[%s] sent %d/%d", rule.name, sent, len(reels))
    if not blocked:
        icon = "✅" if sent == len(reels) else "⚠️"
        notify_me(f"{icon} ReelDrop {rule.name}: sent {sent}/{len(reels)}")
