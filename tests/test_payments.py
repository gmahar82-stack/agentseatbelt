import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import requests

from agentseatbelt import Guard, PaymentBlocked

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SELLER = "0x1111111111111111111111111111111111111111"
URL = "https://paid.example.com/v1/data"


def b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def terms(amount=10_000, asset=USDC, pay_to=SELLER):  # 10_000 = $0.01 in USDC units
    return {"scheme": "exact", "network": "eip155:8453", "amount": str(amount), "asset": asset, "payTo": pay_to, "maxTimeoutSeconds": 60}


class PaidApi:
    """httpx MockTransport handler for an x402 v2 service: 402 without payment, 200 with one."""

    def __init__(self, amount=10_000, asset=USDC, paid_status=200):
        self.amount, self.asset, self.paid_status = amount, asset, paid_status
        self.paid_requests = []

    def __call__(self, request: httpx.Request):
        if not request.headers.get("payment-signature"):
            quote = {"x402Version": 2, "resource": {"url": str(request.url)}, "accepts": [terms(self.amount, self.asset)]}
            return httpx.Response(402, headers={"PAYMENT-REQUIRED": b64(quote)}, json={})
        self.paid_requests.append(request)
        if self.paid_status != 200:
            return httpx.Response(self.paid_status, json={"error": "failed"})
        return httpx.Response(200, headers={"PAYMENT-RESPONSE": b64({"success": True})}, json={"data": 1})


def pay(client, api_amount=10_000, asset=USDC):
    """What an x402 client does: ask, read the price, resend with a signed payment."""
    r = client.get(URL)
    assert r.status_code == 402
    accepted = json.loads(base64.b64decode(r.headers["payment-required"]))["accepts"][0]
    payment = {"x402Version": 2, "accepted": accepted, "payload": {"signature": "0xSIGNED", "authorization": {"to": accepted["payTo"]}}}
    return client.get(URL, headers={"PAYMENT-SIGNATURE": b64(payment)})


def test_payments_count_toward_budget_and_report():
    api = PaidApi()
    client = httpx.Client(transport=httpx.MockTransport(api))
    result = Guard(max_budget_usd=1.0, loop_threshold=None).run(lambda: [pay(client) for _ in range(3)])
    r = result.report
    assert not result.stopped
    assert r.payments == 3 and r.payments_usd == pytest.approx(0.03) and r.cost_usd == pytest.approx(0.03)
    assert "Payments:  3 x402 ($0.0300)" in r.summary()
    assert r.to_dict()["payments_usd"] == pytest.approx(0.03)


def test_payment_that_would_cross_the_budget_is_never_sent():
    api = PaidApi()
    client = httpx.Client(transport=httpx.MockTransport(api))
    result = Guard(max_budget_usd=0.025, loop_threshold=None).run(lambda: [pay(client) for _ in range(5)])
    assert result.report.stop_reason == "budget_exceeded"
    assert len(api.paid_requests) == 2  # the 3rd payment ($0.03 total) would cross $0.025, so it never leaves
    assert result.report.payments_usd == pytest.approx(0.02)


def test_max_payment_blocks_expensive_payment_before_it_is_sent():
    api = PaidApi(amount=50_000)  # $0.05
    client = httpx.Client(transport=httpx.MockTransport(api))
    caught = {}

    def agent():
        try:
            pay(client)
        except PaymentBlocked as e:
            caught["e"] = e
        return "recovered"

    result = Guard(max_payment_usd=0.01).run(agent)
    assert result.output == "recovered" and not result.stopped  # a normal Exception: the agent can recover
    assert "max_payment_usd" in str(caught["e"])
    assert api.paid_requests == []
    assert result.report.payments == 0 and result.report.payments_blocked == 1


def test_unknown_asset_is_blocked_when_a_payment_cap_is_set():
    api = PaidApi(asset="0x9999999999999999999999999999999999999999")
    client = httpx.Client(transport=httpx.MockTransport(api))
    result = Guard(max_payment_usd=1.0, stop_on_blocked=True).run(lambda: pay(client))
    assert result.report.stop_reason == "endpoint_blocked" and api.paid_requests == []


def test_unknown_asset_without_cap_is_counted_but_not_costed():
    api = PaidApi(asset="0x9999999999999999999999999999999999999999")
    client = httpx.Client(transport=httpx.MockTransport(api))
    r = Guard().run(lambda: pay(client)).report
    assert r.payments == 1 and r.unvalued_payments == 1 and r.cost_usd == 0


def test_failed_paid_call_is_not_counted():
    api = PaidApi(paid_status=500)
    client = httpx.Client(transport=httpx.MockTransport(api))
    r = Guard().run(lambda: pay(client)).report
    assert r.payments == 0 and r.cost_usd == 0
    assert any(e.kind == "payment" and "not charged" in e.detail for e in r.events)


def test_x402_v1_payment_is_valued_from_the_402_body():
    def handler(request: httpx.Request):
        if not request.headers.get("x-payment"):
            accepts = [{"scheme": "exact", "network": "base", "maxAmountRequired": "20000", "asset": USDC, "payTo": SELLER, "resource": URL}]
            return httpx.Response(402, json={"x402Version": 1, "accepts": accepts})
        return httpx.Response(200, headers={"X-PAYMENT-RESPONSE": b64({"success": True})}, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def agent():
        client.get(URL)
        v1 = {"x402Version": 1, "scheme": "exact", "network": "base", "payload": {"signature": "0x", "authorization": {"to": SELLER, "value": "20000"}}}
        client.get(URL, headers={"X-PAYMENT": b64(v1)})

    r = Guard().run(agent).report
    assert r.payments == 1 and r.payments_usd == pytest.approx(0.02)


# ---- Pay Safe ------------------------------------------------------------------------------

class PaySafeStub:
    """A local Pay Safe endpoint (the guard calls it with urllib, like the real service)."""

    def __init__(self, verdict="go"):
        self.verdict, self.bodies = verdict, []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                stub.bodies.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
                answer = json.dumps({"verdict": stub.verdict, "summary": f"test says {stub.verdict}", "findings": []}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/preflight"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def pay_safe():
    stubs = []

    def make(verdict="go"):
        stubs.append(PaySafeStub(verdict))
        return stubs[-1]

    yield make
    for s in stubs:
        s.close()


def test_pay_safe_stop_blocks_the_payment(pay_safe):
    stub = pay_safe("stop")
    api = PaidApi()
    client = httpx.Client(transport=httpx.MockTransport(api))
    caught = {}

    def agent():
        try:
            pay(client)
        except PaymentBlocked as e:
            caught["e"] = e

    r = Guard(pay_safe=True, pay_safe_url=stub.url).run(agent).report
    assert api.paid_requests == [] and r.payments_blocked == 1 and r.pay_safe_checks == 1
    assert caught["e"].verdict["verdict"] == "stop"
    # Only the terms are sent to Pay Safe: never the signature.
    sent = stub.bodies[0]
    assert sent["url"] == URL and sent["payment_required"]["accepts"][0]["payTo"] == SELLER
    assert "SIGNED" not in json.dumps(sent)


def test_pay_safe_go_and_caution_pass_by_default_and_answers_are_cached(pay_safe):
    stub = pay_safe("caution")
    api = PaidApi()
    client = httpx.Client(transport=httpx.MockTransport(api))
    r = Guard(pay_safe=True, pay_safe_url=stub.url, loop_threshold=None).run(lambda: [pay(client) for _ in range(3)]).report
    assert r.payments == 3 and len(api.paid_requests) == 3
    assert len(stub.bodies) == 1  # the same payment isn't re-checked within a minute


def test_pay_safe_block_caution(pay_safe):
    stub = pay_safe("caution")
    api = PaidApi()
    client = httpx.Client(transport=httpx.MockTransport(api))
    r = Guard(pay_safe=True, pay_safe_url=stub.url, pay_safe_block="caution", stop_on_blocked=True).run(lambda: pay(client)).report
    assert r.stop_reason == "endpoint_blocked" and api.paid_requests == []


def test_pay_safe_unreachable_fails_open_by_default_and_closed_on_request():
    dead = "http://127.0.0.1:9/v1/preflight"  # nothing listens here
    api = PaidApi()
    client = httpx.Client(transport=httpx.MockTransport(api))
    r = Guard(pay_safe=True, pay_safe_url=dead, pay_safe_timeout=2).run(lambda: pay(client)).report
    assert r.payments == 1 and any(e.kind == "pay_safe" and "unavailable" in e.detail for e in r.events)

    api2 = PaidApi()
    client2 = httpx.Client(transport=httpx.MockTransport(api2))
    r2 = Guard(pay_safe=True, pay_safe_url=dead, pay_safe_timeout=2, pay_safe_fail_closed=True, stop_on_blocked=True).run(lambda: pay(client2)).report
    assert r2.stop_reason == "endpoint_blocked" and api2.paid_requests == []


def test_async_client_with_pay_safe(pay_safe):
    stub = pay_safe("stop")
    api = PaidApi()

    async def agent():
        async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
            r = await client.get(URL)
            accepted = json.loads(base64.b64decode(r.headers["payment-required"]))["accepts"][0]
            with pytest.raises(PaymentBlocked):
                await client.get(URL, headers={"PAYMENT-SIGNATURE": b64({"x402Version": 2, "accepted": accepted, "payload": {}})})

    r = asyncio.run(Guard(pay_safe=True, pay_safe_url=stub.url).arun(agent)).report
    assert api.paid_requests == [] and r.payments_blocked == 1


def test_requests_library_payments_are_capped():
    class Handler(BaseHTTPRequestHandler):
        paid = 0

        def do_GET(self):
            if self.headers.get("payment-signature"):
                Handler.paid += 1
                self.send_response(200)
                self.send_header("PAYMENT-RESPONSE", b64({"success": True}))
            else:
                self.send_response(402)
                self.send_header("PAYMENT-REQUIRED", b64({"x402Version": 2, "resource": {"url": "x"}, "accepts": [terms(2_000_000)]}))
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/paid"
    try:
        def agent():
            quote = json.loads(base64.b64decode(requests.get(url).headers["payment-required"]))
            with pytest.raises(PaymentBlocked):
                requests.get(url, headers={"PAYMENT-SIGNATURE": b64({"x402Version": 2, "accepted": quote["accepts"][0], "payload": {}})})

        r = Guard(max_payment_usd=1.0).run(agent).report  # asks $2
        assert Handler.paid == 0 and r.payments_blocked == 1
    finally:
        server.shutdown()


def test_no_guard_means_payments_pass_untouched():
    api = PaidApi(amount=50_000_000)  # $50, but no guard is running
    client = httpx.Client(transport=httpx.MockTransport(api))
    assert pay(client).status_code == 200 and len(api.paid_requests) == 1
