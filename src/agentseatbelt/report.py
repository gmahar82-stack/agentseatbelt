"""The run report: what the agent did, what it cost and why it stopped."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field

MAX_EVENTS = 200


@dataclass
class Event:
    t: float  # seconds since the run started
    kind: str  # request | llm | action | blocked | wait | stop | note
    detail: str


def _money(usd: float) -> str:
    return f"${usd:,.2f}" if usd >= 1 else f"${usd:.4f}"


@dataclass
class RunReport:
    status: str = "running"  # completed | stopped | error
    stop_reason: str | None = None  # e.g. "budget_exceeded"; see agentseatbelt.errors
    message: str = ""
    duration_s: float = 0.0
    cost_usd: float = 0.0
    estimated_cost_calls: int = 0  # LLM calls priced with the fallback price (unknown model)
    untracked_streams: int = 0  # streamed LLM responses, which aren't costed automatically
    requests: int = 0
    llm_calls: int = 0
    actions: int = 0
    blocked: int = 0
    rate_limit_waits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    hosts: Counter = field(default_factory=Counter)
    models: Counter = field(default_factory=Counter)
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    error: str | None = None

    @property
    def stopped(self) -> bool:
        return self.status == "stopped"

    def summary(self) -> str:
        head = self.status.upper() + (f" ({self.stop_reason})" if self.stop_reason else "")
        lines = [f"AgentSeatbelt report: {head}"]
        if self.message:
            lines.append(f"  Reason:    {self.message}")
        if self.error:
            lines.append(f"  Error:     {self.error}")
        lines.append(f"  Duration:  {self.duration_s:.1f}s")
        cost = _money(self.cost_usd)
        if self.estimated_cost_calls:
            cost += f" (includes {self.estimated_cost_calls} call(s) at the fallback price for unknown models)"
        lines.append(f"  Cost:      {cost}")
        if self.untracked_streams:
            lines.append(f"  Warning:   {self.untracked_streams} streamed LLM response(s) not costed")
        parts = [f"LLM calls: {self.llm_calls}", f"blocked: {self.blocked}"]
        if self.actions:
            parts.append(f"actions: {self.actions}")
        if self.rate_limit_waits:
            parts.append(f"rate-limit waits: {self.rate_limit_waits}")
        lines.append(f"  Requests:  {self.requests} ({', '.join(parts)})")
        if self.input_tokens or self.output_tokens:
            lines.append(f"  Tokens:    {self.input_tokens:,} in / {self.output_tokens:,} out")
        if self.models:
            lines.append("  Models:    " + ", ".join(f"{m} ({n})" for m, n in self.models.most_common(5)))
        if self.hosts:
            lines.append("  Top hosts: " + ", ".join(f"{h} ({n})" for h, n in self.hosts.most_common(5)))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "stop_reason": self.stop_reason,
            "message": self.message,
            "duration_s": round(self.duration_s, 3),
            "cost_usd": round(self.cost_usd, 6),
            "estimated_cost_calls": self.estimated_cost_calls,
            "untracked_streams": self.untracked_streams,
            "requests": self.requests,
            "llm_calls": self.llm_calls,
            "actions": self.actions,
            "blocked": self.blocked,
            "rate_limit_waits": self.rate_limit_waits,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "hosts": dict(self.hosts),
            "models": dict(self.models),
            "events": [{"t": round(e.t, 3), "kind": e.kind, "detail": e.detail} for e in self.events],
            "error": self.error,
        }

    def __str__(self) -> str:
        return self.summary()
