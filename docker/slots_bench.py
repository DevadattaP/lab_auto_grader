"""Benchmark for choosing --sandbox-slots on a given machine.

Runs N simulated concurrent students' compile+run through the real
SandboxPool ('subprocess' backend) at one or more slot counts, and reports
throughput/queue-wait so you can see where adding slots stops helping --
that's the machine's practical ceiling (CPU-bound: each slot is one
concurrently-running gcc or student binary competing for a physical core).

Usage (from inside a throwaway container -- never against the live
student/admin containers, since this creates its own sandbox pool and
scratch dir, separate from the running service's):

    docker run -d --name bench_container --pids-limit 2048 lab-auto-grader:latest tail -f /dev/null
    docker cp docker/slots_bench.py bench_container:/tmp/
    docker exec -w /app bench_container bash -c \\
        "PYTHONPATH=/app python3 /tmp/slots_bench.py --sweep 1,2,4,8,12 --students 35"
    docker rm -f bench_container

Look for where `throughput` stops climbing and `avg_compile_run` starts
rising -- that's the point where slots exceed the machine's core count and
concurrent compiles start competing for the same CPUs instead of adding
real parallelism. See DOCKER.md's `--sandbox-slots` section.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import threading
import time
from pathlib import Path

from grader.config_schema import Limits
from grader.sandbox_pool import PoolExhausted, SandboxPool

# Representative of a typical intro-C question: read input, do a bit of
# work, print output -- similar cost profile to the real questions in
# questions/lab_01 (loops, arithmetic, no heavy I/O).
SRC = """
#include <stdio.h>
int main() {
    long n;
    scanf("%ld", &n);
    long sum = 0;
    for (long i = 0; i < 20000000L; i++) { sum += i % 7; }
    printf("%ld\\n", sum + n);
    return 0;
}
"""


def run_one(pool: SandboxPool, student_id: int, results: list, results_lock: threading.Lock) -> None:
    src = Path(tempfile.mktemp(suffix=".c"))
    src.write_text(SRC)
    dest = Path(tempfile.mktemp())
    t0 = time.time()
    try:
        with pool.checkout(timeout=60) as sb:
            t_checkout = time.time()
            r = sb.compile_c(src, dest, "c11", [])
            if not r.success:
                with results_lock:
                    results.append((student_id, "COMPILE_FAIL", 0.0, 0.0))
                return
            limits = Limits(time_seconds=5, wall_seconds=8, memory_mb=128, max_processes=4, output_bytes=4096)
            rr = sb.run(dest, "5\n", limits)
            t1 = time.time()
            with results_lock:
                results.append((student_id, rr.status, t_checkout - t0, t1 - t_checkout))
    except PoolExhausted:
        with results_lock:
            results.append((student_id, "POOL_EXHAUSTED", 0.0, 0.0))


def bench_one(slots: int, n_students: int) -> None:
    pool = SandboxPool(kind="subprocess", size=slots, scratch_root=Path(f"/tmp/bench_scratch_{slots}"))
    results: list = []
    results_lock = threading.Lock()

    threads = [
        threading.Thread(target=run_one, args=(pool, i, results, results_lock)) for i in range(n_students)
    ]
    wall_start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_total = time.time() - wall_start

    ok = [r for r in results if r[1] == "OK"]
    if not ok:
        print(f"slots={slots} students={n_students} total_wall={wall_total:.2f}s ok=0/{n_students} NO SUCCESSFUL RUNS")
        return

    wait_times = [r[2] for r in ok]
    work_times = [r[3] for r in ok]
    print(
        f"slots={slots} students={n_students} total_wall={wall_total:.2f}s ok={len(ok)}/{n_students} "
        f"avg_queue_wait={sum(wait_times) / len(wait_times):.2f}s "
        f"avg_compile_run={sum(work_times) / len(work_times):.2f}s "
        f"max_queue_wait={max(wait_times):.2f}s "
        f"throughput={len(ok) / wall_total:.2f} students/s"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sweep", default="1,2,4,8,12,16", help="comma-separated slot counts to try")
    parser.add_argument("--students", type=int, default=35, help="concurrent simulated students per slot count")
    args = parser.parse_args()

    slot_values = [int(s) for s in args.sweep.split(",")]
    for slots in slot_values:
        bench_one(slots, args.students)


if __name__ == "__main__":
    sys.exit(main())
