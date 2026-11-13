import os
import logging

from .senders.base import redact

log = logging.getLogger("reeldrop")


def notify_me(text: str):
    """Alert yourself via Telegram. Never raises; falls back to the log."""
    log.info("ALERT: %s", text)
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_MY_CHAT_ID")
    if not (token and chat):
        return
    try:
        import requests  # imported here so the job runs (and is testable) without it

        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text}, timeout=15)
        if r.status_code >= 400:
            log.error("Could not send alert: HTTP %s", r.status_code)
    except Exception as e:
        # the request URL contains the bot token, so redact it from the message
        log.error("Could not send alert: %s: %s", type(e).__name__, redact(e, token))
