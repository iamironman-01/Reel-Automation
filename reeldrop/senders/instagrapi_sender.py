"""Instagram sender built on instagrapi: delivers reels as real Instagram DMs.

send() shares the reel natively (direct_media_share with media_type="video"; the library
defaults to "photo", which is wrong for reels), so the friend gets the actual reel. If the
native share raises a normal error or Instagram returns an empty message, send() fails and
the reel is NOT sent as a link. Set IG_LINK_FALLBACK=1 to send the link as text instead in
that case. Block and challenge errors are never retried: they become BlockedError so the
job pauses.

instagrapi is imported inside __init__ so this module can be imported (and tested with a
fake) without the library installed. Error classes are matched by name, see base.py.
"""
import logging
import os
from pathlib import Path

from .base import BlockedError, Sender, has_error_name, is_block_error, redact

log = logging.getLogger("reeldrop")

SESSION = Path("data/session.json")

# [min, max] seconds of random delay instagrapi adds between private requests.
DELAY_RANGE = [2, 5]

_TWO_FACTOR = frozenset({"TwoFactorRequired"})
_BAD_LOGIN = frozenset({"BadPassword", "BadCredentials"})
_NOT_FOUND = frozenset({"UserNotFound", "ClientNotFoundError"})
_NEEDS_UPGRADE = frozenset({"ClientNeedsUpgradeError"})

# Opt-in: send the reel link as text when the native share fails. Off by default, so a
# reel is either shared as a real reel or not sent at all.
LINK_FALLBACK_ENV = "IG_LINK_FALLBACK"

TWO_FACTOR_HELP = ("Instagram asked for a two-factor code. Set IG_TOTP_SECRET (recommended) "
                   "or IG_VERIFICATION_CODE in .env and restart.")


# instagrapi tries Instagram's current login flow first. When Instagram answers that flow
# with a verification step the library cannot complete, it falls back to an old endpoint,
# which only ever answers "your version of Instagram is out of date". That text is
# misleading, so explain what is really needed.
VERIFICATION_HELP = (
    "Instagram did not complete the login (it answered 'app out of date', which usually "
    "means it wants a verification step for this account). Log in to this account once in "
    "the Instagram app and approve the login, turn on two-factor authentication with an "
    "authenticator app and set IG_TOTP_SECRET in .env, then try again. Do not retry "
    "repeatedly: failed logins can get the account flagged.")


def link_fallback_enabled() -> bool:
    """True if IG_LINK_FALLBACK asks for the link to be sent when the native share fails."""
    return os.environ.get(LINK_FALLBACK_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise RuntimeError(f"Missing environment variable {name} (needed to log in to Instagram)")
    return value


def _safe(text) -> str:
    """Text for logs and exception messages: never contains the Instagram password."""
    return redact(text, os.environ.get("IG_PASSWORD"))


def _blocked(exc: BaseException) -> BlockedError:
    return BlockedError(f"{type(exc).__name__}: {_safe(exc)}")


def _delivered(message) -> bool:
    """True if instagrapi returned a message that Instagram accepted.

    instagrapi can return a DirectMessage with empty id and thread_id when Instagram
    silently did not accept the message.
    """
    if not message:
        return False
    return bool(getattr(message, "id", None) or getattr(message, "thread_id", None))


class InstagrapiSender(Sender):
    name = "instagrapi"

    def __init__(self):
        username = _require_env("IG_USERNAME")
        password = _require_env("IG_PASSWORD")

        try:
            from instagrapi import Client        # lazy: library is optional
        except ImportError:
            raise RuntimeError("The instagrapi package is not installed. "
                               "Run: pip install -r requirements.txt") from None

        self.username = username
        self.last_method = None                  # "native" or "link" after a send()
        self._user_ids = {}
        self.cl = Client()
        self.cl.delay_range = list(DELAY_RANGE)
        self._session_loaded = False
        if SESSION.exists():
            try:
                self.cl.load_settings(SESSION)   # reuse device + cookies
                self._session_loaded = True
            except Exception as e:
                log.warning("Ignoring saved Instagram session (%s: %s); logging in fresh",
                            type(e).__name__, _safe(e))
        self._login(username, password)
        SESSION.parent.mkdir(parents=True, exist_ok=True)
        self.cl.dump_settings(SESSION)           # save for next start

    # --- login -----------------------------------------------------------------

    def _verification_code(self) -> str:
        secret = os.environ.get("IG_TOTP_SECRET", "").replace(" ", "").strip()
        if secret:
            return str(self.cl.totp_generate_code(secret))
        return os.environ.get("IG_VERIFICATION_CODE", "").strip()

    def _login_once(self, username, password, code):
        if code:
            self.cl.login(username, password, verification_code=code)
        else:
            self.cl.login(username, password)

    def _login(self, username, password):
        code = self._verification_code()
        try:
            try:
                self._login_once(username, password, code)
            except Exception as e:
                # A session saved by an older instagrapi has no bloks_versioning_id and the
                # library refuses to log in. Its own error says to reload the saved settings
                # with override_app_version=True. Try that exactly once, for that error only.
                if not (self._session_loaded and "bloks_versioning_id" in str(e)):
                    raise
                log.warning("Saved Instagram session is from an older app version; reloading it")
                self.cl.load_settings(SESSION, override_app_version=True)
                self._login_once(username, password, code)
        except Exception as e:
            if has_error_name(e, _TWO_FACTOR):
                if code:
                    raise RuntimeError(
                        "Instagram rejected the two-factor code. Check IG_TOTP_SECRET "
                        "(or use a fresh IG_VERIFICATION_CODE) and restart.") from None
                raise RuntimeError(TWO_FACTOR_HELP) from None
            if has_error_name(e, _BAD_LOGIN):
                raise RuntimeError("Instagram rejected the username or password.") from None
            if has_error_name(e, _NEEDS_UPGRADE):
                raise RuntimeError(VERIFICATION_HELP) from None
            if is_block_error(e):
                raise _blocked(e) from e
            raise

    # --- helpers ---------------------------------------------------------------

    def resolve_user(self, recipient) -> int:
        """Return the numeric Instagram user id for `recipient` (username, @username or id)."""
        name = str(recipient).strip().lstrip("@")
        if not name:
            raise RuntimeError("Instagram recipient is empty")
        if name.isdigit():
            return int(name)                     # already a user id: no lookup
        if name not in self._user_ids:           # look up once, then cache
            try:
                self._user_ids[name] = int(self.cl.user_id_from_username(name))
            except BlockedError:
                raise
            except Exception as e:
                if is_block_error(e):
                    raise _blocked(e) from e
                if has_error_name(e, _NOT_FOUND):
                    raise RuntimeError(f"Instagram user '{name}' was not found") from None
                raise RuntimeError(f"Could not look up Instagram user '{name}': "
                                   f"{type(e).__name__}: {_safe(e)}") from None
        return self._user_ids[name]

    # --- sending ---------------------------------------------------------------

    def send(self, recipient, reel_url) -> None:
        self.last_method = None
        try:
            uid = self.resolve_user(recipient)
            media_pk = self.cl.media_pk_from_url(reel_url)
            try:
                media_id = self.cl.media_id(media_pk)
                message = self.cl.direct_media_share(media_id, [uid], media_type="video")
                if _delivered(message):
                    self.last_method = "native"
                    return
                reason = "Instagram returned an empty message"
            except Exception as e:
                if is_block_error(e):
                    raise
                reason = f"{type(e).__name__}: {_safe(e)}"
            if not link_fallback_enabled():
                raise RuntimeError(
                    f"Native reel share failed ({reason}). The reel was not sent. "
                    f"Set {LINK_FALLBACK_ENV}=1 to send the link instead when this happens.")
            log.warning("Native reel share failed (%s); sending link instead", reason)
            message = self.cl.direct_send(reel_url, [uid])
            if not _delivered(message):
                raise RuntimeError("Instagram did not confirm the message was sent")
            self.last_method = "link"
        except BlockedError:
            raise
        except Exception as e:
            if is_block_error(e):
                raise _blocked(e) from e
            raise

    def send_text(self, recipient, text) -> None:
        try:
            uid = self.resolve_user(recipient)
            if not _delivered(self.cl.direct_send(text, [uid])):
                raise RuntimeError("Instagram did not confirm the message was sent")
        except BlockedError:
            raise
        except Exception as e:
            if is_block_error(e):
                raise _blocked(e) from e
            raise
