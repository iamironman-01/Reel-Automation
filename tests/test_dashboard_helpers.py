import os
import stat

import pytest

from reeldrop.dashboard_helpers import (
    add_links,
    parse_links,
    password_config_error,
    password_matches,
    queue_query,
    save_schedules,
)

OLD = b"timezone: Asia/Kolkata\nrules: []\n"


@pytest.fixture
def sched(tmp_path):
    p = tmp_path / "schedules.yaml"
    p.write_bytes(OLD)
    return p


def _reject_if_broken(path):
    with open(path, encoding="utf-8") as f:
        if "BROKEN" in f.read():
            raise RuntimeError("bad yaml")


def test_invalid_text_keeps_original_and_leaves_no_temp(sched):
    with pytest.raises(ValueError, match="bad yaml"):
        save_schedules(str(sched), "BROKEN: [", _reject_if_broken)
    assert sched.read_bytes() == OLD
    assert os.listdir(sched.parent) == ["schedules.yaml"]


def test_keyerror_message_is_readable(sched):
    def v(_):
        raise KeyError("time")

    with pytest.raises(ValueError, match="missing required field 'time'"):
        save_schedules(str(sched), "x", v)
    assert sched.read_bytes() == OLD


def test_valid_text_replaces_file(sched):
    seen = []

    def v(path):
        seen.append(path)
        assert os.path.dirname(path) == str(sched.parent)  # same dir -> atomic
        assert path != str(sched)

    save_schedules(str(sched), "daily_cap: 3\n", v)
    assert sched.read_text(encoding="utf-8") == "daily_cap: 3\n"
    assert len(seen) == 1
    assert os.listdir(sched.parent) == ["schedules.yaml"]


def test_permissions_preserved(sched):
    os.chmod(sched, 0o644)
    save_schedules(str(sched), "a: 1\n", lambda p: None)
    assert stat.S_IMODE(os.stat(sched).st_mode) == 0o644


def test_unicode_roundtrip(sched):
    save_schedules(str(sched), "name: café 🎬\n", lambda p: None)
    assert sched.read_text(encoding="utf-8") == "name: café 🎬\n"


def test_real_validator_rejects_broken_yaml(sched):
    pytest.importorskip("yaml")
    pytest.importorskip("reeldrop.config")
    with pytest.raises(ValueError):
        save_schedules(str(sched), "rules: [unclosed", None)
    assert sched.read_bytes() == OLD


# --- password ---

@pytest.mark.parametrize("val", [None, "", "   ", "change-me"])
def test_password_config_error_for_unsafe_values(val):
    assert password_config_error(val)


def test_password_config_ok():
    assert password_config_error("s3cret!") is None


def test_password_matches():
    assert password_matches("s3cret", "s3cret")
    assert not password_matches("wrong", "s3cret")
    assert not password_matches("", "s3cret")
    assert not password_matches("anything", "")
    assert not password_matches(None, None)
    assert password_matches("pässword", "pässword")  # non-ASCII must not raise


# --- add links ---

def test_parse_links_strips_blanks():
    assert parse_links("  a \n\n   \nb\r\n") == ["a", "b"]
    assert parse_links("") == []


def test_add_links_collects_per_line_errors_and_note():
    calls = []

    def add(url, note):
        calls.append((url, note))
        if "bad" in url:
            raise ValueError("Not an Instagram reel/post link")
        if "boom" in url:
            raise RuntimeError("db locked")

    added, errors = add_links("ok1\n\nbad\n boom \nok2", "  fun  ", add)
    assert added == 2
    assert errors == [
        "bad — Not an Instagram reel/post link",
        "boom — unexpected error: db locked",
    ]
    assert calls[0] == ("ok1", "fun")
    assert all(n == "fun" for _, n in calls)


def test_add_links_empty_note_default():
    calls = []
    add_links("x", "", lambda u, n: calls.append(n))
    assert calls == [""]


# --- queue query ---

def test_queue_query_all_and_filtered():
    sql, params = queue_query("all")
    assert "WHERE" not in sql and params == ()
    sql, params = queue_query("failed")
    assert "WHERE status = ?" in sql and params == ("failed",)


def test_queue_query_rejects_unknown_status():
    with pytest.raises(ValueError):
        queue_query("x'; DROP TABLE reels;--")


# --- sender status and session upload -----------------------------------------------

def test_sender_status_defaults_and_values():
    from reeldrop.dashboard_helpers import sender_status

    assert sender_status(lambda key, default=None: default) == {
        "error": "", "at": "", "retry_requested": False}
    store = {"sender_error": "RuntimeError: nope", "sender_error_at": "2026-11-14 20:00:00",
             "sender_retry_requested": "1"}
    assert sender_status(lambda key, default=None: store.get(key, default)) == {
        "error": "RuntimeError: nope", "at": "2026-11-14 20:00:00", "retry_requested": True}


SESSION_JSON = b'{"uuids": {"phone_id": "x"}, "cookies": {}, "authorization_data": {"a": 1}}'


def test_save_session_file_writes_the_exact_bytes(tmp_path):
    from reeldrop.dashboard_helpers import save_session_file

    target = tmp_path / "data" / "session.json"          # folder does not exist yet
    save_session_file(SESSION_JSON, str(target))
    assert target.read_bytes() == SESSION_JSON
    assert [p.name for p in target.parent.iterdir()] == ["session.json"]   # no temp file left


@pytest.mark.parametrize("raw,match", [
    (b"", "empty"),
    (b"not json", "not valid JSON"),
    (bytes([255, 254, 0]), "not valid JSON"),
    (b"[1, 2]", "does not look like"),
    (b'{"timezone": "Asia/Kolkata"}', "does not look like"),
])
def test_save_session_file_rejects_bad_uploads_and_keeps_the_old_file(tmp_path, raw, match):
    from reeldrop.dashboard_helpers import save_session_file

    target = tmp_path / "session.json"
    target.write_bytes(SESSION_JSON)
    with pytest.raises(ValueError, match=match):
        save_session_file(raw, str(target))
    assert target.read_bytes() == SESSION_JSON
    assert [p.name for p in tmp_path.iterdir()] == ["session.json"]


def test_save_session_file_rejects_huge_files(tmp_path):
    from reeldrop.dashboard_helpers import MAX_SESSION_BYTES, save_session_file

    with pytest.raises(ValueError, match="too large"):
        save_session_file(b"{" + b" " * MAX_SESSION_BYTES + b"}", str(tmp_path / "s.json"))
    assert not (tmp_path / "s.json").exists()


def test_session_errors_never_contain_the_file_content(tmp_path):
    from reeldrop.dashboard_helpers import save_session_file

    with pytest.raises(ValueError) as exc:
        save_session_file(b'{"sessionid": "SECRET-COOKIE-VALUE"}', str(tmp_path / "s.json"))
    assert "SECRET-COOKIE-VALUE" not in str(exc.value)
