"""
Operator CLI for offline tasks (user management).

Run with:
    python -m backend.cli useradd alice --admin
    python -m backend.cli userdel alice
    python -m backend.cli passwd alice
    python -m backend.cli userlist

Designed for headless boxes where the SPA isn't reachable. Talks to the
same SQLite file the running service uses — WAL mode lets both write
concurrently, so you don't need to stop the service to add a user.
"""

from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys

from backend.config import ensure_directories, get_settings
from backend.core.auth import hash_password
from backend.db import init_db, run_migrations, queries
from backend.db.connection import transaction


def _bootstrap() -> None:
    settings = get_settings()
    ensure_directories(settings)
    init_db(settings)
    run_migrations()


def _read_password(provided: str | None) -> str:
    if provided:
        return provided
    pw = getpass.getpass("Password: ")
    again = getpass.getpass("Confirm:  ")
    if pw != again:
        sys.exit("Passwords do not match")
    if not pw:
        sys.exit("Password cannot be empty")
    return pw


def cmd_useradd(args: argparse.Namespace) -> int:
    password = _read_password(args.password)
    try:
        with transaction():
            uid = queries.create_user(
                username=args.username,
                password_hash=hash_password(password),
                is_admin=args.admin,
                email=args.email,
            )
    except sqlite3.IntegrityError:
        sys.exit(f"User '{args.username}' already exists")
    print(f"Created user '{args.username}' (id={uid}, admin={args.admin})")
    return 0


def cmd_userdel(args: argparse.Namespace) -> int:
    with transaction():
        ok = queries.delete_user_by_username(args.username)
    if not ok:
        sys.exit(f"No such user '{args.username}'")
    print(f"Deleted user '{args.username}'")
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    user = queries.get_user_by_username(args.username)
    if user is None:
        sys.exit(f"No such user '{args.username}'")
    password = _read_password(args.password)
    with transaction():
        queries.update_user_password(user["id"], hash_password(password))
    print(f"Password updated for '{args.username}'")
    return 0


def cmd_userlist(_: argparse.Namespace) -> int:
    users = queries.list_users()
    if not users:
        print("(no users)")
        return 0
    for u in users:
        flag = "admin" if u["is_admin"] else "user"
        print(f"{u['id']:>4}  {flag:5}  {u['username']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="muse")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("useradd", help="Create a user")
    p.add_argument("username")
    p.add_argument("--password", help="Skip the prompt (visible in shell history)")
    p.add_argument("--email")
    p.add_argument("--admin", action="store_true")
    p.set_defaults(func=cmd_useradd)

    p = sub.add_parser("userdel", help="Delete a user")
    p.add_argument("username")
    p.set_defaults(func=cmd_userdel)

    p = sub.add_parser("passwd", help="Reset a user's password")
    p.add_argument("username")
    p.add_argument("--password", help="Skip the prompt (visible in shell history)")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("userlist", help="List users")
    p.set_defaults(func=cmd_userlist)

    args = parser.parse_args()
    _bootstrap()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
