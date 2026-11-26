"""Commit DAG model and ancestry / lane helpers.

The engine only depends on this small in-memory graph, which keeps the
scheduling rules unit-testable without a git binary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set


@dataclass
class Commit:
    sha: str
    short: str
    subject: str
    parents: List[str] = field(default_factory=list)
    author: str = ""
    date: str = ""
    refs: List[str] = field(default_factory=list)


class CommitGraph:
    """Directed acyclic graph of commits; edges point commit -> parents."""

    def __init__(self, commits: Sequence[Commit], order: Optional[Sequence[str]] = None):
        self.commits: Dict[str, Commit] = {c.sha: c for c in commits}
        if order is not None:
            self.order: List[str] = [s for s in order if s in self.commits]
        else:
            self.order = [c.sha for c in commits]
        # Keep order valid even if the caller passed extra/missing entries.
        if len(self.order) != len(self.commits):
            seen = set(self.order)
            self.order.extend(s for s in self.commits if s not in seen)

        self.children: Dict[str, List[str]] = {sha: [] for sha in self.commits}
        for c in commits:
            for p in c.parents:
                if p in self.commits:
                    self.children.setdefault(p, []).append(c.sha)

        self._anc_cache: Dict[str, Set[str]] = {}
        self._desc_cache: Dict[str, Set[str]] = {}

    # ------------------------------------------------------------------ lookup
    def get(self, sha: str) -> Optional[Commit]:
        return self.commits.get(sha)

    def resolve(self, ref: str) -> Optional[str]:
        """Resolve a sha / short sha / ref label / tag to a full sha."""
        if not ref:
            return None
        ref = ref.strip()
        if ref in self.commits:
            return ref
        low = ref.lower()
        # short sha / prefix
        matches = [sha for sha in self.commits if sha.startswith(low)]
        if len(matches) == 1:
            return matches[0]
        # ref labels (branch / tag), accepting both "foo" and "tag:foo".
        simple: Dict[str, str] = {}
        for sha, c in self.commits.items():
            for r in c.refs:
                simple[r] = sha
                if r.startswith("tag:"):
                    simple[r[4:]] = sha
        if ref in simple:
            return simple[ref]
        for sha, c in self.commits.items():
            if ref in c.refs or low in {r.lower() for r in c.refs}:
                return sha
        return None

    # --------------------------------------------------------------- ancestry
    def ancestors_incl(self, sha: str) -> Set[str]:
        """All ancestors of sha including sha itself."""
        if sha in self._anc_cache:
            return self._anc_cache[sha]
        seen: Set[str] = set()
        stack = [sha]
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in self.commits:
                continue
            seen.add(cur)
            stack.extend(self.commits[cur].parents)
        self._anc_cache[sha] = seen
        return seen

    def descendants_incl(self, sha: str) -> Set[str]:
        """All descendants of sha including sha itself."""
        if sha in self._desc_cache:
            return self._desc_cache[sha]
        seen: Set[str] = set()
        stack = [sha]
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in self.children:
                continue
            seen.add(cur)
            stack.extend(self.children.get(cur, []))
        self._desc_cache[sha] = seen
        return seen

    def is_ancestor(self, maybe_ancestor: str, sha: str) -> bool:
        return maybe_ancestor in self.ancestors_incl(sha)

    # ------------------------------------------------------------- lane layout
    def lanes(self) -> Dict[str, dict]:
        """Compute simple git-graph style lane descriptors.

        Processes commits in ``self.order`` (children before parents).
        Guarantees for every known edge ``c -> p`` that
        ``lanes[c]["parent_lanes"][p] == lanes[p]["lane"]`` so a connector
        drawn to the bottom of c's row meets p's dot on the parent row.
        """
        # lane -> sha currently owning the open line at this point in history
        active: List[Optional[str]] = []
        result: Dict[str, dict] = {}

        def free_lane() -> int:
            for i, owner in enumerate(active):
                if owner is None:
                    return i
            active.append(None)
            return len(active) - 1

        def lane_owned_by(target: str) -> Optional[int]:
            for i, owner in enumerate(active):
                if owner == target:
                    return i
            return None

        for sha in self.order:
            c = self.commits[sha]
            # A previously seen child may have reserved a lane for this sha.
            lane = lane_owned_by(sha)
            if lane is None:
                lane = free_lane()
            # Converge any duplicate reservation onto the chosen lane.
            for i, owner in enumerate(active):
                if owner == sha and i != lane:
                    active[i] = None
            through = [
                i for i, owner in enumerate(active)
                if owner is not None and owner != sha and i != lane
            ]
            active[lane] = sha

            parent_lanes: Dict[str, int] = {}
            parents_known = [p for p in c.parents if p in self.commits]
            if not parents_known:
                # root: line terminates at the dot
                active[lane] = None
            else:
                for idx, p in enumerate(parents_known):
                    existing = lane_owned_by(p)
                    if existing is not None:
                        pl = existing
                        if idx == 0 and pl != lane:
                            # first parent already runs in another lane:
                            # peel our lane towards it instead of duplicating.
                            active[lane] = None
                    elif idx == 0:
                        # first parent continues straight on this lane
                        pl = lane
                        active[lane] = p
                    else:
                        pl = free_lane()
                        active[pl] = p
                    parent_lanes[p] = pl

            width = max(len(active), 1)
            result[sha] = {
                "lane": lane,
                "parent_lanes": parent_lanes,
                "through": through,
                "lanes": width,
            }

        # Normalize width to global maximum so every row aligns.
        total = max((d["lanes"] for d in result.values()), default=1)
        for d in result.values():
            d["lanes"] = total
        return result

    def to_public(self) -> List[dict]:
        layout = self.lanes()
        out = []
        for sha in self.order:
            c = self.commits[sha]
            out.append(
                {
                    "sha": c.sha,
                    "short": c.short,
                    "subject": c.subject,
                    "parents": list(c.parents),
                    "author": c.author,
                    "date": c.date,
                    "refs": list(c.refs),
                    "layout": layout.get(sha),
                }
            )
        return out
