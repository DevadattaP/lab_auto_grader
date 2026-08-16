#!/usr/bin/env python3
"""CLI entry point wiring discovery -> sandboxed compile/run -> scoring -> reports.

Usage:
    python -m grader.grade \
        --questions questions/lab_01 \
        --submissions submissions/lab_01 \
        --out runs/lab_01 \
        --sandbox isolate \
        --parallel 4
        --check-gold
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from grader.config_schema import Question, QuestionConfigError, load_all_questions
from grader.discover import (
    Student,
    StudentMappingError,
    discover_students,
    find_question_file,
    load_overrides,
    load_student_mapping,
)
from grader.report import render_student_report, write_grade_feedback_md, write_summary_csv
from grader.runner import grade_submission, self_check_gold
from grader.sandbox import Sandbox, choose_sandbox_kind, make_sandbox
from grader.scorer import QuestionScore, get_test_matcher, score_question

log = logging.getLogger("grader")


def _setup_logging(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(run_dir / "run.log")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    console_handler.setLevel(logging.INFO)

    log.addHandler(file_handler)
    log.addHandler(console_handler)


def _run_gold_self_checks(sandbox: Sandbox, questions: list[Question], scratch_root: Path) -> None:
    failures: dict[str, list[str]] = {}
    for q in questions:
        tmp_binary = scratch_root / "gold" / q.qid / "a.out"
        tmp_binary.parent.mkdir(parents=True, exist_ok=True)
        failing = self_check_gold(sandbox, q, tmp_binary)
        if failing:
            failures[q.qid] = failing
        else:
            log.info("Gold self-check OK for %s", q.qid)
    if failures:
        for qid, names in failures.items():
            log.error("Gold self-check FAILED for %s: %s", qid, ", ".join(names))
        raise SystemExit(
            "Aborting: the gold solution does not pass its own tests for "
            f"{list(failures)}. Fix the question definition before grading students."
        )


def _grade_student_question(
    sandbox: Sandbox,
    student: Student,
    question: Question,
    run_dir: Path,
) -> tuple[QuestionScore, list[str]]:
    anomalies: list[str] = []
    match = find_question_file(student.folder, question.filename_patterns)
    if match.anomaly == "MISSING":
        anomalies.append(f"{question.qid}: submission file not found")
        return (
            QuestionScore(
                qid=question.qid,
                title=question.title,
                marks_earned=0.0,
                marks_total=question.total_marks,
                compile_ok=False,
                compile_stderr="",
                not_submitted=True,
            ),
            anomalies,
        )
    if match.anomaly == "MULTIPLE_CANDIDATES":
        alt_names = ", ".join(p.name for p in match.alternates)
        anomalies.append(f"{question.qid}: multiple candidate files found ({match.path.name} chosen over {alt_names})")

    binary_dest = run_dir / "raw" / student.roll_no / question.qid / "a.out"
    matcher = get_test_matcher(question)
    result = grade_submission(sandbox, question, match.path, binary_dest, matcher)
    return score_question(result), anomalies


_worker_sandbox: Sandbox | None = None
_worker_questions: list[Question] | None = None
_worker_run_dir: Path | None = None
_worker_only_question: str | None = None


def _pool_init(sandbox_kind, counter, lock, scratch_root, questions, run_dir, only_question):
    global _worker_sandbox, _worker_questions, _worker_run_dir, _worker_only_question
    with lock:
        box_id = counter.value
        counter.value += 1
    _worker_sandbox = make_sandbox(sandbox_kind, box_id, scratch_root)
    _worker_questions = questions
    _worker_run_dir = run_dir
    _worker_only_question = only_question


def _pool_grade_student(student: Student):
    scores_by_qid: dict[str, QuestionScore] = {}
    anomalies: list[str] = list(
        ["folder name did not look like a roll number"] if student.folder_anomaly == "UNRECOGNIZED_FOLDER" else []
    )
    for q in _worker_questions:
        if _worker_only_question and q.qid != _worker_only_question:
            continue
        qs, q_anomalies = _grade_student_question(_worker_sandbox, student, q, _worker_run_dir)
        scores_by_qid[q.qid] = qs
        anomalies.extend(q_anomalies)
    return student.roll_no, scores_by_qid, anomalies


def main() -> None:
    parser = argparse.ArgumentParser(description="Sandboxed C-program auto-grader")
    parser.add_argument("--questions", required=True, type=Path,
                        help="Directory containing question folders with gold.c and tests")
    parser.add_argument("--submissions", type=Path, default=None,
                        help="Directory containing student submission folders (not needed if --check-gold is set)")
    parser.add_argument("--out", required=True, type=Path,
                        help="Directory to write reports and logs into")
    parser.add_argument("--sandbox", choices=["auto", "isolate", "subprocess"], default="isolate",
                        help="Which sandbox backend to use (default 'isolate', fallback to 'subprocess' if isolate is not usable)")
    parser.add_argument("--parallel", type=int, default=1,
                        help="Number of parallel sandbox workers to use (default 1)")
    parser.add_argument("--only-question", default=None,
                        help="If set, only grade this question ID (for debugging)")
    parser.add_argument("--only-student", default=None,
                        help="If set, only grade this student roll number (for debugging)")
    parser.add_argument("--overrides", type=Path, default=None,
                        help="Optional JSON file with overrides for student roll numbers and question configs")
    parser.add_argument("--student-csv", type=Path, default=None,
                        help=("Optional CSV mapping roll_no -> name/ip (columns: roll_no, name[, ip], any "
                              "order) used to add 'ip'/'name' columns to summary.csv. Defaults to "
                              "ip_student_mapping.csv under --submissions if that file exists."))
    parser.add_argument("--dry-run", action="store_true", 
                        help="Discover students and questions, but do not compile or run anything")
    parser.add_argument("--check-gold", action="store_true",
                        help=("Compile+run every gold.c against its own tests and exit -- no "
                              "--submissions needed, no students touched. Use this to sanity-check "
                              "a question bank on its own before any submissions exist."))
    args = parser.parse_args()

    if args.check_gold and args.dry_run:
        parser.error("--check-gold and --dry-run are mutually exclusive")
    if not args.check_gold and args.submissions is None:
        parser.error("--submissions is required unless --check-gold is set")

    run_dir = args.out / datetime.now().strftime("%Y-%m-%d_%H%M")
    _setup_logging(run_dir)

    log.info("Loading questions from %s", args.questions)
    try:
        questions = load_all_questions(args.questions)
    except QuestionConfigError as e:
        log.error("Question config error: %s", e)
        raise SystemExit(1)
    log.info("Loaded %d question(s): %s", len(questions), ", ".join(q.qid for q in questions))

    questions_to_check = (
        [q for q in questions if q.qid == args.only_question] if args.only_question else questions
    )
    if args.only_question and not questions_to_check:
        raise SystemExit(f"--only-question {args.only_question} matched no loaded question")

    if args.check_gold:
        scratch_root = Path(tempfile.mkdtemp(prefix="grader_scratch_"))
        try:
            sandbox_kind = choose_sandbox_kind(args.sandbox, log=log)
            bootstrap_sandbox = make_sandbox(sandbox_kind, box_id=0, scratch_root=scratch_root)
            _run_gold_self_checks(bootstrap_sandbox, questions_to_check, scratch_root)
            log.info("All gold solutions pass their own tests. No submissions were touched.")
        finally:
            shutil.rmtree(scratch_root, ignore_errors=True)
        return

    overrides = load_overrides(args.overrides)
    students = discover_students(args.submissions, overrides)
    log.info("Discovered %d student folder(s)", len(students))
    for s in students:
        if s.folder_anomaly:
            log.warning("Folder anomaly: %s (%s)", s.folder.name, s.folder_anomaly)

    student_csv_path = args.student_csv or (args.submissions / "ip_student_mapping.csv")
    try:
        student_mapping = load_student_mapping(student_csv_path)
    except StudentMappingError as e:
        log.error("Student CSV error: %s", e)
        raise SystemExit(1)
    if student_mapping.entries:
        log.info(
            "Loaded student mapping from %s (%d entr%s, columns: %s)",
            student_csv_path,
            len(student_mapping.entries),
            "y" if len(student_mapping.entries) == 1 else "ies",
            ", ".join(c for c, present in (("ip", student_mapping.has_ip), ("name", student_mapping.has_name)) if present),
        )
    elif args.student_csv is not None:
        log.warning(
            "Student CSV %s not found or has no usable rows -- summary.csv will not be enriched",
            student_csv_path,
        )

    if args.dry_run:
        log.info("Dry run complete -- no compiling or execution performed.")
        return

    scratch_root = Path(tempfile.mkdtemp(prefix="grader_scratch_"))
    try:
        sandbox_kind = choose_sandbox_kind(args.sandbox, log=log)

        bootstrap_sandbox = make_sandbox(sandbox_kind, box_id=0, scratch_root=scratch_root)
        _run_gold_self_checks(bootstrap_sandbox, questions_to_check, scratch_root)

        if args.only_student:
            students = [s for s in students if s.roll_no == args.only_student]
            if not students:
                raise SystemExit(f"--only-student {args.only_student} matched no discovered folder")

        results: list[tuple[str, dict[str, QuestionScore], list[str]]] = []

        if args.parallel <= 1:
            global _worker_sandbox, _worker_questions, _worker_run_dir, _worker_only_question
            _worker_sandbox = bootstrap_sandbox
            _worker_questions = questions
            _worker_run_dir = run_dir
            _worker_only_question = args.only_question
            for student in students:
                log.info("Grading %s", student.roll_no)
                results.append(_pool_grade_student(student))
        else:
            manager = multiprocessing.Manager()
            counter = manager.Value("i", 1)  # box 0 already used by bootstrap_sandbox
            lock = manager.Lock()
            with multiprocessing.Pool(
                processes=args.parallel,
                initializer=_pool_init,
                initargs=(sandbox_kind, counter, lock, scratch_root, questions, run_dir, args.only_question),
            ) as pool:
                for roll_no, scores_by_qid, anomalies in pool.imap_unordered(_pool_grade_student, students):
                    log.info("Graded %s", roll_no)
                    results.append((roll_no, scores_by_qid, anomalies))

        results.sort(key=lambda r: r[0])

        question_ids = [q.qid for q in questions]
        for roll_no, scores_by_qid, anomalies in results:
            report_text = render_student_report(roll_no, list(scores_by_qid.values()), anomalies)
            (run_dir / f"report_{roll_no}.md").write_text(report_text)

        write_summary_csv(run_dir / "summary.csv", question_ids, results, student_mapping)
        write_grade_feedback_md(run_dir / "grade_feedback.md", question_ids, results, student_mapping)
        log.info("Run complete. Reports written to %s", run_dir)
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)


if __name__ == "__main__":
    main()
