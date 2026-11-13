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
    return 0


if __name__ == "__main__":
    sys.exit(main())
