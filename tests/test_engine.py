"""Deterministic engine tests: deduction, candidates, conflicts, scheduling.

These use synthetic DAGs and a recording executor so no threads race.
"""
from __future__ import annotations

import threading

import pytest

from app.engine import (
    BisectSession, Fact, InlineExecutor, choose_next,
    earliest_failure_candidates, find_conflicts, implied_sets,
)
from app.graph import Commit, CommitGraph


def make_graph(spec, order=None):
    """spec: dict sha -> list of parent shas."""
    commits = [
        Commit(sha=s, short=s[:7], subject="subj-" + s, parents=list(ps))
        for s, ps in spec.items()
    ]
    if order is None:
        order = list(spec.keys())
    return CommitGraph(commits, order=order)


# Linear old -> new: g, a, b, c, bad  (order is newest first)
LINEAR = {
    "bad": ["c"], "c": ["b"], "b": ["a"], "a": ["g"], "g": [],
}
LINEAR_ORDER = ["bad", "c", "b", "a", "g"]


class RecordingExecutor:
    """Remembers submitted workers without running them."""

    def __init__(self):
        self.submitted = []
        self.lock = threading.Lock()

    def submit(self, fn):
        with self.lock:
            self.submitted.append(fn)


def new_session(spec=LINEAR, order=LINEAR_ORDER, bad="bad", good="g",
                runner=None, executor=None):
    g = make_graph(spec, order)
    runner = runner or (lambda sha: pytest.fail("runner should not run in recording tests"))
    ex = executor if executor is not None else RecordingExecutor()
    s = BisectSession(g, bad, good, runner, executor=ex)
    return g, s, ex


def result(kind, output=""):
    class R:
        pass
    r = R()
    r.kind = kind
    r.output = output
    r.read_files = []
    r.duration = 0.01
    r.workdir = "/tmp/x"
    return r


# ------------------------------------------------------------- graph facts
def test_ancestry_and_descendants_linear():
    g = make_graph(LINEAR, LINEAR_ORDER)
    assert g.is_ancestor("g", "bad")
    assert g.is_ancestor("a", "c")
    assert not g.is_ancestor("c", "a")
    assert g.ancestors_incl("b") == {"b", "a", "g"}
    assert g.descendants_incl("a") == {"a", "b", "c", "bad"}


def test_diamond_merge_ancestry():
    # g; x,y branch from g; m merge x+y; bad -> m
    spec = {
        "bad": ["m"], "m": ["x", "y"], "x": ["g"], "y": ["g"], "g": [],
    }
    g = make_graph(spec, ["bad", "m", "x", "y", "g"])
    assert g.ancestors_incl("m") == {"m", "x", "y", "g"}
    assert g.is_ancestor("g", "bad")
    assert not g.is_ancestor("x", "y")
    assert "y" in g.descendants_incl("g")


# ------------------------------------------------------------ implied sets
def test_pass_implies_ancestors_fail_implies_descendants():
    g = make_graph(LINEAR, LINEAR_ORDER)
    facts = {
        "b": Fact(kind="pass", source="test", at=1),
        "c": Fact(kind="fail", source="test", at=2),
    }
    p, f = implied_sets(g, facts)
    assert p == {"b", "a", "g"}
    assert f == {"c", "bad"}


def test_skip_gives_no_implication():
    g = make_graph(LINEAR, LINEAR_ORDER)
    facts = {"b": Fact(kind="skip", source="test", at=1)}
    p, f = implied_sets(g, facts)
    assert p == set() and f == set()


# -------------------------------------------------------------- conflicts
def test_find_conflicts_detects_ancestor_fail_descendant_pass():
    g = make_graph(LINEAR, LINEAR_ORDER)
    facts = {
        "a": Fact(kind="fail", source="test", at=1),
        "c": Fact(kind="pass", source="test", at=2),
    }
    pairs = find_conflicts(g, facts)
    assert pairs == [{"fail": "a", "pass": "c"}]


def test_no_conflict_for_descendant_fail_ancestor_pass():
    g = make_graph(LINEAR, LINEAR_ORDER)
    facts = {
        "g": Fact(kind="pass", source="known", at=1),
        "c": Fact(kind="fail", source="test", at=2),
    }
    assert find_conflicts(g, facts) == []


# ------------------------------------------------------------- candidates
def test_candidates_single_linear_culprit():
    g = make_graph(LINEAR, LINEAR_ORDER)
    facts = {
        "bad": Fact("fail", "known", 0),
        "g": Fact("pass", "known", 0),
        "a": Fact("pass", "test", 1),
        "b": Fact("fail", "test", 2),
    }
    assert earliest_failure_candidates(g, "bad", facts) == ["b"]


def test_candidates_skip_leaves_adjacent_pair():
    g = make_graph(LINEAR, LINEAR_ORDER)
    facts = {
        "bad": Fact("fail", "known", 0),
        "g": Fact("pass", "known", 0),
        "a": Fact("pass", "test", 1),
        # b skipped -> unknown
        "c": Fact("fail", "test", 2),
    }
    cands = earliest_failure_candidates(g, "bad", facts)
    assert set(cands) == {"b", "c"}


def test_candidates_parallel_branches():
    spec = {
        "h": ["m"], "m": ["a3", "b3"],
        "a3": ["a2"], "a2": ["a1"], "a1": ["g"],
        "b3": ["b2"], "b2": ["b1"], "b1": ["g"], "g": [],
    }
    order = ["h", "m", "a3", "a2", "a1", "b3", "b2", "b1", "g"]
    g = make_graph(spec, order)
    facts = {
        "h": Fact("fail", "known", 0),
        "g": Fact("pass", "known", 0),
        "a1": Fact("pass", "test", 1),
        "b1": Fact("pass", "test", 2),
        "a2": Fact("fail", "test", 3),
        "b2": Fact("fail", "test", 4),
    }
    cands = set(earliest_failure_candidates(g, "h", facts))
    assert cands == {"a2", "b2"}


def test_unknown_branch_roots_are_also_candidates():
    """Without pass facts on a1/b1 they remain possible earliest failures."""
    spec = {
        "h": ["m"], "m": ["a2", "b2"],
        "a2": ["a1"], "a1": ["g"],
        "b2": ["b1"], "b1": ["g"], "g": [],
    }
    order = ["h", "m", "a2", "a1", "b2", "b1", "g"]
    g = make_graph(spec, order)
    facts = {
        "h": Fact("fail", "known", 0),
        "g": Fact("pass", "known", 0),
        "a2": Fact("fail", "test", 1),
    }
    cands = set(earliest_failure_candidates(g, "h", facts))
    # a1/a2 are an unknown+confirmed pair; b1/b2 both open on branch B.
    assert cands == {"a1", "a2", "b1", "b2"}


# ------------------------------------------------------------ choose next
def test_choose_next_excludes_known_and_deducible():
    g = make_graph(LINEAR, LINEAR_ORDER)
    facts = {
        "bad": Fact("fail", "known", 0),
        "g": Fact("pass", "known", 0),
    }
    # a is deduced pass via g? No: pass at g implies ancestors of g (none),
    # NOT descendants. g passing does not imply a. So a,b,c all eligible.
    nxt = choose_next(g, "bad", facts, set())
    assert nxt in {"a", "b", "c"}
    # Once b passes, a/g are implied pass and c remains the only eligible.
    facts["b"] = Fact("pass", "test", 1)
    assert choose_next(g, "bad", facts, set()) == "c"
    # Once b fails, c/bad are implied fail and a remains eligible.
    del facts["b"]
    facts["b"] = Fact("fail", "test", 1)
    assert choose_next(g, "bad", facts, set()) == "a"


# ------------------------------------------------- session / scheduling
def test_initial_launch_fills_at_most_two_slots():
    g, s, ex = new_session()
    s.start()
    assert len(s.inflight) == 2
    assert len(ex.submitted) == 2
    assert s.status == "running"
    # both chosen commits are eligible unknowns a/b/c
    assert s.inflight <= {"a", "b", "c"}


def test_top_up_only_untried_nondeducible():
    g, s, ex = new_session()
    s.start()
    first = set(s.inflight)
    assert len(first) == 2
    # Complete one with a skip -> no deduction -> exactly one replacement.
    done = sorted(first)[0]
    s.finish_job(done, result("skip"))
    assert len(s.inflight) == 2
    # The replacement must be a fresh commit, never attempted.
    new = s.inflight - (first - {done})
    assert len(new) == 1
    sha = next(iter(new))
    assert sha not in s.facts or s.facts[sha] is not None and sha in s.inflight
    assert sha not in first
    # It must not be deducible.
    p, f = implied_sets(g, s.facts)
    assert sha not in p and sha not in f


def test_skips_exhaust_and_complete():
    g, s, ex = new_session()
    s.start()
    # Drive whatever is inflight with skips until terminal.
    guard = 0
    while s.status == "running" and guard < 50:
        guard += 1
        current = list(s.inflight)
        if not current:
            break
        s.finish_job(current[0], result("skip"))
    assert s.status == "complete"
    # a,b,c all attempted (skip facts), seeds untouched.
    assert {k for k, f in s.facts.items() if f.source == "test"} == {"a", "b", "c"}
    assert all(s.facts[k].kind == "skip" for k in ("a", "b", "c"))
    assert s.inflight == set()


def test_inflight_result_awaited_even_when_deducible_consistent():
    """If another result makes a running job deducible, keep it and count it."""
    g = make_graph(LINEAR, LINEAR_ORDER)
    s = BisectSession(g, "bad", "g", lambda sha: None,
                      executor=RecordingExecutor())
    # Model two jobs that were launched together.
    s._mark_running("a")
    s._mark_running("c")
    # Ancestor a fails -> b,c,bad are now deducible fail while c is running.
    s.finish_job("a", result("fail"))
    assert "c" in s.inflight, "in-flight c must not be cancelled"
    # The real measurement still arrives and is recorded.
    s.finish_job("c", result("fail"))
    assert s.facts["c"].kind == "fail"
    # b was deducible and never attempted; nothing eligible remains.
    assert s.status == "complete"
    assert s.conflicts == []
    assert "b" not in s.facts or s.facts["b"].source != "test"


def test_inflight_actual_pass_creates_conflict():
    g = make_graph(LINEAR, LINEAR_ORDER)
    s = BisectSession(g, "bad", "g", lambda sha: None,
                      executor=RecordingExecutor())
    s._mark_running("a")
    s._mark_running("c")
    s.finish_job("a", result("fail"))
    assert "c" in s.inflight
    # c's actual measurement is a pass -> ancestor fail / descendant pass.
    s.finish_job("c", result("pass"))
    assert s.status == "conflict"
    assert any(p["fail"] == "a" and p["pass"] == "c" for p in s.conflicts)
    snap = s.snapshot()
    assert "a" in snap["conflict_shas"] and "c" in snap["conflict_shas"]
    assert s.facts["c"].kind == "pass"


def test_exactly_two_eligible_both_start_and_conflict():
    """Mirrors the built-in conflict preset: two eligible commits => both run."""
    spec = {"bad": ["q2"], "q2": ["q1"], "q1": ["g"], "g": []}
    order = ["bad", "q2", "q1", "g"]
    g = make_graph(spec, order)
    ex = RecordingExecutor()
    s = BisectSession(g, "bad", "g", lambda sha: None, executor=ex)
    s.start()
    assert s.inflight == {"q1", "q2"}
    # q1 fails first -> q2 is now deducible fail but is already running, so it
    # is kept (no conflict exists yet because q2 has no measured pass).
    s.finish_job("q1", result("fail"))
    assert "q2" in s.inflight
    assert s.status == "running" and s.draining is False
    # q2 actually passes -> measured and counted, terminal conflict.
    s.finish_job("q2", result("pass"))
    assert s.status == "conflict"
    assert s.conflicts == [{"fail": "q1", "pass": "q2"}]
    assert s.snapshot()["counts"]["tested"] == 2


def test_conflict_detection_waits_for_remaining_inflight_job():
    """A conflict found while another job runs drains it before terminal state."""
    spec = {
        "bad": ["z", "p"], "p": ["a"], "z": ["g2"],
        "a": ["g"], "g2": ["g"], "g": [],
    }
    order = ["bad", "z", "p", "a", "g2", "g"]
    g = make_graph(spec, order)
    s = BisectSession(g, "bad", "g", lambda sha: None,
                      executor=RecordingExecutor())
    # Pre-existing measured fail at ancestor a; two new jobs p (descendant)
    # and z are both running.
    s.facts["a"] = Fact("fail", "test", 1)
    s._mark_running("p")
    s._mark_running("z")
    # p really passes -> conflict pair, but z is still running.
    s.finish_job("p", result("pass"))
    assert s.conflicts == [{"fail": "a", "pass": "p"}]
    assert s.draining is True
    assert s.status == "running", "round must not terminate before z is measured"
    assert "z" in s.inflight
    # z finishes (skipped) and is counted, only then the round ends.
    s.finish_job("z", result("skip"))
    assert s.status == "conflict"
    assert s.facts["z"].kind == "skip"


def test_snapshot_is_json_friendly():
    g, s, _ = new_session()
    s.start()
    snap = s.snapshot()
    import json
    json.dumps(snap)  # must not raise
    assert set(snap.keys()) >= {"nodes", "candidates", "conflicts", "events"}


def test_adjacent_good_parent_completes_immediately_with_bad_candidate():
    spec = {"bad": ["good"], "good": ["root"], "root": []}
    order = ["bad", "good", "root"]
    g = make_graph(spec, order)
    s = BisectSession(g, "bad", "good", lambda sha: None,
                      executor=RecordingExecutor()).start()
    assert s.status == "complete"
    assert s.inflight == set()
    assert s.snapshot()["candidates"] == ["bad"]


def test_lane_layout_is_consistent():
    spec = {
        "h": ["m"], "m": ["a2", "b2"],
        "a2": ["a1"], "a1": ["g"],
        "b2": ["b1"], "b1": ["g"], "g": [],
    }
    order = ["h", "m", "a2", "a1", "b2", "b1", "g"]
    g = make_graph(spec, order)
    layout = g.lanes()
    for sha in order:
        d = layout[sha]
        assert d["lane"] >= 0 and d["lanes"] >= d["lane"] + 1
        for p, pl in d["parent_lanes"].items():
            assert pl >= 0 and pl < d["lanes"]
            # edge continuity invariant
            assert pl == layout[p]["lane"], (sha, p, pl, layout[p]["lane"])
    # merge commit m references two distinct parent lanes
    assert layout["m"]["parent_lanes"]["a2"] != layout["m"]["parent_lanes"]["b2"]


def test_resolve_accepts_tag_prefixes_and_short_sha():
    g = make_graph(LINEAR, LINEAR_ORDER)
    # synthetic refs
    g.commits["g"].refs.append("tag:baseline-good")
    assert g.resolve("baseline-good") == "g"
    assert g.resolve("tag:baseline-good") == "g"
    assert g.resolve("bad") == "bad"
