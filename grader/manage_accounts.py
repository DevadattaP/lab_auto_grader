#!/usr/bin/env python3
"""CLI for live-lab account management: student accounts (per-lab session
state in students.csv, shared identity in live/accounts.csv) and the
single admin account (.env). Same functions the admin web UI's account
routes call internally -- usable standalone, with no server running.

No command creates a student account -- their first successful login
(password = their roll number reversed + "@Cp", see
grader.accounts.default_password) auto-provisions it, and they're then
required to set their own password before doing anything else. `list`
just reflects who has already signed in; `reset-student` is for when a
student is locked out or an admin wants to hand out a specific password
directly (works even before that student has ever logged in).

Usage:
    python -m grader.manage_accounts default-password --roll 112201023
    python -m grader.manage_accounts reset-student --lab lab_01 --roll 112201023
    python -m grader.manage_accounts unbind --lab lab_01 --roll 112201023
    python -m grader.manage_accounts list --lab lab_01
    python -m grader.manage_accounts init-admin
    python -m grader.manage_accounts reset-admin
"""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from grader import accounts

LIVE_ROOT = Path(__file__).resolve().parent.parent / "live"
GLOBAL_ACCOUNTS_PATH = LIVE_ROOT / accounts.GLOBAL_ACCOUNTS_FILENAME


def _students_csv(lab: str) -> Path:
    return LIVE_ROOT / lab / "students.csv"


def _cmd_default_password(args: argparse.Namespace) -> None:
    print(accounts.default_password(args.roll))


def _cmd_list(args: argparse.Namespace) -> None:
    csv_path = _students_csv(args.lab)
    rows = accounts.list_students(csv_path)
    if not rows:
        print(f"No students have signed in for lab {args.lab} yet ({csv_path})")
        return
    print(f"{'roll_no':<16}{'status':<10}{'password':<24}{'ip':<18}{'bound_at':<26}last_seen")
    for a in rows:
        status = "online" if a.active else "offline"
        global_account = accounts.get_global_account(GLOBAL_ACCOUNTS_PATH, a.roll_no)
        password_status = "custom" if global_account and global_account.password_set else "default (not changed yet)"
        print(
            f"{a.roll_no:<16}{status:<10}{password_status:<24}{(a.ip or '-'):<18}"
            f"{(a.bound_at or '-'):<26}{a.last_seen or '-'}"
        )


def _cmd_reset_student(args: argparse.Namespace) -> None:
    password = args.password or getpass.getpass(f"New password for {args.roll}: ")
    confirm = args.password or getpass.getpass("Confirm new password: ")
    if password != confirm:
        raise SystemExit("Error: passwords do not match.")
    accounts.set_global_password(GLOBAL_ACCOUNTS_PATH, args.roll, password, create_if_missing=True)
    print(f"Password set for {args.roll} (applies immediately, in every lab).")


def _cmd_unbind(args: argparse.Namespace) -> None:
    accounts.unbind_student_device(_students_csv(args.lab), args.roll)
    print(f"Device unbound for {args.roll} in lab {args.lab} -- they can now log in from a new device.")


def _cmd_init_admin(args: argparse.Namespace) -> None:
    username = args.username or input("Admin username: ")
    password = args.password or getpass.getpass("Admin password: ")
    accounts.init_admin(username, password)
    print("Admin account created in .env.")


def _cmd_reset_admin(args: argparse.Namespace) -> None:
    password = args.password or getpass.getpass("New admin password: ")
    accounts.reset_admin_password(password)
    print("Admin password reset in .env.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage live-lab student/admin accounts (no database -- CSV + .env)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("default-password", help="show the deterministic default password for a roll number")
    p.add_argument("--roll", required=True)
    p.set_defaults(func=_cmd_default_password)

    p = sub.add_parser("list", help="show accounts that have signed in for a lab (status, password, device, timestamps)")
    p.add_argument("--lab", required=True)
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("reset-student", help="set a student's password directly (works even before they've ever logged in)")
    p.add_argument("--roll", required=True)
    p.add_argument("--password", default=None, help="omit to be prompted (recommended)")
    p.set_defaults(func=_cmd_reset_student)

    p = sub.add_parser("unbind", help="sign a student's active session out so they (or someone else) can log in elsewhere")
    p.add_argument("--lab", required=True)
    p.add_argument("--roll", required=True)
    p.set_defaults(func=_cmd_unbind)

    p = sub.add_parser("init-admin", help="bootstrap the admin account in .env (first-time setup)")
    p.add_argument("--username", default=None)
    p.add_argument("--password", default=None, help="omit to be prompted (recommended)")
    p.set_defaults(func=_cmd_init_admin)

    p = sub.add_parser("reset-admin", help="reset the admin password in .env")
    p.add_argument("--password", default=None, help="omit to be prompted (recommended)")
    p.set_defaults(func=_cmd_reset_admin)

    args = parser.parse_args()
    try:
        args.func(args)
    except accounts.AccountError as e:
        raise SystemExit(f"Error: {e}")


if __name__ == "__main__":
    main()
