"""A scripted OpenAI-compatible chat-completions server for harness tests.

Requests that offer tools walk a fixed script by counting the tool results
already in the conversation: first a `write` call, then a `bash` call, then
a final answer. Requests without tools get plain text, except the miner's,
which get an empty candidate list. Every request body is appended to the log file as one JSON line,
so a test can read exactly what the harness sent the model.

Usage: python scripts/mock_openai.py <port> <request-log.jsonl> <project-dir>
MOCK_WRITE_PATH and MOCK_BASH_COMMAND override the scripted write target and command.
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT, LOG, PROJECT = int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
USAGE = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}


def script(body: dict) -> dict:
    if not body.get("tools"):
        prompt = " ".join(str(m.get("content", "")) for m in body.get("messages", []))
        if "candidate miner for rka-memory" in prompt:
            return {"content": json.dumps({"candidates": [], "counter_links": []})}
        return {"content": "rka-memory dsh test"}
    done = sum(1 for m in body.get("messages", []) if m.get("role") == "tool")
    if done == 0:
        args = {"file_path": os.environ.get("MOCK_WRITE_PATH", f"{PROJECT}/hello.py"), "content": "print('hello')\n"}
        return {"tool": ("write", args)}
    if done == 1:
        args = {"description": "run a command", "command": os.environ.get("MOCK_BASH_COMMAND", "ls")}
        if os.environ.get("MOCK_BASH_ESCALATE"):
            args.update(sandbox_permissions=os.environ["MOCK_BASH_ESCALATE"], justification="test escalation")
        return {"tool": ("bash", args)}
    return {"content": "Done."}


def chunk(delta: dict, finish: str | None = None) -> bytes:
    payload = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "mock-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        body = json.dumps({"object": "list", "data": [{"id": "mock-model", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.loads(raw or b"{}")
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"path": self.path, "body": body}) + "\n")
        step = script(body)
        if "tool" in step:
            name, args = step["tool"]
            call = {"index": 0, "id": f"call_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            message, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": step["content"]}, "stop"
        if not body.get("stream"):
            out = json.dumps(
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "mock-model",
                    "choices": [{"index": 0, "message": {**message, "tool_calls": message.get("tool_calls")}, "finish_reason": finish}],
                    "usage": USAGE,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if "tool_calls" in message and message["tool_calls"]:
            self.wfile.write(chunk({"role": "assistant", "tool_calls": message["tool_calls"]}))
        else:
            self.wfile.write(chunk({"role": "assistant", "content": message["content"]}))
        self.wfile.write(chunk({}, finish))
        usage = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": int(time.time()), "model": "mock-model", "choices": [], "usage": USAGE}
        self.wfile.write(f"data: {json.dumps(usage)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer((os.environ.get("MOCK_BIND", "127.0.0.1"), PORT), Handler).serve_forever()
