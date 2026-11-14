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
    rule_jobs = [j for j in sched.jobs
                 if j[2]["id"] not in ("__reload__", "__discovery__", "__test_send__")]   # + the two system jobs
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


# --- sender start-up failures: recorded for the dashboard, and no fast re-login loop -------

def test_record_sender_error_sets_and_clears(main_mod, temp_db):
    from reeldrop import settings
    main_mod.record_sender_error("RuntimeError: verification needed")
    assert settings.get(main_mod.SENDER_ERROR_KEY) == "RuntimeError: verification needed"
    assert settings.get(main_mod.SENDER_ERROR_AT_KEY)
    main_mod.record_sender_error("")
    assert settings.get(main_mod.SENDER_ERROR_KEY) == ""
    assert settings.get(main_mod.SENDER_ERROR_AT_KEY) == ""


def test_record_sender_error_never_raises(main_mod, monkeypatch):
    import reeldrop.settings as settings

    def boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(settings, "set_", boom)
    main_mod.record_sender_error("x")                   # must not raise


@pytest.mark.parametrize("value,seconds", [
    (None, 3600), ("", 3600), ("abc", 3600), ("0", 0), ("-5", 0), ("15", 900),
    (" 30 ", 1800), ("99999", 24 * 3600),
])
def test_login_retry_seconds(main_mod, value, seconds):
    env = {} if value is None else {"LOGIN_RETRY_MINUTES": value}
    assert main_mod.login_retry_seconds(env) == seconds


@pytest.mark.parametrize("sender_name", ["dry_run", "telegram", "official"])
def test_no_wait_for_senders_that_do_not_log_in_to_instagram(main_mod, sender_name):
    slept = []
    assert main_mod.wait_before_exit(sender_name, sleep=slept.append, env={},
                                     requested=lambda: False) == 0
    assert slept == []


def test_wait_before_exit_waits_the_full_time(main_mod):
    slept = []
    waited = main_mod.wait_before_exit("instagrapi", sleep=slept.append,
                                       env={"LOGIN_RETRY_MINUTES": "1"},
                                       requested=lambda: False)
    assert waited == 60 and sum(slept) == 60


def test_wait_before_exit_can_be_disabled(main_mod):
    slept = []
    assert main_mod.wait_before_exit("instagrapi", sleep=slept.append,
                                     env={"LOGIN_RETRY_MINUTES": "0"},
                                     requested=lambda: False) == 0
    assert slept == []


def test_wait_ends_early_when_the_dashboard_asks_for_a_retry(main_mod):
    slept = []
    answers = iter([False, False, False, True])        # stale check, then 3 polls

    waited = main_mod.wait_before_exit("instagrapi", sleep=slept.append,
                                       env={"LOGIN_RETRY_MINUTES": "60"},
                                       requested=lambda: next(answers))
    assert waited == 15 and len(slept) == 3


def test_a_stale_retry_request_does_not_skip_the_wait(main_mod, temp_db):
    from reeldrop import settings
    settings.set_(main_mod.SENDER_RETRY_KEY, "1")       # pressed before this failure
    slept = []
    waited = main_mod.wait_before_exit("instagrapi", sleep=slept.append,
                                       env={"LOGIN_RETRY_MINUTES": "1"})
    assert waited == 60                                 # the old request was dropped
    assert settings.get(main_mod.SENDER_RETRY_KEY) == "0"


def test_retry_requested_is_true_once(main_mod, temp_db):
    from reeldrop import settings
    assert main_mod.retry_requested() is False
    settings.set_(main_mod.SENDER_RETRY_KEY, "1")
    assert main_mod.retry_requested() is True
    assert main_mod.retry_requested() is False


# --- run_rule returns what it did -------------------------------------------------

def test_run_rule_reports_its_result(temp_db, monkeypatch):
    from reeldrop import job as job_mod
    from reeldrop import queue as q
    from reeldrop.config import Config, Rule
    from reeldrop.settings import set_paused

    monkeypatch.setattr(job_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(job_mod, "notify_me", lambda *a, **k: None)
    sent = []

    class S:
        name = "fake"

        def send(self, r, url):
            sent.append((r, url))

    rule = Rule(name="r", days="*", time="09:00", count=2, recipient="pal")
    cfg = Config(daily_cap=5, gap_seconds=0, rules=[rule])
    assert job_mod.run_rule(rule, cfg, S()) == {"status": "empty", "sent": 0, "total": 0}
    q.add_reel("https://www.instagram.com/reel/AAA/")
    assert job_mod.run_rule(rule, cfg, S()) == {"status": "sent", "sent": 1, "total": 1}
    set_paused(True)
    assert job_mod.run_rule(rule, cfg, S())["status"] == "paused"
    set_paused(False)
    capped = Config(daily_cap=1, gap_seconds=0, rules=[rule])
    assert job_mod.run_rule(rule, capped, S())["status"] == "cap"
    quiet = Config(daily_cap=5, gap_seconds=0, rules=[rule], quiet_days=[job_mod._today(cfg)])
    assert job_mod.run_rule(rule, quiet, S())["status"] == "quiet"
    assert len(sent) == 1


# --- test send (dashboard button -> scheduler job) ----------------------------------

def _cfg_with_friends(*friends, gap=7):
    from reeldrop.config import Config, Rule
    rules = [Rule(name=f"r{i}", days="*", time="09:00", count=2, recipient=f)
             for i, f in enumerate(friends)]
    return Config(daily_cap=8, gap_seconds=gap, rules=rules)


def test_send_test_reels_sends_one_reel_to_each_friend_once(main_mod):
    calls, sleeps = [], []

    def runner(rule, cfg, sender):
        calls.append((rule.name, rule.recipient, rule.count))
        return {"status": "sent", "sent": 1, "total": 1}

    cfg = _cfg_with_friends("pal", "buddy", "pal")          # pal has two rules
    summary = main_mod.send_test_reels(cfg, object(), runner=runner, sleep=sleeps.append,
                                       paused=lambda: False)
    assert calls == [("test_send", "pal", 1), ("test_send", "buddy", 1)]
    assert sleeps == [7]                                     # the gap between friends
    assert summary == "@pal: sent 1/1; @buddy: sent 1/1"


def test_send_test_reels_describes_every_outcome(main_mod):
    results = iter([{"status": "empty"}, {"status": "cap"}, {"status": "quiet"},
                    {"status": "paused"}, {"status": "sent", "sent": 0, "total": 1}])
    cfg = _cfg_with_friends("a", "b", "c", "d", "e", gap=0)
    summary = main_mod.send_test_reels(cfg, object(), runner=lambda *a: next(results),
                                       sleep=lambda s: None, paused=lambda: False)
    assert summary == ("@a: no reel waiting for this friend; "
                       "@b: skipped, the daily cap is reached; "
                       "@c: skipped, today is a quiet day; "
                       "@d: skipped, sending is paused; @e: sent 0/1")


def test_send_test_reels_stops_after_a_block(main_mod):
    state = {"paused": False}

    def runner(rule, cfg, sender):
        state["paused"] = True
        return {"status": "blocked", "sent": 0, "total": 1}

    summary = main_mod.send_test_reels(_cfg_with_friends("a", "b"), object(), runner=runner,
                                       sleep=lambda s: None, paused=lambda: state["paused"])
    assert summary.startswith("@a: sent 0/1 - Instagram blocked")
    assert summary.endswith("stopped: sending is paused")
    assert "@b" not in summary


def test_send_test_reels_without_friends(main_mod):
    from reeldrop.config import Config
    assert main_mod.send_test_reels(Config(), object(), runner=lambda *a: 1 / 0) == \
        "Not run: there are no friends in the schedule"


def test_check_test_send_request_runs_once_per_request(main_mod, temp_db):
    from reeldrop import settings
    ran = []

    def runner(rule, cfg, sender):
        ran.append(rule.recipient)
        return {"status": "sent", "sent": 1, "total": 1}

    loader = lambda path: _cfg_with_friends("pal", gap=0)
    assert main_mod.check_test_send_request(object(), loader=loader, runner=runner) is None
    assert ran == []                                          # nothing requested yet
    settings.set_(main_mod.TEST_SEND_REQUEST_KEY, "1")
    assert main_mod.check_test_send_request(object(), loader=loader, runner=runner) == \
        "@pal: sent 1/1"
    assert settings.get(main_mod.TEST_SEND_REQUEST_KEY) == "0"
    assert settings.get(main_mod.TEST_SEND_RESULT_KEY) == "@pal: sent 1/1"
    assert settings.get(main_mod.TEST_SEND_AT_KEY)
    main_mod.check_test_send_request(object(), loader=loader, runner=runner)
    assert ran == ["pal"]                                     # not repeated


def test_check_test_send_request_records_problems(main_mod, temp_db):
    from reeldrop import settings

    def bad_loader(path):
        raise ValueError("rules is not a list")

    settings.set_(main_mod.TEST_SEND_REQUEST_KEY, "1")
    summary = main_mod.check_test_send_request(object(), loader=bad_loader)
    assert summary.startswith("Not run: the schedule could not be loaded")
    assert settings.get(main_mod.TEST_SEND_REQUEST_KEY) == "0"

    def boom(rule, cfg, sender):
        raise RuntimeError("db gone")

    settings.set_(main_mod.TEST_SEND_REQUEST_KEY, "1")
    summary = main_mod.check_test_send_request(
        object(), loader=lambda p: _cfg_with_friends("pal", gap=0), runner=boom)
    assert summary == "Failed: RuntimeError: db gone"


def test_test_send_job_id_is_a_protected_system_job(main_mod):
    assert main_mod.TEST_SEND_JOB_ID in main_mod.SYSTEM_JOB_IDS


def test_real_runner_sends_a_test_reel_end_to_end(main_mod, temp_db, monkeypatch):
    from reeldrop import job as job_mod
    from reeldrop import queue as q

    monkeypatch.setattr(job_mod, "notify_me", lambda *a, **k: None)
    sent = []

    class S:
        name = "fake"

        def send(self, r, url):
            sent.append((r, url))

    q.add_reel("https://www.instagram.com/reel/AAA/")
    q.add_reel("https://www.instagram.com/reel/BBB/")
    summary = main_mod.send_test_reels(_cfg_with_friends("pal", "buddy", gap=0), S(),
                                       sleep=lambda s: None)
    assert summary == "@pal: sent 1/1; @buddy: sent 1/1"
    assert sorted(r for r, _ in sent) == ["buddy", "pal"]
    assert len({u for _, u in sent}) == 2                    # different reels, none twice
    assert [r["status"] for r in q.list_reels()] == ["sent", "sent"]


# --- heartbeat and the one-scheduler guard ------------------------------------------

def test_heartbeat_and_owner(main_mod, temp_db):
    from reeldrop import settings
    main_mod.write_heartbeat("me-1")
    assert settings.get(main_mod.OWNER_KEY) == "me-1"
    assert settings.get(main_mod.HEARTBEAT_KEY)
    assert main_mod.other_scheduler_running("me-1") is None     # it is us
    assert main_mod.other_scheduler_running("me-2") == "me-1"   # someone else, fresh


def test_stale_heartbeat_from_another_scheduler_is_ignored(main_mod, temp_db):
    from reeldrop import settings
    settings.set_(main_mod.OWNER_KEY, "old-host")
    settings.set_(main_mod.HEARTBEAT_KEY, "2026-01-01 00:00:00")
    assert main_mod.other_scheduler_running("me") is None


def test_instance_id_prefers_render(main_mod):
    assert main_mod.instance_id({"RENDER_INSTANCE_ID": "srv-abc"}) == "srv-abc"
    import socket
    assert main_mod.instance_id({}) == f"host:{socket.gethostname()}"
    assert main_mod.instance_id({}) == main_mod.instance_id({})   # stable across restarts


def test_test_send_job_writes_the_heartbeat(main_mod, temp_db):
    from reeldrop import settings
    main_mod.check_test_send_request(object(), loader=lambda p: None)
    assert settings.get(main_mod.HEARTBEAT_KEY)


def test_main_refuses_to_start_next_to_another_scheduler(main_mod, temp_db, monkeypatch):
    from reeldrop import settings
    settings.set_(main_mod.OWNER_KEY, "other-host")
    from reeldrop.db import now
    settings.set_(main_mod.HEARTBEAT_KEY, now())
    slept = []
    monkeypatch.setattr(main_mod.time, "sleep", slept.append)
    monkeypatch.setattr(main_mod, "get_sender", lambda name: pytest.fail("must not log in"))
    with pytest.raises(SystemExit):
        main_mod.main()
    assert slept == [main_mod.OTHER_SCHEDULER_WAIT_SECONDS]
    assert "another scheduler (other-host)" in settings.get("sender_error")
