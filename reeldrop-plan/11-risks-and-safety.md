# 11 — Risks & Safety

| Risk | Impact | Mitigation |
|------|--------|------------|
| Account action-blocked or banned (Route B/C) | Lose the account | Test account only, `daily_cap` 5–10, `gap_seconds` ≥ 30, auto-pause on any warning |
| Library breaks after an Instagram update | Sending stops | Pin versions in `requirements.txt`, watch the library's GitHub issues, keep Route D as fallback |
| Login challenge / 2FA prompt | Can't log in | Reuse `session.json`; clear the challenge in the app manually, then restart |
| Password leaked via GitHub | Account takeover | `.env`, `session.json`, `data/` in `.gitignore`; private repo; check before every push |
| Duplicate sends after crash | Annoys your friend | Status + log written in one transaction right after success; `coalesce=True` |
| Friend no longer wants reels | Becomes spam | Pause button; remove the rule; always respect a "stop" |
| Official API 24 h window missed (Route A) | Sends rejected | Only use Route A if your friend messages the account daily |
| Reel arrives as a link, or lands in Message requests (Route B) | Friend does not see a native reel share | `instagram-check --reel` shows which path was used; ask the friend to follow/accept the sending account |
| Dashboard exposed on the internet | Anyone controls it | Password gate + VPN (e.g., Tailscale) |

## Rules to follow

1. Start with dry-run, then test account → your own second account, then your friend.
2. Never use your main account until weeks of clean running on the test account.
3. Never commit secrets.
4. Stop immediately if Instagram shows any warning.
5. Your friend's consent comes first.
6. Run `python -m reeldrop instagram-check` (login only), then send to your own second account, before the first real send to a friend. It sends nothing unless you pass `--send-test-text` or `--reel`.
