"""End-to-end tests against the real built-in git fixture and archive runner."""
from __future__ import annotations

import os
import time

import pytest

from app.engine import BisectSession, InlineExecutor
from app.fixture import build_builtin_repo
from app.history import load_graph, validate_pair
from app.runner import GitArchiveRunner


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    path = tmp_path_factory.mktemp("builtin") / "history-repo"
    build_builtin_repo(str(path), force=True)
    return str(path)


@pytest.fixture(scope="module")
def graph(repo):
    return load_graph(repo)


@pytest.fixture(scope="module")
def shas(repo):
    out = {}
    with open(os.path.join(repo, "BUILTIN_SHAS"), encoding="utf-8") as fh:
        for line in fh:
            k, v = line.split()
            out[k] = v
    return out


@pytest.fixture()
def runner(repo, tmp_path):
    return GitArchiveRunner(repo, str(tmp_path / "work"), delay_min=0, delay_max=0)


def test_history_has_merges(graph):
    merges = [c for c in graph.commits.values() if len(c.parents) >= 2]
    assert len(merges) >= 3
    assert graph.resolve("main")
    assert graph.resolve("baseline-good")


def test_lane_edges_all_connect(graph):
    layout = graph.lanes()
    for sha, c in graph.commits.items():
        for p in c.parents:
            if p in graph.commits:
                assert layout[sha]["parent_lanes"][p] == layout[p]["lane"]


def test_non_ancestor_pair_rejected(graph):
    bad, good, err = validate_pair(graph, "main", "conflict-bad")
    # conflict-bad is not an ancestor of main
    assert err


def test_main_preset_candidate_set(graph, runner, shas):
    bad, good, err = validate_pair(graph, "main", "baseline-good")
    assert err is None
    s = BisectSession(graph, bad, good, runner,
                      executor=InlineExecutor()).start()
    assert s.status == "complete", [e["text"] for e in s.events]
    assert s.conflicts == []
    got = set(s.snapshot()["candidates"])
    assert got == {shas["a2"], shas["b2"], shas["d2"], shas["d3"]}


def test_conflict_preset_ends_in_conflict(graph, runner, shas):
    bad, good, err = validate_pair(graph, "conflict-bad", "baseline-good")
    assert err is None
    s = BisectSession(graph, bad, good, runner,
                      executor=InlineExecutor()).start()
    assert s.status == "conflict"
    assert s.conflicts == [{"fail": shas["q1"], "pass": shas["q2"]}]
    # Both real measurements are present.
    assert s.facts[shas["q1"]].kind == "fail"
    assert s.facts[shas["q2"]].kind == "pass"
    kinds = {j.sha: j.kind for jobs in s.jobs.values() for j in jobs}
    assert kinds[shas["q1"]] == "fail"
    assert kinds[shas["q2"]] == "pass"


def test_runner_reads_distinct_commit_files(repo, graph):
    """Two shas must yield different extracted content/results."""
    r = GitArchiveRunner(repo, "/tmp/gsb-test-runner-isolation", delay_min=0)
    good = r.run(graph.resolve("650ce09"))   # A1 coupon good
    bad = r.run(graph.resolve("3acd2db"))    # A2 coupon bug
    assert good.kind == "pass"
    assert bad.kind == "fail"
    assert any(f["path"] == "shop/coupon.py" for f in good.read_files)
    assert any(f["path"] == "shop/coupon.py" for f in bad.read_files)
    skip = r.run(graph.resolve("0ddc443"))
    assert skip.kind == "skip"
