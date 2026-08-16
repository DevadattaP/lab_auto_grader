"""Per-lab, runtime-editable display toggles for what stays visible to
students once a session is finalized: workspace browsing (questions/saved
code/last run results), the per-student report, and the leaderboard, each
independently on/off.

Exists because server_admin (which sets these) and server_student (which
serves students and must enforce them) are separate processes with no
shared memory -- CLI args alone can't let an admin flip one of these at
runtime without restarting server_student. Persisted as
`live/<lab>/display_config.json` instead: server_admin writes it,
server_student reads it fresh on every relevant request (not just once at
startup), so a change takes effect for every logged-in student within one
heartbeat poll, no restart needed. Same read/write pattern as
live_session.py (writes are filelock-protected + atomic replace; reads are
lock-free, safe for the same reason accounts.get_account's are).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from filelock import FileLock


@dataclass
class DisplayConfig:
    show_workspace_after_session: bool = True
    show_report: bool = True
    show_leaderboard: bool = True


def _path(live_dir: Path) -> Path:
    return Path(live_dir) / "display_config.json"


def _lock(live_dir: Path) -> FileLock:
    return FileLock(str(_path(live_dir)) + ".lock")


def get_config(live_dir: Path) -> DisplayConfig:
    path = _path(live_dir)
    if not path.exists():
        return DisplayConfig()
    return DisplayConfig(**json.loads(path.read_text()))


def set_config(live_dir: Path, **overrides: bool) -> DisplayConfig:
    """Only the given keyword(s) change; anything not passed keeps its
    current value. Unknown keys are a hard error (a typo'd flag should
    never silently no-op)."""
    with _lock(live_dir):
        path = _path(live_dir)
        current = DisplayConfig(**json.loads(path.read_text())) if path.exists() else DisplayConfig()
        for key, value in overrides.items():
            if not hasattr(current, key):
                raise ValueError(f"unknown display config key: {key}")
            setattr(current, key, bool(value))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(asdict(current), indent=2))
        tmp_path.replace(path)
        return current


def ensure_initialized(live_dir: Path, **defaults: bool) -> DisplayConfig:
    """Called once by server_student at startup: seeds the file from its
    CLI-arg defaults only if nothing is there yet, so server_admin's UI has
    something concrete to show from the very first request -- and never
    clobbers a value an admin already set in a previous run."""
    path = _path(live_dir)
    if path.exists():
        return get_config(live_dir)
    return set_config(live_dir, **defaults)
