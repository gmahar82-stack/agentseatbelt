"""Exceptions raised by AgentSeatbelt."""


class GuardStop(BaseException):
    """Raised inside the agent when AgentSeatbelt stops the run.

    Derives from BaseException (like KeyboardInterrupt) so agent code with a broad
    ``except Exception:`` can't swallow it and keep going.
    """

    reason = "stopped"

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message or self.__class__.__doc__.strip().splitlines()[0]


class BudgetExceeded(GuardStop):
    """The run reached its cost budget."""

    reason = "budget_exceeded"


class RequestLimitExceeded(GuardStop):
    """The run reached its maximum number of requests."""

    reason = "request_limit"


class RateLimitExceeded(GuardStop):
    """The run exceeded its requests-per-minute limit."""

    reason = "rate_limit"


class TimeLimitExceeded(GuardStop):
    """The run reached its time limit."""

    reason = "time_limit"


class LoopDetected(GuardStop):
    """The agent repeated the same request or action too many times."""

    reason = "loop_detected"


class EndpointBlocked(GuardStop):
    """The agent tried to reach a blocked endpoint or action."""

    reason = "endpoint_blocked"


class EmergencyStop(GuardStop):
    """The run was stopped manually."""

    reason = "emergency_stop"


class BlockedRequest(Exception):
    """A single request or action was blocked by the filter; the run itself continues.

    A normal Exception, so the agent can catch it and try something else.
    Pass ``stop_on_blocked=True`` to stop the whole run instead.
    """
