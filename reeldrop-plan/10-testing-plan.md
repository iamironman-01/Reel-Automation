# 10 — Testing Plan

| Phase | Test | How | Pass when |
|-------|------|-----|-----------|
| 1 | Valid link | `add` a normal reel link with `?igsh=` | Stored as clean URL |
| 1 | Invalid link | `add` a YouTube link | Rejected with error |
| 1 | Duplicate | `add` the same reel twice | Second one rejected |
| 2 | Timing | Rule 2 minutes ahead, dry-run | Fires at the right IST minute |
| 2 | Bad config | `time: 25:00` or unquoted time | Clear error on start |
| 2 | Empty queue | Rule fires with no reels | Logged, no crash |
| 2 | Daily cap | `daily_cap: 2`, rule `count: 5` | Only 2 sent that day |
| 3 | Real send | Test account → your second account | Reels arrive in order with gaps |
| 3 | Bad recipient | Non-existent username | Marked failed, no crash |
| 4 | Network error | Disconnect Wi-Fi during a run | Retried once, then marked failed |
| 4 | Block simulation | Make sender raise `BlockedError` | Paused + alert, later rules skip |
| 4 | Restart | Stop the program mid-run, restart | No reel sent twice |
| 5 | Dashboard password | Wrong password | Nothing shown |
| 5 | Broken YAML | Save invalid YAML from dashboard | Rejected, old file kept |
| 6 | Reboot | Restart host machine | Containers come back, next rule fires |

## Simple automated tests (optional)

```bash
pip install pytest
```

```python
# tests/test_queue.py
import pytest
from reeldrop import db
from reeldrop.queue import add_reel, next_pending

@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

def test_add_and_next():
    add_reel("https://www.instagram.com/reel/AAA111/")
    add_reel("https://www.instagram.com/reel/BBB222/")
    assert [r["shortcode"] for r in next_pending(5)] == ["AAA111", "BBB222"]

def test_duplicate_rejected():
    add_reel("https://www.instagram.com/reel/AAA111/")
    with pytest.raises(ValueError):
        add_reel("https://www.instagram.com/reel/AAA111/?igsh=xyz")

def test_invalid_rejected():
    with pytest.raises(ValueError):
        add_reel("https://youtube.com/watch?v=1")
```

```bash
pytest -q
```
