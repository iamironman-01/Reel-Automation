class BlockedError(Exception):
    """Instagram blocked/challenged us — stop everything."""


# instagrapi exception class names that mean "Instagram is challenging/throttling us".
# Matched by name anywhere in the MRO, so no instagrapi import is needed (fakes work too).
# Deliberately excluded: BadPassword, BadCredentials, TwoFactorRequired, UserNotFound and
# ClientBadRequestError. Those are configuration problems, not blocks.
BLOCK_ERROR_NAMES: frozenset = frozenset({
    "ChallengeRequired", "ChallengeError", "ChallengeRedirection",
    "ChallengeSelfieCaptcha", "ChallengeUnknownStep", "SelectContactPointRecoveryForm",
    "RecaptchaChallengeForm", "SubmitPhoneNumberForm", "LegacyForceSetNewPasswordForm",
    "FeedbackRequired", "SignupSpamError", "LoginRequired", "ClientLoginRequired",
    "PleaseWaitFewMinutes", "RateLimitError", "SentryBlock", "AccountSuspended",
    "CaptchaChallengeRequired", "ReloginAttemptExceeded",
    # HTTP 429 from Instagram, and "Instagram has blocked your IP address". Never retry.
    "ClientThrottledError", "ProxyAddressIsBlocked",
})


def has_error_name(exc: BaseException, names) -> bool:
    """True if the class of exc, or any base class, has a name in `names`."""
    return any(c.__name__ in names for c in type(exc).__mro__)


def is_block_error(exc: BaseException) -> bool:
    """True if exc is an instagrapi block/challenge/throttle error (checked by class name)."""
    return has_error_name(exc, BLOCK_ERROR_NAMES)


class Sender:
    name = "base"

    def send(self, recipient: str, reel_url: str) -> None:
        """Deliver one reel. Raise BlockedError or another exception on failure."""
        raise NotImplementedError

    def send_text(self, recipient: str, text: str) -> None:
        """Deliver a plain text message (used for reel notes). Same error rules as send()."""
        raise NotImplementedError


def redact(text, *secrets) -> str:
    """Return str(text) with every non-empty secret replaced by '***'."""
    out = str(text)
    for secret in secrets:
        if secret:
            out = out.replace(str(secret), "***")
    return out
