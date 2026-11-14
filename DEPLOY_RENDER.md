# Deploy ReelDrop on Render

One **web service** runs the dashboard and the scheduler together (`start.py`). `render.yaml` uses Render's **free** plan.

## What the free plan means

| Free plan limit | Effect on ReelDrop | What to do |
|-----------------|--------------------|------------|
| Sleeps after 15 minutes without inbound web traffic | While asleep the scheduler is stopped and nothing is sent | Built in: the app pings itself every 9 minutes (below) |
| No persistent disk; files are wiped on every sleep, restart or redeploy | The queue, send history, pause switch and dashboard edits are lost each time | Put friends and sources in `config/schedules.yaml` on the `render-deploy` branch, and the Instagram login in `IG_SESSION_B64` |
| 750 free hours per month per workspace | One service awake all month fits (a month has at most 744 hours) | Run only this one free service |
| Render may restart a free service at any time | Same as a wipe | As above |

Because the history is wiped, the daily cap starts again from 0 after a restart, and a reel sent before a restart may be found and sent again. For an always-on service that keeps its data, change `plan: free` to `plan: starter` (about $7/month) and add a 1 GB disk at `/app/data` (the comment at the top of `render.yaml` shows the lines).

### Keep it awake

This is built in. Render gives every web service its public address in `RENDER_EXTERNAL_URL`, and `start.py` requests `<that address>/_stcore/health` every 9 minutes, which counts as inbound traffic, so the service never reaches 15 idle minutes. The Render log shows `[keep-alive] pinging ... every 9 minutes` at start.

- `KEEP_ALIVE_MINUTES` changes the interval (1 to 14), `0` turns it off.
- It can only keep a running service awake. If the service does fall asleep (for example after a Render outage), opening the dashboard wakes it within about a minute and the pings start again.
- As a backup you can also add an outside pinger (cron-job.org or UptimeRobot) on the same URL.

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

## Create the web service (no Blueprint)

Code is deployed from the **`render-deploy`** branch. `main` is not used for deploys.

1. In Render: **New → Web Service**. Connect GitHub and pick `iamironman-01/Reel-Automation`.
2. Fill in the form:

   | Field | Value |
   |-------|-------|
   | Name | anything, e.g. `reeldrop` (the address becomes `https://reeldrop.onrender.com` or similar) |
   | Branch | `render-deploy` |
   | Language / Runtime | **Docker** |
   | Region | Singapore (closest to India) |
   | Instance type | **Free** |
   | Dockerfile path (Advanced) | `./Dockerfile.render` |
   | Health check path (Advanced) | `/_stcore/health` |
   | Auto-deploy | On commit (every push to `render-deploy` redeploys) |

   Leave the build and start commands empty: the Dockerfile has them (`python start.py`). Render sets `PORT` itself.
3. **Environment variables** (Advanced, or the Environment tab later). A hand-made service does not read `render.yaml`, so add them here:

   | Key | Value |
   |-----|-------|
   | `DASHBOARD_PASSWORD` | a long password of your choice |
   | `SENDER` | `dry_run` for the first deploy, `instagrapi` afterwards |
   | `TZ_NAME` | `Asia/Kolkata` |
   | `IG_USERNAME` | the sending account's username |
   | `IG_PASSWORD` | its password |
   | `IG_TOTP_SECRET` | the two-factor setup key |
   | `IG_SESSION_B64` | your saved login (see above) |
   | `STORAGE_IS_EPHEMERAL` | `1` |
   | `LOGIN_RETRY_MINUTES` | `60` |
   | `IG_LINK_FALLBACK` | `0` |

4. **Create Web Service.** The first build takes a few minutes. Open the address Render shows, log in with `DASHBOARD_PASSWORD`, and check the dashboard.
5. Set up friends and sources in the dashboard, then copy the **Advanced** tab text into `config/schedules.yaml` on `render-deploy` if they must survive restarts (see above).
6. Change `SENDER` to `instagrapi` in the Environment tab and save. Render restarts the service and the scheduler logs in.
7. Open the dashboard again. A red box means the Instagram login failed; see the next section.

To update the app later, push to `render-deploy`. To stop it, suspend or delete the service in Render.

## If the Instagram login fails on Render

Instagram checks logins from cloud servers more strictly than logins from a home connection.

1. Read the error in the dashboard header or under **Settings → Instagram login**.
2. Log in to the account once in the Instagram app and approve any "Was this you?" prompt.
3. Make sure `IG_TOTP_SECRET` is set.
4. If it still fails, log in on your own computer (`python -m reeldrop instagram-check`), then upload that computer's `data/session.json` under **Settings → Instagram login → Upload a session file**, and press **Retry login now**. The file holds login cookies, so treat it like a password.

After a failed Instagram login the scheduler waits `LOGIN_RETRY_MINUTES` (default 60) before it exits and is restarted, so it does not retry every few minutes and get the account flagged. **Retry login now** ends that wait. The dashboard stays available the whole time.

## How GitHub triggers deploys (the "webhook")

You don't create a webhook by hand. Connecting the repo installs Render's GitHub app, which listens for pushes. `render.yaml` sets `autoDeployTrigger: commit`, so every push (or merge) to `render-deploy` rebuilds and redeploys the service. A hand-made web service has the same setting under Settings → Build & Deploy → Auto-Deploy.

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
