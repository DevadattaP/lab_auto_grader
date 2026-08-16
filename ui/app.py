"""Minimal local web UI for browsing/authoring lab_auto_grader labs.

Serves a single page (tree view of labs -> questions/submissions/result) plus
a small JSON API the page's JS talks to. Local-use tool: binds to
127.0.0.1 only, no auth, and every path segment coming from a URL is
validated against a safe-name pattern before touching the filesystem.

Run with:  python3 -m ui.app        (from the lab_auto_grader directory)
"""

from __future__ import annotations

import ast
import csv
import html as html_escape
import io
import re
import shutil
import sys
from pathlib import Path

import markdown as markdown_lib
import yaml
from flask import Flask, abort, jsonify, request, send_from_directory
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import CLexer

lab_auto_grader_ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_ROOT = lab_auto_grader_ROOT / "questions"
SUBMISSIONS_ROOT = lab_auto_grader_ROOT / "submissions"
RUNS_ROOT = lab_auto_grader_ROOT / "runs"

sys.path.insert(0, str(lab_auto_grader_ROOT))
from grader.code_checks import KNOWN_BUILTIN_CONSTRUCTS  # noqa: E402
from grader.config_schema import QuestionConfigError, load_question  # noqa: E402
from grader.discover import find_question_file  # noqa: E402

app = Flask(__name__)

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

BUILTIN_CONSTRUCTS = KNOWN_BUILTIN_CONSTRUCTS
COMPILE_STANDARDS = ["c89", "c99", "c11", "c17"]
COMMON_COMPILE_FLAGS = ["-Wall", "-Wextra", "-O2", "-O0", "-g", "-lm"]
MATCHER_TYPES = ["exact_trim", "exact", "token", "float_tol", "custom"]


# --------------------------------------------------------------------------
# YAML output styling -- PyYAML's default representer is *correct* (it always
# round-trips) but picks ugly styles for multi-line strings by default: a
# quoted scalar with blank-line-separated fragments to encode embedded
# newlines, instead of a plain `|` block. That's what a hand-authored
# question.yaml never looks like, so the UI forces the same style choices a
# human would make: literal block scalars for anything multi-line, and flow
# ([a, b, c]) style for the handful of short, flat lists that are always
# short in practice (filename_patterns, compile flags, code_checks
# require/forbid) -- exactly matching questions/lab_01/q3_bitwise_playground,
# which was hand-written before this UI existed.
# --------------------------------------------------------------------------


class _LiteralStr(str):
    """A str that yaml.safe_dump renders with `|` block-scalar style."""


class _FlowList(list):
    """A list that yaml.safe_dump renders as `[a, b, c]` instead of one
    item per line."""


def _represent_literal_str(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


def _represent_flow_list(dumper, data):
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)


yaml.add_representer(_LiteralStr, _represent_literal_str, Dumper=yaml.SafeDumper)
yaml.add_representer(_FlowList, _represent_flow_list, Dumper=yaml.SafeDumper)


def _multiline_aware(text: str) -> str:
    """Wrap as a literal block scalar if it actually spans multiple lines;
    leave single-line strings to PyYAML's own (already-correct) default
    quoting so short values like 'PASS' or '75' stay unquoted/plain."""
    return _LiteralStr(text) if "\n" in text else text


def _format_symbol_groups(groups: list) -> str:
    """`[["x", "X", "*"], [":", "="]]` -> `"x,X,*|:,="` -- a compact single-line
    mini-syntax for the text field in the question editor, since a nested
    list has no natural single-input widget. Groups separated by `|`,
    symbols within a group by `,`."""
    return "|".join(",".join(group) for group in groups if group)


def _parse_symbol_groups(text: str) -> list:
    """Inverse of _format_symbol_groups. Blank/whitespace-only input -> [].
    Does not validate group size or duplicate symbols across groups --
    config_schema.load_question does that at save time, the same as every
    other field this form doesn't pre-validate client-side."""
    text = text.strip()
    if not text:
        return []
    groups = []
    for group_text in text.split("|"):
        symbols = [s.strip() for s in group_text.split(",") if s.strip()]
        if symbols:
            groups.append(_FlowList(symbols))
    return groups


def _num(value) -> int | float:
    """marks/weights/penalty are declared as floats throughout config_schema.py
    (float(...) coerces either representation identically at load time), but
    a hand-authored question.yaml always writes a whole number plainly
    (`weight: 4`, not `weight: 4.0`) -- match that instead of forcing every
    value through float() and getting a `.0` on everything."""
    f = float(value)
    return int(f) if f.is_integer() else f


def _safe_name(name: str) -> str:
    """Validate a single path segment coming from a URL. Raises 400 on
    anything that isn't a plain filename-safe token -- blocks path
    traversal (`..`, `/`) and anything else unexpected."""
    if not name or not SAFE_NAME.match(name) or name in (".", ".."):
        abort(400, f"invalid name: {name!r}")
    return name


def _lab_dirs(lab_id: str) -> tuple[Path, Path, Path]:
    lab_id = _safe_name(lab_id)
    return QUESTIONS_ROOT / lab_id, SUBMISSIONS_ROOT / lab_id, RUNS_ROOT / lab_id


# --------------------------------------------------------------------------
# tree
# --------------------------------------------------------------------------


def _latest_run_dir(runs_lab_dir: Path) -> Path | None:
    if not runs_lab_dir.is_dir():
        return None
    candidates = sorted((p for p in runs_lab_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
    return candidates[-1] if candidates else None


def build_tree() -> dict:
    labs = []
    if QUESTIONS_ROOT.is_dir():
        lab_ids = {p.name for p in QUESTIONS_ROOT.iterdir() if p.is_dir()}
    else:
        lab_ids = set()
    if SUBMISSIONS_ROOT.is_dir():
        lab_ids |= {p.name for p in SUBMISSIONS_ROOT.iterdir() if p.is_dir()}

    for lab_id in sorted(lab_ids):
        q_dir, s_dir, r_dir = _lab_dirs(lab_id)

        questions = sorted(p.name for p in q_dir.iterdir() if p.is_dir()) if q_dir.is_dir() else []

        submissions = {}
        if s_dir.is_dir():
            for student_dir in sorted(s_dir.iterdir(), key=lambda p: p.name):
                if student_dir.is_dir():
                    submissions[student_dir.name] = sorted(
                        p.name for p in student_dir.iterdir() if p.is_file()
                    )

        result = None
        latest = _latest_run_dir(r_dir)
        if latest is not None:
            result = {
                "run_id": latest.name,
                "files": sorted(p.name for p in latest.iterdir() if p.is_file()),
            }

        dashboard = _build_dashboard(q_dir, s_dir, questions, list(submissions.keys()))

        labs.append(
            {
                "id": lab_id,
                "questions": questions,
                "submissions": submissions,
                "result": result,
                "dashboard": dashboard,
            }
        )

    return {"labs": labs}


def _build_dashboard(
    q_dir: Path, s_dir: Path, questions: list[str], students: list[str]
) -> dict:
    """Who submitted a file for which question, both ways round -- powers the
    dashboard tab's "by question" / "by student" views. Reuses the same
    filename-pattern matching grade.py itself uses (grader.discover), so the
    dashboard always agrees with what the grader would actually pick up."""
    by_question: dict[str, list[str]] = {}
    by_student: dict[str, list[str]] = {student: [] for student in students}

    for qid in questions:
        yaml_path = q_dir / qid / "question.yaml"
        try:
            raw = yaml.safe_load(yaml_path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
        patterns = raw.get("filename_patterns") or []

        matched: list[str] = []
        for student in students:
            match = find_question_file(s_dir / student, patterns)
            if match.path is not None:
                matched.append(student)
                by_student[student].append(qid)
        by_question[qid] = matched

    return {"by_question": by_question, "by_student": by_student}


@app.get("/api/tree")
def api_tree():
    return jsonify(build_tree())


# --------------------------------------------------------------------------
# labs
# --------------------------------------------------------------------------


@app.post("/api/labs")
def api_create_lab():
    lab_id = _safe_name((request.json or {}).get("id", "").strip())
    q_dir, s_dir, r_dir = _lab_dirs(lab_id)
    if q_dir.exists() or s_dir.exists():
        abort(409, f"lab '{lab_id}' already exists")
    q_dir.mkdir(parents=True)
    s_dir.mkdir(parents=True)
    return jsonify({"id": lab_id}), 201


@app.delete("/api/labs/<lab_id>")
def api_delete_lab(lab_id: str):
    q_dir, s_dir, r_dir = _lab_dirs(lab_id)
    for d in (q_dir, s_dir, r_dir):
        if d.exists():
            shutil.rmtree(d)
    return jsonify({"deleted": lab_id})


# --------------------------------------------------------------------------
# questions -- question.yaml (+ gold.c) as a flat JSON form payload
# --------------------------------------------------------------------------


@app.get("/api/meta")
def api_meta():
    """Static option lists the question-editor form needs -- kept server-side
    so the frontend never hardcodes a second copy of what code_checks.py or
    config_schema.py already know."""
    return jsonify(
        {
            "builtin_constructs": BUILTIN_CONSTRUCTS,
            "compile_standards": COMPILE_STANDARDS,
            "common_compile_flags": COMMON_COMPILE_FLAGS,
            "matcher_types": MATCHER_TYPES,
            "defaults": {
                "compile_standard": "c11",
                "compile_flags_common": ["-O2", "-Wall"],
                "limits": {
                    "time_seconds": 2,
                    "wall_seconds": 5,
                    "memory_mb": 128,
                    "output_bytes": 65536,
                    "max_processes": 1,
                },
                "matcher_type": "exact_trim",
                "code_checks_mode": "gate",
                "code_checks_require_match": "any",
            },
        }
    )


def _split_extra(text: str) -> list[str]:
    """Split a free-text field (comma- and/or newline-separated) into a
    clean list, e.g. compile_flags_extra or code_checks_*_extra."""
    if not text:
        return []
    return [p.strip() for p in re.split(r"[,\n]+", text) if p.strip()]


def question_to_payload(qdir: Path) -> dict:
    raw = yaml.safe_load((qdir / "question.yaml").read_text()) or {}
    marks = raw.get("marks") or {}
    compile_cfg = raw.get("compile") or {}
    limits_cfg = raw.get("limits") or {}
    matcher_cfg = raw.get("matcher") or {"type": "exact_trim"}

    flags = list(compile_cfg.get("flags") or [])
    common_flags = [f for f in flags if f in COMMON_COMPILE_FLAGS]
    extra_flags = [f for f in flags if f not in COMMON_COMPILE_FLAGS]

    payload = {
        "id": raw.get("id", qdir.name),
        "title": raw.get("title", ""),
        "description": raw.get("description", ""),
        "filename_patterns": raw.get("filename_patterns") or [],
        "marks_open": marks.get("open", 0),
        "marks_hidden": marks.get("hidden", 0),
        "compile_standard": compile_cfg.get("standard", "c11"),
        "compile_flags_common": common_flags,
        "compile_flags_extra": " ".join(extra_flags),
        "limits": {
            "time_seconds": limits_cfg.get("time_seconds", 2),
            "wall_seconds": limits_cfg.get("wall_seconds", 5),
            "memory_mb": limits_cfg.get("memory_mb", 128),
            "output_bytes": limits_cfg.get("output_bytes", 65536),
            "max_processes": limits_cfg.get("max_processes", 1),
        },
        "matcher_type": matcher_cfg.get("type", "exact_trim"),
        "matcher_eps": matcher_cfg.get("eps"),
        "matcher_ignore_case": bool(matcher_cfg.get("ignore_case", False)),
        "matcher_ignore_whitespace": bool(matcher_cfg.get("ignore_whitespace", False)),
        "matcher_ignore_punctuation": bool(matcher_cfg.get("ignore_punctuation", False)),
        "matcher_allow_extra_output": bool(matcher_cfg.get("allow_extra_output", False)),
        "matcher_symbol_groups": _format_symbol_groups(matcher_cfg.get("symbol_groups") or []),
        "adjust_char_input": bool(raw.get("adjust_char_input", False)),
        "adjust_scanf_address": bool(raw.get("adjust_scanf_address", False)),
        "code_checks_enabled": False,
        "code_checks_mode": "gate",
        "code_checks_require_builtin": [],
        "code_checks_require_extra": "",
        "code_checks_require_match": "any",
        "code_checks_forbid_builtin": [],
        "code_checks_forbid_extra": "",
        "code_checks_marks": None,
        "code_checks_penalty": None,
        "code_checks_line_gate_text": "",
        "tests_open": [],
        "tests_hidden": [],
        "gold_c": "",
    }

    code_checks_cfg = raw.get("code_checks")
    if code_checks_cfg:
        require = code_checks_cfg.get("require") or []
        forbid = code_checks_cfg.get("forbid") or []
        payload["code_checks_enabled"] = True
        payload["code_checks_mode"] = code_checks_cfg.get("mode", "gate")
        payload["code_checks_require_builtin"] = [c for c in require if c in BUILTIN_CONSTRUCTS]
        payload["code_checks_require_extra"] = ", ".join(c for c in require if c not in BUILTIN_CONSTRUCTS)
        payload["code_checks_require_match"] = code_checks_cfg.get("require_match", "any")
        payload["code_checks_forbid_builtin"] = [c for c in forbid if c in BUILTIN_CONSTRUCTS]
        payload["code_checks_forbid_extra"] = ", ".join(c for c in forbid if c not in BUILTIN_CONSTRUCTS)
        payload["code_checks_marks"] = code_checks_cfg.get("marks")
        payload["code_checks_penalty"] = code_checks_cfg.get("penalty")
        # "line_gate" mode's mapping (0-based output line index -> required
        # construct name) has no dedicated widget -- shown/edited as plain
        # "index: construct" lines, one per entry, same idea as the
        # comma-separated "extra" text fields elsewhere in this form.
        line_gate = code_checks_cfg.get("line_gate") or {}
        payload["code_checks_line_gate_text"] = "\n".join(
            f"{idx}: {construct}" for idx, construct in sorted(line_gate.items(), key=lambda kv: int(kv[0]))
        )

    tests_cfg = raw.get("tests") or {}
    for group in ("open", "hidden"):
        for t in tests_cfg.get(group) or []:
            # A test authored with in_file/out_file (large test data, see
            # AUTOGRADER_DESIGN.md 3) is shown here with its file's content
            # inlined for editing convenience; saving from the UI always
            # writes it back out as inline in/out, not as a file reference.
            in_text = t.get("in")
            if in_text is None and t.get("in_file"):
                in_text = (qdir / t["in_file"]).read_text()
            out_text = t.get("out")
            if out_text is None and t.get("out_file"):
                out_text = (qdir / t["out_file"]).read_text()
            payload[f"tests_{group}"].append(
                {
                    "name": t.get("name", ""),
                    "in": in_text or "",
                    "out": out_text or "",
                    "weight": t.get("weight", 0),
                    "partial": bool(t.get("partial", False)),
                    "ignore_typo": bool(t.get("ignore_typo", False)),
                }
            )

    gold_path = qdir / "gold.c"
    if gold_path.exists():
        payload["gold_c"] = gold_path.read_text()

    return payload


def build_question_yaml(payload: dict) -> dict:
    doc = {
        "id": payload["id"],
        "title": payload.get("title") or payload["id"],
        "description": _multiline_aware(payload.get("description", "")),
        "filename_patterns": _FlowList(p for p in (payload.get("filename_patterns") or []) if p),
        "marks": {"open": _num(payload["marks_open"]), "hidden": _num(payload["marks_hidden"])},
        "compile": {
            "standard": payload.get("compile_standard", "c11"),
            "flags": _FlowList(
                list(payload.get("compile_flags_common") or [])
                + _split_extra(payload.get("compile_flags_extra", ""))
            ),
        },
        "limits": payload.get("limits") or {},
        "matcher": {"type": payload.get("matcher_type", "exact_trim")},
    }
    if doc["matcher"]["type"] == "float_tol" and payload.get("matcher_eps") not in (None, ""):
        doc["matcher"]["eps"] = _num(payload["matcher_eps"])

    if doc["matcher"]["type"] != "custom":
        # Composable tolerances -- only written when set, so a question with
        # none of them looks exactly like it did before they existed.
        for key in (
            "ignore_case",
            "ignore_whitespace",
            "ignore_punctuation",
            "allow_extra_output",
        ):
            if payload.get(f"matcher_{key}"):
                doc["matcher"][key] = True
        symbol_groups = _parse_symbol_groups(payload.get("matcher_symbol_groups", ""))
        if symbol_groups:
            doc["matcher"]["symbol_groups"] = _FlowList(symbol_groups)

    if payload.get("adjust_char_input"):
        doc["adjust_char_input"] = True

    if payload.get("adjust_scanf_address"):
        doc["adjust_scanf_address"] = True

    if payload.get("code_checks_enabled"):
        require = _FlowList(
            list(payload.get("code_checks_require_builtin") or [])
            + _split_extra(payload.get("code_checks_require_extra", ""))
        )
        forbid = _FlowList(
            list(payload.get("code_checks_forbid_builtin") or [])
            + _split_extra(payload.get("code_checks_forbid_extra", ""))
        )
        mode = payload.get("code_checks_mode", "gate")
        cc = {"mode": mode, "require": require}
        if require:
            cc["require_match"] = payload.get("code_checks_require_match", "any")
        cc["forbid"] = forbid
        if mode == "bonus" and payload.get("code_checks_marks") not in (None, ""):
            cc["marks"] = _num(payload["code_checks_marks"])
        elif mode == "penalty" and payload.get("code_checks_penalty") not in (None, ""):
            cc["penalty"] = _num(payload["code_checks_penalty"])
        elif mode == "line_gate":
            line_gate = {}
            for line in (payload.get("code_checks_line_gate_text") or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                if ":" not in line:
                    raise ValueError(f"line_gate entry must be 'index: construct', got {line!r}")
                idx_text, construct = line.split(":", 1)
                line_gate[int(idx_text.strip())] = construct.strip()
            if not line_gate:
                raise ValueError("mode 'line_gate' needs at least one 'index: construct' line")
            cc["line_gate"] = line_gate
        doc["code_checks"] = cc

    doc["tests"] = {"open": [], "hidden": []}
    for group in ("open", "hidden"):
        for t in payload.get(f"tests_{group}") or []:
            entry = {
                "name": t["name"],
                "in": _multiline_aware(t.get("in", "")),
                "out": _multiline_aware(t.get("out", "")),
                "weight": _num(t["weight"]),
            }
            if t.get("partial"):
                entry["partial"] = True
            if t.get("ignore_typo"):
                entry["ignore_typo"] = True
            doc["tests"][group].append(entry)

    return doc


def _rollback(yaml_path: Path, gold_path: Path, old_yaml: str | None, old_gold: str | None, qdir: Path, is_new: bool) -> None:
    if old_yaml is None:
        yaml_path.unlink(missing_ok=True)
    else:
        yaml_path.write_text(old_yaml)
    if old_gold is None:
        gold_path.unlink(missing_ok=True)
    else:
        gold_path.write_text(old_gold)
    if is_new and qdir.exists() and not any(qdir.iterdir()):
        qdir.rmdir()


@app.get("/api/labs/<lab_id>/questions/<qid>")
def api_get_question(lab_id: str, qid: str):
    q_dir, _, _ = _lab_dirs(lab_id)
    qid = _safe_name(qid)
    qdir = q_dir / qid
    if not (qdir / "question.yaml").exists():
        abort(404, "question not found")
    return jsonify(question_to_payload(qdir))


@app.post("/api/labs/<lab_id>/questions/<qid>")
def api_save_question(lab_id: str, qid: str):
    q_dir, _, _ = _lab_dirs(lab_id)
    qid = _safe_name(qid)
    payload = request.json or {}
    if payload.get("id") != qid:
        abort(400, "payload id must match the question's folder name")

    qdir = q_dir / qid
    is_new = not qdir.exists()
    qdir.mkdir(parents=True, exist_ok=True)

    yaml_path = qdir / "question.yaml"
    gold_path = qdir / "gold.c"
    old_yaml = yaml_path.read_text() if yaml_path.exists() else None
    old_gold = gold_path.read_text() if gold_path.exists() else None

    try:
        doc = build_question_yaml(payload)
        yaml_path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
        gold_path.write_text(payload.get("gold_c", ""))
        load_question(qdir)  # validates everything (weight sums, construct names, matcher, ...)
    except QuestionConfigError as e:
        _rollback(yaml_path, gold_path, old_yaml, old_gold, qdir, is_new)
        return jsonify({"error": str(e)}), 400
    except (KeyError, ValueError, TypeError) as e:
        _rollback(yaml_path, gold_path, old_yaml, old_gold, qdir, is_new)
        return jsonify({"error": f"invalid form data: {e}"}), 400

    return jsonify({"id": qid})


@app.delete("/api/labs/<lab_id>/questions/<qid>")
def api_delete_question(lab_id: str, qid: str):
    q_dir, _, _ = _lab_dirs(lab_id)
    qid = _safe_name(qid)
    qdir = q_dir / qid
    if qdir.exists():
        shutil.rmtree(qdir)
    return jsonify({"deleted": qid})


# --------------------------------------------------------------------------
# submissions -- view a student's source file, syntax-highlighted
# --------------------------------------------------------------------------


@app.get("/api/labs/<lab_id>/submissions/<student>/<filename>")
def api_view_submission_file(lab_id: str, student: str, filename: str):
    _, s_dir, _ = _lab_dirs(lab_id)
    student = _safe_name(student)
    filename = _safe_name(filename)
    path = s_dir / student / filename
    if not path.is_file():
        abort(404, "file not found")

    content = path.read_text(errors="replace")
    if filename.endswith(".c") or filename.endswith(".h"):
        formatter = HtmlFormatter(style="default", noclasses=True)
        rendered = highlight(content, CLexer(), formatter)
    else:
        rendered = f"<pre>{html_escape.escape(content)}</pre>"

    return jsonify({"filename": filename, "html": rendered})


# --------------------------------------------------------------------------
# dashboard -- question x student cross-reference; the modal that pairs a
# student's submission with the gold solution and their per-question report
# section
# --------------------------------------------------------------------------


def _find_report_file(latest_run_dir: Path, student: str) -> Path | None:
    """report.py names the file after Student.roll_no, which for a normal
    submissions folder is the folder name upper-cased (see
    grader.discover.discover_students) -- try that, then the folder name
    verbatim in case it was already normalized."""
    for candidate in (f"report_{student.upper()}.md", f"report_{student}.md"):
        path = latest_run_dir / candidate
        if path.is_file():
            return path
    return None


def _extract_report_section(report_text: str, qid: str) -> str | None:
    """Pull out just the `## {qid}: ...` .. next `## ` block from a
    per-student report.md (see grader/report.py:render_student_report)."""
    lines = report_text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(f"## {qid}:")), None)
    if start is None:
        return None
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip()


def _highlight_c(text: str) -> str:
    formatter = HtmlFormatter(style="default", noclasses=True)
    return highlight(text, CLexer(), formatter)


# grader/report.py writes a failed/partial test's input/expected/actual as
# three consecutive list items via Python's repr() -- e.g.
# `'*******\n *****\n  ***\n   *\n'` -- specifically so a real newline or
# run of spaces is visible as literal `\n`/spaces in the raw .md file
# (readable in a plain editor/terminal). Markdown then renders that as
# three separate <li>label: <code>'...'</code></li> items stacked
# vertically, each showing its repr'd text as one long line with literal
# backslash-n *characters* in it, not an actual line break. This matches
# all three together (report.py always emits them as this exact
# input/expected/actual triplet, nothing else interleaved) and replaces
# them with one flex row of three bordered, titled columns -- decoding
# each field's repr text back to its real bytes and rendering it in a
# <pre> so a multi-line value (a pyramid's rows, a multi-line Secret Pair
# report, ...) shows real line breaks and indentation, side by side for
# easy comparison instead of stacked and hard to visually diff. Falls back
# to leaving the original three <li>s untouched if any field isn't valid
# repr() syntax (defensive -- should never happen against report.py's own
# output, but this is markdown someone could hand-edit).
_IO_TRIPLET_RE = re.compile(
    r"<ul>\s*"
    r"<li>input: <code>(.*?)</code></li>\s*"
    r"<li>expected: <code>(.*?)</code></li>\s*"
    r"<li>actual: <code>(.*?)</code></li>\s*"
    r"</ul>",
    re.DOTALL,
)


def _render_io_side_by_side(html: str) -> str:
    def repl(m: re.Match) -> str:
        cols = []
        for label, code_content in zip(("input", "expected", "actual"), m.groups()):
            raw_repr = html_escape.unescape(code_content)
            try:
                value = ast.literal_eval(raw_repr)
            except (ValueError, SyntaxError):
                return m.group(0)
            cols.append(
                f'<div class="io-col"><div class="io-col-title">{label}</div>'
                f"<pre>{html_escape.escape(value)}</pre></div>"
            )
        return f'<div class="io-row">{"".join(cols)}</div>'

    return _IO_TRIPLET_RE.sub(repl, html)


# Markdown wraps a `` `...` `` span as a bare <code>...</code> with no
# white-space CSS override, so consecutive literal spaces inside one (e.g. a
# test *name* like `pair_AK` glued to surrounding punctuation) collapse
# down to a single visible space when a browser renders the HTML -- even
# though the text node itself still has all of them. A ```fenced``` block
# renders as <pre><code>...</code></pre> instead, which already preserves
# whitespace via `.modal-body pre { white-space: pre-wrap }` in style.css,
# so that's left alone here (matched by the first branch, kept verbatim).
# Only a bare inline span (second branch) gets its regular spaces swapped
# for &nbsp; -- sidesteps whitespace-collapsing at the HTML level so it
# doesn't matter what CSS the page embedding this HTML does or doesn't
# apply. Run _render_io_side_by_side first so the input/expected/actual
# fields it already converted to <pre> (which don't need this trick at all)
# aren't also matched here.
_CODE_SPAN_RE = re.compile(r"(<pre><code>.*?</code></pre>)|(<code>.*?</code>)", re.DOTALL)


def _preserve_inline_code_whitespace(html: str) -> str:
    def repl(m: re.Match) -> str:
        inline_span = m.group(2)
        return inline_span.replace(" ", "&nbsp;") if inline_span is not None else m.group(1)

    return _CODE_SPAN_RE.sub(repl, html)


@app.get("/api/labs/<lab_id>/dashboard/<qid>/<student>")
def api_dashboard_detail(lab_id: str, qid: str, student: str):
    q_dir, s_dir, r_dir = _lab_dirs(lab_id)
    qid = _safe_name(qid)
    student = _safe_name(student)

    qdir = q_dir / qid
    if not (qdir / "question.yaml").exists():
        abort(404, "question not found")
    raw = yaml.safe_load((qdir / "question.yaml").read_text()) or {}
    title = raw.get("title", qid)
    filename_patterns = raw.get("filename_patterns") or []

    student_dir = s_dir / student
    if not student_dir.is_dir():
        abort(404, "student not found")
    match = find_question_file(student_dir, filename_patterns)

    student_filename = None
    student_code_html = None
    if match.path is not None:
        student_filename = match.path.name
        student_code_html = _highlight_c(match.path.read_text(errors="replace"))

    gold_path = qdir / "gold.c"
    gold_code_html = _highlight_c(gold_path.read_text(errors="replace")) if gold_path.exists() else None

    report_html = None
    latest = _latest_run_dir(r_dir)
    if latest is not None:
        report_path = _find_report_file(latest, student)
        if report_path is not None:
            section = _extract_report_section(report_path.read_text(errors="replace"), qid)
            if section is not None:
                report_html = markdown_lib.markdown(section, extensions=["fenced_code", "tables"], tab_length=2)
                report_html = _render_io_side_by_side(report_html)
                report_html = _preserve_inline_code_whitespace(report_html)

    return jsonify(
        {
            "title": title,
            "student_filename": student_filename,
            "student_code_html": student_code_html,
            "gold_code_html": gold_code_html,
            "report_html": report_html,
        }
    )


# --------------------------------------------------------------------------
# result -- view a file from the latest run (report .md, run.log, summary.csv)
# --------------------------------------------------------------------------


@app.get("/api/labs/<lab_id>/result/<filename>")
def api_view_result_file(lab_id: str, filename: str):
    _, _, r_dir = _lab_dirs(lab_id)
    filename = _safe_name(filename)
    latest = _latest_run_dir(r_dir)
    if latest is None:
        abort(404, "no runs for this lab")
    path = latest / filename
    if not path.is_file():
        abort(404, "file not found")

    content = path.read_text(errors="replace")

    if filename.endswith(".md"):
        # report.py nests list items with 2 spaces (correct, CommonMark-style --
        # renders fine on GitHub/VS Code). Python-Markdown's default tab_length
        # is 4, so without this it silently flattens every nested list (the
        # per-test PASS/FAIL bullets, and the input/expected/actual bullets
        # nested under a failed open test) into one single-level list.
        rendered = markdown_lib.markdown(content, extensions=["fenced_code", "tables"], tab_length=2)
        rendered = _render_io_side_by_side(rendered)
        rendered = _preserve_inline_code_whitespace(rendered)
        return jsonify({"filename": filename, "kind": "markdown", "html": rendered})

    if filename.endswith(".csv"):
        rows = list(csv.reader(io.StringIO(content)))
        return jsonify({"filename": filename, "kind": "csv", "rows": rows})

    return jsonify(
        {"filename": filename, "kind": "text", "html": f"<pre>{html_escape.escape(content)}</pre>"}
    )


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------


@app.get("/")
def index():
    return send_from_directory(Path(__file__).resolve().parent / "templates", "index.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
