# Changelog

## 0.2.0 (unreleased)

x402 payments: for agents that pay for APIs with USDC.

- x402 payments (v1 and v2) are read from the signed payment before it's sent, and count toward `max_budget_usd`. A payment that would cross the budget is never sent. Note: budgets now include these payments, not only LLM costs.
- `max_payment_usd`: cap on any single payment.
- `pay_safe=True` (opt-in): each payment is checked by the Pay Safe service (service working? fair price? safe, usual wallet?) and blocked on STOP. Options: `pay_safe_block`, `pay_safe_fail_closed`, `pay_safe_timeout`, `pay_safe_url`. Only the URL and payment terms are sent, never the signature.
- New `PaymentBlocked` exception (a `BlockedRequest`), and payment fields in the report (`payments`, `payments_usd`, `payments_blocked`, `unvalued_payments`, `pay_safe_checks`).
- Payments are also caught when an x402 client resends the paid request inside its own transport or adapter (e.g. Coinbase's `x402` package), without double counting.

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
