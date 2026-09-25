# Changelog

## 0.1.0 (2026-09-25)

First release.

- `Guard.run()` / `Guard.arun()` for sync and async agents
- Budget cap from LLM token usage (OpenAI-compatible, OpenAI Responses, Anthropic, Gemini), with a fail-safe fallback price for unknown models
- Request cap, rate limit (wait or stop), time limit with hard kill
- Loop detection for repeated identical requests or actions
- Endpoint block/allow lists and action filters via `checkpoint()`
- Emergency stop via `guard.stop()` or a kill file
- Run report with cost, tokens, models, hosts and events
- Intercepts `httpx`, `httpx2` (newer OpenAI SDKs) and `requests`
