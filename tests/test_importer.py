import sys
import types

import pytest

from reeldrop import importer, queue
from reeldrop.importer import ImportResult, format_result, import_collection
from reeldrop.senders import BlockedError


class Obj(types.SimpleNamespace):
    pass


def reel(code, **kw):
    return Obj(code=code, pk=f"pk-{code}", media_type=2, product_type="clips", **kw)


def photo(code):
    return Obj(code=code, pk=f"pk-{code}", media_type=1, product_type="")


def album(code):
    return Obj(code=code, pk=f"pk-{code}", media_type=8, product_type="")


class FakeClient:
    def __init__(self, medias, collections=None):
        self.medias = medias
        self._collections = collections if collections is not None else [
            Obj(id="111", name="Funny"), Obj(id="222", name="Music")]
        self.fetched = []
        self.collections_error = None
        self.medias_error = None

    def collections(self):
        if self.collections_error:
            raise self.collections_error
        return self._collections

    def collection_medias(self, collection_pk):
        self.fetched.append(collection_pk)
        if self.medias_error:
            raise self.medias_error
        return self.medias


def urls(*codes):
    return [f"https://www.instagram.com/reel/{c}/" for c in codes]


def queued():
    return [r["url"] for r in queue.list_reels()]


def test_imports_only_reels(temp_db):
    client = FakeClient([reel("AAA"), photo("BBB"), album("CCC"), reel("DDD")])
    res = import_collection("Funny", client=client)
    assert res.added == urls("AAA", "DDD")
    assert queued() == urls("AAA", "DDD")
    assert len(res.skipped) == 2 and not res.duplicates and not res.errors
    assert client.fetched == ["111"]


def test_video_without_product_type_is_accepted_and_other_product_types_are_not(temp_db):
    medias = [Obj(code="AAA", media_type=2),
              Obj(code="BBB", media_type=2, product_type="feed"),
              Obj(code="CCC", media_type=2, product_type="igtv")]
    res = import_collection("Funny", client=FakeClient(medias))
    assert res.added == urls("AAA") and len(res.skipped) == 2


def test_duplicates_counted(temp_db):
    queue.add_reel("https://www.instagram.com/reel/AAA/")
    res = import_collection("Funny", client=FakeClient([reel("AAA"), reel("BBB")]))
    assert res.duplicates == urls("AAA")
    assert res.added == urls("BBB")
    assert queued() == urls("AAA", "BBB")


def test_rerun_is_idempotent(temp_db):
    client = FakeClient([reel("AAA"), reel("BBB")])
    import_collection("Funny", client=client)
    res = import_collection("Funny", client=client)
    assert not res.added and len(res.duplicates) == 2


def test_media_without_code_is_skipped(temp_db):
    res = import_collection("Funny", client=FakeClient([Obj(pk="9", media_type=2, code="")]))
    assert not res.added and len(res.skipped) == 1


def test_other_value_error_is_skipped(temp_db, monkeypatch):
    def boom(url, note=""):
        raise ValueError("Not an Instagram reel/post link")
    monkeypatch.setattr(queue, "add_reel", boom)
    res = import_collection("Funny", client=FakeClient([reel("AAA")]))
    assert not res.added and len(res.skipped) == 1 and not res.duplicates


def test_unexpected_exception_is_recorded_and_import_continues(temp_db, monkeypatch):
    real = queue.add_reel

    def flaky(url, note=""):
        if "AAA" in url:
            raise RuntimeError("disk on fire")
        return real(url, note)
    monkeypatch.setattr(queue, "add_reel", flaky)
    res = import_collection("Funny", client=FakeClient([reel("AAA"), reel("BBB")]))
    assert res.added == urls("BBB") and len(res.errors) == 1
    assert "disk on fire" in res.errors[0]


def test_limit_respected(temp_db):
    client = FakeClient([reel(f"R{i}") for i in range(10)])
    res = import_collection("Funny", client=client, limit=3)
    assert res.added == urls("R0", "R1", "R2")
    assert queued() == urls("R0", "R1", "R2")


def test_limit_counts_added_not_duplicates(temp_db):
    queue.add_reel("https://www.instagram.com/reel/R0/")
    res = import_collection("Funny", client=FakeClient([reel(f"R{i}") for i in range(5)]), limit=2)
    assert res.duplicates == urls("R0") and res.added == urls("R1", "R2")


def test_limit_capped_at_maximum(temp_db):
    class Recorder(FakeClient):
        def collection_medias(self, collection_pk, amount=None):
            self.amount = amount
            return self.medias
    client = Recorder([reel(f"R{i}") for i in range(250)])
    res = import_collection("Funny", client=client, limit=10_000)
    assert client.amount == importer.MAX_LIMIT == 200
    assert len(res.added) == 200


@pytest.mark.parametrize("bad", [0, -1, "5", None, 2.5, True])
def test_bad_limit(temp_db, bad):
    with pytest.raises(ValueError, match="limit"):
        import_collection("Funny", client=FakeClient([]), limit=bad)


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_bad_name(temp_db, bad):
    with pytest.raises(ValueError, match="Collection name"):
        import_collection(bad, client=FakeClient([]))


def test_dry_run_adds_nothing(temp_db, monkeypatch):
    monkeypatch.setattr(queue, "add_reel",
                        lambda *a, **k: pytest.fail("add_reel must not be called"))
    res = import_collection("Funny", client=FakeClient([reel("AAA"), photo("BBB"), reel("CCC")]),
                            dry_run=True)
    assert res.dry_run and res.added == urls("AAA", "CCC") and len(res.skipped) == 1
    assert queued() == []


def test_dry_run_respects_limit(temp_db):
    res = import_collection("Funny", client=FakeClient([reel(f"R{i}") for i in range(5)]),
                            limit=2, dry_run=True)
    assert res.added == urls("R0", "R1")


def test_collection_match_case_insensitive_and_pk_fallback(temp_db):
    cols = [Obj(pk="777", name="Funny Cats")]
    client = FakeClient([reel("AAA")], collections=cols)
    res = import_collection("  fUnNy cATS ", client=client)
    assert res.added == urls("AAA") and client.fetched == ["777"]


def test_collections_as_dicts(temp_db):
    client = FakeClient([reel("AAA")], collections=[{"id": "5", "name": "Dicty"}])
    assert import_collection("dicty", client=client).added == urls("AAA")


def test_unknown_collection_lists_names(temp_db):
    with pytest.raises(ValueError) as exc:
        import_collection("Nope", client=FakeClient([reel("AAA")]))
    msg = str(exc.value)
    assert "Nope" in msg and "Funny" in msg and "Music" in msg
    assert queued() == []


def test_unknown_collection_no_collections(temp_db):
    with pytest.raises(ValueError, match=r"\(none\)"):
        import_collection("Nope", client=FakeClient([], collections=[]))


class ChallengeRequired(Exception):
    pass


class PleaseWaitFewMinutes(Exception):
    pass


class LoginRequired(Exception):
    pass


class FeedbackRequired(Exception):
    pass


@pytest.mark.parametrize("exc_cls", [ChallengeRequired, PleaseWaitFewMinutes,
                                     LoginRequired, FeedbackRequired])
@pytest.mark.parametrize("where", ["collections_error", "medias_error"])
def test_block_exceptions_become_blocked_error(temp_db, exc_cls, where):
    client = FakeClient([reel("AAA")])
    setattr(client, where, exc_cls("nope"))
    with pytest.raises(BlockedError, match=exc_cls.__name__):
        import_collection("Funny", client=client)
    assert queued() == []


def test_subclass_of_block_exception_is_blocked(temp_db):
    class Sub(ChallengeRequired):
        pass
    client = FakeClient([])
    client.collections_error = Sub("x")
    with pytest.raises(BlockedError):
        import_collection("Funny", client=client)


def test_other_exceptions_propagate_unchanged(temp_db):
    client = FakeClient([])
    client.medias_error = RuntimeError("network down")
    with pytest.raises(RuntimeError, match="network down") as exc:
        import_collection("Funny", client=client)
    assert not isinstance(exc.value, BlockedError)


# --- lazily built client (fake instagrapi in sys.modules) ---------------------

def install_fake_instagrapi(monkeypatch, medias, login_error=None):
    exc_mod = types.ModuleType("instagrapi.exceptions")
    for n in ("ChallengeRequired", "FeedbackRequired", "LoginRequired", "PleaseWaitFewMinutes"):
        setattr(exc_mod, n, type(n, (Exception,), {}))

    class Client(FakeClient):
        instances = []

        def __init__(self):
            super().__init__(medias)
            self.calls = []
            Client.instances.append(self)

        def load_settings(self, path):
            self.calls.append(("load_settings",))

        def login(self, user, password):
            self.calls.append(("login", user, password))
            if login_error:
                raise getattr(exc_mod, login_error)("login blocked")

        def dump_settings(self, path):
            self.calls.append(("dump_settings",))

    pkg = types.ModuleType("instagrapi")
    pkg.__path__ = []
    pkg.Client = Client
    pkg.exceptions = exc_mod
    monkeypatch.setitem(sys.modules, "instagrapi", pkg)
    monkeypatch.setitem(sys.modules, "instagrapi.exceptions", exc_mod)
    # make sure the sender module is imported against the fakes, and removed afterwards
    sys.modules.pop("reeldrop.senders.instagrapi_sender", None)
    return Client


@pytest.fixture
def lazy_env(monkeypatch, tmp_path):
    monkeypatch.setenv("IG_USERNAME", "me")
    monkeypatch.setenv("IG_PASSWORD", "hunter2-password")
    yield
    sys.modules.pop("reeldrop.senders.instagrapi_sender", None)
    senders = sys.modules.get("reeldrop.senders")
    if senders is not None and hasattr(senders, "instagrapi_sender"):
        delattr(senders, "instagrapi_sender")


def test_lazy_client_is_built_from_sender(temp_db, monkeypatch, tmp_path, lazy_env):
    Client = install_fake_instagrapi(monkeypatch, [reel("AAA"), photo("BBB")])
    from reeldrop.senders import instagrapi_sender
    monkeypatch.setattr(instagrapi_sender, "SESSION", tmp_path / "data" / "session.json")
    res = import_collection("Funny")
    assert res.added == urls("AAA") and len(res.skipped) == 1
    assert len(Client.instances) == 1
    assert ("login", "me", "hunter2-password") in Client.instances[0].calls


def test_lazy_client_login_block_becomes_blocked_error(temp_db, monkeypatch, tmp_path, lazy_env):
    install_fake_instagrapi(monkeypatch, [], login_error="ChallengeRequired")
    from reeldrop.senders import instagrapi_sender
    monkeypatch.setattr(instagrapi_sender, "SESSION", tmp_path / "data" / "session.json")
    with pytest.raises(BlockedError, match="ChallengeRequired") as exc:
        import_collection("Funny")
    assert "hunter2" not in str(exc.value)


def test_lazy_client_missing_credentials(temp_db, monkeypatch, tmp_path, lazy_env):
    install_fake_instagrapi(monkeypatch, [])
    monkeypatch.delenv("IG_USERNAME")
    from reeldrop.senders import instagrapi_sender
    monkeypatch.setattr(instagrapi_sender, "SESSION", tmp_path / "data" / "session.json")
    with pytest.raises(RuntimeError, match="IG_USERNAME"):
        import_collection("Funny")


# --- format_result -------------------------------------------------------------

def test_format_result():
    r = ImportResult(collection="Funny", added=["a", "b"], duplicates=["c"],
                     skipped=["x: not a reel"], errors=["y: boom"])
    out = format_result(r)
    assert out.splitlines()[0] == \
        "Added 2 reel(s) from 'Funny'; 1 already queued, 1 skipped, 1 error(s)."
    assert "skipped: x: not a reel" in out and "error: y: boom" in out


def test_format_result_dry_run():
    assert format_result(ImportResult(collection="F", dry_run=True, added=["a"])
                         ).startswith("Would add 1 reel(s)")
