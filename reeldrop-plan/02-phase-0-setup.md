# Phase 0 — Setup

**Goal:** accounts, tools and an empty project ready to code.
**Time:** 1 day

## Checklist

- [ ] Route chosen (see `00-overview-and-reality-check.md`)
- [ ] Friend agreed; their username noted
- [ ] Separate Instagram **test account** created (Route B), or a Telegram bot (Route D)
- [ ] Python 3.11+, Git, VS Code installed
- [ ] Private GitHub repo created
- [ ] Virtual environment + `requirements.txt`
- [ ] `.gitignore`, `.env.example`, `config/schedules.yaml` created

## Commands

```bash
mkdir reeldrop && cd reeldrop
git init
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate
mkdir -p config data reeldrop/senders
touch reeldrop/__init__.py reeldrop/senders/__init__.py
```

## requirements.txt

```text
apscheduler>=3.10,<4      # v4 has a different API; stay on 3.x
pyyaml
python-dotenv
requests
streamlit
pandas
instagrapi                # only needed for Route B
```

```bash
pip install -r requirements.txt
```

## .gitignore

```text
.venv/
__pycache__/
.env
data/
*.db
session.json
*.log
```

## .env.example  (copy to `.env` and fill in)

```text
SENDER=dry_run                 # dry_run | instagrapi | official | telegram
TZ_NAME=Asia/Kolkata

# Route B
IG_USERNAME=
IG_PASSWORD=

# Route A
IG_ACCESS_TOKEN=
IG_RECIPIENT_IGSID=

# Route D (also used for alerts to yourself)
TELEGRAM_BOT_TOKEN=
TELEGRAM_FRIEND_CHAT_ID=
TELEGRAM_MY_CHAT_ID=

DASHBOARD_PASSWORD=change-me
```

## config/schedules.yaml

```yaml
timezone: Asia/Kolkata
daily_cap: 10          # never send more than this per day
gap_seconds: 45        # wait between two messages
rules:
  - name: evening_drop
    days: "*"          # every day
    time: "20:00"      # ALWAYS quote times — unquoted 20:00 can be read as a number
    count: 3
    recipient: friend_username
  - name: weekend_binge
    days: "sat,sun"
    time: "11:00"
    count: 5
    recipient: friend_username
```

✅ **Done when:** `pip install` works, the folder structure exists, and your first commit is pushed (without `.env`).
