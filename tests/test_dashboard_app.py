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
    for name in ("IG_USERNAME", "IG_PASSWORD", "IG_TOTP_SECRET"):
        monkeypatch.setenv(name, "")
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
