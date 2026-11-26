"""HTTP API tests via Flask's in-process test client."""
from __future__ import annotations

import os
import time

import pytest

from app.fixture import build_builtin_repo
from app.graph import Commit, CommitGraph
from app.history import load_graph
from app.runner import GitArchiveRunner
from app.server import create_app


def linear_graph():
    spec = {
        "bad": ["c"], "c": ["b"], "b": ["a"], "a": ["g"], "g": [],
    }
    order = ["bad", "c", "b", "a", "g"]
    commits = [Commit(sha=s, short=s, subject=s, parents=p) for s, p in spec.items()]
    return CommitGraph(commits, order=order)


class FakeRunner:
    def __init__(self, kinds=None, delay=0.01):
        self.kinds = kinds or {}
        self.delay = delay

    def __call__(self, sha):
        time.sleep(self.delay)

        class R:
            pass
        r = R()
        r.kind = self.kinds.get(sha, "skip")
        r.output = "fake"
        r.read_files = []
        r.duration = self.delay
        r.workdir = "/tmp/x"
        return r


@pytest.fixture()
def client():
    app = create_app({
        "TESTING": True,
        "GRAPH": linear_graph(),
        "RUNNER": FakeRunner(),
        "AUTO_BUILD_FIXTURE": False,
    })
    return app.test_client()


def test_page_and_health(client):
    assert client.get("/api/health").get_json()["ok"] is True
    html = client.get("/").get_data(as_text=True)
    assert "Git 回归定位台" in html
    hist = client.get("/api/history").get_json()
    assert len(hist["commits"]) == 5


def test_create_session_validation(client):
    r = client.post("/api/sessions", json={"bad": "bad", "good": "bad"})
    assert r.status_code == 400
    # c is a descendant of g, so g is ancestor of c but pair bad=c? use
    # bad=g & good=bad -> good not ancestor of bad.
    r = client.post("/api/sessions", json={"bad": "g", "good": "bad"})
    assert r.status_code == 400
    r = client.post("/api/sessions", json={"bad": "nope", "good": "g"})
    assert r.status_code == 400


def test_full_skip_round_over_api(client):
    r = client.post("/api/sessions", json={"bad": "bad", "good": "g"})
    assert r.status_code == 201
    sid = r.get_json()["id"]

    snap = None
    for _ in range(300):
        snap = client.get(f"/api/sessions/{sid}").get_json()
        assert snap["active_count"] <= 2
        if snap["status"] in ("complete", "conflict"):
            break
        time.sleep(0.02)
    assert snap["status"] == "complete"
    assert client.get("/api/sessions/does-not-exist").status_code == 404
    # Round finished -> retry is rejected with 409.
    sha = next(iter(snap["nodes"]))
    rr = client.post(f"/api/sessions/{sid}/retry", json={"sha": "b"})
    assert rr.status_code == 409


# ------------------------------------------------------- real fixture API
@pytest.fixture(scope="module")
def real_repo(tmp_path_factory):
    path = tmp_path_factory.mktemp("api-builtin") / "repo"
    build_builtin_repo(str(path), force=True)
    return str(path)


@pytest.fixture()
def real_client(real_repo, tmp_path):
    graph = load_graph(real_repo)
    runner = GitArchiveRunner(real_repo, str(tmp_path / "w"),
                              delay_min=0, delay_max=0)
    app = create_app({"TESTING": True, "GRAPH": graph, "RUNNER": runner})
    return app.test_client()


def _wait(client, sid, timeout=30):
    deadline = time.time() + timeout
    snap = None
    max_active = 0
    while time.time() < deadline:
        snap = client.get(f"/api/sessions/{sid}").get_json()
        max_active = max(max_active, snap["active_count"])
        assert snap["active_count"] <= 2
        if snap["status"] in ("complete", "conflict") and not snap["draining"]:
            return snap, max_active
        time.sleep(0.03)
    raise AssertionError(f"round did not finish: {snap and snap['status']}")


def test_real_main_round_over_api(real_client, real_repo):
    r = real_client.post("/api/sessions",
                         json={"bad": "main", "good": "baseline-good"})
    assert r.status_code == 201, r.get_data(as_text=True)
    sid = r.get_json()["id"]
    snap, max_active = _wait(real_client, sid)
    assert snap["status"] == "complete"
    assert snap["counts"]["candidates"] == 4
    assert max_active >= 1


def test_real_conflict_round_over_api(real_client):
    r = real_client.post("/api/sessions",
                         json={"bad": "conflict-bad", "good": "baseline-good"})
    assert r.status_code == 201
    created = r.get_json()
    # Two eligible commits => both get job records/slots up front.
    assert len(created["jobs"]) == 2
    assert created["active_count"] == 2
    sid = created["id"]
    snap, max_active = _wait(real_client, sid)
    assert snap["status"] == "conflict"
    assert len(snap["conflicts"]) >= 1
    assert max_active <= 2
    # both real jobs measured and counted
    assert snap["counts"]["tested"] == 2
    kinds = {j["sha"]: j["kind"] for j in snap["jobs"]}
    pair = snap["conflicts"][0]
    assert kinds[pair["fail"]] == "fail"
    assert kinds[pair["pass"]] == "pass"


class TrackingRunner:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.lock = __import__("threading").Lock()

    def __call__(self, sha):
        import time as _t
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        _t.sleep(0.05)
        with self.lock:
            self.active -= 1

        class R:
            kind = "skip"
            output = "x"
            read_files = []
            duration = 0.05
            workdir = "/tmp/x"
        return R()


def test_two_overlapping_rounds_never_exceed_two_real_tests():
    tracker = TrackingRunner()
    app = create_app({
        "TESTING": True,
        "GRAPH": linear_graph(),
        "RUNNER": tracker,
        "AUTO_BUILD_FIXTURE": False,
    })
    c = app.test_client()
    sids = []
    for _ in range(2):
        r = c.post("/api/sessions", json={"bad": "bad", "good": "g"})
        assert r.status_code == 201
        sids.append(r.get_json()["id"])

    deadline = time.time() + 20
    done = set()
    while time.time() < deadline and len(done) < 2:
        for sid in sids:
            d = c.get(f"/api/sessions/{sid}").get_json()
            assert d["active_count"] <= 2
            if d["status"] == "complete":
                done.add(sid)
        time.sleep(0.02)
    assert len(done) == 2
    # The process-wide gate wrapped around the injected runner.
    assert tracker.max_active <= 2
    assert tracker.max_active >= 1
