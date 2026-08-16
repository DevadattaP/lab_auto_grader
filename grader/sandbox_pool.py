"""A small, bounded pool of Sandbox instances for on-demand single-question
runs (server_student's "Run" button) -- distinct from grade.py's batch
`multiprocessing.Pool`, which statically assigns one Sandbox per worker for
a whole offline run.

IsolateSandbox is not safe for concurrent use on one box_id: both
`compile_c` and `run` each do their own `isolate --init`/`--cleanup` under
that id (sandbox.py), so two concurrent calls on the same box_id would race
on the same on-disk box directory. Concurrency here therefore comes from
checking a Sandbox out of a bounded queue of *distinct* box_ids, one per
in-flight Run, never from sharing a single Sandbox across threads. See
LIVE_LAB_DESIGN.md §8.
"""

from __future__ import annotations

import queue
from contextlib import contextmanager
from pathlib import Path

from grader.sandbox import Sandbox, make_sandbox


class PoolExhausted(Exception):
    """Every slot was busy and none freed up within the checkout timeout --
    callers should surface this as "server busy, try again shortly" (e.g.
    HTTP 503), not a 500/stack trace."""


class SandboxPool:
    def __init__(self, kind: str, size: int, scratch_root: Path, box_id_start: int = 0):
        if size < 1:
            raise ValueError("size must be >= 1")
        self.kind = kind
        self.size = size
        self._queue: "queue.Queue[Sandbox]" = queue.Queue()
        for offset in range(size):
            self._queue.put(make_sandbox(kind, box_id_start + offset, scratch_root))

    @contextmanager
    def checkout(self, timeout: float = 15.0):
        """Blocks (up to `timeout` seconds) for a free Sandbox, then returns
        it to the pool when the caller's `with` block exits -- including on
        an exception, so a crashed Run never permanently strands a slot."""
        try:
            sandbox = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise PoolExhausted(f"All {self.size} sandbox slot(s) are busy")
        try:
            yield sandbox
        finally:
            self._queue.put(sandbox)

    def close(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait().close()
