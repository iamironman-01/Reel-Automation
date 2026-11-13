"""ReelDrop dashboard. Run with: streamlit run dashboard.py"""
import os
import sqlite3

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
from reeldrop.config import load_config
from reeldrop.dashboard_helpers import (
    STATUS_FILTERS,
    add_links,
    daily_counts,
    failure_rate,
    password_config_error,
    password_matches,
    queue_query,
    save_schedules,
    weekly_counts,
)
from reeldrop.db import connect, init_db
from reeldrop.queue import add_reel, delete_reel, sent_today
from reeldrop.settings import is_paused, set_paused

CONFIG_PATH = "config/schedules.yaml"

st.set_page_config(page_title="ReelDrop", page_icon="🎬")

# --- password gate (nothing below this block runs until it passes) ---
_expected_pw = os.getenv("DASHBOARD_PASSWORD")
_pw_problem = password_config_error(_expected_pw)
if _pw_problem:
    st.error(_pw_problem)
    st.stop()

if not st.session_state.get("ok"):
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")
    if submitted:
        if password_matches(pw, _expected_pw):
            st.session_state.ok = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()

# --- startup: database and config ---
try:
    init_db()
except (sqlite3.Error, OSError) as e:
    st.error(f"Cannot open the database: {e}")
    st.stop()

try:
    cfg = load_config(CONFIG_PATH)
except Exception as e:  # missing file, bad YAML, invalid rule
    st.error(f"Cannot load {CONFIG_PATH}: {e}")
    st.stop()

# --- header metrics ---
st.title("🎬 ReelDrop")

if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

try:
    sent_count = sent_today()
    with connect() as c:
        pending = c.execute(
            "SELECT COUNT(*) FROM reels WHERE status='pending'"
        ).fetchone()[0]
    paused = is_paused()
except sqlite3.Error as e:
    st.error(f"Database error: {e}")
    st.stop()

c1, c2, c3 = st.columns(3)
c1.metric("Sent today", f"{sent_count} / {cfg.daily_cap}")
c2.metric("In queue", pending)
c3.metric("Status", "⛔ Paused" if paused else "✅ Running")

t_add, t_queue, t_hist, t_stats, t_sched, t_set = st.tabs(
    ["Add reels", "Queue", "History", "Stats", "Schedules", "Settings"]
)

# --- Add reels ---
with t_add:
    text = st.text_area("Paste reel links (one per line)", height=200)
    note = st.text_input(
        "Note (optional, applied to every link added)",
        help="Sent as a message before the reel by rules with send_note: true.",
    )
    if st.button("Add to queue"):
        if not text.strip():
            st.warning("Paste at least one link.")
        else:
            added, errors = add_links(text, note, add_reel)
            if added:
                st.success(f"Added {added} reel(s)")
            elif errors:
                st.error("Nothing was added.")
            for err in errors:
                st.warning(err)

# --- Queue ---
with t_queue:
    status = st.selectbox("Status", STATUS_FILTERS, index=0)
    try:
        sql, params = queue_query(status)
        with connect() as c:
            df = pd.read_sql_query(sql, c, params=params)
    except sqlite3.Error as e:
        st.error(f"Could not read the queue: {e}")
        df = None
    if df is not None:
        st.dataframe(df, use_container_width=True)
        if df.empty:
            st.caption("No reels to show.")
        else:
            rid = st.selectbox(
                "Reel to remove",
                df["id"].tolist(),
                format_func=lambda i: f"#{i} · {df.loc[df['id'] == i, 'url'].iloc[0]}",
            )
            if st.button("Remove"):
                try:
                    delete_reel(int(rid))
                except sqlite3.Error as e:
                    st.error(f"Could not remove reel #{rid}: {e}")
                else:
                    st.session_state["flash"] = f"Removed reel #{rid}"
                    st.rerun()

# --- History ---
with t_hist:
    try:
        with connect() as c:
            log = pd.read_sql_query(
                "SELECT * FROM send_log ORDER BY id DESC LIMIT 200", c
            )
        st.dataframe(log, use_container_width=True)
    except sqlite3.Error as e:
        st.error(f"Could not read history: {e}")

# --- Stats ---
with t_stats:
    try:
        with connect() as c:
            daily = daily_counts(c, days=30)
            weekly = weekly_counts(c, weeks=12)
            rate = failure_rate(c)
    except sqlite3.Error as e:
        st.error(f"Could not read stats: {e}")
    else:
        m1, m2, m3 = st.columns(3)
        m1.metric("Sent (all time)", rate["sent"])
        m2.metric("Failed (all time)", rate["failed"])
        m3.metric("Failure rate", f"{rate['rate']:.1%}")
        st.subheader("Sent per day (last 30 days)")
        st.bar_chart(pd.DataFrame(daily, columns=["day", "sent"]).set_index("day"))
        st.subheader("Sent per week (weeks start on Monday)")
        st.bar_chart(pd.DataFrame(weekly, columns=["week", "sent"]).set_index("week"))

# --- Schedules ---
with t_sched:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        st.error(f"Cannot read {CONFIG_PATH}: {e}")
        raw = None
    if raw is not None:
        new = st.text_area("schedules.yaml", raw, height=350)
        if st.button("Save schedules"):
            try:
                save_schedules(CONFIG_PATH, new)
            except (ValueError, OSError) as e:
                st.error(f"Not saved (existing file kept): {e}")
            else:
                st.success(
                    "Saved. The scheduler picks up the change within about a minute. "
                    "A timezone change still needs `docker compose restart scheduler`."
                )

# --- Settings ---
with t_set:
    st.write(f"**Sender:** `{os.getenv('SENDER', 'dry_run')}`")
    st.write(
        f"**Timezone:** `{cfg.timezone}` · **Daily cap:** {cfg.daily_cap} · "
        f"**Gap:** {cfg.gap_seconds}s"
    )
    st.info(
        "Schedule edits are picked up by the scheduler within about a minute; "
        "only a timezone change needs `docker compose restart scheduler`. "
        "The pause switch below applies immediately."
    )
    if paused:
        if st.button("▶️ Resume sending"):
            set_paused(False)
            st.rerun()
    else:
        if st.button("⏸️ Pause all sending"):
            set_paused(True)
            st.rerun()
