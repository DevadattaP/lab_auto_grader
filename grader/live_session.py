"""Live-lab session/timer state: one JSON file per lab
(`live/<lab_id>/session.json`). This is the single source of truth for
whether a student's save/run should currently be accepted -- checked fresh
against the wall clock on every request by server_student (see
LIVE_LAB_DESIGN.md §6), never cached or trusted from a client-supplied flag,
so a client's local clock lagging the real deadline can't extend a student's
window past when the instructor actually locked the lab.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from filelock import FileLock

STATUS_NOT_STARTED = "not_started"
STATUS_RUNNING = "running"
STATUS_LOCKED = "locked"
STATUS_FINALIZED = "finalized"


class SessionError(Exception):
    """Raised for an invalid session-state transition, e.g. starting an
    already-running lab or finalizing before it's locked."""


@dataclass
class SessionState:
    status: str = STATUS_NOT_STARTED
    start_time: str | None = None
    duration_minutes: int | None = None
    end_time: str | None = None
    locked_at: str | None = None
    finalized_run: str | None = None  # runs/<lab>/<this> once Finalize has published results


def _session_path(live_dir: Path) -> Path:
    return Path(live_dir) / "session.json"


def _lock(live_dir: Path) -> FileLock:
    return FileLock(str(_session_path(live_dir)) + ".lock")


def _read(live_dir: Path) -> SessionState:
    path = _session_path(live_dir)
    if not path.exists():
        return SessionState()
    return SessionState(**json.loads(path.read_text()))


def _write(live_dir: Path, state: SessionState) -> None:
    path = _session_path(live_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(asdict(state), indent=2))
    tmp_path.replace(path)


def get_state(live_dir: Path) -> SessionState:
    with _lock(live_dir):
        return _read(live_dir)


def start(live_dir: Path, duration_minutes: int) -> SessionState:
    if duration_minutes <= 0:
        raise SessionError("duration_minutes must be > 0")
    with _lock(live_dir):
        state = _read(live_dir)
        if state.status == STATUS_RUNNING:
            raise SessionError("Session is already running")
        now = datetime.now().astimezone()
        end = now + timedelta(minutes=duration_minutes)
        state = SessionState(
            status=STATUS_RUNNING,
            start_time=now.isoformat(timespec="seconds"),
            duration_minutes=duration_minutes,
            end_time=end.isoformat(timespec="seconds"),
        )
        _write(live_dir, state)
        return state


def extend(live_dir: Path, additional_minutes: int) -> SessionState:
    with _lock(live_dir):
        state = _read(live_dir)
        if state.status != STATUS_RUNNING or state.end_time is None:
            raise SessionError("Session is not running")
        end = datetime.fromisoformat(state.end_time) + timedelta(minutes=additional_minutes)
        state.end_time = end.isoformat(timespec="seconds")
        state.duration_minutes = (state.duration_minutes or 0) + additional_minutes
        _write(live_dir, state)
        return state


def lock(live_dir: Path) -> SessionState:
    """Admin 'End lab now', or called by auto_lock_if_expired once the
    timer runs out on its own."""
    with _lock(live_dir):
        state = _read(live_dir)
        if state.status != STATUS_RUNNING:
            raise SessionError(f"Cannot lock from status '{state.status}' (must be '{STATUS_RUNNING}')")
        state.status = STATUS_LOCKED
        state.locked_at = datetime.now().astimezone().isoformat(timespec="seconds")
        _write(live_dir, state)
        return state


def mark_finalized(live_dir: Path, run_dir_name: str) -> SessionState:
    with _lock(live_dir):
        state = _read(live_dir)
        if state.status != STATUS_LOCKED:
            raise SessionError(f"Session must be '{STATUS_LOCKED}' before finalizing (is '{state.status}')")
        state.status = STATUS_FINALIZED
        state.finalized_run = run_dir_name
        _write(live_dir, state)
        return state


def reset(live_dir: Path) -> SessionState:
    """Admin escape hatch: wipe session state back to not_started (e.g. to
    re-run a lab slot). Never touches students.csv or submissions/ -- purely
    the timer/status file."""
    with _lock(live_dir):
        state = SessionState()
        _write(live_dir, state)
        return state


def is_write_allowed(live_dir: Path) -> bool:
    """Authoritative gate for /api/save and /api/run -- must be re-checked
    at the moment of the write itself, not read once and cached."""
    with _lock(live_dir):
        state = _read(live_dir)
    if state.status != STATUS_RUNNING or state.end_time is None:
        return False
    return datetime.now().astimezone() < datetime.fromisoformat(state.end_time)


def time_remaining_seconds(live_dir: Path) -> float:
    with _lock(live_dir):
        state = _read(live_dir)
    if state.status != STATUS_RUNNING or state.end_time is None:
        return 0.0
    remaining = (datetime.fromisoformat(state.end_time) - datetime.now().astimezone()).total_seconds()
    return max(0.0, remaining)


def auto_lock_if_expired(live_dir: Path) -> SessionState:
    """Opportunistically flips 'running' -> 'locked' once the wall clock
    passes end_time. Meant to be called on every status poll (both servers'
    /api/.../status routes) so the session doesn't stay stuck in 'running'
    just because nobody clicked 'End lab now' at the exact right moment."""
    with _lock(live_dir):
        state = _read(live_dir)
        if state.status == STATUS_RUNNING and state.end_time is not None:
            if datetime.now().astimezone() >= datetime.fromisoformat(state.end_time):
                state.status = STATUS_LOCKED
                state.locked_at = datetime.now().astimezone().isoformat(timespec="seconds")
                _write(live_dir, state)
        return state
