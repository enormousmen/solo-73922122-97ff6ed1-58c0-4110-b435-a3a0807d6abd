"""Load the built-in repository into a :class:`~app.graph.CommitGraph`."""
from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Optional

from .graph import Commit, CommitGraph

PRESETS = [
    {
        "id": "main-merge-train",
        "label": "合并列车：三条特性分支 + 跳过提交",
        "bad": "main",
        "good": "baseline-good",
        "description": (
            "main 末端已知失败，baseline-good 已通过。三条并行特性线各藏一个 "
            "问题，其中一个提交被跳过；预期得到 4 个可能的最早失败提交。"
        ),
    },
    {
        "id": "conflict-pocket",
        "label": "冲突演示：祖先失败、后代通过",
        "bad": "conflict-bad",
        "good": "baseline-good",
        "description": (
            "已知失败端点与通过祖先之间恰好只有两个未决提交，两个槽位会同时"
            "开跑；实测得到祖先失败、后代通过，本轮以冲突结束。"
        ),
    },
]


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, *args], text=True,
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def load_graph(repo: str) -> CommitGraph:
    # One record per commit, topological children-before-parents order.
    # \x1e separates records, \x1f separates fields.
    fmt = "%x1e%H%x1f%h%x1f%P%x1f%s%x1f%an%x1f%cI"
    raw = _git(repo, "log", "--all", "--topo-order", f"--format={fmt}")

    # Refs per sha (branches / tags). Use a literal space separator:
    # object names contain no spaces and refname is the remainder.
    refs_map: Dict[str, List[str]] = {}
    rout = _git(repo, "for-each-ref", "--format=%(objectname) %(refname)")
    for line in rout.splitlines():
        sha, _, refname = line.partition(" ")
        if not refname:
            continue
        if refname.startswith("refs/heads/"):
            label = refname[len("refs/heads/"):]
        elif refname.startswith("refs/tags/"):
            label = "tag:" + refname[len("refs/tags/"):]
        else:
            label = refname.split("/")[-1]
        # Hide the internal fixture marker tag from the UI.
        if label.startswith("tag:fixture-version-"):
            continue
        refs_map.setdefault(sha, []).append(label)

    commits: List[Commit] = []
    order: List[str] = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 6:
            continue
        full, short, parents_raw, subject, author, date = parts[:6]
        full = full.strip()
        parents = parents_raw.split()
        commits.append(
            Commit(
                sha=full, short=short, subject=subject,
                parents=parents, author=author, date=date,
                refs=sorted(refs_map.get(full, [])),
            )
        )
        order.append(full)

    return CommitGraph(commits, order=order)


def validate_pair(graph: CommitGraph, bad_ref: str, good_ref: str) -> tuple[str, str, Optional[str]]:
    """Resolve and validate user selection. Returns (bad, good, error)."""
    bad = graph.resolve(bad_ref or "")
    good = graph.resolve(good_ref or "")
    if not bad:
        return "", "", f"找不到失败提交：{bad_ref!r}"
    if not good:
        return "", "", f"找不到通过祖先：{good_ref!r}"
    if bad == good:
        return "", "", "失败提交和通过祖先不能是同一个提交"
    if not graph.is_ancestor(good, bad):
        return "", "", (
            f"{good[:7]} 不是 {bad[:7]} 的祖先；"
            "请选择该失败提交历史线上的已通过提交"
        )
    return bad, good, None
