# Lab Auto-Grader

Compiles and runs every student's C submission against instructor-authored test cases inside the `isolate` sandbox, then produces a per-student report and a class summary CSV. Full design rationale (threat model, sandbox choice, `isolate` setup - tested and working on Ubuntu 24.04) lives in [AUTOGRADER_DESIGN.md](AUTOGRADER_DESIGN.md) — this file is the day-to-day "what do I run" reference.

## Layout

```text
lab_auto_grader/
├── AUTOGRADER_DESIGN.md      # full design doc: why isolate, threat model, install steps
├── README.md                 # this file
├── requirements.txt          # PyYAML
├── grader/                   # the tool
│   ├── config_schema.py      #   loads+validates question.yaml
│   ├── discover.py           #   finds student folders / question files, flags anomalies
│   ├── sandbox.py            #   IsolateSandbox (primary) + SubprocessRlimitSandbox (fallback)
│   ├── runner.py             #   compiles once, runs every test, returns raw results
│   ├── scorer.py             #   matchers (exact_trim/exact/token/float_tol/custom) + marks
│   ├── report.py             #   per-student markdown + summary.csv
│   └── grade.py              #   CLI entry point — wires all of the above together
├── questions/                # one folder per lab, then one folder per question
│   └── lab_01/
│       └── q1_.../            #   question.yaml (filename patterns, marks, limits,
│                               #   matcher, test cases inline, optional code_checks)
│                               #   + gold.c, self-checked before any student runs
├── submissions/               # one folder per lab; put that lab's extracted zip here (not committed)
│   └── lab_01/
└── runs/                      # one folder per lab, then one timestamped folder per grading run
    └── lab_01/
```

There's one lab per subfolder under each of `questions/`, `submissions/`, `runs/` — e.g. `questions/lab_01`, `submissions/lab_01`, `runs/lab_01` all refer to the same lab. Grading is run manually, once per lab, after that lab's submission deadline (see [Running a grading batch](#running-a-grading-batch)). Author `questions/lab_02/...` ahead of time; `submissions/lab_02/`  only needs to exist once that lab's zip is ready to grade; `runs/lab_02/` is created automatically on first run. Keeping `questions/` separate from `submissions/` also means a grading run never writes back into the submissions folder.

## One-time setup

### Automated setup: for linux users only (recommended)

Run the interactive installation script — it handles all setup, checks, and configuration:

```bash
chmod +x install.sh
./install.sh
```

The script will:
- Check OS and cgroup v2 support (required for `isolate`)
- Install system dependencies (build tools, Python)
- Build and install `isolate` sandbox from source
- Create Python virtual environment in project folder
- Install Python dependencies
- Create necessary directories (`questions/`, `submissions/`, `runs/`, `live/`)
- Initialize admin account (interactive prompt)

After completion, follow the printed next-steps guide.

### Manual setup (if needed)

If you prefer to install manually, or the script doesn't work on your OS:

#### Python environment

```bash
python3 -m venv venv             # create venv in project directory
source venv/bin/activate
pip install -r requirements.txt  # installs PyYAML, Flask, dependencies
```

#### Installing `isolate`

`isolate` (`github.com/ioi/isolate`) is the sandbox the grader runs student code in — see [AUTOGRADER_DESIGN.md §5.2](AUTOGRADER_DESIGN.md) for why it was chosen and the full threat model. It isn't packaged for Ubuntu 24.04, so it's built from source. This is a one-time, root-level setup on the grading machine.

**1. Build and install:**

```bash
sudo apt update
sudo apt install -y build-essential libseccomp-dev libcap-dev pkg-config git libsystemd-dev asciidoc-base

git clone https://github.com/ioi/isolate.git
cd isolate
make isolate isolate-cg-keeper    # binary targets only; bare `make` also tries to
                                  # build the man page (asciidoc -> a2x -> xmlto ->
                                  # docbook-xsl) and fails partway if that chain is
                                  # incomplete, even though the binary already built fine

sudo make install                 # binary -> /usr/local/bin/isolate
                                  # config  -> /usr/local/etc/isolate
```

**2. Post-install setup** — `isolate` 2.x sandboxes each box under its own Linux user namespace, backed by a UID/GID range declared in `/etc/subuid`/`/etc/subgid` for a dedicated system account:

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
#    box's cgroup under. On a systemd machine, the supported way is to let
#    systemd delegate a slice and keep it alive with the isolate-cg-keeper
#    daemon shipped in the source tree.
# run from inside the cloned isolate/ directory (from step 1's `cd isolate`)
# -- isolate-cg-keeper installs to /usr/local/sbin, not /usr/local/bin, so
# @SBINDIR@ must be substituted with /usr/local/sbin or the service will fail
# to start with "No such file or directory"
sudo cp systemd/isolate.slice /etc/systemd/system/
sudo sed "s#@SBINDIR@#/usr/local/sbin#" \
  systemd/isolate.service.in | sudo tee /etc/systemd/system/isolate.service

sudo systemctl daemon-reload
sudo systemctl enable --now isolate.service
sudo systemctl status isolate.service --no-pager   # should show "active (running)"
```

**3. Privilege model** — install setuid-root so the grader never needs `sudo` per invocation; `isolate` drops privileges internally into a dedicated low-priv uid before running the student binary:

```bash
sudo chown root:root /usr/local/bin/isolate
sudo chmod u+s /usr/local/bin/isolate
```

**4. Smoke test** — this is the exact self-check the grader runs at startup:

```bash
isolate --box-id=0 --cg --init && isolate --box-id=0 --cg --cleanup
```

If both lines run with no error, you're set. If it fails with `User isolate not found in /etc/subuid`, step 2 didn't take effect (check `id isolate` and `grep isolate /etc/subuid /etc/subgid`). If it fails with something cgroup-related, `isolate.service` isn't active (check `systemctl status isolate.service`).

If your machine doesn't have cgroup v2 unified hierarchy (check with `stat -fc %T /sys/fs/cgroup` — should print `cgroup2fs`), see [AUTOGRADER_DESIGN.md §5.2](AUTOGRADER_DESIGN.md) for the v1/hybrid/none cases and how to force full v2.

## Running a grading batch

Point `--questions`/`--submissions`/`--out` at the same lab's subfolder — that's the entire "select which lab" mechanism, there's nothing else to configure per lab:

```bash
cd lab_auto_grader
python3 -m grader.grade \
  --questions questions/lab_01 \
  --submissions submissions/lab_01 \
  --out runs/lab_01 \
  --sandbox isolate \
  --parallel 4
```

Grading a later lab is the same command with `lab_01` swapped for `lab_02` (etc.) in all three paths, once that lab's questions are authored and its submissions zip has been extracted into place.

This will:

1. Load every `question.yaml` under `--questions`, validating that test weights sum to the declared marks (fails immediately if a question is mis-authored).
2. Compile+run the **gold solution** of every question through the sandbox and refuse to grade any student if the gold program doesn't pass its own tests — that means a test case is wrong, not the students.
3. Walk `--submissions`, treating each subfolder name as a roll number; anything that doesn't look like one is flagged, not guessed.
4. For each student × question: find the matching `.c` file, compile it, run every open + hidden test case inside `isolate` (own filesystem view, no network, CPU/wall/memory/process-count limits), and score it.
5. Write `runs/lab_01/<timestamp>/report_<roll_no>.md` per student, `runs/lab_01/<timestamp>/summary.csv` for the whole class, and `run.log` with full detail (compiler output, sandbox status per test, timings).

### Useful flags

| Flag | What it does |
| --- | --- |
| `--dry-run` | Only runs discovery (step 3) — no compiling/executing. Use right after unzipping to catch folder-naming problems before spending time grading. |
| `--check-gold` | Compile+run every `gold.c` against its own tests, then exit — no `--submissions` needed at all (normally required; this is the one case it isn't), no students touched. Use this to sanity-check a question bank by itself, e.g. right after authoring a question in the UI or before a `submissions/<lab>/` even exists. Combine with `--only-question` to check just one. Mutually exclusive with `--dry-run` (one skips execution entirely, the other's whole point is running the gold programs). |
| `--sandbox auto` \| `isolate` \| `subprocess` | `auto` (default) uses `isolate` if it self-tests OK, otherwise falls back to the degraded `subprocess` sandbox with a warning. `isolate` forces it and aborts loudly if unavailable. `subprocess` forces the fallback — only for quick local testing, since it does **not** provide filesystem/network isolation. |
| `--parallel N` | Grade N students concurrently, each in its own `isolate` box-id. Default 1 (sequential). |
| `--only-question q1_result_grade` | Grade just one question — for rerunning after fixing a bug in one question's tests. |
| `--only-student 112201023` | Grade just one student — for debugging a single anomaly without regrading everyone. |
| `--overrides overrides.yaml` | Map a mis-named submission folder to a roll number by hand, e.g. `{"John_Doe_Submission": "112201099"}`, instead of it being flagged as unrecognized. Scope it per lab (e.g. `submissions/lab_01/overrides.yaml`) so a mapping made for one lab doesn't apply to another. |
| `--student-csv mapping.csv` | Enrich `summary.csv` with `ip`/`name` columns looked up by roll number, from a CSV with a header row `roll_no,name,ip` (or just `roll_no,name` — `ip` is optional, column order doesn't matter). Defaults to `ip_student_mapping.csv` under `--submissions` if that file exists; a roll number missing from the CSV just gets blank cells there, it's never dropped from the summary. |

### Trying it against the bundled sample data first

`submissions/lab_01/` currently holds a synthetic fixture (a correct solution on both questions, a buggy one, an infinite loop, a fork bomb, and a mis-named folder), not real student data — useful for exercising the whole pipeline, including the safety behavior on the two malicious/broken cases, before ever pointing this at a real batch:

```bash
python3 -m grader.grade \
  --questions questions/lab_01 --submissions submissions/lab_01 \
  --out runs/lab_01 --sandbox isolate --parallel 1
```

Check `runs/lab_01/<timestamp>/summary.csv` and the per-student `report_*.md` files afterward. Once real lab_01 submissions are ready to grade, replace the contents of `submissions/lab_01/` with the extracted zip (this synthetic fixture is only there to exercise the tool itself).

## Authoring a new question

1. `mkdir -p questions/lab_01/q3_<name>` (or `lab_02`, etc. — whichever lab this question belongs to)
2. Write `gold.c` — a correct reference solution.
3. Write `question.yaml` (copy `questions/lab_01/q1_result_grade/question.yaml` as a template): `filename_patterns` (how students might have named their file), `marks.open`/`marks.hidden`, `limits`, `matcher.type`, and the `tests` list. Each test's input/output goes **inline** as `in`/`out` strings right in the YAML — no separate files needed for ordinary line-based tests:

   ```yaml
   tests:
     open:
       - name: "some_case"
         in: "75"
         out: "PASS"
         weight: 4
   ```

   For a test with large input/output data, use `in_file`/`out_file` instead (a path relative to the question folder, e.g. `tests/big_input.txt`) — exactly one of `in`/`in_file` per test, same for `out`/`out_file`. **The open-test weights must sum to `marks.open`, hidden to `marks.hidden`** — the loader refuses to run otherwise.

   Add `partial: true` to a test whose lines check independent things (e.g. one line for an even/odd check, the next for an unrelated case-toggle) to award `weight / N` per matching line of expected output instead of all-or-nothing:

   ```yaml
   - name: "odd_lowercase"
     in: |
       7
       g
     out: |
       ODD
       G
     weight: 10
     partial: true   # 1 of 2 lines right -> 5 marks, not 0
   ```

   The UI's question editor has a "partial credit" checkbox on each test case for the same thing. It only kicks in when a test doesn't fully match (a full match still earns full `weight`) and only for a completed run — a timeout/crash earns nothing from it either way.

   Matching for `partial: true` is deliberately more than a literal line-for-line comparison: a blank expected line (e.g. separating two sections of a multi-part answer) doesn't count toward the total either way, and a student's real lines are searched as one continuous blob rather than requiring an exact line-for-line lineup — so a `printf` missing its trailing `\n` that glues two independently-correct pieces of output onto one physical line still earns credit for both, and a line that's missing, extra, or printed out of order no longer drags every *other* line's credit down with it just by shifting them out of index alignment. A numeric line (`"GCD = 5"`) is additionally protected against matching as a fragment of a different, wrong number that merely starts or continues with the same digits (`"GCD = 51"`) — but that protection is digit-specific, so a non-numeric line that's a complete substring of a different, opposite-meaning wrong answer (`"Coprime"` inside a wrong `"Not Coprime"`) isn't caught by it. For a question with that specific shape, list the affected line(s) under `matcher.strict_lines` to force that pattern back onto the always-safe original behavior — match found only as the *entire* content of one of the student's actual lines, not just found somewhere inside a longer one — for every line on that question that matches an entry there, while every other line still gets the more lenient search:

   ```yaml
   matcher:
     type: exact_trim
     strict_lines: ["Coprime", "Not Coprime"]
   ```

   Add `ignore_typo: true` to a test whose expected output contains a word students are prone to misspelling in an otherwise-correct `printf` (e.g. `INVALID OPERATOR`, `EXACT PAYMENT, ENJOY!`) — a real batch of submissions turned up `aperator` for `operator` and similar single-letter slips getting 0 marks on tests where the student's actual logic passed:

   ```yaml
   - name: "unknown_op"
     in: "5 ? 3"
     out: "INVALID OPERATOR"
     weight: 5
     ignore_typo: true   # tolerates e.g. "INVALID APERATOR"
   ```

   It's tried only as a fallback once the test's normal matcher has already failed, and only forgives a single-edit slip (an inserted/deleted/substituted letter, or two adjacent letters swapped — e.g. `PAYMETN` for `PAYMENT`) in a word at least 4 letters long — never on a word containing a digit, so a wrong computed number (`43` for expected `42`, also "1 edit away") is never excused as a typo. It's a per-test-case flag rather than a `matcher:` option because the misspelling-prone word is usually confined to one specific test's expected output, not the whole question. The UI's question editor has an "ignore typo" checkbox alongside "partial credit" on each test case.

   Needs `math.h` (`sqrt`, `pow`, `floor`, ...)? Add `-lm` to `compile.flags` — without it, compilation fails with `undefined reference to 'sqrt'` (or whichever function), even though `#include <math.h>` alone looks like it should be enough. The grader places it in the right spot on the compile command line for you regardless of where it's listed among `flags`, so just add it; you don't need to worry about `-lm` needing to come *after* the source file for GNU ld to actually resolve it. It's one of the checkboxes in the UI's Compile section, alongside `-Wall`/`-Wextra`/`-O2`/`-O0`/`-g` — leave it unchecked for questions that don't need it.
4. Run with `--only-question q3_<name> --check-gold` first (or `--dry-run` to check discovery without compiling) — `--check-gold` is what actually catches a broken/missing flag like the `-lm` case above, since it compiles+runs the gold solution the same way `--dry-run` never does. Then a real grading pass once it reports OK.

Matcher types available: `exact_trim` (default — forgives trailing whitespace/newline differences), `exact` (byte-for-byte), `token` (whitespace-insensitive), `float_tol` (numeric comparison with an `eps` tolerance), `custom` (drop a `checker.py` with `check(expected, actual, test_input) -> bool` next to `question.yaml` for questions with multiple valid outputs).

A custom checker gets the raw actual text and full control — none of the `matcher:` options below or `ignore_typo` apply automatically to it (the framework's normalization/typo-tolerance code doesn't know what a given checker is even comparing), and neither do the "reasons" shown for a tolerance-only pass. If the question still needs any of that, `check()` can read this question's own `matcher.options` out of its neighboring `question.yaml` and re-apply that logic itself — see `questions/lab_02/q6_multi_distance/checker.py` for a worked example that also filters out an interactive menu before comparing.

A custom checker can optionally also define `extract(actual: str) -> str` alongside `check`. This matters specifically for `partial`/`ignore_typo`: both are implemented once, generically, as a **raw** line-by-line comparison of `expected_text` against the student's *unfiltered* stdout — that comparison happens outside the checker and knows nothing about what it filters out. If a checker discards interleaved text (e.g. a menu) that `expected_text` never had to begin with, those raw lines never line up and both tolerances silently become no-ops. `extract`, when present, is applied to stdout first so it's reduced to the same shape as `expected_text` before either tolerance runs. Skip it and both simply keep behaving as if this hook didn't exist — this is the same behavior questions using `custom` had before `extract` was added.

### Tolerating common real-submission mistakes (`matcher` options)

Grading real (not synthetic) submissions surfaces a predictable set of harmless mistakes that have nothing to do with whether a student's logic is correct. Five independent, opt-in-per-question options under `matcher:` handle them — the UI's question editor has a widget for each, right below the matcher type radios:

| What you're seeing | Option | Example |
| --- | --- | --- |
| `printf("Enter marks: ");` right before `scanf` (no `\n`) glues the prompt onto the same line as the real output | `allow_extra_output: true` | actual `Enter marks: PASS` still matches expected `PASS` |
| Wrong case | `ignore_case: true` | actual `pass` matches expected `PASS` |
| Missing/extra spacing around an operator | `ignore_whitespace: true` | actual `root1=2.00` matches expected `root1 = 2.00` |
| Stray punctuation | `ignore_punctuation: true` | actual `INVALID:` matches expected `INVALID` |
| Student used one of several interchangeable symbols/spellings for the same thing | `symbol_groups: [[...], ...]` | with `symbol_groups: [["x", "X", "*"]]`, actual `50 * 1` or `50 X 1` matches expected `50 x 1` |

```yaml
matcher:
  type: exact_trim
  ignore_case: true
  allow_extra_output: true
```

Leave all five off (the default) for a question where the exact text *is* the point — e.g. a case-toggle question's second line needs to catch a student who got the case wrong, so `ignore_case` has no business being on for that question even if it's enabled elsewhere. Pick per question, not globally; a typo'd option name (`ignor_case`) is a load-time error, not a silently-ignored no-op.

`symbol_groups` is a list of independent groups, each a list of interchangeable symbols — within a group, every symbol collapses to that group's first (canonical) symbol before any other comparison runs, independent of `ignore_case`:

```yaml
matcher:
  type: exact_trim
  symbol_groups: [["x", "X", "*"], [":", "="]]
```

Here, `x`/`X`/`*` are interchangeable with each other, and separately `:`/`=` are interchangeable with each other — the two groups don't interact. Added for `q05_vending_change`, whose expected output uses `x` as a "times" separator (`"50 x 1"`) and where students substitute `*` (a reasonable multiplication-sign habit) or `X`. A symbol can only belong to one group (the loader rejects a `question.yaml` where it appears in two — otherwise which group's canonical symbol wins would be ambiguous), and each group needs at least 2 symbols (a group of 1 has nothing to collapse). Symbols can be more than one character (e.g. `[["PASS", "PASSED"]]`), matched literally, longest-first within a group so a short symbol never shadows a longer one that contains it.

`allow_extra_output` is narrower than it sounds — it only forgives extra text that's a **prefix** on the same line as the real answer (the `"Enter marks:"` case it exists for). Both the suffix requirement and the numeric-prefix heuristic exist because grading a real batch of submissions turned up cases the original "expected text appears anywhere" version was wrongly letting through:

* A logic bug running two `if` branches instead of one printed both answers back-to-back (`PASSINVALID` for expected `PASS`) — `PASS` is still a substring of that, so plain containment passed it. Requiring the actual output to *end with* the expected text (not just contain it) rejects this, since the real answer is no longer how the output ends.
* A wrong computed value leaking in front of the real message (`-1.00000Imaginary root` for expected `Imaginary root`, from a discriminant that should never have been printed) is structurally identical to a legitimate leftover prompt — both are "some prefix, then the right answer." For **numeric expected output**, the implementation applies one further heuristic: after removing at most one `.` and one `-`, it rejects the leftover prefix if the entire prefix is numeric. Thus, for expected `"0"`, an actual output such as `"0.00"` is rejected because the leftover prefix `"0.0"` is numeric, while `"Enter marks: 100"` for expected `"100"` is accepted because the leftover prefix `"Enter marks: "` is not entirely numeric.

  This is deliberately **not** a rule that says the prefix must contain no digits. A mixed textual prefix such as `"Enter marks: 1"` is still tolerated because the prefix as a whole is not numeric. Likewise, when the expected output is non-numeric, the numeric-prefix restriction does not apply: `"Enter marks-5INVALID"` is accepted for expected `"INVALID"`.

The trade-off is documented rather than accidental: this heuristic catches purely numeric leakage before a numeric answer, while preserving the intended tolerance for textual prompts. It can still tolerate a textual prompt containing digits, such as `"Enter a score (0-100): "`, because that prefix is not entirely numeric. `allow_extra_output` should therefore still be enabled only where the specific failure mode it exists for — an unflushed prompt before `scanf` — is the actual concern.

**A tolerated pass is never silent in the report.** Every one of the six tolerances above (the five `matcher:` options plus `ignore_typo`) only ever *rescues* a test that would otherwise have failed — and whenever one does, `report_<roll_no>.md` names exactly which one(s), the same way a `FAIL` names its category (`TIMEOUT`, `RUNTIME_ERROR`, ...):

```text
- `unknown_op`: PASS (spelling not matched, case not matched)
  - input: ''
  - expected: 'INVALID OPERATOR'
  - actual: 'invalid aperator'
```

instead of a plain `PASS` that looks identical to an exact match. The reason text: `case not matched` (`ignore_case`), `spacing not matched` (`ignore_whitespace`), `got extra punctuation` (`ignore_punctuation`), `got something extra in output` (`allow_extra_output`), `used a different (but interchangeable) symbol` (`symbol_groups`), `spelling not matched` (`ignore_typo`). An exact match — or a question with none of these enabled — still reads as plain `PASS`, exactly as before; the input/expected/actual detail (normally shown only on `FAIL`) is also shown for a tolerated `PASS`, since a reason with nothing to check against wouldn't be actionable. If more than one tolerance was actually necessary for the same test (e.g. both `ignore_case` and `ignore_typo` enabled, and the student's output has both a case slip and a misspelling), all of them are listed, `ignore_typo` first.

### Tolerating the missing-space-before-`%c` scanf mistake (`adjust_char_input`)

A very common beginner mistake: `scanf("%d", &n);` followed by `scanf("%c", &c);` — the `Enter` key that ended the numeric input leaves a `\n` sitting in stdin, and the `%c` read immediately consumes that leftover `\n` instead of the character the student actually typed. The standard fix is a leading space in the format string (`" %c"`), which tells `scanf` to skip any pending whitespace first — but plenty of otherwise-correct submissions never learned that, and fail every test that reads a `char` even though their actual logic is right.

Set a top-level (not under `matcher:`) boolean to have the grader compensate:

```yaml
adjust_char_input: true
```

Before compiling a submission, the grader scans every `scanf` call in the student's source (in program order) for each `%c` in its format string and decides whether it's already safe:

* it's the very first `scanf` call in the program, and `%c` is the first thing in its format string (nothing has been read yet, so there's no leftover newline to trip over);
* it's already preceded by whitespace in the same format string (a literal space/tab/newline, or `\n`/`\t`);
* it's the first thing in its format string, and the *previous* `scanf` call's format string itself ended in whitespace (that trailing whitespace already consumed the leftover newline before this call runs).

Anything else gets a space inserted right before the `%c`. Independently, a comma anywhere in a format string that contains `%c` is always treated as a mistake and stripped out (a comma is a literal character `scanf` must match verbatim against the input, which the test input files here are never written to satisfy).

None of this touches the student's submitted file. If a fix is needed, a patched copy is written alongside the compiled binary (`student_scanf_fixed.c` in that student+question's run directory — shared with `adjust_scanf_address` below, since both fixups can land in the same patched copy) and *that* copy is what actually gets compiled and run for grading. The mistake is never silent, though — same as a tolerated `matcher` pass, it's named in `report_<roll_no>.md`:

```text
- scanf `%c` mistake(s) detected -- your submission was compiled with a corrected copy for grading (your submitted file is unchanged):
  - line 8: scanf("%c", ...) -- missing whitespace (space or \n) before %c in scanf (adjusted to scanf(" %c", ...) for grading)
```

Leave it off (the default) for a question that specifically exists to teach this `scanf` gotcha. The UI's question editor has a checkbox for it under "Input handling", separate from the `matcher:` tolerances since this changes what gets compiled, not how output is compared.

### Tolerating the missing-`&`-before-a-scanf-argument mistake (`adjust_scanf_address`)

Another very common beginner mistake, independent of the one above: `scanf("%d", n);` instead of `scanf("%d", &n);`. `scanf` always writes through a pointer, so without the `&` the variable is never actually written — on most systems this is silent undefined behavior rather than a crash, so the program runs to completion and just behaves as if the input was never read (e.g. every `if`/`switch` on that variable falls through to its "invalid" branch). It's a particularly confusing bug for a student to spot precisely because nothing crashes.

```yaml
adjust_scanf_address: true
```

Before compiling a submission, the grader scans every `scanf` call for arguments that look like this mistake, and only fixes an argument when it's confident doing so is correct — it never guesses:

* the argument must be a bare variable name (`n`, not `&n`, `*p`, `arr[i]`, or any other expression);
* that name must be declared as a plain scalar (`int`, `char`, `float`, `double`, `long`, `short`, with or without `unsigned`/`signed`) somewhere in the file, and never as a pointer or array anywhere in the file — a name used both ways (e.g. reused across separate functions) is ambiguous and is left alone entirely;
* its conversion must be one that actually expects a pointer — `%s`, `%S`, and `%[...]` already expect a bare array/pointer with no `&`, so those are left alone;
* the format string's argument-consuming specifier count (skipping `%%` and suppressed `%*d`-style assignments) must exactly match the number of arguments passed — a mismatched call is a different, unrelated problem and is left alone rather than risk pairing the wrong specifier with the wrong argument.

Same reporting and same patched-copy mechanism as `adjust_char_input` (and the two compose freely — both fixups can apply to the same submission, in the same patched copy):

```text
- scanf missing-`&` mistake(s) detected -- your submission was compiled with a corrected copy for grading (your submitted file is unchanged):
  - line 4: scanf(...) -- missing '&' before player1 (adjusted to &player1 for grading)
```

This is a textual heuristic, not a real C parser, so it doesn't track variable scope and won't catch every declaration style — but it never trades that incompleteness for a wrong edit; when it isn't sure, it does nothing. It also doesn't (yet) handle a missing `&` on an array element (`scanf("%d", arr[i])`, which should be `&arr[i]`) — only bare variable names.

### Requiring or forbidding specific C constructs (`code_checks`)

For a question that exists to make students practice a specific construct (switch, ternary, bitwise operators, ...), matching output alone lets a student route around the point of the exercise entirely. `lab_quest/W01/bitwise_Q.txt` is a good example of the pattern this is for: it both requires bitwise operators and forbids the ways around them (`%`, `if`/`else`, `toupper`/`tolower`):

```yaml
code_checks:
  mode: gate          # "gate", "bonus", or "penalty" -- see below
  require: [bitwise_and, bitwise_xor]
  require_match: all  # "any" (default) or "all" -- see below
  forbid: [modulo, if_else, "function_call:toupper", "function_call:tolower"]
```

This is optional and off by default — most questions don't need it. It runs as a static check on the source (comments and string/char literal contents are stripped first, so a `"%d"` format specifier is never mistaken for the modulo operator), independent of whether the tests pass. Built-in construct names: `switch`, `case`, `if`, `else`, `if_else`, `for_loop`, `while_loop`, `do_while`, `modulo`, `ternary`, `bitwise_and`, `bitwise_or`, `bitwise_xor`, `bitwise_not`, `bitwise_shift_left`, `bitwise_shift_right`, `function_def_used`, `function_call:<name>`; `regex:<pattern>` is an escape hatch for anything else.

`function_def_used` is for a question that wants "write and use your own function" without pinning down its name — unlike `function_call:<name>`, which needs an exact name. It's satisfied when the source defines any function (other than `main`) that's actually *reachable from `main`* through a chain of real calls, not just present somewhere in the file — so a function that only calls itself, or a pair of functions that only call each other with nothing ever reaching them from `main`, correctly don't count. See [AUTOGRADER_DESIGN.md §3.1.1](AUTOGRADER_DESIGN.md#311-name-agnostic-defined-and-used-a-function-check-function_def_used) for the detection mechanism, its documented limitations (function pointers, multi-line macro bodies, deeply nested call arguments).

`require_match` controls how `require` is satisfied: `any` (default) needs at least one of the listed constructs present, `all` needs every one of them. There's no equivalent setting for `forbid` — a single forbidden construct found anywhere is always a violation. Use `all` when the constructs are genuinely independent requirements (e.g. `bitwise_Q.txt` needs *both* `bitwise_and` for one part of the problem and `bitwise_xor` for another); leave it at the `any` default when they're acceptable alternatives (e.g. "use a loop" — `for_loop` or `while_loop` either one should count, not both).

Four `mode`s, picked per question:

| Mode | Effect on violation | Extra key |
| --- | --- | --- |
| `gate` | Zeroes the *entire* question's marks, regardless of test results. | none |
| `bonus` | Withholds a separate `marks:` bucket that's otherwise added on top — **not** true extra credit, since `total_marks` always includes that bucket, so an unsatisfied bonus caps you below the max rather than adding free points. | `marks: <n>` |
| `penalty` | Deducts a fixed `penalty:` amount from whatever the tests already earned, floored at zero (`max(0, test_marks - penalty)`) — the total available marks are unchanged, unlike `bonus`. | `penalty: <n>` |
| `line_gate` | For `partial: true` tests only — ties one construct to one 0-based *output line*, so only that line's own share of the partial credit is zeroed when the construct is missing, not the whole question. See below. | `line_gate: <map>` |

```yaml
code_checks:
  mode: penalty
  penalty: 15          # marks docked from this question if violated, never below 0
  require: [switch]
```

`gate`/`bonus`/`penalty` all act on the question's marks as a whole — a question with two independent parts (e.g. one line of output per required construct) loses everything for missing just one of them, even under `partial: true`, since text-based partial credit and construct checking were otherwise unrelated. `line_gate` closes that gap by tying a specific `require`d construct to a specific expected-output line index (0-based); a line only counts toward partial credit when its text matches **and** its gated construct was found in the source — an otherwise-byte-correct line produced the "wrong" way (e.g. `%` instead of `&`) loses only that line's marks:

```yaml
code_checks:
  mode: line_gate
  require: [bitwise_and, bitwise_xor]
  require_match: all
  forbid: [modulo]      # still checked/reported, but doesn't by itself zero a line's credit
  line_gate:
    0: bitwise_and       # expected output line 0 (e.g. ODD/EVEN) must come from bitwise_and
    1: bitwise_xor       # expected output line 1 (e.g. the case-swapped char) must come from bitwise_xor
```

Every construct named in `line_gate` must also appear in `require` (a load-time error otherwise) since the check reuses that same source scan rather than running a second one. This mode only affects tests with `partial: true`; other tests on the same question still pass/fail on text alone.

See [AUTOGRADER_DESIGN.md §3.1](AUTOGRADER_DESIGN.md) for the full design, including the unary-vs-binary `&`/`|` detection heuristic and its limits.

**Every construct above is actually checked via a real AST first** (`libclang`, `grader/code_checks_ast.py` — `pip install -r requirements.txt` gets it, it's optional), which is more precise than the text-based description above implies — e.g. `bitwise_and`/`bitwise_or` become exact instead of a heuristic. If `libclang` isn't installed, or parsing a particular submission fails or looks incomplete, that construct (or the whole file) automatically falls back to the regex-based check described above, and `report_<roll_no>.md` says so explicitly (`- note: libclang check failed (...) -- falling back to regex-based check`) rather than silently doing something different. See [AUTOGRADER_DESIGN.md §3.1.2](AUTOGRADER_DESIGN.md#312-ast-based-checks-via-libclang-with-regex-fallback) for the construct → AST-node mapping and how it was verified against every real question's actual grading output.

## Reading the output

* **`summary.csv`** — one row per student (sorted by roll number), one column per question (`earned/total`), a `total` column, and an `anomalies` column. When `--student-csv` (or the default `ip_student_mapping.csv`, see the flags table above) resolves to a real mapping, `ip` and/or `name` columns are added at the very start, ahead of `roll_no`. Opens directly in Excel/Sheets.
* **`report_<roll_no>.md`** — per student: compile status, every open **and** hidden test with full input/expected/actual on failure (nothing shown for a pass either way; this file is instructor-only, never distributed raw to students), and for questions with `code_checks` configured, which required constructs were found/missing and which forbidden ones were used, with line numbers — including, in `gate` mode, what the test score would have been absent the violation. A failure whose run itself was fine just reads `FAIL`; a failure from an abnormal run (timeout, crash, ...) names the reason, e.g. `FAIL (TIMEOUT: ...)`; a `partial: true` test that matched some but not all lines reads `PARTIAL (1/2 lines, +5/10 marks)` instead of either.
* **`run.log`** — everything: gold self-check results, per-test sandbox status, anomalies, which sandbox backend was actually used.

## Web UI

A local browser UI (`ui/`) for browsing labs and authoring questions without hand-editing YAML — deliberately unstyled (white background, black text, no custom fonts) except for syntax-highlighted code views. It does **not** run grading itself; that stays a deliberate CLI action per §Running a grading batch. Start it from `lab_auto_grader`:

```bash
source ~/venv/bin/activate
python3 -m ui.app
```

Then open `http://127.0.0.1:5000/`. Binds to localhost only — there's no authentication, so don't expose this beyond your own machine.

What it does:

* **Tree view**: `Auto-grader → lab_NN → questions / submissions / result`, expandable via native `<details>` elements. Only one lab is open at a time, and only one of questions/submissions/result per lab — expanding one closes the others. `+ Add Lab` prompts for an id and creates `questions/<id>/` + `submissions/<id>/`; an expanded lab shows a `Delete` button (confirmed before it removes that lab's questions, submissions, *and* results — irreversible).
* **Submissions**: click a student's `.c` file to view it read-only, syntax-highlighted (via Pygments) in a modal — the one place color is used deliberately, to look like a code editor.
* **Result**: shows the *latest* run only (`raw/` excluded). Click a `report_*.md` to see it rendered as Markdown, `summary.csv` as an actual table, `run.log` as plain text.
* **Questions**: click an existing question, or `+ Add Question`, to open a form covering every `question.yaml` field (marks, limits, compile standard/flags, matcher, `code_checks` with live mode/require/forbid fields, open/hidden test cases with add/remove) plus a `gold.c` textarea — organized as expandable sections. `Save` validates through the same `config_schema.load_question` the CLI uses (a bad weight sum or an unknown `code_checks` construct name is rejected with the same error message you'd get from the CLI, and nothing on disk is left half-written if it fails). `Delete` (existing questions only) is confirmed first. An existing question's `id` is fixed — renaming means deleting and re-creating, since nothing currently migrates a folder rename plus whatever might reference the old id.
* The `code_checks` `require`/`forbid` fields offer a checkbox per built-in construct (§3.1) plus a free-text field for `function_call:`/`regex:` entries not covered by a checkbox — present on **both** `require` and `forbid` (a deliberate addition beyond what was asked for: without it, editing and re-saving a question that already used an advanced `require` entry authored by hand would silently drop it).

**Caveat**: this was built and verified against the real `lab_01` data via the API directly (`curl` against every endpoint — tree listing, question read/save/delete with both a validation-failure rollback and a successful round-trip, submission file highlighting, result rendering — see git history for the exact checks) and the JS was parsed for syntax errors, but **not opened in an actual browser** — there's no browser-automation tool available in this environment to click through the tree, exercise the accordion behavior, or open the modals for real. Please try it in a browser before relying on it, and report anything that doesn't behave as described above.

## Example workflow

1. After student submission folders are received (directory containing zip folders (named by ip address of the system they used in lab), each containing a single student folder named with roll number, inside contains the c programs for each question). To have the lab submission in required format, run the following command to extract the zip files and rename the folders to roll numbers:

  ```bash
  python extract_submissions.py \
    --archive-dir /path/to/archives_dir \
    --lab-dir submissions/lab_no \
    --student-csv /path/to/students_name_rollno.csv
  ```

2. After the submissions are extracted and renamed, run the following command to grade the submissions:

  ```bash
  python -m grader.grade \
    --questions questions/lab_no \
    --submissions submissions/lab_no \
    --out runs/lab_no \
    --sandbox isolate \
    --parallel 4
  ```

3. To view the results, or student-wise/question-wise evaluation of each student, run the following command to start the web UI:
  
  ```bash
  python -m ui.app
  ```

4. If you change a few things about question configuration, scoring, etc, and rerun the grader, you can find only the changed marks by running following command:

  ```bash
  python find_marks_changes.py --directory ./runs/lab_no
  ```

## Live Lab Platform (v2) — Run labs in real-time with immediate feedback

In addition to the offline batch grader (above), this project includes a **live lab platform** for conducting lab sessions in real-time on a lab PC. Students write C code in their browser, click "Run" to get instant feedback against open tests, and the instructor can monitor progress, control the timer, and finalize grading — all using the same underlying grading pipeline.

Full design rationale, architecture, accounts/auth model, and security considerations live in [LIVE_LAB_DESIGN.md](LIVE_LAB_DESIGN.md). This section covers the **day-to-day workflow**.

### How it works (briefly)

Two separate Flask servers run on the instructor's lab PC:

- **`server_student/`** (port 5001): Students log in with roll number + password, write C code in a CodeMirror editor, click "Run" for instant compile+test results against open tests only (hidden tests never sent to student side). Timer counts down; code autosaves every ~60s and on blur/tab-close. Once the session ends/locks, code becomes read-only.

- **`server_admin/`** (port 5002): Instructor logs in, sees a live dashboard of which students are online and their per-question progress, controls the session (start/extend/lock timer), manages student accounts (reset password, sign out a device, lock/unlock a misbehaving student mid-session), and finalize+grades at session end using the exact same batch grader pipeline.

Both servers read/write shared files on disk (`live/<lab>/students.csv` for accounts, `session.json` for timer state, `submissions/<lab>/` for live code) — no database needed. When the session ends, the instructor clicks "Finalize & Grade", which runs the batch grader on all saved code and publishes results/leaderboard back to students.

### Setup

**One-time:**

```bash
# Install additional dependencies for live platform
pip install -r requirements.txt  # adds Flask, python-dotenv, filelock

# Initialize admin credentials (prompted for username + password)
python -m grader.manage_accounts init-admin

# (Optional) Create a student name mapping CSV with name,roll_no columns and point to this file via .env so admin dashboard shows names
echo "STUDENT_NAMES_CSV=student_names.csv" >> .env
```

**Per lab — accounts and passwords:**

Student accounts are **global** (not per-lab) — once a student logs in anywhere, their password is set globally and reused across all labs. The first login uses a deterministic default password (`default_password(roll_no)` = roll number reversed + "@Cp"); the student must then create their own password.

There are three ways to manage student accounts:

**Option 1: Auto-generate with random passwords (recommended for first-time setup)**

```bash
# Create a roster of students (roll numbers, one per line)
cat > roster.txt <<EOF
112201001
112201002
112201003
EOF

# Generate accounts + passwords for this lab (creates live/accounts.csv if it doesn't exist)
python -m grader.manage_accounts generate \
  --lab lab_01 \
  --roster roster.txt

# Passwords are printed — share with students (shown once, never recoverable)
# This creates live/accounts.csv (global, shared across all labs)
```

**Option 2: Let students use the default password on first login**

```bash
# No pre-generation needed. Students log in with:
#   - roll_no: their roll number (e.g., 112201001)
#   - password: roll_no reversed + "@Cp" (e.g., 112201001 → 100102211@Cp)
# On first login, they're prompted to create their own password.
```

**Option 3: Manually reset/set a password**

```bash
# Set a specific password for a student (prompts for password, or use --password to script)
python -m grader.manage_accounts reset-student --roll 112201001

# Or script it:
python -m grader.manage_accounts reset-student --roll 112201001 --password "MyNewPassword123"
```

**Global accounts file:**

All student passwords are stored in `live/accounts.csv` (created automatically on first use). This file is **shared across all labs** — a student's password is the same whether they're doing lab_01, lab_02, or lab_03. The per-lab `live/lab_01/students.csv` (created when the session starts) tracks only that lab's session state (IP, last seen, locked status), not passwords.

```
live/
├── accounts.csv              # Global, all students, all labs — password_hash, password_set flag
├── lab_01/
│   ├── students.csv          # Per-lab session state for lab_01 only
│   └── session.json
└── lab_02/
    ├── students.csv          # Per-lab session state for lab_02 only
    └── session.json
```

**Student name mapping (optional):**

To show student names in the admin dashboard (instead of just roll numbers), create a CSV:

```bash
# Format: roll_no,name,ip (ip is optional, auto-filled at login)
cat > student_names.csv <<EOF
roll_no,name,ip
112201001,Alice Singh,
112201002,Bob Khan,
112201003,Carol Das,
EOF

# Point to it via .env
echo "STUDENT_NAMES_CSV=student_names.csv" >> .env
```

Then start the servers — the admin dashboard will show names alongside roll numbers.

### Running a live lab session

**1. Start the two servers** (on the lab PC, in separate terminals):

```bash
# Terminal 1 — Student server (students point browsers to http://<lab-pc-ip>:5001)
python -m server_student.app --lab lab_01 --port 5001

# Terminal 2 — Admin server (instructor browses to http://127.0.0.1:5002/admin)
python -m server_admin.app --port 5002
```

**2. Admin logs in** at `http://127.0.0.1:5002/admin` with the credentials from `init-admin`.

**3. Instructor controls the session:**

- **Session tab**: Click "Start session", enter duration (e.g., 60 minutes) → timer starts counting down on all student screens.
- **Accounts tab**: See all students, their login/device status, and can reset passwords, sign out devices, or **lock** a student mid-session (disables save/run for that student until unlocked).
- **Live status tab**: Watch per-student, per-question progress (who's online, last run timestamp, open-test pass count).
- **Finalize & Grade**: Once timer expires or you manually lock the session, this button becomes enabled. Click it to run the batch grader against all saved code, produce `summary.csv` + `report_*.md` + leaderboard → published back to students.

**4. Students log in** at `http://<lab-pc-ip>:5001/login` with their roll number + the password they were given.

- Editor loads, question list on left, code in center, results below.
- Click "Run" to compile and test against open tests only (hidden tests stay hidden).
- Code autosaves every ~60s, plus on tab-close or switch.
- A 5-minute warning appears when time is running low.
- Once session locks (time up or instructor locks it), editor becomes read-only, code can't be run.
- After finalization, students can see their report and the leaderboard.

### Key features

| Feature | Why it matters |
| --- | --- |
| **Real-time feedback** | Students see compile errors and test results instantly, can fix and re-run within the same session. |
| **Open tests only** | Hidden tests stay on the server, never sent to student processes — prevents cheating by reverse-engineering test data. |
| **Autosave** | Code autosaves every ~60s and on tab-close via `sendBeacon`, so students don't lose work if their browser crashes. |
| **5-minute warning** | Alert pops up when 5 minutes remain, reminding students to finish and run their code before lock. |
| **Per-student lock** | Instructor can lock a disruptive student mid-session without affecting others — they can still view code and results, just can't modify/run. |
| **Live dashboard** | Instructor sees who's online, how far each student has progressed, and can spot patterns (e.g., many students stuck on the same question). |
| **One-command finalize** | Session ends → instructor clicks "Finalize" → batch grader runs, reports generated, leaderboard published — all reusing the exact offline grading pipeline, so marks are always consistent. |

### Files created at runtime

```
live/lab_01/
├── students.csv                # Generated by manage_accounts; updated by server_student on login/lock/unlock
├── session.json                # Created when session starts; status/timer state updated by server_admin
└── display_config.json         # Toggles (show_workspace_after_session, show_report, show_leaderboard) — runtime-editable by admin

submissions/lab_01/
├── 112201001/
│   ├── q1.c                    # Live code saved by server_student on Run/autosave
│   ├── q1_live.md              # Admin-visible live report for this question (rewritten on each Run)
│   └── q2.c
└── 112201002/
    └── ...
```

### See also

- [LIVE_LAB_DESIGN.md](LIVE_LAB_DESIGN.md) — Full architecture, security model, accounts/auth flow, concurrency, and sandbox pool design.
- `grader/accounts.py` — Account management (authenticate, lock/unlock, reset password, bind device).
- `grader/live_session.py` — Timer state (start, lock, check if writes are allowed).
- `server_student/app.py` — Student-facing routes (login, questions, save, run, autosave via heartbeat).
- `server_admin/app.py` — Admin-facing routes (session control, accounts, live dashboard, finalize).
- `server_student/static/editor.js` — Client-side editor logic (CodeMirror, autosave, local timer ticker, lock/unlock UI updates).
