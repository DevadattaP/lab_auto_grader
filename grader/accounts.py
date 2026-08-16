"""No-database account storage for the live-lab platform (see
LIVE_LAB_DESIGN.md §5): a per-lab `students.csv`
(roll_no/ip/active/bound_at/last_seen) for session state, and one
repo-root-adjacent `live/accounts.csv` (roll_no/password_hash/password_set)
shared across every lab for the actual login identity -- a student's
password is the same account everywhere, but `active`/`ip` tracking is
genuinely per-lab (a student might do lab_01 and lab_02 on different days
from different seats), so those two concerns now live in two different
files rather than one row trying to be both.

No admin action creates an account. The first time a roll number is ever
seen (at login), it's auto-provisioned with a *deterministic* default
password -- `default_password(roll_no)` -- and `password_set=False`.
`authenticate_student` returns whether the caller must now be forced
through a "create your own password" step before doing anything else
(server_student enforces this); `set_global_password` is what completes
that step (also used by an admin's "reset password" action). This is a
deliberate, disclosed tradeoff: the default password is guessable by
construction if the formula itself becomes known (it's not a secret -- an
instructor can announce it to an entire class as one sentence, which is
the whole point), so the *only* thing actually protecting an unclaimed
account is that nobody has logged into it yet. The mandatory first-login
password change is what closes that window, not the formula's obscurity.

A repo-root `.env` holds the single admin account, unrelated to any of the
above -- admin login was never part of this per-student model.

Both servers (server_student, server_admin) are separate OS processes that
can touch the same CSVs concurrently (a student logging in while the admin
resets someone else's password), so every read-modify-write goes through a
cross-process `filelock` -- a plain threading.Lock would not be enough here.
"""

from __future__ import annotations

import csv
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values, set_key
from filelock import FileLock
from werkzeug.security import check_password_hash, generate_password_hash

from grader.discover import ROLL_NO_RE

STUDENTS_CSV_FIELDS = ["roll_no", "ip", "active", "bound_at", "last_seen"]
GLOBAL_ACCOUNTS_FILENAME = "accounts.csv"  # under live/ -- shared across every lab, see module docstring
_GLOBAL_ACCOUNTS_FIELDS = ["roll_no", "password_hash", "password_set"]

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ENV_PATH = _REPO_ROOT / ".env"


class AccountError(Exception):
    """Raised for account/auth problems meant to surface as a clear message
    to the caller (unknown roll number, wrong password, already bound to a
    different device, missing admin account, ...) -- never a stack trace."""


@dataclass
class StudentAccount:
    roll_no: str
    ip: str = ""  # persistent "last device this account used" -- survives logout, only a fresh login overwrites it
    active: bool = False  # is there a currently live (not-logged-out) session -- this, not `ip`, is what "one device at a time" actually enforces
    bound_at: str = ""
    last_seen: str = ""


@dataclass
class GlobalAccount:
    roll_no: str
    password_hash: str
    password_set: bool = False  # False = still the deterministic default_password(), must be changed before anything else


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_roll_no(roll_no: str) -> str:
    """Same normalization+validation discover.py applies to a submissions/
    folder name, so a live account's identity and an offline folder's
    identity are never subtly different for the same student."""
    normalized = (roll_no or "").strip().upper()
    if not ROLL_NO_RE.match(normalized):
        raise AccountError(f"'{roll_no}' does not look like a valid roll number")
    return normalized


def default_password(roll_no: str) -> str:
    """The deterministic password every account starts with: the roll
    number reversed, plus a fixed suffix. `roll_no` is normalized first so
    the formula only ever depends on the canonical (uppercase) form,
    matching what's actually stored/compared everywhere else."""
    return f"{normalize_roll_no(roll_no)[::-1]}@Cp"


def _csv_lock(path: Path) -> FileLock:
    return FileLock(str(path) + ".lock")


# --------------------------------------------------------------------------
# per-lab session state -- students.csv (roll_no/ip/active/bound_at/last_seen)
# --------------------------------------------------------------------------


def _read_students(csv_path: Path) -> dict[str, StudentAccount]:
    if not csv_path.exists():
        return {}
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        return {
            row["roll_no"]: StudentAccount(
                roll_no=row["roll_no"],
                ip=row.get("ip") or "",
                # Missing "active" column (a students.csv written before
                # this field existed) reads as inactive, not an error --
                # the account simply looks logged-out until it next logs in.
                active=(row.get("active") or "") == "1",
                bound_at=row.get("bound_at") or "",
                last_seen=row.get("last_seen") or "",
            )
            for row in reader
        }


def _write_students(csv_path: Path, accounts: dict[str, StudentAccount]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-replace so a reader (e.g. the admin dashboard polling this
    # same file) never observes a half-written CSV.
    tmp_path = csv_path.with_suffix(".csv.tmp")
    with open(tmp_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=STUDENTS_CSV_FIELDS)
        writer.writeheader()
        for roll_no in sorted(accounts):
            a = accounts[roll_no]
            writer.writerow(
                {
                    "roll_no": a.roll_no,
                    "ip": a.ip,
                    "active": "1" if a.active else "",
                    "bound_at": a.bound_at,
                    "last_seen": a.last_seen,
                }
            )
    tmp_path.replace(csv_path)


def get_account(csv_path: Path, roll_no: str) -> StudentAccount | None:
    """Read-only, lock-free lookup -- safe despite concurrent writers because
    _write_students always writes to a temp file and atomically replaces the
    real one, so a reader here only ever sees a fully-written CSV (possibly
    stale by a few milliseconds, never torn/partial). Deliberately cheap:
    called on every authenticated request (see server_student.app's
    per-request device-binding check) to catch the moment an admin unbinds
    this account's device or a different device claims it -- a per-request
    filelock acquisition would be needless overhead for that.
    """
    roll_no = normalize_roll_no(roll_no)
    return _read_students(csv_path).get(roll_no)


def list_students(csv_path: Path) -> list[StudentAccount]:
    return sorted(_read_students(csv_path).values(), key=lambda a: a.roll_no)


def touch_last_seen(csv_path: Path, roll_no: str) -> None:
    """Cheap activity heartbeat for the admin live dashboard -- called on
    save/run, not just login, so 'last seen' reflects actual activity."""
    roll_no = normalize_roll_no(roll_no)
    with _csv_lock(csv_path):
        accounts = _read_students(csv_path)
        if roll_no in accounts:
            accounts[roll_no].last_seen = _now_iso()
            _write_students(csv_path, accounts)


def unbind_student_device(csv_path: Path, roll_no: str) -> None:
    """Marks the account's session inactive, freeing its device slot for
    reuse -- by the same student logging in again from anywhere, or by a
    different student on the same physical machine (see
    authenticate_student: both checks key on `active`, not `ip`).

    Deliberately does NOT clear `ip` itself -- that field is kept as a
    persistent "last device this account used" record even after
    logout/unbind, both for the admin dashboard's device column and so
    `grade.py --student-csv` can enrich summary.csv with it at finalize
    time. Only a fresh login ever overwrites `ip`.

    Used both by the admin's explicit "unbind" action and by a student's
    own /api/logout (server_student calls this directly) -- it's the same
    operation ("this device is no longer this account's active session"),
    only who triggered it differs.
    """
    roll_no = normalize_roll_no(roll_no)
    with _csv_lock(csv_path):
        accounts = _read_students(csv_path)
        if roll_no not in accounts:
            raise AccountError(f"No account for roll number {roll_no} in {csv_path}")
        accounts[roll_no].active = False
        _write_students(csv_path, accounts)


# --------------------------------------------------------------------------
# global account identity -- live/accounts.csv (roll_no/password_hash/password_set)
# --------------------------------------------------------------------------


def _read_global_accounts(path: Path) -> dict[str, GlobalAccount]:
    if not path.exists():
        return {}
    with open(path, newline="") as f:
        return {
            row["roll_no"]: GlobalAccount(
                roll_no=row["roll_no"],
                password_hash=row["password_hash"],
                password_set=(row.get("password_set") or "") == "1",
            )
            for row in csv.DictReader(f)
        }


def _write_global_accounts(path: Path, accounts: dict[str, GlobalAccount]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".csv.tmp")
    with open(tmp_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_GLOBAL_ACCOUNTS_FIELDS)
        writer.writeheader()
        for roll_no in sorted(accounts):
            a = accounts[roll_no]
            writer.writerow(
                {"roll_no": a.roll_no, "password_hash": a.password_hash, "password_set": "1" if a.password_set else ""}
            )
    tmp_path.replace(path)


def get_global_account(global_accounts_path: Path, roll_no: str) -> GlobalAccount | None:
    """Lock-free read, same reasoning as get_account -- called on every
    request that needs to enforce the "must set your own password" gate."""
    roll_no = normalize_roll_no(roll_no)
    return _read_global_accounts(global_accounts_path).get(roll_no)


def set_global_password(
    global_accounts_path: Path, roll_no: str, new_password: str, *, create_if_missing: bool = False
) -> None:
    """Sets this roll number's global password and marks it as no longer
    needing the forced first-login change. Two callers: a student setting
    their own password right after first login or voluntarily later
    (create_if_missing=False -- a logged-in student's account already
    exists by definition), and an admin's reset action
    (create_if_missing=True, so an admin can hand a student a real
    password even before they've ever logged in with the default one).
    """
    roll_no = normalize_roll_no(roll_no)
    with _csv_lock(global_accounts_path):
        accounts = _read_global_accounts(global_accounts_path)
        if roll_no not in accounts:
            if not create_if_missing:
                raise AccountError(f"No account for roll number {roll_no}")
            accounts[roll_no] = GlobalAccount(roll_no=roll_no, password_hash="", password_set=False)
        accounts[roll_no].password_hash = generate_password_hash(new_password)
        accounts[roll_no].password_set = True
        _write_global_accounts(global_accounts_path, accounts)


def authenticate_student(
    csv_path: Path, global_accounts_path: Path, roll_no: str, password: str, remote_addr: str
) -> tuple[str, bool]:
    """Returns (normalized_roll_no, must_set_password).

    No account needs to exist ahead of time: the first time a roll number
    is seen, it's auto-provisioned here, both globally (password_hash =
    the deterministic default, password_set=False) and in this lab's own
    session file -- provided the given password matches
    default_password(roll_no) exactly; getting it wrong is just the usual
    "incorrect roll number or password", not a differently-shaped error,
    so a wrong guess can't be used to probe whether an account exists yet.

    The one-device-at-a-time rule is enforced by `active`, not by `ip`
    alone, and lives entirely in the per-lab session file:
    - If this account is *currently active* on a different IP, reject --
      genuinely already signed in elsewhere. If it's not active (never
      logged in this lab, or properly signed out since), this check
      doesn't apply regardless of what `ip` still says (see
      unbind_student_device).
    - Independently, if *any other* account is currently active on this
      same IP, reject -- one student can't log in as themselves from a
      seat another student is actively using right now.
    - Otherwise: bind `ip` to this device, mark active, allow.
    """
    roll_no = normalize_roll_no(roll_no)
    with _csv_lock(csv_path), _csv_lock(global_accounts_path):
        global_accounts = _read_global_accounts(global_accounts_path)
        global_account = global_accounts.get(roll_no)
        if global_account is None:
            if password != default_password(roll_no):
                raise AccountError("Incorrect roll number or password.")
            global_account = GlobalAccount(
                roll_no=roll_no, password_hash=generate_password_hash(password), password_set=False
            )
            global_accounts[roll_no] = global_account
            _write_global_accounts(global_accounts_path, global_accounts)
        elif not check_password_hash(global_account.password_hash, password):
            raise AccountError("Incorrect roll number or password.")

        accounts = _read_students(csv_path)
        account = accounts.get(roll_no)
        if account is None:
            account = StudentAccount(roll_no=roll_no)

        if account.active and account.ip and account.ip != remote_addr:
            raise AccountError(
                "This account is already signed in on another device. "
                "Ask your instructor to sign it out."
            )

        for other_roll, other in accounts.items():
            if other_roll != roll_no and other.active and other.ip == remote_addr:
                raise AccountError(
                    "This device is currently in use by another student's session. "
                    "Ask them to sign out first, or contact your instructor."
                )

        now = _now_iso()
        account.ip = remote_addr
        account.active = True
        account.bound_at = now
        account.last_seen = now
        accounts[roll_no] = account
        _write_students(csv_path, accounts)

    return roll_no, not global_account.password_set


# --------------------------------------------------------------------------
# Admin account -- single set of credentials in repo-root .env, not a CSV
# --------------------------------------------------------------------------


def admin_env_exists(env_path: Path | None = None) -> bool:
    env_path = env_path or _DEFAULT_ENV_PATH
    values = dotenv_values(env_path)
    return bool(values.get("ADMIN_USERNAME") and values.get("ADMIN_PASSWORD_HASH"))


def init_admin(username: str, password: str, env_path: Path | None = None) -> None:
    """Bootstraps .env with an admin account plus two freshly generated
    session-signing secrets (one for each server -- see
    LIVE_LAB_DESIGN.md §5.2 on why they're separate). Refuses to overwrite an
    existing admin account; use reset_admin_password for that instead."""
    env_path = env_path or _DEFAULT_ENV_PATH
    if admin_env_exists(env_path):
        raise AccountError(f"{env_path} already has an admin account -- use reset_admin_password to change it")
    env_path.touch(exist_ok=True)
    set_key(str(env_path), "ADMIN_USERNAME", username)
    set_key(str(env_path), "ADMIN_PASSWORD_HASH", generate_password_hash(password))
    set_key(str(env_path), "ADMIN_SESSION_SECRET", secrets.token_hex(32))
    set_key(str(env_path), "STUDENT_SESSION_SECRET", secrets.token_hex(32))


def reset_admin_password(new_password: str, env_path: Path | None = None) -> None:
    env_path = env_path or _DEFAULT_ENV_PATH
    if not env_path.exists() or not admin_env_exists(env_path):
        raise AccountError(f"No admin account configured in {env_path} -- run init_admin first")
    set_key(str(env_path), "ADMIN_PASSWORD_HASH", generate_password_hash(new_password))


def authenticate_admin(username: str, password: str, env_path: Path | None = None) -> bool:
    env_path = env_path or _DEFAULT_ENV_PATH
    values = dotenv_values(env_path)
    expected_user = values.get("ADMIN_USERNAME")
    expected_hash = values.get("ADMIN_PASSWORD_HASH")
    if not expected_user or not expected_hash:
        raise AccountError(
            f"No admin account configured in {env_path} -- "
            "run 'python -m grader.manage_accounts init-admin'"
        )
    return username == expected_user and check_password_hash(expected_hash, password)
