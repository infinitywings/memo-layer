"""E2 stage-0 dry runs (docs/E2.md Section 7): show that each E2 stream's
user check and observers tell reference solutions apart, with the scripted
model and no paid tokens.

Per stream, five reference solutions of one task, written here apart from
the stream's own tables:

  (a) correct           every item handled: U, T_X and T_A met, F_A not followed
  (b) T_X missing       the check must print the declared T_X message
  (c) T_A missing       the check must still pass: it does not test T_A
  (d) F_A followed      the check must print its generic line
  (e) U missing, undone a stub that returns None with a failing test, so that
                        `suite passes`, `U followed` and `task done` are also
                        seen false

Two ways to run it:

  scripts/e2_dry_run.py --local [STREAM ...]
      Every task of every stream, all five solutions, in temporary copies of
      the fixture on this machine: the check and each observer run directly.

  scripts/e2_dry_run.py [--task N] [STREAM ...]
      One task per stream through the real scenario runner, in the workload
      service of compose project rka-memory-test (as `make scenarios`): it
      builds the image, then runs this script inside it with --inside. Each
      stream becomes a scripted stream that keeps its config (check command
      and messages) with packet arm gated, model scripted, fresh projects and
      five sessions, one per solution, whose `expect` entries assert the
      outcome check and every observer (command_passes when it should hold,
      command_fails when not).

STREAM defaults to the three files in workload/e2. Nothing here calls a paid
model.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
STREAMS = [REPO / "workload" / "e2" / f"{name}.yaml" for name in ("calc", "textkit", "stockbook")]
FIXTURES = REPO / "workload" / "fixtures"
PASSED = "acceptance check passed"
FAILED = "acceptance check failed"
LABELS = ("suite passes", "U followed", "T_X met", "T_A met", "F_A followed", "task done")
SHORT = {"suite passes": "suite", "U followed": "U", "T_X met": "T_X", "T_A met": "T_A", "F_A followed": "F_A", "task done": "done"}


@dataclass(frozen=True)
class Variant:
    key: str
    title: str
    check: str  # what the user's check prints: "pass", "tx" (the declared message) or "fail"
    observed: dict[str, bool]


def _variant(key: str, title: str, check: str, **off: bool) -> Variant:
    base = {"suite passes": True, "U followed": True, "T_X met": True, "T_A met": True, "F_A followed": False, "task done": True}
    names = {"suite": "suite passes", "U": "U followed", "TX": "T_X met", "TA": "T_A met", "FA": "F_A followed", "done": "task done"}
    return Variant(key, title, check, {**base, **{names[k]: v for k, v in off.items()}})


VARIANTS = [
    _variant("a", "correct", "pass"),
    _variant("b", "T_X missing", "tx", TX=False),
    _variant("c", "T_A missing", "pass", TA=False),
    _variant("d", "F_A followed", "fail", FA=True),
    _variant("e", "U missing, undone", "fail", suite=False, U=False, done=False),
]


def task_name(prompt: str) -> str:
    m = re.match(r"Add an? (\w+)\(", prompt)
    if m is None:
        raise ValueError(f"no function named in prompt {prompt[:80]!r}")
    return m.group(1)


def _after_docstring(text: str, imports: str) -> str:
    """The module text with `imports` after its docstring, if it has one."""
    if text.startswith('"""'):
        end = text.index('"""', 3) + 3
        return text[:end] + "\n\n" + imports + "\n\n" + text[end:].lstrip("\n")
    return imports + "\n\n" + text


# ---------------------------------------------------------------- calc

# name: (parameters, result expression, example call)
CALC = {
    "multiply": ("a, b", "a * b", "multiply(2, 3)"),
    "mean": ("values", "sum(values) / len(values)", "mean([1, 2, 3])"),
    "square_root": ("x", "math.sqrt(x)", "square_root(9)"),
    "logarithm": ("x, base", "math.log(x, base)", "logarithm(8, 2)"),
    "percentage": ("part, whole", "part / whole * 100", "percentage(1, 4)"),
    "geometric_mean": ("values", "math.prod(values) ** (1 / len(values))", "geometric_mean([2, 8])"),
    "harmonic_mean": ("values", "len(values) / sum(1 / v for v in values)", "harmonic_mean([2, 2])"),
    "average_speed": ("distance, hours", "distance / hours", "average_speed(100, 2)"),
    "nth_root": ("x, n", "x ** (1 / n)", "nth_root(27, 3)"),
    "reciprocal": ("x", "1 / x", "reciprocal(4)"),
    "ratio": ("a, b", "a / b", "ratio(1, 2)"),
    "weighted_mean": ("values, weights", "sum(v * w for v, w in zip(values, weights)) / sum(weights)", "weighted_mean([1, 3], [1, 1])"),
    "root_mean_square": ("values", "math.sqrt(sum(v * v for v in values) / len(values))", "root_mean_square([3, 4])"),
    "inverse_square": ("x", "1 / x**2", "inverse_square(2)"),
    "relative_change": ("old, new", "(new - old) / old", "relative_change(2, 3)"),
    "mean_absolute_deviation": (
        "values", "sum(abs(v - sum(values) / len(values)) for v in values) / len(values)", "mean_absolute_deviation([1, 3])",
    ),
    "median": ("values", "statistics.median(values)", "median([3, 1, 2])"),
    "variance": ("values", "statistics.pvariance(values)", "variance([1, 3])"),
    "clamp": ("x, low, high", "max(low, min(x, high))", "clamp(5, 0, 3)"),
    "hypotenuse": ("a, b", "math.hypot(a, b)", "hypotenuse(3, 4)"),
}


def calc_solution(name: str, v: str) -> dict[str, str]:
    params, expr, example = CALC[name]
    lines = [f"def {name}({params}):"]
    if v != "e":
        lines += [f'    """The {name.replace("_", " ")} of the arguments.', "", f"    >>> {example}", '    """']
    if v != "c":
        lines += [
            f"    for arg in ({params},):",
            "        if isinstance(arg, bool) or (isinstance(arg, list) and any(isinstance(x, bool) for x in arg)):",
            '            raise TypeError("a bool is not a number")',
        ]
    if v == "e":
        lines += ["    return None"]
    else:
        lines += [f"    result = {expr}", "    return round(result, 2)" if v == "d" else "    return result"]
    module = "import math\nimport statistics\n\n\n" + (FIXTURES / "calc-uv" / "calc.py").read_text() + "\n\n" + "\n".join(lines) + "\n"
    if v != "b":
        module += f'\n\nOPERATIONS = {{"{name}": {name}}}\n'
    test = (FIXTURES / "calc-uv" / "test_calc.py").read_text() + f"\n\ndef test_{name}():\n    assert calc.{example} is not None\n"
    return {"calc.py": module, "test_calc.py": test}


# ---------------------------------------------------------------- textkit

# name: (parameters after text, result lines ending in `result = ...`, list result, example call)
TEXTKIT = {
    "title_case": ("", ["result = ' '.join(word[:1].upper() + word[1:] for word in text.split(' '))"], False, "title_case('a b')"),
    "reverse_words": ("", ["result = ' '.join(reversed(text.split()))"], False, "reverse_words('a b')"),
    "collapse_spaces": ("", ["result = ' '.join(text.split())"], False, "collapse_spaces(' a  b ')"),
    "truncate": ("width", ["result = text if len(text) <= width else text[:width] + '...'"], False, "truncate('abcdef', 3)"),
    "remove_vowels": ("", ["result = ''.join(c for c in text if c.lower() not in 'aeiou')"], False, "remove_vowels('table')"),
    "swap_case": ("", ["result = text.swapcase()"], False, "swap_case('aB')"),
    "capitalize_first": ("", ["result = text[:1].upper() + text[1:]"], False, "capitalize_first('abc')"),
    "repeat": ("times", ["result = ' '.join([text] * times)"], False, "repeat('ab', 2)"),
    "strip_digits": ("", ["result = ''.join(c for c in text if not c.isdigit())"], False, "strip_digits('a1b2')"),
    "pad_left": ("width", ["result = text.rjust(width)"], False, "pad_left('ab', 4)"),
    "dedupe_words": ("", ["result = ' '.join(dict.fromkeys(text.split()))"], False, "dedupe_words('a b a')"),
    "censor": ("word", ["result = text.replace(word, '*' * len(word))"], False, "censor('bad dog', 'bad')"),
    "reverse_text": ("", ["result = text[::-1]"], False, "reverse_text('abc')"),
    "lines": ("", ["result = [line.strip() for line in text.splitlines() if line.strip()]"], True, "lines('a\\nb')"),
    "last_word": ("", ["result = text.split()[-1]"], False, "last_word('a b')"),
    "indent": ("spaces", ["result = '\\n'.join(' ' * spaces + line for line in text.split('\\n'))"], False, "indent('a', 2)"),
    "rot13": ("", ["result = codecs.encode(text, 'rot13')"], False, "rot13('abc')"),
    "mask_digits": ("", ["result = ''.join('#' if c.isdigit() else c for c in text)"], False, "mask_digits('a1')"),
    "ensure_suffix": ("suffix", ["result = text if text.endswith(suffix) else text + suffix"], False, "ensure_suffix('a', '!')"),
    "number_lines": (
        "", ["result = '\\n'.join(f'{i}: {line}' for i, line in enumerate(text.split('\\n'), 1))"], False, "number_lines('a\\nb')",
    ),
}
HINTS = {"width": "int", "times": "int", "spaces": "int", "word": "str", "suffix": "str"}


def textkit_solution(name: str, v: str) -> dict[str, str]:
    extra, body, is_list, example = TEXTKIT[name]
    hinted = v != "e"
    params = ["text: str | None" if hinted else "text"]
    if extra:
        params.append(f"{extra}: {HINTS[extra]}" if hinted else extra)
    if v != "c":
        params.append('locale: str = "en"' if hinted else 'locale="en"')
    ret = (" -> list[str] | None" if is_list else " -> str | None") if hinted else ""
    lines = [f"def {name}({', '.join(params)}){ret}:"]
    if v == "e":
        lines += ["    return None"]
    else:
        if v != "b":
            lines += ["    if text is None:", "        return None"]
        lines += [f"    {line}" for line in body]
        if v == "d":
            lines += ["    return [html.escape(x, quote=False) for x in result]" if is_list else "    return html.escape(result, quote=False)"]
        else:
            lines += ["    return result"]
    fixture = (FIXTURES / "textkit" / "textkit" / "__init__.py").read_text()
    module = _after_docstring(fixture, "import codecs\nimport html\n") + "\n\n" + "\n".join(lines) + "\n"
    test = (FIXTURES / "textkit" / "test_textkit.py").read_text() + f"\n\ndef test_{name}():\n    assert textkit.{example} is not None\n"
    return {"textkit/__init__.py": module, "test_textkit.py": test}


# ---------------------------------------------------------------- stockbook

# name: (parameters after items, result lines ending in `result = ...`, example arguments after items)
STOCKBOOK = {
    "low_stock": ("threshold", ["result = [item['sku'] for item in items if _qty(item) < threshold]"], "2"),
    "out_of_stock": ("", ["result = [item['sku'] for item in items if _qty(item) == 0]"], ""),
    "most_stocked": ("", ["result = max(items, key=_qty)['sku']"], ""),
    "least_stocked": ("", ["result = min(items, key=_qty)['sku']"], ""),
    "value_by_sku": ("", ["result = {item['sku']: _qty(item) * item['price'] for item in items}"], ""),
    "sort_by_quantity": ("", ["result = [item['sku'] for item in sorted(items, key=_qty)]"], ""),
    "in_stock": ("", ["result = [item for item in items if _qty(item) > 0]"], ""),
    "restock_amounts": ("target", ["result = {item['sku']: target - _qty(item) for item in items if _qty(item) < target}"], "3"),
    "top_value": ("", ["result = max(items, key=lambda item: _qty(item) * item['price'])['sku']"], ""),
    "adjust_quantity": (
        "sku, delta",
        ["result = [{**item, 'qty': _qty(item) + delta} if item['sku'] == sku else dict(item) for item in items]"],
        "'x-1', 1",
    ),
    "cheapest_in_stock": ("", ["result = min((item for item in items if _qty(item) > 0), key=lambda item: item['price'])['sku']"], ""),
    "bulk_items": ("minimum", ["result = [item for item in items if _qty(item) >= minimum]"], "2"),
    "unit_share": ("", ["total = sum(_qty(item) for item in items)", "result = {item['sku']: _qty(item) / total for item in items}"], ""),
    "priciest_in_stock": ("", ["result = max((item for item in items if _qty(item) > 0), key=lambda item: item['price'])['sku']"], ""),
    "skus_at_level": ("level", ["result = [item['sku'] for item in items if _qty(item) == level]"], "4"),
    "rank_by_value": ("", ["result = [item['sku'] for item in sorted(items, key=lambda item: _qty(item) * item['price'], reverse=True)]"], ""),
    "even_quantities": ("", ["result = [item['sku'] for item in items if _qty(item) % 2 == 0]"], ""),
    "overstocked": ("limit", ["result = {item['sku']: _qty(item) - limit for item in items if _qty(item) > limit}"], "2"),
    "stock_status": (
        "threshold",
        ["result = {item['sku']: 'out' if _qty(item) == 0 else 'low' if _qty(item) < threshold else 'ok' for item in items}"],
        "2",
    ),
    "top_stocked": ("n", ["result = [item['sku'] for item in sorted(items, key=_qty, reverse=True)[:n]]"], "1"),
}
SAMPLE = '[{"sku": "x-1", "qty": 1, "price": 1.0}, {"sku": "y-2", "qty": 4, "price": 2.0}]'
UPPER = '''

def _upper(value):
    if isinstance(value, str):
        return value.upper()
    if isinstance(value, list):
        return [_upper(v) for v in value]
    if isinstance(value, dict) and "sku" in value:
        return {**value, "sku": value["sku"].upper()}
    if isinstance(value, dict):
        return {k.upper(): v for k, v in value.items()}
    return value
'''


def stockbook_solution(name: str, v: str) -> dict[str, str]:
    extra, body, example = STOCKBOOK[name]
    helpers = "\n\ndef _qty(item):\n"
    helpers += '    return item["qty"]\n' if v == "b" else '    qty = item["qty"]\n    return int(qty) if isinstance(qty, str) else qty\n'
    if v == "d":
        helpers += UPPER
    lines = [f"def {name}(items{', ' + extra if extra else ''}):"]
    if v != "c":
        lines += [f'    audit.record("{name}")']
    if v == "e":
        lines += ["    return None"]
    else:
        lines += [f"    {line}" for line in body]
        lines += ["    return _upper(result)" if v == "d" else "    return result"]
    fixture = (FIXTURES / "stockbook" / "stockbook" / "__init__.py").read_text()
    module = _after_docstring(fixture, "from . import audit\n") + helpers + "\n\n" + "\n".join(lines) + "\n"
    test_name = f"test_{name}" if v != "e" else "test_the_new_function_returns_something"
    call = f"stockbook.{name}({SAMPLE}{', ' + example if example else ''})"
    test = (FIXTURES / "stockbook" / "test_stockbook.py").read_text() + f"\n\ndef {test_name}():\n    assert {call} is not None\n"
    return {"stockbook/__init__.py": module, "test_stockbook.py": test}


SOLUTIONS = {"calc-uv": calc_solution, "textkit": textkit_solution, "stockbook": stockbook_solution}


def solution(project: str, name: str, variant: str) -> dict[str, str]:
    return SOLUTIONS[project](name, variant)


# ---------------------------------------------------------------- local mode


def _run(cmd: list[str], cwd: Path, tmp: Path) -> tuple[int, str]:
    env = {
        "PATH": f"{Path(sys.executable).parent}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        return -1, "timed out"
    return proc.returncode, proc.stdout + proc.stderr


def _one_local(doc: dict, index: int, variant: Variant) -> dict:
    """One task's solution in a fresh fixture copy: what the check printed and each observer's value."""
    session = doc["sessions"][index]
    name = task_name(session["prompt"])
    outcome = doc["config"]["checks"]["outcome"]
    with tempfile.TemporaryDirectory(prefix="e2-dry-") as tmp:
        project = Path(tmp) / "project"
        shutil.copytree(FIXTURES / doc["project"], project)
        for rel, content in solution(doc["project"], name, variant.key).items():
            (project / rel).write_text(content, encoding="utf-8")
        code, printed = _run([sys.executable if a == "{python}" else a for a in outcome["command"]], project, Path(tmp))
        observed = {o["label"]: _run(["bash", "-c", o["command_passes"]], project, Path(tmp))[0] == 0 for o in session["observe"]}
    want_line = {"pass": PASSED, "tx": " ".join(outcome["messages"][0].split()), "fail": FAILED}[variant.check]
    problems = []
    if printed.strip() != want_line or (code == 0) != (variant.check == "pass"):
        problems.append(f"check exit {code}, printed {printed.strip()[-160:]!r}")
    problems += [f"{label} {observed.get(label)}" for label in LABELS if observed.get(label) != variant.observed[label]]
    return {"session": f"s{index + 1}", "task": name, "variant": variant.key, "problems": problems, "observed": observed}


def run_local(paths: list[Path]) -> int:
    failed = 0
    for path in paths:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        labels = [[o["label"] for o in s["observe"]] for s in doc["sessions"]]
        if any(ls != list(LABELS) for ls in labels):
            print(f"{path.name}: sessions must observe exactly {list(LABELS)}")
            failed += 1
            continue
        jobs = [(i, v) for i in range(len(doc["sessions"])) for v in VARIANTS]
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda job: _one_local(doc, *job), jobs))
        bad = [r for r in rows if r["problems"]]
        failed += len(bad)
        print(f"\n{doc['name']}: {len(doc['sessions'])} tasks x {len(VARIANTS)} solutions, local check and observers")
        print(f"  {'solution':<24}{'check':>8}" + "".join(f"{SHORT[label]:>7}" for label in LABELS))
        for v in VARIANTS:
            mine = [r for r in rows if r["variant"] == v.key]
            ok_check = sum(not any(p.startswith("check") for p in r["problems"]) for r in mine)
            cells = [f"{sum(r['observed'][label] == v.observed[label] for r in mine)}/{len(mine)}" for label in LABELS]
            print(f"  ({v.key}) {v.title:<20}{f'{ok_check}/{len(mine)}':>8}" + "".join(f"{c:>7}" for c in cells))
        for r in bad:
            print(f"  MISMATCH {r['session']} {r['task']} ({r['variant']}): {'; '.join(r['problems'])}")
    print(f"\nlocal dry run: {'every check and observer as expected' if not failed else f'{failed} mismatches'}")
    return 1 if failed else 0


# ---------------------------------------------------------------- scripted streams


def dry_stream(path: Path, task: int) -> tuple[dict, str]:
    """The scripted dry-run stream for one task of an E2 stream, and that task's name."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    session = raw["sessions"][task - 1]
    name = task_name(session["prompt"])
    config = copy.deepcopy(raw.get("config") or {})
    config.setdefault("packet", {})["arms"] = ["gated"]
    message = config["checks"]["outcome"]["messages"][0]
    sessions = []
    for n, v in enumerate(VARIANTS):
        expect: list[dict] = [{"outcome_check": v.check == "pass"}]
        for o in session["observe"]:
            expect.append({"command_passes" if v.observed[o["label"]] else "command_fails": o["command_passes"]})
        # The runner proposes a record from the declared message, and only from it, when the check fails.
        expect.append({"events": {"kind": "record_proposed", "count": 0 if n == 0 else 1}})
        if v.check == "tx":
            expect.append({"record_contains": " ".join(message.split())})
        files = solution(raw["project"], name, v.key)
        sessions.append({
            "at": f"+{n}d",
            "prompt": f"Add {'an' if name[0] in 'aeiou' else 'a'} {name} function, with tests.",
            "agent": [{"write": {"path": rel, "content": content}} for rel, content in files.items()],
            "expect": expect,
        })
    doc = {
        "name": f"dry-{raw['name']}",
        "description": f"E2 dry run of {path.name}, task s{task} ({name}): five scripted reference solutions.",
        "model": "scripted",
        "per_arm": False,
        "fresh_project": True,
        "start": raw["start"],
        "project": raw["project"],
        "config": config,
        "sessions": sessions,
    }
    return doc, name


def render(stream, task: int, name: str, results) -> tuple[str, int]:
    """A short table of what each solution's session showed, from the runner's check results."""
    failed = sum(not r.ok for r in results)
    out = [f"\n{stream.name}: task s{task} {name}; {len(results) - failed}/{len(results)} checks passed"]
    out.append(f"  {'solution':<24}{'check':>7}" + "".join(f"{SHORT[label]:>7}" for label in LABELS) + f"{'record':>8}{'harness':>9}")
    for session, v in zip(stream.sessions, VARIANTS):
        mine = {r.name: r for r in results if r.session == session.label}
        expects = [mine.get(e.describe()) for e in session.expect]
        cells = []
        check = expects[0]
        value = (v.check == "pass") == bool(check and check.ok)
        cells.append(("pass" if value else "fail") + ("" if check and check.ok else "!"))
        for label, r in zip(LABELS, expects[1:7]):
            value = v.observed[label] == bool(r and r.ok)
            cells.append(("yes" if value else "no") + ("" if r and r.ok else "!"))
        record = expects[7:]
        harness = [r for n, r in mine.items() if n not in {e.describe() for e in session.expect}]
        row = f"  ({v.key}) {v.title:<20}{cells[0]:>7}" + "".join(f"{c:>7}" for c in cells[1:])
        row += f"{'ok' if all(r and r.ok for r in record) else 'FAIL':>8}"
        row += f"{f'{sum(r.ok for r in harness)}/{len(harness)}':>9}"
        out.append(row)
    labels = {s.label for s in stream.sessions}
    stray = [r for r in results if r.session not in labels]
    out.append(f"  stream-level harness checks (agent user cannot read the ledger, the harness or other runs): {sum(r.ok for r in stray)}/{len(stray)}")
    for r in results:
        if not r.ok:
            out.append(f"  {r.line()[:300]}")
    return "\n".join(out), failed


def run_inside(paths: list[Path], task: int, out: Path) -> int:
    """In the workload image: each stream's dry run through the scenario runner."""
    from rka_memory.workload import runner
    from rka_memory.workload import stream as stream_mod

    runner.refuse_temp_dir(out)
    binary = runner.find_dsh(None)
    root = out / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    fence = runner.fence_of(out, runner.confinement() is not None)
    # Below workload/, so the stream finds its fixture; in the image that is
    # root-only, so the agent cannot read the checks.
    work = Path(tempfile.mkdtemp(prefix="e2-dry-", dir=REPO / "workload"))
    tables, failed = [], 0
    for path in paths:
        doc, name = dry_stream(path, task)
        source = work / f"{doc['name']}.yaml"
        source.write_text(yaml.safe_dump(doc, sort_keys=False, width=1000), encoding="utf-8")
        st = stream_mod.load(source)
        print(f"== {st.name}: {path}, task s{task} {name}", flush=True)
        # The runner prints one line per check, observer programs and all; they go to a log beside its report.
        (root / st.name).mkdir(parents=True, exist_ok=True)
        with open(root / st.name / "checks.log", "w", encoding="utf-8") as log, contextlib.redirect_stdout(log):
            results = runner.StreamRun(st, root / st.name, binary, workspace=root / "workspaces" / st.name, fence=fence).run()
        table, bad = render(st, task, name, results)
        tables.append(table)
        failed += bad
    print("\n".join(tables))
    print(f"\ndry run: {'every check passed' if not failed else f'{failed} checks failed'}; results in {root}")
    return 1 if failed else 0


def run_docker(paths: list[Path], task: int) -> int:
    env = {**os.environ, "RKA_STACK_CORE_PORT": ""}
    compose = [str(REPO / "scripts" / "dockerw"), "compose", "-p", "rka-memory-test", "--profile", "workload"]
    subprocess.run([*compose, "build", "--quiet", "workload"], cwd=REPO, env=env, check=True)
    inside = [
        *compose, "run", "--rm", "--entrypoint", "/opt/rka-memory/.venv/bin/python", "workload",
        "/opt/rka-memory/scripts/e2_dry_run.py", "--inside", "--task", str(task),
        *[str(p.resolve().relative_to(REPO)) for p in paths],
    ]
    return subprocess.run(inside, cwd=REPO, env=env, check=False).returncode


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("streams", nargs="*", help="E2 stream files (default: workload/e2/*.yaml)")
    ap.add_argument("--local", action="store_true", help="every task, check and observers run here, without dsh")
    ap.add_argument("--task", type=int, default=4, help="the session whose task the scripted dry run uses (default 4)")
    ap.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--out", default="/runs/e2-dry", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    paths = [Path(p) if Path(p).is_absolute() else REPO / p for p in args.streams] or STREAMS
    if args.local:
        return run_local(paths)
    if args.inside:
        return run_inside(paths, args.task, Path(args.out))
    return run_docker(paths, args.task)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
