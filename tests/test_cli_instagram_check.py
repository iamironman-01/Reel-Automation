import sys
import types

import pytest

from reeldrop import settings
from reeldrop.__main__ import main, parse_reel_url
from reeldrop.senders.base import BlockedError

REEL = "https://www.instagram.com/reel/ABC123/"


@pytest.fixture(autouse=True)
def _db(temp_db):
    return temp_db


@pytest.fixture
def fake(monkeypatch):
    """Install a fake instagrapi_sender module; return a state object."""
    state = types.SimpleNamespace(
        constructed=0, texts=[], reels=[], resolved=[],
        method="native", init_error=None, send_error=None,
    )

    class FakeSender:
        username = "me_test"

        def __init__(self):
            state.constructed += 1
            if state.init_error:
                raise state.init_error
            self.last_method = None

        def resolve_user(self, recipient):
            state.resolved.append(recipient)
            return 4242

        def send(self, recipient, reel_url):
            if state.send_error:
                raise state.send_error
            state.reels.append((recipient, reel_url))
            self.last_method = state.method

        def send_text(self, recipient, text):
            state.texts.append((recipient, text))

    module = types.ModuleType("reeldrop.senders.instagrapi_sender")
    module.InstagrapiSender = FakeSender
    monkeypatch.setitem(sys.modules, "reeldrop.senders.instagrapi_sender", module)
    return state


def test_login_only_sends_nothing(fake, capsys):
    assert main(["instagram-check"]) == 0
    out = capsys.readouterr().out
    assert "Logged in as @me_test" in out
    assert fake.texts == [] and fake.reels == [] and fake.resolved == []


def test_to_only_resolves_and_sends_nothing(fake, capsys):
    assert main(["instagram-check", "--to", "@friend"]) == 0
    out = capsys.readouterr().out
    assert "Found @friend (id 4242)" in out
    assert "Message requests" in out
    assert fake.resolved == ["friend"]
    assert fake.texts == [] and fake.reels == []


def test_send_test_text_sends_exactly_one_text(fake, capsys):
    assert main(["instagram-check", "--to", "friend", "--send-test-text"]) == 0
    assert fake.texts == [("friend", "ReelDrop test message")]
    assert fake.reels == []
    assert "Test message sent to @friend" in capsys.readouterr().out


def test_reel_native(fake, capsys):
    assert main(["instagram-check", "--to", "friend", "--reel", REEL]) == 0
    assert fake.reels == [("friend", REEL)]
    assert fake.texts == []
    assert "native Instagram reel share" in capsys.readouterr().out


def test_reel_link_fallback(fake, capsys):
    fake.method = "link"
    assert main(["instagram-check", "--to", "friend", "--reel", REEL]) == 0
    out = capsys.readouterr().out
    assert "as a LINK (native share failed; check the log)" in out
    assert "native Instagram reel share" not in out


def test_reel_url_is_cleaned_before_sending(fake):
    url = "instagram.com/someone/reels/XYZ_9-a/?igsh=abc"
    assert main(["instagram-check", "--to", "friend", "--reel", url]) == 0
    assert fake.reels == [("friend", "https://www.instagram.com/reel/XYZ_9-a/")]


@pytest.mark.parametrize("flag", [["--send-test-text"], ["--reel", REEL]])
def test_send_flags_require_to(fake, flag):
    with pytest.raises(SystemExit) as exc:
        main(["instagram-check", *flag])
    assert exc.value.code == 2
    assert fake.constructed == 0


def test_invalid_reel_rejected_before_login(fake, capsys):
    assert main(["instagram-check", "--to", "friend", "--reel", "https://example.com/x"]) == 1
    assert "Error: Not an Instagram reel/post link" in capsys.readouterr().out
    assert fake.constructed == 0


def test_blocked_login_pauses_and_exits_1(fake, capsys):
    fake.init_error = BlockedError("challenge_required")
    assert not settings.is_paused()
    assert main(["instagram-check"]) == 1
    assert settings.is_paused()
    out = capsys.readouterr().out
    assert "Sending is now paused" in out and "challenge_required" in out


def test_blocked_send_pauses_and_exits_1(fake, capsys):
    fake.send_error = BlockedError("feedback_required")
    assert main(["instagram-check", "--to", "friend", "--reel", REEL]) == 1
    assert settings.is_paused()
    assert "paused" in capsys.readouterr().out.lower()


def test_runtime_error_exits_1_and_prints_message(fake, capsys):
    fake.init_error = RuntimeError("IG_USERNAME and IG_PASSWORD must be set")
    assert main(["instagram-check"]) == 1
    assert "Error: IG_USERNAME and IG_PASSWORD must be set" in capsys.readouterr().out
    assert not settings.is_paused()


def test_parse_reel_url():
    assert parse_reel_url(" https://instagram.com/p/AbC/?x=1 ") == "https://www.instagram.com/reel/AbC/"
    with pytest.raises(ValueError):
        parse_reel_url("https://example.com/reel/abc")
