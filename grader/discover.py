"""Discover student submission folders and locate each question's source file
inside them. Anything ambiguous is flagged as an anomaly for a human to
resolve -- never silently guessed and never silently skipped.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROLL_NO_RE = re.compile(r"^[A-Za-z0-9]+$")


class StudentMappingError(Exception):
    """Raised when --student-csv points at a file missing a 'roll_no' column."""


@dataclass
class FileMatch:
    path: Path | None
    anomaly: str | None = None  # None | "MISSING" | "MULTIPLE_CANDIDATES"
    alternates: list[Path] = field(default_factory=list)


@dataclass
class Student:
    roll_no: str
    folder: Path
    folder_anomaly: str | None = None  # None | "UNRECOGNIZED_FOLDER" | "MAPPED_OVERRIDE"


def load_overrides(path: Path | None) -> dict[str, str]:
    """overrides.yaml maps a weird folder name to the roll number it belongs to,
    e.g. {"John_Doe_Submission": "112201099"}. Never guessed automatically."""
    if path is None or not Path(path).exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return {str(k): str(v) for k, v in data.items()}


@dataclass
class StudentMapping:
    # normalized roll_no (same .strip().upper() as Student.roll_no) -> {"name": ..., "ip": ...},
    # only the keys the source CSV actually had a column for.
    entries: dict[str, dict[str, str]] = field(default_factory=dict)
    has_name: bool = False
    has_ip: bool = False


def load_student_mapping(path: Path | None) -> StudentMapping:
    """Optional roll_no -> name/ip lookup, loaded from a CSV with a header row
    containing 'roll_no' and optionally 'name' and/or 'ip' (any column order,
    header matched case-insensitively). Used to enrich summary.csv -- purely
    opt-in, never required for grading itself, so a missing file just means no
    enrichment (returns an empty mapping) rather than an error. A *present*
    file without a 'roll_no' column is a real misconfiguration, though, and is
    reported rather than silently ignored.
    """
    if path is None or not Path(path).exists():
        return StudentMapping()

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = {(fn or "").strip().lower() for fn in (reader.fieldnames or [])}
        if "roll_no" not in fieldnames:
            raise StudentMappingError(f"{path}: expected a 'roll_no' column, found {sorted(fieldnames)}")
        has_name = "name" in fieldnames
        has_ip = "ip" in fieldnames

        entries: dict[str, dict[str, str]] = {}
        for row in reader:
            normalized = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            roll_no = normalized.get("roll_no", "").strip().upper()
            if not roll_no:
                continue
            entry = {}
            if has_name and normalized.get("name"):
                entry["name"] = normalized["name"]
            if has_ip and normalized.get("ip"):
                entry["ip"] = normalized["ip"]
            entries[roll_no] = entry

    return StudentMapping(entries=entries, has_name=has_name, has_ip=has_ip)


def discover_students(submissions_dir: Path, overrides: dict[str, str] | None = None) -> list[Student]:
    submissions_dir = Path(submissions_dir)
    overrides = overrides or {}
    students: list[Student] = []
    for child in sorted(submissions_dir.iterdir()):
        if not child.is_dir():
            continue
        raw_name = child.name
        if raw_name in overrides:
            students.append(
                Student(roll_no=overrides[raw_name], folder=child, folder_anomaly="MAPPED_OVERRIDE")
            )
            continue
        normalized = raw_name.strip().upper()
        if ROLL_NO_RE.match(normalized):
            students.append(Student(roll_no=normalized, folder=child))
        else:
            students.append(
                Student(roll_no=raw_name, folder=child, folder_anomaly="UNRECOGNIZED_FOLDER")
            )
    return students


def find_question_file(student_folder: Path, filename_patterns: list[str]) -> FileMatch:
    candidates_by_pattern: list[list[Path]] = []
    all_found: list[Path] = []
    files = [p for p in student_folder.iterdir() if p.is_file()]
    for pattern in filename_patterns:
        matches = [p for p in files if p.name.lower() == pattern.lower()]
        candidates_by_pattern.append(matches)
        all_found.extend(matches)

    seen: set[Path] = set()
    unique_found: list[Path] = []
    for p in all_found:
        if p not in seen:
            seen.add(p)
            unique_found.append(p)

    if not unique_found:
        return FileMatch(path=None, anomaly="MISSING")

    # Prefer an exact case-sensitive match against the first pattern that has one.
    for pattern, matches in zip(filename_patterns, candidates_by_pattern):
        exact = [p for p in matches if p.name == pattern]
        if exact:
            chosen = exact[0]
            alternates = [p for p in unique_found if p != chosen]
            anomaly = "MULTIPLE_CANDIDATES" if alternates else None
            return FileMatch(path=chosen, anomaly=anomaly, alternates=alternates)

    # No exact match anywhere -- fall back to the most recently modified candidate.
    chosen = max(unique_found, key=lambda p: p.stat().st_mtime)
    alternates = [p for p in unique_found if p != chosen]
    return FileMatch(path=chosen, anomaly="MULTIPLE_CANDIDATES", alternates=alternates)
