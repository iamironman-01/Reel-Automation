import argparse
import sys

from .db import init_db
from .queue import add_reel, delete_reel, list_reels


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="reeldrop")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add a reel link to the queue")
    a.add_argument("url")
    a.add_argument("--note", default="")
    ls = sub.add_parser("list", help="list queued reels")
    ls.add_argument("--status")
    r = sub.add_parser("remove", help="remove a reel by id")
    r.add_argument("id", type=int)
    args = p.parse_args(argv)

    init_db()

    if args.cmd == "add":
        try:
            print("Added reel id", add_reel(args.url, args.note))
        except ValueError as e:
            print("Error:", e)
            return 1
    elif args.cmd == "list":
        for row in list_reels(args.status):
            print(f"{row['id']:>4}  {row['status']:<8} {row['url']}  {row['note']}")
    elif args.cmd == "remove":
        if delete_reel(args.id):
            print("Removed", args.id)
        else:
            print(f"Error: no reel with id {args.id}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
