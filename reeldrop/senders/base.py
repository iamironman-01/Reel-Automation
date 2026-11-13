class BlockedError(Exception):
    """Instagram blocked/challenged us — stop everything."""


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
