"""Patches httpx, httpx2 and requests so every HTTP call made inside a guarded run passes through the guard.

The OpenAI, Anthropic, Groq and most other Python SDKs use httpx, or its successor httpx2 (newer
OpenAI SDKs), so their calls are covered without any changes to agent code. Outside a guarded run
the patched methods behave exactly like the originals.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import threading
import time

from ._state import current_run
from .payments import has_payment

# Libraries with httpx's Client/AsyncClient.send API.
HTTPX_LIKE = ("httpx", "httpx2")

# x402 clients (e.g. Coinbase's x402 package) resend the paid request inside their own transport or adapter,
# below Client.send / Session.send. So payments are also checked at the network transport, and a request
# whose payment was already checked in send() is marked so it isn't checked twice.
CHECKED = "agentseatbelt_payment_checked"

_installed = False
_install_lock = threading.Lock()


def install() -> None:
    """Patch the HTTP libraries once per process (idempotent)."""
    global _installed
    with _install_lock:
        if _installed:
            return
        for name in HTTPX_LIKE:
            _patch_httpx(name)
        _patch_requests()
        _installed = True


def _as_bytes(body: object) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8", "replace")
    return b""  # streamed/file bodies aren't inspected


def _patch_httpx(module_name: str) -> None:
    try:
        httpx = importlib.import_module(module_name)
    except ImportError:
        return

    def request_body(request) -> bytes:
        try:
            return request.content
        except httpx.RequestNotRead:
            return b""

    original_send = httpx.Client.send

    @functools.wraps(original_send)
    def send(self, request, *args, **kwargs):
        run = current_run()
        if run is None:
            return original_send(self, request, *args, **kwargs)
        url, body = str(request.url), request_body(request)
        while (wait := run.admit("http", request.method, url, body)) > 0:
            time.sleep(wait)
        payment = run.check_payment(request.method, url, request.headers)
        if payment is not None:
            request.extensions[CHECKED] = True
        response = original_send(self, request, *args, **kwargs)
        streamed = bool(kwargs.get("stream", False))
        run.record_response(
            url, body, response.headers.get("content-type", ""), None if streamed else response.content, streamed,
            response.status_code, response.headers, payment,
        )
        return response

    original_async_send = httpx.AsyncClient.send

    @functools.wraps(original_async_send)
    async def async_send(self, request, *args, **kwargs):
        run = current_run()
        if run is None:
            return await original_async_send(self, request, *args, **kwargs)
        url, body = str(request.url), request_body(request)
        while (wait := run.admit("http", request.method, url, body)) > 0:
            await asyncio.sleep(wait)
        payment = None
        if has_payment(request.headers):
            # A Pay Safe check waits on the network: run it off the event loop.
            check = functools.partial(run.check_payment, request.method, url, request.headers)
            payment = await asyncio.to_thread(check) if run.guard.pay_safe else check()
            request.extensions[CHECKED] = True
        response = await original_async_send(self, request, *args, **kwargs)
        streamed = bool(kwargs.get("stream", False))
        run.record_response(
            url, body, response.headers.get("content-type", ""), None if streamed else response.content, streamed,
            response.status_code, response.headers, payment,
        )
        return response

    httpx.Client.send = send
    httpx.AsyncClient.send = async_send

    # Network transports: payments resent inside an x402 transport only pass through here.
    def needs_check(run, request) -> bool:
        return run is not None and not request.extensions.get(CHECKED) and has_payment(request.headers)

    def remember_quote(run, request, response, content: bytes | None) -> None:
        if response.status_code != 402:
            return
        if response.headers.get("payment-required"):
            run.record_payment(str(request.url), 402, response.headers, None)
        else:  # x402 v1: the options are in the JSON body
            run.record_response(str(request.url), b"", response.headers.get("content-type", ""), content, False, 402, response.headers)

    def v1_quote_body(response) -> bool:
        return response.status_code == 402 and not response.headers.get("payment-required")

    original_handle = httpx.HTTPTransport.handle_request

    @functools.wraps(original_handle)
    def handle_request(self, request):
        run = current_run()
        if run is None:
            return original_handle(self, request)
        payment = run.check_payment(request.method, str(request.url), request.headers) if needs_check(run, request) else None
        response = original_handle(self, request)
        remember_quote(run, request, response, response.read() if v1_quote_body(response) else None)
        if payment is not None:
            run.record_payment(str(request.url), response.status_code, response.headers, payment)
        return response

    original_async_handle = httpx.AsyncHTTPTransport.handle_async_request

    @functools.wraps(original_async_handle)
    async def handle_async_request(self, request):
        run = current_run()
        if run is None:
            return await original_async_handle(self, request)
        payment = None
        if needs_check(run, request):
            check = functools.partial(run.check_payment, request.method, str(request.url), request.headers)
            payment = await asyncio.to_thread(check) if run.guard.pay_safe else check()
        response = await original_async_handle(self, request)
        remember_quote(run, request, response, await response.aread() if v1_quote_body(response) else None)
        if payment is not None:
            run.record_payment(str(request.url), response.status_code, response.headers, payment)
        return response

    httpx.HTTPTransport.handle_request = handle_request
    httpx.AsyncHTTPTransport.handle_async_request = handle_async_request


def _patch_requests() -> None:
    try:
        import requests
    except ImportError:
        return

    original_send = requests.Session.send

    @functools.wraps(original_send)
    def send(self, request, **kwargs):
        run = current_run()
        if run is None:
            return original_send(self, request, **kwargs)
        url, body = request.url, _as_bytes(request.body)
        while (wait := run.admit("http", request.method or "GET", url, body)) > 0:
            time.sleep(wait)
        payment = run.check_payment(request.method or "GET", url, request.headers)
        if payment is not None:
            setattr(request, CHECKED, True)
        response = original_send(self, request, **kwargs)
        streamed = bool(kwargs.get("stream", False))
        run.record_response(
            url, body, response.headers.get("content-type", ""), None if streamed else response.content, streamed,
            response.status_code, response.headers, payment,
        )
        return response

    requests.Session.send = send

    # Transport adapter: payments resent inside an x402 adapter (a subclass calling super().send) pass here.
    original_adapter_send = requests.adapters.HTTPAdapter.send

    @functools.wraps(original_adapter_send)
    def adapter_send(self, request, *args, **kwargs):
        run = current_run()
        if run is None:
            return original_adapter_send(self, request, *args, **kwargs)
        payment = None
        if not getattr(request, CHECKED, False) and has_payment(request.headers):
            payment = run.check_payment(request.method or "GET", request.url, request.headers)
        response = original_adapter_send(self, request, *args, **kwargs)
        if response.status_code == 402:
            stream = kwargs.get("stream", args[0] if args else False)
            body = None if stream or response.headers.get("payment-required") else response.content
            run.record_response(request.url, b"", response.headers.get("content-type", ""), body, False, 402, response.headers)
        if payment is not None:
            run.record_payment(request.url, response.status_code, response.headers, payment)
        return response

    requests.adapters.HTTPAdapter.send = adapter_send
