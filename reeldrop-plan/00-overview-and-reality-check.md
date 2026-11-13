# 00 — Overview & Reality Check

## Goal of the project

ReelDrop keeps a **queue** of reels you want to share and sends them to a friend over DM according to **schedule rules** you define.

Core features:
- **Reel queue** — paste reel links; they are stored in order.
- **Schedule rules** — time + days + number of reels + recipient.
- **Automatic sender** — at each scheduled time, sends the next N unsent reels.
- **Dashboard & logs** — see what was sent, what failed, what is left.
- **Safety limits** — daily cap, gap between messages, kill switch, dry-run mode.

## Reality check: how can code send Instagram DMs?

Instagram has **no open, official way** for a personal account to DM friends from code. Read this before writing anything.

| Route | How it works | Can it send any reel to a friend? | Risk |
|-------|--------------|-----------------------------------|------|
| **A. Official Messaging API** | Meta Graph API, Business/Creator account, Meta developer app | **No.** Only replies within 24 h after the person messages you. Native reel sharing works only for your own posts and is unreliable. You can send the reel **link** as text. | Low — fully allowed |
| **B. Unofficial private API** (`instagrapi`) | Python library logs in as your account and talks to Instagram's mobile API | **Yes** | High — against Instagram's Terms; action blocks or bans possible |
| **C. Browser automation** (Playwright) | Script clicks Share → friend → Send on instagram.com | **Yes** | High — same Terms issue; breaks when the site layout changes |
| **D. Different channel** (Telegram bot) | Sends the reel links to your friend on Telegram | **Yes** (as links that open in Instagram) | None |

## Decision

- Build the system with a **pluggable sender**. The queue, scheduler and dashboard never care how delivery happens.
- Start with **DryRunSender** (prints instead of sending).
- Then pick:
  - **Route B** with a **separate test account** if you want real Instagram DMs, or
  - **Route D** for zero account risk.
- Route C is not covered in detail: it does the same thing as B but is more fragile.

## Consent

Tell your friend about the project and confirm they're happy to receive automated reels. Automated messages to people who didn't agree count as spam.

✅ **Done when:** you have chosen your route and your friend has said yes.
