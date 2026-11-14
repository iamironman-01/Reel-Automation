"""ReelDrop dashboard. Run with: streamlit run dashboard.py"""
import base64
import datetime
import os
import sqlite3

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
from reeldrop import config_editor as editor
from reeldrop.config import PICK_MODES, ensure_schedules_file, load_config, schedules_path
from reeldrop.dashboard_helpers import (
    AM_PM,
    SENDER_RETRY_KEY,
    TEST_SEND_REQUEST_KEY,
    dashboard_is_public,
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
    save_session_file,
    sender_status,
    split_12h,
    test_send_status,
    time_12h,
    time_24h,
    timestamp_12h,
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

SESSION_PATH = os.path.join("data", "session.json")   # same file the Instagram sender uses
ANY_FRIEND = "(any friend)"
HIGH_DAILY_CAP = 15

st.set_page_config(page_title="ReelDrop", page_icon="🎬", layout="wide")

# --- password gate (nothing below this block runs until it passes) ---
# DASHBOARD_PUBLIC=1 turns the gate off on purpose. Without it a real password is required.
PUBLIC = dashboard_is_public(os.getenv("DASHBOARD_PUBLIC"))
_expected_pw = os.getenv("DASHBOARD_PASSWORD")
_pw_problem = None if PUBLIC else password_config_error(_expected_pw)
if _pw_problem:
    st.error(_pw_problem)
    st.stop()

if not PUBLIC and not st.session_state.get("ok"):
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
    cfg = None
    config_problem = f"Cannot load {CONFIG_PATH}: {e}"
else:
    config_problem = None

try:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        raw_text = f.read()
except OSError as e:
    raw_text = None
    config_problem = config_problem or f"Cannot read {CONFIG_PATH}: {e}"

try:
    raw_cfg = editor.from_yaml(raw_text) if raw_text is not None else {}
except Exception as e:  # bad YAML: only the Advanced editor can fix it
    raw_cfg = {}
    config_problem = config_problem or f"Cannot read {CONFIG_PATH}: {e}"


def apply_change(change, message: str) -> None:
    """Run one form edit: build the new config, validate and save it, then reload the page.

    `change` takes the current raw config and returns the new one (see config_editor).
    Any problem is shown and the existing file is kept.
    """
    try:
        new_cfg = change(raw_cfg)
        save_schedules(CONFIG_PATH, editor.to_yaml(new_cfg))
    except (ValueError, OSError) as e:
        st.error(f"Not saved (existing settings kept): {e}")
        return
    st.session_state["flash"] = message + " The scheduler picks this up within about a minute."
    st.rerun()


HOURS_12 = list(range(1, 13))


def time_picker(value_24h: str, key: str):
    """Hour / minute / AM-PM pickers in one row. Returns the choice, or None if invalid."""
    hour, minute, am_pm = split_12h(value_24h)
    minutes = sorted(set(range(0, 60, 5)) | {minute})
    c1, c2, c3 = st.columns(3)
    picked_hour = c1.selectbox("Hour", HOURS_12, index=HOURS_12.index(hour), key=f"{key}_h")
    picked_minute = c2.selectbox("Minute", minutes, index=minutes.index(minute),
                                 format_func=lambda m: f"{m:02d}", key=f"{key}_m")
    picked_am_pm = c3.selectbox("AM / PM", AM_PM, index=AM_PM.index(am_pm), key=f"{key}_ap")
    return picked_hour, picked_minute, picked_am_pm


def show_times_12h(df, columns):
    """Copy of `df` with the named timestamp columns shown in 12-hour time."""
    out = df.copy()
    for col in columns:
        if col in out.columns:
            out[col] = out[col].map(timestamp_12h)
    return out


# --- header ---
st.title("🎬 ReelDrop")

if PUBLIC:
    st.warning("No password (DASHBOARD_PUBLIC=1): anyone who has this page's link can "
               "change friends and make your Instagram account send reels. Keep the link "
               "private. Uploading a session file is turned off while there is no password.")

if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

try:
    sent_count = sent_today()
    with connect() as c:
        pending = c.execute(
            "SELECT COUNT(*) FROM reels WHERE status='pending'"
        ).fetchone()[0]
    paused = is_paused()
    send_state = sender_status(get_setting)
except sqlite3.Error as e:
    st.error(f"Database error: {e}")
    st.stop()

sender_name = os.getenv("SENDER", "dry_run")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Sent today", f"{sent_count} / {cfg.daily_cap}" if cfg else str(sent_count))
c2.metric("In queue", pending)
c3.metric("Status", "⛔ Paused" if paused else "✅ Running")
c4.metric("Sender", sender_name)

if send_state["error"]:
    st.error(
        f"The scheduler could not start its sender ({timestamp_12h(send_state['at']) or 'time unknown'}), "
        f"so nothing is being sent: {send_state['error']} See the Settings tab."
    )
if sender_name == "dry_run":
    st.info("Dry-run mode: reels are only printed in the log, nothing is sent. "
            "Set the SENDER environment variable to `instagrapi` to send real Instagram DMs.")
if os.getenv("STORAGE_IS_EPHEMERAL") == "1":
    st.warning(
        "This host has no permanent storage (Render free plan). The queue, history and "
        "any changes made here are lost when the service restarts or sleeps. To keep "
        "friends and sources, copy the code from the Advanced tab into the "
        "SCHEDULES_B64 environment variable in Render."
    )
if config_problem:
    st.error(config_problem + " Fix it in the Advanced tab; the forms are disabled until then.")

forms_ok = cfg is not None and config_problem is None
known_friends = editor.friends(raw_cfg) if forms_ok else []

t_friends, t_topics, t_queue, t_add, t_hist, t_stats, t_set, t_adv = st.tabs(
    ["Friends", "Reel topics", "Queue", "Add reels", "History", "Stats", "Settings", "Advanced"]
)

# --- Friends ---
with t_friends:
    st.caption("Each row is one schedule: which friend gets how many reels, on which days "
               "and at what time. A friend can have more than one schedule.")
    if not forms_ok:
        st.warning("Fix the schedule file in the Advanced tab first.")
    else:
        rules = editor.rule_rows(raw_cfg)
        if rules:
            table = pd.DataFrame(rules)[["friend", "time", "days", "count", "pick", "send_note", "name"]]
            table["time"] = table["time"].map(time_12h)
            table.columns = ["Friend", "Time (IST)", "Days", "Reels", "Order", "Send note",
                             "Schedule name"]
            st.dataframe(table, use_container_width=True, hide_index=True)
        else:
            st.info("No friends yet. Add the first one below.")

        st.subheader("Test: send one reel to every friend now")
        try:
            test_state = test_send_status(get_setting)
        except sqlite3.Error as e:
            st.error(f"Could not read the test send status: {e}")
            test_state = None
        if test_state is not None:
            st.caption("Sends one waiting reel to each friend right away, through the same "
                       "checks as a normal drop: pause, quiet days, the daily cap (test reels "
                       "count towards it) and the gap between reels. The scheduler does the "
                       "sending within about 20 seconds.")
            if sender_name == "dry_run":
                st.caption("Dry-run mode: the reels are only printed in the log, nothing "
                           "reaches Instagram.")
            if test_state["requested"]:
                st.info("A test send is requested and waiting for the scheduler. "
                        "Reload this page in a minute to see the result.")
            elif st.button("Send a test reel to every friend", disabled=not known_friends):
                try:
                    set_setting(TEST_SEND_REQUEST_KEY, "1")
                except sqlite3.Error as e:
                    st.error(f"Could not request the test send: {e}")
                else:
                    st.session_state["flash"] = ("Test send requested for "
                                                 f"{len(known_friends)} friend(s).")
                    st.rerun()
            if test_state["result"]:
                st.write(f"**Last test** ({timestamp_12h(test_state['at'])}): "
                         f"{test_state['result']}")

        st.subheader("Add a friend or another schedule")
        with st.form("add_friend", clear_on_submit=True):
            a1, a2 = st.columns(2)
            new_friend = a1.text_input("Friend's Instagram username", placeholder="friend_username")
            new_count = a2.number_input("Reels per send", min_value=1, max_value=20, value=3, step=1)
            st.caption("Send at (IST)")
            new_time = time_picker("20:00", "add_time")
            new_days = st.multiselect(
                "Days", editor.ALL_DAYS, default=list(editor.ALL_DAYS),
                format_func=lambda d: editor.DAY_LABELS[d],
                help="Leave all seven selected to send every day.",
            )
            b1, b2 = st.columns(2)
            new_pick = b1.selectbox("Order", PICK_MODES, help="oldest: first found, first sent. "
                                                               "random: any waiting reel.")
            new_note = b2.checkbox("Send the reel's note as a message first", value=False)
            if st.form_submit_button("Add schedule"):
                apply_change(
                    lambda d: editor.add_friend_rule(
                        d, new_friend, time_24h(*new_time), int(new_count), new_days,
                        new_pick, new_note),
                    f"Added a schedule for @{editor.clean_username(new_friend)}.",
                )
        st.caption("The friend should follow the sending account, or the reels arrive under "
                   "Message requests. Only add people who agreed to receive them.")

        if rules:
            st.subheader("Change or remove a schedule")
            by_name = {r["name"]: r for r in rules}
            chosen = st.selectbox(
                "Schedule", list(by_name),
                format_func=lambda n: f"@{by_name[n]['friend']} · {time_12h(by_name[n]['time'])} · "
                                      f"{by_name[n]['days']} · {by_name[n]['count']} reel(s)",
            )
            rule = by_name[chosen]
            with st.form(f"edit_{chosen}"):
                e1, e2 = st.columns(2)
                ed_friend = e1.text_input("Friend's Instagram username", value=rule["friend"])
                try:
                    current_count = max(1, min(20, int(rule["count"])))
                except (TypeError, ValueError):
                    current_count = 1
                ed_count = e2.number_input("Reels per send", min_value=1, max_value=20,
                                           value=current_count, step=1)
                st.caption("Send at (IST)")
                ed_time = time_picker(rule["time"], f"edit_time_{chosen}")
                ed_days = st.multiselect("Days", editor.ALL_DAYS,
                                         default=editor.days_to_list(rule["days"]),
                                         format_func=lambda d: editor.DAY_LABELS[d])
                f1, f2 = st.columns(2)
                pick_index = PICK_MODES.index(rule["pick"]) if rule["pick"] in PICK_MODES else 0
                ed_pick = f1.selectbox("Order", PICK_MODES, index=pick_index)
                ed_note = f2.checkbox("Send the reel's note as a message first",
                                      value=rule["send_note"])
                s1, s2 = st.columns(2)
                save_clicked = s1.form_submit_button("Save changes")
                remove_clicked = s2.form_submit_button("Remove this schedule")
            if save_clicked:
                apply_change(
                    lambda d: editor.update_rule(
                        d, chosen, ed_friend, time_24h(*ed_time), int(ed_count), ed_days,
                        ed_pick, ed_note),
                    f"Saved the schedule for @{editor.clean_username(ed_friend)}.",
                )
            if remove_clicked:
                apply_change(lambda d: editor.remove_rule(d, chosen),
                             f"Removed the schedule {chosen}.")

            st.subheader("Remove a friend")
            gone = st.selectbox("Friend", known_friends, key="remove_friend_name",
                                format_func=lambda f: f"@{f}")
            if st.button("Remove this friend and all their schedules"):
                apply_change(lambda d: editor.remove_friend(d, gone), f"Removed @{gone}.")

# --- Reel topics (automatic discovery) ---
with t_topics:
    # The scheduler does the Instagram fetching with its own login. The dashboard never
    # logs in: it edits the settings and can set the refill-request flag.
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
        st.write("**Automatic reel finding:** " + ("✅ on" if disc["enabled"] else "⛔ off"))
        d1, d2, d3 = st.columns(3)
        d1.metric("Waiting in queue", pending)
        d2.metric("Refill below", disc["min_pending"] if disc["min_pending"] is not None else "-")
        d3.metric(
            "Check every",
            f"{disc['check_every_minutes']} min" if disc["check_every_minutes"] is not None else "-",
        )
        st.write(f"**Last refill:** {timestamp_12h(disc['last_run'])}  \n"
                 f"**Last result:** {disc['last_summary']}")
        if disc["refill_pending"]:
            st.info("A refill is requested and waiting for the scheduler.")
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

    if not forms_ok:
        st.warning("Fix the schedule file in the Advanced tab first.")
    else:
        st.divider()
        st.subheader("Where reels come from")
        st.caption("Sources decide what kind of reels are found. Creators give the most "
                   "control over the topic: add accounts that post what your friend likes. "
                   "Explore lets Instagram choose.")
        sources = editor.source_rows(raw_cfg)
        if sources:
            src_table = pd.DataFrame(sources)
            src_table.columns = ["Type", "Value", "Hashtag mode", "Only for"]
            st.dataframe(src_table, use_container_width=True, hide_index=True)
            r1, r2 = st.columns([3, 1])
            drop = r1.selectbox("Source to remove", range(len(sources)),
                                format_func=lambda i: editor.source_label(sources[i]))
            if r2.button("Remove source"):
                apply_change(lambda d: editor.remove_source(d, int(drop)),
                             f"Removed the source {editor.source_label(sources[int(drop)])}.")
        else:
            st.info("No sources yet. Add one below, then turn automatic reel finding on.")

        kind = st.selectbox("Add a source", list(editor.SOURCE_CHOICES))
        stype, fixed_value = editor.SOURCE_CHOICES[kind]
        n1, n2, n3 = st.columns(3)
        if fixed_value is None:
            prompts = {"user": "Creator's username", "collection": "Collection name",
                       "hashtag": "Hashtag (without #)"}
            typed_value = n1.text_input(prompts[stype], key=f"src_value_{stype}")
        else:
            typed_value = fixed_value
            n1.write("No search term needed.")
        src_mode = n2.selectbox("Hashtag mode", ("top", "recent")) if stype == "hashtag" else None
        src_to = n3.selectbox("Only for", [ANY_FRIEND] + known_friends,
                              help="Reels from this source go only to this friend.")
        if stype == "hashtag":
            st.caption("On the test account, Instagram returned nothing for hashtag searches. "
                       "Creators and Explore worked.")
        if st.button("Add source"):
            apply_change(
                lambda d: editor.add_source(
                    d, stype, typed_value, src_mode, None if src_to == ANY_FRIEND else src_to),
                f"Added the source {stype}: {typed_value}.",
            )

        st.divider()
        st.subheader("Topic filter and limits")
        raw_disc = raw_cfg.get("discovery") if isinstance(raw_cfg.get("discovery"), dict) else {}
        with st.form("discovery_settings"):
            on = st.toggle("Find reels automatically", value=bool(raw_disc.get("enabled")))
            k1, k2 = st.columns(2)
            keep_words = k1.text_area(
                "Only reels whose caption mentions any of these",
                value="\n".join(getattr(disc_cfg, "keywords", []) or []),
                help="One word or phrase per line (or separated by commas). Leave empty for "
                     "no filter. A reel without a caption never passes this filter.",
            )
            drop_words = k2.text_area(
                "Never send reels whose caption mentions any of these",
                value="\n".join(getattr(disc_cfg, "exclude_keywords", []) or []),
                help="One word or phrase per line (or separated by commas).",
            )
            with st.expander("Numbers"):
                g1, g2, g3 = st.columns(3)
                min_pending = g1.number_input(
                    "Refill when fewer than this are waiting", 1, 500,
                    int(getattr(disc_cfg, "min_pending", 15)))
                per_source = g2.number_input(
                    "Reels asked from each source", 1, 50,
                    int(getattr(disc_cfg, "fetch_per_source", 10)))
                per_refill = g3.number_input(
                    "Most reels added per refill", 1, 100,
                    int(getattr(disc_cfg, "max_per_refill", 30)))
                h1, h2, h3 = st.columns(3)
                every = h1.number_input(
                    "Minutes between refills", 30, 1440,
                    int(getattr(disc_cfg, "check_every_minutes", 180)))
                likes = h2.number_input(
                    "Minimum likes", 0, 100_000_000, int(getattr(disc_cfg, "min_likes", 0)))
                delay = h3.number_input(
                    "Seconds between sources", 0, 120,
                    int(getattr(disc_cfg, "delay_seconds", 5)))
            if st.form_submit_button("Save reel topic settings"):
                apply_change(
                    lambda d: editor.set_discovery(
                        d, on,
                        {"min_pending": int(min_pending), "fetch_per_source": int(per_source),
                         "max_per_refill": int(per_refill), "check_every_minutes": int(every),
                         "min_likes": int(likes), "delay_seconds": int(delay)},
                        keep_words, drop_words),
                    "Saved the reel topic settings.",
                )
        st.caption("Finding reels uses Instagram's unofficial access with the sending account. "
                   "Keep refills infrequent and use a test account.")

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
        st.dataframe(show_times_12h(df, ["added_at"]), use_container_width=True,
                     hide_index=True)
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
                "Friend (blank = unassigned, any schedule can send it)", key="assign_to"
            )
            if st.button("Set friend"):
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

# --- Add reels ---
with t_add:
    st.caption("Paste specific reels here. They join the same queue as the reels found "
               "automatically.")
    text = st.text_area("Paste reel links (one per line)", height=200)
    note = st.text_input(
        "Note (optional, applied to every link added)",
        help="Sent as a message before the reel by schedules with 'send note' on.",
    )
    to_choice = st.selectbox("Send to", [ANY_FRIEND] + known_friends,
                             help="Pick a friend to keep these reels for them only.")
    if st.button("Add to queue"):
        if not text.strip():
            st.warning("Paste at least one link.")
        else:
            added, errors = add_links(text, note, add_reel,
                                      recipient=None if to_choice == ANY_FRIEND else to_choice)
            if added:
                st.success(f"Added {added} reel(s)")
            elif errors:
                st.error("Nothing was added.")
            for err in errors:
                st.warning(err)

# --- History ---
with t_hist:
    try:
        with connect() as c:
            log = pd.read_sql_query(
                "SELECT * FROM send_log ORDER BY id DESC LIMIT 200", c
            )
        st.dataframe(show_times_12h(log, ["sent_at"]), use_container_width=True,
                     hide_index=True)
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

# --- Settings ---
with t_set:
    st.subheader("Sending")
    if paused:
        st.warning("Sending is paused.")
        if st.button("▶️ Resume sending"):
            set_paused(False)
            st.rerun()
    else:
        if st.button("⏸️ Pause all sending"):
            set_paused(True)
            st.rerun()
    st.caption("The pause switch applies immediately. After Instagram blocks or challenges "
               "the account, sending pauses by itself: clear the warning in the Instagram "
               "app and wait a day before resuming.")

    if forms_ok:
        st.subheader("Limits")
        with st.form("limits"):
            l1, l2 = st.columns(2)
            cap = l1.number_input("Most reels sent per day (all friends together)", 1, 100,
                                  int(cfg.daily_cap))
            gap = l2.number_input("Seconds between two reels", 0, 3600, int(cfg.gap_seconds))
            quiet = st.text_area(
                "Quiet days (nothing is sent)", value="\n".join(cfg.quiet_days),
                help="One date per line, as YYYY-MM-DD, for example 2026-12-25.",
            )
            if st.form_submit_button("Save limits"):
                apply_change(
                    lambda d: editor.set_limits(d, int(cap), int(gap), quiet.splitlines()),
                    "Saved the limits.",
                )
        if cfg.daily_cap > HIGH_DAILY_CAP or cfg.gap_seconds < 30:
            st.warning("These limits are loose for the Instagram route. Many reels per day or "
                       "short gaps make a block or ban more likely; 5 to 10 a day and 30 "
                       "seconds or more are safer.")
        st.caption(f"Timezone: `{cfg.timezone}`. Change it in the Advanced tab; a timezone "
                   "change needs a restart of the service.")

    st.subheader("Instagram login")
    st.write(f"**Sender:** `{sender_name}` (set by the `SENDER` environment variable)")
    if send_state["error"]:
        st.error(f"Last start failed at {timestamp_12h(send_state['at']) or 'an unknown time'}: "
                 f"{send_state['error']}")
        st.caption("After a failed Instagram login the scheduler waits (60 minutes by "
                   "default) before trying again, so the account is not flagged.")
        if send_state["retry_requested"]:
            st.info("A retry is requested; the scheduler will try again within a minute.")
        elif st.button("Retry login now"):
            try:
                set_setting(SENDER_RETRY_KEY, "1")
            except sqlite3.Error as e:
                st.error(f"Could not request a retry: {e}")
            else:
                st.session_state["flash"] = "Retry requested. Reload this page in a minute."
                st.rerun()
    else:
        st.caption("The scheduler started its sender without an error.")
    st.write("**Session file:** " + ("present" if os.path.exists(SESSION_PATH) else "none yet"))
    if PUBLIC:
        st.caption("Session file upload is off while the dashboard has no password. "
                   "Use the IG_SESSION_B64 variable in Render instead.")
    else:
        with st.expander("Upload a session file from a computer where the login works"):
            st.caption("Instagram checks logins from cloud servers more strictly. If the login "
                       "fails here, log in on your own computer first and upload its "
                       "`data/session.json`. The file holds login cookies: treat it like a "
                       "password. Its content is never shown.")
            uploaded = st.file_uploader("session.json", type=["json"])
            if uploaded is not None and st.button("Save session file"):
                try:
                    save_session_file(uploaded.getvalue(), SESSION_PATH)
                except (ValueError, OSError) as e:
                    st.error(f"Not saved: {e}")
                else:
                    st.session_state["flash"] = ("Session file saved. Press 'Retry login now' "
                                                 "or restart the service to use it.")
                    st.rerun()

# --- Advanced ---
with t_adv:
    if raw_text is not None and os.getenv("STORAGE_IS_EPHEMERAL") == "1":
        with st.expander("Keep these settings after a restart (Render free plan)"):
            st.caption("Copy this code into the SCHEDULES_B64 environment variable in Render "
                       "and save. On every start the schedule is restored from it. Copy it "
                       "again after each change you want to keep.")
            st.code(base64.b64encode(raw_text.encode("utf-8")).decode("ascii"),
                    language=None, wrap_lines=True)
    st.caption("The schedule file behind every form on this dashboard. Edit it by hand only "
               "if a form cannot do what you need. Times in this file are 24-hour IST "
               "(\"21:00\" is 9:00 PM); always quote them, like \"21:00\".")
    if raw_text is not None:
        new_text = st.text_area("schedules.yaml", raw_text, height=420)
        if st.button("Save file"):
            try:
                save_schedules(CONFIG_PATH, new_text)
            except (ValueError, OSError) as e:
                st.error(f"Not saved (existing file kept): {e}")
            else:
                st.session_state["flash"] = ("Saved. The scheduler picks up the change within "
                                             "about a minute; a timezone change needs a restart.")
                st.rerun()
