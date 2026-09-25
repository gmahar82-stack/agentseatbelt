import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import requests

from agentseatbelt import (
    BlockedRequest,
    BudgetExceeded,
    Guard,
    GuardStop,
)


# ---- fake LLM providers -----------------------------------------------------------------

def openai_reply(model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=500):
    return {
        "id": "chatcmpl-1",
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


class Recorder:
    """httpx MockTransport handler that records which requests actually reached the network."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply or (lambda req: openai_reply())

    def __call__(self, request: httpx.Request):
        self.calls.append(str(request.url))
        return httpx.Response(200, json=self.reply(request))


def llm_call(client, i=0, model="gpt-4o-mini"):
    body = {"model": model, "messages": [{"role": "user", "content": f"step {i}"}]}
    return client.post("https://api.openai.com/v1/chat/completions", json=body).json()


# ---- tests --------------------------------------------------------------------------------

def test_budget_stops_run_and_costs_are_tracked():
    rec = Recorder(lambda req: openai_reply("gpt-4o", 100_000, 10_000))  # $0.25 + $0.10 = $0.35 per call
    client = httpx.Client(transport=httpx.MockTransport(rec))

    def agent():
        for i in range(100):
            llm_call(client, i, "gpt-4o")

    result = Guard(max_budget_usd=1.00, loop_threshold=None).run(agent)
    r = result.report
    assert result.stopped and r.stop_reason == "budget_exceeded"
    assert len(rec.calls) == 3  # 3rd call crosses $1.00 ($1.05); the 4th is refused before it leaves
    assert r.llm_calls == 3 and r.cost_usd == pytest.approx(1.05)
    assert r.input_tokens == 300_000 and r.output_tokens == 30_000
    assert r.models["gpt-4o"] == 3


def test_request_limit():
    rec = Recorder()
    client = httpx.Client(transport=httpx.MockTransport(rec))
    result = Guard(max_requests=5).run(lambda: [llm_call(client, i) for i in range(50)])
    assert result.report.stop_reason == "request_limit"
    assert len(rec.calls) == 5


def test_loop_detection_on_identical_requests():
    rec = Recorder()
    client = httpx.Client(transport=httpx.MockTransport(rec))

    def stuck_agent():
        while True:
            llm_call(client, 0)  # same prompt forever

    result = Guard(loop_threshold=4).run(stuck_agent)
    assert result.report.stop_reason == "loop_detected"
    assert len(rec.calls) == 3


def test_blocked_endpoint_never_reaches_network_and_run_continues():
    rec = Recorder(lambda req: {"ok": True})
    client = httpx.Client(transport=httpx.MockTransport(rec))

    def agent():
        try:
            client.get("https://api.expensive-service.com/v1/data")
        except BlockedRequest:
            pass
        client.get("https://en.wikipedia.org/wiki/Cat")
        return "done"

    result = Guard(block_endpoints=["expensive-service.com"]).run(agent)
    assert result.output == "done" and not result.stopped
    assert rec.calls == ["https://en.wikipedia.org/wiki/Cat"]
    assert result.report.blocked == 1


def test_allow_list_and_stop_on_blocked():
    rec = Recorder()
    client = httpx.Client(transport=httpx.MockTransport(rec))

    def agent():
        llm_call(client)
        client.get("https://evil.example.com/steal")

    result = Guard(allow_endpoints=["api.openai.com"], stop_on_blocked=True).run(agent)
    assert result.report.stop_reason == "endpoint_blocked"
    assert rec.calls == ["https://api.openai.com/v1/chat/completions"]


def test_time_limit_hard_kills_pure_python_loop():
    def infinite_loop():
        x = 0
        while True:
            x += 1

    start = time.monotonic()
    result = Guard(max_runtime_seconds=0.5).run(infinite_loop)
    assert result.report.stop_reason == "time_limit"
    assert time.monotonic() - start < 2.0


def test_stop_cannot_be_swallowed_by_except_exception():
    rec = Recorder()
    client = httpx.Client(transport=httpx.MockTransport(rec))

    def sneaky_agent():
        for i in range(100):
            try:
                llm_call(client, i)
            except Exception:
                continue  # a careless agent retrying on every error

    result = Guard(max_requests=3).run(sneaky_agent)
    assert result.report.stop_reason == "request_limit"
    assert len(rec.calls) == 3


def test_emergency_stop_from_another_thread():
    guard = Guard()
    threading.Timer(0.3, guard.stop, args=("user pressed stop",)).start()

    def busy():
        while True:
            pass

    result = guard.run(busy)
    assert result.report.stop_reason == "emergency_stop"
    assert "user pressed stop" in result.report.message


def test_kill_file(tmp_path):
    kill = tmp_path / "STOP"
    threading.Timer(0.3, kill.write_text, args=("stop",)).start()

    def busy():
        while True:
            time.sleep(0.01)

    result = Guard(kill_file=str(kill)).run(busy)
    assert result.report.stop_reason == "emergency_stop"


def test_rate_limit_waits():
    rec = Recorder()
    client = httpx.Client(transport=httpx.MockTransport(rec))
    guard = Guard(max_requests_per_minute=2, rate_window_seconds=0.5, loop_threshold=None)
    start = time.monotonic()
    result = guard.run(lambda: [llm_call(client, i) for i in range(5)])
    assert not result.stopped and len(rec.calls) == 5
    assert time.monotonic() - start >= 0.9  # 5 calls at 2 per 0.5s needs two waits
    assert result.report.rate_limit_waits >= 2


def test_rate_limit_stop_mode():
    client = httpx.Client(transport=httpx.MockTransport(Recorder()))
    guard = Guard(max_requests_per_minute=2, on_rate_limit="stop")
    result = guard.run(lambda: [llm_call(client, i) for i in range(5)])
    assert result.report.stop_reason == "rate_limit"


def test_unknown_model_uses_fallback_price():
    rec = Recorder(lambda req: openai_reply("mystery-model-9000", 1_000_000, 0))
    client = httpx.Client(transport=httpx.MockTransport(rec))
    result = Guard().run(lambda: llm_call(client, 0, "mystery-model-9000"))
    assert result.report.cost_usd == pytest.approx(10.0)
    assert result.report.estimated_cost_calls == 1


def test_custom_pricing_and_prefix_match():
    rec = Recorder(lambda req: openai_reply("my-model-2026-01-01", 1_000_000, 1_000_000))
    client = httpx.Client(transport=httpx.MockTransport(rec))
    result = Guard(pricing={"my-model": (1.0, 2.0)}).run(lambda: llm_call(client))
    assert result.report.cost_usd == pytest.approx(3.0)
    assert result.report.estimated_cost_calls == 0


def test_anthropic_and_gemini_formats():
    def reply(req):
        if "anthropic" in str(req.url):
            return {"model": "claude-x", "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 5}}
        return {"modelVersion": "gemini-x", "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3}}

    client = httpx.Client(transport=httpx.MockTransport(Recorder(reply)))

    def agent():
        client.post("https://api.anthropic.com/v1/messages", json={"model": "claude-x"})
        client.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-x:generateContent", json={})

    r = Guard(pricing={"claude-x": (1, 1), "gemini-x": (1, 1)}).run(agent).report
    assert r.llm_calls == 2
    assert (r.input_tokens, r.output_tokens) == (15 + 7, 20 + 3)


def test_async_agent_budget():
    rec = Recorder(lambda req: openai_reply("gpt-4o", 100_000, 10_000))

    async def agent():
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as client:
            for i in range(100):
                body = {"model": "gpt-4o", "messages": [{"role": "user", "content": str(i)}]}
                await client.post("https://api.openai.com/v1/chat/completions", json=body)

    result = asyncio.run(Guard(max_budget_usd=1.00).arun(agent))
    assert result.report.stop_reason == "budget_exceeded"
    assert len(rec.calls) == 3


def test_async_time_limit_cancels_task():
    async def slow():
        await asyncio.sleep(60)

    start = time.monotonic()
    result = asyncio.run(Guard(max_runtime_seconds=0.3).arun(slow))
    assert result.report.stop_reason == "time_limit"
    assert time.monotonic() - start < 2.0


def test_checkpoint_blocks_actions_and_detects_loops():
    guard = Guard(block_actions=["delete_*"], loop_threshold=3)

    def agent():
        with pytest.raises(BlockedRequest):
            guard.checkpoint("delete_database", name="prod")
        while True:
            guard.checkpoint("search", query="same thing")

    r = guard.run(agent).report
    assert r.blocked == 1 and r.stop_reason == "loop_detected"


def test_requests_library_is_guarded():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps(openai_reply("gpt-4o", 1_000_000, 0)).encode()  # $2.50 per call
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        def agent():
            for i in range(10):
                requests.post(url, json={"model": "gpt-4o", "messages": [{"content": str(i)}]})

        r = Guard(max_budget_usd=5.00).run(agent).report
        assert r.stop_reason == "budget_exceeded" and r.llm_calls == 2
    finally:
        server.shutdown()


def test_httpx2_is_guarded():
    httpx2 = pytest.importorskip("httpx2")  # used by newer OpenAI SDKs
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx2.Response(200, json=openai_reply("gpt-4o", 1_000_000, 0))  # $2.50

    client = httpx2.Client(transport=httpx2.MockTransport(handler))

    def agent():
        for i in range(10):
            client.post("https://api.openai.com/v1/chat/completions", json={"model": "gpt-4o", "n": i})

    r = Guard(max_budget_usd=5.00).run(agent).report
    assert r.stop_reason == "budget_exceeded" and len(calls) == 2


def test_worker_threads_are_attributed_to_the_run():
    rec = Recorder()
    client = httpx.Client(transport=httpx.MockTransport(rec))

    def agent():
        threads = [threading.Thread(target=llm_call, args=(client, i)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    r = Guard().run(agent).report
    assert r.requests == 4 and r.llm_calls == 4


def test_no_guard_means_no_interference():
    rec = Recorder()
    client = httpx.Client(transport=httpx.MockTransport(rec))
    Guard(max_requests=1).run(lambda: None)  # installs the patches
    for i in range(5):
        llm_call(client, i)
    assert len(rec.calls) == 5


def test_agent_errors_propagate_with_report():
    def broken():
        raise ValueError("bug in agent")

    with pytest.raises(ValueError) as info:
        Guard().run(broken)
    assert info.value.agentseatbelt_report.status == "error"


def test_raise_on_stop():
    client = httpx.Client(transport=httpx.MockTransport(Recorder()))
    with pytest.raises(GuardStop):
        Guard(max_requests=1, raise_on_stop=True).run(lambda: [llm_call(client, i) for i in range(3)])


def test_budget_crossing_hard_kills_local_work():
    rec = Recorder(lambda req: openai_reply("gpt-4o", 1_000_000, 0))  # $2.50

    def agent():
        llm_call(httpx.Client(transport=httpx.MockTransport(rec)), 0, "gpt-4o")
        while True:  # expensive call done, now crunching locally forever
            pass

    start = time.monotonic()
    result = Guard(max_budget_usd=1.00).run(agent)
    assert result.report.stop_reason == "budget_exceeded"
    assert time.monotonic() - start < 2.0


def test_report_summary_and_dict():
    client = httpx.Client(transport=httpx.MockTransport(Recorder()))
    r = Guard(max_requests=2).run(lambda: [llm_call(client, i) for i in range(5)]).report
    text = r.summary()
    assert "STOPPED (request_limit)" in text and "gpt-4o-mini" in text
    assert json.dumps(r.to_dict())
