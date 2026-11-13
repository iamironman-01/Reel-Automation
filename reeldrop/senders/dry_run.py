from .base import Sender


class DryRunSender(Sender):
    name = "dry_run"

    def send(self, recipient, reel_url):
        print(f"[DRY RUN] would send {reel_url} to {recipient}")

    def send_text(self, recipient, text):
        print(f"[DRY RUN] would send note {text!r} to {recipient}")
