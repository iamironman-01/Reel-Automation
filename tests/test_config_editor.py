"""config_editor: the dashboard's form edits of the schedule file (pure functions)."""
import pytest

from reeldrop import config_editor as ed
from reeldrop.config import parse_config

BASE = {
    "timezone": "Asia/Kolkata",
    "daily_cap": 10,
    "gap_seconds": 45,
    "rules": [
        {"name": "evening_drop", "days": "*", "time": "20:00", "count": 3, "recipient": "pal"},
        {"name": "weekend", "days": "sat,sun", "time": "11:00", "count": 5, "recipient": "pal",
         "pick": "random", "send_note": True},
    ],
}


def valid(data):
    """Every edit must leave a config that the real validator accepts."""
    return parse_config(data)


# --- days --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("*", list(ed.ALL_DAYS)),
    ("sat,sun", ["sat", "sun"]),
    ("mon-fri", ["mon", "tue", "wed", "thu", "fri"]),
    ("sun, mon-tue", ["mon", "tue", "sun"]),
    ("", list(ed.ALL_DAYS)),
    ("nonsense", []),
])
def test_days_to_list(text, expected):
    assert ed.days_to_list(text) == expected


@pytest.mark.parametrize("days,expected", [
    (list(ed.ALL_DAYS), "*"), ([], "*"), (None, "*"),
    (["sun", "sat"], "sat,sun"), (["MON", " wed "], "mon,wed"), (["xyz", "fri"], "fri"),
])
def test_days_from_list(days, expected):
    assert ed.days_from_list(days) == expected


def test_days_round_trip_is_accepted_by_the_validator():
    data = ed.add_friend_rule({}, "pal", "09:30", 2, ["mon", "wed", "fri"])
    assert valid(data).rules[0].days == "mon,wed,fri"


# --- friends -----------------------------------------------------------------------

def test_rule_rows_and_friends():
    rows = ed.rule_rows(BASE)
    assert [r["friend"] for r in rows] == ["pal", "pal"]
    assert rows[1]["pick"] == "random" and rows[1]["send_note"] is True
    assert rows[0]["pick"] == "oldest" and rows[0]["send_note"] is False
    assert ed.friends(BASE) == ["pal"]
    assert ed.rule_rows(None) == [] and ed.friends({}) == []
    assert ed.rule_rows({"rules": ["junk", {"name": "a"}]})[0]["name"] == "a"


def test_add_friend_rule_does_not_change_the_input():
    out = ed.add_friend_rule(BASE, "@New.Friend ", "21:15", 4, ["sat"], "random", True)
    assert len(BASE["rules"]) == 2                      # original untouched
    rule = out["rules"][-1]
    assert rule == {"name": "new_friend_2115", "days": "sat", "time": "21:15", "count": 4,
                    "recipient": "New.Friend", "send_note": True, "pick": "random"}
    cfg = valid(out)
    assert cfg.rules[-1].recipient == "New.Friend" and cfg.rules[-1].time == "21:15"


def test_add_friend_rule_on_an_empty_config():
    out = ed.add_friend_rule(None, "pal", "08:00", 1)
    assert out["rules"] == [{"name": "pal_0800", "days": "*", "time": "08:00", "count": 1,
                             "recipient": "pal"}]
    assert valid(out)


def test_rule_names_stay_unique():
    out = ed.add_friend_rule({}, "pal", "20:00", 1)
    out = ed.add_friend_rule(out, "pal", "20:00", 2)
    out = ed.add_friend_rule(out, "pal", "20:00", 3)
    assert [r["name"] for r in out["rules"]] == ["pal_2000", "pal_2000_2", "pal_2000_3"]
    assert valid(out)


@pytest.mark.parametrize("friend,time,count,pick,match", [
    ("", "20:00", 3, "oldest", "username"),
    ("  @ ", "20:00", 3, "oldest", "username"),
    ("pal", "8pm", 3, "oldest", "HH:MM"),
    ("pal", "24:00", 3, "oldest", "HH:MM"),
    ("pal", "20:00", 0, "oldest", "1 or more"),
    ("pal", "20:00", True, "oldest", "1 or more"),
    ("pal", "20:00", 3, "newest", "Order must be"),
])
def test_add_friend_rule_rejects_bad_input(friend, time, count, pick, match):
    with pytest.raises(ValueError, match=match):
        ed.add_friend_rule(BASE, friend, time, count, None, pick)


def test_update_rule_keeps_the_name():
    out = ed.update_rule(BASE, "weekend", "other", "12:00", 2, ["sun"])
    assert out["rules"][1] == {"name": "weekend", "days": "sun", "time": "12:00", "count": 2,
                               "recipient": "other"}
    assert BASE["rules"][1]["recipient"] == "pal"
    assert valid(out)
    with pytest.raises(ValueError, match="No schedule named"):
        ed.update_rule(BASE, "missing", "x", "12:00", 1)


def test_remove_rule():
    out = ed.remove_rule(BASE, "evening_drop")
    assert [r["name"] for r in out["rules"]] == ["weekend"]
    with pytest.raises(ValueError, match="No schedule named"):
        ed.remove_rule(BASE, "missing")


def test_remove_friend_removes_all_their_rules_and_frees_their_sources():
    data = ed.add_friend_rule(BASE, "other", "07:00", 1)
    data = ed.add_source(data, "user", "creator_a", to="pal")
    data = ed.add_source(data, "user", "creator_b", to="other")
    out = ed.remove_friend(data, "@pal")
    assert ed.friends(out) == ["other"]
    assert out["discovery"]["sources"] == [{"type": "user", "value": "creator_a"},
                                            {"type": "user", "value": "creator_b", "to": "other"}]
    assert valid(out)
    with pytest.raises(ValueError, match="No schedule for"):
        ed.remove_friend(out, "pal")


@pytest.mark.parametrize("bad", ["text", ["list"], 3])
def test_non_mapping_config_is_rejected(bad):
    with pytest.raises(ValueError, match="Advanced editor"):
        ed.add_friend_rule(bad, "pal", "20:00", 1)


def test_non_list_rules_is_rejected():
    with pytest.raises(ValueError, match="rules is not a list"):
        ed.add_friend_rule({"rules": "oops"}, "pal", "20:00", 1)


# --- limits ------------------------------------------------------------------------

def test_set_limits():
    out = ed.set_limits(BASE, 6, 60, ["2026-12-25", " ", "2026-01-01", "2026-12-25"])
    assert (out["daily_cap"], out["gap_seconds"]) == (6, 60)
    assert out["quiet_days"] == ["2026-01-01", "2026-12-25"]
    assert valid(out).quiet_days == ["2026-01-01", "2026-12-25"]
    assert "quiet_days" not in ed.set_limits(out, 6, 60, [])        # cleared
    assert ed.set_limits(out, 6, 60)["quiet_days"] == out["quiet_days"]   # None = keep


@pytest.mark.parametrize("cap,gap", [(0, 45), (-1, 45), (True, 45), (5, -1), (5, 1.5), ("5", 45)])
def test_set_limits_rejects_bad_numbers(cap, gap):
    with pytest.raises(ValueError):
        ed.set_limits(BASE, cap, gap)


def test_bad_quiet_day_is_caught_by_the_real_validator():
    out = ed.set_limits(BASE, 5, 45, ["25-12-2026"])
    with pytest.raises(ValueError, match="quiet_days"):
        valid(out)


# --- discovery ---------------------------------------------------------------------

def test_add_sources_of_every_type():
    data = ed.add_source(BASE, "explore")
    data = ed.add_source(data, "explore", "feed")
    data = ed.add_source(data, "user", "@Some.Creator", to="@pal")
    data = ed.add_source(data, "hashtag", "#funny", mode="recent")
    data = ed.add_source(data, "collection", " Saved ")
    assert data["discovery"]["sources"] == [
        {"type": "explore", "value": "reels"},
        {"type": "explore", "value": "feed"},
        {"type": "user", "value": "Some.Creator", "to": "pal"},
        {"type": "hashtag", "value": "funny", "mode": "recent"},
        {"type": "collection", "value": "Saved"},
    ]
    assert "discovery" not in BASE
    assert len(valid(data).discovery.sources) == 5
    assert [ed.source_label(r) for r in ed.source_rows(data)] == [
        "explore: reels", "explore: feed", "user: Some.Creator -> pal",
        "hashtag: funny (recent)", "collection: Saved"]


@pytest.mark.parametrize("stype,value,mode,match", [
    ("video", "x", None, "Source type must be"),
    ("user", "  ", None, "Enter a value"),
    ("hashtag", "#", None, "Enter a value"),
    ("explore", "trending", None, "Explore value must be"),
    ("hashtag", "funny", "newest", "Hashtag mode must be"),
])
def test_add_source_rejects_bad_input(stype, value, mode, match):
    with pytest.raises(ValueError, match=match):
        ed.add_source(BASE, stype, value, mode)


def test_add_source_rejects_duplicates():
    data = ed.add_source(BASE, "user", "creator")
    with pytest.raises(ValueError, match="already in the list"):
        ed.add_source(data, "user", "@CREATOR")
    data = ed.add_source({"discovery": {"sources": [{"type": "explore"}]}}, "explore", "feed")
    with pytest.raises(ValueError, match="already in the list"):
        ed.add_source(data, "explore")                  # value defaults to "reels"


def test_remove_source_and_auto_disable():
    data = ed.add_source(BASE, "explore")
    data = ed.add_source(data, "user", "creator")
    data = ed.set_discovery(data, True)
    one = ed.remove_source(data, 0)
    assert one["discovery"]["sources"] == [{"type": "user", "value": "creator"}]
    assert one["discovery"]["enabled"] is True
    none = ed.remove_source(one, 0)
    assert none["discovery"]["sources"] == [] and none["discovery"]["enabled"] is False
    assert valid(none).discovery.enabled is False


@pytest.mark.parametrize("index", [-1, 5, True, "0", None])
def test_remove_source_bad_index(index):
    data = ed.add_source(BASE, "explore")
    with pytest.raises(ValueError, match="no longer exists"):
        ed.remove_source(data, index)


def test_set_discovery_needs_a_source_to_turn_on():
    with pytest.raises(ValueError, match="at least one source"):
        ed.set_discovery(BASE, True)
    assert ed.set_discovery(BASE, False)["discovery"]["enabled"] is False


def test_set_discovery_numbers_and_keywords():
    data = ed.add_source(BASE, "explore")
    out = ed.set_discovery(data, True, {"min_pending": 6, "max_per_refill": 8},
                           "Cricket, #IPL\ncricket\n  street   food ", ["Ads", "ads", ""])
    disc = out["discovery"]
    assert disc["enabled"] is True and disc["min_pending"] == 6 and disc["max_per_refill"] == 8
    assert disc["keywords"] == ["cricket", "ipl", "street food"]
    assert disc["exclude_keywords"] == ["ads"]
    cfg = valid(out)
    assert cfg.discovery.keywords == ["cricket", "ipl", "street food"]
    cleared = ed.set_discovery(out, True, keywords="", exclude_keywords=[])
    assert "keywords" not in cleared["discovery"] and "exclude_keywords" not in cleared["discovery"]
    kept = ed.set_discovery(out, False)                 # None = leave keywords alone
    assert kept["discovery"]["keywords"] == ["cricket", "ipl", "street food"]


@pytest.mark.parametrize("numbers", [{"bogus": 1}, {"min_pending": "6"}, {"min_pending": True}])
def test_set_discovery_rejects_bad_numbers(numbers):
    data = ed.add_source(BASE, "explore")
    with pytest.raises(ValueError):
        ed.set_discovery(data, True, numbers)


def test_out_of_range_number_is_caught_by_the_real_validator():
    data = ed.set_discovery(ed.add_source(BASE, "explore"), True, {"check_every_minutes": 5})
    with pytest.raises(ValueError, match="check_every_minutes"):
        valid(data)


@pytest.mark.parametrize("text,expected", [
    ("", []), (None, []), ("a,b\nc", ["a", "b", "c"]), ("  #Tag , tag ,, ", ["tag"]),
])
def test_parse_keywords(text, expected):
    assert ed.parse_keywords(text) == expected


# --- yaml text ---------------------------------------------------------------------

def test_yaml_round_trip_keeps_times_and_dates_as_strings():
    pytest.importorskip("yaml")
    data = ed.set_limits(ed.add_friend_rule({}, "pal", "20:00", 3), 5, 45, ["2026-12-25"])
    text = ed.to_yaml(data)
    assert "'20:00'" in text and "'2026-12-25'" in text       # quoted, so not a number/date
    back = ed.from_yaml(text)
    assert back == data
    assert valid(back).rules[0].time == "20:00"


def test_from_yaml_edge_cases():
    pytest.importorskip("yaml")
    assert ed.from_yaml("") == {} and ed.from_yaml(None) == {}
    with pytest.raises(ValueError, match="not a mapping"):
        ed.from_yaml("- just\n- a list\n")
