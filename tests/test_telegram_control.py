import logging
import sys
import threading
import types

import pytest

from reeldrop import queue, settings, telegram_control as tc
from reeldrop import config as config_module

TOKEN = "123456:SECRET-TOKEN"
CHAT = "4242"
LINK = "https://www.instagram.com/reel/AAA111/"
LINK2 = "https://www.instagram.com/reel/BBB222/"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_MY_CHAT_ID", CHAT)


@pytest.fixture
def no_config(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("no config")
    monkeypatch.setattr(config_module, "load_config", boom)


def update(text, chat_id=int(CHAT), update_id=1):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


# --- fake requests -------------------------------------------------------------

class FakeResponse:
    def __init__(self, data=None, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data


class FakeRequests(types.ModuleType):
    def __init__(self):
        super().__init__("requests")
        self.gets, self.posts = [], []
        self.get_result = FakeResponse({"ok": True, "result": []})
        self.post_result = FakeResponse({"ok": True})

    def get(self, url, **kw):
        self.gets.append((url, kw))
        if isinstance(self.get_result, Exception):
            raise self.get_result
        return self.get_result

    def post(self, url, **kw):
        self.posts.append((url, kw))
        if isinstance(self.post_result, Exception):
            raise self.post_result
        return self.post_result


@pytest.fixture
def fake_requests(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setitem(sys.modules, "requests", fake)
    return fake


# --- auth ------------------------------------------------------------------------

def test_owner_chat_is_processed(temp_db):
    assert "Added" in tc.process_update(update(f"/add {LINK}"))
    assert len(queue.list_reels("pending")) == 1


def test_chat_id_compared_as_string(temp_db, monkeypatch):
    monkeypatch.setenv("TELEGRAM_MY_CHAT_ID", "-100123")
    assert tc.process_update(update("/help", chat_id=-100123)) == tc.HELP_TEXT
    assert tc.process_update(update("/help", chat_id="-100123")) == tc.HELP_TEXT


def test_wrong_chat_ignored_silently(temp_db, caplog):
    caplog.set_level(logging.WARNING)
    assert tc.process_update(update(f"/add {LINK}", chat_id=999)) is None
    assert tc.process_update(update("/pause", chat_id=999)) is None
    assert queue.list_reels() == []
    assert not settings.is_paused()
    assert "unauthorised" in caplog.text
    assert LINK not in caplog.text and "/pause" not in caplog.text


@pytest.mark.parametrize("bad", [None, {}, {"message": None}, {"message": {}},
                                 {"message": {"chat": {}, "text": "/help"}},
                                 {"message": {"chat": {"id": int(CHAT)}}},        # no text
                                 {"message": {"chat": {"id": int(CHAT)}, "text": 5}}])
def test_malformed_updates_return_none(temp_db, bad):
    assert tc.process_update(bad) is None


@pytest.mark.parametrize("missing", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_MY_CHAT_ID"])
def test_missing_env_refuses(monkeypatch, temp_db, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(tc.TelegramControlError, match=missing):
        tc.run_forever()
    with pytest.raises(tc.TelegramControlError):
        tc.poll_once(None)
    assert tc.process_update(update("/help")) is None     # fails closed
    assert tc.start_in_thread() is None


# --- commands --------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["/start", "/help", "/HELP", "/help@MyBot"])
def test_help(cmd):
    assert tc.handle_command(cmd) == tc.HELP_TEXT


def test_unknown_command_and_plain_text():
    assert "Unknown command" in tc.handle_command("/frobnicate")
    assert "/help" in tc.handle_command("hello there")
    assert tc.handle_command("   ") == tc.HELP_TEXT


def test_add_with_note(temp_db):
    reply = tc.handle_command(f"/add {LINK} watch this one")
    assert reply == f"Added: {LINK}"
    (row,) = queue.list_reels("pending")
    assert row["note"] == "watch this one" and row["url"] == LINK


def test_add_normalises_url(temp_db):
    tc.handle_command("/add instagram.com/reel/XyZ_9/?igsh=abc")
    assert queue.list_reels("pending")[0]["url"] == "https://www.instagram.com/reel/XyZ_9/"


def test_implicit_add(temp_db):
    assert tc.handle_command(LINK).startswith("Added")
    assert tc.handle_command(f"look {LINK2}").startswith("Added")
    assert [r["url"] for r in queue.list_reels("pending")] == [LINK, LINK2]


def test_multi_link(temp_db):
    reply = tc.handle_command(f"{LINK} {LINK2}")
    assert reply.splitlines()[0] == "Added 2 of 2 links."
    assert len(queue.list_reels("pending")) == 2


def test_multi_link_with_duplicate(temp_db):
    queue.add_reel(LINK)
    reply = tc.handle_command(f"/add {LINK}\n{LINK2}")
    assert "Added 1 of 2" in reply
    assert "Skipped (This reel is already in the queue)" in reply
    assert len(queue.list_reels("pending")) == 2


def test_add_duplicate_reply(temp_db):
    tc.handle_command(f"/add {LINK}")
    assert "already in the queue" in tc.handle_command(f"/add {LINK}")
    assert len(queue.list_reels()) == 1


def test_add_invalid_and_usage(temp_db):
    assert "doesn't look like" in tc.handle_command("/add https://example.com/x")
    assert "doesn't look like" in tc.handle_command("/add https://evil.com/?u=instagram.com/reel/AAA/")
    assert tc.handle_command("/add").startswith("Usage")
    assert queue.list_reels() == []


def test_too_many_links(temp_db):
    links = " ".join(f"https://www.instagram.com/reel/C{i}/" for i in range(tc.MAX_LINKS_PER_MESSAGE + 3))
    reply = tc.handle_command(links)
    assert "Ignored 3 extra" in reply
    assert len(queue.list_reels()) == tc.MAX_LINKS_PER_MESSAGE


def test_long_input_rejected(temp_db):
    reply = tc.handle_command("/add " + LINK + " " + "x" * tc.MAX_INPUT_CHARS)
    assert "too long" in reply
    assert queue.list_reels() == []


def test_long_note_truncated(temp_db):
    tc.handle_command(f"/add {LINK} " + "word " * 200)
    assert len(queue.list_reels()[0]["note"]) <= tc.MAX_NOTE_CHARS


def test_pause_resume_toggle(temp_db):
    assert "Paused" in tc.handle_command("/pause")
    assert settings.is_paused()
    assert "Resumed" in tc.handle_command("/resume")
    assert not settings.is_paused()


def test_status_without_config(temp_db, no_config):
    queue.add_reel(LINK)
    rid = queue.add_reel(LINK2)
    queue.mark_failed(rid, "x", "r", "dry_run", "boom")
    reply = tc.handle_command("/status")
    assert reply == "State: running\nSent today: 0\nPending: 1\nFailed: 1"


def test_status_with_cap_and_paused(temp_db, monkeypatch):
    monkeypatch.setattr(config_module, "load_config", lambda *a, **k: config_module.Config(daily_cap=7))
    rid = queue.add_reel(LINK)
    queue.mark_sent(rid, "friend", "r", "dry_run")
    queue.add_reel(LINK2)
    tc.handle_command("/pause")
    reply = tc.handle_command("/status")
    assert "State: PAUSED" in reply
    assert "Sent today: 1/7" in reply
    assert "Pending: 1" in reply and "Failed: 0" in reply


def test_queue_command(temp_db):
    assert "empty" in tc.handle_command("/queue")
    for i in range(7):
        queue.add_reel(f"https://www.instagram.com/reel/Q{i}/")
    lines = tc.handle_command("/queue").splitlines()
    assert lines[0] == "Pending: 7. Next 5:"
    assert len(lines) == 6 and lines[1].endswith("/reel/Q0/") and lines[5].endswith("/reel/Q4/")


def test_db_failure_gives_generic_reply(temp_db, monkeypatch):
    def boom():
        raise RuntimeError(f"disk exploded {TOKEN}")
    monkeypatch.setattr(settings, "is_paused", boom)
    reply = tc.handle_command("/status")
    assert "went wrong" in reply and "disk exploded" not in reply


# --- poll_once -------------------------------------------------------------------

def test_poll_once_advances_offset_and_replies(temp_db, fake_requests):
    fake_requests.get_result = FakeResponse({"ok": True, "result": [
        update(f"/add {LINK}", update_id=10),
        update("/pause", chat_id=999, update_id=11),
        update("/status", update_id=12),
    ]})
    new = tc.poll_once(5, timeout=3)
    assert new == 13
    url, kw = fake_requests.gets[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    assert kw["params"] == {"offset": 5, "timeout": 3, "allowed_updates": ["message"]}
    assert kw["timeout"] == 13
    assert len(fake_requests.posts) == 2          # none for the stranger
    for url, kw in fake_requests.posts:
        assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        assert kw["json"]["chat_id"] == CHAT
    assert "Added" in fake_requests.posts[0][1]["json"]["text"]
    assert not settings.is_paused()


def test_poll_once_first_call_has_no_offset(temp_db, fake_requests):
    assert tc.poll_once(None) is None
    assert "offset" not in fake_requests.gets[0][1]["params"]


def test_poll_once_error_does_not_raise(temp_db, fake_requests, caplog):
    caplog.set_level(logging.DEBUG)
    fake_requests.get_result = ConnectionError(f"HTTPSConnectionPool: /bot{TOKEN}/getUpdates failed")
    assert tc.poll_once(7) == 7
    assert "ConnectionError" in caplog.text
    fake_requests.get_result = FakeResponse(status=502)
    assert tc.poll_once(7) == 7
    fake_requests.get_result = FakeResponse({"ok": False})
    assert tc.poll_once(7) == 7
    assert TOKEN not in caplog.text


def test_poll_once_raise_on_error_is_token_free(temp_db, fake_requests):
    fake_requests.get_result = ConnectionError(f"failed for /bot{TOKEN}/getUpdates")
    with pytest.raises(tc.TelegramControlError) as exc:
        tc.poll_once(None, raise_on_error=True)
    assert TOKEN not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_send_failure_does_not_raise_or_leak(temp_db, fake_requests, caplog):
    caplog.set_level(logging.DEBUG)
    fake_requests.get_result = FakeResponse({"ok": True, "result": [update("/help", update_id=3)]})
    fake_requests.post_result = ConnectionError(f"boom at /bot{TOKEN}/sendMessage")
    assert tc.poll_once(None) == 4                # offset still advances
    assert "Could not send Telegram reply" in caplog.text
    fake_requests.post_result = FakeResponse(status=400)
    fake_requests.get_result = FakeResponse({"ok": True, "result": [update("/help", update_id=4)]})
    assert tc.poll_once(4) == 5
    assert TOKEN not in caplog.text


def test_bad_update_does_not_block_others(temp_db, fake_requests, monkeypatch):
    real = tc.process_update

    def flaky(u):
        if u["update_id"] == 1:
            raise RuntimeError(f"bad {TOKEN}")
        return real(u)

    monkeypatch.setattr(tc, "process_update", flaky)
    fake_requests.get_result = FakeResponse({"ok": True, "result": [
        update("/help", update_id=1), update("/help", update_id=2)]})
    assert tc.poll_once(None) == 3
    assert len(fake_requests.posts) == 1


# --- run_forever / thread ----------------------------------------------------------

def test_run_forever_stops_on_event(temp_db, monkeypatch):
    stop = threading.Event()
    calls = []

    def fake_poll(offset, timeout=25, *, raise_on_error=False):
        calls.append(offset)
        if len(calls) == 3:
            stop.set()
        return (offset or 0) + 1

    monkeypatch.setattr(tc, "poll_once", fake_poll)
    tc.run_forever(stop)
    assert calls == [None, 1, 2]


def test_run_forever_backoff_and_reset(temp_db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    stop = threading.Event()
    sleeps, results = [], []
    outcomes = iter([tc.TelegramControlError("x")] * 8 + ["ok"] + [tc.TelegramControlError("x")] + ["stop"])

    def fake_poll(offset, timeout=25, *, raise_on_error=False):
        assert raise_on_error
        r = next(outcomes)
        if isinstance(r, Exception):
            raise r
        if r == "stop":
            stop.set()
        return 1

    monkeypatch.setattr(tc, "poll_once", fake_poll)
    monkeypatch.setattr(tc, "_sleep", lambda s, ev=None: sleeps.append(s))
    tc.run_forever(stop)
    assert sleeps == [1, 2, 4, 8, 16, 32, 60, 60, 1]       # doubles, capped at 60, resets on success


def test_run_forever_survives_unexpected_error(temp_db, monkeypatch):
    stop = threading.Event()
    sleeps, n = [], []

    def fake_poll(offset, timeout=25, *, raise_on_error=False):
        n.append(1)
        if len(n) == 1:
            raise ValueError("weird")
        stop.set()
        return offset

    monkeypatch.setattr(tc, "poll_once", fake_poll)
    monkeypatch.setattr(tc, "_sleep", lambda s, ev=None: sleeps.append(s))
    tc.run_forever(stop)
    assert sleeps == [1] and len(n) == 2


def test_run_forever_end_to_end_with_fake_requests(temp_db, fake_requests, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    stop = threading.Event()
    fake_requests.get_result = ConnectionError(f"down: /bot{TOKEN}/getUpdates")
    monkeypatch.setattr(tc, "_sleep", lambda s, ev=None: stop.set() if len(fake_requests.gets) >= 2 else None)
    tc.run_forever(stop)
    assert len(fake_requests.gets) == 2
    assert TOKEN not in caplog.text


def test_start_in_thread(temp_db, monkeypatch):
    stop = threading.Event()
    stop.set()                                   # run_forever returns immediately
    t = tc.start_in_thread(stop)
    assert t is not None and t.daemon
    t.join(2)
    assert not t.is_alive()


def test_start_in_thread_unconfigured_logs(monkeypatch, caplog):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    caplog.set_level(logging.INFO)
    assert tc.start_in_thread() is None
    assert "Telegram control disabled" in caplog.text
