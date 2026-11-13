import pytest

from reeldrop.__main__ import main
from reeldrop.queue import list_reels

URL = "https://www.instagram.com/reel/ABC123xyz/?igsh=xxxx"
CLEAN = "https://www.instagram.com/reel/ABC123xyz/"


def test_add_list_remove(temp_db, capsys):
    assert main(["add", URL, "--note", "funny"]) == 0
    assert "Added reel id 1" in capsys.readouterr().out

    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert CLEAN in out and "pending" in out and "funny" in out
    assert "igsh" not in out

    assert main(["remove", "1"]) == 0
    assert "Removed 1" in capsys.readouterr().out
    assert list_reels() == []


def test_add_duplicate_errors(temp_db, capsys):
    assert main(["add", URL]) == 0
    capsys.readouterr()
    assert main(["add", CLEAN]) == 1
    assert "Error: This reel is already in the queue" in capsys.readouterr().out
    assert len(list_reels()) == 1


def test_add_invalid_errors(temp_db, capsys):
    assert main(["add", "https://youtube.com/watch?v=1"]) == 1
    assert "Error: Not an Instagram reel/post link" in capsys.readouterr().out
    assert list_reels() == []


def test_remove_missing_id(temp_db, capsys):
    assert main(["remove", "999"]) == 1
    out = capsys.readouterr().out
    assert "no reel with id 999" in out
    assert "Removed" not in out


def test_list_status_filter(temp_db, capsys):
    main(["add", URL])
    capsys.readouterr()
    assert main(["list", "--status", "sent"]) == 0
    assert capsys.readouterr().out == ""
    assert main(["list", "--status", "pending"]) == 0
    assert CLEAN in capsys.readouterr().out


def test_bad_arguments_exit_nonzero(temp_db):
    with pytest.raises(SystemExit) as e:
        main([])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        main(["remove", "abc"])
    assert e.value.code == 2
