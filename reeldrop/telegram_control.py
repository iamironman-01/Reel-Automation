"""Control ReelDrop from your phone through a Telegram bot.

Commands (sent from the chat whose id is TELEGRAM_MY_CHAT_ID; every other chat is ignored):

    /add <link> [note]   queue a reel (a bare reel link works too; several links per message are ok)
    /pause, /resume      flip the kill switch
    /status              paused/running, sent today vs daily cap, pending and failed counts
    /queue               next 5 pending reels
    /help                usage

The bot uses long polling (getUpdates), so no public URL is needed. Environment:
TELEGRAM_BOT_TOKEN and TELEGRAM_MY_CHAT_ID (the same variables notify.py uses).
"""
import logging
import os
import threading
import time

from . import queue, settings
from .senders.base import redact

log = logging.getLogger("reeldrop")

API = "https://api.telegram.org/bot{token}/{method}"
MAX_INPUT_CHARS = 2000
MAX_NOTE_CHARS = 500
MAX_LINKS_PER_MESSAGE = 10
MAX_REPLY_CHARS = 4000
QUEUE_PREVIEW = 5
BACKOFF_START = 1.0
BACKOFF_CAP = 60.0

HELP_TEXT = (
    "ReelDrop commands:\n"
    "/add <link> [note] - queue a reel (or just paste reel links)\n"
    "/pause - stop all sending\n"
    "/resume - allow sending again\n"
    "/status - state, sent today, pending, failed\n"
    "/queue - next 5 pending reels\n"
    "/help - this message"
)


class TelegramControlError(RuntimeError):
    """Misconfiguration or a failed Telegram API call. Messages never contain the bot token."""


# --- command handling (pure: text in, reply text out) ------------------------

def _clean_token(tok: str) -> str:
    return tok.strip("<>()[]\"'")


def _split_links(args: str):
    """Split text into (reel links, leftover words used as the note)."""
    links, rest = [], []
    for tok in args.split():
        if queue.REEL_RE.match(_clean_token(tok)):
            links.append(_clean_token(tok))
        else:
            rest.append(tok)
    return links, " ".join(rest)[:MAX_NOTE_CHARS]


def _cmd_add(args: str) -> str:
    links, note = _split_links(args)
    if not args.strip():
        return "Usage: /add <instagram reel link> [note]"
    if not links:
        return "That doesn't look like an Instagram reel/post link."
    results = []
    added = 0
    for url in links[:MAX_LINKS_PER_MESSAGE]:
        try:
            queue.add_reel(url, note)
            added += 1
            results.append(f"Added: {url}")
        except ValueError as e:
            results.append(f"Skipped ({e}): {url}")
    if len(links) > MAX_LINKS_PER_MESSAGE:
        results.append(f"Ignored {len(links) - MAX_LINKS_PER_MESSAGE} extra links "
                       f"(max {MAX_LINKS_PER_MESSAGE} per message).")
    if len(links) > 1:
        results.insert(0, f"Added {added} of {min(len(links), MAX_LINKS_PER_MESSAGE)} links.")
    return "\n".join(results)


def _daily_cap():
    """Daily cap from the schedule config, or None if the config can't be loaded."""
    try:
        from .config import load_config
        return load_config().daily_cap
    except Exception as e:
        log.debug("Telegram /status: config not available (%s)", type(e).__name__)
        return None


def _cmd_status() -> str:
    state = "PAUSED" if settings.is_paused() else "running"
    cap = _daily_cap()
    sent = queue.sent_today()
    sent_part = f"{sent}/{cap}" if cap is not None else str(sent)
    pending = len(queue.list_reels("pending"))
    failed = len(queue.list_reels("failed"))
    return (f"State: {state}\nSent today: {sent_part}\n"
            f"Pending: {pending}\nFailed: {failed}")


def _cmd_queue() -> str:
    pending = queue.list_reels("pending")
    if not pending:
        return "The queue is empty."
    lines = [f"Pending: {len(pending)}. Next {min(QUEUE_PREVIEW, len(pending))}:"]
    lines += [f"{i}. {r['url']}" for i, r in enumerate(pending[:QUEUE_PREVIEW], start=1)]
    return "\n".join(lines)


def handle_command(text: str) -> str:
    """Return the reply for one incoming message. Never raises."""
    if not isinstance(text, str) or not text.strip():
        return HELP_TEXT
    if len(text) > MAX_INPUT_CHARS:
        return f"Message too long (max {MAX_INPUT_CHARS} characters)."
    text = text.strip()
    try:
        if not text.startswith("/"):
            if any(queue.REEL_RE.match(_clean_token(t)) for t in text.split()):
                return _cmd_add(text)       # bare reel link(s) = implicit /add
            return "I didn't understand that. Send /help for the commands."
        head, _, args = text.partition(" ")
        command = head.split("@", 1)[0].lower()     # "/status@MyBot" in group-style mentions
        args = args.strip()
        if command in ("/start", "/help"):
            return HELP_TEXT
        if command == "/add":
            return _cmd_add(args)
        if command == "/pause":
            settings.set_paused(True)
            return "Paused. Nothing will be sent until you send /resume."
        if command == "/resume":
            settings.set_paused(False)
            return "Resumed. Scheduled sends are active again."
        if command == "/status":
            return _cmd_status()
        if command == "/queue":
            return _cmd_queue()
        return "Unknown command. Send /help for the commands."
    except Exception as e:
        log.error("Telegram command failed: %s: %s", type(e).__name__, e)
        return "Something went wrong while handling that command. Check the ReelDrop log."


# --- Telegram plumbing --------------------------------------------------------

def _credentials():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_MY_CHAT_ID", "").strip()
    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_MY_CHAT_ID", chat)) if not v]
    if missing:
        raise TelegramControlError(
            "Telegram control needs " + " and ".join(missing) + " to be set in the environment")
    return token, chat


def process_update(update: dict) -> str | None:
    """Reply text for one Telegram update, or None (not from the owner / not a text message)."""
    try:
        _, owner = _credentials()
    except TelegramControlError:
        return None                      # fail closed
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if chat_id is None or str(chat_id) != owner:
        log.warning("Ignored Telegram message from unauthorised chat %s", chat_id)
        return None
    text = message.get("text")
    if not isinstance(text, str):
        return None
    return handle_command(text)


def _send_reply(token: str, chat_id: str, text: str):
    """Send a reply; failures are logged, never raised."""
    try:
        import requests  # imported here so the rest of ReelDrop works without it

        r = requests.post(API.format(token=token, method="sendMessage"),
                          json={"chat_id": chat_id, "text": text[:MAX_REPLY_CHARS]}, timeout=15)
        if r.status_code >= 400:
            log.error("Could not send Telegram reply: HTTP %s", r.status_code)
    except Exception as e:
        log.error("Could not send Telegram reply: %s: %s", type(e).__name__, redact(e, token))


def poll_once(offset: int | None, timeout: int = 25, *, raise_on_error: bool = False) -> int | None:
    """Fetch and answer one batch of updates. Returns the offset for the next call.

    On a getUpdates failure the offset is returned unchanged and the error is logged; with
    raise_on_error=True a token-free TelegramControlError is raised instead (run_forever
    uses that to back off).
    """
    token, chat = _credentials()
    try:
        import requests

        params = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        r = requests.get(API.format(token=token, method="getUpdates"),
                         params=params, timeout=timeout + 10)
        if r.status_code >= 400:
            raise TelegramControlError(f"getUpdates failed: HTTP {r.status_code}")
        data = r.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise TelegramControlError("getUpdates failed: Telegram returned ok=false")
        updates = data.get("result") or []
    except TelegramControlError as e:
        if raise_on_error:
            raise
        log.error("%s", e)
        return offset
    except Exception as e:
        err = TelegramControlError(f"getUpdates failed: {type(e).__name__}: {redact(e, token)}")
        if raise_on_error:
            raise err from None
        log.error("%s", err)
        return offset

    new_offset = offset
    for update in updates:
        try:
            uid = update.get("update_id")
            if isinstance(uid, int) and (new_offset is None or uid + 1 > new_offset):
                new_offset = uid + 1     # advance first so a bad update is never re-fetched
            reply = process_update(update)
        except Exception as e:
            log.error("Telegram update failed: %s: %s", type(e).__name__, redact(e, token))
            continue
        if reply:
            _send_reply(token, chat, reply)
    return new_offset


def _sleep(seconds: float, stop_event=None):
    if stop_event is not None:
        stop_event.wait(seconds)
    else:
        time.sleep(seconds)


def run_forever(stop_event=None):
    """Poll until stop_event is set (or forever). Raises TelegramControlError if not configured."""
    _credentials()
    log.info("Telegram control started")
    offset, delay = None, BACKOFF_START
    while stop_event is None or not stop_event.is_set():
        try:
            offset = poll_once(offset, raise_on_error=True)
            delay = BACKOFF_START
        except TelegramControlError as e:
            log.error("Telegram polling error (retrying in %.0fs): %s", delay, e)
            _sleep(delay, stop_event)
            delay = min(delay * 2, BACKOFF_CAP)
        except Exception as e:      # defensive: the loop must not die
            log.error("Telegram polling crashed (retrying in %.0fs): %s", delay, type(e).__name__)
            _sleep(delay, stop_event)
            delay = min(delay * 2, BACKOFF_CAP)
    log.info("Telegram control stopped")


def start_in_thread(stop_event=None) -> threading.Thread | None:
    """Start the poller in a daemon thread, or return None if the env isn't configured."""
    try:
        _credentials()
    except TelegramControlError as e:
        log.info("Telegram control disabled: %s", e)
        return None
    t = threading.Thread(target=run_forever, args=(stop_event,),
                         name="telegram-control", daemon=True)
    t.start()
    return t
