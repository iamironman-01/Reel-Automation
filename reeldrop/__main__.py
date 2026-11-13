import argparse
import sys

from .db import init_db
from .queue import MOVE_DIRECTIONS, add_reel, delete_reel, list_reels, move_reel


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="reeldrop")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add a reel link to the queue")
    a.add_argument("url")
    a.add_argument("--note", default="")
    a.add_argument("--to", metavar="USERNAME", default=None,
                   help="only send this reel to this recipient (default: any rule)")
    ls = sub.add_parser("list", help="list queued reels")
    ls.add_argument("--status")
    r = sub.add_parser("remove", help="remove a reel by id")
    r.add_argument("id", type=int)
    mv = sub.add_parser("move", help="move a pending reel in the queue")
    mv.add_argument("id", type=int)
    mv.add_argument("direction", choices=MOVE_DIRECTIONS)
    ic = sub.add_parser("import-collection",
                        help="import reels from a saved Instagram collection (needs SENDER credentials)")
    ic.add_argument("name", help="collection name (case-insensitive)")
    ic.add_argument("--limit", type=int, default=50, help="max reels to add (default 50, max 200)")
    ic.add_argument("--dry-run", action="store_true", help="show what would be added")
    dc = sub.add_parser("discover",
                        help="find reels from the configured discovery sources and queue them "
                             "(needs Instagram credentials; ignores discovery.enabled)")
    dc.add_argument("--dry-run", action="store_true", help="show what would be added")
    dc.add_argument("--limit", type=int, default=None, help="max reels to add this run")
    dc.add_argument("--sources", action="store_true",
                    help="print the configured sources and settings, then exit (no login)")
    args = p.parse_args(argv)

    init_db()

    if args.cmd == "add":
        try:
            print("Added reel id", add_reel(args.url, args.note, recipient=args.to))
        except ValueError as e:
            print("Error:", e)
            return 1
    elif args.cmd == "list":
        for row in list_reels(args.status):
            to = row["recipient"] or "-"
            print(f"{row['id']:>4}  {row['status']:<8} {to:<12} {row['url']}  {row['note']}")
    elif args.cmd == "remove":
        if delete_reel(args.id):
            print("Removed", args.id)
        else:
            print(f"Error: no reel with id {args.id}")
            return 1
    elif args.cmd == "move":
        if move_reel(args.id, args.direction):
            print("Moved", args.id, args.direction)
        else:
            print(f"Error: reel {args.id} not moved ({args.direction}): "
                  "it is not pending or already at that end of the queue")
            return 1
    elif args.cmd == "import-collection":
        from .importer import format_result, import_collection
        from .senders.base import BlockedError
        try:
            result = import_collection(args.name, limit=args.limit, dry_run=args.dry_run)
        except BlockedError as e:
            # Pause so the scheduler does not keep hitting an account Instagram flagged.
            from .settings import set_paused
            set_paused(True)
            print(f"Error: Instagram blocked or challenged the account ({e}). "
                  "Sending is now paused; clear the warning in the app first.")
            return 1
        except (ValueError, RuntimeError) as e:
            print("Error:", e)
            return 1
        print(format_result(result))
    elif args.cmd == "discover":
        return _discover(args)
    return 0


def _print_sources(disc) -> None:
    print("Discovery settings:")
    for key in ("enabled", "min_pending", "fetch_per_source", "max_per_refill",
                "check_every_minutes", "min_likes", "delay_seconds"):
        print(f"  {key}: {getattr(disc, key, '-')}")
    sources = list(getattr(disc, "sources", None) or [])
    if not sources:
        print("Sources: none")
        return
    print(f"Sources ({len(sources)}):")
    for src in sources:
        line = f"  {getattr(src, 'type', '?')}: {getattr(src, 'value', '?')}"
        mode = getattr(src, "mode", None)
        if mode:
            line += f" (mode={mode})"
        to = getattr(src, "to", None)
        if to:
            line += f" -> {to}"
        print(line)


def _discover(args) -> int:
    """The `discover` command. Runs by hand, so discovery.enabled is not checked."""
    import importlib

    from .config import load_config
    from .senders.base import BlockedError

    if args.limit is not None and args.limit < 1:
        print("Error: --limit must be at least 1")
        return 1
    try:
        disc = getattr(load_config(), "discovery", None)
    except (ValueError, RuntimeError, OSError) as e:
        print("Error:", e)
        return 1
    if disc is not None and args.sources:
        _print_sources(disc)
        return 0
    if disc is None or not getattr(disc, "sources", None):
        print("Error: no discovery sources configured. Add a 'discovery:' section with "
              "'sources' to the schedules file.")
        return 1

    try:
        discovery = importlib.import_module("reeldrop.discovery")
        # No sender to share a login with here, so the provider logs in on its own.
        client = discovery.make_client_provider(object())()
        result = discovery.refill(disc, client, dry_run=args.dry_run, limit=args.limit)
    except BlockedError as e:
        # Pause so the scheduler does not keep hitting an account Instagram flagged.
        from .settings import set_paused
        set_paused(True)
        print(f"Error: Instagram blocked or challenged the account ({e}). "
              "Sending is now paused; clear the warning in the app first.")
        return 1
    except (ValueError, RuntimeError, ImportError) as e:
        print("Error:", e)
        return 1
    print(discovery.format_result(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
