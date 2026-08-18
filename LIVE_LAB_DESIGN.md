# Live Lab Platform — Design & Implementation Plan

Extends `AUTOGRADER_DESIGN.md`. That doc describes the **offline batch grader**: submissions already sit on disk, `grader.grade` is run once, reports come out. This doc describes turning the same lab session *live*: students write and run C code in-browser on their lab PC against a server running on the instructor's PC, while the lab is in progress, with a hard timer, then a full re-grade at lab-end using the exact existing pipeline, unchanged.

**Guiding principle: reuse, don't rebuild.** Every compile/run/score/report primitive already exists in `grader/*` and is already unit-callable outside the CLI batch loop (confirmed by direct code inspection, not assumption — see call-chain references below). The new work is almost entirely *around* that core: two Flask apps, CSV-backed auth, a session/timer file, and a small sandbox pool for on-demand single-question runs. `grader/grade.py`'s batch pipeline is not modified at all — it becomes the "finalize" step, invoked exactly as it is today.

## 1. Requirements → design mapping

| Requirement | Design element |
|---|---|
| Host on a lab PC, students hit an endpoint from other PCs | `server_student/app.py`, Flask bound to `0.0.0.0:<port>` on the LAN |
| Separate admin UI: configure labs/questions/grading, live dashboard, set timer | `server_admin/app.py` — extends existing `ui/app.py`, adds live-session control routes |
| Handle multiple concurrent student submissions | Bounded pool of `Sandbox` instances (§8), threaded Flask |
| Student login, one device (IP) at a time | `grader/accounts.py` + `students.csv` per lab (§5) |
| Coding-platform-style UI, C only | Student server templates (§9) — question list, editor, run panel |
| Run → compile+execute in isolation on server, compare vs **open** tests, return result | `grader/student_view.py::run_open_tests` (§7–8), reusing `sandbox.py`/`runner.py`/`scorer.py` exactly as the batch grader does |
| Timer ends → lock all code, re-evaluate fully, send marks + leaderboard | `grader/live_session.py` (§6) gates writes; "Finalize" invokes unmodified `grader.grade` (§11) |
| No database; CSV like the existing ip↔student mapping | `students.csv` per lab (§5), same spirit as `extract_submissions.py`'s `ip_student_mapping.csv` |
| Password hash in CSV, reset tool, admin can clear IP to allow new device | `grader/manage_accounts.py` CLI + admin routes wrapping it (§5) |
| Live per-question submission → overwrite that question's `.c` file, same directory structure | Writes land in `submissions/<lab_id>/<roll_no>/<canonical_filename>` — the *existing* layout, untouched (§7) |
| Live run result rendered on student UI, not saved to `report_*.md`; full reports only generated after lab completion | Live run returns JSON, rendered client-side; `report_*.md`/`summary.csv` only written by the unchanged `grade.py` finalize step (§11) |
| Per-question live `.md` in student's submission folder, for admin to eyeball in real time | `submissions/<lab_id>/<roll_no>/<qid>_live.md`, rewritten on every Run (§7) |
| Admin login: username + password hash in `.env`, reset overwrites `.env` | `grader/accounts.py::admin` functions + `manage_accounts.py --reset-admin` (§5) |
| Two separate Python servers, admin vs. student | `server_admin/` and `server_student/`, independent Flask processes/ports (§3) |

## 2. What's reused unchanged vs. what's new

**Reused with zero modification**, confirmed callable exactly as needed:
- `grader/config_schema.py::load_question/load_all_questions` — question definitions don't change.
- `grader/sandbox.py::Sandbox/IsolateSandbox/SubprocessRlimitSandbox/compile_c/run` — `compile_c` and `run` are already independent calls; this is the exact granularity an on-demand endpoint needs.
- `grader/discover.py::ROLL_NO_RE`, `find_question_file` — same roll-number and filename-pattern rules apply to live accounts and live file discovery.
- `grader/scorer.py::get_test_matcher/score_question` and `grader/runner.py::grade_submission` — same 3-call sequence `grade.py` already uses (`get_test_matcher(question)` → `grade_submission(sandbox, question, ...)` → `score_question(result)`).
- `grader/grade.py` CLI, in full, unmodified — this *is* the "lab ends, reevaluate everyone" step.
- `grader/report.py::render_student_report` — already takes in-memory `QuestionScore`s and returns a `str`; directly reusable to render the student's post-lab report page.
- `ui/app.py`'s markdown→HTML rendering pattern and `_safe_name` path-validation regex — reused as-is by the new admin routes for report/dashboard viewing.

**New, small, and additive:**
- `grader/accounts.py`, `grader/live_session.py`, `grader/student_view.py`, `grader/manage_accounts.py`.
- `server_student/`, `server_admin/` Flask apps (the latter can literally `import` and extend `ui/app.py`'s blueprint rather than duplicate it).

**One deliberate, scoped change to an existing invariant:** §2 of `AUTOGRADER_DESIGN.md` states `submissions/` is "input, read-only" and "nothing is ever written back." During a *live* lab, `submissions/<lab_id>/` becomes the live write target — that's the whole point (§1's requirement to preserve the existing directory layout). Once the session locks, it reverts to being effectively read-only (enforced at the app layer, §6), and the finalize step consumes it exactly as the batch grader always has. This is called out explicitly rather than silently contradicting the older doc.

## 3. Two-server topology

```
                    LAN (lab subnet)
┌─────────────────────┐          ┌─────────────────────┐
│  Admin PC (or same   │          │  Student PCs         │
│  machine, diff port) │          │                       │
│                       │          │  browser → :STUDENT_PORT
│  server_admin/app.py  │          └──────────┬────────────┘
│  :ADMIN_PORT          │                     │
│  (question editor +   │                     ▼
│   live control panel) │          server_student/app.py
└──────────┬────────────┘          (login, editor, Run,
           │                        timer, results)
           ▼
  shared filesystem: questions/<lab>, submissions/<lab>,
  live/<lab>/{students.csv, session.json}, runs/<lab>/...
```

Both processes run on the same host (the instructor's lab PC) in the common case, on the existing filesystem tree — no network calls between them, just shared files with `filelock`-guarded writes (§5). Nothing stops running them on two different machines sharing an NFS/SMB mount later, but that's not the initial target.

## 4. Directory layout additions

```
lab_auto_grader/
├── live/                              # NEW — per-lab live-session state, no DB
│   └── lab_01/
│       ├── students.csv               # roll_no,password_hash,ip,bound_at,last_seen
│       └── session.json               # status/start/end time — the "timer"
├── submissions/lab_01/
│   └── 112201023/
│       ├── q1.c                       # written live by server_student on Run/autosave
│       ├── q1_live.md                 # rewritten on every Run — admin-visible intermediate report
│       ├── q2.c
│       └── q2_live.md
├── server_admin/
│   ├── app.py                         # imports ui/app.py's routes + adds live-control routes
│   └── templates/ (dashboard additions)
├── server_student/
│   ├── app.py
│   ├── templates/ (login, question list, editor+run page, results, leaderboard)
│   └── static/ (vendored editor JS/CSS — see §13, no CDN)
├── grader/
│   ├── accounts.py                    # NEW
│   ├── live_session.py                # NEW
│   ├── student_view.py                # NEW
│   └── manage_accounts.py             # NEW — CLI
└── .env                               # NEW — admin creds, gitignored
```

`live/` mirrors the existing "one folder per lab" convention used by `questions/`, `submissions/`, `runs/`. It is **not** committed (add to `.gitignore` alongside `submissions/`) since it holds password hashes and per-student IPs.

## 5. Accounts & auth — no database

### 5.1 `students.csv` (`live/<lab_id>/students.csv`)

```csv
roll_no,ip,active,bound_at,last_seen,locked
112201023,10.0.5.14,1,2026-08-16T09:58:03+05:30,2026-08-16T10:41:12+05:30,
112201024,10.0.5.15,1,2026-08-16T09:59:12+05:30,2026-08-16T10:42:03+05:30,1
```

- `ip` empty until first successful login; then bound.
- `active` signals whether there's currently a live (not-logged-out) session — this, not `ip` alone, enforces "one device at a time" (see step 5 below).
- `locked` — admin-controlled per-student code submission lock. When `1` (true), the student cannot save, run, or autosave code for the rest of the session, though they can still view questions, browse results, and access the leaderboard. Enforced server-side on every `/api/save` and `/api/run` request.
- **Login flow** (`grader/accounts.py::authenticate_student`):
  1. Look up `roll_no` (normalized like `discover.py` does: trim, uppercase, validated against `ROLL_NO_RE`).
  2. Check password against `password_hash` (or default if first login: `default_password(roll_no)`).
  3. If `row.active` is true and `row.ip != request.remote_addr` → reject: `"Already signed in from another device. Ask your instructor to reset your device."`
  4. Otherwise, bind `request.remote_addr` and mark `active=1`, issue a signed session cookie.
- Note: The `locked` field is **not** checked at login — a locked student can still log in, view questions, and browse results; they're just prevented from saving/running code via server-side checks on `/api/save` and `/api/run` (§9).
- Every write to `students.csv` (bind IP on login, admin reset/unbind/lock/unlock, admin bulk-generate) goes through a **cross-process** lock, since both `server_admin` and `server_student` are separate OS processes touching the same file: `filelock.FileLock(csv_path + ".lock")` around read-modify-write. This is the one new dependency needed for correctness (`threading.Lock` isn't enough across two processes).

### 5.2 Admin credentials (`.env`, repo root, gitignored)

```
ADMIN_USERNAME=instructor
ADMIN_PASSWORD_HASH=pbkdf2:sha256:...
ADMIN_SESSION_SECRET=<random, generated once>
STUDENT_SESSION_SECRET=<random, generated once>
```

Loaded via `python-dotenv` (new, tiny dependency) at process start by both servers (student server needs `STUDENT_SESSION_SECRET` for its own Flask session signing key; admin server needs both its own and — only if the admin dashboard needs to inspect student sessions, which it doesn't — so just its own). Two separate secrets so a leaked student-session signing key can't be used to forge an admin session.

### 5.3 `grader/manage_accounts.py` — CLI, works without either web server running

```bash
# bulk-generate accounts for a lab from a roll-number list, one per line,
# random passwords assigned, printed/exported for handout
python -m grader.manage_accounts generate --lab lab_01 --roster roster.txt --out live/lab_01/students.csv

# reset one student's password (prompts for new password, or --password to script it)
python -m grader.manage_accounts reset-student --lab lab_01 --roll 112201023

# clear a student's bound IP so they can log in from a different PC
python -m grader.manage_accounts unbind --lab lab_01 --roll 112201023

# reset the admin password in .env
python -m grader.manage_accounts reset-admin
```

Lock/unlock are exposed only through the admin web UI (§10 accounts tab) — no CLI equivalent, since they're intended for real-time mid-session instructor action (misbehaving/disruptive student).

This satisfies the requirement verbatim ("a program on server which on run can be used to reset password") and is also what the admin web UI's reset/unbind buttons call internally — one implementation, two entry points (CLI + HTTP route), avoiding logic duplication.

**Unbinding preserves all submissions**: since live code is stored by `roll_no` (`submissions/<lab>/<roll_no>/...`), never by IP, clearing the `ip` column has zero effect on any code the student has already saved/run — exactly as the user specified.

## 6. Live session / timer state (`live/<lab_id>/session.json`)

```json
{
  "status": "not_started",
  "start_time": null,
  "duration_minutes": null,
  "end_time": null,
  "locked_at": null,
  "finalized_run": null
}
```

`status` ∈ `not_started → running → locked → finalized`. `grader/live_session.py` exposes:

```python
def start(lab_id: str, duration_minutes: int) -> None: ...      # admin "Start lab"
def status(lab_id: str) -> SessionStatus: ...                    # includes time_remaining_seconds
def is_write_allowed(lab_id: str) -> bool: ...                   # status == "running" AND now < end_time
def lock(lab_id: str) -> None: ...                                # admin "End lab now", or auto-called on expiry check
def mark_finalized(lab_id: str, run_dir_name: str) -> None: ...  # after grade.py completes + is published
```

**Authoritative enforcement, not trust-the-client**: `server_student`'s `/api/run` and `/api/save` endpoints call `is_write_allowed(lab_id)` themselves on every request (re-checking wall-clock time against `end_time`, not a cached flag) before touching the filesystem. This closes the obvious race where a client's countdown display lags the real deadline. The UI countdown is purely cosmetic; the server clock is what matters.

Writes are guarded by the same `filelock` used for `students.csv` (a per-lab lock file), since both `server_admin` (start/lock) and `server_student` (read-only checks, high frequency) touch `session.json`.

## 7. Student-safe question view & the live-run codepath

### 7.1 Never let hidden tests reach the student process

Research finding: `scorer.QuestionScore.hidden_summary` currently carries full `input`/`expected`/`actual` detail regardless of group — the doc's stated "hidden tests should stay hidden" intent isn't actually enforced by `scorer.py`; redaction only happens (accidentally, by construction) in `report.py`'s markdown rendering. **The live-run codepath must not rely on that redaction layer at all.** Instead, it must ensure hidden `TestCase`s are never even loaded into the `Question` object that reaches `grade_submission`:

```python
# grader/student_view.py
def open_only(question: Question) -> Question:
    return dataclasses.replace(question, tests=question.tests_in("open"))

def run_open_tests(sandbox: Sandbox, question: Question, roll_no: str, source_text: str,
                    lab_dir: Path) -> LiveRunResult:
    q_open = open_only(question)
    source_path = lab_dir / roll_no / q_open.filename_patterns[0]   # canonical write target
    source_path.write_text(source_text)
    matcher = get_test_matcher(q_open)
    result = grade_submission(sandbox, q_open, source_path, binary_dest=..., matcher_fn=matcher)
    # result.outcomes only ever contains open TestOutcomes — hidden data was never in scope
    write_live_report_md(lab_dir / roll_no / f"{q_open.qid}_live.md", result)
    return LiveRunResult.from_question_result(result)
```

This is a stronger guarantee than filtering the response after the fact — the hidden tests are structurally absent from the `Question` passed down the whole call chain, so there's no dict/field anywhere in this request's lifecycle that could leak them, even by a future bug in `scorer.py`.

`grader.code_checks` (construct require/forbid feedback) is **not** hidden-test content — it's feedback about the student's own source, already implied by the question's stated constraints — so it's shown in full on every Run. This is deliberately useful: the pedagogical goal ("students keep losing marks to silly output mistakes") is served exactly by surfacing `pass_reasons` (`"PASS (case not matched)"`, `"PASS (spelling not matched)"`) on live Runs — the same tolerance-naming machinery `scorer.py` already has for the final report, now visible to the student *while they can still fix it*.

### 7.2 Canonical write target

`question.filename_patterns[0]` is treated as the canonical filename for live writes (e.g. `q1.c`), not whatever a student might have historically named a file offline — the live workflow has no ambiguity to resolve (`discover.py`'s multi-candidate logic is for messy *offline uploads*; it's simply not needed here since the server itself is the only writer).

### 7.3 `<qid>_live.md` — admin-visible intermediate report

Rewritten (not appended) on every Run, using the *existing* single-question rendering building blocks in `report.py` (the per-question section of `render_student_report`, factored out if not already a standalone function) — same visual format the student and the admin dashboard both already understand, just scoped to one question and refreshed live instead of produced once at the end. Placed directly inside `submissions/<lab_id>/<roll_no>/` per the user's explicit ask ("in his submission folder") — this is safe because `discover.py::find_question_file` only matches the specific filenames in `filename_patterns` (e.g. `q1.c`), never a wildcard, so a sibling `.md` file is invisible to file discovery and can't be mistaken for a submission.

## 8. On-demand sandbox pool

`grade.py`'s batch pool statically assigns one `box_id`/`Sandbox` per worker process for the whole run — a good fit for a fixed batch, a bad fit for "any of 35 students may click Run at any moment." `IsolateSandbox` is confirmed **not** safe for concurrent use on the same `box_id` (both `compile_c` and `run` each do their own `_init_box`/`_cleanup_box` under that id) — so concurrency has to come from *multiple box_ids*, not from sharing one.

```python
# grader/sandbox_pool.py
class SandboxPool:
    def __init__(self, kind: str, size: int, scratch_root: Path):
        self._queue: queue.Queue[Sandbox] = queue.Queue()
        for box_id in range(size):
            self._queue.put(_make_sandbox(kind, box_id, scratch_root))

    @contextmanager
    def checkout(self, timeout: float = 15.0):
        sandbox = self._queue.get(timeout=timeout)   # blocks/raises Full→"server busy" if none free
        try:
            yield sandbox
        finally:
            self._queue.put(sandbox)
```

- One `SandboxPool` per `server_student` process, size configurable (`--sandbox-slots`, default e.g. 4–6, tuned to the lab PC's core count — each slot is roughly "one student's Run happening right now").
- Flask runs with `threaded=True`; each request thread blocks on `subprocess.run` inside `isolate`, which releases the GIL for the wait — a `ThreadPoolExecutor`-free design works fine here because the concurrency unit (an isolate box) is already OS-process-level, not Python-thread-level.
- `checkout(timeout=...)` turning a full queue into a clear `"Server is busy — try again in a moment"` response (HTTP 503) rather than an unbounded queue of blocked requests is the difference between graceful degradation and a stuck lab PC under Run-button-mashing.
- `server_admin`'s "Finalize & Grade" step is only ever enabled once the session is `locked` (§10), i.e. after student Runs have stopped — so it's safe for it to invoke `grade.py`'s own independent `multiprocessing.Pool` (box_ids starting at 1, per its existing convention) without colliding with `server_student`'s pool, as long as the two aren't literally executing at the same moment. This is enforced by the UI flow, not by box-id partitioning — worth a one-line startup check in `manage_accounts.py`/finalize route that refuses to start if `server_student`'s pool still shows in-flight Runs (a simple in-memory counter is enough; no correctness-critical shared state needed since this is a single-instructor-triggered action).

## 9. Student server — routes & UI flow

| Route | Method | Behavior |
|---|---|---|
| `/login` | GET/POST | roll_no + password → `accounts.authenticate` → session cookie |
| `/logout` | POST | clear session (does **not** unbind IP — that's an admin-only action) |
| `/` | GET | if session not `running`: waiting/locked/finalized landing page; else question list |
| `/api/questions` | GET | list of this lab's questions via student-safe projection (title, description, filename, open-test count — no marks breakdown of hidden tests, no gold path) |
| `/api/questions/<qid>` | GET | full student view: description + this student's currently-saved source (if any) for the editor |
| `/api/session/status` | GET | polled on heartbeat: returns session state, timer countdown, and `student_locked` flag (true if this account is admin-locked) |
| `/api/save` | POST | body `{qid, source}` → check `student_locked` (reject 423 if true) → `is_write_allowed` check → write `.c` file only, no compile/run (autosave path) |
| `/api/run` | POST | body `{qid, source}` → check `student_locked` (reject 423 if true) → `is_write_allowed` check → `sandbox_pool.checkout()` → `student_view.run_open_tests` → JSON: compile result, per-open-test pass/fail + `pass_reasons` + input/expected/actual, code_checks feedback |
| `/api/results` | GET | only once `finalized`: this student's full report, rendered from `runs/<lab>/<finalized_run>/report_<roll>.md` via the existing markdown→HTML pattern from `ui/app.py` |
| `/api/leaderboard` | GET | only once `finalized`: parsed `summary.csv`, sorted by total, roll numbers only (no names, matching the CSV's own schema — no PII beyond what's already in the roster) |

**Editor page** (`templates/editor.html` + vendored JS, §13): left sidebar = question list with a live per-question status chip (not attempted / compiled+open-tests-passing count); main panel = question description, code editor, Run button, results panel below showing per-test outcomes in the same visual vocabulary as `report.py` already uses (`PASS`, `PASS (case not matched)`, `FAIL (WRONG_ANSWER)`, `FAIL (TIMEOUT: ...)`).

**Timer & notifications**:
- On-screen countdown ticks locally every second (via `tickTimer()`); server-authoritative time is polled every heartbeat (~60s) to stay synchronized.
- When 5 minutes remain (`timeRemainingSeconds === 300`), a modal alert notifies the student: "Session ending in 5 minutes. You will not be able to submit code once the timer reaches 0:00. Finish your work and run your code now."
- When session transitions to `locked` (timeout or admin-triggered), code editor becomes read-only, Run button is disabled, and save status shows "Code has been locked and is being graded."

**Student lock (mid-session instructor action)**:
- If `student_locked` becomes true (admin locks this student via the accounts tab), on the next heartbeat poll the student receives `student_locked: true` and sees an alert: "Your account has been locked. You cannot save or run code. You can still view questions and results. Contact your instructor if you have questions."
- When locked, the Run button is disabled, the code editor is read-only (no edits accepted), and the status line reads "Account locked — cannot submit." Any subsequent `/api/save` or `/api/run` requests are rejected server-side with 423 status.
- If the instructor unlocks the student, the next heartbeat brings `student_locked: false` and triggers an unlock alert; the Run button and editor are re-enabled.
- A locked student can still log in, view questions, browse saved code and previous run results, and see the leaderboard — just not modify/run code.

**Autosave beyond the literal ask**: the user's design has the `.c` file overwritten only on Run-click. Recommend also autosaving on a debounced interval (~60s) and on `beforeunload`/tab-switch via `navigator.sendBeacon('/api/save', ...)` — pure risk mitigation against a student who writes code but never clicks Run before the timer expires, losing everything. Flagging this as a recommended addition, not a requirement change — happy to drop it if you'd rather keep the write path strictly Run-triggered.

## 10. Admin server — routes & UI flow

Built by extending `ui/app.py` rather than duplicating its question-editor routes — `server_admin/app.py` imports that Blueprint as-is (labs/questions CRUD, the existing dashboard/result-viewing routes) and adds:

| Route | Method | Behavior |
|---|---|---|
| `/admin/login` | GET/POST | username + password → `accounts.authenticate_admin` (checks against `.env`) |
| `/api/live/<lab>/accounts` | GET | list all students: roll_no, name, password status, device/IP, last seen, **locked status**, with Reset password / Sign out device / **Lock/Unlock** buttons |
| `/api/live/<lab>/accounts/<roll>/reset` | POST | reset one student's password |
| `/api/live/<lab>/accounts/<roll>/unbind` | POST | clear bound IP (logout + allow new device) |
| `/api/live/<lab>/accounts/<roll>/lock` | POST | mark this student's `locked=1` in students.csv; prevents save/run for this student until unlocked |
| `/api/live/<lab>/accounts/<roll>/unlock` | POST | clear `locked` flag; student can save/run again |
| `/api/live/<lab>/session/start` | POST | body `{duration_minutes}` → `live_session.start` |
| `/api/live/<lab>/session/lock` | POST | force-end early |
| `/api/live/<lab>/session/status` | GET | polled by the live dashboard |
| `/api/live/<lab>/dashboard` | GET | per-student, per-question: logged in? IP bound? last Run timestamp + open-test pass count (read from each `<qid>_live.md`, or a small in-memory cache populated by `server_student` — see note below) |
| `/api/live/<lab>/finalize` | POST | invokes `python -m grader.grade --questions questions/<lab> --submissions submissions/<lab> --out runs/<lab> ...` (subprocess or in-process `grade.main()` call), then `live_session.mark_finalized` |

**Accounts tab**: displays all students with login/device/last-seen info and a "locked" column (🔒 Locked / 🔓 Unlocked). Each row has action buttons: Reset password (sets temporary password, admin shares directly with student), Sign out device (unbind + allow new device), and Lock/Unlock (real-time mid-session toggle for disruptive students). Lock changes are visible to the student on the very next heartbeat poll.

**Dashboard data source**: the admin server can read `<qid>_live.md` files directly off disk (simple, zero coupling to `server_student`'s internals, matches the user's own stated design) — polled every few seconds by the dashboard page's JS, same pattern `ui/app.py` already uses for its existing dashboard route. No IPC between the two servers is needed for this.

**Finalize is a one-way gate**: only enabled once `session.status == "locked"`; running it flips status to `finalized` and records which `runs/<lab>/<timestamp>` directory is the published one. This is the *exact* existing `grade.py` invocation from `AUTOGRADER_DESIGN.md` §9 — no new grading logic, just a button that shells out to it and then tells the student server (by writing `finalized_run` into `session.json`, which `server_student` polls) that results are ready.

## 11. Finalization, reports, and leaderboard

Nothing new to build here beyond the trigger and the read-back:
- `grade.py` runs exactly as documented in §7/§9 of `AUTOGRADER_DESIGN.md` — same gold self-check gate, same `report_<roll>.md`/`summary.csv`/`run.log`/`grade_feedback.md` outputs, same anomaly handling for a student who never submitted a question.
- `server_student`'s `/api/results` reads `runs/<lab>/<finalized_run>/report_<roll_no>.md` back and renders it, reusing `ui/app.py`'s markdown→HTML conversion verbatim (`ui/app.py:602-687`) rather than reinventing report rendering — this was already the established pattern for "score object → readable page" in this codebase.
- Leaderboard is a straight parse of `summary.csv`'s `total` column, sorted descending, roll numbers as the identity (no name column is required by the CSV schema `discover.py::StudentMapping` — names are optional/best-effort there already).

## 12. Concurrency, capacity & rate-limiting

- `SandboxPool` size caps true concurrent compiles/runs; excess requests get a clear 503, not silent queuing that could make the UI look hung.
- A light per-student cooldown (e.g. 2–3s between `/api/run` calls, enforced server-side) prevents one student button-mashing from starving the pool for everyone else.
- `--check-gold`-equivalent startup self-test: `server_student` runs `isolate_self_test()` (already exists, `sandbox.py`) at boot and refuses to serve `/api/run` (falls back to `SubprocessRlimitSandbox`, logged loudly) if `isolate` isn't actually working — mirrors the existing design's "self-test at startup, never silently limp along" principle from `AUTOGRADER_DESIGN.md` §5.2.

## 13. Security & known limitations

- **IP-based single-device enforcement is a LAN heuristic, not a strong guarantee.** It assumes lab PCs have stable per-machine IPs (true for most managed lab networks with static/reserved DHCP leases) — flagging this explicitly rather than overselling it. If the lab network uses aggressive DHCP churn or NAT, two students could transiently collide or one student could get locked out by an IP change; the `unbind` admin action is the designed escape hatch for exactly this.
- **No CDN — vendor the editor locally.** Lab PCs are commonly offline/LAN-only; a code editor widget (e.g. CodeMirror) must be vendored into `server_student/static/` at build time, not pulled from a CDN at runtime, or the editor page will simply fail to load on an air-gapped lab network. This is a hard requirement, not a nice-to-have, given the deployment target.
- **Session cookies, not bearer tokens** — simplest fit for a browser-only client, signed with `STUDENT_SESSION_SECRET`/`ADMIN_SESSION_SECRET` from `.env`.
- **Every write endpoint re-validates roll_no from the session, never from the request body** — a student's own `qid`/`source` payload is trusted, but *which folder it writes to* is always derived server-side from the authenticated session, never from client input, closing any path-traversal-via-roll-number angle. Same `_safe_name`-style validation as `ui/app.py` applies to `qid` before it touches a path.
- **TOCTOU at the lock boundary**: `is_write_allowed` is re-checked at the moment of the write itself (not cached from an earlier request), so the only exploitable window is normal network latency between "student clicks Run at 89:59.9" and the server receiving it — acceptable and unavoidable in any such system, called out rather than pretended away.
- **`.env` and `live/*/students.csv` must never be committed** — add both to `.gitignore` alongside the existing `submissions/` exclusion, since they hold credentials.

## 14. New/changed files summary

```
NEW   grader/accounts.py
NEW   grader/live_session.py
NEW   grader/student_view.py
NEW   grader/sandbox_pool.py
NEW   grader/manage_accounts.py
NEW   server_student/app.py + templates/ + static/ (vendored editor)
NEW   server_admin/app.py + templates/ (live dashboard additions)
NEW   .env (gitignored)
NEW   live/ (gitignored, created per-lab at runtime)
MOD   .gitignore — add .env, live/
MOD   requirements.txt — see §15
UNCHANGED  grader/grade.py, runner.py, sandbox.py, scorer.py, config_schema.py, discover.py, report.py, code_checks*.py, ui/app.py (imported, not edited)
```

## 15. `requirements.txt` additions

```
filelock>=3.15       # cross-process locking for students.csv / session.json, shared by two server processes
python-dotenv>=1.0    # load .env admin credentials
```

Nothing else — password hashing uses Flask's existing transitive `werkzeug` dependency; no task-queue library needed given the sandbox-pool design in §8; no new web framework (stays Flask, per the existing codebase).

## 16. Implementation milestones

1. **Foundations** — `accounts.py` (CSV auth + `filelock`), `live_session.py`, `manage_accounts.py` CLI, `.env` template + `.gitignore` updates. Testable standalone with no Flask involved: generate accounts, log in via a Python REPL call, reset/unbind, start/lock a session, all via the CLI/module functions directly.
2. **Live-run core** — `student_view.py::open_only/run_open_tests`, `sandbox_pool.py`. Testable via a script that simulates one student's Run against a real question in `questions/lab_01` and confirms hidden tests never load and the `.c`/`_live.md` files land in the right place.
3. **`server_student`** — login/session/IP-binding, question list + editor (with a vendored editor widget), Run/Save wired to milestone 2, timer polling + lock enforcement, `_live.md` writes.
4. **`server_admin`** — extend `ui/app.py`, add account generation, session start/lock, live dashboard (reading `_live.md` files), and the Finalize trigger (subprocess call into unmodified `grade.py`).
5. **Results & leaderboard** — `/api/results` and `/api/leaderboard` on the student server, reusing `ui/app.py`'s markdown rendering.
6. **Hardening** — startup `isolate_self_test`, per-student Run cooldown, 503-on-pool-exhaustion, autosave-on-blur (§9's recommended addition), logging parity with `run.log`'s existing spirit for the live phase.

## 17. Testing plan

- Multi-browser concurrent login: same roll_no from two different PCs → second is rejected; same roll_no, same PC, two tabs → both work (same IP).
- IP unbind: bind on PC A, admin unbinds, login succeeds on PC B, all of that student's already-saved `.c` files are untouched.
- Timer expiry mid-Run: kick off a Run just before `end_time`, confirm the server-side check (not client display) is what gates it.
- Sandbox-pool exhaustion: fire more concurrent Runs than pool size, confirm graceful 503s, no stuck requests, no isolate box leakage (same `ps`/`/var/local/lib/isolate/` check `AUTOGRADER_DESIGN.md` §12 already uses for the batch grader).
- Reuse the existing adversarial fixtures from `AUTOGRADER_DESIGN.md` §12 (infinite loop, fork bomb) through the *live* Run path specifically, not just the batch path — confirming the same clean `TIMEOUT`/containment behavior holds when triggered via HTTP instead of the CLI.
- End-to-end: start session → simulated students log in, save/Run a few questions (including one with a deliberately wrong-case output, to confirm `pass_reasons` surfaces it) → lock → Finalize → confirm `runs/<lab>/<ts>/` looks identical in shape to an existing offline batch run → confirm `/api/results` and `/api/leaderboard` render correctly.

## 18. Decisions (resolved) and implementation status

Resolved: sandbox pool defaults to 4 slots (`--sandbox-slots`, tunable per lab PC); editor widget is CodeMirror 5, vendored locally under `server_student/static/vendor/codemirror/` (no CDN, per §13); autosave is on, configurable via `--autosave-interval-seconds` (default 90s), in addition to always saving on Run and on tab-switch/close.

**Milestones 1–3 (§16) are implemented and verified end-to-end**, including a full curl-driven run through a live student server against real `lab_01` data: login → wrong-device rejection → same-device re-login → password reset → device unbind → session start/timer → questions gated until the session starts → save → Run (confirmed hidden tests never appear in the response, and confirmed the `PASS (case not matched)` tolerance-naming feedback the whole platform exists for) → Run-cooldown rejection (429) → forced timer expiry → auto-lock → write rejected after lock (423) → **unmodified `grader.grade` batch run** against the live-populated `submissions/lab_01/` → `mark_finalized` → `/api/results` and `/api/leaderboard` both render correctly from the batch run's real output.

New/changed files beyond §14's original list, discovered while implementing:
- `grader/sandbox.py`: added `make_sandbox` (factored out of `grade.py`'s private `_make_sandbox`, now shared with `sandbox_pool.py`) and `choose_sandbox_kind` (factored out of `grade.py`'s private `_choose_sandbox_kind`, now shared with `server_student`) — `grade.py` itself is behaviorally unchanged, confirmed via a `--check-gold` regression run before and after.
- `grader/report.py`: extracted `_render_question_section` out of `render_student_report`'s loop body (confirmed equivalent output) so `render_live_question_report` (new) can reuse it for the per-question live `.md`, with a `show_hidden` flag so a live, open-tests-only run's report never shows a misleading `Hidden tests: 0/0` line. Also absorbed `ui/app.py`'s markdown-rendering post-processing (`_render_io_side_by_side`, `_preserve_inline_code_whitespace`) as public `render_io_side_by_side`/`preserve_inline_code_whitespace`/`render_markdown_to_html` — `ui/app.py` now calls the shared version instead of its own private copies (verified: fewer lines, identical HTML output).
- `server_student/app.py` additionally exposes `/api/config` (autosave interval) and `/api/whoami`, and a per-student in-memory `LAST_RUN_SUMMARY` (not persisted — lost on restart by design, since it's only a UI convenience for the question-list chips; the source of truth for marks is always the finalize step's fresh grading pass) alongside a per-student Run cooldown (`RUN_COOLDOWN_SECONDS = 2.0`) not explicitly spec'd in §12 but implied by it.

**Also verified in real use** (not just automated checks): the user ran a full 60-minute live session end-to-end in an actual browser against real `lab_01` data — login, editing/running across multiple questions, and, critically, confirmed that once the timer actually expired, code could no longer be edited/saved/run, closing the loop on the one behavior that's hardest to fake in an isolated test (real wall-clock expiry under real usage, not a monkeypatched `end_time`).

**Post-milestone-3 UI iteration**, each a real gap surfaced by actual usage, not anticipated up front:
- Live-run output wasn't retained when switching questions and back — fixed by moving from a small in-memory chip summary (`LAST_RUN_SUMMARY`) to a full server-side `LAST_RUN` cache keyed by `(roll_no, qid)`, storing both the result and the exact source that produced it, so a switch-away-and-back (or even a page reload) restores the full results panel with no re-run.
- "Code changed after a run but not re-run" had no visual indicator — added a stale flag, computed authoritatively server-side (source-that-was-run vs. currently-saved source) and mirrored live client-side the instant the editor changes (filtered on CodeMirror's `changeObj.origin` so `setValue()` during a question switch doesn't falsely trigger it).
- A real bug in the first stale-indicator implementation: it patched the sidebar chip's DOM directly, which `renderQuestionList()` silently discarded on the next render (it rebuilds the whole sidebar from the `questions` array, not from the DOM) — switching away from an edited-but-unrun question and back showed green again. Fixed by mutating the `questions` array itself instead of the DOM, so the "stale" state lives in the same data model every render reads from.
- A code-checks violation (tests passing byte-for-byte via a forbidden/missing construct) looked identical to a clean pass (green) — now shown orange in both the run panel and the sidebar chip, on the reasoning that "tests passed via the wrong construct" is still a "look again" state pedagogically, even though marks-wise it's whatever `gate`/`bonus`/`penalty` says.
- Status polling was hardcoded to 5s, projected to be real load at 30–35 concurrent students — merged into a single 60s `heartbeat()` that does the status poll *and* an opportunistic autosave-if-dirty in one request (renamed `--autosave-interval-seconds` → `--heartbeat-interval-seconds` accordingly), with a local 1-second `tickTimer()` keeping the on-screen countdown smooth between heartbeats and triggering one immediate resync the moment it locally hits zero (rather than waiting up to a full heartbeat interval for the lock to visibly take effect).
- The save/run status line next to the Run button was ephemeral (reset on reload/question-switch) — made persistent by sourcing it from the server: `saved_at` is the source file's own mtime (no separate timestamp to keep in sync), `run_at` is the stored run record's own timestamp, both returned by `/api/save`/`/api/run`/`/api/questions/<qid>` and rendered as "Saved 2:15 PM · Last run 2:14 PM".
- A real gap found while building the above: a signed session cookie has no server-side revocation on its own, so an admin's "unbind device" only affected the *next* login — an already-open browser kept working indefinitely. Fixed by having `_current_roll_no()` re-check the account's currently-bound IP in `students.csv` on every single request (not just at login), so an unbind (or a second device claiming the account) takes effect within one heartbeat; the frontend's `api()` helper now treats any 401 as "force back to `/login?reason=device_changed`" globally.

**`server_admin` (§10, milestone 4) is now built and verified**, including a real subprocess Finalize run (not mocked) against an isolated lab copy:
- `ui/app.py` was converted from a standalone `Flask(__name__)` app into a `Blueprint` (`bp`), with `static_url_path="/static"` set explicitly so it serves at the same URL whether run standalone (`python -m ui.app`, unchanged, still unauthenticated/local-only) or mounted inside `server_admin` behind login. Verified byte-for-byte route/asset parity between the two modes via Flask's test client.
- `server_admin/app.py` registers that blueprint, adds its own `/admin-static`-namespaced static assets (avoiding any collision with `ui`'s `/static`), and gates *everything* — including every route `ui`'s blueprint contributes — behind a single `before_request` admin-session check, with `/admin/login` and `/admin-static/*` as the only public exceptions.
- Accounts, session control (start/extend/lock/reset), and Finalize all wrap the same `grader.accounts`/`grader.live_session` functions the CLI (`grader.manage_accounts`) already used — no parallel implementation, confirmed by the fact that the admin UI's "reset password" button and `manage_accounts.py reset-student` produce byte-identical `students.csv` writes.
- The live-status dashboard reads a small JSON sidecar (`{qid}_live.json`) that `server_student` now writes next to its existing markdown one on every Run — deliberately not shared memory or an IPC channel between the two processes, just files, matching every other cross-process interaction in this design. Verified the dashboard correctly shows `null` for a question a student hasn't attempted yet and reflects `code_check_satisfied: false` distinctly from a clean pass.
- Finalize was verified against a real (not mocked) `subprocess.run(["python", "-m", "grader.grade", ...])` call: locked a session, ran it, and confirmed the resulting `runs/<lab>/<ts>/summary.csv` correctly gated a gate-violating submission to 0 marks — the same outcome the batch CLI has always produced, now reachable from a button instead of a terminal.
- A small addition to the earlier design: the "Live Session" entry point is a plain link added next to each lab's existing "Delete" button in the question-editor's lab tree (`ui/static/app.js`), rather than a new tab bolted into that file's existing single-page app structure — lower-risk than restructuring an already-working 1000+ line UI, and `/live/<lab_id>` is a fully separate page/template/JS bundle.

**Adversarial and concurrency safety, verified specifically through the live on-demand Run path** (§17's testing-plan items, not previously exercised — the batch grader's own adversarial coverage in `AUTOGRADER_DESIGN.md` §12 says nothing about this newer code path):
- An infinite-loop and a fork-bomb submission, run through the exact `SandboxPool` + `student_view.run_open_tests` call chain `server_student`'s `/api/run` uses, both terminated cleanly via `TIMEOUT` on every test — no hang, and (checked immediately after, same method `AUTOGRADER_DESIGN.md` §12 uses) no leftover `a.out` processes or stray `/var/local/lib/isolate/` box directories.
- Two genuinely concurrent Run requests (two threads, each holding a pool slot for the same ~4.5s) were confirmed to use two *distinct* `box_id`s and to actually overlap in wall-clock time (finishing in ~1x a single run's duration, not ~2x) — empirical confirmation that `SandboxPool` delivers real concurrency rather than accidentally serializing on a shared resource.
- With pool size forced to 1, a second concurrent checkout attempt correctly raised `PoolExhausted` while the first was still holding the only slot (the exact exception `server_student`'s `/api/run` maps to a 503) rather than blocking indefinitely or silently corrupting the in-flight run.

Not yet done from §17's testing plan: a full ~30-student concurrent-load simulation (the checks above cover 2-way concurrency and correctness, not steady-state throughput at realistic class size).

## 19. Post-launch UI/UX iteration, from real classroom use

A further round of fixes and features, each verified end-to-end (isolated lab copies + real Flask test-client runs, never against the user's own live data):

- **Sign-out styling**: both apps' `.secondary` button (shared class name, coincidentally identical bug in both) had no explicit text color, so a light background swallowed white-on-white text. Now explicitly red/white in both.
- **Admin dashboard layout**: the tab bar's text was invisible for the same reason (a generic `button { color: white }` rule leaking through), and `.admin-main` was centered with a narrow `max-width`, leaving a wide dead zone that read as an accidental sidebar. Both fixed.
- **Bound device vs. currently active, properly separated**: `students.csv` gained an `active` column, distinct from the pre-existing `ip`. `ip` is now a *persistent* "last device used" record (survives logout, needed for report enrichment below); `active` is the real online/offline signal, cleared on logout or admin unbind. `authenticate_student`'s one-device rule now checks `active`, not raw `ip` — meaning a student who properly signs out frees that seat for *anyone* (themselves from elsewhere, or a different student on the same physical PC), which is what "one device at a time" should have meant all along; only a still-*active* session on a different device blocks a new login. `_current_roll_no()` was updated to force-logout on `active` going false too, not just on `ip` mismatch, since unbind no longer touches `ip`.
- **`summary.csv` IP enrichment**: Finalize's `grader.grade` subprocess call now passes `--student-csv live/<lab>/students.csv` (the file already has `roll_no`/`ip` columns; the extra columns are just ignored by the existing enrichment code) — verified the IP shows up in the report even for a student who has since logged out, precisely because `ip` is no longer cleared by logout.
- **Leaderboard highlight**: the viewing student's own row gets a distinct style, matched against the roll number already embedded in the page via a data attribute.
- **Shared password identity across labs**: `live/accounts.csv` (one file, not per-lab) now holds `roll_no -> password_hash` as the durable identity. `generate_accounts` reuses an existing global hash for a roll number instead of minting a new one — a returning student keeps the same password lab after lab, and the admin UI now reports "N new, M carried over" instead of reissuing everyone's password every time. `reset_student_password` writes through to the global file too, so a reset is picked up by future labs — but deliberately does *not* retroactively touch any other already-generated lab's own `students.csv` (verified: resetting in lab_B leaves lab_A's file, and lab_A's login behavior, untouched). Per-lab `students.csv` keeps its own `password_hash` column (synced at generate/reset time) so `authenticate_student`'s hot path needed zero changes.
- **Student names, fully optional**: both servers gained `--student-names-csv` (falling back to `$STUDENT_NAMES_CSV`, then `live/student_names.csv` if present), reusing `grader.discover.load_student_mapping` (already built for exactly this roll_no/name/ip shape) rather than writing a second parser. Shown in the student header, the leaderboard, and both admin tables — nowhere else changed, and everything still works identically with no such file present (roll numbers only, as before).
- **Post-session content no longer disappears**: this was the biggest behavioral change. Previously, reaching `finalized` status unconditionally swapped the workspace view (questions, saved code, last-run results) for the results view, with no way back. Now there's a small top-level nav (`My work` / `My results`) shown once finalized, and *three* independent server-side toggles — `--hide-workspace-after-session`, `--hide-report`, `--hide-leaderboard` (all default to showing, matching what was asked for) — gate this both client-side (which nav/tabs appear) and server-side (`/api/questions`, `/api/results`, `/api/leaderboard` all 403 if their toggle is off), so a curious student can't just curl a disabled endpoint. The workspace's "My work" view needed no new data plumbing at all — it was already reading from the same `LAST_RUN` cache and `submissions/` files built for the mid-session persistent-results feature (§18), just no longer hidden away after finalize.
- **Non-blocking Finalize**: the POST now starts a background thread and returns `202` immediately; a new `GET /finalize/status` endpoint reports `running`/`done`/`error`, polled by the admin UI every 3s (same polling pattern already used for session status and live dashboard). Verified with a real subprocess grading run completing asynchronously while the initiating request had already returned.

## 20. Second UI iteration: real peer tabs, runtime-editable display config

- **Post-session nav restructured to three flat peers**: "My work" / "My results" (nested report+leaderboard sub-tabs underneath) turned out to be the wrong shape -- the user wanted "My work" / "My report" / "Leaderboard" as three equal top-level views sharing one content area, each independently hideable, not a two-level tab hierarchy. `results-view` was split into separate `report-view`/`leaderboard-view`/`nothing-view` `<main>` elements (the last one for the rare case where an admin disables all three), and `showView()` now switches between five exclusive states instead of three. Caught and fixed a real bug introduced mid-refactor: an old `.post-nav-btn` click handler (from the first version) was never removed when the correct one was added, so both fired on every click -- `showView` got called twice per click, the second time async-ordered after `ensureViewLoaded`. Found by re-reading the file end to end before shipping, not by a test (the double-call was mostly harmless in practice, but wrong).
- **Leaderboard columns**: name and roll number were combined into one cell (`"Asha Kumar (112201023)"`) -- split into two separate `<td>`s per the user's explicit ask.
- **Display-config toggles are now runtime-editable by the admin, not just CLI flags**: this needed real cross-process shared state, since `server_admin` (which sets the toggles) and `server_student` (which enforces them) don't share memory. New `grader/display_config.py` -- `live/<lab>/display_config.json`, same filelock-protected read/write pattern as `live_session.py`. `server_student`'s `create_app` now calls `display_config.ensure_initialized(...)` (seeds the file from CLI args only if nothing exists yet -- never clobbers an admin's already-made runtime choice on a restart), and every gate (`/api/config`, `/api/questions`, `/api/results`, `/api/leaderboard`) reads `display_config.get_config(LIVE_DIR)` fresh on each request instead of a static module global. `server_admin` gained `GET`/`POST /api/live/<lab>/display-config` and three checkboxes in the Session tab. Client-side, `/api/config` is now re-fetched every heartbeat (not just once at page load) specifically so a runtime change is picked up within one interval; `flushDisabledContent()` clears already-rendered report/leaderboard/workspace content the moment its toggle flips off (and resets its "loaded" flag so a later re-enable triggers a real re-fetch rather than showing nothing). Verified the whole loop end-to-end at the HTTP level: admin POSTs a change, the student's very next request (simulating the next heartbeat) reflects it, both in `/api/config`'s response and in the gated endpoints actually 403ing/succeeding accordingly.
- **Two follow-up UI fixes**: (1) switching between "My work" / "My report" / "Leaderboard" wasn't hiding the previous view -- root cause was a real CSS gotcha, not a JS bug: any author rule that sets `display` on an element (`#workspace-view { display: flex; }`, `.post-session-nav { display: flex; }`) unconditionally beats the browser's own `[hidden] { display: none }` rule, *regardless of selector specificity*, because origin/importance is sorted before specificity in the cascade. Fixed with explicit `#id[hidden] { display: none }` overrides for every top-level view. Also fixed the default/fallback priority to workspace → report → leaderboard → "session ended, results coming soon", per spec. (2) the four checkboxes (3 display-config, 1 in the now-removed generate-accounts section) had checkbox-above-text stacking, caused by a general `.form-row label { flex-direction: column }` rule meant for a different label shape ("Duration (minutes)" over a number input) leaking onto checkbox labels too. Fixed with a more-specific `.form-row label.checkbox-label { flex-direction: row }`. One real lesson from this exchange: a fix that only edits an HTML *template* (as opposed to a static JS/CSS file) needs a restart of that Flask process, not just a browser refresh -- `debug=False` (correct for a real server) disables Jinja's template auto-reload, so a template change stays invisible until the process restarts even though the request-response cycle looks identical to a static-asset change from the browser's side.

## 21. Password model rework: deterministic self-service onboarding

A deliberate, disclosed security tradeoff, replacing admin-generated random passwords entirely:

- **Identity is now fully separated from per-lab session state.** `live/accounts.csv` (one file, shared across every lab) holds `roll_no/password_hash/password_set` -- the actual login credential. Per-lab `students.csv` dropped its own `password_hash` column entirely and is now purely session state (`roll_no/ip/active/bound_at/last_seen`). `authenticate_student`'s signature changed to take both paths and returns `(roll_no, must_set_password)` instead of a bare roll_no.
- **No admin action creates an account.** The first time a roll number is ever seen at login, `grader.accounts.default_password(roll_no)` (`<roll number reversed>@Cp`) is the only password that will authenticate it; a correct attempt auto-provisions both the global identity (`password_set=False`) and this lab's session row in the same call. A wrong attempt is indistinguishable from "wrong password" for an existing account -- doesn't leak whether a roll number has been claimed yet. This is consciously guessable-by-construction (the formula is meant to be announced to an entire class as one sentence, not kept secret) -- the actual security property comes from the mandatory next step, not from the formula being hard to guess.
- **Mandatory first-login password change**, enforced server-side, not just client-side: `_require_password_set()` gates every functional route (`/api/questions`, `/api/save`, `/api/run`, `/api/results`, `/api/leaderboard`) with a 403 until `set_global_password` has been called -- deliberately excluding `/api/set-password` itself, `/api/logout`, `/api/whoami`, `/api/config`, `/api/session/status`, or a student could never complete the step. `index()` also redirects `/` back to `/login` if the password isn't set yet (covers the case where a student closed the tab mid-flow); `/login`'s own JS calls `/api/whoami` on load and shows the set-password form directly if needed, without asking for credentials again.
- **Admin's "reset password" now prompts for an explicit new password + confirmation** (two `prompt()` calls, matching the existing lightweight interaction style already used elsewhere in this UI) instead of generating a random one -- and, since it operates on the shared global file via the same `set_global_password(..., create_if_missing=True)` the student-facing flow uses, works even for a roll number that's never logged in anywhere yet.
- **"Generate accounts" removed entirely** from the admin UI, `server_admin`'s API, and the CLI (`accounts.generate_accounts` and its random-password generation deleted, not just hidden) -- there is nothing left to pre-provision. The Accounts tab is now a pure read view of who has actually signed in, plus a "Password: custom / default (not changed yet)" column so the admin can see at a glance who still needs to complete onboarding.
- **Real migration consequence, not a bug**: every student's previously-set password (from the old random-generation model) is void the moment this ships, since authentication now checks a global file (`live/accounts.csv`) that doesn't have any of them in it yet. Everyone's next login is treated as brand new, using the deterministic default password, with the mandatory change prompt firing again. Acceptable here since this was mid-development with no real graded session depending on the old credentials yet -- flagged explicitly so it isn't mistaken for a bug the *next* time this happens.
- Verified end-to-end at the HTTP level: wrong default password creates no account; correct default auto-provisions and returns `must_set_password=True`; every gated route 403s until `/api/set-password` succeeds; a set-password attempt equal to the default itself is rejected; the old default stops working immediately after a real password is set; logging in again from a new device with the new password succeeds; an admin-initiated reset for a student who has never logged in works and does not force a further forced change; a confirm-password mismatch is rejected both client-side and server-side.
