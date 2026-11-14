# CLAUDE.md

Guidance for AI coding assistants working in this repo. For users, see `README.md`.

## What this project is

ReelDrop sends Instagram reels to a friend automatically on a schedule, with no phone interaction. Reels come from a SQLite queue, filled either by hand (CLI/dashboard/Telegram) or by **automatic discovery** from hashtags, creators or saved collections. Delivery goes through a pluggable sender (`dry_run`, `instagrapi`, `official`, `telegram`).

The unofficial Instagram route (`instagrapi`) is the only one that can browse reels and DM an arbitrary friend. It violates Instagram's Terms. Keep that risk visible in docs and never weaken the safety limits.

## Commands

```bash
pip install -r requirements-dev.txt     # Python 3.11+
python -m pytest -q                     # whole suite (about 2 s)
python main.py                          # scheduler (uses .env and config/schedules.yaml)
streamlit run dashboard.py              # dashboard on :8501
python start.py                         # dashboard + scheduler together (Render)
python -m reeldrop add|list|remove|move|import-collection|discover|instagram-check ...   # CLI
bash scripts/run_local.sh demo          # isolated dry-run demo (Windows: scripts\run_local.bat demo)
```

There is no linter or type checker configured. Match the surrounding style.

## Architecture

```
sources / pasted links ─► reels table (SQLite) ─► scheduler rule fires ─► job.run_rule ─► Sender.send ─► friend
```

| File | Role |
|------|------|
| `reeldrop/db.py` | `connect()`, schema, in-place migration. `DB_PATH` is relative: `data/reeldrop.db` |
| `reeldrop/queue.py` | add / pick / mark sent or failed / reorder / recipients / `sent_today` |
| `reeldrop/config.py` | `parse_config(dict)` validates; `load_config()` reads YAML. Path from `SCHEDULES_PATH` |
| `reeldrop/job.py` | what runs when a rule fires: pause, quiet day, daily cap, pick, top-up, send, retry |
| `reeldrop/discovery.py` | fetch reels from sources, `tick` (60 s throttle), `top_up`, client provider |
| `reeldrop/senders/` | `Sender.send(recipient, reel_url)`, `send_text`; `BlockedError` means stop everything |
| `reeldrop/settings.py` | key/value flags in the DB: pause switch, discovery request/last run |
| `main.py` | APScheduler 3.x `BlockingScheduler`: one cron job per rule plus `__reload__` and `__discovery__` |
| `dashboard.py` | Streamlit UI. Logic that needs tests lives in `reeldrop/dashboard_helpers.py` |
| `start.py` | supervisor that runs the dashboard and the scheduler in one container |

## Rules that must keep holding

- **Never send a reel twice.** Only `status='pending'` reels are picked. `mark_sent` runs right after a successful send, in one transaction with the `send_log` row. If it fails, the run pauses instead of continuing. `_RUN_LOCK` serialises rules that fire in the same minute.
- **`BlockedError` is never retried.** It sets the pause flag, alerts the owner, and stops the run. Later reels stay `pending`. This applies to sending and to discovery.
- **Daily cap** is counted from today's `sent` rows in `send_log` (in `TZ_NAME`). Notes (`send_note`) are extra DMs and are not counted.
- **Defaults are safe:** `SENDER=dry_run`, `discovery.enabled: false`. Do not change a default to something that contacts Instagram or anyone else.
- **Secrets** come only from environment variables (`.env`). Never log or put passwords or the Telegram bot token in exception text; use `senders.base.redact`. `.env`, `data/`, `session.json` and `*.db` are gitignored.
- **Config edits are validated before they are written.** The dashboard writes a temp file, validates it with `load_config`, then `os.replace`s it. A bad file must leave the old one untouched.
- **Always quote times** in YAML (`"20:00"`). Unquoted `20:00` loads as a number and is rejected on purpose.
- **Reserved job ids:** `__reload__` and `__discovery__` cannot be rule names, and the live reload must never remove them.
- **One Instagram login.** Discovery reuses `sender.cl` when `SENDER=instagrapi`. Do not add code paths that log in a second time without a reason.

## Conventions and gotchas

- `with connect() as c:` commits or rolls back **and closes** the connection (`_Connection.__exit__`). Do not use a connection after the block.
- `reeldrop/queue.py` shadows the stdlib module name only if the package directory is itself on `sys.path`. Keep using relative imports inside the package.
- `instagrapi` is pinned `>=3.2,<4`. Reels must be shared with `media_type="video"`; the library default is `"photo"`, which does not share a reel properly.
- `InstagrapiSender.send` never sends a link unless `IG_LINK_FALLBACK=1`: a failed native share is an error and the reel stays unsent. Do not make the link fallback the default again.
- `ClientNeedsUpgradeError` at login does not mean the library is too old. It is the legacy endpoint's answer after Instagram's current login flow asked for a verification step. The sender maps it to `VERIFICATION_HELP`; do not "fix" it by faking a newer app version.
- `python -m reeldrop` loads `.env` in `cli()`, not in `main()`, so tests that call `main()` never read a real `.env`.
- Block detection lives in `senders/base.py` (`BLOCK_ERROR_NAMES` / `is_block_error`). That is the single source of truth: do not add separate lists of block exceptions elsewhere.
- `python -m reeldrop instagram-check` is the manual pre-flight. It sends nothing unless `--send-test-text` or `--reel` is given (both need `--to`), and it only changes the pause flag on a block.
- Third-party libraries are imported lazily where tests must run without them (`yaml` in `load_config`, `requests` in `notify_me`, `instagrapi` inside the sender). Keep new code importable without those libraries.
- Each discovery source type has its own small `_fetch_*` function in `discovery.py`. The `instagrapi` method names there were written from documentation and have **not** been run against live Instagram. Fix signature problems there, not in callers.
- Time zones: `TZ_NAME` for timestamps, `timezone:` in the YAML for rules. A timezone change in the YAML needs a scheduler restart.
- Persistence on Render is the disk at `/app/data`. The live schedule file is `SCHEDULES_PATH=/app/data/schedules.yaml`, seeded once from `config/schedules.yaml`.

## Testing

- Use the `temp_db` fixture from `tests/conftest.py` for anything that touches the database. It points `reeldrop.db.DB_PATH` at a temp file. Never write to the real `data/`.
- Tests use fakes, not the network: fake `requests` / `instagrapi` / `apscheduler` / `dotenv` modules injected through `sys.modules`, and a `FakeScheduler` in `tests/test_job.py`. Do not add tests that need internet.
- Make retry waits and sleeps zero in tests (`job.RETRY_WAIT_SECONDS`, `time.sleep`, `discovery._sleep`).
- Six tests are skipped when PyYAML is not installed. They run in CI (`.github/workflows/ci.yml`). Discovery source order is shuffled, so run the suite more than once after touching it.
- Add a test with every behaviour change. Do not claim something is verified unless it actually ran.

## Git and repo workflow

- Do not commit directly to `main`. Work on a branch and open a pull request.
- The owner wants commits authored as **Naveen022006 <kanagunaveen21@gmail.com>** with a co-author trailer for Kiro:
  `Co-authored-by: Kiro Agent <244629292+kiro-agent@users.noreply.github.com>`.
  Set the author per commit (`git -c user.name=... -c user.email=... commit`). Do not edit git config.
- Keep `README.md` and the relevant `reeldrop-plan/*.md` in step with behaviour changes.
- Do not rewrite or force-push shared history, and do not delete data (`data/`, the database, `session.json`).
