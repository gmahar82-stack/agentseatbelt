"""x402 payments: read the signed payment an agent is about to send, before it leaves the machine.

An x402 client answers a ``402 Payment Required`` by resending the request with a signed payment in the
``PAYMENT-SIGNATURE`` header (x402 v2) or ``X-PAYMENT`` (v1). The header is base64 JSON that says how much
is paid, in which asset, on which network and to which wallet. AgentSeatbelt reads it to count the payment
toward the budget, cap single payments and, if enabled, ask Pay Safe whether the payment looks safe.
Blocking the request means the signed payment is never delivered, so nothing is paid.
"""

from __future__ import annotations

import base64
import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

PAYMENT_HEADERS = ("payment-signature", "x-payment")
RESPONSE_HEADERS = ("payment-response", "x-payment-response")
PAY_SAFE_URL = "https://agent-deals.gm-tools.workers.dev/v1/preflight"

# USDC contract addresses (6 decimals) by network. Payments in other assets can't be valued in dollars.
USDC = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # Base
    "0x036cbd53842c5426634e7929541ec2318f3dcf7e",  # Base Sepolia
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # Ethereum
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # Polygon
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # Arbitrum
    "0x0b2c639c533813f4aa9d7837caf62653d097ff85",  # Optimism
    "epjfwdd5aufqssqem2qn1xzybapc8g4wegkzwytdt1v",  # Solana (lowercased)
}


@dataclass
class Payment:
    """A payment found in an outgoing request."""

    version: int
    network: str
    pay_to: str | None
    asset: str | None
    raw_amount: int | None
    requirements: dict | None  # the payment terms the client accepted (v2), or rebuilt from the 402 quote

    @property
    def amount_usd(self) -> float | None:
        """Dollar value for USDC payments; None when the asset or amount is unknown."""
        if self.raw_amount is None or not self.asset or self.asset.lower() not in USDC:
            return None
        return self.raw_amount / 1_000_000

    def describe(self) -> str:
        usd = self.amount_usd
        amount = f"${usd:.6g}" if usd is not None else f"{self.raw_amount} units of {self.asset or 'an unknown asset'}"
        return f"{amount} to {self.pay_to or '?'} on {self.network or '?'}"


def _decode(value: str) -> Any:
    value = value.strip()
    try:
        return json.loads(base64.b64decode(value + "=" * (-len(value) % 4)))
    except (ValueError, UnicodeDecodeError):
        try:
            return json.loads(value)
        except ValueError:
            return None


def _int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def parse_quote(headers: Mapping[str, str], body: Any) -> list[dict]:
    """The payment options in a 402 answer (PAYMENT-REQUIRED header, or a v1 JSON body)."""
    raw = headers.get("payment-required")
    quote = _decode(raw) if raw else body
    accepts = quote.get("accepts") if isinstance(quote, dict) else None
    return [a for a in accepts if isinstance(a, dict)] if isinstance(accepts, list) else []


def parse_payment(headers: Mapping[str, str], quotes: list[dict] | None = None) -> Payment | None:
    """The payment in an outgoing request's headers, or None if it carries no x402 payment.

    ``quotes`` are the options from the 402 answer this request responds to; v1 payments don't name
    their asset, so it's looked up there.
    """
    raw = next((headers.get(h) for h in PAYMENT_HEADERS if headers.get(h)), None)
    if not raw:
        return None
    data = _decode(raw)
    if not isinstance(data, dict):
        return Payment(0, "", None, None, None, None)  # unreadable: treated as a payment of unknown value
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    auth = payload.get("authorization") if isinstance(payload.get("authorization"), dict) else {}
    accepted = data.get("accepted") if isinstance(data.get("accepted"), dict) else None
    if accepted:  # x402 v2: the accepted terms travel with the payment
        return Payment(
            2,
            str(accepted.get("network", "")),
            accepted.get("payTo") or auth.get("to"),
            accepted.get("asset"),
            _int(accepted.get("amount")),
            accepted,
        )
    # x402 v1: amount and recipient are in the signed authorization; the asset comes from the 402 quote.
    pay_to, raw_amount, network = auth.get("to"), _int(auth.get("value")), str(data.get("network", ""))
    match = next(
        (
            q
            for q in quotes or []
            if str(q.get("payTo", "")).lower() == str(pay_to or "").lower() and str(q.get("network", "")) == network
        ),
        None,
    )
    requirements = None
    if match:
        requirements = {**match, "amount": str(raw_amount) if raw_amount is not None else match.get("maxAmountRequired")}
    return Payment(1, network, pay_to, match.get("asset") if match else None, raw_amount, requirements)


def has_payment(headers: Mapping[str, str]) -> bool:
    return any(headers.get(h) for h in PAYMENT_HEADERS)


def settled(headers: Mapping[str, str]) -> bool:
    return any(headers.get(h) for h in RESPONSE_HEADERS)


def pay_safe(url: str, method: str, payment: Payment, endpoint: str, timeout: float) -> dict:
    """Ask the Pay Safe service about a payment. Sends only the service URL and the payment terms
    (amount, asset, network, pay-to wallet), never the signature. Uses urllib, which AgentSeatbelt
    doesn't intercept, so this check isn't counted as one of the agent's requests.

    Returns the service's answer ({"verdict": "go" | "caution" | "stop", "summary": ..., ...}).
    Raises OSError / ValueError if the service can't be reached or answers badly.
    """
    terms = payment.requirements or {
        "scheme": "exact",
        "network": payment.network,
        "amount": str(payment.raw_amount or 0),
        "asset": payment.asset or "",
        "payTo": payment.pay_to or "",
    }
    body = {
        "url": url,
        "method": method,
        "payment_required": {"x402Version": 2, "resource": {"url": url}, "accepts": [terms]},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "user-agent": "agentseatbelt"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        answer = json.loads(response.read())
    if not isinstance(answer, dict) or answer.get("verdict") not in ("go", "caution", "stop"):
        raise ValueError("unexpected answer from Pay Safe")
    return answer
