"""ReelDrop dashboard. Run with: streamlit run dashboard.py"""
import os
import sqlite3

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
from reeldrop.config import ensure_schedules_file, load_config, schedules_path
from reeldrop.dashboard_helpers import (
    STATUS_FILTERS,
    add_links,
    daily_counts,
    discovery_status,
    failure_rate,
    move_message,
    password_config_error,
    password_matches,
    pending_ids,
    queue_query,
    recipient_message,
    request_refill_message,
    save_schedules,
    weekly_counts,
)
from reeldrop.db import connect, init_db
from reeldrop.queue import (
    MOVE_DIRECTIONS,
    add_reel,
    delete_reel,
    move_reel,
    sent_today,
    set_recipient,
)
from reeldrop.settings import get as get_setting
from reeldrop.settings import is_paused, set_paused
from reeldrop.settings import set_ as set_setting

try:
    CONFIG_PATH = ensure_schedules_file()
except OSError:
    CONFIG_PATH = schedules_path()   # the load below reports the problem to the user

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

t_add, t_queue, t_disc, t_hist, t_stats, t_sched, t_set = st.tabs(
    ["Add reels", "Queue", "Discovery", "History", "Stats", "Schedules", "Settings"]
)

# --- Add reels ---
with t_add:
    text = st.text_area("Paste reel links (one per line)", height=200)
    note = st.text_input(
        "Note (optional, applied to every link added)",
        help="Sent as a message before the reel by rules with send_note: true.",
    )
    to = st.text_input(
        "Send to (optional)",
        help="Only the rule with this recipient will send these reels. "
        "Leave blank and any rule can send them.",
    )
    if st.button("Add to queue"):
        if not text.strip():
            st.warning("Paste at least one link.")
        else:
            added, errors = add_links(text, note, add_reel, recipient=to)
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

            st.divider()
            st.subheader("Send to")
            r_id = st.selectbox("Reel to assign", df["id"].tolist(), key="assign_id")
            new_to = st.text_input(
                "Recipient (blank = unassigned, any rule can send it)", key="assign_to"
            )
            if st.button("Set recipient"):
                try:
                    found = set_recipient(int(r_id), new_to)
                except sqlite3.Error as e:
                    st.error(f"Could not update reel #{r_id}: {e}")
                else:
                    if found:
                        st.session_state["flash"] = recipient_message(int(r_id), new_to, True)
                        st.rerun()
                    st.warning(recipient_message(int(r_id), new_to, False))

            st.divider()
            st.subheader("Reorder")
            movable = pending_ids(df.to_dict("records"))
            if not movable:
                st.caption("Only pending reels can be reordered.")
            else:
                m_id = st.selectbox(
                    "Pending reel to move",
                    movable,
                    key="move_id",
                    format_func=lambda i: f"#{i} · {df.loc[df['id'] == i, 'url'].iloc[0]}",
                )
                labels = {"up": "⬆ Up", "down": "⬇ Down", "top": "⏫ Top", "bottom": "⏬ Bottom"}
                for col, direction in zip(st.columns(len(MOVE_DIRECTIONS)), MOVE_DIRECTIONS):
                    if col.button(labels[direction], key=f"move_{direction}"):
                        try:
                            moved = move_reel(int(m_id), direction)
                        except sqlite3.Error as e:
                            st.error(f"Could not move reel #{m_id}: {e}")
                        else:
                            if moved:
                                st.session_state["flash"] = move_message(int(m_id), direction, True)
                                st.rerun()
                            st.warning(move_message(int(m_id), direction, False))

# --- Discovery ---
with t_disc:
    # Written by the scheduler process; the dashboard only reads these settings
    # and sets the refill-request flag. It never logs in to Instagram.
    disc_cfg = getattr(cfg, "discovery", None)
    if "discovery_flash" in st.session_state:
        level, msg = st.session_state.pop("discovery_flash")
        (st.warning if level == "warning" else st.info)(msg)
    try:
        disc = discovery_status(get_setting, disc_cfg)
    except sqlite3.Error as e:
        st.error(f"Could not read discovery status: {e}")
        disc = None
    if disc is not None:
        st.write("**Automatic discovery:** " + ("✅ enabled" if disc["enabled"] else "⛔ disabled"))
        if disc_cfg is None:
            st.caption("This version of the config has no `discovery:` support.")
        d1, d2, d3 = st.columns(3)
        d1.metric("Pending", pending)
        d2.metric("Refill below", disc["min_pending"] if disc["min_pending"] is not None else "-")
        d3.metric(
            "Check every",
            f"{disc['check_every_minutes']} min" if disc["check_every_minutes"] is not None else "-",
        )
        if disc["enabled"] and disc["min_pending"] is not None and pending < disc["min_pending"]:
            st.caption("The queue is below the minimum, so the scheduler will refill it on its next check.")
        st.write(f"**Last refill:** {disc['last_run']}")
        st.write(f"**Last result:** {disc['last_summary']}")
        if disc["refill_pending"]:
            st.info("A refill is requested and waiting for the scheduler.")

        st.subheader("Sources")
        if disc["sources"]:
            st.dataframe(
                pd.DataFrame(disc["sources"], columns=["type", "value", "mode", "to"]),
                use_container_width=True,
            )
        else:
            st.caption("No sources configured. Add a `discovery:` block in the Schedules tab.")

        if st.button("Refill now"):
            try:
                level, msg = request_refill_message(disc["enabled"], disc["refill_pending"])
                if disc["enabled"] and not disc["refill_pending"]:
                    set_setting("discovery_refill_requested", "1")
            except sqlite3.Error as e:
                st.error(f"Could not request a refill: {e}")
            else:
                st.session_state["discovery_flash"] = (level, msg)
                st.rerun()
        st.caption(
            "The scheduler process does the actual Instagram fetching, using its own login. "
            "The dashboard never logs in; this button only asks the scheduler to refill within "
            "about a minute. Unofficial access: use a test account."
        )

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
