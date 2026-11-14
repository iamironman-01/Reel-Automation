"""Phase 7 extensions: captions, random pick, quiet days, live reload, stats."""
import datetime
import importlib
import json
import logging
import os
import sys
import types

import pytest

from reeldrop import db, job, settings
from reeldrop.config import Config, Rule, parse_config
from reeldrop.dashboard_helpers import daily_counts, failure_rate, weekly_counts
from reeldrop.queue import add_reel, list_reels, mark_failed, mark_sent, next_pending
from reeldrop.senders import BlockedError, get_sender
from reeldrop.senders.base import Sender

TOKEN = "123456:SECRET-TOKEN"


def rule_dict(**extra):
    return {"name": "r", "time": "20:00", "count": 1, "recipient": "friend", **extra}


# --- config: send_note / pick / quiet_days -----------------------------------

def test_new_fields_default_off():
    cfg = parse_config({"rules": [rule_dict()]})
    assert cfg.rules[0].send_note is False
    assert cfg.rules[0].pick == "oldest"
    assert cfg.quiet_days == []


def test_new_fields_parsed():
    cfg = parse_config({"rules": [rule_dict(send_note=True, pick="random")]})
    assert cfg.rules[0].send_note is True and cfg.rules[0].pick == "random"


@pytest.mark.parametrize("bad", ["yes", "true", 1, 0, [True]])
def test_send_note_must_be_bool(bad):
    with pytest.raises(ValueError, match="send_note"):
        parse_config({"rules": [rule_dict(send_note=bad)]})


@pytest.mark.parametrize("bad", ["newest", "", 3, True, ["random"]])
def test_pick_must_be_oldest_or_random(bad):
    with pytest.raises(ValueError, match="pick"):
        parse_config({"rules": [rule_dict(pick=bad)]})


def test_pick_is_case_insensitive():
    assert parse_config({"rules": [rule_dict(pick=" Random ")]}).rules[0].pick == "random"


def test_quiet_days_strings_and_date_objects_normalized():
    cfg = parse_config({"quiet_days": ["2026-12-25", datetime.date(2026, 1, 1), "2026-12-25"]})
    assert cfg.quiet_days == ["2026-01-01", "2026-12-25"]


@pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-30", "25/12/2026", "20261225", "tomorrow",
                                 12, None, datetime.datetime(2026, 12, 25, 10, 0)])
def test_quiet_days_rejects_unparseable(bad):
    with pytest.raises(ValueError, match="quiet_days"):
        parse_config({"quiet_days": [bad]})


def test_quiet_days_must_be_list():
    with pytest.raises(ValueError, match="quiet_days"):
        parse_config({"quiet_days": "2026-12-25"})


def test_reload_job_id_is_reserved_rule_name():
    with pytest.raises(ValueError, match="reserved"):
        parse_config({"rules": [rule_dict(name="__reload__")]})


def test_shipped_schedule_only_has_commented_examples():
    with open("config/schedules.yaml", encoding="utf-8") as f:
        lines = f.read().splitlines()
    for key in ("send_note", "pick", "quiet_days"):
        mentions = [ln for ln in lines if key in ln]
        assert mentions and all(ln.lstrip().startswith("#") for ln in mentions)


def test_shipped_schedule_unchanged_defaults():
    pytest.importorskip("yaml")
    from reeldrop.config import load_config
    cfg = load_config("config/schedules.yaml")
    assert cfg.quiet_days == []
    assert all(r.send_note is False and r.pick == "oldest" for r in cfg.rules)


# --- queue.next_pending ------------------------------------------------------

def _fill(n):
    return [add_reel(f"https://www.instagram.com/reel/R{i:03d}/") for i in range(n)]


def test_next_pending_default_is_oldest(temp_db):
    ids = _fill(5)
    assert [r["id"] for r in next_pending(2)] == ids[:2]
    assert [r["id"] for r in next_pending(2, "oldest")] == ids[:2]
    assert [r["id"] for r in next_pending(2, order="oldest")] == ids[:2]


def test_next_pending_random_only_pending_correct_count_sorted(temp_db):
    ids = _fill(20)
    for i in ids[:5]:
        mark_sent(i, "f", "r", "s")
    mark_failed(ids[5], "f", "r", "s", "x")
    pending = set(ids[6:])
    seen = set()
    for _ in range(30):
        picked = [r["id"] for r in next_pending(4, order="random")]
        assert len(picked) == 4 and len(set(picked)) == 4
        assert set(picked) <= pending
        assert picked == sorted(picked)
        seen.update(picked)
    assert len(seen) > 4          # it really varies (chance of a false failure is negligible)


def test_next_pending_random_fewer_than_n_and_zero(temp_db):
    ids = _fill(3)
    assert sorted(r["id"] for r in next_pending(10, order="random")) == ids
    assert next_pending(0, order="random") == []


def test_next_pending_rejects_unknown_order(temp_db):
    with pytest.raises(ValueError, match="order"):
        next_pending(1, order="newest")


# --- job: notes, pick, quiet days -------------------------------------------

class NoteSender:
    name = "fake"

    def __init__(self, text_outcomes=None, send_outcomes=None):
        self.events = []
        self.text_outcomes = list(text_outcomes or [])
        self.send_outcomes = list(send_outcomes or [])

    def send_text(self, recipient, text):
        self.events.append(("note", recipient, text))
        if self.text_outcomes:
            outcome = self.text_outcomes.pop(0)
            if outcome is not None:
                raise outcome

    def send(self, recipient, url):
        self.events.append(("reel", recipient, url))
        if self.send_outcomes:
            outcome = self.send_outcomes.pop(0)
            if outcome is not None:
                raise outcome


@pytest.fixture
def env(temp_db, monkeypatch):
    alerts, sleeps = [], []
    monkeypatch.setattr(job, "notify_me", alerts.append)
    monkeypatch.setattr(job.time, "sleep", sleeps.append)
    monkeypatch.setattr(job, "RETRY_WAIT_SECONDS", 7)
    return types.SimpleNamespace(alerts=alerts, sleeps=sleeps)


def rule(**kw):
    base = dict(name="rule", days="*", time="20:00", count=2, recipient="friend")
    base.update(kw)
    return Rule(**base)


def cfg(**kw):
    return Config(daily_cap=kw.pop("daily_cap", 10), gap_seconds=kw.pop("gap_seconds", 5), **kw)


def statuses():
    return [r["status"] for r in list_reels()]


def test_note_sent_before_reel_when_enabled(env):
    add_reel("https://www.instagram.com/reel/AAA/", "watch this")
    add_reel("https://www.instagram.com/reel/BBB/", "")          # no note: only the reel
    add_reel("https://www.instagram.com/reel/CCC/", "   ")       # blank note: only the reel
    s = NoteSender()
    job.run_rule(rule(count=3, send_note=True), cfg(), s)
    assert s.events == [
        ("note", "friend", "watch this"),
        ("reel", "friend", "https://www.instagram.com/reel/AAA/"),
        ("reel", "friend", "https://www.instagram.com/reel/BBB/"),
        ("reel", "friend", "https://www.instagram.com/reel/CCC/"),
    ]
    assert statuses() == ["sent"] * 3


def test_note_not_sent_when_disabled(env):
    add_reel("https://www.instagram.com/reel/AAA/", "watch this")
    s = NoteSender()
    job.run_rule(rule(count=1), cfg(), s)
    assert s.events == [("reel", "friend", "https://www.instagram.com/reel/AAA/")]


def test_notes_do_not_count_towards_daily_cap(env):
    for i in range(3):
        add_reel(f"https://www.instagram.com/reel/N{i}/", "hi")
    s = NoteSender()
    job.run_rule(rule(count=5, send_note=True), cfg(daily_cap=2), s)
    assert [e[0] for e in s.events] == ["note", "reel", "note", "reel"]
    assert statuses() == ["sent", "sent", "pending"]


def test_note_failure_retried_then_marks_reel_failed_and_skips_reel(env):
    add_reel("https://www.instagram.com/reel/AAA/", "hello")
    add_reel("https://www.instagram.com/reel/BBB/", "second")
    s = NoteSender(text_outcomes=[RuntimeError("boom"), RuntimeError("boom again")])
    job.run_rule(rule(send_note=True), cfg(), s)
    assert env.sleeps[0] == 7                                   # one retry wait
    assert s.events[:2] == [("note", "friend", "hello")] * 2
    assert ("reel", "friend", "https://www.instagram.com/reel/AAA/") not in s.events
    assert statuses() == ["failed", "sent"]                      # next reel carries on
    with db.connect() as c:
        err = c.execute("SELECT error FROM send_log WHERE status='failed'").fetchone()[0]
    assert "note not sent" in err and "boom again" in err
    assert not settings.is_paused()


def test_note_transient_failure_succeeds_on_retry(env):
    add_reel("https://www.instagram.com/reel/AAA/", "hello")
    s = NoteSender(text_outcomes=[RuntimeError("blip")])
    job.run_rule(rule(count=1, send_note=True), cfg(), s)
    assert [e[0] for e in s.events] == ["note", "note", "reel"]
    assert statuses() == ["sent"]


def test_blocked_during_note_pauses_like_a_reel_block(env):
    for i in range(3):
        add_reel(f"https://www.instagram.com/reel/B{i}/", "hi")
    s = NoteSender(text_outcomes=[BlockedError("challenge")])
    job.run_rule(rule(count=3, send_note=True), cfg(), s)
    assert s.events == [("note", "friend", "hi")]                # no retry, no reel
    assert settings.is_paused()
    assert statuses() == ["failed", "pending", "pending"]
    assert len(env.alerts) == 1 and "PAUSED" in env.alerts[0]
    assert env.sleeps == []
    with db.connect() as c:
        err = c.execute("SELECT error FROM send_log").fetchone()[0]
    assert err.startswith("BLOCKED:")


def test_sender_without_send_text_fails_reel_cleanly(env):
    add_reel("https://www.instagram.com/reel/AAA/", "hello")

    class Old(Sender):
        name = "old"

        def send(self, recipient, url):
            raise AssertionError("reel must not be sent")

    job.run_rule(rule(count=1, send_note=True), cfg(), Old())
    assert statuses() == ["failed"]


def test_random_pick_passed_to_queue(env, monkeypatch):
    seen = []
    monkeypatch.setattr(job, "next_pending", lambda n, order="oldest", recipient=None: seen.append((n, order)) or [])
    job.run_rule(rule(pick="random", count=3), cfg(), NoteSender())
    job.run_rule(rule(count=3), cfg(), NoteSender())
    assert seen == [(3, "random"), (3, "oldest")]


def test_random_pick_sends_only_pending_in_id_order(env):
    ids = [add_reel(f"https://www.instagram.com/reel/P{i:02d}/") for i in range(12)]
    mark_sent(ids[0], "f", "r", "s")
    s = NoteSender()
    job.run_rule(rule(pick="random", count=4), cfg(gap_seconds=0), s)
    urls = [e[2] for e in s.events]
    assert len(urls) == 4 and urls == sorted(urls)
    assert "https://www.instagram.com/reel/P00/" not in urls     # was not pending


def test_quiet_day_skips_without_notification(env, monkeypatch):
    add_reel("https://www.instagram.com/reel/AAA/", "n")
    monkeypatch.setattr(job, "_today", lambda c: "2026-12-25")
    s = NoteSender()
    job.run_rule(rule(send_note=True), cfg(quiet_days=["2026-12-25"]), s)
    assert s.events == [] and env.alerts == []
    assert statuses() == ["pending"]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM send_log").fetchone()[0] == 0
    # the next day sends normally
    monkeypatch.setattr(job, "_today", lambda c: "2026-12-26")
    job.run_rule(rule(count=1), cfg(quiet_days=["2026-12-25"]), s)
    assert statuses() == ["sent"]


def test_quiet_day_logs_info(env, monkeypatch, caplog):
    monkeypatch.setattr(job, "_today", lambda c: "2026-12-25")
    with caplog.at_level(logging.INFO, logger="reeldrop"):
        job.run_rule(rule(), cfg(quiet_days=["2026-12-25"]), NoteSender())
    assert any("quiet day" in r.getMessage() for r in caplog.records)


def test_today_uses_config_timezone():
    # 12 hours apart, so the two zones can never agree on the date for long
    a = job._today(Config(timezone="Pacific/Kiritimati"))
    b = job._today(Config(timezone="Pacific/Pago_Pago"))
    assert a != b
    datetime.date.fromisoformat(a)


# --- live reload (main.py) ---------------------------------------------------

class FakeCronTrigger:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeSched:
    """Records add_job / remove_job / get_jobs like APScheduler 3.x."""

    def __init__(self, timezone=None):
        self.timezone = timezone
        self.jobs = {}
        self.removed = []

    def add_job(self, func, trigger, **kw):
        if kw["id"] in self.jobs and not kw.get("replace_existing"):
            raise ValueError("job exists")
        self.jobs[kw["id"]] = types.SimpleNamespace(id=kw["id"], func=func, trigger=trigger, kwargs=kw)

    def remove_job(self, job_id):
        self.removed.append(job_id)
        del self.jobs[job_id]

    def get_jobs(self):
        return list(self.jobs.values())


@pytest.fixture
def main_mod(monkeypatch, tmp_path):
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: None
    schedulers = types.ModuleType("apscheduler.schedulers")
    blocking = types.ModuleType("apscheduler.schedulers.blocking")
    blocking.BlockingScheduler = FakeSched
    cron = types.ModuleType("apscheduler.triggers.cron")
    cron.CronTrigger = FakeCronTrigger
    for name, mod in {"dotenv": dotenv, "apscheduler": types.ModuleType("apscheduler"),
                      "apscheduler.schedulers": schedulers,
                      "apscheduler.schedulers.blocking": blocking,
                      "apscheduler.triggers": types.ModuleType("apscheduler.triggers"),
                      "apscheduler.triggers.cron": cron}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "main", raising=False)
    monkeypatch.chdir(tmp_path)
    mod = importlib.import_module("main")
    yield mod
    sys.modules.pop("main", None)


def json_loader(path):
    with open(path, encoding="utf-8") as f:
        return parse_config(json.load(f))


class Reloader:
    """Helper that owns a config file, a fake scheduler and the reload state."""

    def __init__(self, main_mod, tmp_path, data, monkeypatch):
        self.main = main_mod
        self.path = str(tmp_path / "schedules.json")
        self.alerts = []
        monkeypatch.setattr(main_mod, "notify_me", self.alerts.append)
        self.sender = object()
        self.write(data)
        cfg0 = json_loader(self.path)
        self.sched = FakeSched(timezone=cfg0.timezone)
        main_mod.register_jobs(self.sched, cfg0, self.sender)
        self.state = main_mod.ReloadState(cfg=cfg0, mtime=main_mod.file_mtime(self.path))
        self.cfg0 = cfg0

    def write(self, data, raw=None):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(data))
        # force a distinct mtime regardless of filesystem timestamp granularity
        ns = (getattr(self, "_t", 1_700_000_000_000_000_000)) + 10_000_000_000
        self._t = ns
        os.utime(self.path, ns=(ns, ns))

    def reload(self):
        return self.main.reload_if_changed(self.sched, self.state, self.path, self.sender,
                                           loader=json_loader)


def base_data(**over):
    d = {"timezone": "Asia/Kolkata", "daily_cap": 10, "gap_seconds": 45,
         "rules": [rule_dict(name="a", time="08:00"), rule_dict(name="b", time="09:30")]}
    d.update(over)
    return d


@pytest.fixture
def rl(main_mod, tmp_path, monkeypatch):
    return Reloader(main_mod, tmp_path, base_data(), monkeypatch)


def test_reload_unchanged_mtime_is_noop(rl):
    before = dict(rl.sched.jobs)
    assert rl.reload() == "unchanged"
    assert rl.sched.jobs == before and rl.sched.removed == [] and rl.alerts == []


def test_reload_valid_change_replaces_removes_and_adds(rl):
    old_job_a = rl.sched.jobs["a"]
    rl.write(base_data(daily_cap=3, gap_seconds=1, rules=[
        rule_dict(name="a", time="08:15", send_note=True),   # changed
        rule_dict(name="c", time="21:00", pick="random"),    # new; "b" removed
    ]))
    assert rl.reload() == "reloaded"
    assert set(rl.sched.jobs) == {"a", "c"}
    assert rl.sched.removed == ["b"]
    new_a = rl.sched.jobs["a"]
    assert new_a is not old_job_a
    assert new_a.trigger.kwargs["hour"] == 8 and new_a.trigger.kwargs["minute"] == 15
    rule_a, cfg_a, sender = new_a.kwargs["args"]
    assert rule_a.send_note is True and cfg_a.daily_cap == 3 and cfg_a.gap_seconds == 1
    assert sender is rl.sender
    assert rl.sched.jobs["c"].kwargs["args"][0].pick == "random"
    assert new_a.kwargs["replace_existing"] is True
    assert rl.state.cfg.daily_cap == 3
    assert rl.alerts == []
    # nothing further changes on the next tick
    assert rl.reload() == "unchanged"


def test_reload_cap_only_change_reaches_all_jobs(rl):
    rl.write(base_data(daily_cap=2))
    assert rl.reload() == "reloaded"
    assert all(j.kwargs["args"][1].daily_cap == 2 for j in rl.sched.jobs.values())


def test_reload_logs_what_changed(rl, caplog):
    rl.write(base_data(rules=[rule_dict(name="a", time="08:00"), rule_dict(name="z", time="10:00")]))
    with caplog.at_level(logging.INFO, logger="reeldrop"):
        rl.reload()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "added z" in text and "removed b" in text


def test_reload_invalid_keeps_jobs_and_notifies_once(rl, caplog):
    before = dict(rl.sched.jobs)
    old_mtime = rl.state.mtime
    rl.write(None, raw="{ this is not valid")
    with caplog.at_level(logging.ERROR, logger="reeldrop"):
        assert rl.reload() == "invalid"
    assert rl.sched.jobs == before and rl.sched.removed == []
    assert len(rl.alerts) == 1
    assert rl.state.mtime != old_mtime
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    # same broken file: silent no-op
    assert rl.reload() == "unchanged"
    assert len(rl.alerts) == 1
    # semantically invalid (bad rule) is also rejected, reported once
    rl.write(base_data(rules=[rule_dict(time="25:99")]))
    assert rl.reload() == "invalid"
    assert len(rl.alerts) == 2 and rl.sched.jobs == before
    assert rl.state.cfg is rl.cfg0
    # fixing the file recovers
    rl.write(base_data(daily_cap=4))
    assert rl.reload() == "reloaded"
    assert rl.sched.jobs["a"].kwargs["args"][1].daily_cap == 4


def test_reload_missing_file_keeps_jobs_and_notifies_once(rl):
    before = dict(rl.sched.jobs)
    os.unlink(rl.path)
    assert rl.reload() == "invalid"
    assert rl.reload() == "unchanged"
    assert rl.sched.jobs == before and len(rl.alerts) == 1


def test_reload_timezone_change_warns_and_keeps_scheduler_timezone(rl, caplog):
    rl.write(base_data(timezone="Europe/London", daily_cap=7))
    with caplog.at_level(logging.WARNING, logger="reeldrop"):
        assert rl.reload() == "reloaded"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("restart" in w and "Europe/London" in w for w in warnings)
    for j in rl.sched.jobs.values():
        assert j.trigger.kwargs["timezone"] == "Asia/Kolkata"
        assert j.kwargs["args"][1].timezone == "Asia/Kolkata"
        assert j.kwargs["args"][1].daily_cap == 7                 # other changes still applied
    assert rl.state.cfg.timezone == "Asia/Kolkata"


def test_reload_never_touches_reload_job(rl):
    rl.sched.add_job(rl.main.reload_if_changed, "interval", id="__reload__", replace_existing=True)
    rl.write(base_data(rules=[]))
    assert rl.reload() == "reloaded"
    assert set(rl.sched.jobs) == {"__reload__"}
    assert rl.sched.removed == ["a", "b"]


def test_reload_removed_rule_already_gone_does_not_crash(rl):
    del rl.sched.jobs["b"]                    # e.g. the job vanished on its own
    rl.write(base_data(rules=[rule_dict(name="a", time="08:00")]))
    assert rl.reload() == "reloaded"
    assert rl.sched.removed == []


def test_main_registers_reload_job(main_mod, monkeypatch):
    sched_holder = []
    monkeypatch.setattr(main_mod, "init_db", lambda: None)
    monkeypatch.setattr(main_mod, "setup_logging", lambda: None)
    monkeypatch.setattr(main_mod, "load_config", lambda *a: Config(rules=[Rule("a", "*", "20:00", 1, "f")]))
    monkeypatch.setattr(main_mod, "get_sender", lambda name: types.SimpleNamespace(name="fake"))
    orig = FakeSched.__init__

    def init(self, timezone=None):
        orig(self, timezone)
        sched_holder.append(self)

    monkeypatch.setattr(FakeSched, "start", lambda self: None, raising=False)
    monkeypatch.setattr(FakeSched, "__init__", init)
    main_mod.main()
    sched = sched_holder[0]
    assert set(sched.jobs) == {"a", "__reload__", "__discovery__", "__test_send__"}
    rj = sched.jobs["__reload__"]
    assert rj.func is main_mod.reload_if_changed and rj.trigger == "interval"
    assert rj.kwargs["seconds"] == 60 and rj.kwargs["replace_existing"] is True
    assert rj.kwargs["args"][0] is sched and rj.kwargs["args"][2] == "config/schedules.yaml"


# --- stats helpers -----------------------------------------------------------

def log_sent(conn, when, status="sent"):
    conn.execute("INSERT INTO send_log (reel_id, recipient, rule_name, sender_type, status, sent_at)"
                 " VALUES (1, 'f', 'r', 's', ?, ?)", (status, when))


def test_daily_counts_zero_filled_and_windowed(temp_db):
    today = datetime.date(2026, 3, 15)
    with db.connect() as c:
        log_sent(c, "2026-03-15 09:00:00")
        log_sent(c, "2026-03-15 21:00:00")
        log_sent(c, "2026-03-13 10:00:00")
        log_sent(c, "2026-03-15 10:00:00", status="failed")     # failures are not "sent"
        log_sent(c, "2026-02-01 10:00:00")                      # outside a 7-day window
    with db.connect() as c:
        rows = daily_counts(c, days=7, today=today)
    assert rows == [("2026-03-09", 0), ("2026-03-10", 0), ("2026-03-11", 0), ("2026-03-12", 0),
                    ("2026-03-13", 1), ("2026-03-14", 0), ("2026-03-15", 2)]
    with db.connect() as c:
        assert len(daily_counts(c, today=today)) == 30
        assert daily_counts(c, days=1, today=today) == [("2026-03-15", 2)]
        with pytest.raises(ValueError):
            daily_counts(c, days=0)


def test_daily_counts_empty_db_and_default_today(temp_db):
    with db.connect() as c:
        rows = daily_counts(c)
    assert len(rows) == 30 and all(n == 0 for _, n in rows)
    assert rows[-1][0] == db.now()[:10]


def test_weekly_counts_group_by_monday(temp_db):
    today = datetime.date(2026, 3, 15)            # a Sunday; its week starts Mon 2026-03-09
    with db.connect() as c:
        for when in ("2026-03-15 08:00:00", "2026-03-09 08:00:00",        # this week: 2
                     "2026-03-08 23:59:59", "2026-03-02 00:00:00",        # last week: 2
                     "2026-02-02 10:00:00"):                              # 6 weeks ago: 1
            log_sent(c, when)
        log_sent(c, "2026-03-10 08:00:00", status="failed")
        rows = weekly_counts(c, weeks=3, today=today)
    assert rows == [("2026-02-23", 0), ("2026-03-02", 2), ("2026-03-09", 2)]


def test_weekly_counts_includes_older_weeks_and_validates(temp_db):
    today = datetime.date(2026, 3, 15)
    with db.connect() as c:
        log_sent(c, "2026-02-02 10:00:00")
        rows = weekly_counts(c, weeks=7, today=today)
        assert rows[1] == ("2026-02-02", 1) and rows[0][1] == 0 and len(rows) == 7
        assert len(weekly_counts(c, today=today)) == 12
        with pytest.raises(ValueError):
            weekly_counts(c, weeks=0)


def test_failure_rate(temp_db):
    with db.connect() as c:
        assert failure_rate(c) == {"sent": 0, "failed": 0, "rate": 0.0}
        for _ in range(3):
            log_sent(c, "2026-03-15 08:00:00")
        log_sent(c, "2026-03-15 08:00:00", status="failed")
        res = failure_rate(c)
    assert res == {"sent": 3, "failed": 1, "rate": 0.25}


def test_failure_rate_all_failed(temp_db):
    with db.connect() as c:
        log_sent(c, "2026-03-15 08:00:00", status="failed")
        assert failure_rate(c)["rate"] == 1.0


# --- senders: send_text ------------------------------------------------------

def test_base_send_text_not_implemented():
    with pytest.raises(NotImplementedError):
        Sender().send_text("a", "b")


def test_dry_run_send_text(capsys):
    get_sender("dry_run").send_text("friend", "watch this")
    assert capsys.readouterr().out == "[DRY RUN] would send note 'watch this' to friend\n"


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


@pytest.fixture
def load_fresh(monkeypatch):
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


def test_telegram_send_text(load_fresh, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_FRIEND_CHAT_ID", "4242")
    req = fake_requests(FakeResponse(200))
    mod = load_fresh("telegram_sender", {"requests": req})
    mod.TelegramSender().send_text("ignored", "watch this 😂")
    url, kw = req.calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert kw["json"] == {"chat_id": "4242", "text": "watch this 😂"}   # no caption prefix


@pytest.mark.parametrize("result", [
    FakeResponse(403, text='{"description":"Forbidden"}'),
    FakeRequestException(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage"),
])
def test_telegram_send_text_errors_do_not_leak_token(load_fresh, monkeypatch, result):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_FRIEND_CHAT_ID", "4242")
    mod = load_fresh("telegram_sender", {"requests": fake_requests(result)})
    with pytest.raises(RuntimeError) as exc:
        mod.TelegramSender().send_text("x", "hi")
    assert TOKEN not in str(exc.value)


def test_official_send_text(load_fresh, monkeypatch):
    monkeypatch.setenv("IG_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("IG_RECIPIENT_IGSID", "999")
    req = fake_requests(FakeResponse(200))
    mod = load_fresh("official_api_sender", {"requests": req})
    mod.OfficialApiSender().send_text("ignored", "a note")
    url, kw = req.calls[0]
    assert url == f"https://graph.instagram.com/{mod.API_VERSION}/me/messages"
    assert kw["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert kw["json"] == {"recipient": {"id": "999"}, "message": {"text": "a note"}}


@pytest.mark.parametrize("resp,exc_type", [
    (FakeResponse(500, f"oops {TOKEN}"), RuntimeError),
    (FakeResponse(429, "slow down"), BlockedError),
    (FakeResponse(400, "{}", json_data={"error": {"code": 613}}), BlockedError),
    (FakeRequestException(f"reset Bearer {TOKEN}"), RuntimeError),
])
def test_official_send_text_error_mapping(load_fresh, monkeypatch, resp, exc_type):
    monkeypatch.setenv("IG_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("IG_RECIPIENT_IGSID", "999")
    mod = load_fresh("official_api_sender", {"requests": fake_requests(resp)})
    with pytest.raises(exc_type) as exc:
        mod.OfficialApiSender().send_text("x", "note")
    assert TOKEN not in str(exc.value)
    if exc_type is RuntimeError:
        assert not isinstance(exc.value, BlockedError)


def make_ig(load_fresh, monkeypatch, tmp_path):
    exc_mod = types.ModuleType("instagrapi.exceptions")
    for n in ("ChallengeRequired", "FeedbackRequired", "LoginRequired", "PleaseWaitFewMinutes"):
        setattr(exc_mod, n, type(n, (Exception,), {}))

    class Client:
        def __init__(self):
            self.calls = []
            self.send_error = None

        def load_settings(self, path):
            pass

        def login(self, user, password):
            pass

        def dump_settings(self, path):
            pass

        def user_id_from_username(self, name):
            self.calls.append(("user_id_from_username", name))
            return "555"

        def direct_send(self, text, user_ids):
            self.calls.append(("direct_send", text, user_ids))
            if self.send_error:
                raise self.send_error
            return types.SimpleNamespace(id="m1", thread_id="t1")   # a delivered message

    pkg = types.ModuleType("instagrapi")
    pkg.__path__ = []
    pkg.Client = Client
    pkg.exceptions = exc_mod
    monkeypatch.setenv("IG_USERNAME", "me")
    monkeypatch.setenv("IG_PASSWORD", "pw")
    mod = load_fresh("instagrapi_sender", {"instagrapi": pkg, "instagrapi.exceptions": exc_mod})
    monkeypatch.setattr(mod, "SESSION", tmp_path / "data" / "session.json")
    return mod.InstagrapiSender(), exc_mod


def test_instagrapi_send_text(load_fresh, monkeypatch, tmp_path):
    s, _ = make_ig(load_fresh, monkeypatch, tmp_path)
    s.send_text("friend", "a note")
    s.send_text("friend", "again")
    assert ("direct_send", "a note", [555]) in s.cl.calls
    assert ("direct_send", "again", [555]) in s.cl.calls
    assert s.cl.calls.count(("user_id_from_username", "friend")) == 1    # id cached


@pytest.mark.parametrize("name", ["ChallengeRequired", "FeedbackRequired",
                                  "LoginRequired", "PleaseWaitFewMinutes"])
def test_instagrapi_send_text_block_becomes_blocked_error(load_fresh, monkeypatch, tmp_path, name):
    s, exc = make_ig(load_fresh, monkeypatch, tmp_path)
    s.cl.send_error = getattr(exc, name)("nope")
    with pytest.raises(BlockedError, match=name):
        s.send_text("friend", "note")


def test_instagrapi_send_text_other_errors_propagate(load_fresh, monkeypatch, tmp_path):
    s, _ = make_ig(load_fresh, monkeypatch, tmp_path)
    s.cl.send_error = ValueError("weird")
    with pytest.raises(ValueError):
        s.send_text("friend", "note")
