"""Smoke check of the interactive stack after `make up` (run as `make check-up`).

dsh web sign-in, one headless dsh session through the plugin inside the `dsh`
container, then a memory round that syncs the ledger to the stack's Core and
pulls it back. Prints PASS/FAIL lines and exits non-zero on any FAIL. Runs on
the host with only the standard library; COMPOSE is the compose command.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time

COMPOSE = shlex.split(os.environ["COMPOSE"]) + ["--profile", "interactive"]
failed = False

PROBE = """
import json
from rka_memory.config import load
from rka_memory.service import Memory

cfg = load()
world = Memory.open(cfg).world()
errors = cfg.ledger_dir / "hook-errors.log"
pulled = cfg.ledger_dir / "ledger.pulled.jsonl"
print(json.dumps({
    "events": len(world.events),
    "sessions": {s.id: {"harness": s.started.payload.get("harness"), "ended": s.ended is not None, "mined": s.mined}
                 for s in world.sessions.values() if s.started},
    "hook_errors": errors.read_text()[-500:] if errors.exists() else "",
    "pulled": sum(1 for _ in pulled.open()) if pulled.exists() else None,
}))
"""

SIGNIN = """
import sys, urllib.error, urllib.parse, urllib.request

class Stop(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def get(url, host, cookie=""):
    headers = {"Host": host, **({"Cookie": cookie} if cookie else {})}
    try:
        resp = urllib.request.build_opener(Stop).open(urllib.request.Request(url, headers=headers), timeout=10)
        return resp.status, resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers

u = urllib.parse.urlsplit(sys.argv[1])
code, headers = get(urllib.parse.urlunsplit(("http", "dsh:3080", u.path, u.query, "")), u.netloc)
cookie = (headers.get("Set-Cookie") or "").split(";")[0]
page, _ = get("http://dsh:3080/", u.netloc, cookie)
wrong, _ = get(urllib.parse.urlunsplit(("http", "dsh:3080", u.path, "token=wrong", "")), u.netloc)
print(code, bool(cookie), page, wrong)
"""


def compose(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*COMPOSE, *args], input=stdin, capture_output=True, text=True)


def check(name: str, ok: bool, detail: object = "") -> None:
    global failed
    failed |= not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok or detail == "" else f"  ({detail})"))


def probe() -> dict:
    out = compose("exec", "-T", "memory", "python", "-", stdin=PROBE)
    return json.loads(out.stdout)


def main() -> int:
    logs = compose("logs", "dsh").stdout
    urls = re.findall(r"http://127\.0\.0\.1:\d+/\S*token=\S+", logs)
    check("dsh web printed a sign-in URL", bool(urls))
    if urls:
        out = compose("exec", "-T", "memory", "python", "-", urls[-1], stdin=SIGNIN).stdout.split()
        check("token sign-in answers 303 with a cookie", out[:2] == ["303", "True"], out)
        check("signed-in page loads", out[2:3] == ["200"], out)
        check("a wrong token is refused", out[3:4] == ["401"], out)

    before = set(probe()["sessions"])
    run = compose(
        "exec", "-T", "dsh", "sh", "-c",
        # bounded for a real model; the mock ignores the prompt and runs its fixed script
        'cd /workspaces && /opt/rka-memory/docker/dsh/entrypoint.sh headless --approval-policy never '
        '"Reply with the single word ready. Do not use any tools."',
    )
    check("headless dsh session in the dsh container", run.returncode == 0, run.stderr[-300:])

    # The plugin mines in a detached process after the session ends.
    deadline = time.monotonic() + 90
    while True:
        state = probe()
        new = [s for s in state["sessions"] if s not in before]
        if (new and all(state["sessions"][s]["mined"] for s in new)) or time.monotonic() > deadline:
            break
        time.sleep(1)
    check("one new session in the ledger", len(new) == 1, new)
    check("session recorded with harness dsh", [state["sessions"][s]["harness"] for s in new] == ["dsh"])
    check("session ended and was mined", bool(new) and all(state["sessions"][s]["ended"] and state["sessions"][s]["mined"] for s in new))
    check("no hook errors", not state["hook_errors"], state["hook_errors"])

    rnd = compose("exec", "-T", "memory", "rka-memory", "jobs", "--once")
    try:
        report = json.loads(rnd.stdout)
    except ValueError:
        report = {"errors": rnd.stdout[-300:] + rnd.stderr[-300:]}
    check("memory round: gate, skills, metrics, sync", report.get("errors") == {} and "sync" in report.get("steps", {}), report.get("errors"))
    compose("exec", "-T", "memory", "rka-memory", "pull")
    state = probe()
    check(f"Core holds the whole ledger ({state['events']} events)", state["events"] == state["pulled"], f"pulled {state['pulled']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
