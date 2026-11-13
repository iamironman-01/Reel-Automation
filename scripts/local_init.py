"""Prepare a local checkout: create .env (safe defaults) and the data folder.

Run it from the repo root:  python scripts/local_init.py
Safe to run many times. It never overwrites an existing .env.
"""
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def make_env_text(example_text: str, password: str) -> str:
    """Turn .env.example into a local .env: dry-run sender and a real dashboard password."""
    lines = []
    for line in example_text.splitlines():
        if line.startswith("DASHBOARD_PASSWORD="):
            line = f"DASHBOARD_PASSWORD={password}"
        elif line.startswith("SENDER="):
            line = "SENDER=dry_run"
        lines.append(line)
    return "\n".join(lines) + "\n"


def init(root: Path = ROOT, password: str | None = None) -> str | None:
    """Create .env and data/. Returns the generated password, or None if .env already existed."""
    (root / "data").mkdir(exist_ok=True)
    env = root / ".env"
    if env.exists():
        return None
    password = password or secrets.token_urlsafe(12)
    example = (root / ".env.example").read_text(encoding="utf-8")
    env.write_text(make_env_text(example, password), encoding="utf-8")
    return password


def main() -> int:
    password = init()
    if password is None:
        print(".env already exists - left unchanged.")
    else:
        print("Created .env with SENDER=dry_run (nothing real is sent).")
        print(f"Dashboard password: {password}")
        print("(It is stored in .env, which git ignores. Change it there any time.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
