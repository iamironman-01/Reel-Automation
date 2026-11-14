"""Wiring of auto-discovery into the runtime: job top-up, scheduler job, CLI.

reeldrop.discovery is replaced by a fake module in every test, so these tests do not
depend on the real implementation.
"""
import importlib
import logging
import os
import sys
import types

import pytest

from reeldrop import config as config_mod, db, job, settings
from reeldrop.__main__ import main as cli_main
from reeldrop.config import Config, Rule
from reeldrop.queue import add_reel, list_reels
from reeldrop.senders import BlockedError


# --- fakes -------------------------------------------------------------------

class FakeSender:
    name = "fake"

    def __init__(self):
        self.calls = []

    def send(self, recipient, url):
        self.calls.append((recipient, url))


@pytest.fixture
def fake_discovery(monkeypatch):
    """A stand-in reeldrop.discovery that records calls."""
    mod = types.ModuleType("reeldrop.discovery")
    mod.calls = types.SimpleNamespace(tick=[], top_up=[], provider=[], refill=[])
    mod.top_up_adds = 0           # reels top_up adds to the queue per call
    mod.top_up_error = None
    mod.tick_error = None
    mod.refill_error = None
    mod.refill_result = types.SimpleNamespace(added=["a", "b"], dry_run=False)
    counter = {"n": 0}

    def top_up(needed, get_client, *, loader=None):
        mod.calls.top_up.append((needed, get_client))
        if mod.top_up_error:
            raise mod.top_up_error
        added = min(needed, mod.top_up_adds)
        for _ in range(added):
            counter["n"] += 1
            add_reel(f"https://www.instagram.com/reel/DISC{counter['n']:03d}/")
        return added

    def tick(get_client, *, loader=None, now_fn=None):
        mod.calls.tick.append(get_client)
        if mod.tick_error:
            raise mod.tick_error

    def make_client_provider(sender):
        mod.calls.provider.append(sender)
        return lambda: "client"

    def refill(discovery_cfg, client, *, dry_run=False, limit=None):
        mod.calls.refill.append((discovery_cfg, client, dry_run, limit))
        if mod.refill_error:
            raise mod.refill_error
        return mod.refill_result

    mod.top_up, mod.tick = top_up, tick
    mod.make_client_provider, mod.refill = make_client_provider, refill
    mod.format_result = lambda r: f"SUMMARY added={len(r.added)} dry_run={r.dry_run}"
    monkeypatch.setitem(sys.modules, "reeldrop.discovery", mod)
    return mod


# --- job.py: top-up when the queue is short -----------------------------------

@pytest.fixture
def env(temp_db, monkeypatch):
    alerts = []
    monkeypatch.setattr(job, "notify_me", alerts.append)
    monkeypatch.setattr(job.time, "sleep", lambda s: None)
    monkeypatch.setattr(job, "_client_provider", None)    # restored after the test
    return types.SimpleNamespace(alerts=alerts)


def make(count=3, enabled=True, daily_cap=10):
    rule = Rule(name="rule", days="*", time="20:00", count=count, recipient="friend")
    cfg = Config(daily_cap=daily_cap, gap_seconds=0)
    cfg.discovery = types.SimpleNamespace(enabled=enabled)
    return rule, cfg


def add(n):
    for i in range(n):
        add_reel(f"https://www.instagram.com/reel/CODE{i:03d}/")


def statuses():
    return [r["status"] for r in list_reels()]


def provider():
    return "provider"


def test_set_client_provider_sets_and_clears(env):
    job.set_client_provider(provider)
    assert job._client_provider is provider
    job.set_client_provider(None)
    assert job._client_provider is None


def test_empty_queue_tops_up_once_then_sends(env, fake_discovery):
    fake_discovery.top_up_adds = 10
    job.set_client_provider(provider)
    rule, cfg = make(count=3)
    sender = FakeSender()
    job.run_rule(rule, cfg, sender)
    assert fake_discovery.calls.top_up == [(3, provider)]
    assert len(sender.calls) == 3
    assert statuses() == ["sent"] * 3
    assert env.alerts == ["✅ ReelDrop rule: sent 3/3"]


def test_partly_short_queue_asks_only_for_the_missing_reels(env, fake_discovery):
    fake_discovery.top_up_adds = 10
    job.set_client_provider(provider)
    add(1)
    rule, cfg = make(count=3)
    sender = FakeSender()
    job.run_rule(rule, cfg, sender)
    assert fake_discovery.calls.top_up == [(2, provider)]
    assert len(sender.calls) == 3


def test_short_queue_top_up_adds_fewer_than_needed_sends_what_exists(env, fake_discovery):
    fake_discovery.top_up_adds = 1
    job.set_client_provider(provider)
    rule, cfg = make(count=3)
    sender = FakeSender()
    job.run_rule(rule, cfg, sender)
    assert len(fake_discovery.calls.top_up) == 1          # one attempt only
    assert len(sender.calls) == 1
    assert env.alerts == ["✅ ReelDrop rule: sent 1/1"]


def test_needed_respects_daily_cap_remaining(env, fake_discovery):
    fake_discovery.top_up_adds = 10
    job.set_client_provider(provider)
    rule, cfg = make(count=5, daily_cap=2)
    job.run_rule(rule, cfg, FakeSender())
    assert fake_discovery.calls.top_up == [(2, provider)]


def test_still_empty_after_top_up_notifies_as_before(env, fake_discovery):
    fake_discovery.top_up_adds = 0
    job.set_client_provider(provider)
    rule, cfg = make()
    sender = FakeSender()
    job.run_rule(rule, cfg, sender)
    assert len(fake_discovery.calls.top_up) == 1
    assert sender.calls == []
    assert len(env.alerts) == 1 and "queue is empty" in env.alerts[0]


def test_full_queue_does_not_top_up(env, fake_discovery):
    job.set_client_provider(provider)
    add(3)
    rule, cfg = make(count=3)
    job.run_rule(rule, cfg, FakeSender())
    assert fake_discovery.calls.top_up == []


def test_no_top_up_when_discovery_disabled(env, fake_discovery):
    job.set_client_provider(provider)
    rule, cfg = make(enabled=False)
    job.run_rule(rule, cfg, FakeSender())
    assert fake_discovery.calls.top_up == []
    assert "queue is empty" in env.alerts[0]


def test_no_top_up_when_discovery_absent_from_config(env, fake_discovery):
    job.set_client_provider(provider)
    rule, _ = make()
    cfg = types.SimpleNamespace(timezone="Asia/Kolkata", quiet_days=[], daily_cap=10, gap_seconds=0)
    job.run_rule(rule, cfg, FakeSender())
    assert fake_discovery.calls.top_up == []
    assert "queue is empty" in env.alerts[0]


def test_no_top_up_without_client_provider(env, fake_discovery):
    rule, cfg = make()
    job.run_rule(rule, cfg, FakeSender())
    assert fake_discovery.calls.top_up == []
    assert "queue is empty" in env.alerts[0]


def test_top_up_error_is_swallowed_and_run_continues(env, fake_discovery):
    fake_discovery.top_up_error = RuntimeError("boom")
    job.set_client_provider(provider)
    rule, cfg = make()
    job.run_rule(rule, cfg, FakeSender())
    assert "queue is empty" in env.alerts[0]


def test_missing_discovery_module_does_not_break_the_run(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "reeldrop.discovery", None)    # import fails
    job.set_client_provider(provider)
    add(1)
    rule, cfg = make(count=2)
    sender = FakeSender()
    job.run_rule(rule, cfg, sender)
    assert len(sender.calls) == 1


def test_pause_during_top_up_sends_nothing(env, fake_discovery):
    job.set_client_provider(provider)
    add(1)
    rule, cfg = make(count=3)
    fake_discovery.top_up_adds = 5
    orig = fake_discovery.top_up

    def blocking_top_up(needed, get_client, *, loader=None):
        orig(needed, get_client)
        settings.set_paused(True)         # discovery hit a block and paused everything
        return needed

    fake_discovery.top_up = blocking_top_up
    sender = FakeSender()
    job.run_rule(rule, cfg, sender)
    assert sender.calls == []
    assert env.alerts == []
    assert statuses().count("sent") == 0


# --- main.py: scheduler job ----------------------------------------------------

class FakeCronTrigger:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeSched:
    def __init__(self, timezone=None):
        self.timezone = timezone
        self.jobs = {}
        self.removed = []
        self.started = False

    def add_job(self, func, trigger, **kw):
        if kw["id"] in self.jobs and not kw.get("replace_existing"):
            raise ValueError("job exists")
        self.jobs[kw["id"]] = types.SimpleNamespace(id=kw["id"], func=func, trigger=trigger, kwargs=kw)

    def remove_job(self, job_id):
        self.removed.append(job_id)
        del self.jobs[job_id]

    def get_jobs(self):
        return list(self.jobs.values())

    def start(self):
        self.started = True

    def shutdown(self, wait=True):
        pass


@pytest.fixture
def main_mod(monkeypatch, tmp_path):
    """Import main.py against fake dotenv/apscheduler so no third-party libs are needed."""
    blocking = types.ModuleType("apscheduler.schedulers.blocking")
    blocking.BlockingScheduler = FakeSched
    cron = types.ModuleType("apscheduler.triggers.cron")
    cron.CronTrigger = FakeCronTrigger
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: None
    for name, mod in {"dotenv": dotenv, "apscheduler": types.ModuleType("apscheduler"),
                      "apscheduler.schedulers": types.ModuleType("apscheduler.schedulers"),
                      "apscheduler.schedulers.blocking": blocking,
                      "apscheduler.triggers": types.ModuleType("apscheduler.triggers"),
                      "apscheduler.triggers.cron": cron}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "main", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(job, "_client_provider", None)    # main() sets it; restore afterwards
    mod = importlib.import_module("main")
    yield mod
    sys.modules.pop("main", None)


def run_main(main_mod, monkeypatch, cfg=None, sender=None):
    holder = []
    orig = FakeSched.__init__

    def init(self, timezone=None):
        orig(self, timezone)
        holder.append(self)

    monkeypatch.setattr(FakeSched, "__init__", init)
    monkeypatch.setattr(main_mod, "init_db", lambda: None)
    monkeypatch.setattr(main_mod, "setup_logging", lambda: None)
    monkeypatch.setattr(main_mod, "load_config", lambda *a: cfg or Config())
    the_sender = sender or FakeSender()
    monkeypatch.setattr(main_mod, "get_sender", lambda name: the_sender)
    main_mod.main()
    return holder[0], the_sender


def test_main_registers_discovery_job_with_shared_provider(main_mod, monkeypatch, fake_discovery):
    sched, sender = run_main(main_mod, monkeypatch, Config(rules=[Rule("a", "*", "20:00", 1, "f")]))
    assert set(sched.jobs) == {"a", "__reload__", "__discovery__", "__test_send__"}
    dj = sched.jobs["__discovery__"]
    assert dj.func is main_mod.discovery_tick and dj.trigger == "interval"
    assert dj.kwargs["seconds"] == 60
    assert dj.kwargs["max_instances"] == 1
    assert dj.kwargs["coalesce"] is True and dj.kwargs["replace_existing"] is True
    assert main_mod.DISCOVERY_JOB_ID == "__discovery__"
    # the provider is built once, from the sender, and shared with job.py
    assert fake_discovery.calls.provider == [sender]
    (get_client,) = dj.kwargs["args"]
    assert callable(get_client) and get_client() == "client"
    assert job._client_provider is get_client
    assert sched.started


def test_main_registers_discovery_job_even_when_discovery_is_disabled(main_mod, monkeypatch, fake_discovery):
    sched, _ = run_main(main_mod, monkeypatch, Config())
    assert "__discovery__" in sched.jobs


def test_main_survives_missing_discovery_module(main_mod, monkeypatch):
    monkeypatch.setitem(sys.modules, "reeldrop.discovery", None)    # import fails
    sched, _ = run_main(main_mod, monkeypatch, Config())
    assert sched.started
    assert sched.jobs["__discovery__"].kwargs["args"] == [None]
    assert job._client_provider is None
    main_mod.discovery_tick(None)                                   # harmless no-op


def test_discovery_tick_calls_tick_with_client(main_mod, fake_discovery):
    main_mod.discovery_tick(provider)
    assert fake_discovery.calls.tick == [provider]


def test_discovery_tick_swallows_and_logs_exceptions(main_mod, fake_discovery, caplog):
    fake_discovery.tick_error = RuntimeError("tick exploded")
    with caplog.at_level(logging.ERROR, logger="reeldrop"):
        main_mod.discovery_tick(provider)                           # must not raise
    assert any("tick exploded" in r.getMessage() for r in caplog.records)


def test_discovery_tick_swallows_import_errors(main_mod, monkeypatch):
    monkeypatch.setitem(sys.modules, "reeldrop.discovery", None)
    main_mod.discovery_tick(provider)                               # must not raise


# --- main.py: reload never removes the system jobs ---------------------------

def _rule_dict(name, time="08:00"):
    return {"name": name, "days": "*", "time": time, "count": 1, "recipient": "friend"}


def test_reload_never_removes_system_jobs(main_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "notify_me", lambda m: None)
    cfg0 = config_mod.parse_config({"rules": [_rule_dict("a"), _rule_dict("b")]})
    sched = FakeSched(timezone=cfg0.timezone)
    sender = FakeSender()
    main_mod.register_jobs(sched, cfg0, sender)
    sched.add_job(main_mod.reload_if_changed, "interval", id="__reload__", replace_existing=True)
    sched.add_job(main_mod.discovery_tick, "interval", id="__discovery__", replace_existing=True)

    path = tmp_path / "schedules.json"
    path.write_text("{}")
    state = main_mod.ReloadState(cfg=cfg0, mtime=None)
    new_cfg = config_mod.parse_config({"rules": []})
    assert main_mod.reload_if_changed(sched, state, str(path), sender,
                                      loader=lambda p: new_cfg) == "reloaded"
    assert set(sched.jobs) == {"__reload__", "__discovery__"}
    assert sched.removed == ["a", "b"]


def test_reload_skips_system_ids_even_if_a_rule_carried_one(main_mod, tmp_path, monkeypatch):
    """A rule named like a system job (rejected by config, but guard anyway) is never removed."""
    monkeypatch.setattr(main_mod, "notify_me", lambda m: None)
    sneaky = Rule("__discovery__", "*", "08:00", 1, "friend")
    state_cfg = Config(rules=[sneaky])
    sched = FakeSched(timezone=state_cfg.timezone)
    sched.add_job(main_mod.discovery_tick, "interval", id="__discovery__", replace_existing=True)
    path = tmp_path / "s.json"
    path.write_text("{}")
    state = main_mod.ReloadState(cfg=state_cfg, mtime=None)
    assert main_mod.reload_if_changed(sched, state, str(path), FakeSender(),
                                      loader=lambda p: Config()) == "reloaded"
    assert "__discovery__" in sched.jobs and sched.removed == []


@pytest.mark.parametrize("name", ["__discovery__", "__reload__"])
def test_register_jobs_refuses_a_rule_named_like_a_system_job(main_mod, name):
    sched = FakeSched()
    cfg = Config(rules=[Rule("ok", "*", "08:00", 1, "f"), Rule(name, "*", "09:00", 1, "f")])
    with pytest.raises(ValueError, match="reserved"):
        main_mod.register_jobs(sched, cfg, FakeSender())
    assert sched.jobs == {}                       # nothing half-registered


# --- CLI: reeldrop discover -----------------------------------------------------

@pytest.fixture
def cli(temp_db, monkeypatch, fake_discovery):
    """CLI harness with a configurable discovery section."""
    src = types.SimpleNamespace(type="hashtag", value="funny", mode="top", to=None)
    src2 = types.SimpleNamespace(type="collection", value="Funny", mode=None, to="pal")
    disc = types.SimpleNamespace(enabled=False, min_pending=15, fetch_per_source=10,
                                 max_per_refill=30, check_every_minutes=180, min_likes=0,
                                 delay_seconds=5, sources=[src, src2])
    holder = types.SimpleNamespace(disc=disc, error=None)

    def load():
        if holder.error:
            raise holder.error
        return types.SimpleNamespace(discovery=holder.disc)

    monkeypatch.setattr(config_mod, "load_config", load)
    return holder


def test_discover_sources_prints_sources_without_login(cli, fake_discovery, capsys):
    assert cli_main(["discover", "--sources"]) == 0
    out = capsys.readouterr().out
    assert "hashtag: funny (mode=top)" in out
    assert "collection: Funny -> pal" in out
    assert "min_pending: 15" in out and "check_every_minutes: 180" in out
    assert fake_discovery.calls.provider == [] and fake_discovery.calls.refill == []


def test_discover_sources_with_no_sources_exits_zero(cli, capsys):
    cli.disc.sources = []
    assert cli_main(["discover", "--sources"]) == 0
    assert "Sources: none" in capsys.readouterr().out


def test_discover_dry_run_calls_refill_and_prints_summary(cli, fake_discovery, capsys):
    fake_discovery.refill_result = types.SimpleNamespace(added=["a", "b", "c"], dry_run=True)
    assert cli_main(["discover", "--dry-run", "--limit", "5"]) == 0
    assert "SUMMARY added=3 dry_run=True" in capsys.readouterr().out
    ((disc, client, dry_run, limit),) = fake_discovery.calls.refill
    assert disc is cli.disc and client == "client" and dry_run is True and limit == 5
    assert len(fake_discovery.calls.provider) == 1     # own login, not tied to a sender


def test_discover_defaults_to_real_run_and_ignores_enabled_flag(cli, fake_discovery, capsys):
    assert cli.disc.enabled is False
    assert cli_main(["discover"]) == 0
    ((_, _, dry_run, limit),) = fake_discovery.calls.refill
    assert dry_run is False and limit is None
    assert "SUMMARY" in capsys.readouterr().out


def test_discover_without_sources_is_a_clear_error(cli, fake_discovery, capsys):
    cli.disc.sources = []
    assert cli_main(["discover"]) == 1
    assert "no discovery sources" in capsys.readouterr().out
    assert fake_discovery.calls.refill == []


def test_discover_without_discovery_config_is_a_clear_error(cli, capsys):
    cli.disc = None
    assert cli_main(["discover"]) == 1
    assert "no discovery sources" in capsys.readouterr().out


def test_discover_block_pauses_sending_and_exits_1(cli, fake_discovery, capsys):
    fake_discovery.refill_error = BlockedError("challenge_required")
    assert not settings.is_paused()
    assert cli_main(["discover"]) == 1
    assert settings.is_paused()
    assert "paused" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("exc", [ValueError("bad source"), RuntimeError("IG_USERNAME and IG_PASSWORD must be set")])
def test_discover_errors_exit_1(cli, fake_discovery, capsys, exc):
    fake_discovery.refill_error = exc
    assert cli_main(["discover"]) == 1
    assert f"Error: {exc}" in capsys.readouterr().out
    assert not settings.is_paused()


def test_discover_config_error_exits_1(cli, capsys):
    cli.error = ValueError("bad config")
    assert cli_main(["discover", "--sources"]) == 1
    assert "Error: bad config" in capsys.readouterr().out


def test_discover_rejects_non_positive_limit(cli, fake_discovery, capsys):
    assert cli_main(["discover", "--limit", "0"]) == 1
    assert "--limit" in capsys.readouterr().out
    assert fake_discovery.calls.refill == []
