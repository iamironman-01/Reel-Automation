# Run ReelDrop on your own computer

This guide is for the `local-run` branch. It is **not** part of `main`, and nothing here is deployed.
Everything starts in **dry-run** mode: it prints what it would send and sends nothing.

You need **Python 3.11 or newer** and Git.

## 1. Get the branch

```bash
git clone https://github.com/iamironman-01/Reel-Automation.git
cd Reel-Automation
git checkout local-run
```

(If you already cloned it: `git fetch origin && git checkout local-run`.)

## 2. One-time setup

| System | Command |
|--------|---------|
| Windows | `scripts\setup_local.bat` |
| Linux / macOS / Git Bash | `bash scripts/setup_local.sh` |

This creates `.venv`, installs the packages, and creates `.env` with `SENDER=dry_run` and a random dashboard password. **The password is printed once**; it is also saved in `.env` (git ignores that file).

## 3. See it work (2-minute demo)

| System | Command |
|--------|---------|
| Windows | `scripts\run_local.bat demo` |
| Linux / macOS / Git Bash | `bash scripts/run_local.sh demo` |

It adds 5 fake reels in a separate folder (`.local-demo/`), schedules a rule 2 minutes ahead, and starts the scheduler. After the minute passes you should see two `[DRY RUN] would send ...` lines. Stop it with `Ctrl+C`, then check the result:

```bash
python scripts/local_demo.py --show      # two reels now show "sent"
```

The demo cannot touch your real queue or database, and it ignores any Telegram or Instagram values in `.env`.

## 4. Use it for real (still dry-run)

Open two terminals in the project folder.

```bash
# terminal 1: add reels and run the scheduler
scripts\run_local.bat cli add "https://www.instagram.com/reel/XXXXXXXXXXX/"      # Windows
bash scripts/run_local.sh cli add "https://www.instagram.com/reel/XXXXXXXXXXX/"  # Linux/macOS
scripts\run_local.bat scheduler           # or: bash scripts/run_local.sh scheduler

# terminal 2: the dashboard
scripts\run_local.bat dashboard           # or: bash scripts/run_local.sh dashboard
```

Open http://localhost:8501 and log in with the password from step 2.

- The schedule is `config/schedules.yaml` (or edit it in the dashboard's **Schedules** tab; the scheduler picks changes up within about a minute). Always quote times: `"20:00"`.
- Your computer must be **on and awake** at the scheduled time. A job that is missed by more than 10 minutes is skipped.
- Other commands: `cli list`, `cli remove ID`, `cli move ID up|down|top|bottom`, `test` (runs the test suite).

## 5. Send for real (only when you are ready)

1. Read the golden rules in `README.md`: use a **test account**, and get your friend's consent.
2. Safest: the Telegram route. In `.env` set `SENDER=telegram`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_FRIEND_CHAT_ID`.
3. Instagram (Route B) is against Instagram's Terms and can get the account blocked. If you use it, set `SENDER=instagrapi`, `IG_USERNAME`, `IG_PASSWORD`, keep `daily_cap` at 5 to 10, and test with your own second account first.
4. Restart the scheduler after changing `.env`.

Never commit `.env`, `data/` or `session.json` (they are already in `.gitignore`).

## Reset

Delete `data/` to start with an empty queue. Delete `.local-demo/` to remove demo leftovers.
