# Phase 5 — Dashboard

**Goal:** control everything from a browser.
**Time:** 3 days

## Checklist

- [ ] Password gate
- [ ] Tabs: Add reels · Queue · History · Schedules · Settings
- [ ] Shows today's count vs daily cap and pause status
- [ ] Schedule edits saved to `schedules.yaml` (scheduler restart picks them up)

## dashboard.py

```python
import os
import pandas as pd
import streamlit as st
import yaml
from dotenv import load_dotenv

load_dotenv()
from reeldrop.db import init_db, connect
from reeldrop.queue import add_reel, delete_reel, sent_today
from reeldrop.settings import is_paused, set_paused
from reeldrop.config import load_config

st.set_page_config(page_title="ReelDrop", page_icon="🎬")
init_db()

# --- password gate ---
if not st.session_state.get("ok"):
    pw = st.text_input("Password", type="password")
    if pw and pw == os.getenv("DASHBOARD_PASSWORD"):
        st.session_state.ok = True
        st.rerun()
    st.stop()

cfg = load_config()
st.title("🎬 ReelDrop")
c1, c2, c3 = st.columns(3)
c1.metric("Sent today", f"{sent_today()} / {cfg.daily_cap}")
with connect() as c:
    pending = c.execute("SELECT COUNT(*) FROM reels WHERE status='pending'").fetchone()[0]
c2.metric("In queue", pending)
c3.metric("Status", "⛔ Paused" if is_paused() else "✅ Running")

t_add, t_queue, t_hist, t_sched, t_set = st.tabs(
    ["Add reels", "Queue", "History", "Schedules", "Settings"])

with t_add:
    text = st.text_area("Paste reel links (one per line)", height=200)
    if st.button("Add to queue"):
        added, errors = 0, []
        for line in text.splitlines():
            if line.strip():
                try:
                    add_reel(line.strip()); added += 1
                except ValueError as e:
                    errors.append(f"{line.strip()} — {e}")
        st.success(f"Added {added} reel(s)")
        for err in errors:
            st.warning(err)

with t_queue:
    with connect() as c:
        df = pd.read_sql_query(
            "SELECT id, url, note, status, added_at FROM reels ORDER BY id DESC", c)
    st.dataframe(df, use_container_width=True)
    rid = st.number_input("Reel id to remove", min_value=1, step=1)
    if st.button("Remove"):
        delete_reel(int(rid)); st.rerun()

with t_hist:
    with connect() as c:
        log = pd.read_sql_query("SELECT * FROM send_log ORDER BY id DESC LIMIT 200", c)
    st.dataframe(log, use_container_width=True)

with t_sched:
    path = "config/schedules.yaml"
    raw = open(path, encoding="utf-8").read()
    new = st.text_area("schedules.yaml", raw, height=350)
    if st.button("Save schedules"):
        try:
            yaml.safe_load(new)                      # syntax check
            open(path, "w", encoding="utf-8").write(new)
            load_config(path)                        # rule validation
            st.success("Saved. Restart the scheduler to apply.")
        except Exception as e:
            open(path, "w", encoding="utf-8").write(raw)   # roll back
            st.error(f"Not saved: {e}")

with t_set:
    if is_paused():
        if st.button("▶️ Resume sending"):
            set_paused(False); st.rerun()
    else:
        if st.button("⏸️ Pause all sending"):
            set_paused(True); st.rerun()
```

## Run

```bash
streamlit run dashboard.py
```

Open http://localhost:8501.

✅ **Done when:** you can add reels, see the queue and history, edit schedules, and pause/resume from the browser.

## Update: everything is set with forms (2026-11-14)

The dashboard no longer needs YAML edits for normal use:

- **Friends** tab: add, change and remove friends and their schedules.
- **Reel topics** tab: sources, caption keywords (`keywords`, `exclude_keywords`), refill numbers and the on/off switch.
- **Settings** tab: daily cap, gap, quiet days, pause, Instagram login status, **Retry login now**, and session file upload.
- **Advanced** tab: the raw file, as before.

Form edits go through `reeldrop/config_editor.py` (pure functions on the raw mapping) and are saved with the same validate-then-replace step as the raw editor. `tests/test_dashboard_app.py` runs the page headlessly with Streamlit's `AppTest`.

## Update: 12-hour times and a test send (2026-11-15)

- Every time on the dashboard is shown in 12-hour format with AM/PM (IST); the schedule pickers are Hour / Minute / AM-PM. The file keeps 24-hour times.
- **Send a test reel to every friend** (Friends tab) asks the scheduler, through a settings flag, to send one waiting reel to each friend now. It runs through `job.run_rule`, so every limit and safety check applies, and test reels count towards the daily cap.

