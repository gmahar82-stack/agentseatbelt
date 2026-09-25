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

# Libraries with httpx's Client/AsyncClient.send API.
HTTPX_LIKE = ("httpx", "httpx2")

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
        response = original_send(self, request, *args, **kwargs)
        streamed = bool(kwargs.get("stream", False))
        run.record_response(url, body, response.headers.get("content-type", ""), None if streamed else response.content, streamed)
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
        response = await original_async_send(self, request, *args, **kwargs)
        streamed = bool(kwargs.get("stream", False))
        run.record_response(url, body, response.headers.get("content-type", ""), None if streamed else response.content, streamed)
        return response

    httpx.Client.send = send
    httpx.AsyncClient.send = async_send


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
        response = original_send(self, request, **kwargs)
        streamed = bool(kwargs.get("stream", False))
        run.record_response(url, body, response.headers.get("content-type", ""), None if streamed else response.content, streamed)
        return response

    requests.Session.send = send
