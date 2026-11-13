import importlib
import sys
import types

import pytest

from reeldrop import db, job, settings
from reeldrop.config import Config, Rule
from reeldrop.queue import add_reel, list_reels, sent_today
from reeldrop.senders import BlockedError


class FakeSender:
    name = "fake"

    def __init__(self, outcomes=None):
        # outcomes: list consumed one per send() call; None = success, exception = raise it.
        self.outcomes = list(outcomes or [])
        self.calls = []

    def send(self, recipient, url):
        self.calls.append((recipient, url))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome


@pytest.fixture
def env(temp_db, monkeypatch):
    """Alerts recorded, sleeps recorded (never real)."""
    alerts, sleeps = [], []
    monkeypatch.setattr(job, "notify_me", alerts.append)
    monkeypatch.setattr(job.time, "sleep", sleeps.append)
    monkeypatch.setattr(job, "RETRY_WAIT_SECONDS", 7)
    return types.SimpleNamespace(alerts=alerts, sleeps=sleeps)


def add(n):
    for i in range(n):
        add_reel(f"https://www.instagram.com/reel/CODE{i:03d}/")


def statuses():
    return [r["status"] for r in list_reels()]


def log_rows():
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM send_log ORDER BY id")]


def make(count=3, daily_cap=10, gap=5):
    return (Rule(name="rule", days="*", time="20:00", count=count, recipient="friend"),
            Config(daily_cap=daily_cap, gap_seconds=gap))


def test_normal_send_marks_sent_and_logs(env):
    add(3)
    rule, cfg = make(count=2)
    sender = FakeSender()
    job.run_rule(rule, cfg, sender)
    assert statuses() == ["sent", "sent", "pending"]
    assert [c[0] for c in sender.calls] == ["friend", "friend"]
    assert [c[1] for c in sender.calls] == [
        "https://www.instagram.com/reel/CODE000/", "https://www.instagram.com/reel/CODE001/"]
    rows = log_rows()
    assert [(r["status"], r["rule_name"], r["sender_type"], r["recipient"]) for r in rows] == \
        [("sent", "rule", "fake", "friend")] * 2
    assert env.alerts == ["✅ ReelDrop rule: sent 2/2"]


def test_daily_cap_limits_sends(env):
    add(5)
    rule, cfg = make(count=5, daily_cap=2)
    sender = FakeSender()
    job.run_rule(rule, cfg, sender)
    assert len(sender.calls) == 2
    assert sent_today() == 2
    # a second run the same day sends nothing more
    job.run_rule(rule, cfg, sender)
    assert len(sender.calls) == 2
    assert statuses().count("sent") == 2


def test_empty_queue_notifies(env):
    rule, cfg = make()
    sender = FakeSender()
    job.run_rule(rule, cfg, sender)
    assert sender.calls == []
    assert len(env.alerts) == 1 and "queue is empty" in env.alerts[0]


def test_paused_sends_nothing(env):
    add(2)
    settings.set_paused(True)
    rule, cfg = make()
    sender = FakeSender()
    job.run_rule(rule, cfg, sender)
    assert sender.calls == []
    assert statuses() == ["pending", "pending"]
    assert log_rows() == []
    assert env.alerts == []


def test_pause_during_run_stops_remaining(env):
    add(3)
    rule, cfg = make()

    class PausingSender(FakeSender):
        def send(self, recipient, url):
            super().send(recipient, url)
            settings.set_paused(True)

    sender = PausingSender()
    job.run_rule(rule, cfg, sender)
    assert len(sender.calls) == 1
    assert statuses() == ["sent", "pending", "pending"]


def test_transient_failure_retried_once_then_success(env):
    add(1)
    rule, cfg = make(count=1)
    sender = FakeSender([RuntimeError("network down")])
    job.run_rule(rule, cfg, sender)
    assert len(sender.calls) == 2
    assert env.sleeps == [7]                     # retry wait, no gap (single reel)
    assert statuses() == ["sent"]
    assert [r["status"] for r in log_rows()] == ["sent"]


def test_failure_twice_marks_failed_exactly_once(env):
    add(2)
    rule, cfg = make(count=1)
    sender = FakeSender([RuntimeError("boom 1"), RuntimeError("boom 2")])
    job.run_rule(rule, cfg, sender)
    assert len(sender.calls) == 2
    assert statuses() == ["failed", "pending"]
    rows = log_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "failed" and rows[0]["error"] == "boom 2"
    assert not settings.is_paused()


def test_failed_reel_does_not_stop_the_run(env):
    add(2)
    rule, cfg = make(count=2)
    sender = FakeSender([RuntimeError("a"), RuntimeError("b")])   # reel 1 fails twice
    job.run_rule(rule, cfg, sender)
    assert statuses() == ["failed", "sent"]
    assert len(log_rows()) == 2
    assert env.alerts == ["⚠️ ReelDrop rule: sent 1/2"]


def test_blocked_pauses_alerts_and_leaves_rest_pending(env):
    add(3)
    rule, cfg = make(count=3)
    sender = FakeSender([None, BlockedError("challenge_required")])
    job.run_rule(rule, cfg, sender)
    assert len(sender.calls) == 2                 # 3rd reel never attempted, block not retried
    assert statuses() == ["sent", "failed", "pending"]
    assert settings.is_paused()
    rows = log_rows()
    assert [r["status"] for r in rows] == ["sent", "failed"]
    assert rows[1]["error"] == "BLOCKED: challenge_required"
    assert len(env.alerts) == 1 and "PAUSED" in env.alerts[0]
    assert env.sleeps == [5]                      # gap after reel 1 only; none after the block

    # later rules skip while paused
    job.run_rule(rule, cfg, sender)
    assert len(sender.calls) == 2
    assert statuses() == ["sent", "failed", "pending"]


def test_blocked_during_retry_is_a_block(env):
    add(2)
    rule, cfg = make(count=2)
    sender = FakeSender([RuntimeError("flaky"), BlockedError("rate limited")])
    job.run_rule(rule, cfg, sender)
    assert len(sender.calls) == 2
    assert statuses() == ["failed", "pending"]
    assert settings.is_paused()
    rows = log_rows()
    assert len(rows) == 1 and rows[0]["error"] == "BLOCKED: rate limited"
    assert any("PAUSED" in a for a in env.alerts)


def test_no_duplicates_when_rule_runs_twice(env):
    add(3)
    rule, cfg = make(count=3)
    sender = FakeSender()
    job.run_rule(rule, cfg, sender)               # sends all three
    job.run_rule(rule, cfg, sender)               # "restart": nothing left to send
    urls = [c[1] for c in sender.calls]
    assert len(urls) == 3 and len(set(urls)) == 3
    assert statuses() == ["sent"] * 3
    assert len(log_rows()) == 3
    assert "queue is empty" in env.alerts[-1]


def test_restart_after_partial_run_does_not_resend(env):
    add(3)
    rule, cfg = make(count=3)
    first = FakeSender([None, BlockedError("x")])
    job.run_rule(rule, cfg, first)
    settings.set_paused(False)
    second = FakeSender()
    job.run_rule(rule, cfg, second)
    # reel 0 was sent and reel 1 failed (blocked): only reel 2 goes out now
    assert [c[1] for c in second.calls] == ["https://www.instagram.com/reel/CODE002/"]


def test_gap_sleep_between_reels_not_after_last(env):
    add(3)
    rule, cfg = make(count=3, gap=11)
    job.run_rule(rule, cfg, FakeSender())
    assert env.sleeps == [11, 11]


def test_mark_sent_failure_is_not_recorded_as_failed_send(env, monkeypatch):
    add(2)
    rule, cfg = make(count=2)

    def broken(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(job, "mark_sent", broken)
    sender = FakeSender()
    job.run_rule(rule, cfg, sender)
    assert len(sender.calls) == 1                 # stops after the first
    assert settings.is_paused()
    assert statuses() == ["pending", "pending"]
    assert log_rows() == []
    assert "sent but the database update failed" in env.alerts[0]


def test_retry_wait_argument_overrides_constant(monkeypatch):
    sleeps = []
    monkeypatch.setattr(job.time, "sleep", sleeps.append)
    job._send_with_retry(FakeSender([RuntimeError("x")]), "r", "u", wait=0)
    assert sleeps == [0]


# --- main.py: logging, job registration, startup failures --------------------

class FakeCronTrigger:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeScheduler:
    instances = []

    def __init__(self, timezone=None):
        self.timezone = timezone
        self.jobs = []
        self.started = False
        self.raise_on_start = None
        self.shutdown_called = False
        FakeScheduler.instances.append(self)

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append((func, trigger, kwargs))

    def start(self):
        self.started = True
        if self.raise_on_start:
            raise self.raise_on_start

    def shutdown(self, wait=True):
        self.shutdown_called = True


@pytest.fixture
def main_mod(monkeypatch, tmp_path):
    """Import main.py against fake dotenv/apscheduler so no third-party libs are needed."""
    calls = []
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: calls.append("load_dotenv")
    ap = types.ModuleType("apscheduler")
    schedulers = types.ModuleType("apscheduler.schedulers")
    blocking = types.ModuleType("apscheduler.schedulers.blocking")
    blocking.BlockingScheduler = FakeScheduler
    triggers = types.ModuleType("apscheduler.triggers")
    cron = types.ModuleType("apscheduler.triggers.cron")
    cron.CronTrigger = FakeCronTrigger
    for name, mod in {"dotenv": dotenv, "apscheduler": ap, "apscheduler.schedulers": schedulers,
                      "apscheduler.schedulers.blocking": blocking, "apscheduler.triggers": triggers,
                      "apscheduler.triggers.cron": cron}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "main", raising=False)
    monkeypatch.chdir(tmp_path)   # main() would create ./data; keep it out of the repo
    FakeScheduler.instances.clear()
    mod = importlib.import_module("main")
    mod._dotenv_calls = calls
    yield mod
    sys.modules.pop("main", None)


def test_main_importable_and_loads_dotenv_first(main_mod):
    assert main_mod._dotenv_calls == ["load_dotenv"]
    assert callable(main_mod.main)


def test_register_jobs_one_cron_job_per_rule(main_mod):
    cfg = Config(timezone="Asia/Kolkata", rules=[
        Rule("evening", "*", "20:05", 3, "friend"),
        Rule("weekend", "sat,sun", "08:30", 5, "friend"),
    ])
    sender = FakeSender()
    sched = FakeScheduler()
    main_mod.register_jobs(sched, cfg, sender)
    assert len(sched.jobs) == 2
    func, trigger, kw = sched.jobs[1]
    assert func is job.run_rule
    assert trigger.kwargs == {"day_of_week": "sat,sun", "hour": 8, "minute": 30,
                              "timezone": "Asia/Kolkata"}
    assert kw["id"] == "weekend"
    assert kw["args"] == [cfg.rules[1], cfg, sender]
    assert kw["misfire_grace_time"] == 600 and kw["coalesce"] is True and kw["max_instances"] == 1


def _patch_main(main_mod, monkeypatch, cfg, sender_factory):
    monkeypatch.setattr(main_mod, "init_db", lambda: None)
    monkeypatch.setattr(main_mod, "setup_logging", lambda: None)
    monkeypatch.setattr(main_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(main_mod, "get_sender", sender_factory)


def test_main_exits_nonzero_and_alerts_when_sender_fails(main_mod, monkeypatch):
    alerts = []
    monkeypatch.setattr(main_mod, "notify_me", alerts.append)

    def boom(name):
        raise RuntimeError("login failed")

    _patch_main(main_mod, monkeypatch, Config(), boom)
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code not in (0, None)
    assert len(alerts) == 1 and "login failed" in alerts[0]
    assert FakeScheduler.instances == []          # scheduler never created


def test_main_exits_nonzero_on_bad_config(main_mod, monkeypatch):
    monkeypatch.setattr(main_mod, "init_db", lambda: None)
    monkeypatch.setattr(main_mod, "setup_logging", lambda: None)

    def bad():
        raise ValueError("bad config")

    monkeypatch.setattr(main_mod, "load_config", bad)
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 1


def test_main_starts_scheduler_and_handles_ctrl_c(main_mod, monkeypatch):
    cfg = Config(rules=[Rule("a", "*", "20:00", 1, "friend")])
    _patch_main(main_mod, monkeypatch, cfg, lambda name: FakeSender())
    monkeypatch.setattr(FakeScheduler, "start", lambda self: (_ for _ in ()).throw(KeyboardInterrupt()))
    main_mod.main()                               # must not raise
    sched = FakeScheduler.instances[0]
    rule_jobs = [j for j in sched.jobs if j[2]["id"] != "__reload__"]   # + the reload job
    assert len(rule_jobs) == 1 and sched.shutdown_called


def test_main_uses_sender_env_default_dry_run(main_mod, monkeypatch):
    seen = []
    monkeypatch.delenv("SENDER", raising=False)
    _patch_main(main_mod, monkeypatch, Config(), lambda name: seen.append(name) or FakeSender())
    main_mod.main()
    assert seen == ["dry_run"] and FakeScheduler.instances[0].started


def test_main_starts_telegram_control_only_when_enabled(main_mod, monkeypatch):
    started = []
    fake = types.ModuleType("reeldrop.telegram_control")
    fake.start_in_thread = lambda *a, **k: started.append(1)
    monkeypatch.setitem(sys.modules, "reeldrop.telegram_control", fake)

    monkeypatch.delenv("TELEGRAM_CONTROL", raising=False)
    _patch_main(main_mod, monkeypatch, Config(), lambda name: FakeSender())
    main_mod.main()
    assert started == []                          # off by default

    monkeypatch.setenv("TELEGRAM_CONTROL", "0")
    main_mod.main()
    assert started == []

    monkeypatch.setenv("TELEGRAM_CONTROL", "1")
    main_mod.main()
    assert started == [1]
