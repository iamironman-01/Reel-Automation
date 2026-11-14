import importlib
import os
import sys
import logging
import time
from dataclasses import dataclass, replace
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

load_dotenv()   # must run before the reeldrop imports below read the environment

from reeldrop.config import RELOAD_JOB_ID, ensure_schedules_file, load_config, schedules_path
from reeldrop.db import init_db
from reeldrop.job import run_rule, set_client_provider
from reeldrop.notify import notify_me
from reeldrop.senders import get_sender

log = logging.getLogger("reeldrop")

CONFIG_PATH = schedules_path()   # SCHEDULES_PATH env, default config/schedules.yaml
RELOAD_INTERVAL_SECONDS = 60
DISCOVERY_JOB_ID = "__discovery__"
DISCOVERY_INTERVAL_SECONDS = 60
TEST_SEND_JOB_ID = "__test_send__"
TEST_SEND_INTERVAL_SECONDS = 20
# Scheduler jobs that are not rule jobs; reload and rule registration must never touch them.
SYSTEM_JOB_IDS = frozenset({RELOAD_JOB_ID, DISCOVERY_JOB_ID, TEST_SEND_JOB_ID})


def setup_logging():
    Path("data").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler("data/reeldrop.log", maxBytes=1_000_000, backupCount=3),
                  logging.StreamHandler()],
    )


def register_jobs(sched, cfg, sender):
    """Add (or replace) one cron job per rule on the scheduler."""
    # Build every trigger first so a bad rule cannot leave the schedule half-updated.
    planned = []
    for rule in cfg.rules:
        if rule.name in SYSTEM_JOB_IDS:   # would overwrite a system job
            raise ValueError(f"Rule name {rule.name!r} is reserved")
        hour, minute = rule.time.split(":")
        trigger = CronTrigger(day_of_week=rule.days, hour=int(hour), minute=int(minute),
                              timezone=cfg.timezone)
        planned.append((rule, trigger))
    for rule, trigger in planned:
        sched.add_job(
            run_rule,
            trigger,
            args=[rule, cfg, sender],
            id=rule.name,
            replace_existing=True,    # live reload re-registers the same ids
            misfire_grace_time=600,   # if the PC was busy, still run up to 10 min late
            coalesce=True,            # never run the same missed job twice
            max_instances=1,
        )
        log.info("Scheduled %s: %s at %s → %d reels to %s",
                 rule.name, rule.days, rule.time, rule.count, rule.recipient)


# --- live schedule reload ----------------------------------------------------

@dataclass
class ReloadState:
    """What the reload job remembers between runs."""
    cfg: object                  # the config the scheduler is running with
    mtime: int | None = None     # mtime_ns of the file last handled (good or bad); None = missing


def file_mtime(path):
    """Modification time of path in nanoseconds, or None if it cannot be read."""
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _describe_changes(old, new) -> str:
    old_rules = {r.name: r for r in old.rules}
    new_rules = {r.name: r for r in new.rules}
    parts = []
    added = [n for n in new_rules if n not in old_rules]
    removed = [n for n in old_rules if n not in new_rules]
    changed = [n for n in new_rules if n in old_rules and new_rules[n] != old_rules[n]]
    if added:
        parts.append("added " + ", ".join(added))
    if removed:
        parts.append("removed " + ", ".join(removed))
    if changed:
        parts.append("changed " + ", ".join(changed))
    for attr in ("daily_cap", "gap_seconds", "quiet_days"):
        if getattr(old, attr) != getattr(new, attr):
            parts.append(f"{attr} {getattr(old, attr)!r} → {getattr(new, attr)!r}")
    return "; ".join(parts) or "no changes to rules or limits"


def reload_if_changed(sched, state, path, sender, loader=None):
    """Re-register the rule jobs if the schedules file changed since the last check.

    Returns "unchanged", "reloaded" or "invalid". Never raises. An invalid file leaves
    the running schedule alone; its mtime is remembered so it is reported only once.
    """
    mtime = file_mtime(path)
    if mtime == state.mtime:
        return "unchanged"
    state.mtime = mtime            # handled either way: a broken file is not retried every minute

    try:
        if mtime is None:
            raise FileNotFoundError(f"{path} is missing or unreadable")
        new_cfg = (loader or load_config)(path)
    except Exception as e:
        log.error("Ignoring changed %s, keeping the running schedule: %s: %s",
                  path, type(e).__name__, e)
        notify_me(f"ReelDrop: {path} could not be reloaded; the running schedule is unchanged. "
                  f"{type(e).__name__}: {e}")
        return "invalid"

    if new_cfg.timezone != state.cfg.timezone:
        log.warning("timezone changed to %s in %s but the scheduler runs in %s; "
                    "restart the scheduler to apply it (other changes are applied now)",
                    new_cfg.timezone, path, state.cfg.timezone)
        new_cfg = replace(new_cfg, timezone=state.cfg.timezone)

    try:
        register_jobs(sched, new_cfg, sender)    # add or replace (all triggers are built first)
        names = {r.name for r in new_cfg.rules}
        present = {j.id for j in sched.get_jobs()}
        for rule in state.cfg.rules:             # only ever rule jobs, never the system jobs
            if rule.name in SYSTEM_JOB_IDS:
                continue
            if rule.name not in names and rule.name in present:
                sched.remove_job(rule.name)
    except Exception as e:
        log.error("Could not apply the reloaded schedule: %s: %s", type(e).__name__, e)
        notify_me(f"ReelDrop: reloading {path} failed part-way ({type(e).__name__}: {e}). "
                  "Restart the scheduler to be safe.")
        return "invalid"

    log.info("Reloaded %s: %s", path, _describe_changes(state.cfg, new_cfg))
    state.cfg = new_cfg
    return "reloaded"


def discovery_tick(get_client):
    """Scheduler job: let auto-discovery top up the queue. Never raises.

    Cheap no-op while discovery is disabled; the module is imported lazily so a
    missing or broken one cannot stop the scheduler.
    """
    if get_client is None:
        return
    try:
        discovery = importlib.import_module("reeldrop.discovery")
        discovery.tick(get_client)
    except Exception as e:
        log.error("Auto-discovery tick failed: %s: %s", type(e).__name__, e)


def build_client_provider(sender):
    """One shared client provider for the discovery job and the rule jobs, or None."""
    try:
        discovery = importlib.import_module("reeldrop.discovery")
        return discovery.make_client_provider(sender)
    except Exception as e:
        log.warning("Auto-discovery unavailable: %s: %s", type(e).__name__, e)
        return None


# --- "Send a test reel to every friend" (dashboard button) ------------------------
# The dashboard never logs in to Instagram. Its button only sets a flag; this job sends.
TEST_SEND_REQUEST_KEY = "test_send_requested"
TEST_SEND_RESULT_KEY = "test_send_result"
TEST_SEND_AT_KEY = "test_send_at"
TEST_RULE_NAME = "test_send"                   # shows up in the send history

_RESULT_TEXT = {
    "paused": "skipped, sending is paused",
    "quiet": "skipped, today is a quiet day",
    "cap": "skipped, the daily cap is reached",
    "empty": "no reel waiting for this friend",
}


def describe_run(result) -> str:
    result = result or {}
    status = result.get("status")
    if status in ("sent", "blocked"):
        text = f"sent {result.get('sent', 0)}/{result.get('total', 0)}"
        return text + (" - Instagram blocked the account, sending is paused"
                       if status == "blocked" else "")
    return _RESULT_TEXT.get(status, "not run")


def friends_in(cfg) -> list:
    """Each recipient of a rule once, in the order the rules list them."""
    seen = []
    for rule in cfg.rules:
        if rule.recipient not in seen:
            seen.append(rule.recipient)
    return seen


def send_test_reels(cfg, sender, runner=None, sleep=None, paused=None) -> str:
    """Send one waiting reel to every friend now, through the normal rule runner.

    Pause, quiet days, the daily cap, the duplicate guard and block handling all apply,
    exactly as for a scheduled drop. Returns a one-line summary.
    """
    from reeldrop.config import Rule
    from reeldrop.settings import is_paused

    runner = runner or run_rule
    sleep = sleep or time.sleep
    paused = paused or is_paused
    friends = friends_in(cfg)
    if not friends:
        return "Not run: there are no friends in the schedule"
    parts = []
    for i, friend in enumerate(friends):
        if i:
            if paused():
                parts.append("stopped: sending is paused")
                break
            sleep(cfg.gap_seconds)
        rule = Rule(name=TEST_RULE_NAME, days="*", time="00:00", count=1, recipient=friend)
        parts.append(f"@{friend}: {describe_run(runner(rule, cfg, sender))}")
    return "; ".join(parts)


def check_test_send_request(sender, path=None, loader=None, runner=None, sleep=None):
    """Scheduler job: if the dashboard asked for a test send, run it once and record it."""
    from reeldrop.db import now
    from reeldrop.settings import get, set_

    try:
        if get(TEST_SEND_REQUEST_KEY, "0") != "1":
            return None
        set_(TEST_SEND_REQUEST_KEY, "0")      # one press, one run, even if it fails
    except Exception as e:
        log.warning("Could not read the test send request: %s", e)
        return None
    try:
        cfg = (loader or load_config)(path or CONFIG_PATH)
    except Exception as e:
        summary = f"Not run: the schedule could not be loaded ({e})"
    else:
        log.info("Test send requested from the dashboard")
        try:
            summary = send_test_reels(cfg, sender, runner=runner, sleep=sleep)
        except Exception as e:
            summary = f"Failed: {type(e).__name__}: {e}"
    log.info("Test send: %s", summary)
    try:
        set_(TEST_SEND_RESULT_KEY, summary)
        set_(TEST_SEND_AT_KEY, now())
    except Exception as e:
        log.warning("Could not record the test send result: %s", e)
    return summary


SENDER_ERROR_KEY = "sender_error"            # read by the dashboard header
SENDER_ERROR_AT_KEY = "sender_error_at"
DEFAULT_LOGIN_RETRY_MINUTES = 60


def record_sender_error(message: str) -> None:
    """Store (or clear, with "") the reason the sender could not start. Never raises."""
    try:
        from reeldrop.db import now
        from reeldrop.settings import set_
        set_(SENDER_ERROR_KEY, message)
        set_(SENDER_ERROR_AT_KEY, now() if message else "")
    except Exception as e:
        log.warning("Could not record the sender status: %s", e)


def login_retry_seconds(env=None) -> int:
    """How long to wait after a failed Instagram login before the process exits.

    start.py restarts the scheduler within minutes when it exits. For the Instagram
    sender that would mean a new login attempt every few minutes, and repeated failed
    logins get an account flagged. LOGIN_RETRY_MINUTES (default 60, 0 = no wait).
    """
    raw = (env if env is not None else os.environ).get("LOGIN_RETRY_MINUTES", "")
    try:
        minutes = int(str(raw).strip()) if str(raw).strip() else DEFAULT_LOGIN_RETRY_MINUTES
    except ValueError:
        minutes = DEFAULT_LOGIN_RETRY_MINUTES
    return max(0, min(minutes, 24 * 60)) * 60


SENDER_RETRY_KEY = "sender_retry_requested"  # set by the dashboard's "Retry login now" button
_RETRY_POLL_SECONDS = 5


def retry_requested() -> bool:
    """True once if the dashboard asked for an immediate retry (the flag is cleared)."""
    try:
        from reeldrop.settings import get, set_
        if get(SENDER_RETRY_KEY, "0") == "1":
            set_(SENDER_RETRY_KEY, "0")
            return True
    except Exception:
        pass
    return False


def wait_before_exit(sender_name: str, sleep=time.sleep, env=None, requested=None) -> int:
    """Pause after a failed Instagram login so restarts do not hammer Instagram.

    Returns the seconds waited. The wait ends early when the dashboard's
    "Retry login now" button is pressed.
    """
    seconds = login_retry_seconds(env) if sender_name == "instagrapi" else 0
    if not seconds:
        return 0
    requested = requested or retry_requested
    requested()                       # drop a stale request from before this failure
    log.error("Waiting up to %d minutes before exiting, so the next login attempt is not "
              "too soon. Fix the problem shown above, then press 'Retry login now' in the "
              "dashboard (Settings) or restart the service.", seconds // 60)
    waited = 0
    while waited < seconds:
        sleep(_RETRY_POLL_SECONDS)
        waited += _RETRY_POLL_SECONDS
        if requested():
            log.info("Retry requested from the dashboard; exiting now so the login is retried")
            break
    return waited


def main():
    setup_logging()
    init_db()
    try:
        ensure_schedules_file()       # first start on a fresh disk: seed from the shipped default
    except OSError as e:
        log.error("Cannot prepare %s: %s", CONFIG_PATH, e)
        sys.exit(1)
    mtime = file_mtime(CONFIG_PATH)   # read before loading so an edit in between is not missed
    try:
        cfg = load_config()
    except Exception as e:
        log.error("Invalid config: %s", e)
        sys.exit(1)

    sender_name = os.getenv("SENDER", "dry_run")
    try:
        sender = get_sender(sender_name)
    except Exception as e:
        # e.g. login failure or missing credentials; the message never includes passwords
        log.error("Could not start sender %r: %s: %s", sender_name, type(e).__name__, e)
        notify_me(f"ReelDrop could not start (sender {sender_name}): {type(e).__name__}: {e}")
        record_sender_error(f"{type(e).__name__}: {e}")
        wait_before_exit(sender_name)
        sys.exit(1)
    record_sender_error("")           # started fine: clear any old error shown in the dashboard

    sched = BlockingScheduler(timezone=cfg.timezone)
    register_jobs(sched, cfg, sender)
    sched.add_job(
        reload_if_changed,
        "interval",
        seconds=RELOAD_INTERVAL_SECONDS,
        args=[sched, ReloadState(cfg=cfg, mtime=mtime), CONFIG_PATH, sender],
        id=RELOAD_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    # Always registered: it no-ops while discovery is disabled, and this way enabling
    # discovery from the dashboard takes effect through the live reload, no restart.
    get_client = build_client_provider(sender)
    set_client_provider(get_client)
    sched.add_job(
        discovery_tick,
        "interval",
        seconds=DISCOVERY_INTERVAL_SECONDS,
        args=[get_client],
        id=DISCOVERY_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    sched.add_job(
        check_test_send_request,
        "interval",
        seconds=TEST_SEND_INTERVAL_SECONDS,
        args=[sender],
        id=TEST_SEND_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    if not cfg.rules:
        log.warning("No rules in %s — nothing will be scheduled", CONFIG_PATH)
    if os.getenv("TELEGRAM_CONTROL", "0") == "1":
        # Opt-in: lets you run /add /pause /resume /status from your phone.
        from reeldrop.telegram_control import start_in_thread
        start_in_thread()
    log.info("Sender: %s. Waiting… (Ctrl+C to stop)", sender.name)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down")
        try:
            sched.shutdown(wait=False)
        except Exception:
            pass   # scheduler was not running


if __name__ == "__main__":
    main()
