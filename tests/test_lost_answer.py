"""Lost answers: a signed payment that got no answer may still have been settled by the seller.

The guard must resend the SAME signed payment (it can be settled at most once), never let a new one through
for the same request, and keep counting the amount against the budget until it's resolved.
"""

import asyncio
import base64
import json
import socket
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


def terms():
    return {"scheme": "exact", "network": "eip155:8453", "amount": "10000", "asset": USDC, "payTo": SELLER, "maxTimeoutSeconds": 60}


def signed(sig: str) -> dict:
    return {"PAYMENT-SIGNATURE": b64({"x402Version": 2, "accepted": terms(), "payload": {"signature": sig}})}


class FlakySeller:
    """MockTransport handler: settles each signed payment once, and loses the first `drops` answers."""

    def __init__(self, drops: int, drop_status: int | None = None, resend_status: int | None = None):
        self.drops, self.drop_status, self.resend_status = drops, drop_status, resend_status
        self.settled: set[str] = set()
        self.sends: list[str] = []

    def __call__(self, request: httpx.Request):
        sig = request.headers.get("payment-signature")
        if not sig:
            quote = {"x402Version": 2, "resource": {"url": str(request.url)}, "accepts": [terms()]}
            return httpx.Response(402, headers={"PAYMENT-REQUIRED": b64(quote)}, json={})
        self.sends.append(sig)
        again = sig in self.settled
        self.settled.add(sig)  # charged now, whatever happens to the answer
        if len(self.sends) <= self.drops:
            if self.drop_status:
                return httpx.Response(self.drop_status, text="gateway timeout")
            raise httpx.ReadError("connection reset", request=request)
        if again and self.resend_status:
            return httpx.Response(self.resend_status, json={"error": "already used"})
        return httpx.Response(200, headers={"PAYMENT-RESPONSE": b64({"success": True})}, json={"data": 1})


def guard(**kw) -> Guard:
    return Guard(loop_threshold=None, lost_answer_retry_delay=0, **kw)


def test_lost_answer_is_resent_with_the_same_signature_and_counted_once():
    seller = FlakySeller(drops=1)
    client = httpx.Client(transport=httpx.MockTransport(seller))
    result = guard(max_budget_usd=1.0).run(lambda: client.get(URL, headers=signed("0xONE")).status_code)
    r = result.report
    assert result.output == 200
    assert seller.sends == [seller.sends[0]] * 2 and len(seller.settled) == 1
    assert r.payments == 1 and r.payments_usd == pytest.approx(0.01)
    assert r.unconfirmed_usd == 0 and r.unconfirmed == []
    assert any(e.kind == "resend" for e in r.events)


def test_no_answer_keeps_the_payment_counted_and_blocks_a_new_signature():
    seller = FlakySeller(drops=100)
    client = httpx.Client(transport=httpx.MockTransport(seller))
    g = guard(max_budget_usd=0.015, lost_answer_retries=1)

    def agent():
        out = {}
        with pytest.raises(httpx.ReadError):
            client.get(URL, headers=signed("0xFIRST"))
        out["sends"] = len(seller.sends)  # first send + one resend of the same signature
        with pytest.raises(PaymentBlocked, match="no answer"):
            client.get(URL, headers=signed("0xSECOND"))  # could pay twice
        with pytest.raises(httpx.ReadError):
            client.get(URL, headers=signed("0xFIRST"))  # resending the same one is allowed
        out["resolved"] = g.resolve_unconfirmed(URL, charged=False)
        out["again"] = g.resolve_unconfirmed(URL, charged=False)
        return out

    result = g.run(agent)
    assert result.output["sends"] == 2
    assert result.output["resolved"] is True and result.output["again"] is False
    assert len(seller.settled) == 1  # the new signature never left
    assert result.report.unconfirmed_usd == pytest.approx(0) and result.report.payments == 0


def test_unconfirmed_payment_counts_against_the_budget():
    seller = FlakySeller(drops=1)
    client = httpx.Client(transport=httpx.MockTransport(seller))

    def agent():
        with pytest.raises(httpx.ReadError):
            client.get(URL, headers=signed("0xFIRST"))
        client.get("https://other.example.com/x", headers=signed("0xOTHER"))  # $0.01 unconfirmed + $0.01 > $0.015

    result = guard(max_budget_usd=0.015, lost_answer_retries=0).run(agent)
    assert result.report.stop_reason == "budget_exceeded"
    assert len(seller.sends) == 1  # the second payment never left


def test_unresolved_payment_shows_in_the_report():
    seller = FlakySeller(drops=100)
    client = httpx.Client(transport=httpx.MockTransport(seller))

    def agent():
        try:
            client.get(URL, headers=signed("0xONE"))
        except httpx.ReadError:
            pass

    r = guard(lost_answer_retries=0).run(agent).report
    assert len(seller.sends) == 1
    assert r.unconfirmed_usd == pytest.approx(0.01)
    assert r.unconfirmed[0]["url"] == URL and r.unconfirmed[0]["amount_usd"] == pytest.approx(0.01)
    assert "got no answer" in r.summary()
    assert r.to_dict()["unconfirmed_usd"] == pytest.approx(0.01)


def test_resolve_charged_counts_it_as_paid():
    seller = FlakySeller(drops=100)
    client = httpx.Client(transport=httpx.MockTransport(seller))
    g = guard(lost_answer_retries=0)

    def agent():
        with pytest.raises(httpx.ReadError):
            client.get(URL, headers=signed("0xONE"))
        return g.resolve_unconfirmed(URL, charged=True)

    result = g.run(agent)
    assert result.output is True
    assert result.report.payments == 1 and result.report.payments_usd == pytest.approx(0.01)
    assert result.report.unconfirmed_usd == pytest.approx(0)


def test_gateway_timeout_then_already_used_stays_unconfirmed():
    seller = FlakySeller(drops=1, drop_status=504, resend_status=402)
    client = httpx.Client(transport=httpx.MockTransport(seller))
    result = guard().run(lambda: client.get(URL, headers=signed("0xONE")).status_code)
    r = result.report
    assert result.output == 402 and len(seller.sends) == 2
    assert r.unconfirmed_usd == pytest.approx(0.01) and r.payments == 0
    assert not any("not charged" in e.detail for e in r.events)


def test_plain_server_error_is_not_charged_and_not_resent():
    seller = FlakySeller(drops=1, drop_status=500)
    client = httpx.Client(transport=httpx.MockTransport(seller))
    r = guard().run(lambda: client.get(URL, headers=signed("0xONE"))).report
    assert len(seller.sends) == 1 and r.unconfirmed_usd == 0
    assert any("not charged (HTTP 500)" in e.detail for e in r.events)


def test_block_can_be_turned_off():
    seller = FlakySeller(drops=1)
    client = httpx.Client(transport=httpx.MockTransport(seller))

    def agent():
        with pytest.raises(httpx.ReadError):
            client.get(URL, headers=signed("0xA"))
        return client.get(URL, headers=signed("0xB")).status_code

    result = guard(lost_answer_retries=0, block_new_payment_while_unconfirmed=False).run(agent)
    assert result.output == 200
    assert result.report.payments_usd == pytest.approx(0.01) and result.report.unconfirmed_usd == pytest.approx(0.01)


def test_async_httpx_lost_answer_is_resent():
    seller = FlakySeller(drops=1)

    async def agent():
        async with httpx.AsyncClient(transport=httpx.MockTransport(seller)) as client:
            return (await client.get(URL, headers=signed("0xONE"))).status_code

    result = asyncio.run(guard().arun(agent))
    assert result.output == 200 and len(seller.sends) == 2 and len(seller.settled) == 1
    assert result.report.payments == 1


# ---- a real local server that cuts the connection after reading the paid request ------------------------


@pytest.fixture
def dropping_seller():
    state = {"signatures": [], "drops": 1}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            sig = self.headers.get("payment-signature")
            if sig:
                state["signatures"].append(sig)
                if len(state["signatures"]) <= state["drops"]:
                    self.close_connection = True  # settled, but the answer is lost
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                body = b'{"data": 1}'
                self.send_response(200)
                self.send_header("PAYMENT-RESPONSE", b64({"success": True, "transaction": "0x", "network": "eip155:8453"}))
            else:
                url = f"http://{self.headers['host']}{self.path}"
                accepts = [{**terms(), "extra": {"name": "USD Coin", "version": "2"}}]
                self.send_response(402)
                self.send_header("PAYMENT-REQUIRED", b64({"x402Version": 2, "resource": {"url": url}, "accepts": accepts}))
                body = b"{}"
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/paid"
    yield state
    server.shutdown()


def test_requests_dropped_connection_is_resent(dropping_seller):
    result = guard().run(lambda: requests.Session().get(dropping_seller["url"], headers=signed("0xONE")).status_code)
    sigs = dropping_seller["signatures"]
    assert result.output == 200 and len(sigs) == 2 and sigs[0] == sigs[1]
    assert result.report.payments == 1 and result.report.unconfirmed_usd == 0


def test_official_requests_client_dropped_connection_pays_once(dropping_seller):
    pytest.importorskip("x402")
    eth_account = pytest.importorskip("eth_account")
    from x402 import x402ClientSync
    from x402.http.clients import wrapRequestsWithPayment
    from x402.mechanisms.evm.exact import register_exact_evm_client
    from x402.mechanisms.evm.signers import EthAccountSigner

    client = register_exact_evm_client(x402ClientSync(), EthAccountSigner(eth_account.Account.create()))
    session = wrapRequestsWithPayment(requests.Session(), client)
    result = guard(max_budget_usd=1.0).run(lambda: session.get(dropping_seller["url"]).status_code)
    sigs = dropping_seller["signatures"]
    assert result.output == 200
    assert len(sigs) == 2 and sigs[0] == sigs[1]  # never a second, different payment
    assert result.report.payments == 1 and result.report.payments_usd == pytest.approx(0.01)


def test_official_async_httpx_client_dropped_connection_pays_once(dropping_seller):
    pytest.importorskip("x402")
    eth_account = pytest.importorskip("eth_account")
    from x402 import x402Client
    from x402.http.clients import wrapHttpxWithPayment
    from x402.mechanisms.evm.exact import register_exact_evm_client
    from x402.mechanisms.evm.signers import EthAccountSigner

    client = register_exact_evm_client(x402Client(), EthAccountSigner(eth_account.Account.create()))

    async def agent():
        async with wrapHttpxWithPayment(client) as http:
            return (await http.get(dropping_seller["url"])).status_code

    result = asyncio.run(guard(max_budget_usd=1.0).arun(agent))
    sigs = dropping_seller["signatures"]
    assert result.output == 200
    assert len(sigs) == 2 and sigs[0] == sigs[1]
    assert result.report.payments == 1 and result.report.payments_usd == pytest.approx(0.01)
