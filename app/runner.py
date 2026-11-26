"""Test runner: materialise *the files of one exact commit* and run its QA.

Every job gets a fresh temporary directory populated from ``git archive
<sha>`` of the built-in repository.  Two concurrent jobs therefore read two
different commits' files and can never observe each other's working tree.
"""
from __future__ import annotations

import io
import os
import random
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class RunnerResult:
    kind: str  # pass | fail | skip | error
    output: str = ""
    read_files: List[dict] = field(default_factory=list)
    duration: float = 0.0
    workdir: str = ""


class GitArchiveRunner:
    def __init__(
        self,
        repo_path: str,
        work_root: str = "/tmp/gsb-bisect/work",
        *,
        delay_min: float = 0.0,
        delay_max: float = 0.0,
        timeout: int = 60,
    ):
        self.repo_path = repo_path
        self.work_root = work_root
        self.delay_min = float(delay_min)
        self.delay_max = float(delay_max)
        self.timeout = timeout
        os.makedirs(self.work_root, exist_ok=True)

    # ------------------------------------------------------------- internals
    def _extract(self, sha: str, dest: str) -> None:
        proc = subprocess.run(
            ["git", "-C", self.repo_path, "archive", "--format=tar", sha],
            capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git archive {sha} failed: {proc.stderr.decode('utf-8', 'replace')}"
            )
        with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tf:
            # Python 3.11 supports the filter argument; 'data' strips unsafe bits.
            try:
                tf.extractall(dest, filter="data")
            except TypeError:  # pragma: no cover - older Python
                tf.extractall(dest)

    @staticmethod
    def _parse_read_files(output: str) -> List[dict]:
        files: List[dict] = []
        seen = set()
        for line in output.splitlines():
            if line.startswith("READ "):
                rel = line[5:].strip()
                if rel and rel not in seen:
                    seen.add(rel)
                    files.append({"path": rel})
        return files

    # --------------------------------------------------------------- main API
    def __call__(self, sha: str) -> RunnerResult:
        return self.run(sha)

    def run(self, sha: str) -> RunnerResult:
        started = time.time()
        short = sha[:10]
        dest = tempfile.mkdtemp(prefix=f"qa-{short}-", dir=self.work_root)
        output_parts: List[str] = [
            f"$ # isolated checkout of {sha}",
            f"$ git -C {self.repo_path} archive {sha} | tar -x -C {dest}",
        ]
        try:
            # Materialise THIS commit's files first, so the running QA process
            # and the on-disk tree both correspond to the requested sha.
            self._extract(sha, dest)
            output_parts.append(f"[runner] extracted commit files into {dest}")

            delay = 0.0
            if self.delay_max > 0:
                hi = max(self.delay_max, self.delay_min)
                delay = random.uniform(self.delay_min, hi)
                time.sleep(delay)

            checker = os.path.join(dest, "qa", "run_check.py")
            if not os.path.exists(checker):
                return RunnerResult(
                    kind="error",
                    output="\n".join(output_parts)
                    + "\n[runner] qa/run_check.py missing at this commit",
                    duration=time.time() - started, workdir=dest,
                )

            env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            proc = subprocess.run(
                [sys.executable, "-I", checker],
                cwd=dest, env=env, capture_output=True,
                text=True, timeout=self.timeout,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            output_parts.append(f"$ python -I qa/run_check.py   # exit {proc.returncode}")
            output_parts.append(out.strip())
            read_files = self._parse_read_files(out)
            kind = {0: "pass", 77: "skip"}.get(proc.returncode, "fail")
            if proc.returncode not in (0, 1, 77):
                kind = "error"
            return RunnerResult(
                kind=kind, output="\n".join(output_parts),
                read_files=read_files,
                duration=round(time.time() - started, 3), workdir=dest,
            )
        except subprocess.TimeoutExpired:
            return RunnerResult(
                kind="error",
                output="\n".join(output_parts)
                + f"\n[runner] timed out after {self.timeout}s",
                duration=time.time() - started, workdir=dest,
            )
        except Exception as exc:  # noqa: BLE001
            return RunnerResult(
                kind="error",
                output="\n".join(output_parts) + f"\n[runner] {type(exc).__name__}: {exc}",
                duration=time.time() - started, workdir=dest,
            )
        finally:
            shutil.rmtree(dest, ignore_errors=True)


class GlobalSlotGate:
    """Process-wide cap on actually executing tests.

    Every session already caps itself at two in-flight jobs; this wrapper makes
    the literal "at most two tests run at the same time" guarantee hold even
    if several rounds are created through the API concurrently.
    """

    def __init__(self, inner, slots: int = 2):
        self.inner = inner
        self.semaphore = threading.BoundedSemaphore(slots)
        self.slots = slots

    def __call__(self, sha: str) -> RunnerResult:
        with self.semaphore:
            return self.inner(sha)
