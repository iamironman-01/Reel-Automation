import os

import requests

from .base import Sender, redact

_MAX_BODY = 300


class TelegramSender(Sender):
    name = "telegram"

    def __init__(self, caption: str = "🎬"):
        self.token = os.environ["TELEGRAM_BOT_TOKEN"]
        self.chat_id = os.environ["TELEGRAM_FRIEND_CHAT_ID"]
        self.caption = caption

    def send(self, recipient, reel_url):
        # `recipient` is ignored: the target is the chat id from TELEGRAM_FRIEND_CHAT_ID.
        self._post(f"{self.caption} {reel_url}".strip())

    def send_text(self, recipient, text):
        # Same target rules as send(); the note is posted as-is, without the caption.
        self._post(text)

    def _post(self, text):
        # The request URL contains the bot token, and requests puts the URL in its
        # exception text, so every failure is re-raised as a sanitized RuntimeError.
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=20,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            response = getattr(e, "response", None)
            if response is not None:
                detail = f"Telegram API error {response.status_code}: " \
                         f"{redact(getattr(response, 'text', ''), self.token)[:_MAX_BODY]}"
            else:
                detail = f"Telegram request failed: {type(e).__name__}: {redact(e, self.token)}"
            raise RuntimeError(detail) from None
