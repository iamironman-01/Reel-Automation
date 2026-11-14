# Deploy ReelDrop on Render

One **web service** runs the dashboard and the scheduler together (`start.py`). `render.yaml` uses Render's **free** plan.

## What the free plan means

| Free plan limit | Effect on ReelDrop | What to do |
|-----------------|--------------------|------------|
| Sleeps after 15 minutes without inbound web traffic | While asleep the scheduler is stopped and nothing is sent | Keep it awake with an outside pinger (below) |
| No persistent disk; files are wiped on every sleep, restart or redeploy | The queue, send history, pause switch and dashboard edits are lost each time | Put friends and sources in `config/schedules.yaml` on the `render-deploy` branch, and the Instagram login in `IG_SESSION_B64` |
| 750 free hours per month per workspace | One service awake all month fits (a month has at most 744 hours) | Run only this one free service |
| Render may restart a free service at any time | Same as a wipe | As above |

Because the history is wiped, the daily cap starts again from 0 after a restart, and a reel sent before a restart may be found and sent again. For an always-on service that keeps its data, change `plan: free` to `plan: starter` (about $7/month) and add a 1 GB disk at `/app/data` (the comment at the top of `render.yaml` shows the lines).

### Keep it awake

Use a free outside pinger, for example [cron-job.org](https://cron-job.org) or UptimeRobot:

- URL: `https://<your-service>.onrender.com/_stcore/health`
- Every 10 minutes

### Keep the Instagram login across restarts

Without a disk, `data/session.json` disappears on every restart, and a fresh login from a cloud server is the most likely to be challenged. Save a working session as an environment variable instead:

1. On your own computer, where the login works, run `python -m reeldrop instagram-check` once.
2. In PowerShell, in the project folder, copy the session file as base64 to the clipboard:
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("data\session.json")) | Set-Clipboard
   ```
3. In Render, paste it as the value of `IG_SESSION_B64`. It holds login cookies: treat it like a password.

At each start ReelDrop writes that session back to `data/session.json` and reuses it. If Instagram later logs the session out, repeat these steps.

### Keep your friends and sources

Dashboard changes last only until the next restart (the dashboard shows a yellow warning about this). Set things up in the dashboard, then copy the text from the **Advanced** tab into `config/schedules.yaml` on the `render-deploy` branch and push. Render redeploys with it.

## One-time setup

1. **Use the `render-deploy` branch.** Render deploys that branch (see `branch:` in `render.yaml`). Push to it to redeploy.
2. In Render: **New → Blueprint**, connect your GitHub account, and pick `iamironman-01/Reel-Automation`. Render reads `render.yaml`.
3. Fill in the variables marked *sync: false*:
   - `DASHBOARD_PASSWORD`: a real password (the app refuses `change-me`).
   - Instagram route B (real Instagram DMs): `IG_USERNAME`, `IG_PASSWORD`, `IG_TOTP_SECRET` (the two-factor setup key; in a live test the login only succeeded with it) and `IG_SESSION_B64` (see above). Only with a separate test account.
   - Telegram (optional, alerts or an alternative route): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_MY_CHAT_ID`, and `TELEGRAM_FRIEND_CHAT_ID` if Telegram is the sender.
4. Deploy. Open the service URL, log in, and check the dashboard. `SENDER` starts as `dry_run`, so nothing real is sent. Set up the pinger now.
5. In the dashboard, set everything up before sending for real:
   - **Friends** tab: remove the placeholder `friend_username` schedules and add your friend(s) with a time, days and number of reels.
   - **Reel topics** tab: add sources (creators, Explore), optional caption keywords, then turn on **Find reels automatically**.
   - **Settings** tab: check the daily cap and the gap between reels.
6. Set `SENDER` to `instagrapi` under **Environment** and save. Render redeploys and the scheduler logs in to Instagram.
7. Open the dashboard again. If a red box says the scheduler could not start its sender, the Instagram login failed. See the next section.

## If the Instagram login fails on Render

Instagram checks logins from cloud servers more strictly than logins from a home connection.

1. Read the error in the dashboard header or under **Settings → Instagram login**.
2. Log in to the account once in the Instagram app and approve any "Was this you?" prompt.
3. Make sure `IG_TOTP_SECRET` is set.
4. If it still fails, log in on your own computer (`python -m reeldrop instagram-check`), then upload that computer's `data/session.json` under **Settings → Instagram login → Upload a session file**, and press **Retry login now**. The file holds login cookies, so treat it like a password.

After a failed Instagram login the scheduler waits `LOGIN_RETRY_MINUTES` (default 60) before it exits and is restarted, so it does not retry every few minutes and get the account flagged. **Retry login now** ends that wait. The dashboard stays available the whole time.

## How GitHub triggers deploys (the "webhook")

You don't create a webhook by hand. Connecting the repo installs Render's GitHub app, which listens for pushes. `render.yaml` sets `autoDeployTrigger: commit`, so every push (or merge) to `render-deploy` rebuilds and redeploys the service.

To deploy only after tests pass, change it to `autoDeployTrigger: checksPass`. That uses the **CI** workflow (`.github/workflows/ci.yml`, runs `pytest`), which requires GitHub Actions to be enabled on the repo (Settings → Actions). I could not confirm that CI runs on this repo, so the default does not depend on it.

## Where things live

| Path on the disk (`/app/data`) | What |
|-----|------|
| `reeldrop.db` | queue, send history, pause switch |
| `session.json` | Instagram login session (Route B) |
| `reeldrop.log` | rotating log |
| `schedules.yaml` | live schedule. Seeded from `config/schedules.yaml` on first start; edits in the dashboard are saved here and the scheduler reloads them within about a minute. Later changes to `config/schedules.yaml` in git do **not** overwrite it. |

## Risks specific to a cloud host

- **Instagram Route B from a cloud IP** is riskier: cloud addresses get more login checks and blocks than a home connection. `session.json` stays on the persistent disk so ReelDrop reuses the login instead of logging in again, and `IG_TOTP_SECRET` lets two-factor prompts be answered automatically. A session uploaded from another computer is then used from a different IP address, which Instagram may also question. If it keeps getting challenged, run Route B at home instead. Telegram stays optional here, for alerts to yourself (`TELEGRAM_MY_CHAT_ID`).
- **Backups:** the disk is the only copy. Render takes daily disk snapshots, but also download the database now and then (Render Shell: `cp /app/data/reeldrop.db ...`).
- **Dashboard is public.** The password gate is the only protection. Use a long password.
- **Telegram control:** keep `TELEGRAM_CONTROL=0` until `TELEGRAM_MY_CHAT_ID` is your own chat id.

## Not yet verified

This setup was written without access to a Render account or Docker. The first deploy is the real test. If it fails, the Render **Logs** tab shows why. Login, Explore and creator discovery, and native reel sending were seen working from a home computer (2026-11-14), but not from Render.
