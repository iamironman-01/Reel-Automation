from reeldrop import settings


def test_defaults(temp_db):
    assert settings.get("missing") is None
    assert settings.get("missing", "x") == "x"
    assert settings.is_paused() is False


def test_pause_round_trip(temp_db):
    settings.set_paused(True)
    assert settings.is_paused() is True
    assert settings.get("paused") == "1"
    settings.set_paused(False)
    assert settings.is_paused() is False


def test_set_overwrites_and_stringifies(temp_db):
    settings.set_("k", 1)
    settings.set_("k", 2)
    assert settings.get("k") == "2"
