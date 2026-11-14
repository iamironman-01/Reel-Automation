"""Form-style edits of schedules.yaml for the dashboard.

Every function takes the raw config mapping (what yaml.safe_load returns) and returns a
NEW mapping with one change applied; nothing here touches a file. The dashboard then turns
the result into text with to_yaml() and saves it through dashboard_helpers.save_schedules,
which validates with load_config before replacing the file, so a bad edit never lands.

Stdlib only at module level (PyYAML is imported lazily) so this can be unit-tested
without PyYAML. Comments in a hand-written schedules.yaml are not preserved once a form
saves it; the raw editor in the dashboard's Advanced section still allows hand edits.
"""
import copy
import re

from .config import (
    _DAY_ORDER,
    EXPLORE_VALUES,
    HASHTAG_MODES,
    PICK_MODES,
    SOURCE_TYPES,
    TIME_RE,
)

DAY_LABELS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
              "fri": "Fri", "sat": "Sat", "sun": "Sun"}
ALL_DAYS = tuple(_DAY_ORDER)

# What the "Add a source" form offers: label -> (type, fixed value or None)
SOURCE_CHOICES = {
    "Explore reels (Instagram picks)": ("explore", "reels"),
    "Reels feed of the sending account": ("explore", "feed"),
    "Creator (username)": ("user", None),
    "Saved collection (name)": ("collection", None),
    "Hashtag": ("hashtag", None),
}

DISCOVERY_NUMBER_FIELDS = ("min_pending", "fetch_per_source", "max_per_refill",
                           "check_every_minutes", "min_likes", "delay_seconds")

_NAME_SAFE_RE = re.compile(r"[^a-z0-9]+")


def _copy(data) -> dict:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("The schedule file is not a mapping; fix it in the Advanced editor")
    return copy.deepcopy(data)


def _rules(data: dict) -> list:
    rules = data.get("rules")
    if rules is None:
        rules = []
    if not isinstance(rules, list):
        raise ValueError("rules is not a list; fix it in the Advanced editor")
    data["rules"] = rules
    return rules


def _discovery(data: dict) -> dict:
    disc = data.get("discovery")
    if disc is None:
        disc = {}
    if not isinstance(disc, dict):
        raise ValueError("discovery is not a mapping; fix it in the Advanced editor")
    data["discovery"] = disc
    return disc


def _sources(disc: dict) -> list:
    sources = disc.get("sources")
    if sources is None:
        sources = []
    if not isinstance(sources, list):
        raise ValueError("discovery sources is not a list; fix it in the Advanced editor")
    disc["sources"] = sources
    return sources


def clean_username(name) -> str:
    """Trim a typed Instagram username and drop a leading @."""
    return str(name or "").strip().lstrip("@").strip()


# --- days ------------------------------------------------------------------------

def days_to_list(days) -> list[str]:
    """Expand a rule's days string ("*", "sat,sun", "mon-fri") into day names."""
    text = str(days or "*").replace(" ", "").lower()
    if text == "*":
        return list(ALL_DAYS)
    out = []
    for part in text.split(","):
        if "-" in part:
            first, _, last = part.partition("-")
            if first in ALL_DAYS and last in ALL_DAYS:
                for day in ALL_DAYS[ALL_DAYS.index(first):ALL_DAYS.index(last) + 1]:
                    if day not in out:
                        out.append(day)
        elif part in ALL_DAYS and part not in out:
            out.append(part)
    return [d for d in ALL_DAYS if d in out]


def days_from_list(days) -> str:
    """Turn selected day names into a rule's days string; all seven (or none) become "*"."""
    chosen = [d for d in ALL_DAYS if d in {str(x).strip().lower() for x in (days or [])}]
    if not chosen or len(chosen) == len(ALL_DAYS):
        return "*"
    return ",".join(chosen)


# --- friends (rules) -------------------------------------------------------------

def rule_rows(data) -> list[dict]:
    """The rules as plain dicts for a table (never raises on odd entries)."""
    rows = []
    for r in (data or {}).get("rules") or []:
        if not isinstance(r, dict):
            continue
        rows.append({
            "name": str(r.get("name", "")),
            "friend": str(r.get("recipient", "")),
            "time": str(r.get("time", "")),
            "days": str(r.get("days", "*")),
            "count": r.get("count", ""),
            "pick": str(r.get("pick") or "oldest"),
            "send_note": bool(r.get("send_note") or False),
        })
    return rows


def friends(data) -> list[str]:
    """Distinct recipients across all rules, in first-seen order."""
    seen = []
    for row in rule_rows(data):
        if row["friend"] and row["friend"] not in seen:
            seen.append(row["friend"])
    return seen


def make_rule_name(data, friend: str, time: str) -> str:
    """A unique, readable rule name such as "naveen_k_in_2000"."""
    base = _NAME_SAFE_RE.sub("_", clean_username(friend).lower()).strip("_") or "friend"
    base = f"{base}_{str(time).replace(':', '')}"
    taken = {row["name"] for row in rule_rows(data)}
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def _build_rule(name, friend, time, count, days, pick, send_note) -> dict:
    friend = clean_username(friend)
    if not friend:
        raise ValueError("Enter the friend's Instagram username")
    if not isinstance(time, str) or not TIME_RE.match(time):
        raise ValueError(f"Time must be HH:MM, for example 20:00 (got {time!r})")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("Reels per send must be a whole number of 1 or more")
    pick = str(pick or "oldest").strip().lower()
    if pick not in PICK_MODES:
        raise ValueError(f"Order must be one of {', '.join(PICK_MODES)}")
    rule = {"name": name, "days": days_from_list(days), "time": time,
            "count": count, "recipient": friend}
    if send_note:
        rule["send_note"] = True
    if pick != "oldest":
        rule["pick"] = pick
    return rule


def add_friend_rule(data, friend, time, count, days=None, pick="oldest",
                    send_note=False) -> dict:
    """Add a schedule for a friend. Returns the new config mapping."""
    out = _copy(data)
    rules = _rules(out)
    name = make_rule_name(out, friend, time)
    rules.append(_build_rule(name, friend, time, count, days, pick, send_note))
    return out


def update_rule(data, name, friend, time, count, days=None, pick="oldest",
                send_note=False) -> dict:
    """Replace the rule called `name` (its name is kept). Returns the new config mapping."""
    out = _copy(data)
    rules = _rules(out)
    for i, r in enumerate(rules):
        if isinstance(r, dict) and r.get("name") == name:
            rules[i] = _build_rule(name, friend, time, count, days, pick, send_note)
            return out
    raise ValueError(f"No schedule named {name!r}")


def remove_rule(data, name) -> dict:
    out = _copy(data)
    rules = _rules(out)
    kept = [r for r in rules if not (isinstance(r, dict) and r.get("name") == name)]
    if len(kept) == len(rules):
        raise ValueError(f"No schedule named {name!r}")
    out["rules"] = kept
    return out


def remove_friend(data, friend) -> dict:
    """Remove every schedule for a friend, and stop sources that were only for them."""
    friend = clean_username(friend)
    out = _copy(data)
    rules = _rules(out)
    kept = [r for r in rules
            if not (isinstance(r, dict) and clean_username(r.get("recipient")) == friend)]
    if len(kept) == len(rules):
        raise ValueError(f"No schedule for {friend!r}")
    out["rules"] = kept
    disc = out.get("discovery")
    if isinstance(disc, dict) and isinstance(disc.get("sources"), list):
        for s in disc["sources"]:
            if isinstance(s, dict) and clean_username(s.get("to")) == friend:
                s.pop("to", None)       # keep the source; it now feeds every friend
    return out


# --- limits ----------------------------------------------------------------------

def set_limits(data, daily_cap, gap_seconds, quiet_days=None) -> dict:
    """Set the daily cap, the gap between messages and (optionally) the quiet days."""
    if isinstance(daily_cap, bool) or not isinstance(daily_cap, int) or daily_cap < 1:
        raise ValueError("Daily cap must be a whole number of 1 or more")
    if isinstance(gap_seconds, bool) or not isinstance(gap_seconds, int) or gap_seconds < 0:
        raise ValueError("Gap must be a whole number of seconds, 0 or more")
    out = _copy(data)
    out["daily_cap"] = daily_cap
    out["gap_seconds"] = gap_seconds
    if quiet_days is not None:
        days = sorted({str(d).strip() for d in quiet_days if str(d).strip()})
        if days:
            out["quiet_days"] = days
        else:
            out.pop("quiet_days", None)
    return out


# --- discovery -------------------------------------------------------------------

def source_rows(data) -> list[dict]:
    disc = (data or {}).get("discovery")
    rows = []
    for s in (disc.get("sources") if isinstance(disc, dict) else None) or []:
        if not isinstance(s, dict):
            continue
        stype = str(s.get("type", "")).strip().lower()
        value = s.get("value")
        if stype == "explore" and value is None:
            value = EXPLORE_VALUES[0]
        rows.append({"type": stype, "value": str(value or ""),
                     "mode": str(s.get("mode") or "-") if stype == "hashtag" else "-",
                     "to": str(s.get("to") or "-")})
    return rows


def source_label(row: dict) -> str:
    """Short text for one source row, e.g. "user: natgeo" or "explore: reels -> friend"."""
    text = f"{row['type']}: {row['value']}"
    if row.get("mode") not in (None, "-"):
        text += f" ({row['mode']})"
    if row.get("to") not in (None, "-"):
        text += f" -> {row['to']}"
    return text


def add_source(data, stype, value=None, mode=None, to=None) -> dict:
    """Add one discovery source. Returns the new config mapping."""
    stype = str(stype or "").strip().lower()
    if stype not in SOURCE_TYPES:
        raise ValueError(f"Source type must be one of {', '.join(SOURCE_TYPES)}")
    value = str(value or "").strip()
    if stype == "explore":
        value = (value or EXPLORE_VALUES[0]).lower()
        if value not in EXPLORE_VALUES:
            raise ValueError(f"Explore value must be one of {', '.join(EXPLORE_VALUES)}")
    elif stype == "hashtag":
        value = value.lstrip("#").strip()
    elif stype == "user":
        value = clean_username(value)
    if not value:
        raise ValueError("Enter a value for the source")
    out = _copy(data)
    sources = _sources(_discovery(out))
    for s in sources:
        if (isinstance(s, dict) and str(s.get("type", "")).strip().lower() == stype
                and str(s.get("value") or (EXPLORE_VALUES[0] if stype == "explore" else ""))
                .strip().lstrip("#@").casefold() == value.casefold()):
            raise ValueError(f"The source {stype}: {value} is already in the list")
    source = {"type": stype, "value": value}
    if stype == "hashtag":
        mode = str(mode or "top").strip().lower()
        if mode not in HASHTAG_MODES:
            raise ValueError(f"Hashtag mode must be one of {', '.join(HASHTAG_MODES)}")
        source["mode"] = mode
    to = clean_username(to)
    if to:
        source["to"] = to
    sources.append(source)
    return out


def remove_source(data, index: int) -> dict:
    """Remove the discovery source at `index` (position in source_rows)."""
    out = _copy(data)
    disc = _discovery(out)
    sources = _sources(disc)
    positions = [i for i, s in enumerate(sources) if isinstance(s, dict)]
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(positions):
        raise ValueError("That source no longer exists; reload the page")
    del sources[positions[index]]
    if not sources and disc.get("enabled"):
        disc["enabled"] = False         # enabled with no sources is invalid
    return out


def parse_keywords(text) -> list[str]:
    """Split typed keywords (commas or new lines) into a clean, de-duplicated list."""
    out = []
    for piece in re.split(r"[,\n]", str(text or "")):
        word = " ".join(piece.split()).lstrip("#").lower()
        if word and word not in out:
            out.append(word)
    return out


def set_discovery(data, enabled, numbers: dict | None = None, keywords=None,
                  exclude_keywords=None) -> dict:
    """Turn discovery on or off and set its numbers and topic keywords."""
    out = _copy(data)
    disc = _discovery(out)
    if enabled and not _sources(disc):
        raise ValueError("Add at least one source before turning automatic discovery on")
    disc["enabled"] = bool(enabled)
    for key, value in (numbers or {}).items():
        if key not in DISCOVERY_NUMBER_FIELDS:
            raise ValueError(f"Unknown discovery setting {key!r}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be a whole number")
        disc[key] = value
    for key, words in (("keywords", keywords), ("exclude_keywords", exclude_keywords)):
        if words is None:
            continue
        words = parse_keywords(words) if isinstance(words, str) else parse_keywords("\n".join(words))
        if words:
            disc[key] = words
        else:
            disc.pop(key, None)
    return out


# --- text ------------------------------------------------------------------------

def from_yaml(text: str) -> dict:
    import yaml  # lazy: keep this module importable without PyYAML

    data = yaml.safe_load(text or "")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("The schedule file is not a mapping; fix it in the Advanced editor")
    return data


def to_yaml(data) -> str:
    """Config mapping -> YAML text. Times such as "20:00" come out quoted."""
    import yaml  # lazy: keep this module importable without PyYAML

    return yaml.safe_dump(data or {}, sort_keys=False, default_flow_style=False,
                          allow_unicode=True)
