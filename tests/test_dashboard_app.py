"""Run dashboard.py headlessly (Streamlit's AppTest) and use its forms.

No network and no Instagram: the dashboard never logs in. Skipped when streamlit, pandas
or PyYAML is not installed. Everything lives in a temp folder.
"""
import os
from pathlib import Path

import pytest

pytest.importorskip("yaml")
pytest.importorskip("pandas")
pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from reeldrop.config import load_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "app-test-password"


@pytest.fixture
def app(tmp_path, monkeypatch, temp_db):
    live = tmp_path / "schedules.yaml"
    live.write_text(
        'timezone: Asia/Kolkata\ndaily_cap: 10\ngap_seconds: 45\nrules:\n'
        '  - name: evening_drop\n    days: "*"\n    time: "20:00"\n    count: 3\n'
        '    recipient: first_friend\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("SCHEDULES_PATH", str(live))
    monkeypatch.setenv("SENDER", "dry_run")
    monkeypatch.delenv("STORAGE_IS_EPHEMERAL", raising=False)
    monkeypatch.delenv("SCHEDULES_B64", raising=False)
    monkeypatch.delenv("DASHBOARD_PUBLIC", raising=False)
    for name in ("IG_USERNAME", "IG_PASSWORD", "IG_TOTP_SECRET"):
        monkeypatch.setenv(name, "")
    from reeldrop import db, settings
    settings.set_("scheduler_heartbeat", db.now())     # a live scheduler, as on a real host
    settings.set_("scheduler_owner", "test-host")
    at = AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=60)
    at.run()
    return at, live


def log_in(at):
    at.text_input[0].set_value(PASSWORD)
    at.button[0].click().run()
    assert not at.exception
    return at


def by_label(widgets, label):
    matches = [w for w in widgets if w.label == label or w.label.startswith(label)]
    assert matches, f"no widget labelled {label!r}"
    return matches[0]


def test_nothing_is_shown_before_the_password(app):
    at, _ = app
    assert not at.exception
    assert len(at.tabs) == 0 and len(at.metric) == 0
    at.text_input[0].set_value("wrong")
    at.button[0].click().run()
    assert [e.value for e in at.error] == ["Wrong password."]
    assert len(at.tabs) == 0


def test_tabs_and_header_after_login(app):
    at, _ = app
    log_in(at)
    assert [t.label for t in at.tabs] == ["Friends", "Reel topics", "Queue", "Add reels",
                                          "History", "Stats", "Settings", "Advanced"]
    assert ("Sent today", "0 / 10") in [(m.label, m.value) for m in at.metric]
    assert any("Dry-run mode" in i.value for i in at.info)
    assert not at.error


def test_add_a_friend_through_the_form(app):
    at, live = app
    log_in(at)
    by_label(at.text_input, "Friend's Instagram username").set_value("@second_friend")
    by_label(at.button, "Add schedule").click().run()
    assert not at.exception and not at.error
    assert any("Added a schedule for @second_friend" in s.value for s in at.success)
    cfg = load_config(str(live))
    assert [(r.recipient, r.time, r.count, r.days) for r in cfg.rules] == [
        ("first_friend", "20:00", 3, "*"), ("second_friend", "20:00", 3, "*")]


def test_a_bad_friend_name_is_rejected_and_the_file_is_kept(app):
    at, live = app
    before = live.read_text(encoding="utf-8")
    log_in(at)
    by_label(at.button, "Add schedule").click().run()        # username left empty
    assert any("Not saved" in e.value and "username" in e.value for e in at.error)
    assert live.read_text(encoding="utf-8") == before


def test_sources_keywords_and_discovery_switch(app):
    at, live = app
    log_in(at)
    by_label(at.button, "Add source").click().run()          # default: Explore reels
    assert not at.exception and not at.error
    by_label(at.selectbox, "Add a source").set_value("Creator (username)").run()
    by_label(at.text_input, "Creator's username").set_value("some_creator")
    by_label(at.button, "Add source").click().run()
    assert not at.exception and not at.error
    by_label(at.toggle, "Find reels automatically").set_value(True)
    by_label(at.text_area, "Only reels whose caption").set_value("cricket, coding")
    by_label(at.text_area, "Never send reels").set_value("sponsored")
    by_label(at.button, "Save reel topic settings").click().run()
    assert not at.exception and not at.error
    disc = load_config(str(live)).discovery
    assert disc.enabled is True
    assert [(s.type, s.value) for s in disc.sources] == [("explore", "reels"),
                                                         ("user", "some_creator")]
    assert disc.keywords == ["cricket", "coding"] and disc.exclude_keywords == ["sponsored"]


def test_discovery_cannot_be_turned_on_without_a_source(app):
    at, live = app
    log_in(at)
    by_label(at.toggle, "Find reels automatically").set_value(True)
    by_label(at.button, "Save reel topic settings").click().run()
    assert any("at least one source" in e.value for e in at.error)
    assert load_config(str(live)).discovery.enabled is False


def test_limits_form(app):
    at, live = app
    log_in(at)
    by_label(at.number_input, "Most reels sent per day").set_value(6)
    by_label(at.number_input, "Seconds between two reels").set_value(60)
    by_label(at.button, "Save limits").click().run()
    assert not at.exception and not at.error
    cfg = load_config(str(live))
    assert (cfg.daily_cap, cfg.gap_seconds) == (6, 60)


def test_pause_and_resume(app):
    from reeldrop.settings import is_paused

    at, _ = app
    log_in(at)
    by_label(at.button, "⏸️ Pause all sending").click().run()
    assert is_paused() is True
    assert ("Status", "⛔ Paused") in [(m.label, m.value) for m in at.metric]
    by_label(at.button, "▶️ Resume sending").click().run()
    assert is_paused() is False


def test_sender_error_is_shown_with_a_retry_button(app):
    from reeldrop import settings

    at, _ = app
    settings.set_("sender_error", "RuntimeError: Instagram did not complete the login")
    settings.set_("sender_error_at", "2026-11-14 20:00:00")
    log_in(at)
    assert any("could not start its sender" in e.value for e in at.error)
    by_label(at.button, "Retry login now").click().run()
    assert settings.get("sender_retry_requested") == "1"


def test_broken_schedule_file_disables_the_forms_but_not_the_editor(app):
    at, live = app
    live.write_text("rules: [\n", encoding="utf-8")
    log_in(at)
    assert not at.exception
    assert any("Advanced tab" in e.value for e in at.error)
    assert not [b for b in at.button if b.label == "Add schedule"]
    assert [b for b in at.button if b.label == "Save file"]
    assert os.path.exists(live)


def test_free_plan_warning_only_when_storage_is_ephemeral(app, monkeypatch):
    at, _ = app
    log_in(at)
    assert not any("no permanent storage" in w.value for w in at.warning)
    monkeypatch.setenv("STORAGE_IS_EPHEMERAL", "1")
    at.run()
    assert any("no permanent storage" in w.value for w in at.warning)


def test_private_schedule_code_is_offered_on_the_free_plan(app, monkeypatch):
    import base64

    at, live = app
    log_in(at)
    assert not [e for e in at.expander if "Keep these settings" in e.label]
    monkeypatch.setenv("STORAGE_IS_EPHEMERAL", "1")
    at.run()
    box = [e for e in at.expander if "Keep these settings" in e.label]
    assert box
    code = [c.value for c in at.code]
    assert base64.b64encode(live.read_text(encoding="utf-8").encode()).decode() in code


def test_times_are_shown_in_12_hour_format(app):
    at, _ = app
    log_in(at)
    friends_table = at.dataframe[0].value
    assert list(friends_table["Time (IST)"]) == ["8:00 PM"]
    assert "20:00" not in friends_table.to_string()


def test_add_a_friend_with_a_12_hour_time(app):
    at, live = app
    log_in(at)
    by_label(at.text_input, "Friend's Instagram username").set_value("morning_friend")
    hour = [s for s in at.selectbox if s.label == "Hour"][0]
    minute = [s for s in at.selectbox if s.label == "Minute"][0]
    am_pm = [s for s in at.selectbox if s.label == "AM / PM"][0]
    hour.set_value(9)
    minute.set_value(30)
    am_pm.set_value("AM")
    by_label(at.button, "Add schedule").click().run()
    assert not at.exception and not at.error
    times = {r.recipient: r.time for r in load_config(str(live)).rules}
    assert times["morning_friend"] == "09:30"


def test_test_send_button_sets_the_request(app):
    from reeldrop import settings

    at, _ = app
    log_in(at)
    by_label(at.button, "Send a test reel to every friend").click().run()
    assert not at.exception
    assert settings.get("test_send_requested") == "1"
    assert any("Test send requested for 1 friend" in s.value for s in at.success)
    assert not [b for b in at.button if b.label == "Send a test reel to every friend"]
    assert any("waiting for the scheduler" in i.value for i in at.info)


def test_last_test_result_is_shown(app):
    from reeldrop import settings

    at, _ = app
    settings.set_("test_send_result", "@first_friend: sent 1/1")
    settings.set_("test_send_at", "2026-11-15 21:05:00")
    log_in(at)
    assert any("15 Nov 2026, 9:05 PM" in m.value and "sent 1/1" in m.value
               for m in at.markdown)


def test_public_dashboard_opens_without_a_password(app, monkeypatch):
    at, _ = app
    monkeypatch.setenv("DASHBOARD_PUBLIC", "1")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "")      # empty, so the real .env cannot fill it in
    at.run()
    assert not at.exception
    assert not [t for t in at.text_input if t.label == "Password"]
    assert [t.label for t in at.tabs][0] == "Friends"
    assert any("No password (DASHBOARD_PUBLIC=1)" in w.value for w in at.warning)
    assert not [e for e in at.expander if "Upload a session file" in e.label]


def test_password_is_still_required_without_the_switch(app, monkeypatch):
    at, _ = app
    monkeypatch.setenv("DASHBOARD_PUBLIC", "0")
    at.run()
    assert [t for t in at.text_input if t.label == "Password"]
    assert len(at.tabs) == 0


def test_no_password_set_and_no_switch_is_refused(app, monkeypatch):
    at, _ = app
    monkeypatch.delenv("DASHBOARD_PUBLIC", raising=False)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "")      # empty, so the real .env cannot fill it in
    at.run()
    assert any("DASHBOARD_PASSWORD is not set" in e.value for e in at.error)
    assert len(at.tabs) == 0


def test_missing_scheduler_is_reported(app):
    from reeldrop import settings

    at, _ = app
    settings.set_("scheduler_heartbeat", "2026-01-01 08:00:00")   # long ago
    log_in(at)
    assert any("scheduler (the part that sends reels) is not running" in e.value
               and "1 Jan 2026, 8:00 AM" in e.value for e in at.error)


def test_scheduler_never_started_is_reported(app):
    from reeldrop import settings

    at, _ = app
    settings.set_("scheduler_heartbeat", "")
    log_in(at)
    assert any("it has not started yet" in e.value for e in at.error)


@pytest.mark.skipif(bool(os.environ.get("TEST_DATABASE_URL")), reason="checks SQLite")
def test_storage_line_shows_local_file(app):
    at, _ = app
    log_in(at)
    assert any("Storage: local file" in c.value for c in at.caption)

