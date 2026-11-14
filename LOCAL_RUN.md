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
2. To deliver reels as real Instagram DMs, use the Instagram route. It is against Instagram's Terms and can get the account blocked, so use a test account and keep `daily_cap` at 5 to 10. In `.env` set `SENDER=instagrapi`, `IG_USERNAME`, `IG_PASSWORD`. If the account uses two-factor, also set `IG_TOTP_SECRET` (the base32 seed from your authenticator setup); `IG_VERIFICATION_CODE` is a one-time code if Instagram asks for one.
3. In `config/schedules.yaml`, set the friend's Instagram username as `recipient`.
4. Check it step by step. The first command sends nothing; the others send only to the account you name with `--to`, so use your own second account first:
   ```bash
   cli instagram-check                                                  # login only
   cli instagram-check --to your_second_account                         # looks the account up
   cli instagram-check --to your_second_account --send-test-text        # sends "ReelDrop test message"
   cli instagram-check --to your_second_account --reel "https://www.instagram.com/reel/XXXXXXXXXXX/"
   ```
   The last command confirms the reel arrived as a native reel share, or prints an error if Instagram did not accept it (the link is only sent instead when `IG_LINK_FALLBACK=1`). If login fails with a message about a verification step, log in once in the Instagram app, approve the login, set `IG_TOTP_SECRET`, and do not retry repeatedly. The friend should follow (or accept) the sending account, or the DM can appear under **Message requests**.
5. Restart the scheduler after changing `.env`, and stop if Instagram shows any warning.
6. Optional: Telegram alerts to your phone. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_MY_CHAT_ID`. (Telegram can also deliver reels instead of Instagram with `SENDER=telegram`, `TELEGRAM_FRIEND_CHAT_ID`, but then the friend gets links on Telegram, not Instagram DMs.)

Never commit `.env`, `data/` or `session.json` (they are already in `.gitignore`).

## 6. Make it fully automatic (optional, no pasted links)

ReelDrop can find reels itself from hashtags, creators or a saved collection. It uses Instagram's unofficial API, so use a test account only.

1. In `.env` set `SENDER=instagrapi` plus `IG_USERNAME` and `IG_PASSWORD`. (With the optional `SENDER=telegram` discovery still needs the `IG_*` login, read-only.)
2. In `config/schedules.yaml` (or the dashboard's Schedules tab) add a `discovery:` block with `enabled: true` and at least one source. A commented example is at the end of the file, and `README.md` describes every field.
3. Check it first: `cli discover --sources`, then `cli discover --dry-run` (shows what would be added, adds nothing).
4. Start the scheduler. It tops the queue up on its own; the dashboard's **Discovery** tab has a **Refill now** button.

This has not been verified against live Instagram yet.

## Reset

Delete `data/` to start with an empty queue. Delete `.local-demo/` to remove demo leftovers.
