"""Token usage extraction and cost estimation for common LLM APIs."""

from __future__ import annotations

import re
from dataclasses import dataclass

# USD per 1M tokens: (input, output). Approximate list prices, which change often.
# Always verify for your provider and override with Guard(pricing={...}).
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o3": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
}

# Used for models with no known price. Deliberately high so budgets fail safe (overestimate, never under).
FALLBACK_PRICE: tuple[float, float] = (10.00, 30.00)

_GEMINI_MODEL_IN_URL = re.compile(r"/models/([^/:]+)")


@dataclass
class Usage:
    model: str | None
    input_tokens: int
    output_tokens: int
    reported_cost: float | None = None  # some gateways (e.g. OpenRouter) return the real cost


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def extract_usage(response: object, request: object = None, url: str = "") -> Usage | None:
    """Return token usage from an LLM API response body, or None if it isn't one we recognise.

    Supports OpenAI-compatible APIs (OpenAI, Groq, Together, OpenRouter, ...), OpenAI's Responses
    API, Anthropic and Gemini.
    """
    if not isinstance(response, dict):
        return None
    req = request if isinstance(request, dict) else {}
    model = response.get("model") or response.get("modelVersion") or req.get("model")
    if not model:
        m = _GEMINI_MODEL_IN_URL.search(url)
        model = m.group(1) if m else None

    usage = response.get("usage")
    if isinstance(usage, dict):
        has_in = "prompt_tokens" in usage or "input_tokens" in usage
        has_out = "completion_tokens" in usage or "output_tokens" in usage
        if not (has_in or has_out):
            return None
        input_tokens = (
            _int(usage.get("prompt_tokens", usage.get("input_tokens")))
            # Anthropic reports cached tokens separately. Counting them at full price overestimates, which is the safe side.
            + _int(usage.get("cache_creation_input_tokens"))
            + _int(usage.get("cache_read_input_tokens"))
        )
        output_tokens = _int(usage.get("completion_tokens", usage.get("output_tokens")))
        cost = usage.get("cost")
        reported = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
        return Usage(model, input_tokens, output_tokens, reported)

    meta = response.get("usageMetadata")
    if isinstance(meta, dict):
        return Usage(
            model,
            _int(meta.get("promptTokenCount")),
            _int(meta.get("candidatesTokenCount")) + _int(meta.get("thoughtsTokenCount")),
        )
    return None


def lookup_price(model: str | None, prices: dict[str, tuple[float, float]]) -> tuple[float, float] | None:
    """Find a model's price: exact name first, then the longest matching prefix
    (so "gpt-4o-mini-2024-07-18" uses "gpt-4o-mini", not "gpt-4o")."""
    if not model:
        return None
    name = model.lower().strip()
    candidates = [name]
    if "/" in name:  # "openai/gpt-4o" or "models/gemini-2.0-flash"
        candidates.append(name.rsplit("/", 1)[1])
    for candidate in candidates:
        if candidate in prices:
            return prices[candidate]
    for candidate in candidates:
        matches = [key for key in prices if candidate.startswith(key)]
        if matches:
            return prices[max(matches, key=len)]
    return None


def estimate_cost(
    usage: Usage,
    prices: dict[str, tuple[float, float]],
    fallback: tuple[float, float] = FALLBACK_PRICE,
) -> tuple[float, bool]:
    """Return (cost_usd, estimated). ``estimated`` is True when the fallback price was used."""
    if usage.reported_cost is not None:
        return usage.reported_cost, False
    price = lookup_price(usage.model, prices)
    estimated = price is None
    in_price, out_price = price or fallback
    cost = (usage.input_tokens * in_price + usage.output_tokens * out_price) / 1_000_000
    return cost, estimated
