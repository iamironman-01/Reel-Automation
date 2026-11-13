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
