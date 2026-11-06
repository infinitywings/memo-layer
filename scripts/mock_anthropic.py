"""A scripted Anthropic Messages API for driving a real `claude -p` in tests.

Requests that offer tools walk the tool calls in the JSON file given on the
command line (one per turn, chosen by how many tool results the conversation
already holds), then answer "Done." Requests without tools get plain text.
Every request body is appended to the log as one JSON line, so a test can
read what Claude Code sent and every tool result it got back.

Usage: python scripts/mock_anthropic.py <port> <request-log.jsonl> <steps.json>
steps.json: [{"name": "Bash", "input": {"command": "..."}}, ...]
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT, LOG, STEPS = int(sys.argv[1]), Path(sys.argv[2]), json.loads(Path(sys.argv[3]).read_text())


def tool_results(body: dict) -> int:
    n = 0
    for m in body.get("messages", []):
        if isinstance(m.get("content"), list):
            n += sum(1 for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result")
    return n


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _json(self, code: int, obj: dict) -> None:
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self) -> None:
        self._json(200, {"data": [], "has_more": False})

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.loads(raw or b"{}")
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"path": self.path, "body": body}) + "\n")
        if "count_tokens" in self.path:
            self._json(200, {"input_tokens": 100})
            return
        done = tool_results(body)
        step = STEPS[done] if body.get("tools") and done < len(STEPS) else None
        usage = {"input_tokens": 100, "output_tokens": 20}
        if step is None:
            block = {"type": "text", "text": "Done." if body.get("tools") else "Test"}
            stop = "end_turn"
        else:
            block = {"type": "tool_use", "id": f"toolu_{done:02d}", "name": step["name"], "input": step["input"]}
            stop = "tool_use"
        if not body.get("stream"):
            self._json(200, {"id": "msg_mock", "type": "message", "role": "assistant", "model": body.get("model", "mock"),
                             "content": [block], "stop_reason": stop, "stop_sequence": None, "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        w = self.wfile.write
        w(sse("message_start", {"type": "message_start", "message": {
            "id": "msg_mock", "type": "message", "role": "assistant", "model": body.get("model", "mock"),
            "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 1}}}))
        if block["type"] == "text":
            w(sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
            w(sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": block["text"]}}))
        else:
            start = {**block, "input": {}}
            w(sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": start}))
            w(sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                          "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}}))
        w(sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
        w(sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 20}}))
        w(sse("message_stop", {"type": "message_stop"}))
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
