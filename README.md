# ReelDrop

ReelDrop keeps a queue of Instagram reel links and sends them to a friend as direct messages on a schedule you define.

> Example: "Every day at 8:00 PM, send 3 reels to my friend. On weekends at 11:00 AM, send 5."

It has five parts:

- **Reel queue**: reel links stored in SQLite, oldest first.
- **Schedule rules**: time, days, number of reels and recipient, written in `config/schedules.yaml`.
- **Scheduler**: at each rule's time it sends the next N pending reels (APScheduler 3.x).
- **Pluggable senders**: dry-run, `instagrapi`, official API, Telegram. The queue, scheduler and dashboard do not depend on which one is used.
- **Dashboard**: a Streamlit page to add reels, view the queue and history, edit schedules, and pause sending.

## Golden rules

1. Never start with your main Instagram account. Use a test account.
2. Build and test everything in dry-run mode first.
3. Your friend must agree to receive automated reels. Stop if they ask you to.
4. Never commit `.env`, `session.json` or the database. `.gitignore` already excludes them.

## Read this before sending real DMs

Instagram has no open, official way for a personal account to DM a friend from code. Pick a route knowingly:

| Route | `SENDER` value | How it works | Sends any reel to a friend? | Risk |
|-------|----------------|--------------|-----------------------------|------|
| A. Official Messaging API | `official` | Meta Graph API with a Business/Creator account | No. Only replies within 24 hours after the friend messages you, and it sends the reel link as text | Low, fully allowed |
| B. Unofficial private API (`instagrapi`) | `instagrapi` | Logs in as your account and uses Instagram's mobile API | Yes | **High**: violates Instagram's Terms of Service. Action blocks or a permanent ban are possible |
| C. Browser automation | not implemented | Clicks through instagram.com | Yes | High, same Terms issue, and breaks when the site changes |
| D. Telegram bot | `telegram` | Sends the reel links to your friend on Telegram | Yes, as links | None |
| Dry run | `dry_run` (default) | Prints what it would send | n/a | None |

**Route B is against Instagram's Terms of Service.** Use only a separate test account, keep `daily_cap` low (5 to 10), keep `gap_seconds` at 30 or more, and expect that the account may be blocked or banned. The library can also stop working after an Instagram update. If you want zero account risk, use Route D. Route C is not covered by this project.

## Quickstart

Requires Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then edit .env; keep SENDER=dry_run for now
```

Add some reels (see [CLI usage](#cli-usage)):

```bash
python -m reeldrop add "https://www.instagram.com/reel/ABC123xyz/" --note "funny"
python -m reeldrop list
```

Run the scheduler in dry-run mode. It prints `[DRY RUN] would send ...` at each rule's time and marks those reels as sent:

```bash
python main.py
```

To test quickly, set a rule in `config/schedules.yaml` two minutes in the future with `count: 2` and `gap_seconds: 5`.

Run the dashboard in a second terminal and open http://localhost:8501:

```bash
streamlit run dashboard.py
```

Only switch `SENDER` in `.env` to a real route after dry-run works, and start with a test account (see the golden rules).

## CLI usage

```bash
python -m reeldrop add "<reel url>" [--note "text"] [--to USERNAME]   # --to: only that friend's rule sends it
python -m reeldrop list [--status pending|sent|failed]
python -m reeldrop remove <id>
python -m reeldrop move <id> {up,down,top,bottom}                     # reorder a pending reel
python -m reeldrop import-collection "<name>" [--limit N] [--dry-run]  # needs instagrapi credentials
python -m reeldrop discover [--sources | --dry-run]                    # auto-discovery, see below
```

`add` rejects links that are not Instagram reel/post links, and reels already in the queue. Tracking parameters are stripped, so the same reel with a different `?igsh=` counts as a duplicate.

## Configuration

### `.env` (secrets, never committed)

| Variable | Used for |
|----------|----------|
| `SENDER` | `dry_run`, `instagrapi`, `official` or `telegram` |
| `TZ_NAME` | Timezone for timestamps, default `Asia/Kolkata` |
| `IG_USERNAME`, `IG_PASSWORD` | Route B login |
| `IG_ACCESS_TOKEN`, `IG_RECIPIENT_IGSID` | Route A |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_FRIEND_CHAT_ID` | Route D |
| `TELEGRAM_MY_CHAT_ID` | Optional alerts to yourself (pause, empty queue, results) |
| `TELEGRAM_CONTROL` | `1` to let your bot accept `/add`, `/pause`, `/status` from `TELEGRAM_MY_CHAT_ID` (see Extensions) |
| `DASHBOARD_PASSWORD` | Dashboard password. Change the default `change-me` |

### `config/schedules.yaml`

```yaml
timezone: Asia/Kolkata
daily_cap: 10          # never send more than this per day
gap_seconds: 45        # wait between two messages
rules:
  - name: evening_drop
    days: "*"          # every day
    time: "20:00"      # ALWAYS quote times
    count: 3
    recipient: friend_username
  - name: weekend_binge
    days: "sat,sun"
    time: "11:00"
    count: 5
    recipient: friend_username
```

| Field | Meaning |
|-------|---------|
| `timezone` | IANA timezone used for all rule times |
| `daily_cap` | Maximum reels sent per day across all rules. Counted from today's `sent` rows in the log |
| `gap_seconds` | Pause between two messages in the same run |
| `rules[].name` | Unique name, shown in logs and history |
| `rules[].days` | `"*"` for every day, or cron-style weekdays such as `"sat,sun"` or `"mon-fri"` |
| `rules[].time` | `"HH:MM"` in 24-hour format |
| `rules[].count` | Reels to send when the rule fires (at least 1) |
| `rules[].recipient` | Username or ID the sender delivers to |

**Always quote times.** Unquoted `20:00` can be parsed by YAML as a number, and the config loader rejects times that are not `HH:MM`.

The scheduler checks the file's modification time every 60 seconds and reloads it when it changes (see [Live schedule reload](#live-schedule-reload)). Optional fields (`send_note`, `pick`, `quiet_days`) are described under [Extensions](#extensions).

## Safety limits and kill switch

- **Daily cap**: no more than `daily_cap` reels per day in total, however many rules fire.
- **Gap**: `gap_seconds` between messages.
- **Dry run**: the default sender never contacts anyone.
- **Kill switch**: a pause flag stored in the database. While it is set, every rule fires but sends nothing. Toggle it from the dashboard (Settings tab), or from the shell:
  ```bash
  python -c "from reeldrop.settings import set_paused; set_paused(True)"    # pause
  python -c "from reeldrop.settings import set_paused; set_paused(False)"   # resume
  ```
- **Auto-pause**: if the sender reports that Instagram blocked or challenged the account, sending pauses and an alert is sent (if Telegram alerts are configured). Normal errors are retried once. The status and log are written together right after a successful send, and missed runs are coalesced, so a crash or restart should not send the same reel twice.
- **After a pause**: clear any warning in the Instagram app, wait 24 to 48 hours, lower `daily_cap`, then resume.

Stop immediately if Instagram shows any warning. See [reeldrop-plan/11-risks-and-safety.md](reeldrop-plan/11-risks-and-safety.md) for the full risk table.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

Tests use temporary databases and do not touch `data/`.

## Docker (run 24/7)

`docker-compose.yml` defines two services that share `./data` and `./config`:

- `scheduler`: runs `python main.py`.
- `dashboard`: runs Streamlit on port 8501 and has a health check.

Both restart automatically (`restart: unless-stopped`).

```bash
cp .env.example .env               # fill in; secrets stay on the host only
mkdir -p data config
docker compose up -d --build       # start
docker compose logs -f scheduler   # watch logs
docker compose restart scheduler   # restart (needed after a timezone change or code update)
docker compose down                # stop
```

`.env` is passed to the containers at run time and is excluded from the image by `.dockerignore`, as are `data/`, databases, logs and `session.json`.

**File permissions.** The containers run as a non-root user with UID/GID 1000. The bind-mounted `./data` and `./config` must be writable by that user. If your host user is also UID 1000 (check with `id -u`) nothing is needed. Otherwise either fix ownership:

```bash
sudo chown -R 1000:1000 data config
```

or build the image with your own IDs by setting `REELDROP_UID` and `REELDROP_GID` in `.env` (or the shell) and running `docker compose up -d --build`.

**Security.** Do not expose port 8501 to the open internet. The dashboard controls sending, and a single shared password is not enough protection. Reach it over a VPN such as Tailscale, or change the port mapping to `127.0.0.1:8501:8501` and use an SSH tunnel. `.env` and `data/session.json` stay on the host.

**Surviving a reboot.** The containers come back on their own if the Docker service starts at boot. On systemd Linux: `sudo systemctl enable docker`. Then reboot once and check `docker compose ps` and the scheduler logs before trusting it.

**Host choice.** A home machine or Raspberry Pi is best for Route B, because home IPs trigger fewer Instagram security checks than cloud IPs. A free-tier cloud VM is fine for Routes A and D.

### Backup

`scripts/backup.sh` archives `data/` and `config/` into `~/reeldrop-backups/reeldrop-backup-YYYY-MM-DD.tgz` and deletes archives older than 56 days (8 weeks).

```bash
scripts/backup.sh                       # default folder
scripts/backup.sh /mnt/usb/reeldrop     # another folder
REELDROP_KEEP_DAYS=0 scripts/backup.sh  # never delete old backups
```

Weekly, Sundays at 2 AM, via `crontab -e`:

```cron
0 2 * * 0  /home/youruser/reeldrop/scripts/backup.sh >> /home/youruser/reeldrop-backup.log 2>&1
```

The archive contains `session.json` and the database, so keep it private. To restore, stop the containers, extract the archive in the project folder (`tar xzf reeldrop-backup-YYYY-MM-DD.tgz`), and start them again.

## Project layout

```
reeldrop/
├── config/
│   └── schedules.yaml
├── data/                      # created at runtime (db, session, logs); not committed
├── reeldrop/
│   ├── __init__.py
│   ├── __main__.py            # CLI: add / list / remove / move / import-collection / discover
│   ├── db.py                  # connection + schema + in-place migration
│   ├── queue.py               # queue functions
│   ├── importer.py            # import reels from a saved collection
│   ├── discovery.py           # find reels from hashtags / users / collections
│   ├── telegram_control.py    # /add /pause /status from your phone
│   ├── settings.py            # pause switch
│   ├── config.py              # loads schedules.yaml
│   ├── job.py                 # what runs when a rule fires
│   ├── notify.py              # alerts to yourself
│   └── senders/
│       ├── __init__.py        # get_sender()
│       ├── base.py
│       ├── dry_run.py
│       ├── instagrapi_sender.py
│       ├── official_api_sender.py
│       └── telegram_sender.py
├── scripts/
│   └── backup.sh              # tar data/ and config/ into a dated .tgz
├── tests/
├── reeldrop-plan/             # step-by-step phase documentation
├── main.py                    # starts the scheduler
├── dashboard.py               # Streamlit UI
├── .env                       # secrets, never commit
├── .env.example
├── .gitignore
├── .dockerignore
├── pytest.ini
├── requirements.txt
├── requirements-dev.txt
├── Dockerfile
└── docker-compose.yml
```

## Deploy on Render

See [DEPLOY_RENDER.md](DEPLOY_RENDER.md): one always-on web service with a persistent disk, defined in `render.yaml`, redeployed automatically from GitHub after CI passes.

## Documentation

The full build plan, one phase per file, is in [reeldrop-plan/](reeldrop-plan/README.md): overview and route comparison, architecture, setup, queue, scheduler, sender, reliability, dashboard, deployment, extensions, testing plan, and risks.

## Extensions

Nine optional extensions are implemented. All are off by default or have no effect until you use them: with the shipped `config/schedules.yaml` and no new fields, ReelDrop behaves as before. The idea list is in [reeldrop-plan/09-phase-7-extensions.md](reeldrop-plan/09-phase-7-extensions.md).

```yaml
timezone: Asia/Kolkata
daily_cap: 10
gap_seconds: 45
quiet_days: ["2026-12-25", "2027-01-01"]   # no sends on these dates
rules:
  - name: evening_drop
    days: "*"
    time: "20:00"
    count: 3
    recipient: friend_username
    send_note: true      # send the reel's note before each reel
    pick: random         # random pending reels instead of oldest first
```

### Captions (`send_note`)

Set `send_note: true` on a rule. For each reel that has a non-empty note (`--note` on the CLI, or the Note field on the dashboard's Add reels tab), ReelDrop first sends the note as a text message to the same recipient, then the reel. Default is `false`.

- Notes are extra DMs. `daily_cap` and the sent counter count reels only, so a day with the cap reached still contains up to that many notes as well.
- The note uses the same retry-once and block handling as the reel. A block while sending a note pauses sending, exactly like a reel block. If the note still fails after the retry, that reel is marked `failed` and is not sent.
- Telegram posts the note as-is (without the 🎬 prefix). With the official API route, a note is a text message subject to the same 24-hour reply window as the link. Dry run prints `[DRY RUN] would send note ...`.

### Random pick (`pick`)

`pick: oldest` (default) sends the oldest pending reels. `pick: random` chooses the rule's reels at random from the pending ones. Within a run the chosen reels are sent in queue order. Only pending reels are ever chosen.

### Quiet days (`quiet_days`)

A top-level list of dates (`YYYY-MM-DD`, quoted or not) on which no rule sends anything. The date is checked in the config `timezone`. A skipped run is only logged: no alert, and nothing is counted. Anything that is not a valid date makes the config invalid.

### Live schedule reload

The scheduler checks `config/schedules.yaml` every 60 seconds. When the file changed, it validates it and then removes jobs for deleted rules and adds or replaces the jobs for the others, so changes to `daily_cap`, `gap_seconds`, `quiet_days` and the rules apply without a restart. Saving from the dashboard validates the file first, so this is the normal way to edit.

- An invalid file is ignored: the running schedule stays, the error is logged, and one alert is sent (if Telegram alerts are set up). The same broken file is reported only once; the next save is checked again.
- A changed `timezone` is not applied. A warning is logged and the scheduler keeps the old timezone until you restart it (`docker compose restart scheduler`). Other changes in the same save are applied.
- `__reload__` is the reload job's internal id and cannot be used as a rule name.
- The reload runs inside the scheduler process. It was tested against a fake scheduler, not a live APScheduler run.

### Stats

The dashboard's **Stats** tab shows the all-time number sent and failed, the failure rate (`failed / (sent + failed)`), reels sent per day for the last 30 days, and per week (Monday to Sunday) for the last 12 weeks. Notes are not counted.

### Multiple friends

Each reel can have a recipient. Add one with `python -m reeldrop add URL --to alice`, with the "Send to" box on the dashboard's Add reels tab, or assign one later from the Queue tab. A rule only sends reels assigned to its `recipient` plus unassigned reels. A reel assigned to one friend is never sent to another. Matching ignores case. `list` shows the recipient column (`-` means any rule can send it). Existing databases are upgraded automatically on startup, with no data loss.

### Queue order

Pending reels are sent in queue order, which is the order they were added unless you change it. Reorder with `python -m reeldrop move ID up|down|top|bottom` or the Up/Down/Top/Bottom buttons in the Queue tab. Only pending reels can be moved. `pick: random` ignores queue order.

### Auto-import from a saved collection

`python -m reeldrop import-collection "Funny" --limit 20` logs in with `IG_USERNAME` / `IG_PASSWORD` (Route B credentials, same session file) and adds the reels in your saved collection with that name. Photos and albums are skipped, reels already queued count as duplicates, and `--dry-run` lists what would be added. This uses the same unofficial API as Route B, so the same Terms-of-Service risk applies. If Instagram blocks or challenges the account, the import stops and sending is paused.

### Telegram control

Set `TELEGRAM_CONTROL=1` in `.env` (plus `TELEGRAM_BOT_TOKEN` and `TELEGRAM_MY_CHAT_ID`) and the scheduler also listens to your bot. Commands: `/add <link> [note]` (or just paste a reel link), `/pause`, `/resume`, `/status`, `/queue`, `/help`. Only messages from `TELEGRAM_MY_CHAT_ID` are accepted; everything else is ignored. The bot uses long polling, so it needs no open port.

### Automatic discovery (no pasted links)

ReelDrop can find reels by itself instead of you pasting links. You list sources (hashtags, creators, or one of your saved collections). When fewer than `min_pending` reels are waiting, the scheduler fetches reels from those sources and adds them to the queue. Sending works exactly as before.

**Requirements.** Discovery reads Instagram through `instagrapi`, so it needs `IG_USERNAME` / `IG_PASSWORD` in `.env`. Two setups work:

- `SENDER=instagrapi` (Route B): one login is used for both discovery and sending.
- `SENDER=telegram`: discovery logs in separately, read-only, and reels are delivered over Telegram.

The official API (`SENDER=official`) cannot browse reels, so it cannot be used for discovery.

**Config.** Add a top-level `discovery:` block to `config/schedules.yaml` (or edit it in the dashboard's Schedules tab):

```yaml
discovery:
  enabled: true
  min_pending: 15            # refill when fewer than this are pending
  fetch_per_source: 10       # reels requested per source per refill
  max_per_refill: 30         # hard cap added in one refill
  check_every_minutes: 180   # at most one automatic refill per 3 hours
  min_likes: 0
  delay_seconds: 5           # pause between sources
  sources:
    - type: hashtag
      value: funny
      mode: top              # top | recent
    - type: user
      value: some_creator
    - type: collection
      value: "Funny"
      to: friend_username    # optional: only that friend gets these
```

| Field | Default | Meaning |
|-------|---------|---------|
| `enabled` | `false` | Nothing logs in to Instagram unless this is `true`. It needs at least one source |
| `min_pending` | 15 | Refill when fewer than this many reels are pending (1 to 500) |
| `fetch_per_source` | 10 | Reels requested from each source per refill (1 to 50) |
| `max_per_refill` | 30 | Hard cap on reels added in one refill (1 to 100) |
| `check_every_minutes` | 180 | At most one automatic refill per this many minutes (30 to 1440) |
| `min_likes` | 0 | Skip reels with fewer likes than this |
| `delay_seconds` | 5 | Pause between sources (0 to 120) |
| `sources[].type` | | `hashtag`, `user` or `collection` |
| `sources[].value` | | Hashtag without `#`, username without `@`, or the saved collection's name |
| `sources[].mode` | `top` | Hashtags only: `top` or `recent` |
| `sources[].to` | none | Optional recipient. Reels from this source are only sent by the rule for that friend, like `add --to` |

Only reels are kept (photos and albums are skipped). A reel already queued or sent is never added again. When a rule fires and the queue is empty, one immediate top-up is tried before giving up.

**Throttling and safety.** Discovery is off by default. Automatic refills are limited by `check_every_minutes` (at least 30), `max_per_refill` and `delay_seconds` between sources. `daily_cap` and `gap_seconds` still limit sending. If Instagram blocks or challenges the account, sending is paused and an alert is sent, as for sending.

**Commands.**

```bash
python -m reeldrop discover --sources    # list the configured sources
python -m reeldrop discover --dry-run    # fetch and show what would be added; adds nothing
python -m reeldrop discover              # run one refill now
```

Run `discover --dry-run` first. The dashboard's **Discovery** tab shows the sources, last refill time and result, and pending versus `min_pending`. Its **Refill now** button sets a flag; the scheduler process (not the dashboard, which never logs in to Instagram) picks it up within about a minute.

**Risks.** This uses Instagram's unofficial private API, which violates Instagram's Terms of Service. Accounts can be action-blocked or banned, and the library can break after an Instagram update. Use a test account only, keep `daily_cap` low, and stop if Instagram shows a warning. This feature has been tested with fakes only and has not been verified against live Instagram; the first run on your test account is the real test. See [reeldrop-plan/12-phase-8-auto-discovery.md](reeldrop-plan/12-phase-8-auto-discovery.md).

### Not implemented

Route C (browser automation) is not covered. Telegram control, auto-import and auto-discovery were tested with fakes only, not against the live services.
