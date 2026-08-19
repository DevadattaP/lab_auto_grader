# C Programming Auto-Grader — System Design

## 1. Problem statement

Given a folder of student submissions (one subfolder per roll number, each containing one `.c` file per question), automatically compile, run against gold-defined test cases, score, and produce a per-student report — safely, i.e. without a buggy or malicious student program being able to hang, fork-bomb, or damage the grading machine.

## 2. Directory layout

Everything below lives under `lab_auto_grader/`, which is *outside* the extracted submissions zip. Nothing is ever written back into the submissions folder.

`questions/`, `submissions/`, and `runs/` are each subdivided **per lab** (`lab_01`, `lab_02`, ...), since there are multiple labs over a course and grading is run once per lab, manually, after that lab's submission deadline — not a single flat pool of everything. The three trees are always kept in lockstep by lab name (`questions/lab_01` pairs with `submissions/lab_01` and `runs/lab_01`), which is what makes `--questions questions/<lab> --submissions submissions/<lab> --out runs/<lab>` (§9) a single pattern that covers every lab without the tool itself needing any notion of "lab":

```bash
lab_auto_grader/
├── AUTOGRADER_DESIGN.md          # this file
├── grader/                       # the tool itself
│   ├── grade.py                  # CLI entry point
│   ├── runner.py                 # sandboxed compile+execute
│   ├── discover.py               # find student folders / files
│   ├── scorer.py                 # compare output, apply marks
│   ├── report.py                 # per-student + summary report generation
│   └── config_schema.py          # validates question YAML/JSON
├── questions/                    # one folder per lab, then one folder per question
│   └── lab_01/
│       ├── q1_result_grade/
│       │   ├── question.yaml     #   metadata + all test cases inline (in/out, see §3)
│       │   └── gold.c            #   reference/gold solution
│       └── q2_.../               #   a question with large test data may add a tests/ dir and use in_file/out_file instead (§3)
├── submissions/                  # <- one folder per lab; each holds the extracted zip
│   └── lab_01/                   #    (input, read-only) -- not committed, see .gitignore
├── runs/                         # one folder per lab, then one timestamped folder per run
│   └── lab_01/
│       └── 2026-08-03_1500/
│           ├── raw/<rollno>/<qid>/   # compiled binary, stdout, stderr, exit code, timing per test
│           ├── report_<rollno>.md    # per-student human-readable report
│           ├── summary.csv           # one row per student, one column per question + total
│           └── run.log
├── .gitignore
├── README.md
└── requirements.txt
```

Keeping `questions/` separate from `submissions/` means each lab's question bank can be authored and reviewed well before that lab's submissions exist, and a grading run never writes back into the submissions folder. Adding `lab_02` is just creating `questions/lab_02/` (author questions ahead of time), then dropping the extracted zip into `submissions/lab_02/` once students have submitted — `runs/lab_02/` is created automatically on first grading run.

## 3. Question definition format

One YAML file per question, e.g. `questions/lab_01/q1_result_grade/question.yaml`. Each test's input/expected-output is written **inline**, directly in the YAML — no separate `.in`/`.out` file pair per test case. For ~13 tests that's the difference between authoring 1 file and 26; multi-line values just use a YAML block scalar (`|`), so this isn't limited to one-liners:

```yaml
id: q1_result_grade
title: "Pass/Fail/Invalid classifier"
description: "Write a C program that reads marks and prints PASS/FAIL/INVALID."
filename_patterns:            # how to find this question's file inside a student folder
  - "q1.c"
  - "question1.c"
  - "Q1.c"
marks:
  open: 20                    # total marks distributed across open tests (visible to students)
  hidden: 80                  # total marks distributed across hidden tests
compile:
  standard: c11
  flags: ["-O2", "-Wall"]
limits:
  time_seconds: 2
  wall_seconds: 5
  memory_mb: 128
  output_bytes: 65536
  max_processes: 1            # forbid fork() spawning further processes
matcher:
  type: exact_trim             # exact_trim | exact | token | float_tol | custom
  # exact_trim: strip trailing whitespace/newlines per line before comparing
tests:
  open:
    - name: "sample_pass"
      in: "75"
      out: "PASS"
      weight: 4
    - name: "sample_fail"
      in: "35"
      out: "FAIL"
      weight: 4
    - name: "sample_invalid_high"
      in: "105"
      out: "INVALID"
      weight: 4
    - name: "sample_invalid_low"
      in: "-5"
      out: "INVALID"
      weight: 4
    - name: "sample_boundary_40"
      in: "40"
      out: "PASS"
      weight: 4
  hidden:
    - name: "boundary_0"
      in: "0"
      out: "FAIL"
      weight: 10
    - name: "boundary_100"
      in: "100"
      out: "PASS"
      weight: 10
    # ... six more hidden tests, same shape
```

**Large test data**: for a test whose input or expected output is too big to comfortably inline (a large array, a multi-KB matrix), use `in_file`/`out_file` instead of `in`/`out` — a path relative to the question's folder, read as plain text at load time. Exactly one of `in`/`in_file` must be given per test (same for `out`/`out_file`); mixing both on the same test, or giving neither, is a load-time error.

Sum of weights within `open` must equal `marks.open`; same for `hidden`. The grader validates this at load time and refuses to run if they don't match (fail fast on authoring mistakes, not silently mis-score 35 students).

`gold.c` is compiled and run through the same harness once at load time, purely as a **sanity check** — if the gold program itself doesn't score 100%, the grader aborts and tells the instructor which test is wrong, before any student is touched.

**Per-line partial credit** (`partial: true` on a test entry): a test case is normally all-or-nothing — output matches or it doesn't. Setting `partial: true` instead awards `weight / N` for each matching line of expected output, where `N` is the number of lines in that test's `out`. This exists for a test whose lines check independent things — e.g. `q6_bitwise_playground`'s odd/even line and case-toggle line are unrelated computations sharing one test case purely because they're read from the same two lines of input; getting only one right shouldn't be indistinguishable from getting neither right. Line comparison uses the same normalization as the `exact_trim` matcher (rstrip each line, ignore trailing blank lines) regardless of the question's configured `matcher` — the two are independent knobs, since `matcher` governs whether a test **fully** passes and `partial` only kicks in once it hasn't. A test that fully matches earns full `weight` either way; `partial` only changes what happens when it *doesn't* fully match. It only applies to a completed run (`status: OK`) — a `TIMEOUT`/`RUNTIME_ERROR`/etc. earns nothing from it, since "some lines were right" doesn't excuse the run failing abnormally. Report output makes a partially-credited test explicit rather than folding it into a plain FAIL: `` `t1`: PARTIAL (1/2 lines, +5/10 marks) ``, with the usual input/expected/actual detail underneath.

**Per-test typo tolerance** (`ignore_typo: true` on a test entry): grading a real (non-synthetic) batch of student submissions surfaced students losing all of a test's marks over a single misspelled word in an otherwise letter-perfect, logically-correct `printf` — `aperator` for `operator`, `paymeny`/`paymetn` for `payment`. `ignore_typo` (implemented in `scorer.match_with_typo_tolerance`, `grader/scorer.py`) is tried only as a fallback once that test's normal matcher has already failed: it compares expected vs actual line-by-line and word-by-word (same line count and word count required — this forgives a misspelling, not a missing/extra word), each word pair matching exactly or within one edit under `scorer._edit_distance` -- Damerau-Levenshtein (restricted/OSD variant): insertion, deletion, substitution, *or* one adjacent-letter swap, each costing 1, since a fat-finger transposition (`PAYMETN` for `PAYMENT`) is the single most common typo shape and is 2 edits under plain Levenshtein, which would put it outside the threshold entirely. Skips words shorter than 4 characters (a 1-edit "typo" on `no`/`ok` is really just a different word) and never fuzzes a word that contains a digit (a wrong computed answer, e.g. actual `43` for expected `42`, is also "1 edit away" and must never be excused as a typo). It's deliberately a per-test-case flag, like `partial`, rather than a question-wide `matcher:` option — the failure mode is confined to whichever specific test's expected output happens to contain a word students are prone to misspelling (e.g. `INVALID OPERATOR`), and most tests on a question don't need it.

Unlike `partial`'s line-normalization, `ignore_typo` does need to compose with the question's other enabled tolerances (`scorer._typo_tolerant_match`) — a student can easily have both a case slip and a misspelling in the same line (expected `INVALID OPERATOR`, actual `invalid aperator`, question has `ignore_case: true`), and matching only against raw text would catch neither. Before the word-level typo comparison, `_typo_tolerant_match` applies whichever of `ignore_case`/`ignore_punctuation`/`symbol_groups` the question has enabled (never `ignore_whitespace`, which strips *all* whitespace and would destroy the word boundaries the typo check splits on). This changed `grader/runner.grade_submission`'s injected-matcher signature from `(expected, actual, test_input) -> bool` to `(test: TestCase, actual) -> (bool, list[str])` (via `scorer.get_test_matcher`, wrapping the existing `get_matcher` question-level matcher) — the base `MatcherFn` type (used by custom checkers and the composable `matcher` options themselves) is unchanged. See "Naming which tolerance rescued a test" below for what the `list[str]` is for.

### 3.1 Code construct checks (`code_checks`)

Output matching alone is the wrong tool for a question whose actual point is "practice using a switch statement" or "practice bitwise operators" — a student can get full marks by writing the same logic with `if`/`else` or `%`, learning nothing the exercise intended. `code_checks` (in `grader/code_checks.py`) is a static check, independent of test correctness, on whether specific C constructs actually appear in the submitted source — or, just as importantly for questions like `lab_quest/W01/bitwise_Q.txt` ("You are NOT allowed to use %, if-else, or ctype.h functions"), that specific constructs *don't* appear.

**The core problem it has to get right**: a naive text search for `%` to detect modulo usage would falsely flag `printf("%d %c", n, ch)` — the format specifier, not an operator. Before any pattern is checked, `strip_comments_and_literals` walks the source once and blanks out comment bodies and the *contents* of `"..."`/`'...'` literals (honoring `\"`/`\'` escapes), preserving line numbers and structure but not their content. Every check below runs against this cleaned text, never the raw source.

```yaml
code_checks:
  mode: gate                # "gate", "bonus", "penalty", or "line_gate" -- see below
  require: [bitwise_and, bitwise_xor]
  require_match: all        # "any" (default) or "all" -- see below
  forbid: [modulo, if_else, "function_call:toupper", "function_call:tolower"]
```

**`require_match` controls how `require` is satisfied** — `any` (the default) needs at least one of the listed constructs present; `all` needs every one of them. `forbid` has no equivalent setting: a single forbidden construct found anywhere is always a violation, unconditionally — there's only one sensible way to interpret "don't use X, Y, or Z". Get `require_match` wrong and a question either passes submissions it shouldn't (`any` when `all` was meant — e.g. `q6_bitwise_playground` genuinely needs *both* `bitwise_and` for Part A and `bitwise_xor` for Part B, two independent requirements, not alternatives, so it must set `require_match: all` explicitly) or rejects ones it shouldn't (`all` when `any` was meant — e.g. "use a loop" where `for_loop` or `while_loop` either one should count, not both). Default to `any` and only reach for `all` when the constructs are genuinely independent requirements rather than acceptable alternatives.

**Built-in construct names**: `switch`, `case`, `if`, `else`, `if_else` (either), `for_loop`, `while_loop`, `do_while` (`do`, unambiguous like the other keyword-based constructs since `do` is reserved in C and can't be an identifier), `modulo` (`%`), `ternary` (`?`, reliable in C since `?` has no other legitimate meaning once literals are stripped), `bitwise_and` (`&`), `bitwise_or` (`|`), `bitwise_xor` (`^`), `bitwise_not` (`~`), `bitwise_shift_left` (`<<`), `bitwise_shift_right` (`>>`), `function_def_used` (satisfied when the source defines *any* function, name unspecified, that's reachable from `main` through a chain of real calls — see §3.1.1 — for a question that wants "write and use your own function" without fixing its name). `function_call:<name>` matches a call to any named function (e.g. `function_call:toupper`). `regex:<pattern>` is a raw escape hatch for anything not covered by the built-in vocabulary, run directly against the cleaned source — the same "named built-ins plus a raw escape hatch" pattern already used for `matcher: custom` (§6). Unknown construct names are a load-time `QuestionConfigError`, same fail-fast philosophy as the weight-sum check above — a typo'd construct name should never silently never-match for an entire batch.

**`bitwise_and`/`bitwise_or` are a heuristic, not a parse.** C reuses `&` for both bitwise-AND and the address-of operator (`|` has no such ambiguity but is handled the same way for symmetry). Distinguishing them without a real parser means judging by what precedes the operator: preceded by an identifier/number/`)`/`]` (`n & 1`) reads as binary; preceded by an operator, `(`, `,`, or the start of an expression (`&n`) reads as unary. This matches reliably on the compact, single-expression style typical of introductory code (confirmed against the real `bitwise.c` — `scanf("%d %c", &n, &ch)`'s two address-of calls are correctly excluded while `(n & 1)` is correctly detected), but unusual formatting could in principle evade it. Every other built-in construct above is unambiguous by construction and has no such caveat.

### 3.1.1 Name-agnostic "defined and used a function" check (`function_def_used`)

`function_call:<name>` only works when the question can name the exact function a student must write — useless for a question whose actual point is "practice defining and calling your own function," where the name is deliberately left up to the student. `function_def_used` is a `code_checks` construct built for that case: it's satisfied when the source defines *any* function (excluding `main`) that's actually reachable from `main` through a chain of real calls — no name to configure.

**Detecting a definition, not a call.** A function *definition* has a shape no call ever has: `name(...) {` — a signature immediately followed by a body. A declaration/prototype ends in `;` instead; a call to a library function (`printf(...)`) never has a body supplied in student source at all. So `_FUNC_SIG_RE` (`grader/code_checks.py`) just looks for that shape, run against the same comment/string-literal-stripped source every other `code_checks` construct uses. Two exclusions keep this from producing false positives: C's control-flow keywords (`if`, `for`, `while`, `switch`, ...), which share the exact same textual shape (`if (x) {` parses identically to `name(...) {`) but can't legally be identifiers; and a signature match whose line starts a preprocessor directive (`#define NAME(x) { ... }`, a single-line macro using the brace-bodied idiom) — otherwise a macro with no compiled body of its own would be mistaken for a real function. `main` itself is tracked (it's needed as the call graph's root, below) but is never itself a `function_def_used` match.

**Detecting "used": a call graph, not a flat text search.** The first version of this check asked only "does this name appear as a call anywhere in the file" — which incorrectly credited a function that calls only itself recursively (no caller anywhere else in the file, but the recursive call site itself satisfied the flat search), and would equally have credited a chain of two functions that call each other but that `main` never reaches. The fix: build an actual caller → callee graph and only count a function as "used" if it's reachable from `main`.

1. `_matching_brace` finds each definition's body span by counting `{`/`}` from the opening brace. This is **exact, not a heuristic** — the source has already had comment bodies and the *contents* of string/char literals blanked out (`strip_comments_and_literals`, above), which removes the only things in C that could fake a brace (a `'{'` char literal, a `"}"` inside a string), so plain depth-counting on the cleaned text can't be fooled by anything that would actually compile.
2. `_find_all_function_defs` collects every definition this way, `main` included this time.
3. `_calls_in_span` finds a body's outgoing call edges: for each other known function name, a call site is a `name(...)` whose arguments don't look like a declaration's parameter-type list (`int add(int a, int b);` is filtered out by checking whether the parenthesized content starts with a C type keyword — this is what tells a lone prototype apart from an actual call `add(2, 3)`, both of which can end in `;`).
4. `_reachable_from_main` is a plain BFS over that graph starting at `main`.

A function only satisfies `function_def_used` if it's in the BFS's visited set. `main` calling `wrapper` calling `square` credits `square` no matter how many hops deep; a function that only calls itself, or a pair of functions that only call each other, are both correctly excluded, since neither is ever visited starting from `main`.

**Known, documented limitations** (not solved here):

- A call made through a function pointer (`op = add; op(x, y);`) creates no textual `add(` at the call site, so it's invisible to this graph. This is **not** actually a parsing gap a real AST would close for free — resolving which function a pointer variable points to is a data-flow/points-to problem, a level beyond what parsing alone gives you, so this limitation would survive a straight libclang port too, not just the regex version.
- A `#define NAME(x) { ... }` macro body that spans multiple physical lines via a `\` continuation isn't caught by the directive-line guard (which only checks the line the signature-shaped match starts on) — rare in introductory student code, not chased further here.
- Nested calls as arguments (`add(square(2), 3)`) are resolved via `_calls_in_span`'s regex, which handles one level of paren-nesting inside an argument list (enough for `add(square(2), 3)`) but not two (`add(square(cube(2)), 3)`).

A `libclang`-based version of this (and every other `code_checks` construct) now exists, per §3.1.2 below, and is what actually runs by default — falling back to the regex implementation above whenever it can't be trusted for a given file.

### 3.1.2 AST-based checks via `libclang`, with regex fallback

Every construct in §3.1/§3.1.1 has a second implementation in `grader/code_checks_ast.py`, using `libclang` (`pip install libclang` — ships a prebuilt `libclang.so`, no system package needed) to parse the student's actual source into a real Clang AST and check node kinds directly, instead of pattern-matching text. This is what `run_code_checks` tries **first**; the regex implementation is now purely the fallback, used per-construct (or for a whole file) whenever the AST path can't be trusted — never a hard failure, since a beginner's C file is exactly the kind of source most likely to trip up a stricter parser in some edge case the regex heuristics happen to tolerate.

**What the AST buys over the regex heuristics, concretely:**

- `bitwise_and`/`bitwise_or` stop being a heuristic entirely. `n & 1` is a `BINARY_OPERATOR` cursor; `&n` (address-of) is a completely different cursor kind, `UNARY_OPERATOR` — no more "judge by what precedes the operator" guessing, and no "unusual formatting could in principle evade it" caveat.
- The whole reason `strip_comments_and_literals` exists — `printf("%d")`'s `%` looking like modulo — doesn't apply: a string literal is a `STRING_LITERAL` token in the AST, never mistaken for an operator, so the AST path parses the *original* source directly rather than the comment/literal-blanked `cleaned` text the regex path needs.
- `function_def_used`'s reachability graph is built from the compiler's own understanding of each call (a `CALL_EXPR` cursor's `.spelling`, resolved against the file's real `FUNCTION_DECL` definitions) rather than a regex-approximated one — immune to the regex version's one-level nested-call-argument limit and its `#define`-continuation blind spot (a call made *through* a function-pointer *variable* is still invisible to this too, same as the regex version, for the same reason: that's a data-flow problem neither approach solves by parsing alone).

**Construct → AST mapping** (`code_checks_ast.detect`): `switch`/`case`/`if`/`for_loop`/`while_loop`/`do_while`/`ternary` are each a single unambiguous cursor kind (`SWITCH_STMT`/`CASE_STMT`/`IF_STMT`/`FOR_STMT`/`WHILE_STMT`/`DO_STMT`/`CONDITIONAL_OPERATOR`). `else` and `if_else` derive from an `IF_STMT`'s children: `[cond, then]` (2 children) means no `else`, `[cond, then, else]` (3) means there is one, and the reported line is the `else`-branch child's own location, not the `if`'s. `modulo`/`bitwise_xor`/`bitwise_and`/`bitwise_or`/`bitwise_shift_left`/`bitwise_shift_right` check a `BINARY_OPERATOR`/`COMPOUND_ASSIGNMENT_OPERATOR` cursor's actual operator spelling (derived from tokens — `clang.cindex` doesn't expose it as a plain string property — by taking whatever token(s) sit between the two operands' own token spans) against an exact spelling set per construct, e.g. `{"&", "&="}` for `bitwise_and`; this is a closed set, not a `startswith` check, specifically because `&&`/`||` also start with `&`/`|` and must not match. `bitwise_not` is a `UNARY_OPERATOR` with operator `~` (always prefix in C, unlike `++`/`--`, so no prefix/postfix disambiguation is needed). `function_call:<name>` is a `CALL_EXPR` cursor whose `.spelling` equals `<name>`. `function_def_used` is the graph/BFS from §3.1.1, rebuilt on `FUNCTION_DECL`/`CALL_EXPR` cursors instead of regex spans. `regex:<pattern>` has **no** AST mapping by design — it's explicitly a raw-pattern escape hatch, not a named language construct, so `run_code_checks` never even attempts the AST path for it.

**Parsing setup.** `code_checks_ast.build_context` parses with `-std=<compile.standard>` (now threaded through `run_code_checks(..., compile_standard=...)` from `runner.grade_submission`, which already knew `question.compile_standard` for the real `gcc` compile) and, if a system `clang`/`clang-N` binary is found on `PATH`, adds `-resource-dir=<that clang's resource dir>`. Without it, `pip install libclang` (which ships only `libclang.so`, not a full toolchain) produces a **fatal** `'stddef.h' file not found` diagnostic parsing anything that `#include`s glibc's `<stdio.h>` — confirmed by hand that this alone doesn't actually corrupt the resulting AST for ordinary intro-C student code (no `stddef`/`stdarg`-dependent constructs), but pointing at a real resource directory avoids relying on that, and costs nothing when a system clang happens to be present (as it was in testing, `clang-18`, matching the installed `libclang` 18.1.1).

**The completeness gate.** A parse succeeding with zero diagnostics doesn't by itself prove the AST is trustworthy for what we're about to check. `build_context` cross-checks the AST's set of top-level `FUNCTION_DECL` definitions against `_find_all_function_defs`'s regex-based result (§3.1.1's already-proven-reliable scanner) for the *same* file — if the AST is missing a definition the regex scan found, that's treated as an untrustworthy parse (e.g. some resource-dir/include problem silently dropped part of the program) and raises rather than returning quietly-incomplete results. The AST is free to find *more* than the regex scan (it's the more capable of the two — e.g. it correctly parses a function-pointer parameter the regex version's `_FUNC_SIG_RE` can't), just never less.

**Fallback, at two granularities**, both recorded in `CodeCheckResult.fallback_notes` and surfaced in the student's report (`report._render_code_check`) as a `- note: ...` line — never silently absorbed:

- **Whole-file**: `libclang` isn't installed, or `build_context` raises for any reason (parse exception, the completeness gate above) — every construct for this submission falls back to the regex path, with one note, e.g. `libclang check failed (No module named 'clang') -- falling back to regex-based checks for this submission`.
- **Per-construct**: the file parsed and passed the completeness gate, but `code_checks_ast.detect` raises for one specific construct name — only that construct falls back, with its own note, e.g. `libclang check for 'bitwise_and' failed (...) -- falling back to regex-based check`. (No built-in construct is currently known to trigger this path — it's defensive, for a construct/cursor-shape combination not covered above.)

**One deliberate, documented divergence from the regex version**: a `do { ... } while (cond);` loop's trailing `while (cond);` is **not** counted as a separate `while_loop` match under the AST path, since it's the condition-check of the `DO_STMT`, not an independent `WHILE_STMT` — whereas the regex version's plain `\bwhile\s*\(` textual search matches it regardless. The AST is the more semantically correct of the two here. This was checked against every question in `lab_01` that pairs `require: [while_loop]` with `forbid: [do_while]` (`q03_sum_n`, `q08_hailstone`) and confirmed to change no real outcome — a `do`-`while`-only submission is already gated to zero via the forbidden `do_while` match regardless of whether its trailing `while` also (mis)counts toward `while_loop` — but a future question that requires `while_loop` *without* also forbidding `do_while` would see the two implementations disagree on a `do`-`while`-only submission. Not treated as a bug to paper over; the AST behavior is left as-is.

**Two real bugs this surfaced** (found by writing a script that runs both implementations side by side on the same file and diffs their line numbers per construct — not by inspection):

1. In `code_checks_ast.py` itself, during development: the first version of `else`/`if_else` detection reported the enclosing `IF_STMT`'s own location (the `if` keyword's line) for `else`, instead of the else-branch child cursor's location (the actual `else` keyword's line) — fixed before landing, by using `children[2].location.line`.
2. In the **pre-existing regex** `function_call:<name>` check (`code_checks.py`, unrelated to this feature but exposed by comparing against it): `\bname\s*\(` matched a function's own *definition* signature (`int toupper(int c) { ... }`) as if it were a *call* to `toupper`, since the plain regex had no way to tell "immediately followed by `{`" (a definition) from "immediately followed by an actual call". No real question uses `function_call:` yet (per the "not yet exercised" note removed above), so this was latent, not actively wrong for anyone — but it's a real bug in code that's now also the fallback path for this exact construct, so it was fixed alongside this work (excluding a match immediately followed by `{`, the same disambiguation `_calls_in_span`/`function_def_used` already used).

**Verified:**

- Ran the real `lab_01` question bank (10 questions, real student submissions, `code_checks` covering `switch`/`case`/`if`/`else`/`if_else`/`for_loop`/`while_loop`/`do_while`/`modulo`/`bitwise_and`/`bitwise_xor`, plus `line_gate` mode) through `grader.grade` twice: once with `libclang` installed (0 fallback notes anywhere, every gold self-check passing), once with the `clang` Python package temporarily hidden to simulate "not installed" (a `- note: libclang check failed (No module named 'clang') -- ...` on every code-checked question, for every student). **`summary.csv` was byte-for-byte identical between the two runs** — the AST and regex implementations agree on every real, previously-graded question in the actual question bank.
- Re-ran the 16-case `function_def_used` demo lab (§3.1.1) with `libclang` installed: identical 16/16 results to the regex-only run, 0 fallback notes.
- For the constructs no currently-authored question exercises yet (`ternary`, `bitwise_or`, `bitwise_not`, `bitwise_shift_left`, `bitwise_shift_right`, `function_call:<name>`), a dedicated synthetic file exercising every construct at once (`/tmp/test_constructs.c`, not part of the repo) confirmed exact line-number agreement between `code_checks._detect` and `code_checks_ast.detect` for all of them (after the two fixes above), plus the one confirmed, intentional `while_loop` divergence described above.

**Scoring — `mode` is set per question, matching the different tones already present in the actual lab questions:**

- **`gate`**: any violation (a missing `require`, or a present `forbid`) zeroes the *entire* question's marks, regardless of test results. Fits `bitwise_Q.txt`'s hard "NOT allowed" framing — a student who reaches the right output via `%` and `if`/`else` has not done the exercise, and the report says so explicitly (§7) while still showing what the test score would otherwise have been, so the student can see their logic was sound even though the marks are zero. No `marks`/`penalty` key in this mode — `gate` isn't a bucket of points, it's a precondition on the ones already defined by `marks.open`/`marks.hidden`.
- **`bonus`**: a separate `code_checks.marks` bucket, *added to* `marks.open + marks.hidden` to form the question's `total_marks`, independent of test correctness — awarded in full if satisfied, zero if not. Note this is not literally "extra credit beyond 100%": since `total_marks` always includes the bucket, a student who doesn't satisfy it is capped *below* the max, not scoring less than test correctness alone would give them. It behaves more like a required bonus than free points on top.
- **`penalty`**: a separate `code_checks.penalty` amount, *subtracted from* whatever the tests already earned if violated, floored at zero (`max(0, test_marks_earned - penalty)`) — `total_marks` is **not** increased by this, unlike `bonus`. This is the most direct way to say "you lose N marks for not following the constraint," independent of how many marks the question is worth; a student who violates it can still pass on test correctness alone if the penalty doesn't wipe out everything earned.
- **`line_gate`**: the odd one out — `gate`/`bonus`/`penalty` all act on the question's marks *as a whole*, so a question with two independent parts (one construct per output line, e.g. `q06_bitwise_playground`'s line 0 = odd/even via `bitwise_and`, line 1 = case-swap via `bitwise_xor`) loses everything for missing just one construct, even under `partial: true` on the test — the text-based per-line scorer (§ "Per-line `partial` credit" below) and the construct check were otherwise unrelated to each other. `line_gate` is a `{0-based line index: construct name}` map that only applies to `partial: true` tests: a line only counts toward that test's partial credit when its text matches **and** the construct gating that line was found in the source. Every construct named in `line_gate` must also already be listed in `require` (a load-time error otherwise) since it's implemented by re-checking `found_required` from the very same source scan, not a second one. Unlike the other three modes, `line_gate` never touches `QuestionScore.marks_earned` as a post-hoc adjustment — the construct-awareness is baked directly into each test's earned marks during scoring, so `report.py`'s "Code checks (line-gate): ..." line is purely descriptive (found/missing/forbidden constructs, no pass/fail framing) rather than announcing a gate/bonus/penalty outcome. `forbid` still runs and reports under this mode too, but doesn't by itself zero any line's credit — only a missing gated `require` construct does.

`bonus` and `penalty` land on a similar place for a fully-compliant, fully-correct submission (full marks either way) but diverge for a non-compliant one: `bonus` withholds only the dedicated bucket while the denominator also shrinks by the same amount (so the *percentage* lost depends on how large the bucket is relative to the rest), whereas `penalty` docks a fixed amount from an unchanged denominator. Fits `rock_paper_scissors_Q.txt`'s softer "using switch statements" expectation either way — pick `bonus` to frame it as encouraged, `penalty` to frame it as a fixed deduction for not following instructions, without going as far as zeroing the question like `gate`.

All four modes only run after a successful compile (same as open/hidden tests — an uncompilable submission is already 0, checking its source for constructs is moot). The gold solution is checked too, as part of the existing self-check (§3): if `gold.c` itself doesn't satisfy its own `code_checks`, that's an authoring bug and grading is aborted before any student is touched, exactly like a failing gold test case.

## 4. Discovering student submissions

`discover.py`:

1. Walk immediate subdirectories of `submissions/`.
2. Each subdirectory name is treated as the roll number. Normalize (trim, uppercase) and validate against a roll-number regex (e.g. `^[A-Z0-9]+$`); anything that doesn't match is logged as an anomaly and included in the report as `UNRECOGNIZED_FOLDER` rather than silently skipped or silently guessed — a human resolves it (rename or map via an override file, see §8).
3. For each question, search the student folder for a file matching any of `filename_patterns` (case-insensitive). If:
   - zero matches → student gets 0 for that question, remark `"submission file not found"`.
   - multiple matches → prefer exact case-sensitive match to the first pattern; otherwise flag `MULTIPLE_CANDIDATES` and pick the most recently modified file, recording the alternate names in the remarks so the instructor can spot-check.

## 5. Sandboxed compile & execute — the core safety layer

This is the part that must not be improvised casually, since it runs arbitrary, occasionally malicious/broken student code.

### 5.1 Threat model

- Infinite loops / busy CPU spin.
- Fork bombs / thread bombs.
- Unbounded memory allocation.
- Writing huge files to disk (fill the disk).
- Reading/deleting files outside its own sandbox (`../../etc/passwd`, `rm -rf`, etc.).
- Network access (exfiltration, downloading a second stage) — should just not exist.
- Escaping via `system()`, `execve`, symlinks.

### 5.2 Sandbox backend: `isolate`

**Chosen backend.** `isolate` (`github.com/ioi/isolate`) is the sandbox used by IOI/Codeforces/DOMjudge — cgroups + Linux namespaces, purpose-built for running one untrusted program per invocation with hard resource limits, no network, and a filesystem view restricted to its own box. Not packaged in Ubuntu 24.04's apt repos, so it's built from source (one-time setup on the grading machine).

**Install** (Ubuntu 24.04; this machine already runs unified cgroup v2, which recent `isolate` supports):

```bash
sudo apt update
sudo apt install -y build-essential libseccomp-dev libcap-dev pkg-config git libsystemd-dev asciidoc-base

git clone https://github.com/ioi/isolate.git
cd isolate
make isolate isolate-cg-keeper     # binary targets only; bare `make` also tries to
                                    # build the man page (asciidoc -> a2x -> xmlto ->
                                    # docbook-xsl) and fails partway if that chain is
                                    # incomplete, even though the binary already built fine

sudo make install                  # binary -> /usr/local/bin/isolate
                                    # config  -> /usr/local/etc/isolate
```

**Portability — what if the grading machine doesn't have cgroup v2 unified hierarchy?** Check first: `stat -fc %T /sys/fs/cgroup` — `cgroup2fs` means full unified v2 (this machine); `tmpfs` with several per-controller subdirectories (`memory`, `cpu`, `pids`, ...) means legacy v1 or **hybrid** (v1 controllers + a v2 mount only for systemd's own accounting, the default on some older systemd distros e.g. pre-22.04 Ubuntu).

- **Legacy v1**: correction from earlier drafts of this doc — as of `isolate` 2.x (the version actually installed, confirmed via its manual page), **v1 is no longer supported at all**; `isolate` requires v2. A machine stuck on pure v1 needs a kernel/distro upgrade, not a config tweak.
- **Hybrid**: can confuse `isolate`'s auto-detection. It's technically possible to force full v2 (add `systemd.unified_cgroup_hierarchy=1` to the GRUB kernel command line, `update-grub`, reboot), but **this isn't recommended** unless you're certain nothing else on the machine still depends on v1 (older Docker/container runtimes, some system services) — it's a whole-machine change, not scoped to this grader, and isn't easily reversible without another kernel-command-line edit and reboot. Prefer `--sandbox subprocess` instead (`install.sh` offers this automatically when it detects hybrid/v1 — see README.md) unless you specifically need `isolate`'s stronger isolation and have verified the GRUB change is safe on this machine.
- **No usable cgroups at all** (nested container/CI runner without cgroup delegation, WSL1, some restricted VPS): `--cg` fails outright.

Don't assume any of this holds on a machine you haven't checked — have the grader **self-test at startup**: run `isolate --box-id=0 --cg --init` once before grading anything; if it errors, log a clear warning and drop to the `SubprocessRlimitSandbox` fallback rather than silently limping along on a half-working cgroup setup (memory/process-count limits would then be best-effort rlimits only, which the report/log should say explicitly).

**Post-install setup — required, one-time, root** (discovered from the manual page and config parser of the actual installed version, `isolate` 2.6; earlier drafts of this doc assumed the older fixed-uid-pool model, which this version has replaced with Linux user namespaces backed by `/etc/subuid`/`/etc/subgid`):

```bash
# 1. Dedicated system account that owns the sandbox UID/GID range. isolate
#    reads this account's row in /etc/subuid/subgid (via `subid_user = isolate`
#    in /usr/local/etc/isolate) to learn which UID range it may hand out to
#    sandboxed processes, one UID per --box-id.
sudo useradd -r -s /usr/sbin/nologin isolate

# 2. Grant that account a UID/GID range to sub-lease to sandboxes. 65536 IDs
#    supports up to 65536 concurrent box-ids -- far more than --parallel will
#    ever need, but isolate reads this file's declared count as num_boxes.
echo "isolate:200000:65536" | sudo tee -a /etc/subuid
echo "isolate:200000:65536" | sudo tee -a /etc/subgid

# 3. isolate needs a persistently-delegated cgroup v2 subtree to place each
#    box's cgroup under. On a systemd machine (this one), the supported way
#    is to let systemd delegate a slice and keep it alive with the
#    isolate-cg-keeper daemon shipped in the source tree -- rather than
#    isolate creating/tearing down that subtree itself on every run, which is
#    the "delegated subtree doesn't survive across cycles" issue flagged
#    earlier in this doc.
# isolate-cg-keeper installs to /usr/local/sbin, not /usr/local/bin -- @SBINDIR@
# must be substituted accordingly or the service fails to start with
# "No such file or directory"
sudo cp /home/devadatta/isolate/systemd/isolate.slice /etc/systemd/system/
sudo sed "s#@SBINDIR@#/usr/local/sbin#" \
  /home/devadatta/isolate/systemd/isolate.service.in | sudo tee /etc/systemd/system/isolate.service

sudo systemctl daemon-reload
sudo systemctl enable --now isolate.service
sudo systemctl status isolate.service --no-pager   # should show "active (running)"

# 4. Smoke test -- this is the exact self-check the grader runs at startup
#    (see the self-test note above): if these two lines succeed with no
#    output/error, isolate is fully operational.
isolate --box-id=0 --cg --init
isolate --box-id=0 --cg --cleanup
```

If step 4 fails with `User isolate not found in /etc/subuid`, steps 1–2 didn't take effect (check `id isolate` and `grep isolate /etc/subuid /etc/subgid`). If it fails with something cgroup-related, `isolate.service` isn't active (check `systemctl status isolate.service`) or `cg_root` in `/usr/local/etc/isolate` doesn't match where that service delegated the slice.

**Privilege model** — install setuid-root so the grader never needs `sudo` per invocation; `isolate` drops privileges internally into a dedicated low-priv uid from its configured pool before running the student binary:

```bash
sudo chown root:root /usr/local/bin/isolate
sudo chmod u+s /usr/local/bin/isolate
```

**cgroup v2**: pass `--cg` on every invocation to get memory/process-count enforcement via cgroups rather than best-effort rlimits. If the delegated cgroup subtree doesn't survive across init/cleanup cycles when run outside an interactive login session (a known rough edge on some cgroup v2 setups), run `isolate-cg-keeper` as a small persistent daemon (systemd unit provided in the repo) to hold it open — test one full init→run→cleanup cycle by hand right after install before trusting it for a real batch.

**Per-run isolation properties** (free from `isolate`'s namespacing, no extra code needed): the student binary sees only `/box` plus a minimal read-only system view (no `submissions/` folder, no other students, no grader code), and gets no network interface at all. `--processes=1` is the actual fork-bomb defense — a second `fork()` fails outright inside the box instead of needing to be caught after the fact.

**Fallback design** (only if `isolate` turns out unusable on the grading machine — e.g. cgroup v2 delegation genuinely won't cooperate): implement `runner.py` against an abstract `Sandbox` interface with two backends — `IsolateSandbox` (primary) and `SubprocessRlimitSandbox` (fallback) — so the rest of the pipeline doesn't care which is active. The subprocess fallback still must apply:

- New process group (`os.setsid`) so a timeout can kill the whole tree, not just the immediate child.
- `resource.setrlimit(RLIMIT_CPU, ...)`, `RLIMIT_AS` (address space / memory), `RLIMIT_NPROC` (process count — the fork-bomb defense), `RLIMIT_FSIZE` (max file size written), `RLIMIT_NOFILE`.
- Run as a dedicated low-privilege OS user (`grader_sandbox`), not the TA's own account, with a private working directory that's wiped after each test.
- No network namespace / `unshare --net` if available.
- Hard wall-clock timeout via `subprocess.run(..., timeout=...)` in addition to `RLIMIT_CPU`, since CPU limit alone doesn't catch a process blocked on I/O (e.g. an accidental `scanf` waiting forever because stdin closed early — see §5.4).
- Kill the whole process group on timeout (`os.killpg`), not just the leader.

### 5.3 Compile step

- Compile once per student per question (not once per test) into `runs/<ts>/raw/<roll>/<qid>/a.out`.
- `gcc -std=c11 -O2 -Wall -o a.out student.c` with a compile timeout too (a pathological `#include` loop or template-like macro expansion could hang the compiler — rare in C but cheap to guard).
- Compiler still runs inside the sandbox (it's still executing student-influenced input, and diagnostics/warnings are captured for remarks, e.g. "implicit declaration of function").
- Compile failure → 0 marks for that question, remark = compiler stderr (truncated to a reasonable length, e.g. 2000 chars).

**Linker (`-l...`) flags are placed *after* `student.c` on the command line, compiler flags before it.** A question needing `math.h` (`sqrt`, `pow`, ...) must add `-lm` to `compile.flags`; GNU ld only resolves a library's symbols against object files it has *already* seen, so `-lm` before `student.c` on the command line silently fails to link with "undefined reference to `sqrt`" even though the flag is present and correctly spelled. `sandbox.py`'s `_split_link_flags` splits any `-l<name>` out of the configured flags and appends it after the source file regardless of where it was listed in `question.yaml`, so this isn't something a question author needs to get the ordering right on by hand — just add `-lm` (or whatever `-l...`) anywhere in `flags:` and it lands in the correct position. This was found (not designed for up front) via a real authored question (`q2_quadratic_roots`, needing `sqrt`) failing its gold self-check with a compile error that turned out to be truncated before the actually-relevant line — see the fix for the truncation bug alongside it, below.

**Gold self-check failure messages are not truncated at an arbitrarily short length.** An earlier version cut the compiler-stderr shown in the `Gold self-check FAILED` message at 200 characters — short enough that a routine warning (e.g. `scanf`'s `-Wunused-result`) could push the *actual* fatal error (the linker's `undefined reference` in the case above) past the cutoff, leaving only a warning visible and no clue why compilation actually failed. Fixed by dropping the second, stricter truncation in `runner.self_check_gold` entirely — the message now shows the same (already reasonably-capped, `COMPILE_FLAGS_MAX_STDERR` in `sandbox.py`) stderr a student's own compile failure would show.

### 5.4 Execute step, per test case

- Feed `.in` file as stdin.
- Capture stdout, stderr, exit code, wall time, (peak memory if the sandbox reports it).
- Apply `time_seconds` (CPU) and `wall_seconds` (wall clock) limits from the question YAML, with sane global defaults (2s CPU / 5s wall) if unset.
- Truncate captured stdout at `output_bytes` to defend against a program that prints gigabytes in a loop instead of looping infinitely on CPU — cap it and mark as failed with remark `"output truncated — exceeded size limit"`.
- Outcomes to distinguish (important for remarks quality):
  - `PASS` — output matches.
  - `WRONG_ANSWER` — ran fine, output differs.
  - `TIMEOUT` — exceeded time/wall limit (killed).
  - `RUNTIME_ERROR` — non-zero exit / signal (SIGSEGV, SIGFPE, SIGABRT — decode signal name into remark, e.g. "crashed: segmentation fault").
  - `MEMORY_LIMIT_EXCEEDED`.
  - `SANDBOX_VIOLATION` — blocked syscall/fork limit hit (rare, but log it distinctly from a plain runtime error since it's evidence of the fork-bomb/escape case).

### 5.5 `isolate` invocation lifecycle

One box-id per concurrent worker (`--parallel 4` → box-ids 0–3, reused for the worker's whole run — no extra locking needed since each worker owns its id exclusively). Implemented in `grader/sandbox.py` (`IsolateSandbox`); compile happens once per student+question, then `init → run → read meta+stdout → cleanup` repeats per test case, so no state (temp files, leftover writes) leaks between test cases of the same student.

Two things the initial sketch of this section got wrong, found by actually running it against a real submission (see §12 for the full incident):

1. **The `--meta` file cannot live under `box_dir.parent`.** That directory (`/var/local/lib/isolate/<id>/`) is root-owned; `isolate` deliberately writes the meta file as the *invoking* user (a security fix — see `isolate`'s own NEWS for 2.5), so it must point at a scratch directory the grader process actually owns instead.
2. **Compiling needs an explicit `PATH`.** `isolate` starts every sandboxed process with an empty environment. `gcc` itself runs fine without `PATH`, but `collect2` locates the linker (`ld`) via `execvp`, which needs `PATH` to resolve — without it, compilation fails with `cannot find 'ld'` even though the source has no error. Fixed by adding `--env=PATH=/usr/bin:/bin` to the compile invocation only (the *student binary* itself is invoked by absolute path and needs no `PATH`).

```python
def init_box(box_id: int) -> Path:
    out = subprocess.run(["isolate", f"--box-id={box_id}", "--cg", "--init"],
                          capture_output=True, text=True, check=True)
    return Path(out.stdout.strip()) / "box"        # /var/local/lib/isolate/<id>/box

def run_test(box_id: int, box_dir: Path, binary: Path, in_file: Path, limits: dict,
             meta_dir: Path):  # meta_dir: scratch dir the grader process owns
    shutil.copy(binary, box_dir / "a.out")
    meta_path = meta_dir / f"run_meta_{box_id}.txt"
    cmd = [
        "isolate", f"--box-id={box_id}", "--cg",
        f"--time={limits['time_seconds']}",
        f"--wall-time={limits['wall_seconds']}",
        f"--mem={limits['memory_mb'] * 1024}",      # KB
        "--fsize=1024",                               # KB, blocks disk-fill
        "--processes=1",                               # fork-bomb defense
        f"--stdin={in_file.name}",
        "--stdout=out.txt", "--stderr=err.txt",
        f"--meta={meta_path}",
        "--run", "--", "/box/a.out",
    ]
    subprocess.run(cmd, cwd=box_dir, timeout=limits['wall_seconds'] + 2)
    return parse_meta(meta_path), (box_dir / "out.txt").read_text(errors="replace")

def cleanup_box(box_id: int):
    subprocess.run(["isolate", f"--box-id={box_id}", "--cg", "--cleanup"], capture_output=True)
```

The `--meta` file is parsed directly into the outcome categories above instead of hand-rolling resource accounting:

| meta file | meaning | outcome |
| --- | --- | --- |
| *(status field absent)*, `exitcode:0` | ran to completion | compare stdout → `PASS`/`WRONG_ANSWER` |
| `status:TO` | CPU or wall time exceeded | `TIMEOUT` |
| `status:SG`, `exitsig:11` | killed by signal (11 = SIGSEGV) | `RUNTIME_ERROR: segmentation fault` |
| `status:RE`, `exitcode:N` | nonzero exit | `RUNTIME_ERROR: exit code N` |
| `status:XX` | isolate couldn't run it at all (sandbox setup failure) | `SANDBOX_VIOLATION` — always worth a human look |
| `cg-mem` over the `--mem` limit | | `MEMORY_LIMIT_EXCEEDED` |

## 6. Scoring

`scorer.py`:

- Per test: full test `weight` if matcher says match, else 0 (or partial credit if `partial: true` on that test — §3 — in which case a many-small-weight-tests split isn't the only way to express partial credit anymore).
- Matcher types:
  - `exact_trim` (default): compare line-by-line after `rstrip()` on each line and stripping trailing blank lines — forgives trailing-newline/trailing-space mismatches, which are the single biggest source of false negatives in naive string equality and not something students should lose marks over.
  - `exact`: byte-for-byte, for questions where whitespace is part of the spec.
  - `token`: split on whitespace and compare token sequences — forgives spacing differences entirely, *as long as some whitespace already separates the tokens*; it does not help when a student omits whitespace that should be there (`root1=2.00` has no split point at all, see `ignore_whitespace` below).
  - `float_tol`: numeric tokens compared with `abs(a-b) <= eps` (configurable epsilon), for questions involving floating point.
  - `custom`: escape hatch — question folder may supply `checker.py` with a `def check(expected: str, actual: str, test_input: str) -> bool`, for questions with multiple valid outputs (e.g. "print any one of the valid orderings"). Bypasses everything below — a custom checker gets raw `expected`/`actual` text and full control, on the assumption that if you're already writing Python you'll normalize however you want yourself.

**Composable tolerance options** (`matcher.ignore_case`, `.ignore_whitespace`, `.ignore_punctuation`, `.allow_extra_output` — all boolean, default off — and `.symbol_groups`, a list of interchangeable-symbol groups, default unset) layer on top of *any* `type` above except `custom`. These exist because a real batch of student submissions surfaces a predictable set of harmless mistakes that have nothing to do with whether the student's actual logic is correct, and forcing every question to tolerate all of them by default would hide genuine mistakes a question is specifically testing for (`q06_bitwise_playground`'s second line is a case-*toggle* — enabling `ignore_case` there would make the whole exercise unable to fail on the one thing it's checking). Each is independently opt-in per question:

- `ignore_case`: `pass` matches expected `PASS`. Implemented as `.lower()` on both sides before the chosen `type` runs.
- `ignore_whitespace`: `root1=2.00` matches expected `root1 = 2.00`. Strips *all* whitespace (not just runs of it) from both sides — stronger than `token`, which only tolerates extra/varying whitespace where some already exists, not whitespace that's missing entirely. Degenerates whatever `type` is chosen down to plain string equality on the stripped text (there's nothing left to split on), so combining it with `token` is redundant and combining it with `float_tol` actively breaks number-parsing (adjacent numbers glue into one unparseable token) — pair it with `exact` or the default `exact_trim` instead.
- `ignore_punctuation`: `INVALID:` matches expected `INVALID`. Strips `. , : ; ! ? ' "` from both sides.
- `allow_extra_output`: tolerates a prompt printed right before `scanf` ending up mixed into the real output — most commonly `printf("Enter marks: ");` with no trailing `\n`, which glues the prompt onto the *same line* as whatever gets printed next (so this isn't just an extra line to skip; a purely line-based "ignore leading lines" design would miss it). Implemented in `scorer._extra_output_tolerates` as a *suffix* check (`actual.endswith(expected)`, after whatever other tolerances already normalized both), not plain substring containment: a real batch of submissions surfaced two failure modes plain containment (`expected in actual`) was wrongly passing that a suffix check correctly rejects — a logic bug running two `if` branches instead of one and printing both answers back-to-back (`PASSINVALID` for expected `PASS`, since `PASS` is still `in` that string even though it isn't how the output ends), and stray extra tokens after the real answer (`"0 1"` for expected `"0"`).

  The remaining ambiguous case is extra text that's a *prefix*, which has the same shape as the legitimate prompt (`-1.00000Imaginary root` for expected `Imaginary root`, from a wrongly-printed discriminant). For **numeric expected output**, the implementation adds a guard on that leftover prefix: after removing at most one `.` and one `-`, it rejects the prefix only if the entire remaining prefix consists of digits. This prevents a purely numeric value such as `"0.00"` from being treated as harmless extra text when the expected output is `"0"`. For example, expected `"0"` with actual `"0.00"` is rejected because the leftover prefix `"0.0"` is numeric after normalization, while `"Enter marks: 100"` with expected `"100"` is tolerated because the leftover prefix `"Enter marks: "` is not entirely numeric.

  Importantly, this is **not** a general "prefix must contain no digits" rule. A mixed textual prefix such as `"Enter marks: 1"` is still accepted by the current implementation, because the prefix is not composed entirely of digits after the `.`/`-` normalization. Likewise, when the expected output is non-numeric, no numeric-prefix restriction is applied: `"Enter marks-5INVALID"` is accepted for expected `"INVALID"`.

  This is a real, disclosed trade-off rather than permissive-by-accident: the suffix requirement prevents arbitrary trailing output and the numeric-prefix check catches purely numeric leakage before a numeric answer, but textual prefixes containing numbers can still pass. Enable `allow_extra_output` only where the specific failure mode (an unflushed prompt before `scanf`) is the actual concern.

- `symbol_groups`: a list of independent groups of interchangeable symbols, e.g. `[["x", "X", "*"], [":", "="]]`. Runs first, before the other tolerances — within each group, every symbol is replaced with that group's first (canonical) symbol (`scorer._apply_symbol_groups`, matched literally via `re.escape`, longest symbol first within a group so a short one never partially shadows a longer one containing it), independent of whether `ignore_case` is also set; the groups themselves don't interact with each other. Started as a single hardcoded `normalize_multiplication_sign` boolean (`re.sub(r"[xX*]", "x", text)`) added for `q05_vending_change`, whose expected output uses `x` as a "times" separator (`"50 x 1"`) and where students substitute `*` (a reasonable multiplication-sign habit) or `X`; generalized to arbitrary symbol groups once a second question needed a different interchangeable pair (e.g. `:`/`=`) that had nothing to do with multiplication. `config_schema.py` rejects a group of fewer than 2 symbols (nothing to collapse) and a symbol appearing in two groups (which group's canonical symbol should win would be ambiguous).

All five are validated at load time against a fixed option set (`VALID_MATCHER_OPTIONS`, `config_schema.py`) — a typo'd key (`ignor_case`) is a load-time `QuestionConfigError`, not a silently-inert option, matching every other fail-fast validation in this file. When none are set — true for every question written before these existed — `get_matcher()` returns the exact same base-matcher function object as before, not just equivalent behavior; this was the actual regression check run when adding them (see §12).

**Naming which tolerance rescued a test**: a tolerated pass used to be indistinguishable from an exact match in the report — a real concern, since these tolerances exist precisely to paper over mistakes a student should still be told about (wrong case, a stray typo, ...). `scorer.get_test_matcher` now returns `(passed, reasons)` instead of a plain bool; `reasons` is empty for an exact match or an outright fail, and otherwise names every tolerance that was actually necessary for the pass, surfaced in `report.py` as `PASS (case not matched, spelling not matched)` instead of a bare `PASS` — the same instinct as naming a `FAIL`'s category (`TIMEOUT`, `RUNTIME_ERROR`, ...) instead of just saying `FAIL`. A tolerated `PASS` also gets the same input/expected/actual detail a `FAIL` gets (normally withheld on a pass to keep hidden tests hidden), since a reason with nothing to check against isn't actionable.

"Necessary" is derived, not just "which tolerances happen to be enabled" — an enabled `ignore_case` on a question doesn't get mentioned for a test that already matched exactly. `scorer._reasons_for_pass` does this via leave-one-out: given the question's full matcher already passed, remove one currently-enabled option at a time (`scorer._wrap_matcher`, factored out of `get_matcher` so it can rebuild the wrapped matcher for an arbitrary option subset) and re-check; if dropping just that option turns the pass back into a fail, it was load-bearing. This is a real gap-detection algorithm, not exhaustive-search overkill, only because these five options are each independently monotonic — every one only strips information from both sides before comparing, so enabling more of them can only make two strings *more* likely to compare equal, never less. That guarantee is what makes leave-one-out sound: if the full set passes and the empty set (`_base_matcher` alone) fails, at least one single removal is guaranteed to flip it back to a fail, and if two options are only jointly sufficient (neither alone would do it), removing *either* one flips the result, so leave-one-out reports both — which is the correct answer, not a false positive. `ignore_typo`'s reason ("spelling not matched") is attributed the same way, but as a separate step (`scorer._typo_tolerant_match`) since it's a per-test flag layered underneath the question-level matcher rather than one more member of `_OPTION_REASON`'s leave-one-out set.

- Per-question score = sum of passed test weights, out of `marks.open + marks.hidden`.
- Total score per student = sum across all questions.
- Both open- and hidden-test failures get **full detail** in the report (which test, input, expected vs actual) — `report_<roll_no>.md` is instructor-only (never distributed raw to students; per-student results go out through whatever the course's usual channel is, not this file), so withholding hidden-test detail from the TA's own copy only made debugging a hidden-test failure impossible without going to `run.log`, for no actual confidentiality benefit. (Earlier drafts of this doc said hidden failures show only pass/fail + category "to keep the habit consistent" — reversed once actually using the report to debug a real failure showed that habit had no upside and a real cost. `run.log` still has everything else: gold self-check results, per-test sandbox status, which backend ran.) A passing hidden test still reveals nothing either way — detail only appears on failure, for both groups.
- A failure whose run itself was fine (wrong output, `status: OK`) shows plainly as `FAIL` — no misleading `FAIL (OK)`, which reads as contradictory. A failure from an abnormal run still names the reason, e.g. `FAIL (TIMEOUT: exceeded time/wall-time limit)`.

## 7. Report generation

`report.py` produces, per run:

1. **`summary.csv`** — one row per roll number, columns: `roll_no`, one column per question's score, `total`, `anomalies` (free text: unrecognized folder, missing file, multiple candidates, compile errors present, etc.). Opens directly in Excel/Sheets for the instructor's gradebook.
2. **`report_<roll>.md`** per student — human-readable:

   ```markdown
   # Report — 112201023

   ## Q1: Pass/Fail/Invalid classifier — 62 / 100
   - Compiled: yes (no warnings)
   - Open tests: 20/20
   - Hidden tests: 42/80
     - boundary_0: PASS
     - boundary_100: PASS
     - boundary_39: FAIL (WRONG_ANSWER)
     - negative_large: PASS
     - large_value: FAIL (RUNTIME_ERROR: segmentation fault)
     - ...

   ## Q2: ... 
   ...

   ## Total: 138 / 200
   ```
  
   Both open- and hidden-test remarks show the input/expected/actual diff on failure, per §6.
3. **`run.log`** — full instructor-facing log: every compile command, every test invocation, timings, sandbox backend used, anomalies list, gold-program self-check result. This is the debugging trail if a score looks wrong later.
4. Optional: aggregate stats (`stats.md`) — class average/median per question, histogram of scores, list of students with 0 (likely missing submissions) — useful for the instructor to sanity-check the exam/assignment itself, not just individual students.

## 8. Handling messy real-world input

- **Roll-number folder typos**: an `overrides.yaml` (passed via `--overrides`, e.g. `submissions/lab_01/overrides.yaml`) lets the TA map a weird folder name to a roll number by hand: `{"John_Doe_Submission": "112201099"}`. Anything not in `overrides.yaml` and not matching the roll regex is flagged, never guessed. Being per-lab like everything else under `submissions/` (§2), an override made for `lab_01` doesn't leak into `lab_02`'s grading run.
- **Missing question file / wrong question number in filename**: reported as anomaly with 0 marks, not silently skipped — so it shows up in `summary.csv` and the TA can decide (manual regrade, contact student, etc.).
- **Non-UTF8 / weird encodings, BOM, CRLF line endings**: normalize student source and expected-output files to UTF-8 + LF before compiling/comparing, so a Windows student isn't penalized for CRLF.
- **Duplicate submissions in one folder** (e.g. `q1.c`, `q1_final.c`, `q1 (2).c`): handled by the `MULTIPLE_CANDIDATES` rule in §4.

## 9. CLI shape

The tool itself has no notion of "lab" — `--questions`/`--submissions`/`--out` are just paths. Pointing all three at the matching `lab_NN` subfolder (§2) is what makes a run scoped to one lab; running lab_02 later is the same command with `lab_01` swapped for `lab_02`, nothing else changes:

```bash
python -m grader.grade \
  --questions lab_auto_grader/questions/lab_01 \
  --submissions lab_auto_grader/submissions/lab_01 \
  --out lab_auto_grader/runs/lab_01 \
  --sandbox isolate \
  --parallel 4          # how many students to grade concurrently
```

- `--parallel` controls a multiprocessing pool over *students* (each student's grading is independent); tests within a student's question run sequentially to keep sandbox resource accounting simple, though this could be revisited if grading speed becomes a bottleneck.
- `--only-question q1_result_grade` and `--only-student 112201023` for reruns / debugging a single case without regrading all 35.
- `--dry-run` compiles and checks discovery/anomalies without executing tests — useful right after unzipping, to catch folder-naming problems early.
- `--check-gold` runs *only* the gold self-check (§3: compile+run every `gold.c` against its own tests) and exits — `--submissions` becomes optional (the only flag combination where it isn't required), and no student is touched. This is the same check that already gates every normal run before any student is graded (§9's example command runs it implicitly); `--check-gold` is for running just that part, on demand, e.g. right after authoring a question — before a `submissions/<lab>/` even exists to point `--submissions` at. Mutually exclusive with `--dry-run`, since the two want opposite things (skip all execution vs. execute only the gold programs).

## 10. Implementation order (suggested milestones)

1. `discover.py` + question YAML schema + loader/validator (`config_schema.py`). Get anomaly detection working first — most real-world pain is here, not in the sandboxing.
2. `runner.py` with the `SubprocessRlimitSandbox` fallback — gets you an end-to-end working grader fast, on any machine, no root needed.
3. `scorer.py` + `report.py` — get one full run producing `summary.csv` and per-student reports against the example Q1 above, with gold.c self-check.
4. Swap in / add `IsolateSandbox` for production-grade isolation once the pipeline shape is proven.
5. Add `token`/`float_tol`/`custom` matchers and multi-question batching as more questions are authored.

## 11. Dependencies

- Python 3.10+, standard library only for the core (`subprocess`, `resource`, `multiprocessing`, `csv`, `yaml` via `PyYAML`).
- `gcc` on PATH.
- `isolate` (optional but recommended) — package `isolate` on Debian/Ubuntu, or build from `github.com/ioi/isolate`.
- No network access needed at grading time at all — another reason to physically disable networking in the sandbox regardless of which backend is chosen.

## 12. Implementation status

Built and working end-to-end against a real `isolate` install (`grader/` — `config_schema.py`, `discover.py`, `sandbox.py`, `runner.py`, `scorer.py`, `report.py`, `grade.py`, `code_checks.py`). Three questions authored under `questions/lab_01/` per §3 — `q1_result_grade` (single-line I/O), `q2_matrix_mult` (multi-line I/O, via YAML block scalars), and `q3_bitwise_playground` (real question from `lab_quest/W01/bitwise_Q.txt`, `code_checks` in `gate` mode) — graded against a `submissions/lab_01/` fixture covering: a fully correct solution on all three questions, a boundary-condition bug on q1, a subtle multiplication-order bug on q2, a submission that gets q3's output byte-correct via the exact constructs the question forbids (`%`, `if`/`else`, `toupper`/`tolower`), an infinite loop, a fork bomb, and a folder with a non-roll-number name.

```bash
source ~/venv/bin/activate
cd lab_auto_grader
python3 -m grader.grade --questions questions/lab_01 --submissions submissions/lab_01 \
  --out runs/lab_01 --sandbox isolate --parallel 4
```

**Verified, latest full run:** correct solution scored 200/200 across both questions; the q1 boundary bug lost exactly the open test it should (96/100, hidden tests all passed since none happen to hit marks=40 exactly — a reminder that a boundary worth testing should appear in *both* open and hidden sets, not just one) and separately the q2 multiplication-order bug scored 38/100 (passes only where operand order doesn't matter — identity, zero, or self-multiplying matrices — demonstrating why several test cases matter, not just trivial ones); the infinite loop and the fork bomb both scored 0/100 via clean `TIMEOUT`s on every test, with **no leftover student processes and no uncleaned isolate boxes on the host afterward** — confirmed by checking `ps` and `/var/local/lib/isolate/` immediately after the run. The fork bomb in particular never actually forked: `--processes=1` made every `fork()` call fail immediately, so the "bomb" degraded to a tight `while(1) fork();` loop that simply burned CPU until the time limit killed it — exactly the intended containment, not an accident of timing. A student missing a submission for one question (but present for another) is reported distinctly as "no submission found" rather than a compile failure.

**Bugs found only by running it for real, not by reading docs:**

- Two in the `isolate` integration (now in §5.5, not just here): the `--meta` file must be written to a directory the invoking user owns, not under `isolate`'s own (root-owned) box root; and compiling needs `--env=PATH=/usr/bin:/bin` or `collect2` can't find `ld`. Neither was documented anywhere in `isolate`'s manual page under those exact symptoms (`Failed to open metafile ...`, `collect2: fatal error: cannot find 'ld'`).
- One in the `subprocess` fallback sandbox: it set `RLIMIT_NPROC` to a small absolute number, but that limit counts *every thread for the whole real UID, system-wide* — not just the sandboxed subtree — so it starved instantly on a desktop machine already running hundreds of threads (editor, extensions, etc.). Fixed by offsetting the limit from the actual current thread count (`/proc/*/task/`) plus a safety margin.

This is the reason this design doc gets updated *after* running the tool, not just after reading about it — none of the three bugs above were predictable from documentation alone.

**Also verified**: `--dry-run` (discovery only, no compile/execute, confirmed by inspecting the run folder) and `--overrides` (mapping a mis-named folder to a roll number; confirmed the mapped roll number is used consistently through `--only-student` filtering, the compiled binary's path, the report filename, and `summary.csv`). `--check-gold` (§9): confirmed it runs with no `--submissions` given at all against the real (by-then 10-question) `lab_01` bank, that `--only-question` narrows it to one, that it's mutually exclusive with `--dry-run`, and that omitting `--submissions` in normal (non-`--check-gold`) mode fails with a clear argparse error rather than a confusing downstream one.

**`code_checks` (§3.1), verified against real questions from `lab_quest/W01`:** gold self-check confirmed `bitwise.c` itself satisfies its own `require`/`forbid` constraints (proving the check isn't so strict it'd fail a correct solution); a submission using `(n & 1)` and `ch ^ 32` scored 100/100 with "Code checks (gate): PASSED"; a submission producing byte-identical correct output via `n % 2`, `if`/`else`, and `toupper()`/`tolower()` instead scored **0/100** — the report lists each violation with its line number and states the test score would otherwise have been 100, so the student can see their *logic* was right even though the marks are zero. The comment/string stripper was the deciding factor: `bitwise.c`'s own `scanf("%d %c", &n, &ch)` contains both a `%` inside a string literal (would have falsely tripped the forbidden-`modulo` check) and two address-of `&` calls (would have falsely tripped the required-`bitwise_and` check) if checked against raw source instead of the cleaned text.

`bonus` and `penalty` modes (added after `gate`) are validated so far only via synthetic `Question`/`score_question`/`render_student_report` calls covering the marks math (including `penalty`'s floor-at-zero when the penalty exceeds what was earned, and its cap on `penalty_applied` so the report never claims to deduct more than existed) — not a full compile-and-run pass through a real authored question the way `gate` was (§ above, `q3_bitwise_playground`).

**Per-line `partial` credit**, verified via the actual save/read UI API round-trip (not just synthetic `Question` construction like `bonus`/ `penalty` above): a two-line `partial: true` test authored through the form produced clean YAML (`partial: true` only written when set, omitted otherwise) that reloaded byte-identical, and `--check-gold` confirmed a gold solution scores full marks on it. The actual partial-scoring math (1-of-2-lines correct → exactly half marks, 0-of-2 → zero, `partial: false` staying strict all-or-nothing) was verified via `score_question` directly — not yet through a real student submission that only gets one line right in a full `grader.grade` run.

**Composable `matcher` tolerance options** (`ignore_case`/`ignore_whitespace`/`ignore_punctuation`/`allow_extra_output`, §6), added after actually grading a real submission batch surfaced the exact four failure modes they solve. Verified: each individually against the literal cases reported (`pass` vs `PASS`, `root1=2.00` vs `root1 = 2.00`, `INVALID:` vs `INVALID`, an unflushed-prompt-before-`scanf` case gluing `"Enter marks: "` onto the front of the real output); all three normalizing options combined at once on one test, through the real sandbox via `--check-gold`, not just the matcher function in isolation; a typo'd option name rejected at load time; and — the backward-compatibility check that actually matters here — `get_matcher()` returning the *same function object* as before when none of the four are set, not just equivalent behavior, so every question written before these existed is provably unaffected.

Not yet exercised: `bonus`/`penalty` in a full pipeline run against a real question; `token`/`float_tol`/`custom` matchers (no authored question uses them yet); the `regex:`/`function_call:` `code_checks` escape hatches (only the built-in construct names have been used so far); `allow_extra_output` against a real student submission with a genuinely unflushed prompt (verified so far only against a synthetic reproduction of that pattern). Grading a real (non-synthetic) batch of actual student submissions is, as of this note, underway rather than "not yet exercised" — that's what surfaced the need for the tolerance options above in the first place.

**`normalize_multiplication_sign`** (§6), added for `q05_vending_change` specifically (its expected output uses `x` as a "times" separator, e.g. `"50 x 1"`) rather than as a global default, since a question where `x`/`*` are meaningfully different (none currently authored, but plausible) shouldn't silently tolerate the substitution. Verified via a synthetic matcher-level check (`50 x 1`/`50 * 1`/`50 X 1` all match expected `50 x 1`; an unrelated letter like `50 y 1` correctly does not) and end-to-end through the real sandbox: `--check-gold` on the question with the option enabled, then a full `grader.grade` run against a submission with every `x` separator swapped for `*`, scoring 10/10 with all hidden tests passing.

**`normalize_multiplication_sign` generalized to `symbol_groups`** (§6), once a second question needed a different interchangeable pair (`:`/`=`) that had nothing to do with multiplication and a single hardcoded boolean couldn't express. `q05_vending_change` migrated to `symbol_groups: [["x", "X", "*"]]` (same regex-escaped, longest-first collapse-to-first-symbol behavior as before, just table-driven instead of hardcoded to `[xX*]` → `x`). Also fixed a real bug the migration surfaced in `scorer._reasons_for_pass`: its `sufficient()` probe built each trial option combo as `{name: True for name in combo}`, which is fine for the boolean options but would hand `symbol_groups=True` (not the actual list of groups) to `_normalize_for_matching` — changed to `{name: opts.get(name) for name in combo}` so the real value threads through regardless of the option's type. Verified via a synthetic matcher-level check with two independent groups (`symbol_groups: [["x","X","*"],[":","="]]` — `50 * 1`/`50 X 1` matching `50 x 1`, and separately `a=1` matching `a:1`, an unrelated substitution in either group's "shape" not spilling into the other) and a load-time rejection check for a symbol listed in two groups.

**`code_checks` mode `line_gate`** (§3.1), added for `q06_bitwise_playground` after its original `penalty` config turned out to have `penalty: 0` (a load-time error — a zero penalty is a no-op, defeating the whole point of the mode) and, once that surfaced, a more fundamental mismatch: the question actually needed *per-line* construct gating (line 0 requires `bitwise_and`, line 1 requires `bitwise_xor`), not a whole-question penalty, since a student getting both output lines byte-correct via `%` instead of `&` should lose only the odd/even line's marks, not the whole test's. Verified end-to-end through the real sandbox: `--check-gold` passes with `mode: line_gate` configured; a submission using `%` for line 0 but `^ 32` correctly for line 1 scored 3/6 (`PARTIAL (1/2 lines, +1/2 marks)` on every hidden test, `bitwise_and` reported **not found** and `modulo` reported found-but-not-gating) while a fully bitwise-correct submission (identical to gold) scored 6/6 — confirming byte-identical hidden-test output does *not* imply identical marks under this mode, which was the entire reason for building it. A load-time check also confirmed a `line_gate` construct not also listed in `require` is rejected before grading starts, rather than silently never matching. **Not yet exercised**: the UI question editor — `code_checks_mode` radio options (`ui/static/app.js`) and payload round-trip (`ui/app.py`) were not extended for `line_gate`/`line_gate` mapping, so this question must be authored/edited via `question.yaml` directly for now; opening it in the browser editor's Save flow is unverified and could mishandle the new field.
