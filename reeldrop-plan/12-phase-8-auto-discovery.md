# Phase 8 — Auto-discovery (no pasted links)

**Goal:** ReelDrop finds reels by itself and sends them to your friend as real Instagram DMs. You never paste a link.
**Needs:** Route B (`instagrapi`) login. Meta's official API cannot browse reels or DM an arbitrary friend, so no official route can do this.

## How it works

```
sources in schedules.yaml ──► discovery ──► queue (reels table) ──► scheduler rule fires ──► Sender ──► friend
 hashtag / user / collection     │              ▲  dedupe by shortcode
                                 └─ runs when pending < min_pending, or when you press "Refill now"
```

1. You list **sources**: hashtags, creators, or one of your saved collections.
2. A background tick (every minute, throttled by `check_every_minutes`) tops the queue up when fewer than `min_pending` reels are waiting.
3. Only **reels** are kept (`media_type == 2`, `product_type == "clips"`), optionally filtered by `min_likes`.
4. Reels already queued or sent are never added again (unique shortcode).
5. When a rule fires and the queue is empty, one immediate top-up is tried before giving up.
6. A block or challenge from Instagram **pauses everything** and alerts you, same as sending.

## Config (top-level `discovery:` in `config/schedules.yaml`)

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

`enabled` defaults to **false**. Nothing logs in to Instagram unless you turn it on.

Validation (in `reeldrop/config.py`): `enabled: true` needs at least one source; `min_pending` 1–500, `fetch_per_source` 1–50, `max_per_refill` 1–100, `check_every_minutes` 30–1440, `min_likes` ≥ 0, `delay_seconds` 0–120; `mode` is only allowed on hashtag sources; duplicate sources are rejected; a leading `#` / `@` in a value is stripped.

## Interfaces (the three work streams build against these)

| Module | Provides |
|--------|----------|
| `reeldrop/config.py` | `DiscoverySource`, `Discovery`, `Config.discovery`, validation in `parse_config` |
| `reeldrop/discovery.py` | `refill(discovery, client, dry_run=False, limit=None) -> RefillResult`, `tick(get_client, ...)`, `top_up(needed, get_client, ...)`, `request_refill()`, `make_client_provider(sender)`, `format_result(result)`, setting-key constants |
| `main.py` / `job.py` / `reeldrop/__main__.py` | 60-second `__discovery__` job, top-up when a rule finds an empty queue, `discover` CLI command |
| `dashboard.py` | Discovery tab: sources, status, "Refill now" (sets a flag the scheduler picks up) |

One Instagram login only: discovery reuses the sender's client when `SENDER=instagrapi`. With `SENDER=telegram` it logs in separately, read-only, and delivers over Telegram.

## Safety

- Disabled by default; `dry_run` demo never enables it.
- Throttled: `check_every_minutes` ≥ 30, `delay_seconds` between sources, `max_per_refill` cap.
- Daily send cap and gap still apply to sending.
- Test account only. Keep `daily_cap` low. Instagram can block automated access at any time.

## Done when

- With `discovery.enabled: true`, an empty queue fills itself from the sources with no pasted links.
- A rule firing with an empty queue triggers one top-up and then sends.
- A simulated block pauses sending and alerts you.
- Disabled config changes nothing about current behaviour.

## Not verifiable offline

Real `instagrapi` method behaviour (`hashtag_medias_top/recent`, `user_medias`, `collection_medias`), whether `direct_media_share` accepts the discovered reels, and how Instagram reacts. The first run on your test account is the real test.

## Live findings (2026-11-14, instagrapi 3.2.12)

- `hashtag_medias_top`, `hashtag_medias_top_v1` and `hashtag_medias_reels_v1` returned no medias on a live test account, without raising an error.
- `explore_reels` and `reels` each returned reels (`media_type` 2, `product_type` `clips`).
- A fourth source type, `explore`, was added for this: `value: reels` (default) uses `explore_reels`, `value: feed` uses `reels`. It needs no search term.
- One reel found through `explore` was delivered to a second account as a native reel share (`direct_media_share` with `media_type="video"`).
- `user` and `collection` sources are still untested against live Instagram.
