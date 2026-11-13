# Phase 4 — Reliability & Safety

**Goal:** recover from errors without duplicates or spam, and protect the account.
**Time:** 2–3 days

## Checklist

- [ ] Pause switch (kill switch) stored in the database
- [ ] Auto-pause on any `BlockedError` + alert to yourself
- [ ] One retry for normal errors
- [ ] Rotating log file
- [ ] Daily cap across all rules (already in Phase 2 — verify)
- [ ] Restart behaviour checked (`misfire_grace_time`, `coalesce`)

## reeldrop/settings.py

```python
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
```

## reeldrop/notify.py  (alerts to yourself via Telegram — optional)

```python
import os
import logging
import requests

log = logging.getLogger("reeldrop")

def notify_me(text: str):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_MY_CHAT_ID")
    if not (token and chat):
        log.info("ALERT: %s", text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=15)
    except Exception as e:
        log.error("Could not send alert: %s", e)
```

## Logging setup (add to the top of main.py)

```python
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

Path("data").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[RotatingFileHandler("data/reeldrop.log", maxBytes=1_000_000, backupCount=3),
              logging.StreamHandler()],
)
```

## reeldrop/job.py  (upgraded version — replaces Phase 2)

```python
import time
import logging
from .queue import next_pending, mark_sent, mark_failed, sent_today
from .settings import is_paused, set_paused
from .senders import BlockedError
from .notify import notify_me

log = logging.getLogger("reeldrop")

def _send_with_retry(sender, recipient, url, wait=60):
    try:
        sender.send(recipient, url)
    except BlockedError:
        raise                       # never retry a block
    except Exception as e:
        log.warning("Send failed (%s), retrying once in %ss", e, wait)
        time.sleep(wait)
        sender.send(recipient, url)

def run_rule(rule, cfg, sender):
    if is_paused():
        log.warning("[%s] paused — skipping", rule.name)
        return
    remaining = cfg.daily_cap - sent_today()
    if remaining <= 0:
        log.info("[%s] daily cap reached", rule.name)
        return
    reels = next_pending(min(rule.count, remaining))
    if not reels:
        notify_me(f"ReelDrop: queue is empty for {rule.name} — add more reels")
        return

    sent = 0
    for i, reel in enumerate(reels):
        try:
            _send_with_retry(sender, rule.recipient, reel["url"])
            mark_sent(reel["id"], rule.recipient, rule.name, sender.name)
            sent += 1
        except BlockedError as e:
            set_paused(True)
            mark_failed(reel["id"], rule.recipient, rule.name, sender.name, f"BLOCKED: {e}")
            notify_me(f"⛔ ReelDrop PAUSED: Instagram blocked/challenged the account.\n{e}")
            break
        except Exception as e:
            mark_failed(reel["id"], rule.recipient, rule.name, sender.name, str(e))
            log.error("[%s] reel %s failed: %s", rule.name, reel["id"], e)
        if i < len(reels) - 1:
            time.sleep(cfg.gap_seconds)

    log.info("[%s] sent %d/%d", rule.name, sent, len(reels))
    notify_me(f"✅ ReelDrop {rule.name}: sent {sent}/{len(reels)}")
```

Also handle a failure **during login** at startup: wrap `get_sender()` in `main.py` in try/except, call `notify_me`, and exit.

## After a pause

1. Open the Instagram app on the test account and clear any warning or security check.
2. Wait at least 24–48 hours.
3. Unpause from the dashboard (Phase 5) or run:
   `python -c "from reeldrop.settings import set_paused; set_paused(False)"`
4. Lower `daily_cap` before resuming.

✅ **Done when:** a simulated failure is logged, retried once, never causes a duplicate, and a `BlockedError` pauses everything and alerts you.
