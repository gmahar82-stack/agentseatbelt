"""AgentGuard: a seatbelt for AI agents.

Budget caps, rate limits, time limits, loop detection, endpoint filters, an emergency stop and a
cost report, for any agent that talks to its LLM and tools over HTTP (httpx or requests).
"""

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
from .guard import Guard, GuardResult
from .pricing import DEFAULT_PRICES
from .report import Event, RunReport

__version__ = "0.1.0"

__all__ = [
    "Guard",
    "GuardResult",
    "RunReport",
    "Event",
    "DEFAULT_PRICES",
    "GuardStop",
    "BudgetExceeded",
    "RequestLimitExceeded",
    "RateLimitExceeded",
    "TimeLimitExceeded",
    "LoopDetected",
    "EndpointBlocked",
    "EmergencyStop",
    "BlockedRequest",
]
