# 01 — Architecture

## Components

| Component | Tech | Job |
|-----------|------|-----|
| Reel queue | SQLite | Stores reel links and their status (pending / sent / failed) |
| Schedule rules | YAML file | Times, days, counts, recipients |
| Scheduler | APScheduler 3.x | Fires a job at each scheduled time |
| Sender | Python adapter classes | DryRun / Instagrapi / OfficialApi / Telegram |
| Dashboard | Streamlit | Add reels, view queue, history, pause switch |
| Logs | `logging` + `send_log` table | Every attempt recorded |

## Flow

```
You paste reel links ──► Queue (SQLite)
                              │
schedules.yaml ──► Scheduler ─┤ 20:00 fires "evening_drop"
                              ▼
                     Job: check pause + daily cap
                              │
                     take next N pending reels
                              │
                     Sender.send(friend, reel)  ── wait gap_seconds ── next reel
                              │
                     mark sent / failed  ──► send_log ──► Dashboard
```

## Folder structure (the code project you will create)

```
reeldrop/
├── config/
│   └── schedules.yaml
├── data/                      # created at runtime (db, session, logs)
├── reeldrop/
│   ├── __init__.py
│   ├── __main__.py            # CLI: add / list / remove
│   ├── db.py                  # connection + schema
│   ├── queue.py               # queue functions
│   ├── settings.py            # pause switch (Phase 4)
│   ├── config.py              # loads schedules.yaml
│   ├── job.py                 # what runs when a rule fires
│   ├── notify.py              # alerts to yourself (Phase 4)
│   └── senders/
│       ├── __init__.py        # get_sender()
│       ├── base.py
│       ├── dry_run.py
│       ├── instagrapi_sender.py
│       ├── official_api_sender.py
│       └── telegram_sender.py
├── main.py                    # starts the scheduler
├── dashboard.py               # Streamlit UI
├── .env                       # secrets — never commit
├── .env.example
├── .gitignore
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

## Database

| Table | Columns | Purpose |
|-------|---------|---------|
| `reels` | id, url, shortcode (unique), note, status, added_at | The queue |
| `send_log` | id, reel_id, recipient, rule_name, sender_type, status, error, sent_at | History of every attempt |
| `settings` | key, value | Pause switch and other flags |

The **daily cap** is calculated by counting today's `sent` rows in `send_log`, so no separate counter table is needed.

## Example schedule rules

| Rule | Days | Time | Reels | Recipient |
|------|------|------|-------|-----------|
| evening_drop | every day | 20:00 | 3 | friend_username |
| weekend_binge | sat, sun | 11:00 | 5 | friend_username |
| morning_single | mon–fri | 08:30 | 1 | friend_username |
