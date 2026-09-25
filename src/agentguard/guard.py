"""The Guard: runs an agent function under budget, rate, time, loop and endpoint limits."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import inspect
import json
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from . import _state, interceptors
from .errors import (
    BlockedRequest,
    BudgetExceeded,
    EmergencyStop,
    EndpointBlocked,
    GuardStop,
    LoopDetected,
    RateLimitExceeded,
    RequestLimitExceeded,
    TimeLimitExceeded,
)
from .pricing import DEFAULT_PRICES, FALLBACK_PRICE, estimate_cost, extract_usage
from .report import Event, RunReport

MAX_JSON_BYTES = 5 * 1024 * 1024
FINGERPRINT_BODY_BYTES = 64 * 1024
WATCH_INTERVAL_S = 0.2


@dataclass
class GuardResult:
    output: Any
    report: RunReport

    @property
    def stopped(self) -> bool:
        return self.report.stopped


def _parse_json(data: bytes | None, content_type: str = "application/json") -> Any:
    if not data or len(data) > MAX_JSON_BYTES or "json" not in content_type.lower():
        return None
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None


def _matches(pattern: str, url: str) -> bool:
    """Endpoint patterns: "api.example.com" (host and its subdomains), "api.example.com/v1/admin"
    (host + path prefix), "https://..." (full URL prefix), or globs with * such as "*.openai.com"."""
    p = pattern.lower().strip()
    parts = urlsplit(url.lower())
    host, host_path = parts.hostname or "", (parts.hostname or "") + parts.path
    if "://" in p:
        return fnmatch(url.lower(), p) if "*" in p else url.lower().startswith(p)
    if "/" in p:
        return fnmatch(host_path, p) if "*" in p else host_path.startswith(p)
    if "*" in p:
        return fnmatch(host, p)
    return host == p or host.endswith("." + p)


class _Run:
    """State of one guarded run. Thread-safe: requests may come from several threads."""

    def __init__(self, guard: "Guard"):
        self.guard = guard
        self.lock = threading.RLock()
        self.started = time.monotonic()
        limit = guard.max_runtime_seconds
        self.deadline = self.started + limit if limit else None
        self.report = RunReport()
        self.stopped: GuardStop | None = None
        self.finished = False
        self.done = threading.Event()
        self.request_times: deque[float] = deque()
        self.fingerprints: deque[str] = deque(maxlen=guard.loop_window)
        # Hard-kill bookkeeping (sync runs) and cancellation (async runs).
        self.thread_id: int | None = None
        self.in_agent = False
        self.needs_kill = False
        self.task: asyncio.Task | None = None

    # ---- events and stopping -------------------------------------------------------------

    def event(self, kind: str, detail: str) -> None:
        ev = Event(time.monotonic() - self.started, kind, detail)
        self.report.events.append(ev)
        if self.guard.verbose:
            print(f"[agentguard {ev.t:7.2f}s] {kind}: {detail}", file=sys.stderr)
        if self.guard.on_event:
            try:
                self.guard.on_event(ev)
            except Exception:  # a broken callback must not break the guard
                pass

    def mark_stop(self, exc: GuardStop, kill: bool = True) -> GuardStop:
        """Record a stop (first reason wins). ``kill`` asks the watchdog to interrupt the agent
        right away; stops raised inside the agent's own call don't need that."""
        with self.lock:
            if self.stopped is None:
                self.stopped = exc
                self.report.stop_reason = exc.reason
                self.report.message = exc.message
                self.event("stop", exc.message)
                self.needs_kill = kill
            return self.stopped

    def _raise_stop(self, exc: GuardStop):
        raise self.mark_stop(exc, kill=False).with_traceback(None)

    def _check_limits(self) -> None:
        g = self.guard
        if self.stopped is not None:
            self.needs_kill = False  # the stop is being raised right here; no need to interrupt as well
            raise self.stopped.with_traceback(None)
        if g.kill_file and os.path.exists(g.kill_file):
            self._raise_stop(EmergencyStop(f"Kill file found: {g.kill_file}"))
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self._raise_stop(TimeLimitExceeded(f"Time limit of {g.max_runtime_seconds}s reached"))
        if g.max_budget_usd is not None and self.report.cost_usd >= g.max_budget_usd:
            self._raise_stop(
                BudgetExceeded(f"Budget of ${g.max_budget_usd:.2f} reached (${self.report.cost_usd:.4f} spent)")
            )

    def _block(self, what: str) -> None:
        self.report.blocked += 1
        self.event("blocked", what)
        if self.guard.stop_on_blocked:
            self._raise_stop(EndpointBlocked(f"Blocked: {what}"))
        raise BlockedRequest(f"AgentGuard blocked {what}")

    def _check_loop(self, fingerprint: str, label: str) -> None:
        g = self.guard
        if not g.loop_threshold:
            return
        self.fingerprints.append(fingerprint)
        repeats = sum(1 for f in self.fingerprints if f == fingerprint)
        if repeats >= g.loop_threshold:
            self._raise_stop(
                LoopDetected(f"Same call repeated {repeats} times in the last {len(self.fingerprints)}: {label}")
            )

    # ---- called by the interceptors and by Guard.checkpoint ------------------------------

    def admit(self, kind: str, method: str, target: str, payload: bytes) -> float:
        """Check every limit before a request/action. Returns 0 to proceed, or seconds to wait
        (rate limit). Raises GuardStop to stop the run, or BlockedRequest to refuse one call."""
        g = self.guard
        with self.lock:
            self._check_limits()

            if kind == "http":
                if any(_matches(p, target) for p in g.block_endpoints) or (
                    g.allow_endpoints is not None and not any(_matches(p, target) for p in g.allow_endpoints)
                ):
                    self._block(f"{method} {target}")
                if g.max_requests is not None and self.report.requests >= g.max_requests:
                    self._raise_stop(RequestLimitExceeded(f"Request limit of {g.max_requests} reached"))
                if g.max_requests_per_minute:
                    now = time.monotonic()
                    while self.request_times and self.request_times[0] <= now - g.rate_window_seconds:
                        self.request_times.popleft()
                    if len(self.request_times) >= g.max_requests_per_minute:
                        if g.on_rate_limit == "stop":
                            self._raise_stop(
                                RateLimitExceeded(f"More than {g.max_requests_per_minute} requests per minute")
                            )
                        wait = self.request_times[0] + g.rate_window_seconds - now
                        if self.deadline is not None:
                            wait = min(wait, max(self.deadline - now, 0.0))
                        self.report.rate_limit_waits += 1
                        self.event("wait", f"rate limit, waiting {wait:.2f}s")
                        return max(wait, 0.001)
                    self.request_times.append(now)
                label = f"{method} {target}"
            else:
                if any(fnmatch(target.lower(), p.lower()) for p in g.block_actions):
                    self._block(f"action {target}")
                label = f"action {target}"

            digest = hashlib.sha1(f"{kind}|{method}|{target}|".encode() + payload[:FINGERPRINT_BODY_BYTES])
            self._check_loop(digest.hexdigest(), label)

            if kind == "http":
                self.report.requests += 1
                self.report.hosts[urlsplit(target).hostname or "?"] += 1
                self.event("request", label)
            else:
                self.report.actions += 1
                self.event("action", target)
            return 0.0

    def record_response(self, url: str, request_body: bytes, content_type: str, body: bytes | None, streamed: bool) -> None:
        g = self.guard
        request_json = _parse_json(request_body)
        if streamed:
            if isinstance(request_json, dict) and "model" in request_json:
                with self.lock:
                    self.report.untracked_streams += 1
                    self.event("note", f"streamed LLM response from {urlsplit(url).hostname} not costed")
            return
        usage = extract_usage(_parse_json(body, content_type), request_json, url)
        if usage is None:
            return
        cost, estimated = estimate_cost(usage, g.prices, g.fallback_price)
        with self.lock:
            r = self.report
            r.llm_calls += 1
            r.input_tokens += usage.input_tokens
            r.output_tokens += usage.output_tokens
            r.cost_usd += cost
            r.models[usage.model or "unknown"] += 1
            if estimated:
                r.estimated_cost_calls += 1
            self.event(
                "llm",
                f"{usage.model or 'unknown model'}: {usage.input_tokens}+{usage.output_tokens} tokens, "
                f"${cost:.4f}{' (fallback price)' if estimated else ''}, total ${r.cost_usd:.4f}",
            )
            if g.max_budget_usd is not None and r.cost_usd >= g.max_budget_usd:
                self.mark_stop(
                    BudgetExceeded(f"Budget of ${g.max_budget_usd:.2f} reached (${r.cost_usd:.4f} spent)")
                )

    def add_cost(self, usd: float, note: str) -> None:
        g = self.guard
        with self.lock:
            self.report.cost_usd += usd
            self.event("note", f"manual cost ${usd:.4f}{': ' + note if note else ''}")
            if g.max_budget_usd is not None and self.report.cost_usd >= g.max_budget_usd:
                self.mark_stop(
                    BudgetExceeded(f"Budget of ${g.max_budget_usd:.2f} reached (${self.report.cost_usd:.4f} spent)")
                )

    # ---- watchdog ------------------------------------------------------------------------

    def poll_external(self) -> None:
        """Deadline and kill-file checks that must fire even when the agent makes no calls."""
        g = self.guard
        if self.stopped is not None:
            return
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.mark_stop(TimeLimitExceeded(f"Time limit of {g.max_runtime_seconds}s reached"))
        elif g.kill_file and os.path.exists(g.kill_file):
            self.mark_stop(EmergencyStop(f"Kill file found: {g.kill_file}"))

    def inject_stop(self) -> None:
        """Raise the stop exception inside the agent's thread (interrupts pure-Python loops;
        code blocked inside a C call stops as soon as that call returns)."""
        with self.lock:
            if not (self.in_agent and self.needs_kill and self.thread_id is not None and self.stopped):
                return
            self.needs_kill = False
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(self.thread_id), ctypes.py_object(type(self.stopped))
            )

    def clear_pending_kill(self) -> None:
        if self.thread_id is not None:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(self.thread_id), None)

    def finish(self, status: str, error: BaseException | None) -> RunReport:
        with self.lock:
            self.finished = True
            r = self.report
            r.duration_s = time.monotonic() - self.started
            if error is not None:
                r.status = "error"
                r.error = f"{type(error).__name__}: {error}"
            elif self.stopped is not None:
                r.status = "stopped"
            else:
                r.status = status
            return r


class Guard:
    """A seatbelt for agents.

    Example::

        guard = Guard(max_budget_usd=5.00, max_requests=100, max_runtime_seconds=300,
                      block_endpoints=["api.expensive-service.com"])
        result = guard.run(my_agent, input_data)
        print(result.report.summary())
    """

    def __init__(
        self,
        *,
        max_budget_usd: float | None = None,
        max_requests: int | None = None,
        max_requests_per_minute: int | None = None,
        max_runtime_seconds: float | None = None,
        block_endpoints: Iterable[str] = (),
        allow_endpoints: Iterable[str] | None = None,
        block_actions: Iterable[str] = (),
        loop_threshold: int | None = 5,
        loop_window: int = 20,
        pricing: dict[str, tuple[float, float]] | None = None,
        fallback_price: tuple[float, float] = FALLBACK_PRICE,
        on_rate_limit: str = "wait",
        stop_on_blocked: bool = False,
        kill_file: str | None = None,
        hard_kill: bool = True,
        raise_on_stop: bool = False,
        verbose: bool = False,
        on_event: Callable[[Event], None] | None = None,
        rate_window_seconds: float = 60.0,
    ):
        """
        Args:
            max_budget_usd: Stop when the estimated LLM cost reaches this. Checked before each call,
                so the call that crosses the limit still completes.
            max_requests: Stop after this many HTTP requests.
            max_requests_per_minute: Rate limit; see ``on_rate_limit``.
            max_runtime_seconds: Stop after this long, even mid-loop (see ``hard_kill``).
            block_endpoints: Endpoint patterns that are refused (host, host/path prefix, URL prefix, or glob).
            allow_endpoints: If given, only these endpoint patterns are allowed.
            block_actions: Glob patterns for action names passed to ``checkpoint()``, e.g. ["delete_*"].
            loop_threshold: Stop when the identical request/action appears this many times within the
                last ``loop_window`` calls. None disables loop detection.
            pricing: Extra/override prices, {model: (usd_per_1M_input, usd_per_1M_output)}.
            fallback_price: Price for unknown models (deliberately high so budgets fail safe).
            on_rate_limit: "wait" (throttle) or "stop" (end the run).
            stop_on_blocked: End the run on a blocked call instead of raising BlockedRequest for that call.
            kill_file: If this file appears, the run stops (an emergency stop you can trigger from anywhere).
            hard_kill: Interrupt the agent's thread on time limit / emergency stop / budget, even when it
                is not making requests.
            raise_on_stop: Re-raise the GuardStop from run() instead of returning a result.
            verbose: Print every event to stderr.
            on_event: Callback receiving each Event (live monitoring).
        """
        if on_rate_limit not in ("wait", "stop"):
            raise ValueError('on_rate_limit must be "wait" or "stop"')
        self.max_budget_usd = max_budget_usd
        self.max_requests = max_requests
        self.max_requests_per_minute = max_requests_per_minute
        self.max_runtime_seconds = max_runtime_seconds
        self.block_endpoints = list(block_endpoints)
        self.allow_endpoints = list(allow_endpoints) if allow_endpoints is not None else None
        self.block_actions = list(block_actions)
        self.loop_threshold = loop_threshold
        self.loop_window = max(loop_window, loop_threshold or 1)
        self.prices = {**DEFAULT_PRICES, **{k.lower(): v for k, v in (pricing or {}).items()}}
        self.fallback_price = fallback_price
        self.on_rate_limit = on_rate_limit
        self.stop_on_blocked = stop_on_blocked
        self.kill_file = kill_file
        self.hard_kill = hard_kill
        self.raise_on_stop = raise_on_stop
        self.verbose = verbose
        self.on_event = on_event
        self.rate_window_seconds = rate_window_seconds
        self.last_report: RunReport | None = None
        self._runs: set[_Run] = set()
        self._runs_lock = threading.Lock()

    # ---- public API ----------------------------------------------------------------------

    def run(self, fn: Callable[..., Any], *args, **kwargs) -> GuardResult:
        """Run a synchronous agent function under this guard."""
        if inspect.iscoroutinefunction(fn):
            raise TypeError("fn is async: use `await guard.arun(fn, ...)`")
        run = self._start()
        run.thread_id = threading.get_ident()
        watchdog = threading.Thread(target=self._watch_sync, args=(run,), daemon=True, name="agentguard-watchdog")
        token = _state.current.set(run)
        output, error = None, None
        try:
            try:
                with run.lock:
                    run.in_agent = True
                try:
                    watchdog.start()
                    output = fn(*args, **kwargs)
                finally:
                    with run.lock:
                        run.in_agent = False
                        run.clear_pending_kill()
            except GuardStop:
                pass
        except BaseException as e:  # the agent's own errors, including KeyboardInterrupt
            error = e
        finally:
            _state.current.reset(token)
            report = self._end(run, error)
        return self._result(run, output, report, error)

    async def arun(self, fn: Callable[..., Any], *args, **kwargs) -> GuardResult:
        """Run an async agent function under this guard."""
        run = self._start()
        token = _state.current.set(run)  # the task below copies this context
        try:
            run.task = asyncio.ensure_future(fn(*args, **kwargs))
        finally:
            _state.current.reset(token)
        watcher = asyncio.ensure_future(self._watch_async(run))
        output, error = None, None
        try:
            output = await run.task
        except asyncio.CancelledError:
            if run.stopped is None:
                error = asyncio.CancelledError()
        except GuardStop:
            pass
        except BaseException as e:
            error = e
        finally:
            watcher.cancel()
            report = self._end(run, error)
        return self._result(run, output, report, error)

    def stop(self, reason: str = "Emergency stop") -> None:
        """Stop every active run of this guard now (safe to call from any thread)."""
        with self._runs_lock:
            runs = list(self._runs)
        for run in runs:
            run.mark_stop(EmergencyStop(reason))
            run.inject_stop()

    def checkpoint(self, action: str, **details: Any) -> None:
        """Report a non-HTTP step (e.g. a tool call). Applies stops, ``block_actions`` and loop
        detection to it. Does nothing outside a guarded run."""
        run = _state.current_run()
        if run is None:
            return
        payload = json.dumps(details, sort_keys=True, default=repr).encode()
        run.admit("action", "", action, payload)

    def add_cost(self, usd: float, note: str = "") -> None:
        """Add a cost the guard can't see (e.g. a paid API it doesn't parse, or a streamed response)."""
        run = _state.current_run()
        if run is not None:
            run.add_cost(usd, note)

    # ---- internals -----------------------------------------------------------------------

    def _start(self) -> _Run:
        interceptors.install()
        run = _Run(self)
        with self._runs_lock:
            self._runs.add(run)
        with _state.active_lock:
            _state.active_runs.add(run)
        return run

    def _end(self, run: _Run, error: BaseException | None) -> RunReport:
        report = run.finish("completed", error)
        run.done.set()
        with self._runs_lock:
            self._runs.discard(run)
        with _state.active_lock:
            _state.active_runs.discard(run)
        self.last_report = report
        return report

    def _result(self, run: _Run, output: Any, report: RunReport, error: BaseException | None) -> GuardResult:
        if error is not None:
            try:
                error.agentguard_report = report  # type: ignore[attr-defined]
            except Exception:
                pass
            raise error
        if run.stopped is not None and self.raise_on_stop:
            raise run.stopped.with_traceback(None)
        return GuardResult(output, report)

    def _watch_sync(self, run: _Run) -> None:
        while not run.done.wait(WATCH_INTERVAL_S):
            run.poll_external()
            if self.hard_kill and run.needs_kill:
                run.inject_stop()

    async def _watch_async(self, run: _Run) -> None:
        while True:
            await asyncio.sleep(WATCH_INTERVAL_S)
            run.poll_external()
            if run.needs_kill and self.hard_kill and run.task is not None and not run.task.done():
                run.needs_kill = False
                run.task.cancel()
