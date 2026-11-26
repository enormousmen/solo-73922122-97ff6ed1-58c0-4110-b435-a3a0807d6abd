"""Regression tests for the site-wide two-slot scheduling fix.

These cover the bug report:

* a round waiting for a site slot must NOT show commits as "running";
* a queued commit is re-validated against the newest facts right before it
  starts and is skipped if its result is now inferable (so it cannot create a
  spurious conflict);
* rounds automatically continue when a slot frees;
* once a conflict exists, queued new tests are cancelled while tests that
  already hold a slot run to completion and are counted.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.engine import BisectSession, Fact
from app.graph import Commit, CommitGraph
from app.scheduler import SlotRequest, SlotScheduler
from app.server import create_app


def linear_graph(n=3):
    # bad -> k_n -> ... -> k_1 -> g
    spec = {"bad": [f"k{n}"]}
    order = ["bad"]
    for i in range(n, 0, -1):
        spec[f"k{i}"] = [f"k{i-1}" if i > 1 else "g"]
        order.append(f"k{i}")
    spec["g"] = []
    order.append("g")
    commits = [Commit(sha=s, short=s, subject=s, parents=p) for s, p in spec.items()]
    return CommitGraph(commits, order=order)


def result(kind):
    class R:
        pass
    r = R()
    r.kind = kind
    r.output = ""
    r.read_files = []
    r.duration = 0.0
    r.workdir = ""
    return r


class RecordingExecutor:
    def __init__(self):
        self.submitted = []

    def submit(self, fn):
        self.submitted.append(fn)


class BlockRunner:
    """Deterministic runner: blocks each real execution on its own event.

    Safe if two sessions share the runner and execute the same sha at
    different times / concurrently: every invocation gets its own run
    record and ``free_all`` releases all of them.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.runs = []
        # sha -> latest run events (convenient when shas are unique per round)
        self.entered = {}
        self.release = {}
        self.kinds = {}
        self.active = 0
        self.max_active = 0
        self.ran = []

    def __call__(self, sha):
        entered = threading.Event()
        release = threading.Event()
        with self.lock:
            run = {"sha": sha, "entered": entered, "release": release}
            self.runs.append(run)
            self.entered[sha] = entered
            self.release[sha] = release
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        entered.set()
        try:
            assert release.wait(10), "blocked runner timed out"
        finally:
            with self.lock:
                self.active -= 1
                self.ran.append(sha)
        return result(self.kinds.get(sha, "skip"))

    def free_all(self, kind="skip"):
        with self.lock:
            for sha in list(self.release):
                self.kinds.setdefault(sha, kind)
            runs = list(self.runs)
        for run in runs:
            run["release"].set()

    def start_auto_release(self, kind="skip", interval=0.01):
        """Release every blocked (and future) run until the returned Event is set."""
        stop = threading.Event()

        def loop():
            while not stop.wait(interval):
                self.free_all(kind)

        threading.Thread(target=loop, daemon=True).start()
        return stop


def wait_for(predicate, timeout=5.0, interval=0.01):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------- core fix
def test_waiting_round_shows_queued_not_running():
    g = linear_graph(3)
    runner1 = BlockRunner()
    runner2 = BlockRunner()
    pool = SlotScheduler(slots=2)

    s1 = BisectSession(g, "bad", "g", runner1, scheduler=pool).start()
    assert wait_for(lambda: len(runner1.entered) == 2)
    assert pool.active_count() == 2

    s2 = BisectSession(g, "bad", "g", runner2, scheduler=pool).start()
    time.sleep(0.15)  # give a buggy implementation time to mark "running"

    snap = s2.snapshot()
    assert snap["active_count"] == 0
    assert snap["running_jobs"] == []
    assert snap["queued_count"] == 2
    assert {j["sha"] for j in snap["queued_jobs"]} <= {f"k{i}" for i in range(1, 4)}
    for sha, node in snap["nodes"].items():
        if sha in snap["queued_shas"]:
            assert node["state"] == "queued"
            assert node["queued"] is True
        assert node["state"] != "running"
    # The site pool really is saturated by the first round.
    assert pool.active_count() == 2
    assert runner2.active == 0

    stop1 = runner1.start_auto_release("skip")
    stop2 = runner2.start_auto_release("skip")
    try:
        assert wait_for(lambda: s1.status == "complete", 10)
        assert wait_for(lambda: s2.status == "complete", 10)
        assert runner1.max_active <= 2 and runner2.max_active <= 2
        assert pool.active_count() == 0
    finally:
        stop1.set()
        stop2.set()
        runner1.free_all("skip")
        runner2.free_all("skip")


def test_queued_commit_rechecked_against_latest_facts_and_skipped():
    # bad -> y -> x -> g ; the two unknowns are launched in [y, x] order.
    spec = {"bad": ["y"], "y": ["x"], "x": ["g"], "g": []}
    order = ["bad", "y", "x", "g"]
    g = CommitGraph(
        [Commit(sha=s, short=s, subject=s, parents=p) for s, p in spec.items()],
        order=order,
    )
    r1 = BlockRunner()
    r2 = BlockRunner()
    pool = SlotScheduler(2)

    s1 = BisectSession(g, "bad", "g", r1, scheduler=pool).start()
    assert wait_for(lambda: len(r1.entered) == 2)
    s2 = BisectSession(g, "bad", "g", r2, scheduler=pool).start()
    time.sleep(0.1)
    assert [j["sha"] for j in s2.snapshot()["queued_jobs"]] == ["y", "x"]

    # Free exactly one site slot -> head waiter y is admitted for real.
    first_holder = sorted(r1.release)[0]
    r1.release[first_holder].set()
    assert wait_for(lambda: "y" in r2.entered)
    assert "x" not in r2.entered
    assert s2.snapshot()["nodes"]["x"]["state"] == "queued"

    # y passes -> ancestor x is deducible as pass.  Even though we configure
    # x to fail, the still-queued x must never run, so no conflict is born.
    r2.kinds["y"] = "pass"
    r2.kinds["x"] = "fail"
    r2.release["y"].set()

    assert wait_for(lambda: s2.status == "complete", 10)
    assert "x" not in r2.ran
    assert s2.conflicts == []
    node_x = s2.snapshot()["nodes"]["x"]
    assert node_x["state"] == "deduced-pass"
    stop1 = r1.start_auto_release("skip")
    try:
        assert wait_for(lambda: s1.status == "complete", 10)
    finally:
        stop1.set()
        r1.free_all("skip")


def test_round_auto_continues_after_slot_frees():
    g = linear_graph(4)
    runner = BlockRunner()
    pool = SlotScheduler(2)
    s = BisectSession(g, "bad", "g", runner, scheduler=pool).start()
    assert wait_for(lambda: len(runner.entered) == 2)
    # Every commit eventually runs when all results are skips and slots free.
    stop = runner.start_auto_release("skip")
    try:
        assert wait_for(lambda: s.status == "complete", 15)
        tested = {sha for sha, f in s.facts.items() if f.source == "test"}
        assert tested == {f"k{i}" for i in range(1, 5)}
        assert pool.active_count() == 0
    finally:
        stop.set()


def test_conflict_cancels_queued_but_keeps_inflight_job():
    g = linear_graph(3)  # k1,k2,k3 unknown
    ex = RecordingExecutor()
    pool = SlotScheduler(2)
    s = BisectSession(g, "bad", "g", lambda sha: None,
                      concurrency=3, executor=ex, scheduler=pool)
    s.start()

    # Pool has two slots: two requests admitted, the third stays queued both
    # in the session and in the site FIFO.
    assert len(s.inflight) == 2
    assert len(s.queued) == 1
    queued_sha = next(iter(s.queued))
    assert len(ex.submitted) == 2
    assert pool.active_count() == 2

    # Inject a failing ancestor fact, then let an inflight descendant pass:
    # that creates a genuine ancestor-fail / descendant-pass conflict.
    s.facts["k1"] = Fact(kind="fail", source="test", at=1.0)
    descendant = next(sha for sha in s.inflight
                      if sha != "k1" and s.graph.is_ancestor("k1", sha))
    other = next(sha for sha in s.inflight if sha != descendant)

    s.finish_job(descendant, result("pass"))

    # The queued third test was cancelled and never submitted...
    assert queued_sha not in s.queued
    assert len(ex.submitted) == 2
    assert pool.waiting_count() == 0
    # ...the round is draining because the other admitted job still runs.
    assert s.draining is True
    assert s.status == "running"
    assert other in s.inflight

    # The already-running job must finish and be counted.
    s.finish_job(other, result("skip"))
    assert s.status == "conflict"
    assert s.facts[other].kind == "skip"
    assert pool.active_count() == 0


def test_retry_queues_when_site_slots_busy():
    g = linear_graph(2)
    holder_runner = BlockRunner()
    retry_runner = BlockRunner()
    pool = SlotScheduler(2)

    # Hold both site slots with a first round.
    holder = BisectSession(g, "bad", "g", holder_runner, scheduler=pool).start()
    assert wait_for(lambda: len(holder_runner.entered) == 2)

    s = BisectSession(g, "bad", "g", retry_runner, scheduler=pool)
    s.facts["k1"] = Fact(kind="skip", source="test", at=1.0)
    ok, msg, _ = s.retry("k1")
    assert ok, msg
    snap = s.snapshot()
    assert snap["active_count"] == 0
    assert snap["queued_count"] == 1

    stop_h = holder_runner.start_auto_release("skip")
    try:
        assert wait_for(lambda: "k1" in retry_runner.entered, 10)
        # The manual retry really holds a slot and is not auto-freed yet.
        assert wait_for(lambda: s.snapshot()["active_count"] == 1)
        assert retry_runner.active == 1
        stop_r = retry_runner.start_auto_release("skip")
        try:
            assert wait_for(lambda: s.status == "complete", 10)
            assert "k1" in retry_runner.ran
            assert wait_for(lambda: holder.status == "complete", 10)
        finally:
            stop_r.set()
    finally:
        stop_h.set()
        holder_runner.free_all("skip")
        retry_runner.free_all("skip")


# --------------------------------------------------------------- HTTP layer
class _HttpBlockRunner(BlockRunner):
    pass


def test_http_two_overlapping_rounds_share_two_slots():
    br = _HttpBlockRunner()
    app = create_app({
        "TESTING": True,
        "GRAPH": linear_graph(4),
        "RUNNER": br,
        "AUTO_BUILD_FIXTURE": False,
    })
    client = app.test_client()

    r = client.post("/api/sessions", json={"bad": "bad", "good": "g"})
    s1 = r.get_json()["id"]
    assert wait_for(lambda: len(br.entered) >= 2)

    r = client.post("/api/sessions", json={"bad": "bad", "good": "g"})
    assert r.status_code == 201
    s2_created = r.get_json()
    s2 = s2_created["id"]
    assert s2_created["active_count"] == 0
    assert s2_created["queued_count"] >= 1

    d2 = client.get(f"/api/sessions/{s2}").get_json()
    assert d2["active_count"] == 0
    assert d2["running_jobs"] == []
    assert d2["slot_pool"]["slots"] == 2
    assert d2["slot_pool"]["active"] <= 2

    stop = br.start_auto_release("skip")
    statuses = {}
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            for sid in (s1, s2):
                d = client.get(f"/api/sessions/{sid}").get_json()
                assert d["active_count"] <= 2
                statuses[sid] = d["status"]
            if all(v in ("complete", "conflict") for v in statuses.values()):
                break
            time.sleep(0.03)
        assert all(v in ("complete", "conflict") for v in statuses.values()), statuses
        assert br.max_active <= 2
        assert br.max_active >= 1
    finally:
        stop.set()
