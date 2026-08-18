"""Admin-facing server for the live-lab platform (LIVE_LAB_DESIGN.md §10):
question editor (reused from ui/app.py, now behind login), account
generation/reset/unbind, session/timer control, a live dashboard of
per-student per-question status, and the "Finalize & Grade" trigger.

A separate process from server_student (§3) -- never imports it, and the
two share no in-memory state, only files on disk (students.csv,
session.json, the *_live.json sidecars server_student writes on every Run,
and eventually runs/<lab>/<ts>/ once Finalize runs the batch grader).

Run with:
    python3 -m server_admin.app --port 5002
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, session

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from grader import accounts, display_config, live_session  # noqa: E402
from grader.config_schema import QuestionConfigError, load_all_questions  # noqa: E402
from grader.discover import StudentMapping, StudentMappingError, load_student_mapping  # noqa: E402
from grader.report import render_markdown_to_html  # noqa: E402
from grader.student_view import open_only  # noqa: E402
from ui.app import _lab_dirs, _safe_name, bp as ui_bp  # noqa: E402

log = logging.getLogger("server_admin")

LIVE_ROOT = REPO_ROOT / "live"
GLOBAL_ACCOUNTS_PATH = LIVE_ROOT / accounts.GLOBAL_ACCOUNTS_FILENAME
GRADE_PARALLEL = 4  # overridden by --grade-parallel
STUDENT_MAPPING = StudentMapping()  # optional roll_no -> name, see --student-names-csv


def _student_name(roll_no: str) -> str | None:
    return STUDENT_MAPPING.entries.get(roll_no, {}).get("name")

app = Flask(__name__, static_folder="static", static_url_path="/admin-static", template_folder="templates")
app.register_blueprint(ui_bp)

# Routes that must stay reachable *without* being logged in yet -- the
# login page itself, its own POST target, and its static assets (which
# live under /admin-static, distinct from ui_bp's own "/static" so the two
# UIs' assets never collide).
_PUBLIC_PATHS = {"/admin/login"}


@app.before_request
def _require_admin():
    if request.path in _PUBLIC_PATHS or request.path.startswith("/admin-static/"):
        return None
    if not session.get("admin"):
        if request.path.startswith("/api/"):
            abort(401, "admin login required")
        return redirect("/admin/login")
    return None


def _live_dir(lab_id: str) -> Path:
    return LIVE_ROOT / _safe_name(lab_id)


def _students_csv(lab_id: str) -> Path:
    return _live_dir(lab_id) / "students.csv"


# --------------------------------------------------------------------------
# admin auth
# --------------------------------------------------------------------------


@app.get("/admin/login")
def admin_login_page():
    if session.get("admin"):
        return redirect("/")
    return render_template("admin_login.html")


@app.post("/admin/login")
def admin_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", ""))
    password = str(data.get("password", ""))
    try:
        ok = accounts.authenticate_admin(username, password)
    except accounts.AccountError as e:
        return jsonify({"error": str(e)}), 500
    if not ok:
        return jsonify({"error": "Incorrect username or password."}), 401
    session.clear()
    session["admin"] = True
    session.permanent = True
    return jsonify({"ok": True})


@app.post("/admin/logout")
def admin_logout():
    session.clear()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------


@app.get("/api/live/<lab_id>/accounts")
def api_list_accounts(lab_id: str):
    # No admin action creates these rows -- a student's first successful
    # login (default_password(roll_no), see grader.accounts) auto-provisions
    # both the global identity and this lab's own session row, so this list
    # naturally starts empty and grows as students actually sign in.
    rows = accounts.list_students(_students_csv(lab_id))
    out = []
    for a in rows:
        global_account = accounts.get_global_account(GLOBAL_ACCOUNTS_PATH, a.roll_no)
        out.append(
            {
                "roll_no": a.roll_no,
                "name": _student_name(a.roll_no),
                "ip": a.ip,
                "active": a.active,
                "bound_at": a.bound_at,
                "last_seen": a.last_seen,
                "password_set": bool(global_account and global_account.password_set),
                "locked": a.locked,
            }
        )
    return jsonify({"accounts": out})


@app.post("/api/live/<lab_id>/accounts/<roll>/reset")
def api_reset_account(lab_id: str, roll: str):
    data = request.get_json(silent=True) or {}
    new_password = str(data.get("password", ""))
    confirm_password = str(data.get("confirm_password", ""))
    if len(new_password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    if new_password != confirm_password:
        return jsonify({"error": "Passwords do not match."}), 400
    try:
        # create_if_missing=True: an admin can hand a student a real
        # password even before they've ever logged in with the default one.
        accounts.set_global_password(GLOBAL_ACCOUNTS_PATH, roll, new_password, create_if_missing=True)
    except accounts.AccountError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True})


@app.post("/api/live/<lab_id>/accounts/<roll>/unbind")
def api_unbind_account(lab_id: str, roll: str):
    try:
        accounts.unbind_student_device(_students_csv(lab_id), roll)
    except accounts.AccountError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True})


@app.post("/api/live/<lab_id>/accounts/<roll>/lock")
def api_lock_account(lab_id: str, roll: str):
    try:
        accounts.lock_student(_students_csv(lab_id), roll)
    except accounts.AccountError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True})


@app.post("/api/live/<lab_id>/accounts/<roll>/unlock")
def api_unlock_account(lab_id: str, roll: str):
    try:
        accounts.unlock_student(_students_csv(lab_id), roll)
    except accounts.AccountError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# session / timer
# --------------------------------------------------------------------------


@app.get("/api/live/<lab_id>/session/status")
def api_session_status(lab_id: str):
    state = live_session.auto_lock_if_expired(_live_dir(lab_id))
    return jsonify(
        {
            "status": state.status,
            "start_time": state.start_time,
            "duration_minutes": state.duration_minutes,
            "end_time": state.end_time,
            "locked_at": state.locked_at,
            "finalized_run": state.finalized_run,
            "time_remaining_seconds": live_session.time_remaining_seconds(_live_dir(lab_id)),
        }
    )


@app.post("/api/live/<lab_id>/session/start")
def api_session_start(lab_id: str):
    data = request.get_json(silent=True) or {}
    try:
        duration_minutes = int(data.get("duration_minutes", 0))
        state = live_session.start(_live_dir(lab_id), duration_minutes)
    except (live_session.SessionError, ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": state.status, "end_time": state.end_time})


@app.post("/api/live/<lab_id>/session/extend")
def api_session_extend(lab_id: str):
    data = request.get_json(silent=True) or {}
    try:
        additional_minutes = int(data.get("additional_minutes", 0))
        state = live_session.extend(_live_dir(lab_id), additional_minutes)
    except (live_session.SessionError, ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": state.status, "end_time": state.end_time})


@app.post("/api/live/<lab_id>/session/lock")
def api_session_lock(lab_id: str):
    try:
        state = live_session.lock(_live_dir(lab_id))
    except live_session.SessionError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": state.status})


@app.post("/api/live/<lab_id>/session/reset")
def api_session_reset(lab_id: str):
    state = live_session.reset(_live_dir(lab_id))
    return jsonify({"status": state.status})


# --------------------------------------------------------------------------
# display config -- what stays visible to students once finalized. Written
# here, read fresh by server_student on every relevant request (see
# grader/display_config.py) -- a change here takes effect for every
# logged-in student within one heartbeat poll, no restart of either
# process needed.
# --------------------------------------------------------------------------


@app.get("/api/live/<lab_id>/display-config")
def api_get_display_config(lab_id: str):
    cfg = display_config.get_config(_live_dir(lab_id))
    return jsonify(
        {
            "show_workspace_after_session": cfg.show_workspace_after_session,
            "show_report": cfg.show_report,
            "show_leaderboard": cfg.show_leaderboard,
        }
    )


@app.post("/api/live/<lab_id>/display-config")
def api_set_display_config(lab_id: str):
    data = request.get_json(silent=True) or {}
    overrides = {}
    for key in ("show_workspace_after_session", "show_report", "show_leaderboard"):
        if key in data:
            overrides[key] = bool(data[key])
    if not overrides:
        return jsonify({"error": "No display-config fields given."}), 400
    try:
        cfg = display_config.set_config(_live_dir(lab_id), **overrides)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(
        {
            "show_workspace_after_session": cfg.show_workspace_after_session,
            "show_report": cfg.show_report,
            "show_leaderboard": cfg.show_leaderboard,
        }
    )


# --------------------------------------------------------------------------
# live dashboard -- per-student, per-question status, read straight off the
# *_live.json sidecars server_student writes on every Run (see that
# module's api_run) -- no IPC between the two processes needed.
# --------------------------------------------------------------------------


def _finalized_totals(lab_id: str) -> tuple[dict[str, float], float] | None:
    """roll_no -> earned marks, plus the shared total-possible, read from the
    finalized run's summary.csv (same "earned/possible" cell format and
    parsing server_student's own /api/leaderboard uses) -- the real,
    hidden-tests-included totals, available only once finalize has actually
    published a run. Returns None if the session isn't finalized yet."""
    state = live_session.get_state(_live_dir(lab_id))
    if state.status != "finalized" or not state.finalized_run:
        return None
    _, _, r_dir = _lab_dirs(lab_id)
    summary_path = r_dir / state.finalized_run / "summary.csv"
    if not summary_path.is_file():
        return None

    import csv

    earned_by_roll: dict[str, float] = {}
    possible = 0.0
    with open(summary_path, newline="") as f:
        for row in csv.DictReader(f):
            total_cell = row.get("total", "0/0")
            earned_str, _, possible_str = total_cell.partition("/")
            try:
                earned = float(earned_str)
                possible = float(possible_str) if possible_str else possible
            except ValueError:
                earned = 0.0
            earned_by_roll[row.get("roll_no", "")] = earned
    return earned_by_roll, possible


@app.get("/api/live/<lab_id>/dashboard")
def api_live_dashboard(lab_id: str):
    q_dir, s_dir, _ = _lab_dirs(lab_id)
    try:
        questions = load_all_questions(q_dir)
    except QuestionConfigError as e:
        return jsonify({"error": str(e)}), 400
    qids = [q.qid for q in questions]

    # Before finalize, only open-test marks are ever known live (hidden
    # tests never run outside the offline grader -- LIVE_LAB_DESIGN.md
    # §7.1), so the Total column can only reflect that partial ceiling.
    # Once finalized, the real per-student totals (hidden tests included)
    # already exist in the published run's summary.csv and are no longer
    # secret from students themselves, so prefer those instead -- otherwise
    # every "Total" would misleadingly read 0/0 for labs that (like this
    # one) put all their marks on hidden tests.
    finalized = _finalized_totals(lab_id)
    if finalized is not None:
        finalized_earned, grand_total = finalized
    else:
        finalized_earned = None
        grand_total = sum(open_only(q).total_marks for q in questions)

    student_rows = accounts.list_students(_students_csv(lab_id))
    out = []
    for a in student_rows:
        per_question = {}
        marks_earned_sum = 0.0
        for qid in qids:
            live_json = s_dir / a.roll_no / f"{qid}_live.json"
            entry = None
            if live_json.exists():
                try:
                    entry = json.loads(live_json.read_text())
                except (OSError, json.JSONDecodeError):
                    entry = None
            per_question[qid] = entry
            if entry:
                marks_earned_sum += entry.get("marks_earned") or 0.0
        if finalized_earned is not None:
            marks_earned_sum = finalized_earned.get(a.roll_no, 0.0)
        out.append(
            {
                "roll_no": a.roll_no,
                "name": _student_name(a.roll_no),
                "logged_in": a.active,
                "last_seen": a.last_seen,
                "questions": per_question,
                "total_earned": marks_earned_sum,
            }
        )
    return jsonify({"question_ids": qids, "students": out, "total_possible": grand_total})


@app.get("/api/live/<lab_id>/dashboard/<qid>/<roll>/detail")
def api_live_dashboard_detail(lab_id: str, qid: str, roll: str):
    """Full live.md for one student+question, rendered -- the "click a
    cell to see the actual test detail" drill-down, same rendering
    vocabulary as ui.app's own (finalized-run) dashboard detail view."""
    _, s_dir, _ = _lab_dirs(lab_id)
    qid = _safe_name(qid)
    roll = _safe_name(roll)
    live_md = s_dir / roll / f"{qid}_live.md"
    if not live_md.is_file():
        return jsonify({"html": None})
    return jsonify({"html": render_markdown_to_html(live_md.read_text(errors="replace"))})


# --------------------------------------------------------------------------
# finalize -- invoke the unmodified batch grader (grader.grade) exactly as
# the offline CLI workflow always has (AUTOGRADER_DESIGN.md §9), then
# publish the result to students.csv via mark_finalized. Runs in a
# background thread (grading a full class can take minutes) -- the POST
# just starts the job and returns immediately; the admin UI polls
# /finalize/status for progress, the same pattern as session/live-status
# polling already uses.
# --------------------------------------------------------------------------

_finalize_lock = threading.Lock()
_finalize_jobs: dict[str, dict] = {}  # lab_id -> {"status": "running"|"done"|"error", ...}


def _run_finalize_job(lab_id: str) -> None:
    live_dir = _live_dir(lab_id)
    q_dir, s_dir, r_dir = _lab_dirs(lab_id)
    cmd = [
        sys.executable, "-m", "grader.grade",
        "--questions", str(q_dir),
        "--submissions", str(s_dir),
        "--out", str(r_dir),
        "--sandbox", "isolate",
        "--parallel", str(GRADE_PARALLEL),
        # students.csv has roll_no + ip columns (plus password_hash/active/
        # bound_at/last_seen, which load_student_mapping just ignores) --
        # grade.py enriches summary.csv's "ip" column from this, same as
        # the offline workflow's ip_student_mapping.csv always has.
        "--student-csv", str(_students_csv(lab_id)),
    ]
    log.info("Finalize: running %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired as e:
        with _finalize_lock:
            _finalize_jobs[lab_id] = {"status": "error", "error": f"Grading run timed out: {e}"}
        return

    if proc.returncode != 0:
        log.error("Finalize failed (exit %d): %s", proc.returncode, proc.stderr[-4000:])
        with _finalize_lock:
            _finalize_jobs[lab_id] = {
                "status": "error",
                "error": "Grading run failed -- see server log.",
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
            }
        return

    run_dir = _latest_run_dir(r_dir)
    if run_dir is None:
        with _finalize_lock:
            _finalize_jobs[lab_id] = {"status": "error", "error": "Grading run completed but no run directory was found."}
        return

    new_state = live_session.mark_finalized(live_dir, run_dir.name)
    with _finalize_lock:
        _finalize_jobs[lab_id] = {
            "status": "done",
            "finalized_run": new_state.finalized_run,
            "stdout": proc.stdout[-4000:],
        }


@app.post("/api/live/<lab_id>/finalize")
def api_finalize(lab_id: str):
    state = live_session.get_state(_live_dir(lab_id))
    if state.status != live_session.STATUS_LOCKED:
        return jsonify(
            {"error": f"Session must be locked before finalizing (is '{state.status}')."}
        ), 400

    with _finalize_lock:
        existing = _finalize_jobs.get(lab_id)
        if existing and existing["status"] == "running":
            return jsonify({"error": "A grading run is already in progress for this lab."}), 409
        _finalize_jobs[lab_id] = {"status": "running"}

    threading.Thread(target=_run_finalize_job, args=(lab_id,), daemon=True).start()
    return jsonify({"status": "running"}), 202


@app.get("/api/live/<lab_id>/finalize/status")
def api_finalize_status(lab_id: str):
    with _finalize_lock:
        job = _finalize_jobs.get(lab_id)
    if job is None:
        return jsonify({"status": "idle"})
    return jsonify(job)


def _latest_run_dir(runs_lab_dir: Path) -> Path | None:
    if not runs_lab_dir.is_dir():
        return None
    candidates = sorted((p for p in runs_lab_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
    return candidates[-1] if candidates else None


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------


@app.get("/live/<lab_id>")
def live_dashboard_page(lab_id: str):
    return render_template("live_dashboard.html", lab_id=_safe_name(lab_id))


def main() -> None:
    global GRADE_PARALLEL, STUDENT_MAPPING
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Admin server for lab_auto_grader's live-lab platform")
    parser.add_argument("--host", default="127.0.0.1", help="admin server should generally stay off the open LAN")
    parser.add_argument("--port", type=int, default=5002)
    parser.add_argument("--grade-parallel", type=int, default=4, help="--parallel passed to grader.grade at Finalize time")
    parser.add_argument(
        "--student-names-csv", default=None,
        help="optional CSV (roll_no,name[,ip]) to show student names in the accounts/live-status "
             "tables -- falls back to $STUDENT_NAMES_CSV, then live/student_names.csv if present",
    )
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    secret_key = os.environ.get("ADMIN_SESSION_SECRET")
    if not secret_key:
        raise SystemExit(
            "ADMIN_SESSION_SECRET is not set -- run 'python -m grader.manage_accounts init-admin' "
            "once to bootstrap .env."
        )
    app.secret_key = secret_key
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    GRADE_PARALLEL = args.grade_parallel

    student_names_csv = args.student_names_csv or os.environ.get("STUDENT_NAMES_CSV")
    if not student_names_csv:
        default_names_path = LIVE_ROOT / "student_names.csv"
        student_names_csv = str(default_names_path) if default_names_path.exists() else None
    try:
        STUDENT_MAPPING = load_student_mapping(Path(student_names_csv) if student_names_csv else None)
    except StudentMappingError as e:
        raise SystemExit(f"--student-names-csv error: {e}")
    if STUDENT_MAPPING.entries:
        log.info("Loaded %d student name(s) from %s", len(STUDENT_MAPPING.entries), student_names_csv)

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
