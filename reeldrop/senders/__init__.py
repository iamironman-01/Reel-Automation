from .base import Sender, BlockedError

__all__ = ["Sender", "BlockedError", "get_sender"]


def get_sender(name: str) -> Sender:
    if name == "dry_run":
        from .dry_run import DryRunSender; return DryRunSender()
    if name == "instagrapi":
        from .instagrapi_sender import InstagrapiSender; return InstagrapiSender()
    if name == "official":
        from .official_api_sender import OfficialApiSender; return OfficialApiSender()
    if name == "telegram":
        from .telegram_sender import TelegramSender; return TelegramSender()
    raise ValueError(f"Unknown sender: {name}")
