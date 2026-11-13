import datetime
import os
import re
import shutil
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_RE = r"(?:mon|tue|wed|thu|fri|sat|sun)"
# "*", or comma-separated days / day ranges: "sat,sun", "mon-fri", "mon-wed,sat"
_DAYS_RE = re.compile(rf"^{_DAY_RE}(?:-{_DAY_RE})?(?:,{_DAY_RE}(?:-{_DAY_RE})?)*$")


# Scheduler job id of the live-reload job; a rule may not use it.
RELOAD_JOB_ID = "__reload__"

PICK_MODES = ("oldest", "random")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Rule:
    name: str
    days: str
    time: str
    count: int
    recipient: str
    send_note: bool = False     # send reels.note as a text message before each reel
    pick: str = "oldest"        # "oldest" or "random"


@dataclass
class Config:
    timezone: str = "Asia/Kolkata"
    daily_cap: int = 10
    gap_seconds: int = 45
    rules: list[Rule] = field(default_factory=list)
    quiet_days: list[str] = field(default_factory=list)   # ISO dates (YYYY-MM-DD) with no sends


def _as_int(value, label: str) -> int:
    """Convert a YAML value to int, rejecting bools and non-integral numbers."""
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise ValueError(f"{label} must be a whole number, got {value!r}")


def _validate_days(days, label: str) -> str:
    if not isinstance(days, str):
        raise ValueError(f"{label}: days must be '*' or a list like \"mon-fri\" or \"sat,sun\", got {days!r}")
    normalized = days.replace(" ", "").lower()
    if normalized == "*":
        return "*"
    if not _DAYS_RE.match(normalized):
        raise ValueError(
            f"{label}: invalid days {days!r}. Use '*' or day names mon..sun, "
            f"e.g. \"mon-fri\" or \"sat,sun\""
        )
    for part in normalized.split(","):
        if "-" in part:
            first, last = part.split("-")
            if _DAY_ORDER.index(first) > _DAY_ORDER.index(last):
                raise ValueError(
                    f"{label}: invalid days range {part!r} (start must not come after end; "
                    f"write it as a comma list instead, e.g. \"sat,sun,mon\")"
                )
    return normalized


def _parse_rule(r, index: int, seen_names: set) -> Rule:
    if not isinstance(r, dict):
        raise ValueError(f"Rule #{index}: each rule must be a mapping with name/time/count/recipient")
    missing = [k for k in ("name", "time", "count", "recipient") if r.get(k) in (None, "")]
    if missing:
        label = r.get("name") or f"#{index}"
        raise ValueError(f"Rule {label}: missing required key(s): {', '.join(missing)}")

    name = r["name"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"Rule #{index}: name must be a non-empty string, got {name!r}")
    name = name.strip()
    if name in seen_names:
        raise ValueError(f"Rule {name}: duplicate rule name (names must be unique)")
    if name == RELOAD_JOB_ID:
        raise ValueError(f"Rule name {RELOAD_JOB_ID!r} is reserved")
    seen_names.add(name)
    label = f"Rule {name}"

    raw_time = r["time"]
    if not isinstance(raw_time, str) or not TIME_RE.match(raw_time):
        # An unquoted `time: 20:00` is parsed by YAML as the integer 1200.
        raise ValueError(f"{label}: time must be HH:MM in quotes (e.g. \"20:00\"), got {raw_time}")

    count = _as_int(r["count"], f"{label}: count")
    if count < 1:
        raise ValueError(f"{label}: count must be at least 1")

    recipient = r["recipient"]
    if not isinstance(recipient, str) or not recipient.strip():
        raise ValueError(f"{label}: recipient must be a non-empty string, got {recipient!r}")

    days = _validate_days(r.get("days", "*"), label)

    send_note = r.get("send_note", False)
    if send_note is None:
        send_note = False
    if not isinstance(send_note, bool):
        raise ValueError(f"{label}: send_note must be true or false, got {send_note!r}")

    pick = r.get("pick", "oldest")
    if pick is None:
        pick = "oldest"
    if not isinstance(pick, str) or pick.strip().lower() not in PICK_MODES:
        raise ValueError(f"{label}: pick must be one of {', '.join(PICK_MODES)}, got {pick!r}")

    return Rule(name=name, days=days, time=raw_time, count=count,
                recipient=recipient.strip(), send_note=send_note, pick=pick.strip().lower())


def _parse_quiet_days(raw) -> list[str]:
    """Normalize quiet_days to a sorted, de-duplicated list of ISO date strings.

    YAML turns an unquoted 2026-12-25 into a datetime.date, so both are accepted.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"quiet_days must be a list of dates like \"2026-12-25\", got {raw!r}")
    days = set()
    for item in raw:
        if isinstance(item, datetime.datetime):
            raise ValueError(f"quiet_days: expected a date (YYYY-MM-DD), got a date with time {item!r}")
        if isinstance(item, datetime.date):
            days.add(item.isoformat())
            continue
        if isinstance(item, str) and _ISO_DATE_RE.match(item.strip()):
            try:
                days.add(datetime.date.fromisoformat(item.strip()).isoformat())
                continue
            except ValueError:
                pass
        raise ValueError(f"quiet_days: invalid date {item!r}, use YYYY-MM-DD such as \"2026-12-25\"")
    return sorted(days)


def parse_config(data) -> Config:
    """Validate an already-loaded YAML mapping and build a Config."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("Config must be a YAML mapping (timezone, daily_cap, gap_seconds, rules)")

    timezone = data.get("timezone", "Asia/Kolkata")
    if not isinstance(timezone, str):
        raise ValueError(f"timezone must be a string like Asia/Kolkata, got {timezone!r}")
    try:
        ZoneInfo(timezone)
    except Exception as e:  # ZoneInfoNotFoundError (a KeyError) or ValueError for bad keys
        raise ValueError(f"Unknown timezone {timezone!r}") from e

    daily_cap = _as_int(data.get("daily_cap", 10), "daily_cap")
    if daily_cap < 1:
        raise ValueError("daily_cap must be at least 1")
    gap_seconds = _as_int(data.get("gap_seconds", 45), "gap_seconds")
    if gap_seconds < 0:
        raise ValueError("gap_seconds must not be negative")

    raw_rules = data.get("rules")
    if raw_rules is None:
        raw_rules = []
    if not isinstance(raw_rules, list):
        raise ValueError("rules must be a list")
    seen: set = set()
    rules = [_parse_rule(r, i, seen) for i, r in enumerate(raw_rules, start=1)]
    quiet_days = _parse_quiet_days(data.get("quiet_days"))
    return Config(timezone=timezone, daily_cap=daily_cap, gap_seconds=gap_seconds,
                  rules=rules, quiet_days=quiet_days)


DEFAULT_SCHEDULES_PATH = "config/schedules.yaml"


def schedules_path() -> str:
    """Where the live schedule file is. Set SCHEDULES_PATH to keep it on a persistent disk."""
    return os.environ.get("SCHEDULES_PATH") or DEFAULT_SCHEDULES_PATH


def ensure_schedules_file(default: str = DEFAULT_SCHEDULES_PATH) -> str:
    """Make sure the live schedule file exists, seeding it from the repo default.

    On a host where the image is rebuilt on every deploy (e.g. Render), edits made in
    the dashboard must live on the persistent disk, not inside the image. The first
    start copies the shipped default there; later starts leave the edited file alone.
    """
    target = schedules_path()
    if not os.path.exists(target) and os.path.abspath(target) != os.path.abspath(default):
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        shutil.copyfile(default, target)
    return target


def load_config(path: str | None = None) -> Config:
    import yaml  # imported here so parse_config() works without PyYAML installed

    with open(path or schedules_path(), encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return parse_config(data)
