# Phase 2 — Scheduler (Dry-Run)

**Goal:** run jobs exactly at the times in `schedules.yaml`, without sending anything real yet.
**Time:** 2–3 days

## Checklist

- [ ] `config.py` — load + validate YAML
- [ ] `senders/base.py`, `senders/dry_run.py`, `senders/__init__.py`
- [ ] `job.py` — daily cap → next N reels → send → mark
- [ ] `main.py` — one CronTrigger per rule
- [ ] Tested with a rule 2 minutes in the future

## reeldrop/config.py

```python
import re
from dataclasses import dataclass, field
import yaml

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

@dataclass
class Rule:
    name: str
    days: str
    time: str
    count: int
    recipient: str

@dataclass
class Config:
    timezone: str = "Asia/Kolkata"
    daily_cap: int = 10
    gap_seconds: int = 45
    rules: list[Rule] = field(default_factory=list)

def load_config(path: str = "config/schedules.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    rules = []
    for r in data.get("rules", []):
        rule = Rule(name=r["name"], days=str(r.get("days", "*")), time=str(r["time"]),
                    count=int(r["count"]), recipient=r["recipient"])
        if not TIME_RE.match(rule.time):
            raise ValueError(f"Rule {rule.name}: time must be HH:MM in quotes, got {rule.time}")
        if rule.count < 1:
            raise ValueError(f"Rule {rule.name}: count must be at least 1")
        rules.append(rule)
    return Config(
        timezone=data.get("timezone", "Asia/Kolkata"),
        daily_cap=int(data.get("daily_cap", 10)),
        gap_seconds=int(data.get("gap_seconds", 45)),
        rules=rules,
    )
```

## reeldrop/senders/base.py

```python
class BlockedError(Exception):
    """Instagram blocked/challenged us — stop everything."""

class Sender:
    name = "base"
    def send(self, recipient: str, reel_url: str) -> None:
        """Deliver one reel. Raise BlockedError or another exception on failure."""
        raise NotImplementedError
```

## reeldrop/senders/dry_run.py

```python
from .base import Sender

class DryRunSender(Sender):
    name = "dry_run"
    def send(self, recipient, reel_url):
        print(f"[DRY RUN] would send {reel_url} to {recipient}")
```

## reeldrop/senders/__init__.py

```python
from .base import Sender, BlockedError

def get_sender(name: str) -> Sender:
    if name == "dry_run":
        from .dry_run import DryRunSender; return DryRunSender()
    if name == "instagrapi":
        from .instagrapi_sender import InstagrapiSender; return InstagrapiSender()
    if name == "official":
        from .official_api_sender import OfficialApiSender; return OfficialApiSender()
    if name == "telegram":
        from .telegram_sender import TelegramSender; return TelegramSender()
    raise ValueError(f"Unknown sender: {name}")
```

## reeldrop/job.py  (first version — Phase 4 upgrades it)

```python
import time
from .queue import next_pending, mark_sent, mark_failed, sent_today

def run_rule(rule, cfg, sender):
    remaining = cfg.daily_cap - sent_today()
    if remaining <= 0:
        print(f"[{rule.name}] daily cap reached, skipping")
        return
    reels = next_pending(min(rule.count, remaining))
    if not reels:
        print(f"[{rule.name}] queue is empty")
        return
    for i, reel in enumerate(reels):
        try:
            sender.send(rule.recipient, reel["url"])
            mark_sent(reel["id"], rule.recipient, rule.name, sender.name)
        except Exception as e:
            mark_failed(reel["id"], rule.recipient, rule.name, sender.name, str(e))
        if i < len(reels) - 1:
            time.sleep(cfg.gap_seconds)
```

## main.py

```python
import os
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

load_dotenv()

from reeldrop.config import load_config
from reeldrop.db import init_db
from reeldrop.job import run_rule
from reeldrop.senders import get_sender

def main():
    init_db()
    cfg = load_config()
    sender = get_sender(os.getenv("SENDER", "dry_run"))
    sched = BlockingScheduler(timezone=cfg.timezone)
    for rule in cfg.rules:
        hour, minute = rule.time.split(":")
        sched.add_job(
            run_rule,
            CronTrigger(day_of_week=rule.days, hour=int(hour), minute=int(minute),
                        timezone=cfg.timezone),
            args=[rule, cfg, sender],
            id=rule.name,
            misfire_grace_time=600,   # if the PC was busy, still run up to 10 min late
            coalesce=True,            # never run the same missed job twice
            max_instances=1,
        )
        print(f"Scheduled {rule.name}: {rule.days} at {rule.time} → {rule.count} reels to {rule.recipient}")
    print(f"Sender: {sender.name}. Waiting… (Ctrl+C to stop)")
    sched.start()

if __name__ == "__main__":
    main()
```

## Test it

1. Add 5 reels with the CLI.
2. In `schedules.yaml`, set a test rule 2 minutes from now, `count: 2`, `gap_seconds: 5`.
3. `python main.py` with `SENDER=dry_run`.
4. At that minute you should see two `[DRY RUN]` lines, and `python -m reeldrop list` shows them as `sent`.

✅ **Done when:** jobs fire at the right IST minute and reels are marked sent in dry-run mode.
