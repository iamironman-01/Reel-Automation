# Phase 3 — Real Sender

**Goal:** deliver reels for real using the route you picked.
**Time:** 3–5 days

> ⚠️ Test with **your test account sending to your own second account** first. Only then send to your friend.

## Checklist

- [ ] Sender for your route implemented
- [ ] Credentials only in `.env`
- [ ] Blocks/challenges raise `BlockedError`
- [ ] Tested: test account → your second account
- [ ] Tested: test account → friend (with their consent)

---

## Route B — instagrapi (real Instagram DM share)

Risk: against Instagram's Terms. Test account, low volumes. Library behaviour changes often — check its docs and GitHub issues if something breaks.

### reeldrop/senders/instagrapi_sender.py

```python
import os
from pathlib import Path
from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired, FeedbackRequired, LoginRequired, PleaseWaitFewMinutes,
)
from .base import Sender, BlockedError

SESSION = Path("data/session.json")

class InstagrapiSender(Sender):
    name = "instagrapi"

    def __init__(self):
        self.cl = Client()
        if SESSION.exists():
            self.cl.load_settings(SESSION)          # reuse device + cookies
        self.cl.login(os.environ["IG_USERNAME"], os.environ["IG_PASSWORD"])
        self.cl.dump_settings(SESSION)               # save for next start
        self._user_ids = {}

    def _uid(self, username):
        if username not in self._user_ids:           # look up once, then cache
            self._user_ids[username] = self.cl.user_id_from_username(username)
        return self._user_ids[username]

    def send(self, recipient, reel_url):
        try:
            uid = self._uid(recipient)
            media_pk = self.cl.media_pk_from_url(reel_url)
            media_id = self.cl.media_id(media_pk)
            try:
                self.cl.direct_media_share(media_id, [uid])   # native reel share
            except Exception:
                self.cl.direct_send(reel_url, [int(uid)])     # fallback: send link
        except (ChallengeRequired, FeedbackRequired, LoginRequired, PleaseWaitFewMinutes) as e:
            raise BlockedError(str(e)) from e
```

Tips:
- Log in **once manually** in the Instagram app on the test account, complete any security check, then run the script.
- Run it from your **home internet** at first; logins from cloud servers trigger more security checks.
- Keep `daily_cap` low (5–10) and `gap_seconds` ≥ 30.

---

## Route A — Official Instagram Messaging API

Works only if: the sending account is a **Business/Creator** account connected to a **Meta developer app**, and your friend has **messaged that account in the last 24 hours**. It sends the reel **link** as text.

Setup outline:
1. Convert the sending account to Professional (Creator/Business).
2. Create an app at developers.facebook.com, add the Instagram product, generate an access token with messaging permission.
3. Set up a webhook to receive messages and get your friend's **IGSID** (Instagram-scoped user ID) when they message you.

### reeldrop/senders/official_api_sender.py

```python
import os
import requests
from .base import Sender

API_VERSION = "v21.0"   # check the current version in Meta's docs

class OfficialApiSender(Sender):
    name = "official"

    def __init__(self):
        self.token = os.environ["IG_ACCESS_TOKEN"]
        self.igsid = os.environ["IG_RECIPIENT_IGSID"]   # friend's IGSID from the webhook

    def send(self, recipient, reel_url):
        r = requests.post(
            f"https://graph.instagram.com/{API_VERSION}/me/messages",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"recipient": {"id": self.igsid}, "message": {"text": reel_url}},
            timeout=20,
        )
        if r.status_code != 200:
            raise RuntimeError(f"API error {r.status_code}: {r.text}")
```

Limitation: if your friend hasn't messaged in 24 h, the API rejects the send. Realistic only if they reply/message daily.

---

## Route D — Telegram bot (zero risk)

1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Your friend opens your bot and presses **Start**.
3. Get their chat ID: open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser after they press Start, and read `message.chat.id`.

### reeldrop/senders/telegram_sender.py

```python
import os
import requests
from .base import Sender

class TelegramSender(Sender):
    name = "telegram"

    def __init__(self):
        self.token = os.environ["TELEGRAM_BOT_TOKEN"]
        self.chat_id = os.environ["TELEGRAM_FRIEND_CHAT_ID"]

    def send(self, recipient, reel_url):
        r = requests.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": f"🎬 {reel_url}"},
            timeout=20,
        )
        r.raise_for_status()
```

---

## Switch sender

In `.env`:

```text
SENDER=instagrapi      # or official / telegram / dry_run
```

✅ **Done when:** reels arrive at the scheduled time, in order, with gaps between them.
