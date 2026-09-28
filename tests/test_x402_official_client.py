"""AgentSeatbelt with Coinbase's official x402 Python client, which resends the paid request inside its own
httpx transport / requests adapter. Skipped unless `x402[evm,clients]` is installed.

The seller is a local test server and the signing key is a fresh random one with no funds: nothing is paid.
"""

import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("x402")
eth_account = pytest.importorskip("eth_account")

import requests  # noqa: E402
from x402 import x402Client, x402ClientSync  # noqa: E402
from x402.http.clients import wrapHttpxWithPayment, wrapRequestsWithPayment  # noqa: E402
from x402.mechanisms.evm.exact import register_exact_evm_client  # noqa: E402
from x402.mechanisms.evm.signers import EthAccountSigner  # noqa: E402

from agentseatbelt import Guard, PaymentBlocked  # noqa: E402

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SELLER = "0x1111111111111111111111111111111111111111"


def b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


@pytest.fixture
def seller():
    state = {"paid": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("payment-signature"):
                state["paid"] += 1
                body = b'{"data": 1}'
                self.send_response(200)
                self.send_header("PAYMENT-RESPONSE", b64({"success": True, "transaction": "0x", "network": "eip155:8453"}))
            else:
                url = f"http://{self.headers['host']}{self.path}"
                accepts = [{"scheme": "exact", "network": "eip155:8453", "amount": "10000", "asset": USDC, "payTo": SELLER,
                            "maxTimeoutSeconds": 60, "extra": {"name": "USD Coin", "version": "2"}}]
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


def signer():
    return EthAccountSigner(eth_account.Account.create())  # random key, no funds


def blocked_by_seatbelt(exc: BaseException) -> bool:
    """The official client wraps errors in its own PaymentError; the seatbelt's block is the cause."""
    while exc is not None:
        if isinstance(exc, PaymentBlocked):
            return True
        exc = exc.__cause__
    return False


def test_official_async_httpx_client_payment_is_counted_once(seller):
    client = register_exact_evm_client(x402Client(), signer())

    async def agent():
        async with wrapHttpxWithPayment(client) as http:
            return (await http.get(seller["url"])).status_code

    result = asyncio.run(Guard(max_budget_usd=1.0).arun(agent))
    assert result.output == 200 and seller["paid"] == 1
    assert result.report.payments == 1 and result.report.payments_usd == pytest.approx(0.01)


def test_official_async_httpx_client_payment_cap(seller):
    client = register_exact_evm_client(x402Client(), signer())

    async def agent():
        async with wrapHttpxWithPayment(client) as http:
            try:
                await http.get(seller["url"])
            except Exception as e:
                return blocked_by_seatbelt(e)

    result = asyncio.run(Guard(max_payment_usd=0.001).arun(agent))
    assert result.output is True and seller["paid"] == 0 and result.report.payments_blocked == 1


def test_official_requests_client(seller):
    client = register_exact_evm_client(x402ClientSync(), signer())
    session = wrapRequestsWithPayment(requests.Session(), client)

    ok = Guard(max_budget_usd=1.0).run(lambda: session.get(seller["url"]).status_code)
    assert ok.output == 200 and seller["paid"] == 1 and ok.report.payments == 1

    def capped():
        try:
            session.get(seller["url"])
        except Exception as e:
            return blocked_by_seatbelt(e)

    blocked = Guard(max_payment_usd=0.001).run(capped)
    assert blocked.output is True and seller["paid"] == 1 and blocked.report.payments_blocked == 1
