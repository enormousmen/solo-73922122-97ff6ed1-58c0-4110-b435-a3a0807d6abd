"""Real-thread tests for the two-slot limit, overlap and slot top-up."""
from __future__ import annotations

import threading
import time

from app.engine import BisectSession
from app.graph import Commit, CommitGraph


def long_linear(n=8):
    # bad -> k_n -> ... -> k_1 -> g
    spec = {"bad": [f"k{n}"]}
    order = ["bad"]
    for i in range(n, 0, -1):
        parent = f"k{i-1}" if i > 1 else "g"
        spec[f"k{i}"] = [parent]
        order.append(f"k{i}")
    spec["g"] = []
    order.append("g")
    commits = [Commit(sha=s, short=s, subject=s, parents=p) for s, p in spec.items()]
    return CommitGraph(commits, order=order)


class CountingRunner:
    def __init__(self, delay=0.12, kind="skip"):
        self.delay = delay
        self.kind = kind
        self.active = 0
        self.max_active = 0
        self.ran = []
        self.lock = threading.Lock()

    def __call__(self, sha):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
            self.ran.append(sha)

        class R:
            pass
        r = R()
        r.kind = self.kind
        r.output = "ok"
        r.read_files = [{"path": "x"}]
        r.duration = self.delay
        r.workdir = "/tmp/x"
        return r


def test_never_more_than_two_and_slots_are_refilled():
    g = long_linear(8)
    runner = CountingRunner(delay=0.1)
    s = BisectSession(g, "bad", "g", runner, concurrency=2).start()

    max_seen = 0
    deadline = time.time() + 15
    while time.time() < deadline:
        snap = s.snapshot()
        max_seen = max(max_seen, snap["active_count"])
        assert snap["active_count"] <= 2, "more than two concurrent tests!"
        if snap["status"] == "complete":
            break
        time.sleep(0.01)

    assert s.status == "complete"
    assert max_seen == 2
    assert runner.max_active == 2
    # Every one of the 8 unknown commits was actually tested (all skipped).
    assert len(runner.ran) == 8
    assert set(runner.ran) == {f"k{i}" for i in range(1, 9)}


def test_two_jobs_genuinely_overlap():
    g = long_linear(4)
    runner = CountingRunner(delay=0.2)
    s = BisectSession(g, "bad", "g", runner, concurrency=2).start()
    time.sleep(0.05)
    snap = s.snapshot()
    assert snap["active_count"] == 2
    assert len(snap["running_jobs"]) == 2
    shas = {j["sha"] for j in snap["running_jobs"]}
    assert len(shas) == 2
    deadline = time.time() + 10
    while s.status == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert s.status == "complete"
    assert runner.max_active == 2


def test_global_gate_caps_parallel_execution_across_callers():
    from app.runner import GlobalSlotGate

    inner = CountingRunner(delay=0.08)
    gate = GlobalSlotGate(inner, slots=2)
    threads = [threading.Thread(target=gate, args=(f"sha{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert inner.max_active == 2
    assert len(inner.ran) == 6
