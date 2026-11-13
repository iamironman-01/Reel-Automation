import pytest

from reeldrop.config import Config, Rule, TIME_RE, load_config, parse_config


def rule(**over):
    base = {"name": "r1", "time": "20:00", "count": 3, "recipient": "friend"}
    base.update(over)
    return base


def cfg_with(*rules, **top):
    return parse_config({"rules": list(rules), **top})


def test_valid_config_and_defaults():
    cfg = parse_config({"rules": [rule()]})
    assert cfg.timezone == "Asia/Kolkata"
    assert cfg.daily_cap == 10 and cfg.gap_seconds == 45
    assert cfg.rules == [Rule(name="r1", days="*", time="20:00", count=3, recipient="friend")]


def test_empty_inputs_are_ok():
    assert parse_config(None) == Config()
    assert parse_config({}).rules == []
    assert parse_config({"rules": []}).rules == []
    assert parse_config({"rules": None}).rules == []


@pytest.mark.parametrize("t", ["00:00", "08:30", "23:59"])
def test_time_re_accepts(t):
    assert TIME_RE.match(t)


@pytest.mark.parametrize("bad", [1200, 1500, 20.0, "25:00", "8:30", "20:60", "2000"])
def test_bad_time(bad):
    with pytest.raises(ValueError, match="HH:MM in quotes"):
        cfg_with(rule(time=bad))


@pytest.mark.parametrize("empty", ["", None])
def test_empty_time_counts_as_missing(empty):
    with pytest.raises(ValueError, match="missing required key.*time"):
        cfg_with(rule(time=empty))


@pytest.mark.parametrize("key", ["name", "time", "count", "recipient"])
def test_missing_required_key(key):
    r = rule()
    del r[key]
    with pytest.raises(ValueError, match=f"missing required key.*{key}"):
        cfg_with(r)


def test_rule_not_a_mapping():
    with pytest.raises(ValueError, match="mapping"):
        cfg_with("evening")


def test_duplicate_rule_names():
    with pytest.raises(ValueError, match="duplicate"):
        cfg_with(rule(), rule(time="21:00"))


@pytest.mark.parametrize("count", [0, -1, "abc", 1.5, True])
def test_bad_count(count):
    with pytest.raises(ValueError, match="count"):
        cfg_with(rule(count=count))


def test_count_string_number_accepted():
    assert cfg_with(rule(count="4")).rules[0].count == 4


@pytest.mark.parametrize("cap", [0, -3, "x"])
def test_bad_daily_cap(cap):
    with pytest.raises(ValueError, match="daily_cap"):
        cfg_with(daily_cap=cap)


def test_bad_gap_seconds():
    with pytest.raises(ValueError, match="gap_seconds"):
        cfg_with(gap_seconds=-1)
    assert cfg_with(gap_seconds=0).gap_seconds == 0


def test_unknown_timezone():
    with pytest.raises(ValueError, match="timezone"):
        cfg_with(timezone="Mars/Olympus")
    assert cfg_with(timezone="UTC").timezone == "UTC"


@pytest.mark.parametrize("days", ["*", "mon-fri", "sat,sun", "mon-wed,sat", "Sat, Sun", "fri"])
def test_valid_days(days):
    assert cfg_with(rule(days=days)).rules[0].days == days.replace(" ", "").lower()


@pytest.mark.parametrize("days", ["funday", "mon-", "mon,,tue", "", "mon-fri-sun", "sun-mon", "1-5", ["mon"], 5])
def test_invalid_days(days):
    with pytest.raises(ValueError, match="days"):
        cfg_with(rule(days=days))


def test_top_level_not_mapping():
    with pytest.raises(ValueError):
        parse_config(["a"])


def test_rules_not_a_list():
    with pytest.raises(ValueError, match="rules"):
        parse_config({"rules": {"name": "x"}})


# --- load_config (needs PyYAML) ---------------------------------------------

def test_load_shipped_schedule():
    pytest.importorskip("yaml")
    cfg = load_config("config/schedules.yaml")
    assert [r.name for r in cfg.rules] == ["evening_drop", "weekend_binge"]
    assert cfg.rules[1].days == "sat,sun"


def test_load_unquoted_time_from_yaml(tmp_path):
    pytest.importorskip("yaml")
    f = tmp_path / "s.yaml"
    f.write_text("rules:\n  - {name: a, time: 20:00, count: 1, recipient: x}\n")
    with pytest.raises(ValueError, match="HH:MM in quotes"):
        load_config(str(f))


def test_load_empty_yaml_file(tmp_path):
    pytest.importorskip("yaml")
    f = tmp_path / "s.yaml"
    f.write_text("")
    assert load_config(str(f)).rules == []
