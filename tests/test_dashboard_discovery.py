from types import SimpleNamespace

from reeldrop.dashboard_helpers import discovery_status, request_refill_message


def _getter(values):
    return lambda key, default=None: values.get(key, default)


def _disc(**kw):
    base = dict(
        enabled=True,
        min_pending=15,
        check_every_minutes=180,
        sources=[
            SimpleNamespace(type="hashtag", value="funny", mode="top", to=None),
            SimpleNamespace(type="collection", value="Funny", mode="top", to="alice"),
        ],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_status_with_values():
    st = discovery_status(
        _getter({
            "discovery_last_run": "2026-01-02 10:00:00",
            "discovery_last_summary": "added 12",
            "discovery_refill_requested": "1",
        }),
        _disc(),
    )
    assert st["enabled"] is True
    assert st["last_run"] == "2026-01-02 10:00:00"
    assert st["last_summary"] == "added 12"
    assert st["refill_pending"] is True
    assert st["min_pending"] == 15
    assert st["check_every_minutes"] == 180
    assert st["sources"] == [
        {"type": "hashtag", "value": "funny", "mode": "top", "to": "-"},
        {"type": "collection", "value": "Funny", "mode": "top", "to": "alice"},
    ]


def test_status_defaults_when_never_run():
    st = discovery_status(_getter({}), _disc())
    assert st["last_run"] == "never"
    assert st["last_summary"] == "-"
    assert st["refill_pending"] is False


def test_empty_setting_values_use_defaults():
    st = discovery_status(
        _getter({"discovery_last_run": "", "discovery_last_summary": "", "discovery_refill_requested": "0"}),
        _disc(),
    )
    assert (st["last_run"], st["last_summary"], st["refill_pending"]) == ("never", "-", False)


def test_status_with_none_config():
    st = discovery_status(_getter({}), None)
    assert st["enabled"] is False
    assert st["sources"] == []
    assert st["min_pending"] is None
    assert st["check_every_minutes"] is None
    assert st["last_run"] == "never"


def test_status_disabled_with_no_sources():
    st = discovery_status(_getter({}), _disc(enabled=False, sources=[]))
    assert st["enabled"] is False
    assert st["sources"] == []


def test_status_source_none_list():
    assert discovery_status(_getter({}), _disc(sources=None))["sources"] == []


def test_message_disabled():
    level, text = request_refill_message(False, False)
    assert level == "warning"
    assert "discovery.enabled: true" in text
    assert "Schedules" in text


def test_message_disabled_wins_over_already_requested():
    assert request_refill_message(False, True)[0] == "warning"


def test_message_already_requested():
    level, text = request_refill_message(True, True)
    assert level == "info"
    assert "already" in text


def test_message_new_request():
    level, text = request_refill_message(True, False)
    assert level == "info"
    assert "within about a minute" in text
    assert "already" not in text
