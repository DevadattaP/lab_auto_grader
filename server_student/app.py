"""Student-facing live-lab server (LIVE_LAB_DESIGN.md §9): login, question
browsing, in-browser editor, on-demand "Run" against open tests only, the
session timer, and post-finalize results/leaderboard.

A separate process from server_admin (§3, §10) -- deliberately never
imports it. This process only ever touches `submissions/<lab>/` (its own
live write target), `questions/<lab>/` (read-only), `live/<lab>/`
(students.csv + session.json), and, read-only, `runs/<lab>/<finalized_run>/`
once the admin has finalized grading.

Run with:
    python3 -m server_student.app --lab lab_01
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from grader import accounts, display_config, live_session, student_view  # noqa: E402
from grader.config_schema import QuestionConfigError, load_all_questions  # noqa: E402
from grader.discover import StudentMapping, StudentMappingError, load_student_mapping  # noqa: E402
from grader.report import render_live_question_report, render_markdown_to_html  # noqa: E402
from grader.sandbox import choose_sandbox_kind  # noqa: E402
from grader.sandbox_pool import PoolExhausted, SandboxPool  # noqa: E402

log = logging.getLogger("server_student")

SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
RUN_COOLDOWN_SECONDS = 2.0

app = Flask(__name__)

# Populated once by main() before app.run() -- module-level so route
# functions (which Flask calls with no way to inject extra args) can reach
# them. All of it is read-only after startup except LAST_RUN and
# _last_run_at, which are protected by _state_lock.
LAB_ID: str = ""
QUESTIONS_DIR: Path
SUBMISSIONS_DIR: Path
LIVE_DIR: Path
RUNS_DIR: Path
STUDENTS_CSV: Path
# Shared across every lab by default (grader.accounts module docstring) --
# overridable via --global-accounts-path (see main()) so a dry-run/load-test
# against synthetic lab ids can't accidentally claim real students' default
# password logins on the production file.
GLOBAL_ACCOUNTS_PATH: Path = REPO_ROOT / "live" / accounts.GLOBAL_ACCOUNTS_FILENAME
QUESTIONS: dict = {}
SANDBOX_POOL: SandboxPool | None = None
HEARTBEAT_INTERVAL_SECONDS: int = 60  # overridden by --heartbeat-interval-seconds; also saved on blur/tab-switch and always on Run
STUDENT_MAPPING: StudentMapping = StudentMapping()  # optional roll_no -> name, see --student-names-csv
# The three "what stays visible after finalize" flags are NOT read as
# static globals -- they're runtime-editable by server_admin (a separate
# process) via grader.display_config, so every place that needs them calls
# display_config.get_config(LIVE_DIR) fresh. --hide-workspace-after-session
# etc. on this process's own CLI only seed live/<lab>/display_config.json
# the first time (see create_app's ensure_initialized call); after that,
# the file is the one source of truth for both servers.

_state_lock = threading.Lock()
_last_run_at: dict[str, float] = {}  # roll_no -> time.monotonic() of last /api/run
# roll_no -> {qid -> {"source": <exact text that was run>, "result": <the same
# JSON /api/run returns>}} -- kept server-side (not just in the browser) so
# a run's output survives switching questions *and* a page reload, and so
# "is this result stale" (source has since changed) can be judged from a
# single source of truth instead of trusting the client's own memory of
# what it last ran. In-memory only, by design: this is a UI convenience,
# never the source of truth for marks (that's always a fresh grading pass
# at finalize), so losing it on a server restart is harmless.
LAST_RUN: dict[str, dict[str, dict]] = {}


def _safe_qid(qid: str) -> str:
    if not qid or not SAFE_NAME_RE.match(qid) or qid not in QUESTIONS:
        abort(404, "unknown question id")
    return qid


def _current_roll_no() -> str:
    """Not just a session-cookie check: also re-confirms, on every request,
    that this session is still the account's currently-active one in
    students.csv. A signed session cookie has no server-side revocation on
    its own, so without this, an admin unbind (or a second device claiming
    the account) would only affect the *next* login -- an already-logged-in
    browser would keep working indefinitely on its stale cookie.

    Checks both `active` (an unbind/logout clears this without touching
    `ip` -- see accounts.unbind_student_device) and `ip` (a *different*
    device having since become the active one, which changes `ip` but
    leaves `active` true) -- either one changing means this particular
    browser's session is no longer the account's live one. Checked fresh
    every time, so it takes effect within one request (in practice, one
    heartbeat poll -- see editor.js's HEARTBEAT_INTERVAL_MS) instead of
    only on next login."""
    roll_no = session.get("roll_no")
    if not roll_no:
        abort(401, "not logged in")
    account = accounts.get_account(STUDENTS_CSV, roll_no)
    if account is None or not account.active or account.ip != request.remote_addr:
        session.clear()
        abort(401, "This account is now signed in from a different device. You have been signed out.")
    return roll_no


def _require_password_set(roll_no: str) -> None:
    """A student whose account still has the deterministic default
    password (brand new, or reset with must_set_password not otherwise
    cleared) must create their own private password before doing anything
    else that matters -- otherwise the default formula being guessable by
    construction would stay exploitable indefinitely. 403, not 401: the
    session itself is genuinely authenticated, just not done with setup.
    Deliberately NOT called from /api/set-password itself (or /api/logout,
    /api/whoami, /api/config, /api/session/status) -- those must keep
    working regardless, or a student could never complete this step."""
    global_account = accounts.get_global_account(GLOBAL_ACCOUNTS_PATH, roll_no)
    if global_account is None or not global_account.password_set:
        abort(403, "You must set a new password before continuing.")


def _student_name(roll_no: str) -> str | None:
    return STUDENT_MAPPING.entries.get(roll_no, {}).get("name")


def _workspace_gate_error(state) -> str | None:
    """Returns a student-facing error if question/code/run-result browsing
    shouldn't be reachable right now, else None."""
    if state.status == live_session.STATUS_NOT_STARTED:
        return "The lab has not started yet."
    if state.status == live_session.STATUS_FINALIZED and not display_config.get_config(LIVE_DIR).show_workspace_after_session:
        return "This lab has ended and workspace browsing is turned off for it."
    return None


def _get_student_locked_status(roll_no: str) -> bool:
    """Returns whether this student account is currently locked."""
    account = accounts.get_account(STUDENTS_CSV, roll_no)
    return account is not None and account.locked


def _question_paper_path() -> Path:
    return QUESTIONS_DIR / f"{LAB_ID}_qp.pdf"


def _question_paper_available(state) -> bool:
    """Deliberately different from _workspace_gate_error: reachable while
    the session is actually running, and again once finalized (per the
    admin's explicit ask -- students should be able to revisit the paper
    alongside their published report/leaderboard), but never while merely
    locked (mid-grading, not yet finalized) or before the session has
    started. Also gated by the admin's own show_question_paper toggle
    (grader.display_config, same runtime-editable mechanism as
    show_workspace_after_session etc.) and by the file actually existing."""
    return (
        state.status in (live_session.STATUS_RUNNING, live_session.STATUS_FINALIZED)
        and display_config.get_config(LIVE_DIR).show_question_paper
        and _question_paper_path().is_file()
    )


def _source_path(roll_no: str, question) -> Path:
    return SUBMISSIONS_DIR / roll_no / question.filename_patterns[0]


def _live_report_path(roll_no: str, question) -> Path:
    return SUBMISSIONS_DIR / roll_no / f"{question.qid}_live.md"


def _live_json_path(roll_no: str, question) -> Path:
    """A structured sibling of _live_report_path's markdown -- same
    directory, same "invisible to discover.py's filename-pattern matching"
    reasoning (it's a .json, never one of a question's configured .c
    filename_patterns). Exists so server_admin's live dashboard can read a
    clean, stable data shape instead of regex-scraping the human-readable
    markdown, which is free to change wording without breaking anything
    that only ever displayed it."""
    return SUBMISSIONS_DIR / roll_no / f"{question.qid}_live.json"


def _binary_dest(roll_no: str, question) -> Path:
    # Scratch, not part of submissions/ or runs/ -- those are the offline
    # grader's own territory (see LIVE_LAB_DESIGN.md §2).
    return LIVE_DIR / "bin" / roll_no / question.qid / "a.out"


def _save_source(roll_no: str, question, source: str) -> None:
    path = _source_path(roll_no, question)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def _iso_from_mtime(path: Path) -> str | None:
    """The file's own mtime doubles as "last saved at" -- no separate
    timestamp needs to be tracked alongside it, and it's correct regardless
    of whether the write came from /api/save or /api/run (both call
    _save_source). Surfaced by the question-detail endpoint so the save/run
    status line survives a page reload or switching questions instead of
    living only in the browser's memory."""
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    roll_no = str(data.get("roll_no", ""))
    password = str(data.get("password", ""))
    try:
        # Checked BEFORE authenticate_student, not after: that call
        # auto-provisions a brand-new global account on a correct
        # default-password attempt (see its own docstring), so checking
        # roster membership afterwards would let an unlisted roll number's
        # first attempt silently create an account moments before being
        # rejected. Same error message/shape as a genuinely wrong
        # roll_no/password so an unlisted student can't tell the
        # difference from a typo. Off by default (DisplayConfig) -- most
        # labs never turn this on, so the common case pays only one cheap
        # dict lookup per login attempt.
        if display_config.get_config(LIVE_DIR).restrict_login_to_roster:
            normalized_for_roster_check = accounts.normalize_roll_no(roll_no)
            if normalized_for_roster_check not in STUDENT_MAPPING.entries:
                raise accounts.AccountError("Incorrect roll number or password.")
        normalized, must_set_password = accounts.authenticate_student(
            STUDENTS_CSV, GLOBAL_ACCOUNTS_PATH, roll_no, password, request.remote_addr
        )
    except accounts.AccountError as e:
        return jsonify({"error": str(e)}), 401
    session.clear()
    session["roll_no"] = normalized
    session.permanent = True
    return jsonify({"ok": True, "roll_no": normalized, "must_set_password": must_set_password})


@app.post("/api/logout")
def api_logout():
    # Marks the session inactive (not the same as clearing `ip` -- see
    # accounts.unbind_student_device) so the dashboard correctly shows this
    # student offline, and so the same student (or a different one on the
    # same physical PC) can sign in again without needing an admin unbind.
    # Requiring an explicit logout to reach this state is what still
    # prevents two *simultaneous* devices: a still-active session elsewhere
    # keeps blocking new logins regardless of this.
    roll_no = session.get("roll_no")
    if roll_no:
        accounts.unbind_student_device(STUDENTS_CSV, roll_no)
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/whoami")
def api_whoami():
    roll_no = session.get("roll_no")
    must_set_password = False
    if roll_no:
        global_account = accounts.get_global_account(GLOBAL_ACCOUNTS_PATH, roll_no)
        must_set_password = global_account is None or not global_account.password_set
    return jsonify({"roll_no": roll_no, "must_set_password": must_set_password})


@app.post("/api/set-password")
def api_set_password():
    """Completes the mandatory first-login step (or a voluntary later
    change) -- does NOT go through _require_password_set (that would make
    this endpoint unreachable for the exact student who needs it)."""
    roll_no = _current_roll_no()
    data = request.get_json(silent=True) or {}
    new_password = str(data.get("new_password", ""))
    confirm_password = str(data.get("confirm_password", ""))
    if len(new_password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    if new_password != confirm_password:
        return jsonify({"error": "Passwords do not match."}), 400
    if new_password == accounts.default_password(roll_no):
        return jsonify({"error": "Choose a password other than the default one."}), 400
    accounts.set_global_password(GLOBAL_ACCOUNTS_PATH, roll_no, new_password)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# session / timer
# --------------------------------------------------------------------------


@app.get("/api/config")
def api_config():
    _current_roll_no()
    display_cfg = display_config.get_config(LIVE_DIR)
    return jsonify(
        {
            "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
            "show_workspace_after_session": display_cfg.show_workspace_after_session,
            "show_report": display_cfg.show_report,
            "show_leaderboard": display_cfg.show_leaderboard,
        }
    )


@app.get("/api/session/status")
def api_session_status():
    roll_no = _current_roll_no()
    state = live_session.auto_lock_if_expired(LIVE_DIR)
    locked = _get_student_locked_status(roll_no)
    paper_available = _question_paper_available(state)
    return jsonify(
        {
            "status": state.status,
            "duration_minutes": state.duration_minutes,
            "end_time": state.end_time,
            "time_remaining_seconds": live_session.time_remaining_seconds(LIVE_DIR),
            "finalized_run": state.finalized_run,
            "student_locked": locked,
            "question_paper_available": paper_available,
        }
    )


# --------------------------------------------------------------------------
# questions
# --------------------------------------------------------------------------


def _chip_summary(result: dict) -> dict:
    """The small subset of a full /api/run result the question-list sidebar
    needs for its chip -- derived from the same stored result the detail
    view and the run response itself use, rather than kept as a second,
    separately-updated cache."""
    return {
        "open_passed": sum(1 for d in result["tests"] if d["passed"]),
        "open_total": len(result["tests"]),
        "compile_ok": result["compile_ok"],
        "code_check_satisfied": result["code_check"]["satisfied"] if result["code_check"] else None,
    }


@app.get("/api/questions")
def api_questions():
    roll_no = _current_roll_no()
    _require_password_set(roll_no)
    state = live_session.auto_lock_if_expired(LIVE_DIR)
    gate_error = _workspace_gate_error(state)
    if gate_error:
        return jsonify({"error": gate_error}), 403

    out = []
    with _state_lock:
        runs = dict(LAST_RUN.get(roll_no, {}))
    for qid in sorted(QUESTIONS):
        q = QUESTIONS[qid]
        s = student_view.student_question_summary(q)
        source_path = _source_path(roll_no, q)
        has_saved = source_path.exists()
        entry = runs.get(qid)
        last_run = None
        stale = False
        if entry:
            last_run = _chip_summary(entry["result"])
            current_source = source_path.read_text() if has_saved else ""
            stale = entry["source"] != current_source
        out.append(
            {
                "qid": s.qid,
                "title": s.title,
                "open_test_count": s.open_test_count,
                "marks_open": s.marks_open,
                "has_saved_code": has_saved,
                "last_run": last_run,
                "last_run_stale": stale,
            }
        )
    return jsonify({"questions": out})


@app.get("/api/questions/<qid>")
def api_question_detail(qid: str):
    roll_no = _current_roll_no()
    _require_password_set(roll_no)
    qid = _safe_qid(qid)
    state = live_session.auto_lock_if_expired(LIVE_DIR)
    gate_error = _workspace_gate_error(state)
    if gate_error:
        return jsonify({"error": gate_error}), 403

    question = QUESTIONS[qid]
    s = student_view.student_question_summary(question)
    source_path = _source_path(roll_no, question)
    source = source_path.read_text() if source_path.exists() else ""

    with _state_lock:
        entry = LAST_RUN.get(roll_no, {}).get(qid)
    last_run = entry["result"] if entry else None
    last_run_stale = entry is not None and entry["source"] != source

    return jsonify(
        {
            "qid": s.qid,
            "title": s.title,
            "description": s.description,
            "open_test_count": s.open_test_count,
            "marks_open": s.marks_open,
            "source": source,
            "saved_at": _iso_from_mtime(source_path),
            "write_allowed": live_session.is_write_allowed(LIVE_DIR),
            "last_run": last_run,
            "last_run_stale": last_run_stale,
        }
    )


@app.get("/api/question-paper")
def api_question_paper():
    # Same auth as every other student endpoint -- this is not a static
    # asset (nothing under server_student/static/ is auth-gated), so a
    # student must have a live, current session to fetch it, and the file
    # itself lives under questions/<lab_id>/ rather than server_student's
    # own static folder to keep it scoped to this process's one lab.
    _current_roll_no()
    state = live_session.auto_lock_if_expired(LIVE_DIR)
    if not _question_paper_available(state):
        abort(403, "The question paper is not available right now.")
    pdf_path = _question_paper_path()
    return send_file(pdf_path, mimetype="application/pdf", as_attachment=False, download_name=f"{LAB_ID}_question_paper.pdf")


# --------------------------------------------------------------------------
# save / run
# --------------------------------------------------------------------------


@app.post("/api/save")
def api_save():
    roll_no = _current_roll_no()
    _require_password_set(roll_no)
    data = request.get_json(silent=True) or {}
    qid = _safe_qid(data.get("qid", ""))
    source = data.get("source", "")
    if _get_student_locked_status(roll_no):
        return jsonify({"error": "Your account has been locked. You cannot submit code."}), 423
    if not live_session.is_write_allowed(LIVE_DIR):
        return jsonify({"error": "The lab is not currently accepting submissions."}), 423

    question = QUESTIONS[qid]
    _save_source(roll_no, question, source)
    accounts.touch_last_seen(STUDENTS_CSV, roll_no)
    return jsonify({"ok": True, "saved_at": _iso_from_mtime(_source_path(roll_no, question))})


@app.post("/api/run")
def api_run():
    roll_no = _current_roll_no()
    _require_password_set(roll_no)
    data = request.get_json(silent=True) or {}
    qid = _safe_qid(data.get("qid", ""))
    source = data.get("source", "")
    if _get_student_locked_status(roll_no):
        return jsonify({"error": "Your account has been locked. You cannot run code."}), 423
    if not live_session.is_write_allowed(LIVE_DIR):
        return jsonify({"error": "The lab is not currently accepting submissions."}), 423

    now = time.monotonic()
    with _state_lock:
        last = _last_run_at.get(roll_no, 0.0)
        if now - last < RUN_COOLDOWN_SECONDS:
            wait = RUN_COOLDOWN_SECONDS - (now - last)
            return jsonify({"error": f"Please wait {wait:.1f}s before running again."}), 429
        _last_run_at[roll_no] = now

    question = QUESTIONS[qid]
    _save_source(roll_no, question, source)

    try:
        with SANDBOX_POOL.checkout() as sandbox:
            qs = student_view.run_open_tests(sandbox, question, _source_path(roll_no, question), _binary_dest(roll_no, question))
    except PoolExhausted:
        return jsonify({"error": "The grading server is busy -- please try again in a moment."}), 503

    _live_report_path(roll_no, question).write_text(render_live_question_report(roll_no, qs))
    accounts.touch_last_seen(STUDENTS_CSV, roll_no)

    result_payload = {
        "qid": qs.qid,
        "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "compile_ok": qs.compile_ok,
        "compile_stderr": qs.compile_stderr,
        "marks_earned": qs.marks_earned,
        "marks_total": qs.marks_total,
        "tests": qs.open_details,
        "code_check": (
            None
            if qs.code_check_mode is None
            else {
                "mode": qs.code_check_mode,
                "require_match": qs.code_check_require_match,
                "satisfied": qs.code_check_satisfied,
                "missing": qs.code_check_missing,
                "forbidden": qs.code_check_forbidden,
                "found": qs.code_check_found,
                "gated_to_zero": qs.gated_to_zero,
                "penalty_applied": qs.penalty_applied,
                "marks_earned": qs.code_check_marks_earned,
                "marks_total": qs.code_check_marks_total,
            }
        ),
    }

    # Stored keyed to the exact source that produced it -- this is what lets
    # /api/questions and /api/questions/<qid> later tell whether the editor
    # has since diverged from what was actually run (see _chip_summary and
    # the "last_run_stale" fields above).
    with _state_lock:
        LAST_RUN.setdefault(roll_no, {})[qid] = {"source": source, "result": result_payload}

    _live_json_path(roll_no, question).write_text(
        json.dumps({"run_at": result_payload["run_at"], "marks_earned": qs.marks_earned,
                     "marks_total": qs.marks_total, **_chip_summary(result_payload)})
    )

    return jsonify(result_payload)


# --------------------------------------------------------------------------
# post-finalize: results + leaderboard
# --------------------------------------------------------------------------


def _finalized_run_dir() -> Path | None:
    state = live_session.get_state(LIVE_DIR)
    if state.status != live_session.STATUS_FINALIZED or not state.finalized_run:
        return None
    run_dir = RUNS_DIR / state.finalized_run
    return run_dir if run_dir.is_dir() else None


@app.get("/api/results")
def api_results():
    roll_no = _current_roll_no()
    _require_password_set(roll_no)
    if not display_config.get_config(LIVE_DIR).show_report:
        return jsonify({"error": "Reports are turned off for this lab."}), 403
    run_dir = _finalized_run_dir()
    if run_dir is None:
        return jsonify({"error": "Results are not available yet."}), 409

    report_path = None
    for candidate in (f"report_{roll_no.upper()}.md", f"report_{roll_no}.md"):
        p = run_dir / candidate
        if p.is_file():
            report_path = p
            break
    if report_path is None:
        return jsonify({"error": "No report found for you in this lab's results."}), 404

    html = render_markdown_to_html(report_path.read_text(errors="replace"))
    return jsonify({"html": html})


@app.get("/api/leaderboard")
def api_leaderboard():
    roll_no = _current_roll_no()
    _require_password_set(roll_no)
    if not display_config.get_config(LIVE_DIR).show_leaderboard:
        return jsonify({"error": "The leaderboard is turned off for this lab."}), 403
    run_dir = _finalized_run_dir()
    if run_dir is None:
        return jsonify({"error": "Results are not available yet."}), 409

    import csv

    summary_path = run_dir / "summary.csv"
    if not summary_path.is_file():
        return jsonify({"error": "No summary available for this lab's results."}), 404

    rows = []
    with open(summary_path, newline="") as f:
        for row in csv.DictReader(f):
            total_cell = row.get("total", "0/0")
            earned_str, _, possible_str = total_cell.partition("/")
            try:
                earned = float(earned_str)
                possible = float(possible_str) if possible_str else 0.0
            except ValueError:
                earned, possible = 0.0, 0.0
            roll_no = row.get("roll_no", "")
            rows.append({"roll_no": roll_no, "name": _student_name(roll_no), "earned": earned, "possible": possible})

    rows.sort(key=lambda r: r["earned"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return jsonify({"leaderboard": rows})


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------


@app.get("/")
def index():
    if not session.get("roll_no"):
        return redirect("/login")
    roll_no = session["roll_no"]
    # A student who somehow has a valid session but hasn't completed the
    # mandatory first-login password change (e.g. they closed the tab
    # right after logging in, before finishing that step) gets bounced
    # back to /login, whose own JS detects this via /api/whoami and shows
    # the set-password form directly rather than asking for credentials
    # again.
    global_account = accounts.get_global_account(GLOBAL_ACCOUNTS_PATH, roll_no)
    if global_account is None or not global_account.password_set:
        return redirect("/login")
    return render_template("editor.html", lab_id=LAB_ID, roll_no=roll_no, student_name=_student_name(roll_no))


@app.get("/login")
def login_page():
    # Must agree with index()'s own redirect condition, or the two bounce
    # forever: a session with roll_no set but password not yet set would
    # otherwise get redirected /login -> / -> /login -> / ... with no page
    # ever actually rendering (a real infinite loop, not just a slow one --
    # this is a plain 302, no JS involved, so the browser follows it as
    # fast as it can get a response).
    roll_no = session.get("roll_no")
    if roll_no:
        global_account = accounts.get_global_account(GLOBAL_ACCOUNTS_PATH, roll_no)
        if global_account and global_account.password_set:
            return redirect("/")
        # Still logged in but password not set yet -- render login.html
        # anyway; its own JS calls /api/whoami and shows the set-password
        # form directly, without asking for credentials again.
    return render_template("login.html")


def create_app(
    lab_id: str,
    sandbox_kind_requested: str = "auto",
    sandbox_slots: int = 4,
    secret_key: str | None = None,
    heartbeat_interval_seconds: int = 60,
    student_names_csv: str | None = None,
    show_workspace_after_session: bool = True,
    show_report: bool = True,
    show_leaderboard: bool = True,
    global_accounts_path: Path | None = None,
) -> Flask:
    global LAB_ID, QUESTIONS_DIR, SUBMISSIONS_DIR, LIVE_DIR, RUNS_DIR, STUDENTS_CSV, QUESTIONS, SANDBOX_POOL
    global HEARTBEAT_INTERVAL_SECONDS, STUDENT_MAPPING, GLOBAL_ACCOUNTS_PATH

    if global_accounts_path is not None:
        GLOBAL_ACCOUNTS_PATH = Path(global_accounts_path)
    LAB_ID = lab_id
    HEARTBEAT_INTERVAL_SECONDS = heartbeat_interval_seconds
    QUESTIONS_DIR = REPO_ROOT / "questions" / lab_id
    SUBMISSIONS_DIR = REPO_ROOT / "submissions" / lab_id
    LIVE_DIR = REPO_ROOT / "live" / lab_id
    RUNS_DIR = REPO_ROOT / "runs" / lab_id
    STUDENTS_CSV = LIVE_DIR / "students.csv"
    SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_DIR.mkdir(parents=True, exist_ok=True)

    # Seeds live/<lab>/display_config.json from this process's own CLI args
    # ONLY if nothing is there yet -- once it exists, server_admin owns it
    # at runtime (see grader/display_config.py), so a restart of this
    # process must never reset an admin's already-made choice back to the
    # CLI defaults.
    display_config.ensure_initialized(
        LIVE_DIR,
        show_workspace_after_session=show_workspace_after_session,
        show_report=show_report,
        show_leaderboard=show_leaderboard,
    )

    try:
        questions_list = load_all_questions(QUESTIONS_DIR)
    except QuestionConfigError as e:
        raise SystemExit(f"Cannot load questions for lab '{lab_id}': {e}")
    QUESTIONS = {q.qid: q for q in questions_list}
    log.info("Loaded %d question(s) for %s: %s", len(QUESTIONS), lab_id, ", ".join(sorted(QUESTIONS)))

    if not STUDENTS_CSV.exists():
        log.info(
            "%s does not exist yet -- that's fine, it's created the moment the first student "
            "logs in (default password = their roll number reversed + '@Cp').",
            STUDENTS_CSV,
        )

    # Optional, purely cosmetic -- falls back to REPO_ROOT/live/student_names.csv
    # (resolved by main(), same convention as the shared accounts.csv) if not
    # given, and to no names at all if that doesn't exist either.
    try:
        STUDENT_MAPPING = load_student_mapping(Path(student_names_csv) if student_names_csv else None)
    except StudentMappingError as e:
        raise SystemExit(f"--student-names-csv error: {e}")
    if STUDENT_MAPPING.entries:
        log.info("Loaded %d student name(s) from %s", len(STUDENT_MAPPING.entries), student_names_csv)

    sandbox_kind = choose_sandbox_kind(sandbox_kind_requested, log=log)
    SANDBOX_POOL = SandboxPool(kind=sandbox_kind, size=sandbox_slots, scratch_root=LIVE_DIR / "sandbox_scratch")
    log.info("Sandbox pool ready: kind=%s slots=%d", sandbox_kind, sandbox_slots)

    if not secret_key:
        raise SystemExit(
            "STUDENT_SESSION_SECRET is not set -- run 'python -m grader.manage_accounts init-admin' "
            "once to bootstrap .env (it generates this alongside the admin account)."
        )
    app.secret_key = secret_key
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Student-facing live-lab server")
    parser.add_argument("--lab", required=True, help="lab id, e.g. lab_01 -- must exist under questions/")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--sandbox", choices=["auto", "isolate", "subprocess"], default="auto")
    parser.add_argument("--sandbox-slots", type=int, default=4)
    parser.add_argument(
        "--heartbeat-interval-seconds", type=int, default=60,
        help="how often each logged-in browser checks session/timer status AND autosaves the editor "
             "in the background (one combined poll, not two) -- in addition to always saving on Run "
             "and on tab-switch/close. Keep this reasonably long (default 60s) with 30+ students "
             "logged in at once, since it's one HTTP request per student per interval.",
    )
    parser.add_argument(
        "--student-names-csv", default=None,
        help="optional CSV (roll_no,name[,ip]) to show student names alongside roll numbers -- falls "
             "back to $STUDENT_NAMES_CSV, then live/student_names.csv if present; omitted entirely "
             "(roll numbers only) if none of those resolve to a real file",
    )
    parser.add_argument(
        "--hide-workspace-after-session", action="store_true",
        help="once finalized, hide questions/code/run-results and show only the report/leaderboard "
             "(default: keep them browsable alongside the results)",
    )
    parser.add_argument("--hide-report", action="store_true", help="don't show each student their own per-question report after finalize")
    parser.add_argument("--hide-leaderboard", action="store_true", help="don't show the class leaderboard after finalize")
    parser.add_argument(
        "--global-accounts-path", default=None,
        help="override live/accounts.csv (the shared, cross-lab password identity file) -- use this "
             "to point a dry-run/load-test lab at an isolated file instead of real students' actual "
             "accounts. Defaults to the real shared file if omitted.",
    )
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    import os

    secret_key = os.environ.get("STUDENT_SESSION_SECRET")

    student_names_csv = args.student_names_csv or os.environ.get("STUDENT_NAMES_CSV")
    if not student_names_csv:
        default_names_path = REPO_ROOT / "live" / "student_names.csv"
        student_names_csv = str(default_names_path) if default_names_path.exists() else None

    flask_app = create_app(
        lab_id=args.lab,
        sandbox_kind_requested=args.sandbox,
        sandbox_slots=args.sandbox_slots,
        secret_key=secret_key,
        heartbeat_interval_seconds=args.heartbeat_interval_seconds,
        student_names_csv=student_names_csv,
        show_workspace_after_session=not args.hide_workspace_after_session,
        show_report=not args.hide_report,
        show_leaderboard=not args.hide_leaderboard,
        global_accounts_path=Path(args.global_accounts_path) if args.global_accounts_path else None,
    )
    flask_app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
