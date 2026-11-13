"""Dry-run demo for your own computer.

Seeds a few FAKE reels, writes a schedule that fires 2 minutes from now, and runs the
scheduler. Nothing is sent anywhere: SENDER is forced to dry_run, and everything lives in
an isolated folder (.local-demo/) so your real queue and database are never touched.

Run from the repo root:  python scripts/local_demo.py
Stop with Ctrl+C. Look at the result with:  python scripts/local_demo.py --show
"""
import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / ".local-demo"


def demo_schedule_text(now: datetime, tz_name: str, minutes_ahead: int = 2,
                       count: int = 2, gap_seconds: int = 5) -> str:
    """YAML for one rule that fires `minutes_ahead` minutes after `now` (time is quoted)."""
    when = (now + timedelta(minutes=minutes_ahead)).replace(second=0, microsecond=0)
    return (
        f"timezone: {tz_name}\n"
        "daily_cap: 10\n"
        f"gap_seconds: {gap_seconds}\n"
        "rules:\n"
        "  - name: demo_drop\n"
        '    days: "*"\n'
        f'    time: "{when:%H:%M}"\n'
        f"    count: {count}\n"
        "    recipient: demo_friend\n"
    )


def demo_urls(n: int) -> list[str]:
    return [f"https://www.instagram.com/reel/DemoReel{i:03d}/" for i in range(1, n + 1)]


def reset_demo_dir(path: Path = DEMO_DIR) -> None:
    """Delete and recreate the isolated demo folder (refuses to touch anything else)."""
    if path.name != ".local-demo":
        raise ValueError(f"refusing to delete {path}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def seed_reels(n: int) -> int:
    """Add n fake reels to the database in the CURRENT working directory."""
    from reeldrop.db import init_db
    from reeldrop.queue import add_reel

    init_db()
    added = 0
    for url in demo_urls(n):
        try:
            add_reel(url, note="demo")
            added += 1
        except ValueError:
            pass   # already there
    return added


def show() -> int:
    """Print the demo queue (run after the demo)."""
    if not (DEMO_DIR / "data" / "reeldrop.db").exists():
        print("No demo has been run yet.")
        return 1
    os.chdir(DEMO_DIR)
    sys.path.insert(0, str(ROOT))
    from reeldrop.queue import list_reels

    for row in list_reels():
        print(f"{row['id']:>3}  {row['status']:<8} {row['url']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Local dry-run demo (nothing is really sent).")
    p.add_argument("--show", action="store_true", help="print the demo queue and exit")
    p.add_argument("--no-run", action="store_true", help="prepare the demo but do not start the scheduler")
    p.add_argument("--reels", type=int, default=5, help="fake reels to add (default 5)")
    p.add_argument("--minutes", type=int, default=2, help="fire this many minutes from now (default 2)")
    args = p.parse_args(argv)

    if args.show:
        return show()
    if not 1 <= args.minutes <= 60 or not 1 <= args.reels <= 50:
        print("Error: --minutes must be 1-60 and --reels 1-50")
        return 2

    tz_name = os.environ.get("TZ_NAME", "Asia/Kolkata")
    now = datetime.now(ZoneInfo(tz_name))
    reset_demo_dir()
    schedule = DEMO_DIR / "schedules.yaml"
    schedule.write_text(demo_schedule_text(now, tz_name, args.minutes), encoding="utf-8")

    sys.path.insert(0, str(ROOT))
    os.chdir(DEMO_DIR)                     # the database is data/reeldrop.db relative to here
    added = seed_reels(args.reels)
    fire = (now + timedelta(minutes=args.minutes)).replace(second=0, microsecond=0)
    print(f"Added {added} fake reels. The demo rule fires at {fire:%H:%M} ({tz_name}),")
    print("sends 2 of them in DRY RUN, then you can check with: python scripts/local_demo.py --show")
    if args.no_run:
        return 0

    env = {**os.environ, "SENDER": "dry_run", "TZ_NAME": tz_name,
           "SCHEDULES_PATH": str(schedule), "PYTHONPATH": str(ROOT)}
    # Fully offline: python-dotenv never overrides variables that already exist, so blanking
    # these stops the demo from messaging your Telegram or starting a second bot poller
    # even if your real .env has them set.
    env.update({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_MY_CHAT_ID": "",
                "TELEGRAM_FRIEND_CHAT_ID": "", "TELEGRAM_CONTROL": "0",
                "IG_USERNAME": "", "IG_PASSWORD": ""})
    print("Starting the scheduler. Press Ctrl+C to stop.\n")
    try:
        return subprocess.call([sys.executable, str(ROOT / "main.py")], cwd=DEMO_DIR, env=env)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
