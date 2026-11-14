from pathlib import Path

from reeldrop import config


def test_default_path_when_env_unset(monkeypatch):
    monkeypatch.delenv("SCHEDULES_PATH", raising=False)
    assert config.schedules_path() == "config/schedules.yaml"


def test_env_overrides_path(monkeypatch):
    monkeypatch.setenv("SCHEDULES_PATH", "/x/y.yaml")
    assert config.schedules_path() == "/x/y.yaml"


def test_empty_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SCHEDULES_PATH", "")
    assert config.schedules_path() == "config/schedules.yaml"


def test_ensure_seeds_missing_file_from_default(tmp_path, monkeypatch):
    default = tmp_path / "default.yaml"
    default.write_text("daily_cap: 3\n", encoding="utf-8")
    target = tmp_path / "disk" / "schedules.yaml"          # parent dir does not exist yet
    monkeypatch.setenv("SCHEDULES_PATH", str(target))
    assert config.ensure_schedules_file(str(default)) == str(target)
    assert target.read_text(encoding="utf-8") == "daily_cap: 3\n"


def test_ensure_never_overwrites_existing_edits(tmp_path, monkeypatch):
    default = tmp_path / "default.yaml"
    default.write_text("daily_cap: 3\n", encoding="utf-8")
    target = tmp_path / "schedules.yaml"
    target.write_text("daily_cap: 99  # edited in dashboard\n", encoding="utf-8")
    monkeypatch.setenv("SCHEDULES_PATH", str(target))
    config.ensure_schedules_file(str(default))
    assert "99" in target.read_text(encoding="utf-8")


def test_ensure_is_noop_when_target_is_the_default(tmp_path, monkeypatch):
    default = tmp_path / "default.yaml"
    default.write_text("daily_cap: 3\n", encoding="utf-8")
    monkeypatch.setenv("SCHEDULES_PATH", str(default))
    assert config.ensure_schedules_file(str(default)) == str(default)
    assert default.read_text(encoding="utf-8") == "daily_cap: 3\n"


# --- SCHEDULES_B64: a private schedule for hosts without a disk -------------------

import base64  # noqa: E402
import logging  # noqa: E402

import pytest  # noqa: E402

PRIVATE = (
    'timezone: Asia/Kolkata\n'
    'daily_cap: 8\n'
    'rules:\n'
    '  - name: morning_drop\n'
    '    time: "09:00"\n'
    '    count: 2\n'
    '    recipient: private_friend\n'
)


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode()


@pytest.fixture
def no_private(monkeypatch):
    monkeypatch.delenv("SCHEDULES_B64", raising=False)


def test_missing_file_is_restored_from_the_private_variable(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    default = tmp_path / "default.yaml"
    default.write_text("daily_cap: 3\n", encoding="utf-8")
    target = tmp_path / "disk" / "schedules.yaml"
    monkeypatch.setenv("SCHEDULES_PATH", str(target))
    monkeypatch.setenv("SCHEDULES_B64", _b64(PRIVATE))
    assert config.ensure_schedules_file(str(default)) == str(target)
    assert target.read_text(encoding="utf-8") == PRIVATE
    cfg = config.load_config(str(target))
    assert [(r.time, r.count, r.recipient) for r in cfg.rules] == [("09:00", 2, "private_friend")]


def test_private_variable_never_overwrites_an_existing_file(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    default = tmp_path / "default.yaml"
    default.write_text("daily_cap: 3\n", encoding="utf-8")
    target = tmp_path / "schedules.yaml"
    target.write_text("daily_cap: 99\n", encoding="utf-8")
    monkeypatch.setenv("SCHEDULES_PATH", str(target))
    monkeypatch.setenv("SCHEDULES_B64", _b64(PRIVATE))
    config.ensure_schedules_file(str(default))
    assert target.read_text(encoding="utf-8") == "daily_cap: 99\n"


def test_private_variable_unused_when_target_is_the_repo_file(tmp_path, monkeypatch):
    default = tmp_path / "default.yaml"
    default.write_text("daily_cap: 3\n", encoding="utf-8")
    monkeypatch.setenv("SCHEDULES_PATH", str(default))
    monkeypatch.setenv("SCHEDULES_B64", _b64(PRIVATE))
    config.ensure_schedules_file(str(default))
    assert default.read_text(encoding="utf-8") == "daily_cap: 3\n"


@pytest.mark.parametrize("value,reason", [
    ("%%%", "not valid base64"),
    (base64.b64encode(bytes([255, 254])).decode(), "not valid base64"),
    (_b64("rules: [\n"), "invalid"),
    (_b64('rules:\n  - name: a\n    time: 20:00\n    count: 1\n    recipient: x\n'), "invalid"),
])
def test_bad_private_variable_falls_back_to_the_default(tmp_path, monkeypatch, caplog,
                                                        value, reason):
    pytest.importorskip("yaml")
    default = tmp_path / "default.yaml"
    default.write_text("daily_cap: 3\n", encoding="utf-8")
    target = tmp_path / "disk" / "schedules.yaml"
    monkeypatch.setenv("SCHEDULES_PATH", str(target))
    monkeypatch.setenv("SCHEDULES_B64", value)
    with caplog.at_level(logging.WARNING, logger="reeldrop"):
        config.ensure_schedules_file(str(default))
    assert target.read_text(encoding="utf-8") == "daily_cap: 3\n"
    assert "Ignoring SCHEDULES_B64" in caplog.text and reason in caplog.text


def test_schedules_from_env_accepts_line_breaks_and_env_mapping():
    pytest.importorskip("yaml")
    value = _b64(PRIVATE)
    assert config.schedules_from_env({"SCHEDULES_B64": value[:10] + chr(10) + value[10:]}) == PRIVATE
    assert config.schedules_from_env({}) is None
    assert config.schedules_from_env({"SCHEDULES_B64": "  "}) is None


def test_private_schedule_content_is_never_logged(tmp_path, monkeypatch, caplog):
    pytest.importorskip("yaml")
    bad = 'rules:\n  - name: a\n    time: "20:00"\n    count: 0\n    recipient: secret_friend_x\n'
    with caplog.at_level(logging.DEBUG):
        assert config.schedules_from_env({"SCHEDULES_B64": _b64(bad)}) is None
    assert "Ignoring SCHEDULES_B64" in caplog.text
    assert "secret_friend_x" not in caplog.text
