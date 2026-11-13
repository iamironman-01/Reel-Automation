import logging
import os
from pathlib import Path

from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired, FeedbackRequired, LoginRequired, PleaseWaitFewMinutes,
)

from .base import Sender, BlockedError

log = logging.getLogger("reeldrop")

SESSION = Path("data/session.json")

# Errors that mean Instagram is challenging or throttling us: never swallow these.
BLOCK_ERRORS = (ChallengeRequired, FeedbackRequired, LoginRequired, PleaseWaitFewMinutes)


class InstagrapiSender(Sender):
    name = "instagrapi"

    def __init__(self):
        username = os.environ["IG_USERNAME"]
        password = os.environ["IG_PASSWORD"]
        self.cl = Client()
        if SESSION.exists():
            self.cl.load_settings(SESSION)          # reuse device + cookies
        self.cl.login(username, password)
        SESSION.parent.mkdir(parents=True, exist_ok=True)
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
            except BLOCK_ERRORS:
                raise                                          # handled by the outer except
            except Exception as e:
                log.warning("Native share failed (%s: %s), sending link instead",
                            type(e).__name__, e)
                self.cl.direct_send(reel_url, [int(uid)])     # fallback: send link
        except BLOCK_ERRORS as e:
            raise BlockedError(f"{type(e).__name__}: {e}") from e

    def send_text(self, recipient, text):
        try:
            uid = self._uid(recipient)
            self.cl.direct_send(text, [int(uid)])
        except BLOCK_ERRORS as e:
            raise BlockedError(f"{type(e).__name__}: {e}") from e
