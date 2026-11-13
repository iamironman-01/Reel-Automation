import importlib
import sys
import types

import pytest

from reeldrop.senders import BlockedError, get_sender
from reeldrop.senders.base import Sender

TOKEN = "123456:SECRET-TOKEN"


@pytest.fixture
def load_fresh(monkeypatch):
    """Import a sender module with fake third-party modules in sys.modules.

    The module is removed again afterwards so later tests never see the fakes.
    """
    loaded = []

    def loader(modname, fakes):
        for name, mod in fakes.items():
            monkeypatch.setitem(sys.modules, name, mod)
        full = f"reeldrop.senders.{modname}"
        sys.modules.pop(full, None)
        module = importlib.import_module(full)
        loaded.append(modname)
        return module

    yield loader
    for modname in loaded:
        sys.modules.pop(f"reeldrop.senders.{modname}", None)
        if hasattr(sys.modules["reeldrop.senders"], modname):
            delattr(sys.modules["reeldrop.senders"], modname)


# --- registry / dry run ------------------------------------------------------

def test_get_sender_unknown():
    with pytest.raises(ValueError, match="Unknown sender"):
        get_sender("carrier_pigeon")


def test_get_sender_dry_run():
    s = get_sender("dry_run")
    assert s.name == "dry_run" and isinstance(s, Sender)


def test_base_send_not_implemented():
    with pytest.raises(NotImplementedError):
        Sender().send("a", "b")


def test_dry_run_prints(capsys):
    get_sender("dry_run").send("friend", "https://www.instagram.com/reel/AAA/")
    assert capsys.readouterr().out == \
        "[DRY RUN] would send https://www.instagram.com/reel/AAA/ to friend\n"


# --- fake requests -----------------------------------------------------------

class FakeRequestException(Exception):
    def __init__(self, msg="", response=None):
        super().__init__(msg)
        self.response = response


class FakeResponse:
    def __init__(self, status=200, text="ok", json_data=None):
        self.status_code = status
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            # like real requests: the message contains the full URL
            raise FakeRequestException(
                f"{self.status_code} Client Error for url: "
                f"https://api.telegram.org/bot{TOKEN}/sendMessage", response=self)


def fake_requests(result):
    mod = types.ModuleType("requests")
    mod.RequestException = FakeRequestException
    mod.calls = []

    def post(url, **kwargs):
        mod.calls.append((url, kwargs))
        if isinstance(result, Exception):
            raise result
        return result

    mod.post = post
    return mod


# --- telegram ----------------------------------------------------------------

@pytest.fixture
def tg_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_FRIEND_CHAT_ID", "4242")


def test_telegram_success(load_fresh, tg_env):
    req = fake_requests(FakeResponse(200))
    mod = load_fresh("telegram_sender", {"requests": req})
    s = mod.TelegramSender()
    assert s.name == "telegram"
    s.send("ignored_recipient", "https://www.instagram.com/reel/AAA/")
    url, kw = req.calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert kw["json"] == {"chat_id": "4242", "text": "🎬 https://www.instagram.com/reel/AAA/"}
    assert kw["timeout"] == 20


def test_telegram_custom_caption(load_fresh, tg_env):
    req = fake_requests(FakeResponse(200))
    mod = load_fresh("telegram_sender", {"requests": req})
    mod.TelegramSender(caption="Watch this:").send("x", "U")
    assert req.calls[0][1]["json"]["text"] == "Watch this: U"


def test_telegram_requires_env(load_fresh, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_FRIEND_CHAT_ID", raising=False)
    mod = load_fresh("telegram_sender", {"requests": fake_requests(FakeResponse())})
    with pytest.raises(KeyError):
        mod.TelegramSender()


def test_telegram_http_error_does_not_leak_token(load_fresh, tg_env):
    resp = FakeResponse(403, text='{"ok":false,"description":"Forbidden: bot was blocked"}')
    mod = load_fresh("telegram_sender", {"requests": fake_requests(resp)})
    with pytest.raises(RuntimeError) as exc:
        mod.TelegramSender().send("x", "U")
    assert "403" in str(exc.value) and "bot was blocked" in str(exc.value)
    assert TOKEN not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_telegram_network_error_does_not_leak_token(load_fresh, tg_env):
    err = FakeRequestException(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage")
    mod = load_fresh("telegram_sender", {"requests": fake_requests(err)})
    with pytest.raises(RuntimeError) as exc:
        mod.TelegramSender().send("x", "U")
    assert TOKEN not in str(exc.value)
    assert "FakeRequestException" in str(exc.value)


# --- official API ------------------------------------------------------------

@pytest.fixture
def api_env(monkeypatch):
    monkeypatch.setenv("IG_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("IG_RECIPIENT_IGSID", "999")


def test_official_success(load_fresh, api_env):
    req = fake_requests(FakeResponse(200))
    mod = load_fresh("official_api_sender", {"requests": req})
    s = mod.OfficialApiSender()
    assert s.name == "official"
    s.send("ignored", "https://www.instagram.com/reel/AAA/")
    url, kw = req.calls[0]
    assert url == f"https://graph.instagram.com/{mod.API_VERSION}/me/messages"
    assert kw["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert kw["json"] == {"recipient": {"id": "999"},
                          "message": {"text": "https://www.instagram.com/reel/AAA/"}}


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_official_http_errors_become_runtime_error(load_fresh, api_env, status):
    body = f'{{"error":{{"message":"bad","code":100}},"echo":"{TOKEN}"}}'
    mod = load_fresh("official_api_sender", {"requests": fake_requests(FakeResponse(status, body))})
    with pytest.raises(RuntimeError) as exc:
        mod.OfficialApiSender().send("x", "U")
    msg = str(exc.value)
    assert str(status) in msg and "bad" in msg
    assert TOKEN not in msg
    assert not isinstance(exc.value, BlockedError)


def test_official_permission_error_has_hint(load_fresh, api_env):
    mod = load_fresh("official_api_sender", {"requests": fake_requests(FakeResponse(403, "denied"))})
    with pytest.raises(RuntimeError, match="IG_ACCESS_TOKEN"):
        mod.OfficialApiSender().send("x", "U")


@pytest.mark.parametrize("resp", [
    FakeResponse(429, "slow down"),
    FakeResponse(400, "{}", json_data={"error": {"code": 613, "message": "calls"}}),
])
def test_official_rate_limit_is_a_block(load_fresh, api_env, resp):
    mod = load_fresh("official_api_sender", {"requests": fake_requests(resp)})
    with pytest.raises(BlockedError) as exc:
        mod.OfficialApiSender().send("x", "U")
    assert isinstance(exc.value, RuntimeError)
    assert TOKEN not in str(exc.value)


def test_official_network_error_sanitized(load_fresh, api_env):
    err = FakeRequestException(f"connection reset; auth was Bearer {TOKEN}")
    mod = load_fresh("official_api_sender", {"requests": fake_requests(err)})
    with pytest.raises(RuntimeError) as exc:
        mod.OfficialApiSender().send("x", "U")
    assert TOKEN not in str(exc.value)


# --- instagrapi --------------------------------------------------------------

def make_instagrapi_fakes():
    exc_mod = types.ModuleType("instagrapi.exceptions")
    for n in ("ChallengeRequired", "FeedbackRequired", "LoginRequired", "PleaseWaitFewMinutes"):
        setattr(exc_mod, n, type(n, (Exception,), {}))

    class Client:
        instances = []

        def __init__(self):
            self.calls = []
            self.share_error = None
            self.send_error = None
            self.lookup_error = None
            Client.instances.append(self)

        def load_settings(self, path):
            self.calls.append(("load_settings", str(path)))

        def login(self, user, password):
            self.calls.append(("login", user, password))

        def dump_settings(self, path):
            self.calls.append(("dump_settings", str(path)))

        def user_id_from_username(self, name):
            self.calls.append(("user_id_from_username", name))
            if self.lookup_error:
                raise self.lookup_error
            return "555"

        def media_pk_from_url(self, url):
            return "pk1"

        def media_id(self, pk):
            return "pk1_owner"

        def direct_media_share(self, media_id, user_ids):
            self.calls.append(("direct_media_share", media_id, user_ids))
            if self.share_error:
                raise self.share_error

        def direct_send(self, text, user_ids):
            self.calls.append(("direct_send", text, user_ids))
            if self.send_error:
                raise self.send_error

    pkg = types.ModuleType("instagrapi")
    pkg.__path__ = []
    pkg.Client = Client
    pkg.exceptions = exc_mod
    return {"instagrapi": pkg, "instagrapi.exceptions": exc_mod}, Client, exc_mod


@pytest.fixture
def ig(load_fresh, monkeypatch, tmp_path):
    monkeypatch.setenv("IG_USERNAME", "me")
    monkeypatch.setenv("IG_PASSWORD", "hunter2-password")
    fakes, Client, exc = make_instagrapi_fakes()
    mod = load_fresh("instagrapi_sender", fakes)
    monkeypatch.setattr(mod, "SESSION", tmp_path / "data" / "session.json")
    sender = mod.InstagrapiSender()
    return types.SimpleNamespace(sender=sender, cl=sender.cl, exc=exc, mod=mod, Client=Client)


URL = "https://www.instagram.com/reel/AAA/"


def test_instagrapi_login_and_session_saved(ig):
    assert ig.sender.name == "instagrapi"
    kinds = [c[0] for c in ig.cl.calls]
    assert kinds == ["login", "dump_settings"]       # no saved session yet
    assert ig.cl.calls[0] == ("login", "me", "hunter2-password")


def test_instagrapi_reuses_saved_session(load_fresh, monkeypatch, tmp_path):
    monkeypatch.setenv("IG_USERNAME", "me")
    monkeypatch.setenv("IG_PASSWORD", "pw")
    fakes, _, _ = make_instagrapi_fakes()
    mod = load_fresh("instagrapi_sender", fakes)
    session = tmp_path / "session.json"
    session.write_text("{}")
    monkeypatch.setattr(mod, "SESSION", session)
    s = mod.InstagrapiSender()
    assert [c[0] for c in s.cl.calls] == ["load_settings", "login", "dump_settings"]


def test_instagrapi_native_share(ig):
    ig.sender.send("friend", URL)
    assert ("direct_media_share", "pk1_owner", ["555"]) in ig.cl.calls
    assert not any(c[0] == "direct_send" for c in ig.cl.calls)


def test_instagrapi_user_id_cached(ig):
    ig.sender.send("friend", URL)
    ig.sender.send("friend", URL)
    assert [c for c in ig.cl.calls if c[0] == "user_id_from_username"] == \
        [("user_id_from_username", "friend")]


def test_instagrapi_fallback_to_direct_send(ig):
    ig.cl.share_error = ValueError("share endpoint changed")
    ig.sender.send("friend", URL)
    assert ("direct_send", URL, [555]) in ig.cl.calls


@pytest.mark.parametrize("name", ["ChallengeRequired", "FeedbackRequired",
                                  "LoginRequired", "PleaseWaitFewMinutes"])
def test_instagrapi_block_in_native_share_is_not_swallowed(ig, name):
    ig.cl.share_error = getattr(ig.exc, name)("nope")
    with pytest.raises(BlockedError, match=name):
        ig.sender.send("friend", URL)
    assert not any(c[0] == "direct_send" for c in ig.cl.calls)   # no fallback attempted


@pytest.mark.parametrize("name", ["ChallengeRequired", "PleaseWaitFewMinutes"])
def test_instagrapi_block_in_fallback_and_lookup(ig, name):
    ig.cl.share_error = ValueError("x")
    ig.cl.send_error = getattr(ig.exc, name)("nope")
    with pytest.raises(BlockedError):
        ig.sender.send("friend", URL)

    ig.cl.lookup_error = getattr(ig.exc, name)("nope")
    with pytest.raises(BlockedError):
        ig.sender.send("someone_else", URL)


def test_instagrapi_other_errors_propagate_unchanged(ig):
    ig.cl.share_error = ValueError("x")
    ig.cl.send_error = RuntimeError("user not found")
    with pytest.raises(RuntimeError, match="user not found") as exc:
        ig.sender.send("friend", URL)
    assert not isinstance(exc.value, BlockedError)


def test_instagrapi_password_never_in_errors(ig):
    ig.cl.share_error = ig.exc.ChallengeRequired("challenge")
    with pytest.raises(BlockedError) as exc:
        ig.sender.send("friend", URL)
    assert "hunter2" not in str(exc.value)
