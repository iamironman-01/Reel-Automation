# ReelDrop — Scheduled Instagram Reel Sender

Send a set number of reels to a friend at fixed times, automatically, without touching your phone.

> Example: "Every day at 8:00 PM, send 3 reels to my friend. On weekends at 11:00 AM, send 5."

## How to use this folder

Read the files **in order**. Each phase ends with something that works on its own, so test it before moving on.

| # | File | What you build | Time |
|---|------|----------------|------|
| 00 | [00-overview-and-reality-check.md](00-overview-and-reality-check.md) | Understand the limits and pick a sender route | 1 hr |
| 01 | [01-architecture.md](01-architecture.md) | System design, folder structure, database | 1 hr |
| 02 | [02-phase-0-setup.md](02-phase-0-setup.md) | Repo, venv, accounts, config files | 1 day |
| 03 | [03-phase-1-reel-queue.md](03-phase-1-reel-queue.md) | Database + queue + CLI | 2 days |
| 04 | [04-phase-2-scheduler.md](04-phase-2-scheduler.md) | Time-based jobs (dry-run) | 2–3 days |
| 05 | [05-phase-3-sender.md](05-phase-3-sender.md) | Real sending (instagrapi / official API / Telegram) | 3–5 days |
| 06 | [06-phase-4-reliability.md](06-phase-4-reliability.md) | Retries, kill switch, logs | 2–3 days |
| 07 | [07-phase-5-dashboard.md](07-phase-5-dashboard.md) | Streamlit web dashboard | 3 days |
| 08 | [08-phase-6-deployment.md](08-phase-6-deployment.md) | Docker, run 24/7 | 2 days |
| 09 | [09-phase-7-extensions.md](09-phase-7-extensions.md) | Extra features | Ongoing |
| 10 | [10-testing-plan.md](10-testing-plan.md) | Test cases for every phase | — |
| 11 | [11-risks-and-safety.md](11-risks-and-safety.md) | Risks and mitigations | — |

## Progress tracker

- [x] Phase 0 — Setup
- [x] Phase 1 — Reel queue
- [x] Phase 2 — Scheduler (dry-run)
- [x] Phase 3 — Real sender
- [x] Phase 4 — Reliability & safety
- [x] Phase 5 — Dashboard
- [x] Phase 6 — Deployment
- [x] Phase 7 — Extensions

## Timeline

| Week | Phases | Outcome |
|------|--------|---------|
| 1 | 0, 1, 2 | Queue + scheduler working in dry-run mode |
| 2 | 3, 4 | Real sending from a test account, with safety limits |
| 3 | 5, 6 | Dashboard + 24/7 deployment |
| 4+ | 7 | Extensions and polish |

## Golden rules

1. **Never start with your main Instagram account.** Use a test account.
2. **Build everything in dry-run mode first.**
3. **Your friend must agree** to receive automated reels.
4. **Never commit** `.env`, `session.json` or the database to GitHub.
