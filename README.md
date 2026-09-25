# AgentGuard

**A seatbelt for AI agents.** Budget caps, rate limits, time limits, loop detection, endpoint filters, an emergency stop and a cost report, for any Python agent, with no changes to your agent code.

Agents go off the rails. They make 10,000 API calls when you expected 10, burn your API budget in an hour, loop forever, or call endpoints they shouldn't. AgentGuard wraps the run and stops it when it crosses a line you set.

```python
from agentguard import Guard

guard = Guard(
    max_budget_usd=5.00,
    max_requests=100,
    max_runtime_seconds=300,
    block_endpoints=["api.expensive-service.com"],
)

result = guard.run(my_agent_function, input_data)

print(result.output)            # whatever your agent returned (None if it was stopped)
print(result.report.summary())
```

```
AgentGuard report: STOPPED (budget_exceeded)
  Reason:    Budget of $5.00 reached ($5.0312 spent)
  Duration:  41.7s
  Cost:      $5.03
  Requests:  87 (LLM calls: 80, blocked: 1)
  Tokens:    1,203,400 in / 82,110 out
  Models:    gpt-4o (80)
  Top hosts: api.openai.com (80), api.search.example (7)
```

## Install

```bash
pip install agentguard
```

No dependencies. Works on Python 3.10+.

## How it works

AgentGuard hooks the HTTP libraries almost every Python agent uses underneath: **httpx** and its successor **httpx2** (used by the OpenAI, Anthropic, Groq, Mistral and most other SDKs; newer OpenAI SDKs use httpx2) and **requests**. Every call made inside `guard.run(...)` passes through the guard first. That means it works with **any framework**, including LangChain, CrewAI, LlamaIndex, the raw SDKs or your own code, without wrappers or config.

Outside a guarded run, nothing changes.

## Features

| Limit | Option | What happens |
|---|---|---|
| Budget cap | `max_budget_usd=5.0` | Token usage is read from each LLM response and priced. The run stops when the budget is reached. |
| Request cap | `max_requests=100` | Stops after N HTTP requests. |
| Rate limit | `max_requests_per_minute=60` | Throttles by default (`on_rate_limit="wait"`), or stops (`"stop"`). |
| Time limit | `max_runtime_seconds=300` | Stops even an agent stuck in a pure-Python loop (see *Hard kill*). |
| Loop detection | `loop_threshold=5` (default) | Stops when the identical request or action repeats 5 times within the last 20. |
| Endpoint filter | `block_endpoints=[...]`, `allow_endpoints=[...]` | Blocked calls never leave your machine. |
| Action filter | `block_actions=["delete_*"]` | For tool calls reported via `guard.checkpoint(...)`. |
| Emergency stop | `guard.stop()` or `kill_file="STOP"` | Stop from another thread, or by creating a file. |
| Cost report | `result.report` | Cost, tokens, models, hosts, the reason it stopped, and the last 200 events. |

### Stops can't be swallowed
When a limit is hit, AgentGuard raises a `GuardStop` inside the agent. It derives from `BaseException` (like `KeyboardInterrupt`), so an agent with a careless `except Exception: retry` can't catch it and keep going. After a stop, every further request is refused.

### Hard kill
Time limits, emergency stops and budget overruns also interrupt the agent's thread directly, so an agent spinning in a local loop (no HTTP calls) is stopped too. Code stuck inside a single blocking C call (e.g. a socket read with no timeout) stops as soon as that call returns. Async agents are cancelled. Disable with `hard_kill=False`.

### Endpoint patterns
- `"api.example.com"`: that host and its subdomains
- `"api.example.com/v1/admin"`: host plus path prefix
- `"https://api.example.com/v1/"`: full URL prefix
- `"*.example.com"`, `"*/admin/*"`: globs

A blocked call raises `BlockedRequest` (a normal `Exception`, so the agent can recover and try something else). Pass `stop_on_blocked=True` to end the run instead.

## Cost tracking

Usage is read from OpenAI-compatible APIs (OpenAI, Groq, Together, OpenRouter and others), the OpenAI Responses API, Anthropic and Gemini. If a gateway reports the real cost (OpenRouter's `usage.cost`), that is used.

Prices are **your responsibility to verify**. A small default table ships in `agentguard.DEFAULT_PRICES`, but prices change, so override them:

```python
Guard(max_budget_usd=5, pricing={"gpt-4o": (2.50, 10.00), "my-model": (0.20, 0.80)})  # USD per 1M tokens (in, out)
```

Unknown models use a deliberately high `fallback_price` (default $10 in / $30 out per 1M tokens), so a budget **overestimates rather than underestimates**. The report shows how many calls used it.

For costs the guard can't see, add them yourself: `guard.add_cost(0.02, "image generation")`.

## Tool calls and non-HTTP steps

```python
guard = Guard(block_actions=["delete_*", "send_email"])

def my_tool(name, **args):
    guard.checkpoint(name, **args)   # applies stops, the action filter and loop detection
    ...
```

## Async agents

```python
result = await guard.arun(my_async_agent, input_data)
```

## Live monitoring

```python
Guard(verbose=True)                              # print every event to stderr
Guard(on_event=lambda e: send_to_dashboard(e))   # or handle events yourself
```

## Known limits (v0.1)

- **Streamed LLM responses aren't costed automatically.** They're counted in `report.untracked_streams`. Use non-streaming calls, or `guard.add_cost(...)`.
- Only `httpx`, `httpx2` and `requests` are intercepted. `aiohttp`, `urllib` and subprocesses aren't seen.
- The budget is checked **before** each call, so the call that crosses the limit still completes. The overshoot is at most one call.
- Worker threads started by the agent are attributed to the run when exactly one guarded run is active in the process.
- Budgets are based on token counts and your price table. They're an estimate, not your provider's invoice.

## License

MIT
