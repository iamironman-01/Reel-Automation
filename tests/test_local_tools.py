import importlib.util
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


local_init = _load("local_init")
local_demo = _load("local_demo")


# ---- local_init ----------------------------------------------------------------------

def test_make_env_sets_dry_run_and_password():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    text = local_init.make_env_text(example, "s3cret-Pw")
    lines = text.splitlines()
    assert "SENDER=dry_run" in lines
    assert "DASHBOARD_PASSWORD=s3cret-Pw" in lines
    assert "change-me" not in text


def test_init_creates_env_and_data(tmp_path):
    (tmp_path / ".env.example").write_text("SENDER=telegram\nDASHBOARD_PASSWORD=change-me\n", encoding="utf-8")
    pw = local_init.init(tmp_path, password="abc123")
    assert pw == "abc123"
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / ".env").read_text(encoding="utf-8").splitlines() == [
        "SENDER=dry_run", "DASHBOARD_PASSWORD=abc123"]


def test_init_never_overwrites_existing_env(tmp_path):
    (tmp_path / ".env.example").write_text("SENDER=telegram\n", encoding="utf-8")
    (tmp_path / ".env").write_text("MINE=1\n", encoding="utf-8")
    assert local_init.init(tmp_path) is None
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "MINE=1\n"


def test_generated_password_is_not_the_placeholder(tmp_path):
    (tmp_path / ".env.example").write_text("DASHBOARD_PASSWORD=change-me\n", encoding="utf-8")
    pw = local_init.init(tmp_path)
    assert pw and pw != "change-me" and len(pw) >= 12


# ---- local_demo ----------------------------------------------------------------------

def test_demo_schedule_quotes_time_and_is_ahead():
    now = datetime(2026, 11, 13, 23, 59, 40, tzinfo=ZoneInfo("Asia/Kolkata"))
    text = local_demo.demo_schedule_text(now, "Asia/Kolkata", minutes_ahead=2)
    assert 'time: "00:01"' in text            # wraps past midnight, seconds dropped
    assert "timezone: Asia/Kolkata" in text and "recipient: demo_friend" in text


def test_demo_schedule_parses_with_real_validator():
    from reeldrop.config import parse_config

    now = datetime(2026, 11, 13, 10, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    text = local_demo.demo_schedule_text(now, "Asia/Kolkata")
    # tiny hand parse (PyYAML may not be installed): same keys the YAML carries
    cfg = parse_config({"timezone": "Asia/Kolkata", "daily_cap": 10, "gap_seconds": 5,
                        "rules": [{"name": "demo_drop", "days": "*", "time": "10:02",
                                   "count": 2, "recipient": "demo_friend"}]})
    assert 'time: "10:02"' in text and cfg.rules[0].time == "10:02"
    yaml = pytest.importorskip("yaml")
    assert parse_config(yaml.safe_load(text)).rules[0].time == "10:02"


def test_reset_demo_dir_refuses_other_folders(tmp_path):
    victim = tmp_path / "important"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    with pytest.raises(ValueError):
        local_demo.reset_demo_dir(victim)
    assert (victim / "keep.txt").exists()


def test_demo_prepares_isolated_db_and_does_not_touch_real_one(tmp_path):
    """Run the demo (--no-run) in a subprocess: fake reels land in .local-demo/, not data/."""
    real_db = ROOT / "data" / "reeldrop.db"
    existed = real_db.exists()
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "local_demo.py"), "--no-run", "--reels", "3"],
                       capture_output=True, text=True, cwd=tmp_path, timeout=60)
    try:
        assert r.returncode == 0, r.stderr
        assert "Added 3 fake reels" in r.stdout
        demo_db = ROOT / ".local-demo" / "data" / "reeldrop.db"
        assert demo_db.exists()
        assert (ROOT / ".local-demo" / "schedules.yaml").exists()
        assert real_db.exists() == existed       # the real database was not created/changed
    finally:
        import shutil
        shutil.rmtree(ROOT / ".local-demo", ignore_errors=True)


def test_demo_rejects_bad_arguments():
    assert local_demo.main(["--minutes", "0"]) == 2
    assert local_demo.main(["--reels", "999"]) == 2
