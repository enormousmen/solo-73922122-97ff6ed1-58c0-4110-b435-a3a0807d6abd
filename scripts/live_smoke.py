"""Against a running server: verify page, concurrency over real HTTP, outcomes.

Usage: python live_smoke.py [base_url]
"""
import sys
import time

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5055"


def wait_health(timeout=30):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            r = requests.get(BASE + "/api/health", timeout=2)
            if r.ok:
                return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.25)
    raise SystemExit(f"server not healthy: {last}")


def wait_terminal(sid, sample):
    end = time.time() + 60
    max_active = 0
    violations = []
    first_two = None
    while time.time() < end:
        d = requests.get(f"{BASE}/api/sessions/{sid}", timeout=5).json()
        sample.append(d)
        ac = d["active_count"]
        max_active = max(max_active, ac)
        if ac > 2:
            violations.append(ac)
        if ac == 2 and first_two is None:
            first_two = [j["sha"][:7] for j in d["running_jobs"]]
        if d["status"] in ("complete", "conflict") and not d["draining"]:
            return d, max_active, violations, first_two
        time.sleep(0.12)
    raise SystemExit("session did not finish in time")


def main():
    h = wait_health()
    print("health:", h)

    page = requests.get(BASE + "/", timeout=5)
    assert page.status_code == 200 and "Git 回归定位台" in page.text
    for asset in ("/static/style.css", "/static/app.js"):
        r = requests.get(BASE + asset, timeout=5)
        assert r.status_code == 200 and len(r.content) > 200, asset
    print("page + static assets OK")

    hist = requests.get(BASE + "/api/history", timeout=5).json()
    print("history commits:", len(hist["commits"]), "presets:",
          [p["id"] for p in hist["presets"]])
    assert len(hist["commits"]) >= 19

    # validation error path
    r = requests.post(BASE + "/api/sessions",
                      json={"bad": "main", "good": "conflict-bad"}, timeout=5)
    assert r.status_code == 400, r.status_code
    print("non-ancestor pair rejected:", r.json()["error"][:40], "...")

    # ---------------- conflict scenario (observe two live slots) ----------
    t0 = time.time()
    r = requests.post(BASE + "/api/sessions",
                      json={"bad": "conflict-bad", "good": "baseline-good"},
                      timeout=5)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    samples = []
    d, mx, viol, first_two = wait_terminal(sid, samples)
    print(f"\n[conflict] {d['status']} in {time.time()-t0:.1f}s; "
          f"max_active={mx}; first pair={first_two}; violations={viol}")
    assert viol == [] and mx == 2, (mx, viol)
    assert first_two and len(first_two) == 2
    assert d["status"] == "conflict"
    assert len(d["conflicts"]) == 1
    pair = d["conflicts"][0]
    print("pair:", pair["fail"][:7], "fail ancestor ->",
          pair["pass"][:7], "pass descendant")
    assert d["counts"]["tested"] == 2
    kinds = {j["sha"]: j["kind"] for j in d["jobs"]}
    assert kinds[pair["fail"]] == "fail" and kinds[pair["pass"]] == "pass"
    # both jobs expose real per-commit file reads
    assert all(j["read_files"] for j in d["jobs"]), "jobs should report read files"

    # ---------------- main scenario --------------------------------------
    t0 = time.time()
    r = requests.post(BASE + "/api/sessions",
                      json={"bad": "main", "good": "baseline-good"}, timeout=5)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    samples = []
    d, mx, viol, _ = wait_terminal(sid, samples)
    print(f"\n[main] {d['status']} in {time.time()-t0:.1f}s; max_active={mx}; "
          f"violations={viol}")
    assert viol == [] and mx == 2
    assert d["status"] == "complete"
    cands = [c[:7] for c in d["candidates"]]
    print("candidates:", cands)
    print("counts:", d["counts"])
    assert len(d["candidates"]) == 4
    print("\nLIVE_SMOKE_OK")


if __name__ == "__main__":
    main()
