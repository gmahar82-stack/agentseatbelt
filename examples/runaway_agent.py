"""Demo: a runaway agent using the real OpenAI SDK, stopped by AgentSeatbelt.

Runs offline against a tiny fake OpenAI-compatible server, so no API key or money is needed:
    pip install openai
    python examples/runaway_agent.py
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openai import OpenAI

from agentseatbelt import Guard


class FakeOpenAI(BaseHTTPRequestHandler):
    """Answers every chat completion with 20k prompt + 2k completion tokens of gpt-4o."""

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        body = json.dumps({
            "id": "chatcmpl-demo",
            "object": "chat.completion",
            "created": 0,
            "model": request["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "Let me search again..."}}],
            "usage": {"prompt_tokens": 20_000, "completion_tokens": 2_000, "total_tokens": 22_000},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def runaway_agent(task: str) -> str:
    """A buggy agent that never decides it's done."""
    client = OpenAI(base_url=f"http://127.0.0.1:{PORT}/v1", api_key="demo", max_retries=0)
    history = [{"role": "user", "content": task}]
    while True:
        reply = client.chat.completions.create(model="gpt-4o", messages=history)
        history.append({"role": "assistant", "content": reply.choices[0].message.content})
        history.append({"role": "user", "content": "keep going"})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
    PORT = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()

    guard = Guard(max_budget_usd=1.00, max_runtime_seconds=60, verbose=False)
    result = guard.run(runaway_agent, "Find the best laptop under $500")
    print(result.report.summary())
    server.shutdown()
