import sys
import threading
import types
from datetime import datetime, timedelta

import pytest

from reeldrop import discovery, queue, settings
from reeldrop.config import Config, Discovery, DiscoverySource, parse_config
from reeldrop.discovery import (
    SETTING_LAST_RUN, SETTING_LAST_SUMMARY, SETTING_REQUESTED, RefillResult,
    fetch_source, format_result, make_client_provider, refill, request_refill, tick, top_up,
)
from reeldrop.senders import BlockedError


class Obj(types.SimpleNamespace):
    pass


def reel(code, **kw):
    return Obj(code=code, pk=f"pk-{code}", media_type=2, product_type="clips", **kw)


def photo(code):
    return Obj(code=code, pk=f"pk-{code}", media_type=1, product_type="")


def album(code):
    return Obj(code=code, pk=f"pk-{code}", media_type=8, product_type="")


def url(code):
    return f"https://www.instagram.com/reel/{code}/"


def src(type_="hashtag", value="funny", **kw):
    return DiscoverySource(type=type_, value=value, **kw)


def disc(*sources, **kw):
    kw.setdefault("enabled", True)
    kw.setdefault("delay_seconds", 0)
    return Discovery(sources=list(sources), **kw)


class ChallengeRequired(Exception):
    pass


class PleaseWaitFewMinutes(Exception):
    pass


class SubChallenge(ChallengeRequired):
    pass


class FakeClient:
    """hashtag/user/collection endpoints driven by dicts: name -> list of medias (or exception)."""

    def __init__(self, hashtags=None, users=None, collections=None, clips=True):
        self.hashtags = hashtags or {}
        self.users = users or {}
        self.collections_ = collections or {}
        self.calls = []
        if clips:
            self.user_clips = self._user_clips

    def _get(self, table, key):
        value = table[key]
        if isinstance(value, Exception):
            raise value
        return value

    def hashtag_medias_top(self, name, amount):
        self.calls.append(("top", name, amount))
        return self._get(self.hashtags, name)

    def hashtag_medias_recent(self, name, amount):
        self.calls.append(("recent", name, amount))
        return self._get(self.hashtags, name)

    def user_id_from_username(self, name):
        self.calls.append(("uid", name))
        return f"uid-{name}"

    def _user_clips(self, uid, amount):
        self.calls.append(("clips", uid, amount))
        return self._get(self.users, uid)

    def user_medias(self, uid, amount):
        self.calls.append(("medias", uid, amount))
        return self._get(self.users, uid)

    def collections(self):
        return [Obj(id=f"id-{n}", name=n) for n in self.collections_]

    def collection_medias(self, pk):
        self.calls.append(("collection", pk))
        return self._get(self.collections_, pk[3:])


def queued():
    return [r["url"] for r in queue.list_reels()]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(discovery, "_sleep", sleeps.append)
    return sleeps


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(discovery, "notify_me", sent.append)
    return sent


# --- config -------------------------------------------------------------------

def test_config_absent_discovery_is_disabled_default():
    cfg = parse_config({"rules": []})
    assert cfg.discovery == Discovery()
    assert cfg.discovery.enabled is False and cfg.discovery.sources == []
    assert parse_config(None).discovery.enabled is False
    assert parse_config({"discovery": None}).discovery == Discovery()


def test_config_defaults_do_not_share_sources_list():
    a, b = Discovery(), Discovery()
    a.sources.append(src())
    assert b.sources == []


def test_config_full_example():
    cfg = parse_config({"discovery": {
        "enabled": True, "min_pending": 20, "fetch_per_source": 5, "max_per_refill": 40,
        "check_every_minutes": 60, "min_likes": 100, "delay_seconds": 2,
        "sources": [
            {"type": "hashtag", "value": "#funny", "mode": "recent"},
            {"type": "hashtag", "value": "memes"},
            {"type": "user", "value": "@some.creator_1"},
            {"type": "collection", "value": " Funny ", "to": " pal "},
            {"type": "collection", "value": "Music", "to": "  "},
        ]}})
    d = cfg.discovery
    assert (d.enabled, d.min_pending, d.fetch_per_source, d.max_per_refill) == (True, 20, 5, 40)
    assert (d.check_every_minutes, d.min_likes, d.delay_seconds) == (60, 100, 2)
    assert d.sources == [
        DiscoverySource("hashtag", "funny", "recent", None),
        DiscoverySource("hashtag", "memes", "top", None),
        DiscoverySource("user", "some.creator_1", "top", None),
        DiscoverySource("collection", "Funny", "top", "pal"),
        DiscoverySource("collection", "Music", "top", None),
    ]


def test_config_disabled_may_have_no_sources():
    assert parse_config({"discovery": {"enabled": False}}).discovery.sources == []
    assert parse_config({"discovery": {}}).discovery.enabled is False


@pytest.mark.parametrize("raw", ["yes", ["a"], 5])
def test_config_discovery_must_be_mapping(raw):
    with pytest.raises(ValueError, match="discovery must be a mapping"):
        parse_config({"discovery": raw})


@pytest.mark.parametrize("flag", ["true", 1, "yes", 0])
def test_config_enabled_must_be_bool(flag):
    with pytest.raises(ValueError, match="enabled must be true or false"):
        parse_config({"discovery": {"enabled": flag}})


def test_config_enabled_without_sources_rejected():
    with pytest.raises(ValueError, match="sources is empty"):
        parse_config({"discovery": {"enabled": True}})
    with pytest.raises(ValueError, match="sources is empty"):
        parse_config({"discovery": {"enabled": True, "sources": []}})


@pytest.mark.parametrize("key,bad", [
    ("min_pending", 0), ("min_pending", 501),
    ("fetch_per_source", 0), ("fetch_per_source", 51),
    ("max_per_refill", 0), ("max_per_refill", 101),
    ("check_every_minutes", 29), ("check_every_minutes", 1441),
    ("min_likes", -1),
    ("delay_seconds", -1), ("delay_seconds", 121),
])
def test_config_int_ranges(key, bad):
    with pytest.raises(ValueError, match=key):
        parse_config({"discovery": {key: bad}})


@pytest.mark.parametrize("key,good", [
    ("min_pending", 1), ("min_pending", 500), ("check_every_minutes", 30),
    ("check_every_minutes", 1440), ("min_likes", 0), ("delay_seconds", 0), ("delay_seconds", 120),
    ("fetch_per_source", 50), ("max_per_refill", 100),
])
def test_config_int_boundaries_accepted(key, good):
    assert getattr(parse_config({"discovery": {key: good}}).discovery, key) == good


@pytest.mark.parametrize("bad", [True, "abc", 1.5, [1]])
def test_config_ints_must_be_whole_numbers(bad):
    with pytest.raises(ValueError, match="min_pending"):
        parse_config({"discovery": {"min_pending": bad}})


def test_config_sources_must_be_list_of_mappings():
    with pytest.raises(ValueError, match="sources must be a list"):
        parse_config({"discovery": {"sources": "funny"}})
    with pytest.raises(ValueError, match="must be a mapping"):
        parse_config({"discovery": {"sources": ["funny"]}})


@pytest.mark.parametrize("bad", [None, "", "video", 3])
def test_config_source_type_validated(bad):
    with pytest.raises(ValueError, match="type must be one of"):
        parse_config({"discovery": {"sources": [{"type": bad, "value": "x"}]}})


@pytest.mark.parametrize("t,bad", [
    ("hashtag", None), ("hashtag", ""), ("hashtag", "  "), ("hashtag", 2024), ("user", None),
    ("collection", ""), ("collection", 5),
    ("hashtag", "#"), ("hashtag", "two words"), ("hashtag", "fun-ny"), ("hashtag", "a.b"),
    ("user", "@"), ("user", "bad name"), ("user", "bad-name"), ("user", "bad/name"),
])
def test_config_source_value_validated(t, bad):
    with pytest.raises(ValueError):
        parse_config({"discovery": {"sources": [{"type": t, "value": bad}]}})


def test_config_hashtag_allows_unicode_letters_and_underscore():
    cfg = parse_config({"discovery": {"sources": [{"type": "hashtag", "value": "#café_1"}]}})
    assert cfg.discovery.sources[0].value == "café_1"


def test_config_mode_rules():
    ok = parse_config({"discovery": {"sources": [{"type": "hashtag", "value": "a", "mode": "RECENT"}]}})
    assert ok.discovery.sources[0].mode == "recent"
    with pytest.raises(ValueError, match="mode must be one of"):
        parse_config({"discovery": {"sources": [{"type": "hashtag", "value": "a", "mode": "hot"}]}})
    for t in ("user", "collection"):
        with pytest.raises(ValueError, match="mode only applies to hashtag"):
            parse_config({"discovery": {"sources": [{"type": t, "value": "a", "mode": "top"}]}})


def test_config_to_must_be_string():
    with pytest.raises(ValueError, match="to must be"):
        parse_config({"discovery": {"sources": [{"type": "user", "value": "a", "to": 5}]}})


def test_config_duplicate_sources_rejected():
    with pytest.raises(ValueError, match="duplicate source"):
        parse_config({"discovery": {"sources": [
            {"type": "hashtag", "value": "#funny"}, {"type": "hashtag", "value": "Funny", "mode": "recent"}]}})
    # same value, different type is fine
    cfg = parse_config({"discovery": {"sources": [
        {"type": "hashtag", "value": "funny"}, {"type": "user", "value": "funny"}]}})
    assert len(cfg.discovery.sources) == 2


# --- fetch_source ---------------------------------------------------------------

def test_fetch_hashtag_top_and_recent():
    c = FakeClient(hashtags={"funny": [reel("A")]})
    assert fetch_source(c, src(mode="top"), 7) == c.hashtags["funny"]
    fetch_source(c, src(mode="recent"), 4)
    assert c.calls == [("top", "funny", 7), ("recent", "funny", 4)]


def test_fetch_user_prefers_user_clips():
    c = FakeClient(users={"uid-bob": [reel("A")]})
    assert len(fetch_source(c, src("user", "bob"), 6)) == 1
    assert c.calls == [("uid", "bob"), ("clips", "uid-bob", 6)]


def test_fetch_user_falls_back_to_user_medias_overfetching():
    c = FakeClient(users={"uid-bob": [reel("A"), photo("B")]}, clips=False)
    assert not hasattr(c, "user_clips")
    assert len(fetch_source(c, src("user", "bob"), 6)) == 2
    assert c.calls == [("uid", "bob"), ("medias", "uid-bob", 18)]


def test_fetch_collection_uses_importer_lookup():
    c = FakeClient(collections={"Funny": [reel("A")]})
    assert fetch_source(c, src("collection", "funny"), 5) == c.collections_["Funny"]
    assert c.calls == [("collection", "id-Funny")]


def test_fetch_collection_unknown_name():
    c = FakeClient(collections={"Funny": []})
    with pytest.raises(ValueError, match="not found"):
        fetch_source(c, src("collection", "Nope"), 5)


def test_fetch_explore_reels_and_feed():
    c = FakeClient()
    c.explore_reels = lambda amount: (c.calls.append(("explore_reels", amount)), [reel("A")])[1]
    c.reels = lambda amount: (c.calls.append(("reels", amount)), [reel("B"), reel("C")])[1]
    assert len(fetch_source(c, src("explore", "reels"), 6)) == 1
    assert len(fetch_source(c, src("explore", "feed"), 4)) == 2
    assert c.calls == [("explore_reels", 6), ("reels", 4)]


def test_refill_from_explore_adds_only_reels(temp_db):
    c = FakeClient()
    c.explore_reels = lambda amount: [reel("EXP1"), photo("PHOTO"), reel("EXP1"), reel("EXP2")]
    result = discovery.refill(disc(src("explore", "reels")), client=c)
    assert len(result.added) == 2 and result.skipped == 1 and result.duplicates == 1
    assert result.per_source == {"explore:reels": 2}
    assert len(queued()) == 2


def test_explore_block_raises_blocked_error(temp_db):
    c = FakeClient()

    def boom(amount):
        raise ChallengeRequired("challenge")

    c.explore_reels = boom
    with pytest.raises(BlockedError):
        discovery.refill(disc(src("explore", "reels")), client=c)


def test_config_explore_source_defaults_and_values():
    cfg = parse_config({"discovery": {"sources": [
        {"type": "explore"}, {"type": "Explore", "value": " FEED "}]}})
    assert cfg.discovery.sources == [
        DiscoverySource("explore", "reels", "top", None),
        DiscoverySource("explore", "feed", "top", None),
    ]


@pytest.mark.parametrize("bad", ["funny", "", 3])
def test_config_explore_value_validated(bad):
    with pytest.raises(ValueError, match="explore value must be one of|non-empty string"):
        parse_config({"discovery": {"sources": [{"type": "explore", "value": bad}]}})


def test_config_explore_rejects_mode_and_duplicates():
    with pytest.raises(ValueError, match="mode only applies to hashtag"):
        parse_config({"discovery": {"sources": [{"type": "explore", "mode": "top"}]}})
    with pytest.raises(ValueError, match="duplicate source explore:reels"):
        parse_config({"discovery": {"sources": [{"type": "explore"},
                                                {"type": "explore", "value": "reels"}]}})


def test_fetch_source_none_result_is_empty_list():
    c = FakeClient()
    c.hashtag_medias_top = lambda name, amount: None
    assert fetch_source(c, src(), 3) == []


def test_fetch_unknown_type():
    with pytest.raises(ValueError, match="Unknown discovery source type"):
        fetch_source(FakeClient(), src("bogus", "x"), 3)


# --- filtering ------------------------------------------------------------------

def test_reel_filtering(temp_db):
    c = FakeClient(hashtags={"funny": [
        reel("AAA"), photo("BBB"), album("CCC"),
        Obj(code="DDD", media_type=2),                                  # no product_type: accepted
        Obj(code="EEE", media_type=2, product_type="feed"),             # regular video post: no
        Obj(code="", media_type=2, product_type="clips"),              # no code: skipped
    ]})
    res = refill(disc(src()), c)
    assert res.added == [url("AAA"), url("DDD")]
    assert res.skipped == 4
    assert queued() == [url("AAA"), url("DDD")]


def test_min_likes_filter(temp_db):
    c = FakeClient(hashtags={"funny": [
        reel("AAA", like_count=500), reel("BBB", like_count=99), reel("CCC"),
        reel("DDD", like_count=100)]})
    res = refill(disc(src(), min_likes=100), c)
    assert res.added == [url("AAA"), url("DDD")]
    assert res.skipped == 2


def test_missing_like_count_ok_when_min_likes_zero(temp_db):
    c = FakeClient(hashtags={"funny": [reel("AAA")]})
    assert refill(disc(src(), min_likes=0), c).added == [url("AAA")]


def test_dict_medias_supported(temp_db):
    c = FakeClient(hashtags={"funny": [{"code": "AAA", "media_type": 2, "product_type": "clips"}]})
    assert refill(disc(src()), c).added == [url("AAA")]


# --- refill ---------------------------------------------------------------------

def three_source_client(n=10):
    return FakeClient(
        hashtags={"a": [reel(f"A{i}") for i in range(n)], "b": [reel(f"B{i}") for i in range(n)]},
        users={"uid-c": [reel(f"C{i}") for i in range(n)]},
    )


SOURCES3 = (src("hashtag", "a"), src("hashtag", "b"), src("user", "c"))


def test_round_robin_fairness(temp_db):
    res = refill(disc(*SOURCES3, max_per_refill=3), three_source_client())
    assert sorted(u.split("/")[-2][0] for u in res.added) == ["A", "B", "C"]
    assert res.per_source == {"hashtag:a": 1, "hashtag:b": 1, "user:c": 1}


def test_round_robin_uneven_pools(temp_db):
    c = FakeClient(hashtags={"a": [reel("A0"), reel("A1"), reel("A2"), reel("A3")], "b": [reel("B0")]})
    res = refill(disc(src("hashtag", "a"), src("hashtag", "b"), max_per_refill=4), c)
    assert len(res.added) == 4
    assert res.per_source == {"hashtag:a": 3, "hashtag:b": 1}


def test_fetch_per_source_bounds_each_source(temp_db):
    res = refill(disc(*SOURCES3, fetch_per_source=2), three_source_client())
    assert len(res.added) == 6
    assert res.per_source == {"hashtag:a": 2, "hashtag:b": 2, "user:c": 2}


def test_source_order_is_shuffled(temp_db, monkeypatch):
    seen = []
    monkeypatch.setattr(discovery.random, "shuffle", lambda lst: (seen.append(list(lst)), lst.reverse()))
    c = three_source_client(1)
    res = refill(disc(*SOURCES3, max_per_refill=1), c)
    assert seen and res.added == [url("C0")]      # reversed order: user:c goes first


def test_max_per_refill_cap(temp_db):
    res = refill(disc(*SOURCES3, max_per_refill=5), three_source_client())
    assert len(res.added) == 5 and len(queue.list_reels()) == 5


def test_limit_lower_than_cap_and_bounded_by_cap(temp_db):
    d = disc(*SOURCES3, max_per_refill=5)
    assert len(refill(d, three_source_client(), limit=2).added) == 2
    # limit above max_per_refill is bounded by it; duplicates don't use up the cap
    res = refill(d, three_source_client(), limit=99)
    assert len(res.added) == 5 and res.duplicates >= 2
    assert len(queue.list_reels()) == 7


@pytest.mark.parametrize("bad", [0, -1, True, "3"])
def test_bad_limit_rejected(temp_db, bad):
    with pytest.raises(ValueError, match="limit"):
        refill(disc(src()), FakeClient(), limit=bad)


def test_dry_run_adds_nothing(temp_db):
    res = refill(disc(*SOURCES3, max_per_refill=4), three_source_client(), dry_run=True)
    assert res.dry_run is True and len(res.added) == 4
    assert queue.list_reels() == []


def test_dry_run_counts_duplicates_and_cross_source_repeats(temp_db):
    queue.add_reel(url("AAA"))
    c = FakeClient(hashtags={"a": [reel("AAA"), reel("BBB")], "b": [reel("BBB"), reel("CCC")]})
    res = refill(disc(src("hashtag", "a"), src("hashtag", "b")), c, dry_run=True)
    assert sorted(res.added) == [url("BBB"), url("CCC")]
    assert res.duplicates == 2
    assert len(queue.list_reels()) == 1


def test_duplicates_counted_and_do_not_use_cap(temp_db):
    queue.add_reel(url("AAA"))
    c = FakeClient(hashtags={"funny": [reel("AAA"), reel("BBB"), reel("CCC")]})
    res = refill(disc(src(), max_per_refill=2), c)
    assert res.added == [url("BBB"), url("CCC")]
    assert res.duplicates == 1


def test_sent_reels_are_duplicates_too(temp_db):
    rid = queue.add_reel(url("AAA"))
    queue.mark_sent(rid, "pal", "r", "dry_run")
    res = refill(disc(src()), FakeClient(hashtags={"funny": [reel("AAA")]}))
    assert res.added == [] and res.duplicates == 1


def test_per_source_recipient_assigned(temp_db):
    c = FakeClient(hashtags={"a": [reel("AAA")], "b": [reel("BBB")]})
    refill(disc(src("hashtag", "a", to="pal"), src("hashtag", "b")), c)
    rows = {r["shortcode"]: r["recipient"] for r in queue.list_reels()}
    assert rows == {"AAA": "pal", "BBB": None}
    assert all(r["note"] == "" for r in queue.list_reels())


def test_one_failing_source_does_not_stop_others(temp_db, monkeypatch):
    monkeypatch.setenv("IG_PASSWORD", "hunter2-password")
    c = FakeClient(hashtags={"bad": RuntimeError("boom hunter2-password"), "good": [reel("AAA")]})
    res = refill(disc(src("hashtag", "bad"), src("hashtag", "good")), c)
    assert res.added == [url("AAA")]
    assert len(res.errors) == 1
    assert "hashtag:bad" in res.errors[0] and "RuntimeError" in res.errors[0]
    assert "hunter2" not in res.errors[0]
    assert res.per_source == {"hashtag:bad": 0, "hashtag:good": 1}


def test_unknown_collection_is_a_source_error_not_a_crash(temp_db):
    c = FakeClient(collections={"Funny": []}, hashtags={"a": [reel("AAA")]})
    res = refill(disc(src("collection", "Nope"), src("hashtag", "a")), c)
    assert res.added == [url("AAA")] and "not found" in res.errors[0]


@pytest.mark.parametrize("exc", [ChallengeRequired("c"), PleaseWaitFewMinutes("p"), SubChallenge("s")])
def test_block_raises_blocked_error(temp_db, exc):
    c = FakeClient(hashtags={"blocked": exc, "good": [reel("AAA")]})
    with pytest.raises(BlockedError):
        refill(disc(src("hashtag", "blocked"), src("hashtag", "good")), c)
    assert queue.list_reels() == []       # all Instagram calls happen before any add


def test_blocked_error_from_client_passes_through(temp_db):
    c = FakeClient(hashtags={"a": BlockedError("x")})
    with pytest.raises(BlockedError):
        refill(disc(src("hashtag", "a")), c)


def test_sleeps_between_sources_only(temp_db, no_sleep):
    refill(disc(*SOURCES3, delay_seconds=7), three_source_client(1))
    assert no_sleep == [7, 7]
    no_sleep.clear()
    refill(disc(src("hashtag", "a"), delay_seconds=7), three_source_client(1))
    assert no_sleep == []
    refill(disc(*SOURCES3, delay_seconds=0), three_source_client(1))
    assert no_sleep == []


def test_no_sources_gives_empty_result(temp_db):
    res = refill(Discovery(enabled=True), FakeClient())
    assert res.added == [] and res.per_source == {}


def test_add_reel_unexpected_error_recorded(temp_db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(queue, "add_reel", boom)
    res = refill(disc(src()), FakeClient(hashtags={"funny": [reel("AAA")]}))
    assert res.added == [] and "db locked" in res.errors[0]


def test_add_reel_other_value_error_is_skipped(temp_db, monkeypatch):
    def bad(*a, **k):
        raise ValueError("Not an Instagram reel/post link")
    monkeypatch.setattr(queue, "add_reel", bad)
    res = refill(disc(src()), FakeClient(hashtags={"funny": [reel("AAA")]}))
    assert res.skipped == 1 and res.added == [] and res.duplicates == 0


# --- format_result ----------------------------------------------------------------

def test_format_result():
    r = RefillResult(added=["a", "b"], duplicates=1, skipped=2, errors=[f"e{i}" for i in range(7)],
                     per_source={"x:1": 2, "y:2": 0})
    lines = format_result(r).split("\n")
    assert lines[0] == "Added 2 reel(s) from 2 source(s); 1 already queued, 2 skipped, 7 error(s)."
    assert lines[1:] == [f"  error: e{i}" for i in range(5)]


def test_format_result_dry_run_no_errors():
    out = format_result(RefillResult(added=["a"], dry_run=True))
    assert out.startswith("Would add 1 reel(s)") and "\n" not in out


# --- request_refill / tick ---------------------------------------------------------

T0 = datetime(2026, 3, 1, 12, 0, 0)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def loader_for(discovery_cfg):
    return lambda: Config(discovery=discovery_cfg)


class Provider:
    def __init__(self, client):
        self.client = client
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.client


def tick_setup(**kw):
    kw.setdefault("min_pending", 5)
    kw.setdefault("check_every_minutes", 60)
    d = disc(src("hashtag", "a"), **kw)
    client = FakeClient(hashtags={"a": [reel(f"A{i}") for i in range(10)]})
    return d, Provider(client)


def test_request_refill_sets_flag(temp_db):
    request_refill()
    assert settings.get(SETTING_REQUESTED) == "1"


def test_tick_runs_when_never_ran(temp_db):
    d, prov = tick_setup()
    clock = Clock()
    res = tick(prov, loader=loader_for(d), now_fn=clock)
    assert len(res.added) == 10
    assert settings.get(SETTING_LAST_RUN) == "2026-03-01 12:00:00"
    assert settings.get(SETTING_LAST_SUMMARY) == format_result(res)
    assert prov.calls == 1


def test_tick_default_clock_stamps_db_now(temp_db):
    d, prov = tick_setup()
    assert tick(prov, loader=loader_for(d)) is not None
    stamp = settings.get(SETTING_LAST_RUN)
    assert datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")      # parseable
    # and the throttle works against it
    queue_rows = queue.list_reels()
    for r in queue_rows[:8]:
        queue.delete_reel(r["id"])
    assert tick(prov, loader=loader_for(d)) is None


def test_tick_throttled_after_recent_run_then_due(temp_db):
    d, prov = tick_setup(max_per_refill=1)
    clock = Clock()
    assert tick(prov, loader=loader_for(d), now_fn=clock) is not None
    clock.advance(minutes=59)
    assert tick(prov, loader=loader_for(d), now_fn=clock) is None
    assert prov.calls == 1
    clock.advance(minutes=1)
    assert tick(prov, loader=loader_for(d), now_fn=clock) is not None
    assert prov.calls == 2


def test_tick_manual_request_overrides_throttle_and_pending(temp_db):
    d, prov = tick_setup(max_per_refill=1, min_pending=1)
    clock = Clock()
    assert tick(prov, loader=loader_for(d), now_fn=clock) is not None       # ran now
    assert tick(prov, loader=loader_for(d), now_fn=clock) is None           # pending>=1 + throttled
    request_refill()
    res = tick(prov, loader=loader_for(d), now_fn=clock)
    assert res is not None and len(res.added) == 1
    assert settings.get(SETTING_REQUESTED) == "0"
    assert tick(prov, loader=loader_for(d), now_fn=clock) is None           # flag cleared


def test_tick_skips_when_enough_pending(temp_db):
    for i in range(5):
        queue.add_reel(url(f"X{i}"))
    d, prov = tick_setup(min_pending=5)
    assert tick(prov, loader=loader_for(d), now_fn=Clock()) is None
    assert prov.calls == 0
    assert settings.get(SETTING_LAST_RUN) is None


def test_tick_only_counts_pending(temp_db):
    for i in range(5):
        queue.mark_sent(queue.add_reel(url(f"X{i}")), "pal", "r", "dry_run")
    d, prov = tick_setup(min_pending=5)
    assert tick(prov, loader=loader_for(d), now_fn=Clock()) is not None


def test_tick_disabled_skips_and_clears_request(temp_db):
    d, prov = tick_setup()
    d.enabled = False
    request_refill()
    assert tick(prov, loader=loader_for(d), now_fn=Clock()) is None
    assert prov.calls == 0 and queue.list_reels() == []
    assert settings.get(SETTING_REQUESTED) == "0"


def test_tick_paused_skips_and_keeps_request(temp_db):
    d, prov = tick_setup()
    settings.set_paused(True)
    request_refill()
    assert tick(prov, loader=loader_for(d), now_fn=Clock()) is None
    assert prov.calls == 0
    assert settings.get(SETTING_REQUESTED) == "1"


def test_tick_config_error_returns_none(temp_db):
    def bad():
        raise ValueError("bad yaml")
    prov = Provider(FakeClient())
    assert tick(prov, loader=bad, now_fn=Clock()) is None
    assert prov.calls == 0


def test_tick_block_pauses_notifies_and_clears_flag(temp_db, alerts):
    c = FakeClient(hashtags={"a": ChallengeRequired("challenge please")})
    d = disc(src("hashtag", "a"))
    request_refill()
    assert tick(Provider(c), loader=loader_for(d), now_fn=Clock()) is None
    assert settings.is_paused() is True
    assert settings.get(SETTING_REQUESTED) == "0"
    assert settings.get(SETTING_LAST_SUMMARY) == "BLOCKED"
    assert len(alerts) == 1
    assert alerts[0].startswith("⛔ ReelDrop PAUSED") and "discovery" in alerts[0]
    assert "ChallengeRequired" in alerts[0]


def test_tick_block_from_client_provider(temp_db, alerts):
    def provider():
        raise BlockedError("LoginRequired: nope")
    d = disc(src("hashtag", "a"))
    assert tick(provider, loader=loader_for(d), now_fn=Clock()) is None
    assert settings.is_paused() is True and "LoginRequired" in alerts[0]


def test_tick_error_sets_last_run_and_does_not_raise(temp_db, alerts, monkeypatch):
    monkeypatch.setenv("IG_PASSWORD", "hunter2-password")

    def provider():
        raise RuntimeError("Missing environment variable IG_USERNAME hunter2-password")
    d = disc(src("hashtag", "a"))
    request_refill()
    assert tick(provider, loader=loader_for(d), now_fn=Clock()) is None
    assert settings.is_paused() is False and alerts == []
    assert settings.get(SETTING_LAST_RUN) == "2026-03-01 12:00:00"
    summary = settings.get(SETTING_LAST_SUMMARY)
    assert summary.startswith("error: RuntimeError") and "hunter2" not in summary
    assert settings.get(SETTING_REQUESTED) == "0"
    # throttled afterwards: a broken setup is not retried every minute
    calls = []
    assert tick(lambda: calls.append(1), loader=loader_for(d), now_fn=Clock()) is None
    assert calls == []


def test_tick_never_raises_even_if_db_fails(temp_db, monkeypatch):
    monkeypatch.setattr(queue, "list_reels", lambda *a: (_ for _ in ()).throw(RuntimeError("db")))
    d, prov = tick_setup()
    assert tick(prov, loader=loader_for(d), now_fn=Clock()) is None


def test_tick_source_errors_are_in_summary(temp_db):
    c = FakeClient(hashtags={"a": RuntimeError("boom"), "b": [reel("AAA")]})
    d = disc(src("hashtag", "a"), src("hashtag", "b"))
    res = tick(Provider(c), loader=loader_for(d), now_fn=Clock())
    assert len(res.added) == 1
    assert "1 error(s)" in settings.get(SETTING_LAST_SUMMARY)


# --- top_up --------------------------------------------------------------------------

def test_top_up_adds_up_to_needed(temp_db):
    d, prov = tick_setup()
    assert top_up(3, prov, loader=loader_for(d)) == 3
    assert len(queue.list_reels()) == 3
    assert settings.get(SETTING_LAST_RUN) is not None
    assert settings.get(SETTING_LAST_SUMMARY).startswith("Added 3 reel(s)")


def test_top_up_bounded_by_max_per_refill_and_min_one(temp_db):
    d, prov = tick_setup(max_per_refill=4)
    assert top_up(50, prov, loader=loader_for(d)) == 4
    assert top_up(0, prov, loader=loader_for(d)) == 1
    assert top_up(-5, prov, loader=loader_for(d)) == 1


def test_top_up_disabled_or_paused_does_nothing(temp_db):
    d, prov = tick_setup()
    d.enabled = False
    assert top_up(3, prov, loader=loader_for(d)) == 0
    d.enabled = True
    settings.set_paused(True)
    assert top_up(3, prov, loader=loader_for(d)) == 0
    assert prov.calls == 0 and queue.list_reels() == []


def test_top_up_ignores_throttle(temp_db):
    d, prov = tick_setup()
    clock = Clock()
    tick(prov, loader=loader_for(d), now_fn=clock)
    assert top_up(2, prov, loader=loader_for(d)) >= 0     # runs even though throttled
    assert prov.calls == 2


def test_top_up_block_pauses_and_returns_zero(temp_db, alerts):
    c = FakeClient(hashtags={"a": PleaseWaitFewMinutes("wait")})
    d = disc(src("hashtag", "a"))
    assert top_up(3, Provider(c), loader=loader_for(d)) == 0
    assert settings.is_paused() is True and len(alerts) == 1
    assert settings.get(SETTING_LAST_SUMMARY) == "BLOCKED"


def test_top_up_error_returns_zero_and_records(temp_db):
    def provider():
        raise RuntimeError("no creds")
    d = disc(src("hashtag", "a"))
    assert top_up(3, provider, loader=loader_for(d)) == 0
    assert settings.get(SETTING_LAST_SUMMARY).startswith("error: RuntimeError")
    assert settings.get(SETTING_LAST_RUN) is not None


def test_top_up_config_error_returns_zero(temp_db):
    def bad():
        raise ValueError("x")
    assert top_up(3, Provider(FakeClient()), loader=bad) == 0


def test_tick_and_top_up_never_run_concurrently(temp_db):
    d = disc(src("hashtag", "a"))
    inside, overlap, started = [], [], threading.Event()
    release = threading.Event()

    class Slow(FakeClient):
        def hashtag_medias_top(self, name, amount):
            inside.append(1)
            if len(inside) > 1:
                overlap.append(1)
            started.set()
            release.wait(5)
            inside.pop()
            return [reel("S1")]

    prov = Provider(Slow())
    t1 = threading.Thread(target=lambda: tick(prov, loader=loader_for(d), now_fn=Clock()))
    t1.start()
    assert started.wait(5)
    t2 = threading.Thread(target=lambda: top_up(1, prov, loader=loader_for(d)))
    t2.start()
    t2.join(0.3)
    assert t2.is_alive()          # waiting for the lock while tick refills
    release.set()
    t1.join(5)
    t2.join(5)
    assert not overlap and not t1.is_alive() and not t2.is_alive()
    assert prov.calls == 2


# --- client provider -------------------------------------------------------------------

def test_provider_reuses_sender_client():
    sender = types.SimpleNamespace(cl=object())
    get = make_client_provider(sender)
    assert get() is sender.cl and get() is sender.cl


def install_fake_instagrapi(monkeypatch, login_error=None):
    exc_mod = types.ModuleType("instagrapi.exceptions")
    for n in ("ChallengeRequired", "FeedbackRequired", "LoginRequired", "PleaseWaitFewMinutes"):
        setattr(exc_mod, n, type(n, (Exception,), {}))

    class Client:
        instances = []

        def __init__(self):
            self.calls = []
            Client.instances.append(self)

        def load_settings(self, path):
            pass

        def login(self, user, password):
            self.calls.append(("login", user, password))
            if login_error:
                raise getattr(exc_mod, login_error)("login blocked")

        def dump_settings(self, path):
            pass

    pkg = types.ModuleType("instagrapi")
    pkg.__path__ = []
    pkg.Client = Client
    pkg.exceptions = exc_mod
    monkeypatch.setitem(sys.modules, "instagrapi", pkg)
    monkeypatch.setitem(sys.modules, "instagrapi.exceptions", exc_mod)
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


def patch_session(monkeypatch, tmp_path):
    from reeldrop.senders import instagrapi_sender
    monkeypatch.setattr(instagrapi_sender, "SESSION", tmp_path / "data" / "session.json")


@pytest.mark.parametrize("sender", [None, types.SimpleNamespace(name="telegram")])
def test_provider_builds_lazily_and_caches(monkeypatch, tmp_path, lazy_env, sender):
    Client = install_fake_instagrapi(monkeypatch)
    patch_session(monkeypatch, tmp_path)
    get = make_client_provider(sender)
    assert Client.instances == []                       # nothing built at creation
    first = get()
    assert get() is first and len(Client.instances) == 1
    assert ("login", "me", "hunter2-password") in first.calls


def test_provider_thread_safe_single_build(monkeypatch, tmp_path, lazy_env):
    Client = install_fake_instagrapi(monkeypatch)
    patch_session(monkeypatch, tmp_path)
    get = make_client_provider(None)
    results = []
    threads = [threading.Thread(target=lambda: results.append(get())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(Client.instances) == 1
    assert len(results) == 8 and all(r is results[0] for r in results)


def test_provider_login_block_becomes_blocked_error(monkeypatch, tmp_path, lazy_env):
    install_fake_instagrapi(monkeypatch, login_error="ChallengeRequired")
    patch_session(monkeypatch, tmp_path)
    get = make_client_provider(None)
    with pytest.raises(BlockedError, match="ChallengeRequired") as exc:
        get()
    assert "hunter2" not in str(exc.value)


def test_provider_missing_credentials_not_cached(monkeypatch, tmp_path, lazy_env):
    Client = install_fake_instagrapi(monkeypatch)
    patch_session(monkeypatch, tmp_path)
    monkeypatch.delenv("IG_USERNAME")
    get = make_client_provider(None)
    with pytest.raises(RuntimeError, match="IG_USERNAME"):
        get()
    monkeypatch.setenv("IG_USERNAME", "me")
    assert get() is Client.instances[-1]                # a later call can still succeed
