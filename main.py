import os
import sys
import logging
from dataclasses import dataclass, replace
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

load_dotenv()   # must run before the reeldrop imports below read the environment

from reeldrop.config import RELOAD_JOB_ID, load_config
from reeldrop.db import init_db
from reeldrop.job import run_rule
from reeldrop.notify import notify_me
from reeldrop.senders import get_sender

log = logging.getLogger("reeldrop")

CONFIG_PATH = "config/schedules.yaml"
RELOAD_INTERVAL_SECONDS = 60


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
        for rule in state.cfg.rules:             # only ever rule jobs, never the reload job
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


def main():
    setup_logging()
    init_db()
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
        sys.exit(1)

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
    if not cfg.rules:
        log.warning("No rules in config/schedules.yaml — nothing will be scheduled")
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
