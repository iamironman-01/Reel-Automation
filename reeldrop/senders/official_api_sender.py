import os

import requests

from .base import Sender, BlockedError, redact

API_VERSION = "v21.0"   # check the current version in Meta's docs

# Meta Graph API error codes that mean "too many calls / throttled".
_RATE_LIMIT_CODES = {4, 17, 32, 613}
_MAX_BODY = 500


class RateLimitedError(BlockedError, RuntimeError):
    """The API is throttling us. Treated as a block (pause + alert) so we stop hammering it."""


class OfficialApiSender(Sender):
    name = "official"

    def __init__(self):
        self.token = os.environ["IG_ACCESS_TOKEN"]
        self.igsid = os.environ["IG_RECIPIENT_IGSID"]   # friend's IGSID from the webhook

    def _error_code(self, r):
        try:
            return int(r.json()["error"]["code"])
        except Exception:
            return None

    def send(self, recipient, reel_url):
        self._post_text(reel_url)

    def send_text(self, recipient, text):
        self._post_text(text)

    def _post_text(self, text):
        try:
            r = requests.post(
                f"https://graph.instagram.com/{API_VERSION}/me/messages",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"recipient": {"id": self.igsid}, "message": {"text": text}},
                timeout=20,
            )
        except requests.RequestException as e:
            raise RuntimeError(
                f"API request failed: {type(e).__name__}: {redact(e, self.token)}"
            ) from None
        if r.status_code == 200:
            return

        body = redact(r.text, self.token)[:_MAX_BODY]
        message = f"API error {r.status_code}: {body}"
        if r.status_code == 429 or self._error_code(r) in _RATE_LIMIT_CODES:
            raise RateLimitedError(f"{message} (rate limited, backing off)")
        if r.status_code in (401, 403) or self._error_code(r) == 190:
            message += " (check IG_ACCESS_TOKEN and messaging permission)"
        raise RuntimeError(message)
