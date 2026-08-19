"""Sandboxed compile/execute backends.

Two backends behind one interface:
  - IsolateSandbox           -- primary, backed by github.com/ioi/isolate
  - SubprocessRlimitSandbox  -- fallback, plain subprocess + resource.setrlimit,
                                used when `isolate` isn't installed/working on
                                this machine

Neither backend does output-vs-expected comparison -- that's scorer.py's job.
This module only answers "did it compile, and what happened when it ran".
"""

from __future__ import annotations

import os
import pwd
import resource
import shutil
import signal
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from grader.config_schema import Limits

# Generous limits for the *compiler itself* -- gcc needs much more time/memory
# than the tiny student programs it's compiling, and it forks helper processes
# (cc1, as, collect2) so process-count must allow for that too.
COMPILE_TIME_SECONDS = 10.0
COMPILE_WALL_SECONDS = 15.0
COMPILE_MEMORY_MB = 512
COMPILE_MAX_PROCESSES = 8
COMPILE_FLAGS_MAX_STDERR = 4000  # chars kept in remarks


@dataclass
class CompileResult:
    success: bool
    stderr: str
    binary_path: Path | None


@dataclass
class RunResult:
    status: str  # OK | WRONG_OUTPUT_PENDING | TIMEOUT | RUNTIME_ERROR | MEMORY_LIMIT_EXCEEDED | SANDBOX_VIOLATION
    exit_code: int | None
    signal_name: str | None
    stdout: str
    stderr: str
    time_used: float | None
    wall_time_used: float | None
    memory_used_kb: int | None
    message: str


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated, {len(text) - limit} more bytes]"


def _split_link_flags(flags: list[str]) -> tuple[list[str], list[str]]:
    """`-l<name>` (e.g. -lm for math.h's sqrt/pow/...) must appear *after*
    the object/source files that reference it on the command line, or GNU ld
    fails with "undefined reference" even though the library is linked --
    the library is only searched for symbols still unresolved at the point
    ld reaches it, and there are none yet if it comes first. Compiler flags
    (-Wall, -O2, -std, ...) have no such ordering constraint. Split so
    callers can place -l flags last regardless of how they were listed in
    question.yaml."""
    compile_flags = [f for f in flags if not f.startswith("-l")]
    link_flags = [f for f in flags if f.startswith("-l")]
    return compile_flags, link_flags


class Sandbox(ABC):
    """One Sandbox instance is bound to one worker slot and is reused across
    that worker's whole run (many students, many questions, many tests)."""

    @abstractmethod
    def compile_c(
        self, source: Path, dest_binary: Path, standard: str, flags: list[str]
    ) -> CompileResult:
        ...

    @abstractmethod
    def run(self, binary: Path, stdin_text: str, limits: Limits) -> RunResult:
        ...

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --------------------------------------------------------------------------
# isolate backend
# --------------------------------------------------------------------------

_SIGNAL_NAMES = {v: k for k, v in signal.__dict__.items() if k.startswith("SIG") and isinstance(v, int)}


class IsolateBoxError(RuntimeError):
    """isolate itself failed to set up/tear down a box -- an environment
    problem, not a student-code problem."""


class IsolateSandbox(Sandbox):
    def __init__(self, box_id: int, isolate_bin: str = "isolate", meta_dir: Path | None = None):
        self.box_id = box_id
        self.isolate_bin = isolate_bin
        # isolate writes the --meta file as the *invoking* user, deliberately,
        # so it can't be tricked into overwriting root-owned files -- it must
        # therefore live somewhere this process actually owns, never under
        # isolate's own box_root (that's root:root and not writable by us).
        self.meta_dir = Path(meta_dir) if meta_dir else Path(
            __import__("tempfile").mkdtemp(prefix=f"isolate_meta_{box_id}_")
        )

    def _base_cmd(self) -> list[str]:
        return [self.isolate_bin, f"--box-id={self.box_id}", "--cg"]

    def _init_box(self) -> Path:
        proc = subprocess.run(
            self._base_cmd() + ["--init"], capture_output=True, text=True, timeout=30
        )
        if proc.returncode != 0:
            raise IsolateBoxError(f"isolate --init failed (box {self.box_id}): {proc.stderr.strip()}")
        return Path(proc.stdout.strip()) / "box"

    def _cleanup_box(self) -> None:
        subprocess.run(self._base_cmd() + ["--cleanup"], capture_output=True, text=True, timeout=30)

    @staticmethod
    def _parse_meta(meta_path: Path) -> dict[str, str]:
        meta: dict[str, str] = {}
        if not meta_path.exists():
            return meta
        for line in meta_path.read_text(errors="replace").splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key] = value
        return meta

    def _classify(self, meta: dict[str, str], mem_limit_kb: int) -> tuple[str, str, str | None]:
        """Returns (status, message, signal_name)."""
        status = meta.get("status")
        cg_mem = meta.get("cg-mem")
        if cg_mem is not None and int(cg_mem) >= mem_limit_kb:
            return "MEMORY_LIMIT_EXCEEDED", f"exceeded memory limit ({cg_mem} KB used)", None

        if status is None:
            return "OK", "", None
        if status == "TO":
            return "TIMEOUT", "exceeded time/wall-time limit", None
        if status == "SG":
            sig = int(meta.get("exitsig", "0"))
            sig_name = _SIGNAL_NAMES.get(sig, f"signal {sig}")
            return "RUNTIME_ERROR", f"killed by {sig_name}", sig_name
        if status == "RE":
            code = meta.get("exitcode", "?")
            return "RUNTIME_ERROR", f"exited with code {code}", None
        if status == "XX":
            return "SANDBOX_VIOLATION", f"isolate internal error: {meta.get('message', '')}", None
        return "RUNTIME_ERROR", f"unrecognized isolate status '{status}'", None

    def compile_c(
        self, source: Path, dest_binary: Path, standard: str, flags: list[str]
    ) -> CompileResult:
        box_dir = self._init_box()
        meta_path = self.meta_dir / f"compile_meta_{self.box_id}.txt"
        try:
            shutil.copy(source, box_dir / "student.c")
            compile_flags, link_flags = _split_link_flags(flags)
            cmd = self._base_cmd() + [
                f"--time={COMPILE_TIME_SECONDS}",
                f"--wall-time={COMPILE_WALL_SECONDS}",
                f"--mem={COMPILE_MEMORY_MB * 1024}",
                "--fsize=8192",
                f"--processes={COMPILE_MAX_PROCESSES}",
                "--stdout=compile_out.txt",
                "--stderr=compile_err.txt",
                f"--meta={meta_path}",
                # gcc's collect2 locates `ld` via PATH (execvp) rather than a
                # hardcoded path -- isolate otherwise starts with an empty
                # environment and compilation fails with "cannot find 'ld'".
                "--env=PATH=/usr/bin:/bin",
                "--run",
                "--",
                "/usr/bin/gcc",
                f"-std={standard}",
                *compile_flags,
                "-o",
                "a.out",
                "student.c",
                *link_flags,
            ]
            subprocess.run(cmd, capture_output=True, text=True, timeout=COMPILE_WALL_SECONDS + 5)
            meta = self._parse_meta(meta_path)
            stderr_text = (box_dir / "compile_err.txt").read_text(errors="replace") if (
                box_dir / "compile_err.txt"
            ).exists() else ""
            status = meta.get("status")
            binary = box_dir / "a.out"
            if status is not None or not binary.exists():
                return CompileResult(
                    success=False, stderr=_truncate(stderr_text, COMPILE_FLAGS_MAX_STDERR), binary_path=None
                )
            dest_binary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(binary, dest_binary)
            dest_binary.chmod(0o755)
            return CompileResult(success=True, stderr=_truncate(stderr_text, COMPILE_FLAGS_MAX_STDERR), binary_path=dest_binary)
        finally:
            self._cleanup_box()

    def run(self, binary: Path, stdin_text: str, limits: Limits) -> RunResult:
        box_dir = self._init_box()
        meta_path = self.meta_dir / f"run_meta_{self.box_id}.txt"
        try:
            shutil.copy(binary, box_dir / "a.out")
            (box_dir / "a.out").chmod(0o755)
            (box_dir / "input.txt").write_text(stdin_text)
            mem_limit_kb = limits.memory_mb * 1024
            cmd = self._base_cmd() + [
                f"--time={limits.time_seconds}",
                f"--wall-time={limits.wall_seconds}",
                f"--extra-time=0.5",
                f"--mem={mem_limit_kb}",
                "--fsize=1024",
                f"--processes={limits.max_processes}",
                "--stdin=input.txt",
                "--stdout=output.txt",
                "--stderr=error.txt",
                f"--meta={meta_path}",
                "--run",
                "--",
                "/box/a.out",
            ]
            subprocess.run(
                cmd, capture_output=True, text=True, timeout=limits.wall_seconds + 5
            )
            meta = self._parse_meta(meta_path)
            status, message, sig_name = self._classify(meta, mem_limit_kb)
            stdout_text = (box_dir / "output.txt").read_text(errors="replace") if (
                box_dir / "output.txt"
            ).exists() else ""
            stderr_text = (box_dir / "error.txt").read_text(errors="replace") if (
                box_dir / "error.txt"
            ).exists() else ""
            truncated = len(stdout_text) > limits.output_bytes
            stdout_text = stdout_text[: limits.output_bytes]
            if truncated and status == "OK":
                status = "RUNTIME_ERROR"
                message = "output truncated -- exceeded output size limit"

            exit_code = int(meta["exitcode"]) if "exitcode" in meta else None
            return RunResult(
                status=status,
                exit_code=exit_code,
                signal_name=sig_name,
                stdout=stdout_text,
                stderr=_truncate(stderr_text, 2000),
                time_used=float(meta["time"]) if "time" in meta else None,
                wall_time_used=float(meta["time-wall"]) if "time-wall" in meta else None,
                memory_used_kb=int(meta["cg-mem"]) if "cg-mem" in meta else None,
                message=message,
            )
        finally:
            self._cleanup_box()

    def close(self) -> None:
        shutil.rmtree(self.meta_dir, ignore_errors=True)


def choose_sandbox_kind(requested: str, log=None) -> str:
    """Resolves a `--sandbox`-style choice ("auto" | "isolate" | "subprocess")
    to the kind actually usable on this machine, via isolate_self_test --
    shared by the batch grader (grade.py) and the live-lab platform
    (server_student) so both apply the exact same "never silently limp
    along on a half-working setup" rule from AUTOGRADER_DESIGN.md §5.2:
    an explicit `isolate` request that fails the self-test is a hard error,
    not a silent downgrade; `auto` downgrades to the degraded subprocess
    backend but always logs loudly that it did.
    """
    import logging

    log = log or logging.getLogger("grader.sandbox")

    if requested == "subprocess":
        log.warning(
            "Sandbox backend forced to 'subprocess' -- filesystem/network isolation is "
            "NOT provided, only best-effort resource limits. Use only for local testing."
        )
        return "subprocess"

    ok, detail = isolate_self_test()
    if requested == "isolate":
        if not ok:
            log.error("isolate self-test failed: %s", detail)
            raise SystemExit(
                "isolate was requested (--sandbox isolate) but is not usable on this machine. "
                "See lab_auto_grader/AUTOGRADER_DESIGN.md section 5.2 for setup steps."
            )
        log.info("isolate self-test passed.")
        return "isolate"

    # auto
    if ok:
        log.info("isolate self-test passed -- using isolate as the sandbox backend.")
        return "isolate"
    log.warning(
        "isolate self-test failed (%s) -- falling back to the degraded subprocess sandbox. "
        "Memory/process-count limits will be best-effort only; see lab_auto_grader/AUTOGRADER_DESIGN.md "
        "section 5.2 to fix isolate.",
        detail,
    )
    return "subprocess"


def make_sandbox(kind: str, box_id: int, scratch_root: Path) -> Sandbox:
    """Factory shared by the batch grader (grade.py, one Sandbox per pool
    worker) and the live-lab on-demand pool (sandbox_pool.py, one Sandbox per
    concurrent Run slot) -- one place that knows how to turn a `--sandbox`
    kind + box_id into a concrete backend instance."""
    if kind == "isolate":
        return IsolateSandbox(box_id=box_id)
    work_root = Path(scratch_root) / f"worker_{box_id}"
    work_root.mkdir(parents=True, exist_ok=True)
    return SubprocessRlimitSandbox(work_root=work_root)


def isolate_self_test(isolate_bin: str = "isolate", box_id: int = 0) -> tuple[bool, str]:
    """Run once at grader startup: confirm isolate can actually init+cleanup a
    box on this machine before trusting it for a real batch."""
    try:
        proc = subprocess.run(
            [isolate_bin, f"--box-id={box_id}", "--cg", "--init"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return False, proc.stderr.strip()
        subprocess.run(
            [isolate_bin, f"--box-id={box_id}", "--cg", "--cleanup"],
            capture_output=True, text=True, timeout=30,
        )
        return True, "ok"
    except FileNotFoundError:
        return False, f"'{isolate_bin}' not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "isolate did not respond within 30s"


# --------------------------------------------------------------------------
# subprocess + rlimit fallback backend
# --------------------------------------------------------------------------


def _current_proc_count() -> int:
    """Number of *threads* currently owned by this real UID, system-wide --
    used to offset RLIMIT_NPROC (see the comment where it's applied).
    RLIMIT_NPROC counts threads, not just processes -- counting only
    top-level PIDs badly undercounts on any desktop-ish machine (an editor
    alone can easily hold several hundred threads under one UID)."""
    uid = os.getuid()
    count = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
            count += len(os.listdir(f"/proc/{entry}/task"))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    return count


def _unprivileged_run_user() -> pwd.struct_passwd | None:
    """Resolves the user to drop to before exec'ing a *compiled student
    binary* (run(), never compile_c() -- gcc needs the invoking user's own
    permissions). Root is exempt from RLIMIT_NPROC entirely, so a fork-bomb
    run as root only stops once it wins a race against this process's
    SIGKILL -- confirmed by hand to wedge a container's whole pids cgroup
    (every subsequent compile/run then fails with EAGAIN until something
    restarts the container), even with a Docker-level --pids-limit already
    in place, because a plain `while(1) fork();` can out-fork a single
    SIGKILL sweep. Dropping to a real unprivileged UID first makes the
    *kernel* enforce RLIMIT_NPROC atomically at the fork() syscall itself --
    no race, nothing to sweep. `nobody` is used rather than a dedicated
    account because it's present on every Linux distro without setup and,
    unlike the invoking UID, reliably owns ~0 processes host-wide (the
    baseline this whole scheme assumes, and what makes it safe to set a
    small absolute nproc_limit without the desktop-machine offset dance
    _current_proc_count() otherwise needs)."""
    if os.getuid() != 0:
        return None
    try:
        return pwd.getpwnam("nobody")
    except KeyError:
        return None


class SubprocessRlimitSandbox(Sandbox):
    """Degraded fallback for machines where `isolate` isn't available. Still
    enforces CPU/wall time, address-space size, process count, and file size,
    but filesystem/network isolation is NOT provided -- the student program
    runs as the same OS user as the grader, in a private temp directory. Only
    use this when isolate_self_test() has failed and the operator has been
    told explicitly that isolation is degraded.
    """

    def __init__(self, work_root: Path):
        self.work_root = Path(work_root)
        self.work_root.mkdir(parents=True, exist_ok=True)

    def _run_subprocess(
        self, cmd: list[str], cwd: Path, stdin_text: str, timeout: float,
        cpu_seconds: float, mem_bytes: int, nproc: int, fsize_bytes: int,
        drop_to: pwd.struct_passwd | None = None,
    ) -> tuple[int | None, str | None, str, str, bool]:
        """Returns (exit_code, signal_name, stdout, stderr, timed_out).

        `drop_to`, when set, setuid/setgid's to that user *before* applying
        rlimits -- see _unprivileged_run_user() for why. In that case
        RLIMIT_NPROC can be the small caller-supplied `nproc` directly: the
        target user is expected to own ~0 processes host-wide, so there's no
        pre-existing count to offset for."""

        if drop_to is not None:
            nproc_limit = nproc
        else:
            # RLIMIT_NPROC caps the *whole real UID's* thread count
            # system-wide, not just this subtree -- setting it to a small
            # absolute number starves immediately on any account that
            # already runs other processes/threads (shell, IDE, ...).
            # Offset by the current count, plus a safety margin for the gap
            # between measuring it here and the fork actually happening on
            # a busy desktop machine.
            nproc_limit = _current_proc_count() + nproc + 32

        def _limits():
            os.setsid()
            if drop_to is not None:
                # Must happen before setrlimit(RLIMIT_NPROC, ...): once
                # unprivileged, this process can no longer raise a limit it
                # already lowered on itself, but setuid itself needs no
                # special rlimit state, and CPU/AS/FSIZE aren't
                # UID-dependent. Group first -- setuid drops the capability
                # needed to change the group afterward.
                os.setgid(drop_to.pw_gid)
                os.setuid(drop_to.pw_uid)
            resource.setrlimit(resource.RLIMIT_CPU, (int(cpu_seconds) + 1, int(cpu_seconds) + 1))
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc_limit, nproc_limit))
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))

        proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            preexec_fn=_limits,
        )
        try:
            stdout, stderr = proc.communicate(input=stdin_text.encode(), timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            stdout, stderr = proc.communicate()
            timed_out = True

        returncode = proc.returncode
        sig_name = None
        exit_code = None
        if returncode is not None and returncode < 0:
            sig_name = _SIGNAL_NAMES.get(-returncode, f"signal {-returncode}")
        elif returncode is not None:
            exit_code = returncode
        return exit_code, sig_name, stdout.decode(errors="replace"), stderr.decode(errors="replace"), timed_out

    def compile_c(
        self, source: Path, dest_binary: Path, standard: str, flags: list[str]
    ) -> CompileResult:
        workdir = Path(
            __import__("tempfile").mkdtemp(prefix="compile_", dir=self.work_root)
        )
        try:
            shutil.copy(source, workdir / "student.c")
            compile_flags, link_flags = _split_link_flags(flags)
            cmd = ["gcc", f"-std={standard}", *compile_flags, "-o", "a.out", "student.c", *link_flags]
            exit_code, sig_name, _out, err, timed_out = self._run_subprocess(
                cmd, cwd=workdir, stdin_text="", timeout=COMPILE_WALL_SECONDS,
                cpu_seconds=COMPILE_TIME_SECONDS, mem_bytes=COMPILE_MEMORY_MB * 1024 * 1024,
                nproc=COMPILE_MAX_PROCESSES, fsize_bytes=8 * 1024 * 1024,
            )
            binary = workdir / "a.out"
            if timed_out or exit_code != 0 or not binary.exists():
                return CompileResult(success=False, stderr=_truncate(err, COMPILE_FLAGS_MAX_STDERR), binary_path=None)
            dest_binary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(binary, dest_binary)
            dest_binary.chmod(0o755)
            return CompileResult(success=True, stderr=_truncate(err, COMPILE_FLAGS_MAX_STDERR), binary_path=dest_binary)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def run(self, binary: Path, stdin_text: str, limits: Limits) -> RunResult:
        workdir = Path(__import__("tempfile").mkdtemp(prefix="run_", dir=self.work_root))
        try:
            local_binary = workdir / "a.out"
            shutil.copy(binary, local_binary)
            local_binary.chmod(0o755)
            drop_to = _unprivileged_run_user()
            if drop_to is not None:
                # mkdtemp's default 0700 shuts out anyone but its owner
                # (root, in the case this exists to handle) -- world
                # readable+executable so `drop_to` can cwd into it and exec
                # a.out; world-writable too, since a student program may
                # legitimately create/write its own scratch files here.
                workdir.chmod(0o777)
            exit_code, sig_name, stdout, stderr, timed_out = self._run_subprocess(
                ["./a.out"], cwd=workdir, stdin_text=stdin_text, timeout=limits.wall_seconds,
                cpu_seconds=limits.time_seconds, mem_bytes=limits.memory_mb * 1024 * 1024,
                nproc=limits.max_processes, fsize_bytes=1024 * 1024,
                drop_to=drop_to,
            )
            truncated = len(stdout) > limits.output_bytes
            stdout = stdout[: limits.output_bytes]

            if timed_out:
                status, message = "TIMEOUT", "exceeded time/wall-time limit"
            elif sig_name:
                status, message = "RUNTIME_ERROR", f"killed by {sig_name}"
            elif exit_code not in (0, None):
                status, message = "RUNTIME_ERROR", f"exited with code {exit_code}"
            elif truncated:
                status, message = "RUNTIME_ERROR", "output truncated -- exceeded output size limit"
            else:
                status, message = "OK", ""

            return RunResult(
                status=status, exit_code=exit_code, signal_name=sig_name,
                stdout=stdout, stderr=_truncate(stderr, 2000),
                time_used=None, wall_time_used=None, memory_used_kb=None,
                message=message,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
