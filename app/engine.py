"""Parallel git-bisect style regression localization engine.

The rules implemented here (all explicitly requested):

* at most ``concurrency`` (default 2) test jobs run at once;
* a free slot is filled with a commit that has never been attempted and
  whose result cannot be deduced from known facts;
* a passing commit implies all its ancestors pass;
* a failing commit implies all its descendants fail;
* a skipped / errored test gives no information (unknown);
* already started jobs are always awaited and their real result is recorded,
  even if deduction would now cover them;
* an ancestor failure together with a descendant pass is a contradiction:
  conflicting commits are marked and the round ends in ``conflict`` once
  in-flight jobs have been drained;
* the possible earliest-failing set is computed over the bad endpoint and
  all of its ancestors.
"""
from __future__ import annotations

import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Set

from .graph import CommitGraph

PASS = "pass"
FAIL = "fail"
SKIP = "skip"
ERROR = "error"
RESULT_KINDS = {PASS, FAIL, SKIP, ERROR}

CONCURRENCY = 2


class RunnerResultLike(Protocol):
    kind: str
    output: str
    read_files: List[dict]
    duration: float


@dataclass
class Fact:
    kind: str  # pass | fail | skip | error
    source: str  # known | test | manual
    at: float
    output: str = ""
    read_files: List[dict] = field(default_factory=list)
    duration: float = 0.0
    attempts: int = 1


@dataclass
class Job:
    sha: str
    number: int
    started_at: float
    finished_at: Optional[float] = None
    kind: Optional[str] = None
    output: str = ""
    read_files: List[dict] = field(default_factory=list)
    duration: float = 0.0
    workdir: str = ""

    def to_json(self, now: float) -> dict:
        return {
            "sha": self.sha,
            "number": self.number,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "running": self.finished_at is None,
            "elapsed": round((self.finished_at or now) - self.started_at, 2),
            "kind": self.kind,
            "output": self.output,
            "read_files": self.read_files,
            "duration": round(self.duration, 3),
            "workdir": self.workdir,
        }


class Executor:
    """Where test work runs. Production uses daemon threads."""

    def submit(self, fn: Callable[[], None]) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ThreadExecutor(Executor):
    def submit(self, fn: Callable[[], None]) -> None:
        t = threading.Thread(target=fn, daemon=True)
        t.start()


class InlineExecutor(Executor):
    """Deterministic test executor: run work immediately on the caller thread."""

    def submit(self, fn: Callable[[], None]) -> None:
        fn()


# --------------------------------------------------------------------------- #
# Pure rule helpers
# --------------------------------------------------------------------------- #
def implied_sets(
    graph: CommitGraph, facts: Dict[str, Fact]
) -> tuple[Set[str], Set[str]]:
    """Return (commits implied to pass, commits implied to fail)."""
    pass_implies: Set[str] = set()
    fail_implies: Set[str] = set()
    for sha, fact in facts.items():
        if sha not in graph.commits:
            continue
        if fact.kind == PASS:
            pass_implies |= graph.ancestors_incl(sha)
        elif fact.kind == FAIL:
            fail_implies |= graph.descendants_incl(sha)
    return pass_implies, fail_implies


def find_conflicts(
    graph: CommitGraph, facts: Dict[str, Fact]
) -> List[dict]:
    """All (failing ancestor, passing descendant) fact pairs."""
    fails = [s for s, f in facts.items() if f.kind == FAIL]
    passes = [s for s, f in facts.items() if f.kind == PASS]
    pairs: List[dict] = []
    for fsha in fails:
        anc = graph.ancestors_incl(fsha)
        for psha in passes:
            if fsha == psha:
                continue
            # fsha must be an ancestor of psha  <=>  psha in descendants(fsha)
            if psha in graph.descendants_incl(fsha):
                pairs.append({"fail": fsha, "pass": psha})
    pairs.sort(key=lambda p: (p["fail"], p["pass"]))
    return pairs


def earliest_failure_candidates(
    graph: CommitGraph, bad: str, facts: Dict[str, Fact]
) -> List[str]:
    """All commits that could be the earliest failing revision.

    A commit C is a candidate when:
      * C is the selected bad endpoint or one of its ancestors,
      * C is not known/deduced to pass,
      * no *factually* failing commit is a strict ancestor of C.
    Skipped / errored commits stay unknown and therefore remain possible.
    """
    domain = graph.ancestors_incl(bad)
    pass_implies, _ = implied_sets(graph, facts)
    fail_facts = {s for s, f in facts.items() if f.kind == FAIL}

    candidates: List[str] = []
    for sha in domain:
        if sha in pass_implies:
            continue
        if sha in facts and facts[sha].kind == PASS:
            continue
        strict_ancestors = graph.ancestors_incl(sha) - {sha}
        if strict_ancestors & fail_facts:
            continue
        candidates.append(sha)

    # Newest-first graph order for stable display.
    order_index = {s: i for i, s in enumerate(graph.order)}
    candidates.sort(key=lambda s: order_index.get(s, 1 << 30))
    return candidates


def choose_next(
    graph: CommitGraph,
    bad: str,
    facts: Dict[str, Fact],
    inflight: Set[str],
) -> Optional[str]:
    """Pick the most bisect-informative unattempted, non-deducible commit."""
    domain = graph.ancestors_incl(bad)
    pass_implies, fail_implies = implied_sets(graph, facts)

    eligible: List[str] = []
    for sha in domain:
        if sha in facts or sha in inflight:
            continue
        if sha in pass_implies or sha in fail_implies:
            continue
        eligible.append(sha)

    if not eligible:
        return None

    order_index = {s: i for i, s in enumerate(graph.order)}

    def score(sha: str):
        anc = graph.ancestors_incl(sha) & domain
        desc = graph.descendants_incl(sha) & domain
        gain_pass = len(anc - pass_implies)
        gain_fail = len(desc - fail_implies)
        # Maximise the smaller side (halving power), then balance, then
        # prefer newer / topologically earlier commits and sha as tie break.
        return (
            min(gain_pass, gain_fail),
            -abs(gain_pass - gain_fail),
            -len(anc | desc),
            -order_index.get(sha, 1 << 29),
            sha,
        )

    eligible.sort(key=score, reverse=True)
    # reverse=True also reverses the sha tie-break; re-sort tail deterministically
    best_score = score(eligible[0])
    top = [s for s in eligible if score(s)[:-1] == best_score[:-1]]
    top.sort()
    return top[0]


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
class BisectSession:
    _ids = itertools.count(1)

    def __init__(
        self,
        graph: CommitGraph,
        bad: str,
        good: str,
        runner: Callable[[str], RunnerResultLike],
        *,
        concurrency: int = CONCURRENCY,
        executor: Optional[Executor] = None,
        clock: Callable[[], float] = time.time,
        sid: Optional[str] = None,
    ):
        self.id = sid or f"s{next(self._ids)}-{int(clock())}"
        self.graph = graph
        self.bad = bad
        self.good = good
        self.runner = runner
        self.concurrency = concurrency
        self.executor = executor or ThreadExecutor()
        self.clock = clock

        self.lock = threading.RLock()
        self.facts: Dict[str, Fact] = {}
        self.jobs: Dict[str, List[Job]] = {}
        self.inflight: Set[str] = set()
        self.status = "running"  # running | complete | conflict
        self.draining = False
        self.conflicts: List[dict] = []
        self.events: List[dict] = []
        self.created_at = clock()
        self.finished_at: Optional[float] = None
        self._job_seq = itertools.count(1)

        self._seed()

    # ------------------------------------------------------------ setup
    def _seed(self) -> None:
        now = self.clock()
        self.facts[self.bad] = Fact(
            kind=FAIL, source="known", at=now,
            output="发布工程师指定的已知失败端点（known bad）",
        )
        self.facts[self.good] = Fact(
            kind=PASS, source="known", at=now,
            output="发布工程师确认的已通过祖先（known good）",
        )
        self._event("info", f"建立搜索区间：失败端点 {self._short(self.bad)}，"
                            f"通过祖先 {self._short(self.good)}")

    def _short(self, sha: str) -> str:
        c = self.graph.get(sha)
        return c.short if c else sha[:7]

    def _event(self, level: str, text: str) -> None:
        self.events.append({"t": round(self.clock(), 3), "level": level, "text": text})
        if len(self.events) > 200:
            self.events = self.events[-200:]

    # ------------------------------------------------------------ lifecycle
    def start(self) -> "BisectSession":
        launches = self._plan_and_mark()
        for sha in launches:
            self.executor.submit(self._make_worker(sha))
        return self

    def _plan_and_mark(self) -> List[str]:
        """Fill free slots. Must be called with self.lock held conceptually;
        acquires the lock itself and marks jobs running before returning."""
        launches: List[str] = []
        with self.lock:
            if self.status != "running" or self.draining:
                return launches
            while len(self.inflight) < self.concurrency:
                sha = choose_next(self.graph, self.bad, self.facts, self.inflight)
                if sha is None:
                    break
                self._mark_running(sha)
                launches.append(sha)
            if not self.inflight and not launches:
                self._finish_if_done_locked()
        return launches

    def _mark_running(self, sha: str) -> Job:
        job = Job(sha=sha, number=next(self._job_seq), started_at=self.clock())
        self.jobs.setdefault(sha, []).append(job)
        self.inflight.add(sha)
        self._event("info", f"开始实测 {self._short(sha)}（第 {job.number} 次）："
                           f"将该提交的文件解出后运行 QA")
        return job

    def _make_worker(self, sha: str) -> Callable[[], None]:
        def work() -> None:
            result = None
            try:
                result = self.runner(sha)
            except Exception as exc:  # noqa: BLE001 - runner must never kill worker
                result = _ErrorResult(
                    f"runner 异常: {exc}\n{traceback.format_exc(limit=4)}"
                )
            self.finish_job(sha, result)
        return work

    def finish_job(self, sha: str, result) -> None:
        """Record a real test result and advance the state machine."""
        spawn: List[str] = []
        with self.lock:
            job_list = self.jobs.get(sha, [])
            job = next((j for j in reversed(job_list) if j.finished_at is None), None)
            now = self.clock()
            kind = getattr(result, "kind", ERROR)
            if kind not in RESULT_KINDS:
                kind = ERROR
            output = getattr(result, "output", "") or ""
            read_files = getattr(result, "read_files", []) or []
            duration = float(getattr(result, "duration", 0.0) or 0.0)
            workdir = getattr(result, "workdir", "") or ""

            if job is not None:
                job.finished_at = now
                job.kind = kind
                job.output = output[-4000:]
                job.read_files = read_files
                job.duration = duration
                job.workdir = workdir
            self.inflight.discard(sha)

            attempt_no = len(self.jobs.get(sha, []))
            self.facts[sha] = Fact(
                kind=kind, source="test", at=now, output=output[-4000:],
                read_files=read_files, duration=duration, attempts=attempt_no,
            )

            label = {PASS: "通过", FAIL: "失败", SKIP: "跳过（未知）",
                     ERROR: "测试出错（未知）"}[kind]
            level = {"pass": "success", "fail": "danger", "skip": "warn",
                     "error": "warn"}[kind]
            self._event(level, f"实测完成 {self._short(sha)}：{label}")

            # Conflict evaluation uses every known / measured fact.
            self.conflicts = find_conflicts(self.graph, self.facts)
            if self.conflicts:
                if not self.draining:
                    self.draining = True
                    self._event("danger",
                                "发现“祖先失败、后代通过”的冲突事实；"
                                "停止补测，等待已开跑的测试实测完成后结束本轮")
                if not self.inflight:
                    self.status = "conflict"
                    self.draining = False
                    self.finished_at = now
                    pairs = "；".join(
                        f"{self._short(p['fail'])} ✗ 是 {self._short(p['pass'])} ✓ 的祖先"
                        for p in self.conflicts
                    )
                    self._event("danger", f"本轮以冲突结束：{pairs}")
            else:
                while len(self.inflight) < self.concurrency:
                    nxt = choose_next(self.graph, self.bad, self.facts, self.inflight)
                    if nxt is None:
                        break
                    self._mark_running(nxt)
                    spawn.append(nxt)
                self._finish_if_done_locked()

        for nxt in spawn:
            self.executor.submit(self._make_worker(nxt))

    def _finish_if_done_locked(self) -> None:
        if self.status != "running":
            return
        if self.inflight:
            return
        if self.draining:
            self.status = "conflict"
            self.draining = False
            self.finished_at = self.clock()
            return
        nxt = choose_next(self.graph, self.bad, self.facts, self.inflight)
        if nxt is None:
            self.status = "complete"
            self.finished_at = self.clock()
            cands = earliest_failure_candidates(self.graph, self.bad, self.facts)
            self._event("success",
                        f"没有空位需要补测，定位结束；可能的最早失败提交 "
                        f"{len(cands)} 个：" + "、".join(self._short(c) for c in cands))

    # ------------------------------------------------------------ manual retry
    def retry(self, sha: str) -> tuple[bool, str, List[str]]:
        """Manually re-run a skipped / errored commit. Respects slot limit."""
        spawn: List[str] = []
        with self.lock:
            if self.status != "running" or self.draining:
                return False, "本轮已经结束，不能再补测；请发起新一轮", []
            if sha not in self.graph.commits:
                return False, "未知提交", []
            fact = self.facts.get(sha)
            if fact is None or fact.kind not in (SKIP, ERROR):
                return False, "只能手动重测曾跳过或出错的提交", []
            if sha in self.inflight:
                return False, "该提交正在实测中", []
            if len(self.inflight) >= self.concurrency:
                return False, "两个测试槽位都在忙碌", []
            # Remove the old unknown fact while keeping job history.
            self.facts.pop(sha, None)
            self._mark_running(sha)
            spawn.append(sha)
        for s in spawn:
            self.executor.submit(self._make_worker(s))
        return True, "已重新加入实测", spawn

    # ------------------------------------------------------------ snapshot
    def snapshot(self) -> dict:
        now = self.clock()
        with self.lock:
            pass_implies, fail_implies = implied_sets(self.graph, self.facts)
            candidates = earliest_failure_candidates(self.graph, self.bad, self.facts)
            cand_set = set(candidates)
            conflict_shas = set()
            for p in self.conflicts:
                conflict_shas.add(p["fail"])
                conflict_shas.add(p["pass"])

            domain = self.graph.ancestors_incl(self.bad)
            nodes: Dict[str, dict] = {}
            for sha in self.graph.order:
                nodes[sha] = self._node_json(
                    sha, now, domain, pass_implies, fail_implies,
                    cand_set, conflict_shas,
                )

            running_jobs = [
                j.to_json(now)
                for sha in self.inflight
                for j in self.jobs.get(sha, [])
                if j.finished_at is None
            ]
            all_jobs = [
                j.to_json(now)
                for jobs in self.jobs.values()
                for j in jobs
            ]
            all_jobs.sort(key=lambda j: j["started_at"])

            tested = [s for s, f in self.facts.items() if f.source in ("test", "manual")]
            counts = {
                "commits": len(self.graph.order),
                "in_range": len(domain),
                "tested": len(tested),
                "passed": sum(1 for f in self.facts.values() if f.kind == PASS
                              and f.source != "known"),
                "failed": sum(1 for f in self.facts.values() if f.kind == FAIL
                              and f.source != "known"),
                "skipped": sum(1 for f in self.facts.values() if f.kind == SKIP),
                "errors": sum(1 for f in self.facts.values() if f.kind == ERROR),
                "deduced_pass": len(pass_implies - {s for s, f in self.facts.items()
                                                    if f.kind == PASS}),
                "deduced_fail": len(fail_implies - {s for s, f in self.facts.items()
                                                    if f.kind == FAIL}),
                "candidates": len(candidates),
            }

            return {
                "id": self.id,
                "bad": self.bad,
                "good": self.good,
                "status": self.status,
                "draining": self.draining,
                "concurrency": self.concurrency,
                "active_count": len(self.inflight),
                "running_jobs": running_jobs,
                "jobs": all_jobs,
                "counts": counts,
                "nodes": nodes,
                "candidates": candidates,
                "conflicts": self.conflicts,
                "conflict_shas": sorted(conflict_shas),
                "events": list(self.events),
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "elapsed": round((self.finished_at or now) - self.created_at, 2),
            }

    def _node_json(
        self, sha, now, domain, pass_implies, fail_implies,
        cand_set, conflict_shas,
    ) -> dict:
        c = self.graph.commits[sha]
        fact = self.facts.get(sha)
        active_jobs = [j.to_json(now) for j in self.jobs.get(sha, [])
                       if j.finished_at is None]
        last_job = self.jobs.get(sha, [])[-1] if self.jobs.get(sha) else None

        deduced_from = None
        if fact is not None:
            if fact.source == "known":
                state = "known-bad" if fact.kind == FAIL else "known-good" \
                    if fact.kind == PASS else f"known-{fact.kind}"
            else:
                state = {
                    PASS: "test-pass", FAIL: "test-fail",
                    SKIP: "skip", ERROR: "error",
                }.get(fact.kind, "unknown")
        elif sha in self.inflight or active_jobs:
            state = "running"
        elif sha in pass_implies and sha in fail_implies:
            state = "conflict-deduced"
        elif sha in fail_implies:
            state = "deduced-fail"
            for fsha, f in self.facts.items():
                if f.kind == FAIL and sha in self.graph.descendants_incl(fsha):
                    deduced_from = fsha
                    break
        elif sha in pass_implies:
            state = "deduced-pass"
            for psha, f in self.facts.items():
                if f.kind == PASS and sha in self.graph.ancestors_incl(psha):
                    deduced_from = psha
                    break
        elif sha in domain:
            state = "pending"
        else:
            state = "outside"

        return {
            "sha": sha,
            "short": c.short,
            "subject": c.subject,
            "refs": c.refs,
            "in_range": sha in domain,
            "state": state,
            "is_candidate": sha in cand_set,
            "is_conflict": sha in conflict_shas,
            "deduced_from": deduced_from,
            "attempts": len(self.jobs.get(sha, [])),
            "fact": None if fact is None else {
                "kind": fact.kind,
                "source": fact.source,
                "at": fact.at,
                "output": fact.output,
                "read_files": fact.read_files,
                "duration": round(fact.duration, 3),
                "attempts": fact.attempts,
            },
            "last_job": last_job.to_json(now) if last_job else None,
            "active_jobs": active_jobs,
        }


class _ErrorResult:
    kind = ERROR
    output = ""
    read_files: List[dict] = []
    duration = 0.0
    workdir = ""

    def __init__(self, output: str):
        self.output = output
