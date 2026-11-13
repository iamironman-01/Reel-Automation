# Phase 7 — Extensions (Optional)

**Goal:** add features once the core works. Pick what interests you.

| Feature | Idea | Difficulty |
|---------|------|------------|
| Multiple friends | Add `recipient` column to `reels`, or one queue per friend | Medium |
| Captions | Send a short note before each reel ("watch this 😂") using `reels.note` | Easy |
| Auto-import | Pull reels from a saved collection (Route B: `collection_medias`) | Medium |
| Live schedule reload | Scheduler checks `schedules.yaml` modification time every minute and re-registers jobs | Medium |
| Queue reorder | Add a `position` column; drag/sort in the dashboard | Easy |
| Stats page | Charts of reels sent per day/week, failure rate | Easy |
| Random pick | Choose N random pending reels instead of oldest first | Easy |
| Quiet days | Skip dates listed in config (exams, festivals) | Easy |
| Telegram control | Bot commands: `/add <link>`, `/pause`, `/status` from your phone | Medium |

## Portfolio tips

- Write a clean README with a screenshot of the dashboard and an architecture diagram.
- Highlight the design choices: pluggable senders, kill switch, daily cap, consent.
- Be honest about Route B's Terms-of-Service risk in your write-up; reviewers value that.
