# Deploy ReelDrop on Render (24/7)

One always-on **web service** runs the dashboard and the scheduler together (`start.py`), with one **persistent disk** for the database, Instagram session, logs and your edited schedules.

## Cost and why

| Need | Render requirement |
|------|--------------------|
| Never sleeps | Free web services spin down after 15 minutes without traffic. Use a paid plan. |
| Keeps the database | Free instances lose their files on every restart. Persistent disks need a paid plan. |
| Scheduler + dashboard | A disk can't be shared between services, so both run in one container. |

`render.yaml` uses the `starter` plan (about $7/month) plus a 1 GB disk (about $0.25/month). Check [render.com/pricing](https://render.com/pricing) for current prices.

A disk means each deploy restarts the one instance, so the dashboard is unavailable for a minute or two. `start.py` gives a running send job up to 240 seconds (`SHUTDOWN_GRACE_SECONDS`) to finish before it stops.

## One-time setup

1. **Merge the code to `main`** (Render deploys the `main` branch, see `render.yaml`).
2. In Render: **New → Blueprint**, connect your GitHub account, and pick `iamironman-01/Reel-Automation`. Render reads `render.yaml`.
3. Fill in the variables marked *sync: false*:
   - `DASHBOARD_PASSWORD`: a real password (the app refuses `change-me`).
   - Instagram route B (real Instagram DMs): `IG_USERNAME`, `IG_PASSWORD`, and ideally `IG_TOTP_SECRET`. Only with a separate test account.
   - Telegram (optional, alerts or an alternative route): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_MY_CHAT_ID`, and `TELEGRAM_FRIEND_CHAT_ID` if Telegram is the sender.
4. Deploy. Open the service URL, log in, and check the dashboard. `SENDER` starts as `dry_run`, so nothing real is sent.
5. When dry-run looks right, set `SENDER` to `instagrapi` (or `telegram`) under **Environment** and save. Render redeploys.

## How GitHub triggers deploys (the "webhook")

You don't create a webhook by hand. Connecting the repo installs Render's GitHub app, which listens for pushes. `render.yaml` sets `autoDeployTrigger: commit`, so every push (or merge) to `main` rebuilds and redeploys the service.

To deploy only after tests pass, change it to `autoDeployTrigger: checksPass`. That uses the **CI** workflow (`.github/workflows/ci.yml`, runs `pytest`), which requires GitHub Actions to be enabled on the repo (Settings → Actions). I could not confirm that CI runs on this repo, so the default does not depend on it.

## Where things live

| Path on the disk (`/app/data`) | What |
|-----|------|
| `reeldrop.db` | queue, send history, pause switch |
| `session.json` | Instagram login session (Route B) |
| `reeldrop.log` | rotating log |
| `schedules.yaml` | live schedule. Seeded from `config/schedules.yaml` on first start; edits in the dashboard are saved here and the scheduler reloads them within about a minute. Later changes to `config/schedules.yaml` in git do **not** overwrite it. |

## Risks specific to a cloud host

- **Instagram Route B from a cloud IP** is riskier: cloud addresses get more login checks and blocks than a home connection. Keep `session.json` on the persistent disk so ReelDrop reuses the login instead of logging in again, and set `IG_TOTP_SECRET` so two-factor prompts can be answered automatically. If it keeps getting challenged, run Route B at home instead. Telegram stays optional here, for alerts to yourself (`TELEGRAM_MY_CHAT_ID`).
- **Backups:** the disk is the only copy. Render takes daily disk snapshots, but also download the database now and then (Render Shell: `cp /app/data/reeldrop.db ...`).
- **Dashboard is public.** The password gate is the only protection. Use a long password.
- **Telegram control:** keep `TELEGRAM_CONTROL=0` until `TELEGRAM_MY_CHAT_ID` is your own chat id.

## Not yet verified

This setup was written without access to a Render account or Docker. The first deploy is the real test. If it fails, the Render **Logs** tab shows why.
