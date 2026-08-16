"""Load and validate question.yaml files into typed Question objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from grader.code_checks import CodeCheckConfigError, validate_construct_name

VALID_MATCHERS = {"exact_trim", "exact", "token", "float_tol", "custom"}
VALID_CODE_CHECK_MODES = {"gate", "bonus", "penalty", "line_gate"}
VALID_REQUIRE_MATCH = {"all", "any"}
# "eps" only means something for matcher type "float_tol"; the rest are
# composable normalization toggles layered on top of *any* type (except
# "custom", which bypasses them -- a custom checker already gets raw text and
# full control). See scorer.py get_matcher() for how they're applied.
VALID_MATCHER_OPTIONS = {
    "eps",
    "ignore_case",
    "ignore_whitespace",
    "ignore_punctuation",
    "allow_extra_output",
    "symbol_groups",
    "strict_lines",
}


class QuestionConfigError(Exception):
    """Raised when a question.yaml is missing, malformed, or internally inconsistent."""


@dataclass
class Limits:
    time_seconds: float = 2.0
    wall_seconds: float = 5.0
    memory_mb: int = 128
    output_bytes: int = 65536
    max_processes: int = 1


@dataclass
class TestCase:
    name: str
    input_text: str
    expected_text: str
    weight: float
    group: str  # "open" | "hidden"
    partial: bool = False  # award weight/N per matching line of expected output, instead of all-or-nothing
    ignore_typo: bool = False  # tolerate a 1-edit spelling slip in a word of the expected output (see scorer.match_with_typo_tolerance)


@dataclass
class Question:
    qid: str
    title: str
    description: str
    dir: Path
    filename_patterns: list[str]
    marks_open: float
    marks_hidden: float
    compile_standard: str
    compile_flags: list[str]
    limits: Limits
    matcher_type: str
    matcher_options: dict
    gold_path: Path
    tests: list[TestCase]
    checker_path: Path | None = None
    adjust_char_input: bool = False  # see grader.char_input_fix -- relaxes the classic missing-space-before-%c scanf mistake
    adjust_scanf_address: bool = False  # see grader.scanf_address_fix -- relaxes the classic missing-& scanf mistake
    code_checks_mode: str | None = None  # None | "gate" | "bonus" | "penalty" | "line_gate"
    code_checks_require: list[str] = field(default_factory=list)
    code_checks_require_match: str = "any"  # "any" (default) | "all"
    code_checks_forbid: list[str] = field(default_factory=list)
    code_checks_marks: float = 0.0  # only meaningful in "bonus" mode
    code_checks_penalty: float = 0.0  # only meaningful in "penalty" mode
    code_checks_line_gate: dict[int, str] = field(default_factory=dict)  # only meaningful in "line_gate" mode: 0-based output line index -> required construct name

    @property
    def total_marks(self) -> float:
        bonus = self.code_checks_marks if self.code_checks_mode == "bonus" else 0.0
        return self.marks_open + self.marks_hidden + bonus

    def tests_in(self, group: str) -> list[TestCase]:
        return [t for t in self.tests if t.group == group]


def _require(d: dict, key: str, ctx: str):
    if not isinstance(d, dict) or key not in d:
        raise QuestionConfigError(f"{ctx}: missing required key '{key}'")
    return d[key]


def _resolve_test_text(t: dict, inline_key: str, file_key: str, question_dir: Path, ctx: str) -> str:
    """A test's input/expected-output can be given inline (short, single-file
    authoring -- the common case) or as a path to a separate file (for large
    test data). Exactly one of the two must be present."""
    has_inline = inline_key in t
    has_file = file_key in t
    if has_inline and has_file:
        raise QuestionConfigError(f"{ctx}: specify either '{inline_key}' or '{file_key}', not both")
    if has_inline:
        return str(t[inline_key])
    if has_file:
        path = question_dir / t[file_key]
        if not path.exists():
            raise QuestionConfigError(f"{ctx}: {file_key} not found: {path}")
        return path.read_text()
    raise QuestionConfigError(f"{ctx}: must specify either '{inline_key}' or '{file_key}'")


def load_question(question_dir: Path) -> Question:
    question_dir = Path(question_dir)
    yaml_path = question_dir / "question.yaml"
    if not yaml_path.exists():
        raise QuestionConfigError(f"{question_dir}: no question.yaml found")

    with open(yaml_path) as f:
        raw = yaml.safe_load(f)
    ctx = str(yaml_path)

    qid = _require(raw, "id", ctx)
    if qid != question_dir.name:
        raise QuestionConfigError(
            f"{ctx}: id '{qid}' must match its containing folder name '{question_dir.name}'"
        )
    title = raw.get("title", qid)
    description = raw.get("description", "")

    filename_patterns = _require(raw, "filename_patterns", ctx)
    if not isinstance(filename_patterns, list) or not filename_patterns:
        raise QuestionConfigError(f"{ctx}: filename_patterns must be a non-empty list")

    marks = _require(raw, "marks", ctx)
    marks_open = float(_require(marks, "open", f"{ctx}.marks"))
    marks_hidden = float(_require(marks, "hidden", f"{ctx}.marks"))

    compile_cfg = raw.get("compile") or {}
    compile_standard = compile_cfg.get("standard", "c11")
    compile_flags = list(compile_cfg.get("flags", ["-O2", "-Wall"]))

    limits_cfg = raw.get("limits") or {}
    limits = Limits(
        time_seconds=float(limits_cfg.get("time_seconds", 2.0)),
        wall_seconds=float(limits_cfg.get("wall_seconds", 5.0)),
        memory_mb=int(limits_cfg.get("memory_mb", 128)),
        output_bytes=int(limits_cfg.get("output_bytes", 65536)),
        max_processes=int(limits_cfg.get("max_processes", 1)),
    )

    matcher_cfg = raw.get("matcher") or {"type": "exact_trim"}
    matcher_type = matcher_cfg.get("type", "exact_trim")
    if matcher_type not in VALID_MATCHERS:
        raise QuestionConfigError(
            f"{ctx}: unknown matcher type '{matcher_type}', expected one of {sorted(VALID_MATCHERS)}"
        )
    matcher_options = {k: v for k, v in matcher_cfg.items() if k != "type"}
    if matcher_type != "custom":
        for key in matcher_options:
            if key not in VALID_MATCHER_OPTIONS:
                raise QuestionConfigError(
                    f"{ctx}.matcher: unknown option '{key}', expected one of {sorted(VALID_MATCHER_OPTIONS)}"
                )
        if "symbol_groups" in matcher_options:
            # e.g. [["x", "X", "*"], [":", "="]] -- within each group, every
            # symbol is treated as interchangeable with that group's first
            # (canonical) symbol; groups are independent of each other. A
            # symbol in two groups would make "which one is canonical" for
            # that symbol ambiguous, so it's rejected at load time rather
            # than silently picking whichever group happens to run first.
            groups = matcher_options["symbol_groups"]
            if not isinstance(groups, list) or not groups:
                raise QuestionConfigError(
                    f"{ctx}.matcher.symbol_groups: must be a non-empty list of symbol groups"
                )
            seen_symbols: set[str] = set()
            for group in groups:
                if not isinstance(group, list) or len(group) < 2:
                    raise QuestionConfigError(
                        f"{ctx}.matcher.symbol_groups: each group must be a list of at least 2 "
                        f"interchangeable symbols, got {group!r}"
                    )
                for sym in group:
                    if not isinstance(sym, str) or not sym:
                        raise QuestionConfigError(
                            f"{ctx}.matcher.symbol_groups: symbols must be non-empty strings, got {sym!r}"
                        )
                    if sym in seen_symbols:
                        raise QuestionConfigError(
                            f"{ctx}.matcher.symbol_groups: symbol {sym!r} appears in more than one group"
                        )
                    seen_symbols.add(sym)

    # Unlike the checks above, this isn't skipped for matcher_type == "custom":
    # _score_partial_lines reads matcher_options.strict_lines regardless of
    # matcher type (see score_question in scorer.py), since `partial: true`
    # composes with a custom checker too.
    if "strict_lines" in matcher_options:
        # A line whose normalized text exactly matches one of these entries
        # is always held to _score_partial_lines' strictest rule for partial
        # credit -- it must equal an *entire* actual line, never just be
        # found via the more lenient digit-guarded substring search that
        # rule normally falls back to. Exists for questions with two
        # possible answers where one is a complete substring of the other
        # (e.g. "Coprime" is a substring of "Not Coprime") -- without
        # flagging it, the substring search would silently credit "Coprime"
        # against an actual "Not Coprime" (the opposite answer). See
        # _score_partial_lines in grader/scorer.py.
        strict_lines = matcher_options["strict_lines"]
        if not isinstance(strict_lines, list) or not strict_lines:
            raise QuestionConfigError(f"{ctx}.matcher.strict_lines: must be a non-empty list of strings")
        for line in strict_lines:
            if not isinstance(line, str) or not line:
                raise QuestionConfigError(
                    f"{ctx}.matcher.strict_lines: entries must be non-empty strings, got {line!r}"
                )

    checker_path = None
    if matcher_type == "custom":
        checker_path = question_dir / "checker.py"
        if not checker_path.exists():
            raise QuestionConfigError(
                f"{ctx}: matcher type 'custom' requires a checker.py in {question_dir}"
            )

    gold_path = question_dir / "gold.c"
    if not gold_path.exists():
        raise QuestionConfigError(f"{ctx}: gold.c not found in {question_dir}")

    adjust_char_input = bool(raw.get("adjust_char_input", False))
    adjust_scanf_address = bool(raw.get("adjust_scanf_address", False))

    tests_cfg = _require(raw, "tests", ctx)
    tests: list[TestCase] = []
    seen_names: set[str] = set()
    for group in ("open", "hidden"):
        for t in tests_cfg.get(group) or []:
            name = _require(t, "name", f"{ctx}.tests.{group}")
            if name in seen_names:
                raise QuestionConfigError(f"{ctx}: duplicate test name '{name}'")
            seen_names.add(name)
            test_ctx = f"{ctx}.tests.{group}.{name}"
            input_text = _resolve_test_text(t, "in", "in_file", question_dir, test_ctx)
            expected_text = _resolve_test_text(t, "out", "out_file", question_dir, test_ctx)
            weight = float(_require(t, "weight", test_ctx))
            partial = bool(t.get("partial", False))
            ignore_typo = bool(t.get("ignore_typo", False))
            tests.append(
                TestCase(
                    name=name, input_text=input_text, expected_text=expected_text,
                    weight=weight, group=group, partial=partial, ignore_typo=ignore_typo,
                )
            )

    if not tests:
        raise QuestionConfigError(f"{ctx}: no tests defined")

    # Fail fast on authoring mistakes -- a mismatched weight sum would otherwise
    # silently mis-score every student against this question.
    for group, declared in (("open", marks_open), ("hidden", marks_hidden)):
        group_tests = [t for t in tests if t.group == group]
        total_weight = sum(t.weight for t in group_tests)
        if group_tests and abs(total_weight - declared) > 1e-6:
            raise QuestionConfigError(
                f"{ctx}: sum of {group} test weights ({total_weight}) != marks.{group} ({declared})"
            )
        if not group_tests and declared > 0:
            raise QuestionConfigError(
                f"{ctx}: marks.{group}={declared} but no {group} tests are defined"
            )

    code_checks_mode = None
    code_checks_require: list[str] = []
    code_checks_require_match = "any"
    code_checks_forbid: list[str] = []
    code_checks_marks = 0.0
    code_checks_penalty = 0.0
    code_checks_line_gate: dict[int, str] = {}
    code_checks_cfg = raw.get("code_checks")
    if code_checks_cfg is not None:
        code_checks_mode = _require(code_checks_cfg, "mode", f"{ctx}.code_checks")
        if code_checks_mode not in VALID_CODE_CHECK_MODES:
            raise QuestionConfigError(
                f"{ctx}.code_checks: mode must be one of {sorted(VALID_CODE_CHECK_MODES)}, got '{code_checks_mode}'"
            )
        code_checks_require = list(code_checks_cfg.get("require") or [])
        code_checks_forbid = list(code_checks_cfg.get("forbid") or [])
        if not code_checks_require and not code_checks_forbid:
            raise QuestionConfigError(
                f"{ctx}.code_checks: at least one of 'require'/'forbid' must be non-empty"
            )
        for name in code_checks_require + code_checks_forbid:
            try:
                validate_construct_name(name)
            except CodeCheckConfigError as e:
                raise QuestionConfigError(f"{ctx}.code_checks: {e}")

        # "any" (default): satisfied once at least one required construct is
        # present. "all": every one must be present -- the only behavior
        # before this option existed, so a question needing several
        # independent constructs (e.g. bitwise AND for one part of the
        # problem, bitwise XOR for another) must now say so explicitly.
        code_checks_require_match = code_checks_cfg.get("require_match", "any")
        if code_checks_require_match not in VALID_REQUIRE_MATCH:
            raise QuestionConfigError(
                f"{ctx}.code_checks: require_match must be one of {sorted(VALID_REQUIRE_MATCH)}, "
                f"got '{code_checks_require_match}'"
            )

        # 'marks' (bonus) and 'penalty' are mode-specific and mutually exclusive --
        # each only makes sense for its own mode, so using the wrong key for the
        # configured mode is a load-time error rather than a silently-ignored key.
        if code_checks_mode == "bonus":
            code_checks_marks = float(_require(code_checks_cfg, "marks", f"{ctx}.code_checks"))
            if code_checks_marks <= 0:
                raise QuestionConfigError(f"{ctx}.code_checks: marks must be > 0 in 'bonus' mode")
        elif "marks" in code_checks_cfg:
            raise QuestionConfigError(
                f"{ctx}.code_checks: 'marks' is only used in 'bonus' mode "
                f"(mode is '{code_checks_mode}') -- remove it or switch mode"
            )

        if code_checks_mode == "penalty":
            code_checks_penalty = float(_require(code_checks_cfg, "penalty", f"{ctx}.code_checks"))
            if code_checks_penalty <= 0:
                raise QuestionConfigError(f"{ctx}.code_checks: penalty must be > 0 in 'penalty' mode")
        elif "penalty" in code_checks_cfg:
            raise QuestionConfigError(
                f"{ctx}.code_checks: 'penalty' is only used in 'penalty' mode "
                f"(mode is '{code_checks_mode}') -- remove it or switch mode"
            )

        # "line_gate": ties a specific required construct to a specific
        # 0-based line of a `partial: true` test's expected output, instead
        # of gate/bonus/penalty acting on the question's marks as a whole.
        # A line only earns its share of partial credit when its text
        # matches *and* its gated construct was found in the source -- e.g.
        # q06_bitwise_playground's line 0 (odd/even) requires bitwise_and,
        # line 1 (case swap) requires bitwise_xor, so a student who gets
        # byte-correct output via `%` instead of `&` loses only that line's
        # marks, not the whole test's, even though `partial: true` is what
        # makes per-line credit possible in the first place.
        if code_checks_mode == "line_gate":
            line_gate_cfg = _require(code_checks_cfg, "line_gate", f"{ctx}.code_checks")
            if not isinstance(line_gate_cfg, dict) or not line_gate_cfg:
                raise QuestionConfigError(
                    f"{ctx}.code_checks: 'line_gate' must be a non-empty mapping of "
                    f"0-based output line index -> construct name in 'line_gate' mode"
                )
            for raw_idx, construct in line_gate_cfg.items():
                try:
                    idx = int(raw_idx)
                except (TypeError, ValueError):
                    raise QuestionConfigError(
                        f"{ctx}.code_checks.line_gate: keys must be integers "
                        f"(0-based output line index), got {raw_idx!r}"
                    )
                if idx < 0:
                    raise QuestionConfigError(
                        f"{ctx}.code_checks.line_gate: line index must be >= 0, got {idx}"
                    )
                if construct not in code_checks_require:
                    raise QuestionConfigError(
                        f"{ctx}.code_checks.line_gate: construct '{construct}' (line {idx}) must "
                        f"also appear in 'require' -- the line-gate check reuses its scan results"
                    )
                code_checks_line_gate[idx] = construct
        elif "line_gate" in code_checks_cfg:
            raise QuestionConfigError(
                f"{ctx}.code_checks: 'line_gate' is only used in 'line_gate' mode "
                f"(mode is '{code_checks_mode}') -- remove it or switch mode"
            )

    return Question(
        qid=qid,
        title=title,
        description=description,
        dir=question_dir,
        filename_patterns=filename_patterns,
        marks_open=marks_open,
        marks_hidden=marks_hidden,
        compile_standard=compile_standard,
        compile_flags=compile_flags,
        limits=limits,
        matcher_type=matcher_type,
        matcher_options=matcher_options,
        gold_path=gold_path,
        tests=tests,
        checker_path=checker_path,
        adjust_char_input=adjust_char_input,
        adjust_scanf_address=adjust_scanf_address,
        code_checks_mode=code_checks_mode,
        code_checks_require=code_checks_require,
        code_checks_require_match=code_checks_require_match,
        code_checks_forbid=code_checks_forbid,
        code_checks_marks=code_checks_marks,
        code_checks_penalty=code_checks_penalty,
        code_checks_line_gate=code_checks_line_gate,
    )


def load_all_questions(questions_dir: Path) -> list[Question]:
    questions_dir = Path(questions_dir)
    questions = []
    for child in sorted(questions_dir.iterdir()):
        if child.is_dir() and (child / "question.yaml").exists():
            questions.append(load_question(child))
    if not questions:
        raise QuestionConfigError(f"No questions found under {questions_dir}")
    return questions
