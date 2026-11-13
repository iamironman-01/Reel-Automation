"""InstagrapiSender behaviour with a fake instagrapi (no network, library not needed)."""
import importlib
import logging
import sys
import types

import pytest

from reeldrop.senders.base import BLOCK_ERROR_NAMES, BlockedError, is_block_error

URL = "https://www.instagram.com/reel/AAA/"
PASSWORD = "hunter2-Pa55word"

EXCEPTION_NAMES = [
    "ChallengeRequired", "ChallengeError", "FeedbackRequired", "LoginRequired",
    "ClientLoginRequired", "PleaseWaitFewMinutes", "RateLimitError", "SentryBlock",
    "AccountSuspended", "CaptchaChallengeRequired", "ReloginAttemptExceeded",
    "TwoFactorRequired", "BadPassword", "BadCredentials", "UserNotFound",
    "ClientThrottledError", "ProxyAddressIsBlocked",
]


class Message:
    def __init__(self, id="m1", thread_id="t1"):
        self.id = id
        self.thread_id = thread_id


def make_fakes():
    exc = types.ModuleType("instagrapi.exceptions")
    for n in EXCEPTION_NAMES:
        setattr(exc, n, type(n, (Exception,), {}))
    # a challenge subclass, like instagrapi's ChallengeUnknownStep(ChallengeError)
    exc.ChallengeSubclass = type("ChallengeSubclass", (exc.ChallengeError,), {})

    class Client:
        instances = []

        def __init__(self):
            self.calls = []
            self.share_error = None
            self.share_result = Message()
            self.send_error = None
            self.send_result = Message()
            self.lookup_error = None
            self.login_error = None
            self.login_errors = []
            self.load_error = None
            self.media_id_error = None
            self.login_kwargs = None
            Client.instances.append(self)

        def load_settings(self, path, override_app_version=False):
            self.calls.append(("load_settings", str(path), override_app_version))
            if self.load_error:
                raise self.load_error

        def login(self, username, password, relogin=False, verification_code=""):
            self.calls.append(("login", username, password, verification_code))
            if self.login_errors:                       # one error per call, then success
                raise self.login_errors.pop(0)
            if self.login_error:
                raise self.login_error

        def dump_settings(self, path):
            self.calls.append(("dump_settings", str(path)))

        @staticmethod
        def totp_generate_code(seed):
            return "T" + seed

        def user_id_from_username(self, name):
            self.calls.append(("lookup", name))
            if self.lookup_error:
                raise self.lookup_error
            return "555"

        def media_pk_from_url(self, url):
            self.calls.append(("media_pk_from_url", url))
            return "pk1"

        def media_id(self, pk):
            self.calls.append(("media_id", pk))
            if self.media_id_error:
                raise self.media_id_error
            return "pk1_owner"

        def direct_media_share(self, media_id, user_ids=(), thread_ids=(),
                               send_attribute="feed_timeline", media_type="photo"):
            self.calls.append(("share", media_id, list(user_ids), media_type))
            if self.share_error:
                raise self.share_error
            return self.share_result

        def direct_send(self, text, user_ids=()):
            self.calls.append(("send", text, list(user_ids)))
            if self.send_error:
                raise self.send_error
            return self.send_result

    pkg = types.ModuleType("instagrapi")
    pkg.__path__ = []
    pkg.Client = Client
    pkg.exceptions = exc
    return {"instagrapi": pkg, "instagrapi.exceptions": exc}, Client, exc


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("IG_USERNAME", "me")
    monkeypatch.setenv("IG_PASSWORD", PASSWORD)
    monkeypatch.delenv("IG_TOTP_SECRET", raising=False)
    monkeypatch.delenv("IG_VERIFICATION_CODE", raising=False)
    return tmp_path


@pytest.fixture
def mod(monkeypatch, env):
    """The sender module imported against the fake instagrapi; SESSION points at tmp_path."""
    fakes, Client, exc = make_fakes()
    for name, fake in fakes.items():
        monkeypatch.setitem(sys.modules, name, fake)
    sys.modules.pop("reeldrop.senders.instagrapi_sender", None)
    module = importlib.import_module("reeldrop.senders.instagrapi_sender")
    monkeypatch.setattr(module, "SESSION", env / "data" / "session.json")
    module.Client = Client
    module.exc = exc
    yield module
    sys.modules.pop("reeldrop.senders.instagrapi_sender", None)
    senders = sys.modules.get("reeldrop.senders")
    if senders is not None and hasattr(senders, "instagrapi_sender"):
        delattr(senders, "instagrapi_sender")


@pytest.fixture
def sender(mod):
    return mod.InstagrapiSender()


def kinds(cl):
    return [c[0] for c in cl.calls]


# --- is_block_error / BLOCK_ERROR_NAMES ---------------------------------------

def fake_exc(name, base=Exception):
    return type(name, (base,), {})(f"{name} happened")


@pytest.mark.parametrize("name", sorted(BLOCK_ERROR_NAMES))
def test_every_block_name_is_a_block(name):
    assert is_block_error(fake_exc(name))


def test_block_names_contain_the_contract_set():
    expected = {
        "ChallengeRequired", "ChallengeError", "ChallengeRedirection",
        "ChallengeSelfieCaptcha", "ChallengeUnknownStep", "SelectContactPointRecoveryForm",
        "RecaptchaChallengeForm", "SubmitPhoneNumberForm", "LegacyForceSetNewPasswordForm",
        "FeedbackRequired", "SignupSpamError", "LoginRequired", "ClientLoginRequired",
        "PleaseWaitFewMinutes", "RateLimitError", "SentryBlock", "AccountSuspended",
        "CaptchaChallengeRequired", "ReloginAttemptExceeded",
        "ClientThrottledError", "ProxyAddressIsBlocked",    # added after reading the real library
    }
    assert BLOCK_ERROR_NAMES == frozenset(expected)


def test_is_block_error_matches_subclasses_through_the_mro():
    challenge_error = type("ChallengeError", (Exception,), {})
    unknown_step = type("ChallengeUnknownStepish", (challenge_error,), {})
    assert is_block_error(unknown_step("x"))
    assert is_block_error(type("Deeper", (unknown_step,), {})("x"))


@pytest.mark.parametrize("name", ["BadPassword", "BadCredentials", "TwoFactorRequired",
                                  "UserNotFound", "ClientBadRequestError"])
def test_config_errors_are_not_blocks(name):
    assert not is_block_error(fake_exc(name))


@pytest.mark.parametrize("exc", [ValueError("x"), RuntimeError("x"), KeyError("x"), Exception()])
def test_ordinary_exceptions_are_not_blocks(exc):
    assert not is_block_error(exc)


def test_importer_and_discovery_share_the_same_detection():
    from reeldrop import discovery, importer
    assert importer.BLOCK_ERROR_NAMES is BLOCK_ERROR_NAMES
    for name in ("RateLimitError", "SentryBlock", "ChallengeError"):
        e = fake_exc(name)
        assert importer._is_block_error(e) and discovery._is_block_error(e)
    assert not importer._is_block_error(fake_exc("BadPassword"))


# --- construction / login ---------------------------------------------------------

def test_sets_attributes_delay_and_logs_in(mod, env):
    s = mod.InstagrapiSender()
    assert s.name == "instagrapi"
    assert s.username == "me"
    assert s.last_method is None
    assert s._user_ids == {}
    assert s.cl.delay_range == [2, 5]
    assert kinds(s.cl) == ["login", "dump_settings"]
    assert s.cl.calls[0] == ("login", "me", PASSWORD, "")
    assert (env / "data").is_dir()        # data/ created before dump_settings


@pytest.mark.parametrize("missing", ["IG_USERNAME", "IG_PASSWORD"])
def test_missing_env_names_the_variable(mod, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=missing) as exc:
        mod.InstagrapiSender()
    assert PASSWORD not in str(exc.value)


def test_empty_env_counts_as_missing(mod, monkeypatch):
    monkeypatch.setenv("IG_USERNAME", "  ")
    with pytest.raises(RuntimeError, match="IG_USERNAME"):
        mod.InstagrapiSender()


def test_saved_session_is_loaded(mod, env):
    (env / "data").mkdir()
    (env / "data" / "session.json").write_text("{}")
    s = mod.InstagrapiSender()
    assert kinds(s.cl) == ["load_settings", "login", "dump_settings"]


def test_no_load_settings_without_a_session_file(sender):
    assert "load_settings" not in kinds(sender.cl)


def test_broken_saved_session_is_ignored(mod, env):
    (env / "data").mkdir()
    (env / "data" / "session.json").write_text("not json")
    orig_init = mod.Client.__init__

    def init(self):
        orig_init(self)
        self.load_error = ValueError("corrupt session")

    mod.Client.__init__ = init
    s = mod.InstagrapiSender()
    assert kinds(s.cl) == ["load_settings", "login", "dump_settings"]


def patch_login_error(mod, error):
    orig_init = mod.Client.__init__

    def init(self):
        orig_init(self)
        self.login_error = error

    mod.Client.__init__ = init


def test_two_factor_without_code_gives_a_clear_error(mod):
    patch_login_error(mod, mod.exc.TwoFactorRequired("2fa needed"))
    with pytest.raises(RuntimeError) as exc:
        mod.InstagrapiSender()
    assert str(exc.value) == ("Instagram asked for a two-factor code. Set IG_TOTP_SECRET "
                              "(recommended) or IG_VERIFICATION_CODE in .env and restart.")
    assert not isinstance(exc.value, BlockedError)


def test_totp_secret_is_turned_into_a_code(mod, monkeypatch):
    monkeypatch.setenv("IG_TOTP_SECRET", "ABCD EFGH")
    s = mod.InstagrapiSender()
    assert s.cl.calls[0] == ("login", "me", PASSWORD, "TABCDEFGH")


def test_verification_code_is_used(mod, monkeypatch):
    monkeypatch.setenv("IG_VERIFICATION_CODE", " 123456 ")
    s = mod.InstagrapiSender()
    assert s.cl.calls[0] == ("login", "me", PASSWORD, "123456")


def test_totp_secret_wins_over_verification_code(mod, monkeypatch):
    monkeypatch.setenv("IG_TOTP_SECRET", "SEED")
    monkeypatch.setenv("IG_VERIFICATION_CODE", "123456")
    assert mod.InstagrapiSender().cl.calls[0][3] == "TSEED"


def test_two_factor_with_a_code_that_is_rejected(mod, monkeypatch):
    monkeypatch.setenv("IG_VERIFICATION_CODE", "000000")
    patch_login_error(mod, mod.exc.TwoFactorRequired("bad code"))
    with pytest.raises(RuntimeError, match="rejected the two-factor code") as exc:
        mod.InstagrapiSender()
    assert not isinstance(exc.value, BlockedError)


@pytest.mark.parametrize("name", ["BadPassword", "BadCredentials"])
def test_bad_password_is_a_runtime_error_without_the_password(mod, name):
    patch_login_error(mod, getattr(mod.exc, name)(f"wrong: {PASSWORD}"))
    with pytest.raises(RuntimeError) as exc:
        mod.InstagrapiSender()
    assert str(exc.value) == "Instagram rejected the username or password."
    assert not isinstance(exc.value, BlockedError)
    assert PASSWORD not in str(exc.value)


@pytest.mark.parametrize("name", ["ChallengeRequired", "RateLimitError", "SentryBlock",
                                  "AccountSuspended", "LoginRequired", "FeedbackRequired",
                                  "PleaseWaitFewMinutes"])
def test_block_during_login_becomes_blocked_error(mod, name):
    patch_login_error(mod, getattr(mod.exc, name)(f"blocked, pw {PASSWORD}"))
    with pytest.raises(BlockedError, match=name) as exc:
        mod.InstagrapiSender()
    assert PASSWORD not in str(exc.value)


def test_other_login_errors_propagate(mod):
    patch_login_error(mod, ConnectionError("network down"))
    with pytest.raises(ConnectionError):
        mod.InstagrapiSender()


# --- resolve_user -----------------------------------------------------------------

def test_numeric_recipient_skips_the_lookup(sender):
    assert sender.resolve_user("12345") == 12345
    assert sender.resolve_user("@12345") == 12345
    assert "lookup" not in kinds(sender.cl)


def test_at_sign_is_stripped_and_id_is_an_int(sender):
    assert sender.resolve_user("@friend") == 555
    assert ("lookup", "friend") in sender.cl.calls


def test_lookup_is_cached(sender):
    sender.resolve_user("friend")
    sender.resolve_user("@friend")
    assert kinds(sender.cl).count("lookup") == 1


def test_unknown_user_is_a_runtime_error(sender, mod):
    sender.cl.lookup_error = mod.exc.UserNotFound("no such user")
    with pytest.raises(RuntimeError, match="Instagram user 'ghost' was not found") as exc:
        sender.resolve_user("ghost")
    assert not isinstance(exc.value, BlockedError)


def test_other_lookup_failure_is_a_runtime_error(sender):
    sender.cl.lookup_error = ValueError(f"weird {PASSWORD}")
    with pytest.raises(RuntimeError, match="Could not look up Instagram user 'x'") as exc:
        sender.resolve_user("x")
    assert PASSWORD not in str(exc.value)
    assert not isinstance(exc.value, BlockedError)


def test_block_during_lookup(sender, mod):
    sender.cl.lookup_error = mod.exc.RateLimitError("slow down")
    with pytest.raises(BlockedError, match="RateLimitError"):
        sender.resolve_user("friend")


def test_empty_recipient_is_rejected(sender):
    with pytest.raises(RuntimeError, match="empty"):
        sender.resolve_user("@")


# --- send: native / link --------------------------------------------------------------

def test_native_share_uses_video_and_the_user_id(sender):
    sender.send("friend", URL)
    assert ("media_pk_from_url", URL) in sender.cl.calls
    assert ("media_id", "pk1") in sender.cl.calls
    assert ("share", "pk1_owner", [555], "video") in sender.cl.calls
    assert "send" not in kinds(sender.cl)
    assert sender.last_method == "native"


def test_numeric_recipient_send(sender):
    sender.send("777", URL)
    assert ("share", "pk1_owner", [777], "video") in sender.cl.calls
    assert "lookup" not in kinds(sender.cl)


def test_falls_back_to_link_when_native_raises(sender, caplog):
    sender.cl.share_error = ValueError("endpoint changed")
    with caplog.at_level(logging.WARNING, logger="reeldrop"):
        sender.send("friend", URL)
    assert ("send", URL, [555]) in sender.cl.calls
    assert sender.last_method == "link"
    assert "ValueError" in caplog.text and "endpoint changed" in caplog.text


def test_falls_back_to_link_when_media_id_fails(sender):
    sender.cl.media_id_error = KeyError("owner")
    sender.send("friend", URL)
    assert "share" not in kinds(sender.cl)
    assert ("send", URL, [555]) in sender.cl.calls
    assert sender.last_method == "link"


@pytest.mark.parametrize("empty", [None, Message(id="", thread_id=""), Message(None, None),
                                   types.SimpleNamespace()])
def test_falls_back_to_link_when_native_message_is_empty(sender, empty):
    sender.cl.share_result = empty
    sender.send("friend", URL)
    assert ("send", URL, [555]) in sender.cl.calls
    assert sender.last_method == "link"


@pytest.mark.parametrize("partial", [Message(id="m", thread_id=""), Message(id="", thread_id="t")])
def test_message_with_only_id_or_thread_id_counts_as_delivered(sender, partial):
    sender.cl.share_result = partial
    sender.send("friend", URL)
    assert sender.last_method == "native"


@pytest.mark.parametrize("empty", [None, Message(id="", thread_id="")])
def test_empty_link_message_is_an_error(sender, empty):
    sender.cl.share_error = ValueError("x")
    sender.cl.send_result = empty
    with pytest.raises(RuntimeError, match="did not confirm the message was sent") as exc:
        sender.send("friend", URL)
    assert not isinstance(exc.value, BlockedError)
    assert sender.last_method is None


def test_both_empty_is_an_error(sender):
    sender.cl.share_result = Message(id="", thread_id="")
    sender.cl.send_result = None
    with pytest.raises(RuntimeError, match="did not confirm"):
        sender.send("friend", URL)


def test_link_failure_with_a_normal_error_propagates(sender):
    sender.cl.share_error = ValueError("x")
    sender.cl.send_error = RuntimeError("thread closed")
    with pytest.raises(RuntimeError, match="thread closed") as exc:
        sender.send("friend", URL)
    assert not isinstance(exc.value, BlockedError)


def test_last_method_is_reset_on_each_send(sender):
    sender.send("friend", URL)
    assert sender.last_method == "native"
    sender.cl.share_error = ValueError("x")
    sender.cl.send_error = RuntimeError("fail")
    with pytest.raises(RuntimeError):
        sender.send("friend", URL)
    assert sender.last_method is None


# --- send: blocks ------------------------------------------------------------------------

BLOCK_CASES = ["ChallengeRequired", "RateLimitError", "SentryBlock", "AccountSuspended",
               "LoginRequired", "FeedbackRequired", "PleaseWaitFewMinutes", "ChallengeSubclass"]


@pytest.mark.parametrize("name", BLOCK_CASES)
def test_block_in_native_share_is_not_retried_as_a_link(sender, mod, name):
    sender.cl.share_error = getattr(mod.exc, name)(f"nope {PASSWORD}")
    with pytest.raises(BlockedError, match=name) as exc:
        sender.send("friend", URL)
    assert "send" not in kinds(sender.cl)
    assert PASSWORD not in str(exc.value)
    assert sender.last_method is None


@pytest.mark.parametrize("name", BLOCK_CASES)
def test_block_in_media_id_is_not_retried_as_a_link(sender, mod, name):
    sender.cl.media_id_error = getattr(mod.exc, name)("nope")
    with pytest.raises(BlockedError, match=name):
        sender.send("friend", URL)
    assert "send" not in kinds(sender.cl)


@pytest.mark.parametrize("name", BLOCK_CASES)
def test_block_in_link_fallback(sender, mod, name):
    sender.cl.share_error = ValueError("x")
    sender.cl.send_error = getattr(mod.exc, name)("nope")
    with pytest.raises(BlockedError, match=name):
        sender.send("friend", URL)


@pytest.mark.parametrize("name", BLOCK_CASES)
def test_block_in_lookup_during_send(sender, mod, name):
    sender.cl.lookup_error = getattr(mod.exc, name)("nope")
    with pytest.raises(BlockedError, match=name):
        sender.send("friend", URL)
    assert "share" not in kinds(sender.cl) and "send" not in kinds(sender.cl)


@pytest.mark.parametrize("name", BLOCK_CASES)
def test_block_in_send_text(sender, mod, name):
    sender.cl.send_error = getattr(mod.exc, name)("nope")
    with pytest.raises(BlockedError, match=name):
        sender.send_text("friend", "hi")


def test_block_message_includes_the_class_name(sender, mod):
    sender.cl.share_error = mod.exc.ChallengeSubclass("step 3")
    with pytest.raises(BlockedError) as exc:
        sender.send("friend", URL)
    assert str(exc.value) == "ChallengeSubclass: step 3"


# --- send_text -------------------------------------------------------------------------------

def test_send_text(sender):
    sender.send_text("@friend", "a note")
    assert ("send", "a note", [555]) in sender.cl.calls


def test_send_text_numeric_recipient_skips_lookup(sender):
    sender.send_text("42", "a note")
    assert ("send", "a note", [42]) in sender.cl.calls
    assert "lookup" not in kinds(sender.cl)


def test_send_text_empty_message_is_an_error(sender):
    sender.cl.send_result = Message(id="", thread_id="")
    with pytest.raises(RuntimeError, match="did not confirm") as exc:
        sender.send_text("friend", "hi")
    assert not isinstance(exc.value, BlockedError)


def test_send_text_normal_error_propagates(sender):
    sender.cl.send_error = ValueError("boom")
    with pytest.raises(ValueError, match="boom"):
        sender.send_text("friend", "hi")


# --- the password never leaks ---------------------------------------------------------------------

def test_password_never_in_logs_or_errors(mod, sender, caplog):
    sender.cl.share_error = ValueError(f"share failed for {PASSWORD}")
    with caplog.at_level(logging.DEBUG):
        sender.send("friend", URL)
        sender.cl.send_error = mod.exc.ChallengeRequired(f"challenge {PASSWORD}")
        with pytest.raises(BlockedError) as exc:
            sender.send("friend", URL)
    assert PASSWORD not in caplog.text
    assert PASSWORD not in str(exc.value)
    assert "***" in caplog.text


def test_password_not_in_session_load_logs(mod, env, caplog):
    (env / "data").mkdir()
    (env / "data" / "session.json").write_text("{}")
    orig_init = mod.Client.__init__

    def init(self):
        orig_init(self)
        self.load_error = ValueError(f"bad session {PASSWORD}")

    mod.Client.__init__ = init
    with caplog.at_level(logging.DEBUG):
        mod.InstagrapiSender()
    assert PASSWORD not in caplog.text


# --- real-library findings: throttling, IP block, older saved sessions -------------

@pytest.mark.parametrize("name", ["ClientThrottledError", "ProxyAddressIsBlocked"])
def test_throttle_and_ip_block_are_blocks(name):
    assert is_block_error(fake_exc(name))


@pytest.mark.parametrize("name", ["ClientThrottledError", "ProxyAddressIsBlocked"])
def test_throttle_during_send_pauses_instead_of_falling_back(sender, mod, name):
    sender.cl.share_error = type(name, (Exception,), {})("429")
    with pytest.raises(BlockedError):
        sender.send("friend", URL)
    assert "send" not in kinds(sender.cl)         # no link fallback after a throttle


def _session_file(env):
    (env / "data").mkdir()
    (env / "data" / "session.json").write_text("{}")


def test_old_saved_session_is_reloaded_with_override_once(mod, env, monkeypatch):
    _session_file(env)
    orig_init = mod.Client.__init__

    def init(self):
        orig_init(self)
        self.login_errors = [RuntimeError("CAA login requires bloks_versioning_id for the saved app profile.")]

    monkeypatch.setattr(mod.Client, "__init__", init)
    s = mod.InstagrapiSender()
    loads = [c for c in s.cl.calls if c[0] == "load_settings"]
    assert [c[2] for c in loads] == [False, True]            # second load overrides the app version
    assert kinds(s.cl) == ["load_settings", "login", "load_settings", "login", "dump_settings"]


def test_other_login_errors_do_not_trigger_the_reload(mod, env, monkeypatch):
    _session_file(env)
    orig_init = mod.Client.__init__

    def init(self):
        orig_init(self)
        self.login_errors = [RuntimeError("network unreachable")]

    monkeypatch.setattr(mod.Client, "__init__", init)
    with pytest.raises(RuntimeError, match="network unreachable"):
        mod.InstagrapiSender()


def test_override_reload_only_when_a_session_was_loaded(mod, env, monkeypatch):
    orig_init = mod.Client.__init__             # no session file exists in this test

    def init(self):
        orig_init(self)
        self.login_errors = [RuntimeError("CAA login requires bloks_versioning_id")]

    monkeypatch.setattr(mod.Client, "__init__", init)
    with pytest.raises(RuntimeError, match="bloks_versioning_id"):
        mod.InstagrapiSender()


def test_override_reload_happens_at_most_once(mod, env, monkeypatch):
    _session_file(env)
    orig_init = mod.Client.__init__

    def init(self):
        orig_init(self)
        self.login_errors = [RuntimeError("bloks_versioning_id missing"),
                             RuntimeError("bloks_versioning_id missing")]

    monkeypatch.setattr(mod.Client, "__init__", init)
    with pytest.raises(RuntimeError):
        mod.InstagrapiSender()
    # exactly two logins were attempted, not an endless loop


def test_missing_library_gives_an_install_hint(monkeypatch, env):
    sys.modules.pop("reeldrop.senders.instagrapi_sender", None)
    monkeypatch.setitem(sys.modules, "instagrapi", None)      # makes `import instagrapi` raise ImportError
    module = importlib.import_module("reeldrop.senders.instagrapi_sender")
    try:
        with pytest.raises(RuntimeError, match="pip install -r requirements.txt"):
            module.InstagrapiSender()
    finally:
        sys.modules.pop("reeldrop.senders.instagrapi_sender", None)
