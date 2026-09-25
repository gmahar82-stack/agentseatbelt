"""Which guarded run (if any) the current code belongs to."""

from __future__ import annotations

import contextvars
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .guard import _Run

current: contextvars.ContextVar["_Run | None"] = contextvars.ContextVar("agentguard_run", default=None)
active_runs: set["_Run"] = set()
active_lock = threading.Lock()


def current_run() -> "_Run | None":
    """The run for the calling code.

    Context variables follow asyncio tasks but not new threads, so a request from a worker thread
    (e.g. a framework's thread pool) is attributed to the run when exactly one run is active.
    """
    run = current.get()
    if run is not None and not run.finished:
        return run
    with active_lock:
        if len(active_runs) == 1:
            return next(iter(active_runs))
    return None
