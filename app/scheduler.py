"""Site-wide scheduling of real test executions.

The whole web site shares a fixed number of *real* test slots (two in
production).  A commit is only "testing" once a slot has actually been
reserved for its request **and** the owning session has re-validated and
accepted that request via :meth:`SlotRequest.handle_granted`.

Requests that cannot get a slot immediately wait in a single fair FIFO
queue.  When a slot is released it is handed directly to the oldest waiter;
the session then checks, against its newest facts, whether the commit still
needs a measurement.  If not (it became deducible, the round ended / found a
conflict, ...) the grant is rejected, no test process is started, and the
slot moves on to the next waiter.

Only queue/accounting state lives under the scheduler lock; session locks
are never acquired while holding it, so lock ordering is one-directional
(scheduler lock -> session lock inside the grant callback).
"""
from __future__ import annotations

import itertools
import threading
from collections import deque
from typing import Callable, Deque, List, Optional


class SlotRequest:
    """One wanted test execution for one session.

    The same object travels from the session's planning set into the global
    queue and finally to the granted worker.  Identity is object identity.
    """

    _ids = itertools.count(1)

    def __init__(self, session, sha: str, kind: str = "auto"):
        self.id = next(self._ids)
        self.session = session
        self.sha = sha
        # auto = fill-a-free-slot supplementary test; manual = user retry.
        self.kind = kind
        self.slot_held = False
        self.cancelled = False
        self.enqueued_at: Optional[float] = None

    def handle_granted(self) -> bool:
        """Invoke the session's admission + launch logic.

        Returns ``True`` when the request was accepted (a worker was launched
        and now owns the slot until it finishes) and ``False`` when the grant
        must be released / handed to the next waiter.
        """
        return self.session._grant_request(self)


# A grant function is mostly indirected so tests / alternate front-ends can
# plug behaviour without subclassing the scheduler.
GrantFn = Callable[[SlotRequest], bool]


class SlotScheduler:
    """A FIFO permit pool with ``slots`` identical real-execution permits."""

    def __init__(self, slots: int = 2):
        self.slots = max(1, int(slots))
        self._lock = threading.Lock()
        self._active = 0
        self._queue: Deque[SlotRequest] = deque()

    # ------------------------------------------------------------ introspection
    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def active_count(self) -> int:
        with self._lock:
            return self._active

    def waiting_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def stats(self) -> dict:
        with self._lock:
            return {
                "slots": self.slots,
                "active": self._active,
                "waiting": len(self._queue),
                "free": max(0, self.slots - self._active),
            }

    def waiting_shas(self) -> List[str]:
        with self._lock:
            return [r.sha for r in self._queue]

    def position_of(self, req: SlotRequest) -> Optional[int]:
        """1-based global queue position, or None if not queued."""
        with self._lock:
            for i, r in enumerate(self._queue, start=1):
                if r is req:
                    return i
        return None

    def positions(self) -> dict:
        """Map ``SlotRequest.id`` -> 1-based global FIFO position."""
        with self._lock:
            return {r.id: i for i, r in enumerate(self._queue, start=1)}

    # ------------------------------------------------------------- queue ops
    def submit(self, reqs) -> List[SlotRequest]:
        """Enqueue requests; immediately reserve slots for as many as free.

        Returns the requests for which a slot was reserved.  The caller is
        responsible for admitting them and must call :meth:`release` for any
        rejected grant.  Everything else stays in the FIFO queue.
        """
        if isinstance(reqs, SlotRequest):
            reqs = [reqs]
        granted: List[SlotRequest] = []
        with self._lock:
            for req in reqs:
                if req.cancelled:
                    continue
                if self._active < self.slots:
                    self._active += 1
                    req.slot_held = True
                    granted.append(req)
                else:
                    self._queue.append(req)
        return granted

    def cancel(self, reqs) -> int:
        """Remove not-yet-granted requests from the FIFO queue."""
        if not reqs:
            return 0
        dead = {id(r): r for r in reqs}
        removed = 0
        with self._lock:
            kept: Deque[SlotRequest] = deque()
            for r in self._queue:
                if id(r) in dead:
                    r.cancelled = True
                    removed += 1
                else:
                    kept.append(r)
            self._queue = kept
        for r in reqs:
            r.cancelled = True
        return removed

    # ----------------------------------------------------------- slot release
    def release(self, req: SlotRequest, grant_fn: Optional[GrantFn] = None) -> None:
        """Release a slot owned by ``req`` and drain queued waiters.

        The freed slot is handed directly to the oldest waiter.  Every handed
        request is passed to ``grant_fn`` (defaults to
        :meth:`SlotRequest.handle_granted`); rejected grants release / hand
        off again until some waiter is accepted or the queue is empty.
        """
        grant = grant_fn or (lambda r: r.handle_granted())
        nxt = self._free_or_handoff(req)
        while nxt is not None:
            accepted = False
            try:
                accepted = bool(grant(nxt))
            except Exception:  # pragma: no cover - defensive
                # A broken session callback must not swallow the site slot.
                accepted = False
            if accepted:
                return
            nxt = self._free_or_handoff(nxt)

    def _free_or_handoff(self, req: SlotRequest) -> Optional[SlotRequest]:
        """Drop ``req``'s slot; hand it to the next live waiter if possible."""
        with self._lock:
            if not req.slot_held:
                return None
            req.slot_held = False
            while self._queue:
                nxt = self._queue.popleft()
                if nxt.cancelled:
                    continue
                nxt.slot_held = True
                return nxt
            self._active = max(0, self._active - 1)
            return None
