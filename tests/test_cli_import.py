import pytest

from reeldrop import importer, settings
from reeldrop.__main__ import main
from reeldrop.senders.base import BlockedError


@pytest.fixture(autouse=True)
def _db(temp_db):
    return temp_db


def test_import_collection_prints_result(monkeypatch, capsys):
    seen = {}

    def fake_import(name, limit, dry_run):
        seen.update(name=name, limit=limit, dry_run=dry_run)
        return importer.ImportResult(collection=name, dry_run=dry_run, added=["u1", "u2"])

    monkeypatch.setattr(importer, "import_collection", fake_import)
    assert main(["import-collection", "Funny", "--limit", "7", "--dry-run"]) == 0
    assert seen == {"name": "Funny", "limit": 7, "dry_run": True}
    assert "Funny" in capsys.readouterr().out


def test_import_collection_error_exit_code(monkeypatch, capsys):
    def boom(name, limit, dry_run):
        raise ValueError("No collection 'x'. Available: Funny")

    monkeypatch.setattr(importer, "import_collection", boom)
    assert main(["import-collection", "x"]) == 1
    assert "Available: Funny" in capsys.readouterr().out


def test_import_collection_missing_credentials_is_clean_error(monkeypatch, capsys):
    def boom(name, limit, dry_run):
        raise RuntimeError("IG_USERNAME and IG_PASSWORD must be set")

    monkeypatch.setattr(importer, "import_collection", boom)
    assert main(["import-collection", "x"]) == 1
    assert "IG_USERNAME" in capsys.readouterr().out


def test_import_collection_block_pauses_sending(monkeypatch, capsys):
    def blocked(name, limit, dry_run):
        raise BlockedError("challenge_required")

    monkeypatch.setattr(importer, "import_collection", blocked)
    assert not settings.is_paused()
    assert main(["import-collection", "x"]) == 1
    assert settings.is_paused()
    assert "paused" in capsys.readouterr().out.lower()
