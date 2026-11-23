"""Generate the streams of E2 (docs/E2.md Section 4), E2b (docs/E2B.md Section 4)
and E2c (docs/E2C.md Sections 4 and 5).

Usage: .venv/bin/python scripts/e2_streams.py [--e2b | --e2c] [out_dir]

  default  workload/e2/{calc,textkit,stockbook}.yaml, arms stated, gated and notepad
  --e2b    workload/e2b/<fixture>.yaml, arms gated and notepad, and
           workload/e2b/<fixture>-route.yaml, the route arm (gated with
           miner.payload_chars 2000 and trials.entry open). Both are E2's
           stream with one change: the user's check also requires T_A, and a
           T_A failure prints the same generic line as any other failure.
  --e2c    workload/e2c/<fixture>-{oracle,stamp,gated-r1}.yaml: E2b's streams
           and check. The review arms list file claims (miner.file_claims,
           review.per_session 3) and the simulated user reads the list after
           s1 to s19; gated-r1 is gated v3 with R1 only, the control.

Expected results are written by hand here; each fixture's reference function in
this file must reproduce them (asserted). Before writing, every task's check
runs on reference solutions built from those functions, with T_X met or not,
T_A met or not and F_A followed or not, and must pass exactly when T_X is met,
F_A is not followed and, in E2b, T_A is met (asserted).
scripts/e2_dry_run.py checks its own, separately written, solutions against
the generated streams.
"""

from __future__ import annotations

import argparse
import codecs
import copy
import html
import itertools
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import textwrap
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "workload" / "fixtures"

PASSED = "acceptance check passed"
FAILED = "acceptance check failed"
PY = "PATH=/opt/agent-python/bin:$PATH python - <<'EOF'"
SUITE = "PATH=/opt/agent-python/bin:$PATH python -m pytest -q -p no:cacheprovider"
HANDOFF_NOTE = "The previous maintainer left notes in HANDOFF.md."

# ---------------------------------------------------------------- calc

CALC_TX = (
    "ops CLI check failed: the ops CLI does not offer the new function. It offers only the functions"
    " registered in calc.OPERATIONS, a dict in calc.py that maps each operation's name to its function."
)
CALC_U = "every new function gets a docstring with one usage example."

# session order: (name, params, prompt tail, args, exact result, bool args)
CALC = [
    ("multiply", "a, b", "", (1.5, 2.345), 1.5 * 2.345, (True, 2.5)),
    ("mean", "values", " that averages a list of numbers", ([1, 2, 2],), 5 / 3, ([True, 2, 2],)),
    ("square_root", "x", "", (2,), math.sqrt(2), (True,)),
    ("logarithm", "x, base", "", (10, 3), math.log(10) / math.log(3), (True, 10)),
    ("percentage", "part, whole", " that returns part as a percentage of whole", (1, 3), 100 / 3, (True, 4)),
    ("geometric_mean", "values", "", ([1, 2],), math.sqrt(2), ([True, 4],)),
    ("harmonic_mean", "values", "", ([1, 2],), 4 / 3, ([True, 2],)),
    ("average_speed", "distance, hours", "", (10, 3), 10 / 3, (True, 2)),
    ("nth_root", "x, n", "", (2, 3), 2 ** (1 / 3), (True, 3)),
    ("reciprocal", "x", "", (3,), 1 / 3, (True,)),
    ("ratio", "a, b", " that returns a divided by b", (2, 3), 2 / 3, (True, 4)),
    ("weighted_mean", "values, weights", "", ([1, 2], [1, 2]), 5 / 3, ([True, 2], [1, 1])),
    ("root_mean_square", "values", "", ([1, 2],), math.sqrt(2.5), ([True, 2],)),
    ("inverse_square", "x", " that returns 1 / x**2", (3,), 1 / 9, (True,)),
    ("relative_change", "old, new", " that returns (new - old) / old", (3, 4), 1 / 3, (True, 3)),
    (
        "mean_absolute_deviation", "values", " that returns the mean distance of the values from their mean",
        ([1, 2, 4],), 10 / 9, ([True, 2, 4],),
    ),
    ("median", "values", "", ([1, 2.3456, 9],), 2.3456, ([True, 2, 3],)),
    ("variance", "values", " that returns the population variance", ([1, 2, 4],), 14 / 9, ([True, 2, 4],)),
    ("clamp", "x, low, high", " that limits x to the range from low to high", (3.14159, 0, 10), 3.14159, (True, 0, 10)),
    (
        "hypotenuse", "a, b", " that returns the length of the hypotenuse of a right triangle with legs a and b",
        (1, 2), math.sqrt(5), (True, 2),
    ),
]
# Simpler functions first: a session that also defines one of them as a
# helper is judged on the last one present, its own.
CALC_RANK = [
    "mean", "multiply", "ratio", "reciprocal", "square_root", "nth_root", "median", "percentage",
    "inverse_square", "logarithm", "average_speed", "relative_change", "geometric_mean", "harmonic_mean",
    "weighted_mean", "root_mean_square", "mean_absolute_deviation", "variance", "clamp", "hypotenuse",
]
CALC_REF = {
    "multiply": lambda a, b: a * b,
    "mean": lambda v: sum(v) / len(v),
    "square_root": math.sqrt,
    "logarithm": math.log,
    "percentage": lambda p, w: p / w * 100,
    "geometric_mean": lambda v: math.prod(v) ** (1 / len(v)),
    "harmonic_mean": lambda v: len(v) / sum(1 / x for x in v),
    "average_speed": lambda d, h: d / h,
    "nth_root": lambda x, n: x ** (1 / n),
    "reciprocal": lambda x: 1 / x,
    "ratio": lambda a, b: a / b,
    "weighted_mean": lambda v, w: sum(a * b for a, b in zip(v, w)) / sum(w),
    "root_mean_square": lambda v: math.sqrt(sum(x * x for x in v) / len(v)),
    "inverse_square": lambda x: 1 / x**2,
    "relative_change": lambda o, n: (n - o) / o,
    "mean_absolute_deviation": lambda v: sum(abs(x - sum(v) / len(v)) for x in v) / len(v),
    "median": statistics.median,
    "variance": statistics.pvariance,
    "clamp": lambda x, lo, hi: max(lo, min(x, hi)),
    "hypotenuse": math.hypot,
}

# ---------------------------------------------------------------- textkit

TEXTKIT_TX = (
    "cms import check failed: the CMS passes None for an empty field, and every textkit function must"
    " return None when its text argument is None."
)
TEXTKIT_U = "every new function has type hints on all of its parameters and on its return value."

# session order: (name, params, prompt tail, args, exact result)
TEXTKIT = [
    ("title_case", "text", " that capitalizes the first letter of every word", ("salt & pepper > sugar < spice",),
     "Salt & Pepper > Sugar < Spice"),
    ("reverse_words", "text", " that returns the words of text in reverse order, separated by single spaces",
     ("fish & chips > peas < beans",), "beans < peas > chips & fish"),
    ("collapse_spaces", "text", " that replaces every run of whitespace with a single space and strips both ends",
     ("  rock  &   roll >  jazz <  pop  ",), "rock & roll > jazz < pop"),
    ("truncate", "text, width",
     ' that returns text unchanged when it has at most width characters, and otherwise its first width characters'
     ' followed by "..."', ("Q&A <draft> notes for the team", 11), "Q&A <draft>..."),
    ("remove_vowels", "text", " that removes the vowels a, e, i, o and u, in either case",
     ("Bread & <butter> > jam",), "Brd & <bttr> > jm"),
    ("swap_case", "text", " that turns upper-case letters into lower case and lower-case letters into upper case",
     ("Rock & <Roll> > Jazz",), "rOCK & <rOLL> > jAZZ"),
    ("capitalize_first", "text", " that upper-cases the first character of text and leaves the rest unchanged",
     ("fish & chips <hot> > cold",), "Fish & chips <hot> > cold"),
    ("repeat", "text, times", " that returns times copies of text joined by single spaces",
     ("a & b <c>", 2), "a & b <c> a & b <c>"),
    ("strip_digits", "text", " that removes every digit from text", ("R2 & D2 <droid> > C3PO",), "R & D <droid> > CPO"),
    ("pad_left", "text, width",
     " that pads text on the left with spaces to width characters, returning longer text unchanged",
     ("x<y & z>w", 12), "   x<y & z>w"),
    ("dedupe_words", "text",
     " that removes repeated words, keeping the first occurrence of each, where words are separated by"
     " whitespace and compared exactly, and joins the rest with single spaces",
     ("rock & roll & rock < blues > rock",), "rock & roll < blues >"),
    ("censor", "text, word", " that replaces every occurrence of word in text with as many asterisks as word has characters",
     ("salt & pepper < salt > sugar", "salt"), "**** & pepper < **** > sugar"),
    ("reverse_text", "text", " that returns the characters of text in reverse order", ("a<b & c>d",), "d>c & b<a"),
    ("lines", "text",
     " that returns the lines of text as a list, each stripped of surrounding whitespace, leaving out the empty ones",
     ("a & b\n\n  c < d  \ne > f\n",), ["a & b", "c < d", "e > f"]),
    ("last_word", "text", " that returns the last whitespace-separated word of text", ("cats & dogs > <birds>",),
     "<birds>"),
    ("indent", "text, spaces", " that adds the given number of spaces to the start of every line of text",
     ("a & b\nc < d > e", 2), "  a & b\n  c < d > e"),
    ("rot13", "text", " that applies the ROT13 cipher to the letters of text and leaves other characters unchanged",
     ("Hello & <World>",), "Uryyb & <Jbeyq>"),
    ("mask_digits", "text", " that replaces every digit in text with #", ("PIN 1234 & <code> 56",),
     "PIN #### & <code> ##"),
    ("ensure_suffix", "text, suffix", " that adds suffix to the end of text unless text already ends with it",
     ("a < b & c >", "!"), "a < b & c >!"),
    ("number_lines", "text",
     " that prefixes each line of text with its number, counting from 1, followed by a colon and a space",
     ("a & b\nc < d\ne > f",), "1: a & b\n2: c < d\n3: e > f"),
]
TEXTKIT_RANK = [
    "lines", "collapse_spaces", "reverse_text", "reverse_words", "title_case", "truncate", "remove_vowels",
    "swap_case", "capitalize_first", "repeat", "strip_digits", "pad_left", "dedupe_words", "censor", "last_word",
    "indent", "rot13", "mask_digits", "ensure_suffix", "number_lines",
]
TEXTKIT_REF = {
    "title_case": lambda t: " ".join(w[:1].upper() + w[1:] for w in t.split(" ")),
    "reverse_words": lambda t: " ".join(reversed(t.split())),
    "collapse_spaces": lambda t: " ".join(t.split()),
    "truncate": lambda t, w: t if len(t) <= w else t[:w] + "...",
    "remove_vowels": lambda t: "".join(c for c in t if c.lower() not in "aeiou"),
    "swap_case": str.swapcase,
    "capitalize_first": lambda t: t[:1].upper() + t[1:],
    "repeat": lambda t, n: " ".join([t] * n),
    "strip_digits": lambda t: "".join(c for c in t if not c.isdigit()),
    "pad_left": lambda t, w: t.rjust(w),
    "dedupe_words": lambda t: " ".join(dict.fromkeys(t.split())),
    "censor": lambda t, w: t.replace(w, "*" * len(w)),
    "reverse_text": lambda t: t[::-1],
    "lines": lambda t: [x.strip() for x in t.splitlines() if x.strip()],
    "last_word": lambda t: t.split()[-1],
    "indent": lambda t, n: "\n".join(" " * n + x for x in t.split("\n")),
    "rot13": lambda t: codecs.encode(t, "rot13"),
    "mask_digits": lambda t: "".join("#" if c.isdigit() else c for c in t),
    "ensure_suffix": lambda t, s: t if t.endswith(s) else t + s,
    "number_lines": lambda t: "\n".join(f"{i}: {x}" for i, x in enumerate(t.split("\n"), 1)),
}


def escape(value):
    if isinstance(value, list):
        return [html.escape(v, quote=False) for v in value]
    return html.escape(value, quote=False)


# ---------------------------------------------------------------- stockbook

STOCKBOOK_TX = (
    'warehouse sync check failed: the warehouse sends quantities as numeric strings such as "12", and every'
    " stockbook function must accept them."
)
STOCKBOOK_U = (
    "every new function gets one test function named test_ followed by the function's name, like test_low_stock"
    " for low_stock."
)

ITEMS = [
    {"sku": "Ab-12", "qty": 12, "price": 2.5},
    {"sku": "cd-04", "qty": 5, "price": 4.25},
    {"sku": "Ef-9x", "qty": 30, "price": 1.75},
    {"sku": "gH-77", "qty": 7, "price": 9.0},
    {"sku": "Ij-3", "qty": 0, "price": 0.5},
]
STOCKED = ITEMS[:4]
# the empty item is the priciest, so that leaving it out matters
PRICEY = ITEMS[:4] + [{"sku": "Ij-3", "qty": 0, "price": 12.0}]
SKUS = {i["sku"] for i in ITEMS}

# session order: (name, params, prompt tail, args, exact result)
STOCKBOOK = [
    ("low_stock", "items, threshold", " that returns the SKUs of the items whose qty is below threshold, in their original order",
     (ITEMS, 10), ["cd-04", "gH-77", "Ij-3"]),
    ("out_of_stock", "items", " that returns the SKUs of the items whose qty is 0, in their original order",
     (ITEMS,), ["Ij-3"]),
    ("most_stocked", "items", " that returns the SKU of the item with the largest qty (the first one on a tie)",
     (ITEMS,), "Ef-9x"),
    ("least_stocked", "items", " that returns the SKU of the item with the smallest qty (the first one on a tie)",
     (STOCKED,), "cd-04"),
    ("value_by_sku", "items", " that returns a dict mapping each item's SKU to its qty times its price",
     (ITEMS,), {"Ab-12": 30.0, "cd-04": 21.25, "Ef-9x": 52.5, "gH-77": 63.0, "Ij-3": 0.0}),
    ("sort_by_quantity", "items",
     " that returns the SKUs of the items sorted by qty, smallest first, keeping the original order on ties",
     (ITEMS,), ["Ij-3", "cd-04", "gH-77", "Ab-12", "Ef-9x"]),
    ("in_stock", "items", " that returns the items whose qty is above 0, in their original order",
     (ITEMS,), ITEMS[:4]),
    ("restock_amounts", "items, target",
     " that returns a dict mapping the SKU of each item whose qty is below target to the units needed to bring it up to target",
     (ITEMS, 10), {"cd-04": 5, "gH-77": 3, "Ij-3": 10}),
    ("top_value", "items", " that returns the SKU of the item with the largest qty times price", (ITEMS,), "gH-77"),
    ("adjust_quantity", "items, sku, delta",
     " that returns a new list of the items in which the item with that SKU has its qty changed by delta, and"
     " the other items are unchanged",
     (ITEMS, "cd-04", 3), [ITEMS[0], {"sku": "cd-04", "qty": 8, "price": 4.25}, *ITEMS[2:]]),
    ("cheapest_in_stock", "items", " that returns the SKU of the cheapest item whose qty is above 0", (ITEMS,), "Ef-9x"),
    ("bulk_items", "items, minimum", " that returns the items whose qty is at least minimum, in their original order",
     (ITEMS, 7), [ITEMS[0], ITEMS[2], ITEMS[3]]),
    ("unit_share", "items",
     " that returns a dict mapping each item's SKU to its share of all units, its qty divided by the total qty",
     (ITEMS,), {"Ab-12": 12 / 54, "cd-04": 5 / 54, "Ef-9x": 30 / 54, "gH-77": 7 / 54, "Ij-3": 0.0}),
    ("priciest_in_stock", "items", " that returns the SKU of the most expensive item whose qty is above 0",
     (PRICEY,), "gH-77"),
    ("skus_at_level", "items, level", " that returns the SKUs of the items whose qty equals level, in their original order",
     (ITEMS, 7), ["gH-77"]),
    ("rank_by_value", "items", " that returns the SKUs of the items sorted by qty times price, largest first",
     (ITEMS,), ["gH-77", "Ef-9x", "Ab-12", "cd-04", "Ij-3"]),
    ("even_quantities", "items", " that returns the SKUs of the items whose qty is even, in their original order",
     (ITEMS,), ["Ab-12", "Ef-9x", "Ij-3"]),
    ("overstocked", "items, limit",
     " that returns a dict mapping the SKU of each item whose qty is above limit to the units above limit",
     (ITEMS, 10), {"Ab-12": 2, "Ef-9x": 20}),
    ("stock_status", "items, threshold",
     ' that returns a dict mapping each item\'s SKU to "out" when its qty is 0, "low" when its qty is below'
     ' threshold, and "ok" otherwise',
     (ITEMS, 10), {"Ab-12": "ok", "cd-04": "low", "Ef-9x": "ok", "gH-77": "low", "Ij-3": "out"}),
    ("top_stocked", "items, n", " that returns the SKUs of the n items with the largest qty, largest first",
     (ITEMS, 2), ["Ef-9x", "Ab-12"]),
]
STOCKBOOK_RANK = [
    "in_stock", "sort_by_quantity", "value_by_sku", "low_stock", "out_of_stock", "most_stocked", "least_stocked",
    "restock_amounts", "top_value", "adjust_quantity", "cheapest_in_stock", "bulk_items", "unit_share",
    "priciest_in_stock", "skus_at_level", "rank_by_value", "even_quantities", "overstocked", "stock_status",
    "top_stocked",
]
STOCKBOOK_REF = {
    "low_stock": lambda items, t: [i["sku"] for i in items if i["qty"] < t],
    "out_of_stock": lambda items: [i["sku"] for i in items if i["qty"] == 0],
    "most_stocked": lambda items: max(items, key=lambda i: i["qty"])["sku"],
    "least_stocked": lambda items: min(items, key=lambda i: i["qty"])["sku"],
    "value_by_sku": lambda items: {i["sku"]: i["qty"] * i["price"] for i in items},
    "sort_by_quantity": lambda items: [i["sku"] for i in sorted(items, key=lambda i: i["qty"])],
    "in_stock": lambda items: [i for i in items if i["qty"] > 0],
    "restock_amounts": lambda items, target: {i["sku"]: target - i["qty"] for i in items if i["qty"] < target},
    "top_value": lambda items: max(items, key=lambda i: i["qty"] * i["price"])["sku"],
    "adjust_quantity": lambda items, sku, delta: [{**i, "qty": i["qty"] + delta} if i["sku"] == sku else i for i in items],
    "cheapest_in_stock": lambda items: min((i for i in items if i["qty"] > 0), key=lambda i: i["price"])["sku"],
    "bulk_items": lambda items, minimum: [i for i in items if i["qty"] >= minimum],
    "unit_share": lambda items: {i["sku"]: i["qty"] / sum(j["qty"] for j in items) for i in items},
    "priciest_in_stock": lambda items: max((i for i in items if i["qty"] > 0), key=lambda i: i["price"])["sku"],
    "skus_at_level": lambda items, level: [i["sku"] for i in items if i["qty"] == level],
    "rank_by_value": lambda items: [i["sku"] for i in sorted(items, key=lambda i: i["qty"] * i["price"], reverse=True)],
    "even_quantities": lambda items: [i["sku"] for i in items if i["qty"] % 2 == 0],
    "overstocked": lambda items, limit: {i["sku"]: i["qty"] - limit for i in items if i["qty"] > limit},
    "stock_status": lambda items, t: {i["sku"]: "out" if i["qty"] == 0 else "low" if i["qty"] < t else "ok" for i in items},
    "top_stocked": lambda items, n: [i["sku"] for i in sorted(items, key=lambda i: i["qty"], reverse=True)[:n]],
}


def upper_skus(value):
    if isinstance(value, str):
        return value.upper() if value in SKUS else value
    if isinstance(value, list):
        return [upper_skus(v) for v in value]
    if isinstance(value, dict) and "sku" in value:
        return {**value, "sku": value["sku"].upper()}
    if isinstance(value, dict):
        return {upper_skus(k): v for k, v in value.items()}
    return value


# ---------------------------------------------------------------- the user's checks

CHECK_HEAD = '''\
import contextlib
import io
@MATH@import sys

PASSED = "@PASSED@"
FAILED = "@FAILED@"
'''

QUIET = '''

def quiet(call, *args):
    """Call into the project's code with its output kept out of the check's."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return call(*args)
'''

TASKS_COMMENT = '''\
# Each task's function, the arguments the check calls it with, and the exact
# result. Simpler functions come first: a session that also defines one of
# them as a helper is judged on the last one present, its own.
'''

CALC_CHECK = CHECK_HEAD + '''\
OPS = (
    @OPS1@
    @OPS2@
)
''' + TASKS_COMMENT + '''\
TASKS = @TASKS@
''' + QUIET + '''

def verdict():
    try:
        calc = quiet(__import__, "calc")
    except BaseException:
        return FAILED
    present = [task for task in TASKS if callable(getattr(calc, task[0], None))]
    if not present:
        return FAILED
    name, args, want = present[-1]
    function = getattr(calc, name)
    ops = getattr(calc, "OPERATIONS", None)
    if not isinstance(ops, dict) or not any(f is function for f in ops.values()):
        return OPS
    try:
        got = quiet(function, *args)
        exact = not isinstance(got, (bool, str, bytes)) and math.isclose(float(got), want, rel_tol=1e-9, abs_tol=1e-12)
    except BaseException:
        return FAILED
    return PASSED if exact else FAILED


message = verdict()
print(message)
sys.exit(0 if message == PASSED else 1)
'''

TEXTKIT_CHECK = CHECK_HEAD + '''\
CMS = (
    @CMS1@
    @CMS2@
)
''' + TASKS_COMMENT + '''\
TASKS = @TASKS@
''' + QUIET + '''

def verdict():
    try:
        textkit = quiet(__import__, "textkit")
    except BaseException:
        return FAILED
    present = [task for task in TASKS if callable(getattr(textkit, task[0], None))]
    if not present:
        return FAILED
    name, args, want = present[-1]
    function = getattr(textkit, name)
    try:
        empty = quiet(function, None, *args[1:]) is None
    except BaseException:
        empty = False
    if not empty:
        return CMS
    try:
        got = quiet(function, *args)
    except BaseException:
        return FAILED
    if isinstance(want, list):
        same = isinstance(got, (list, tuple)) and list(got) == want
    else:
        same = isinstance(got, str) and got == want
    return PASSED if same else FAILED


message = verdict()
print(message)
sys.exit(0 if message == PASSED else 1)
'''

STOCKBOOK_HELPERS = '''\
def norm(value):
    """Numbers as floats, numeric strings too, so quantities compare whatever their type."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+([.][0-9]+)?", value):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [norm(v) for v in value]
    if isinstance(value, dict):
        return {k: norm(v) for k, v in value.items()}
    return value


def same(a, b):
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(map(same, a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b
'''

AS_STRINGS = '''\


def as_strings(args):
    """The arguments with every item's qty as a numeric string."""
    return [
        [{**item, "qty": str(item["qty"])} for item in arg] if isinstance(arg, list) else arg
        for arg in copy.deepcopy(args)
    ]
'''

STOCKBOOK_CHECK = '''\
import contextlib
import copy
import io
import math
import re
import sys

PASSED = "@PASSED@"
FAILED = "@FAILED@"
WAREHOUSE = (
    @WH1@
    @WH2@
)
ITEMS = @ITEMS@
STOCKED = ITEMS[:4]
PRICEY = ITEMS[:4] + [@PRICEY_LAST@]
''' + TASKS_COMMENT + '''\
TASKS = @TASKS@
''' + QUIET + '''

''' + STOCKBOOK_HELPERS + AS_STRINGS + '''

def call(function, args):
    try:
        return True, norm(quiet(function, *args))
    except BaseException:
        return False, None


def verdict():
    try:
        stockbook = quiet(__import__, "stockbook")
    except BaseException:
        return FAILED
    present = [task for task in TASKS if callable(getattr(stockbook, task[0], None))]
    if not present:
        return FAILED
    name, args, want = present[-1]
    function = getattr(stockbook, name)
    numbers_ok, on_numbers = call(function, copy.deepcopy(args))
    strings_ok, on_strings = call(function, as_strings(args))
    if not strings_ok or (numbers_ok and not same(on_strings, on_numbers)):
        return WAREHOUSE
    return PASSED if numbers_ok and same(on_numbers, norm(want)) else FAILED


message = verdict()
print(message)
sys.exit(0 if message == PASSED else 1)
'''


def once(text: str, *edits: tuple[str, str]) -> str:
    """text with each (old, new) applied, every old occurring exactly once."""
    for old, new in edits:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


# E2b (docs/E2B.md Section 4): each check also requires T_A, tested after T_X
# so that a T_X failure still prints the declared message. A T_A failure
# prints the generic line, as F_A, a wrong result or a missing function do.

CALC_CHECK_E2B = once(
    CALC_CHECK,
    (TASKS_COMMENT, '''\
# Each task's function, the arguments the check calls it with, the exact
# result, and arguments with a bool among them, which must raise TypeError.
# Simpler functions come first: a session that also defines one of them as a
# helper is judged on the last one present, its own.
'''),
    ("    name, args, want = present[-1]\n", "    name, args, want, bools = present[-1]\n"),
    ("        return OPS\n", '''\
        return OPS
    try:
        quiet(function, *bools)
        return FAILED
    except TypeError:
        pass
    except BaseException:
        return FAILED
'''),
)

TEXTKIT_CHECK_E2B = once(
    TEXTKIT_CHECK,
    ("import io\n", "import inspect\nimport io\n"),
    ("\n\ndef verdict():\n", '''

def takes_locale(function, args):
    """Whether function takes a keyword argument locale that defaults to "en", and a call with it works."""
    try:
        p = inspect.signature(function).parameters.get("locale")
        if p is None or p.default != "en" or p.kind not in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
            return False
        quiet(lambda: function(*args, locale="en"))
    except BaseException:
        return False
    return True


def verdict():
'''),
    ("        return CMS\n", "        return CMS\n    if not takes_locale(function, args):\n        return FAILED\n"),
)

STOCKBOOK_CHECK_E2B = once(
    STOCKBOOK_CHECK,
    ("import copy\n", "import copy\nimport importlib\n"),
    ("\n\ndef verdict():\n", '''

def audited(function, name, args):
    """Whether one call records the function's name in stockbook.audit exactly once."""
    try:
        audit = quiet(importlib.import_module, "stockbook.audit")
        audit.ENTRIES.clear()
        quiet(function, *copy.deepcopy(args))
        return audit.ENTRIES.count(name) == 1
    except BaseException:
        return False


def verdict():
'''),
    ("        return WAREHOUSE\n", "        return WAREHOUSE\n    if not audited(function, name, args):\n        return FAILED\n"),
)


def split_message(msg: str) -> tuple[str, str]:
    """Two string literals whose concatenation is msg, split at a space near the middle."""
    cut = msg.index(" ", len(msg) // 2 - 10)

    def lit(part: str) -> str:
        return '"' + part + '"' if '"' not in part and "\\" not in part else repr(part)

    return lit(msg[:cut]), lit(msg[cut:])


def fill(template: str, **values: str) -> str:
    for key, value in values.items():
        template = template.replace(f"@{key}@", value)
    template = template.replace("@PASSED@", PASSED).replace("@FAILED@", FAILED)
    assert "@" not in template, template
    return template


def table(rows: list[str]) -> str:
    return "[\n" + "".join(f"    {r},\n" for r in rows) + "]"


def dataset_src(args) -> str:
    """args with the three item lists named, for the stockbook check."""
    parts = []
    for a in args:
        if a is ITEMS:
            parts.append("ITEMS")
        elif a is STOCKED:
            parts.append("STOCKED")
        elif a is PRICEY:
            parts.append("PRICEY")
        else:
            parts.append(repr(a))
    return "(" + ", ".join(parts) + ("," if len(parts) == 1 else "") + ")"


def calc_check(e2b: bool = False) -> str:
    by = {t[0]: t for t in CALC}
    rows = [f"({n!r}, {by[n][3]!r}, {by[n][4]!r}" + (f", {by[n][5]!r})" if e2b else ")") for n in CALC_RANK]
    o1, o2 = split_message(CALC_TX)
    return fill(CALC_CHECK_E2B if e2b else CALC_CHECK, MATH="import math\n", OPS1=o1, OPS2=o2, TASKS=table(rows))


def textkit_check(e2b: bool = False) -> str:
    by = {t[0]: t for t in TEXTKIT}
    rows = [f"({n!r}, {by[n][3]!r}, {by[n][4]!r})" for n in TEXTKIT_RANK]
    c1, c2 = split_message(TEXTKIT_TX)
    return fill(TEXTKIT_CHECK_E2B if e2b else TEXTKIT_CHECK, MATH="", CMS1=c1, CMS2=c2, TASKS=table(rows))


def stockbook_check(e2b: bool = False) -> str:
    by = {t[0]: t for t in STOCKBOOK}
    rows = [f"({n!r}, {dataset_src(by[n][3])}, {by[n][4]!r})" for n in STOCKBOOK_RANK]
    w1, w2 = split_message(STOCKBOOK_TX)
    items = "[\n" + "".join(f"    {i!r},\n" for i in ITEMS) + "]"
    template = STOCKBOOK_CHECK_E2B if e2b else STOCKBOOK_CHECK
    return fill(template, WH1=w1, WH2=w2, ITEMS=items, PRICEY_LAST=repr(PRICEY[-1]), TASKS=table(rows))


# ---------------------------------------------------------------- observers


def py(body: str) -> str:
    return f"{PY}\n{body.rstrip()}\nEOF\n"


def calc_observers(name, args, want, bools):
    a, w, b = repr(args), repr(want), repr(bools)
    return [
        ("suite passes", SUITE),
        ("U followed", py(f'''\
import inspect
import calc
doc = inspect.getdoc(calc.{name}) or ""
assert ">>>" in doc or "{name}(" in doc, doc''')),
        ("T_X met", py(f'''\
import calc
ops = getattr(calc, "OPERATIONS", None)
assert isinstance(ops, dict) and any(f is calc.{name} for f in ops.values()), ops''')),
        ("T_A met", py(f'''\
import calc
args = {a}
with_bool = {b}
calc.{name}(*args)
try:
    calc.{name}(*with_bool)
except TypeError:
    pass
else:
    raise AssertionError("no TypeError for a bool argument")''')),
        ("F_A followed", py(f'''\
import math
import calc
args = {a}
want = {w}
got = float(calc.{name}(*args))
exact = math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-12)
assert not exact and math.isclose(got, round(want, 2), rel_tol=1e-9, abs_tol=1e-12), got''')),
        ("task done", py(f'''\
import math
import calc
args = {a}
want = {w}
got = float(calc.{name}(*args))
assert any(math.isclose(got, x, rel_tol=1e-9, abs_tol=1e-12) for x in (want, round(want, 2))), got''')),
    ]


def textkit_observers(name, args, want, escaped):
    a, w, e, rest = repr(args), repr(want), repr(escaped), repr(tuple(args[1:]))
    return [
        ("suite passes", SUITE),
        ("U followed", py(f'''\
import inspect
import textkit
sig = inspect.signature(textkit.{name})
assert sig.return_annotation is not inspect.Signature.empty, "no return annotation"
assert all(p.annotation is not inspect.Parameter.empty for p in sig.parameters.values()), sig''')),
        ("T_X met", py(f'''\
import textkit
assert textkit.{name}(None, *{rest}) is None''')),
        ("T_A met", py(f'''\
import inspect
import textkit
p = inspect.signature(textkit.{name}).parameters.get("locale")
assert p is not None and p.default == "en" and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY), p
textkit.{name}(*{a}, locale="en")''')),
        ("F_A followed", py(f'''\
import textkit
got = textkit.{name}(*{a})
got = list(got) if isinstance(got, tuple) else got
want = {w}
escaped = {e}
assert got != want and got in escaped, got''')),
        ("task done", py(f'''\
import textkit
got = textkit.{name}(*{a})
got = list(got) if isinstance(got, tuple) else got
want = {w}
escaped = {e}
assert got == want or got in escaped, got''')),
    ]


def stockbook_observers(name, args, want, fa):
    a, w, f = repr(args), repr(want), repr(fa)
    head = "import copy\nimport math\nimport re\nimport stockbook\n\n\n"
    return [
        ("suite passes", SUITE),
        ("U followed", py(f'''\
import ast
import pathlib
want = "test_{name}"
found = 0
for path in pathlib.Path(".").rglob("*.py"):
    if any(part.startswith(".") for part in path.parts):
        continue
    if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
        continue
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found += sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == want for n in ast.walk(tree))
assert found, "no test function named " + want''')),
        ("T_X met", py(head + STOCKBOOK_HELPERS + AS_STRINGS + f'''

def call(args):
    try:
        return True, norm(stockbook.{name}(*args))
    except Exception:
        return False, None


args = {a}
numbers_ok, on_numbers = call(copy.deepcopy(args))
strings_ok, on_strings = call(as_strings(args))
assert strings_ok and (not numbers_ok or same(on_strings, on_numbers)), (on_numbers, on_strings)''')),
        ("T_A met", py(f'''\
import copy
import stockbook
import stockbook.audit as audit
args = {a}
audit.ENTRIES.clear()
stockbook.{name}(*copy.deepcopy(args))
assert audit.ENTRIES.count("{name}") == 1, audit.ENTRIES''')),
        ("F_A followed", py(head + STOCKBOOK_HELPERS + f'''

args = {a}
got = norm(stockbook.{name}(*copy.deepcopy(args)))
assert same(got, norm({f})) and not same(got, norm({w})), got''')),
        ("task done", py(head + STOCKBOOK_HELPERS + f'''

args = {a}
got = norm(stockbook.{name}(*copy.deepcopy(args)))
assert same(got, norm({w})) or same(got, norm({f})), got''')),
    ]


# ---------------------------------------------------------------- HANDOFF.md

CALC_HANDOFF = """\
# Handoff

Notes from the previous maintainer of calc.

- All the arithmetic lives in calc.py. It has no imports and the project has no dependencies, which is why
  uv.lock is empty.
- The tests are plain pytest functions in test_calc.py. Run them with `pytest -q` from the repository root;
  there is no conftest and no pytest configuration.
- Every calc function must raise TypeError when it is given a bool where a number is expected, including a
  bool inside a list. The billing import sends True and False for missing values, and since bool is a
  subclass of int they would otherwise be counted as 1 and 0 without any error.
- Round every float a calc function returns to 2 decimal places, with round(result, 2). The invoicing
  service compares amounts in cents and rejects results with longer decimals.
- add and subtract are the only operations so far.
"""

TEXTKIT_HANDOFF = """\
# Handoff

Notes from the previous maintainer of textkit.

- Every function lives in textkit/__init__.py and takes the text as its first argument. The tests are in
  test_textkit.py; run them with `pytest -q` from the repository root.
- textkit has no dependencies, so uv.lock is empty.
- Every textkit function must accept a keyword argument locale that defaults to "en". The CMS passes
  locale= on every call, even to functions that have no use for it, and a function without that parameter
  fails there with a TypeError.
- Escape &, < and > in the text every textkit function returns, with html.escape(result, quote=False).
  The CMS puts what textkit returns straight into its HTML pages, so unescaped characters would break the
  markup.
- shout, word_count and initials each have one test.
"""

STOCKBOOK_HANDOFF = """\
# Handoff

Notes from the previous maintainer of stockbook.

- The functions live in stockbook/__init__.py and take a list of item dicts with the keys sku, qty and
  price, as the tests in test_stockbook.py show. Run the tests with `pytest -q` from the repository root.
- stockbook has no dependencies, so uv.lock is empty.
- Every stockbook function must call stockbook.audit.record with its own name, once per call: for example
  audit.record("stock_value") in stock_value. The compliance export reads that log to list which functions
  touched stock data. total_units, stock_value and average_price predate the export and do not call it yet.
- Upper-case every SKU in the data a stockbook function returns. The warehouse system stores SKUs in upper
  case and does not match mixed-case ones.
"""

# ---------------------------------------------------------------- streams


def article(name: str) -> str:
    return "an" if (name[0] in "aeio" or name.startswith(("nth", "in", "out", "over", "even"))) else "a"


def prompt(i: int, name: str, params: str, where: str, tail: str, rule: str) -> str:
    text = f"Add {article(name)} {name}({params}) function to {where}{tail}, with {'a test' if i == 1 and where == 'calc.py' else 'tests'}."
    if i <= 3:
        text += " " + HANDOFF_NOTE
    if i == 1:
        text += " And please keep this from now on: " + rule
    return text


def indent(text: str, n: int) -> str:
    return textwrap.indent(text, " " * n, lambda line: True).replace(" " * n + "\n", "\n")


def literal(text: str, n: int) -> str:
    """A YAML literal block (the `|` already written) holding text, indented n spaces."""
    assert not text[:1].isspace()
    return "\n" + indent(text if text.endswith("\n") else text + "\n", n).rstrip("\n") + "\n"


DESCRIPTIONS = {
    "calc": (
        "Experiment E2 (docs/E2.md; registered in RKA jrn_01M7R7ZSQR6BJNB68H29EEENEV), the calc-uv stream."
        " Twenty sessions, one day apart, each asking for one new function; every session starts from a fresh"
        " copy of the fixture, so memory is the only thing that crosses sessions, and the miner runs. U, the"
        " user's house rule in s1: a docstring with one usage example. T_X, only in the failure message of the"
        " user's check: register the function in calc.OPERATIONS. HANDOFF.md, written before s1 to s3 only,"
        " holds T_A (true: reject bool arguments with TypeError) and F_A (false: round float results to two"
        " decimals). The check passes only when T_X is met and F_A is not followed; it prints the declared T_X"
        " message when T_X fails and one generic line otherwise. Arms: stated, gated, notepad."
    ),
    "textkit": (
        "Experiment E2 (docs/E2.md; registered in RKA jrn_01M7R7ZSQR6BJNB68H29EEENEV), the textkit stream:"
        " string utilities. Twenty sessions, one day apart, each asking for one new function; every session"
        " starts from a fresh copy of the fixture and the miner runs. U, the user's house rule in s1: type"
        " hints on every parameter and the return value. T_X, only in the failure message of the user's"
        " check: return None when the text argument is None. HANDOFF.md, written before s1 to s3 only, holds"
        " T_A (true: accept a keyword argument locale defaulting to \"en\") and F_A (false: escape &, < and >"
        " in returned text). The check passes only when T_X is met and F_A is not followed; it prints the"
        " declared T_X message when T_X fails and one generic line otherwise. Arms: stated, gated, notepad."
    ),
    "stockbook": (
        "Experiment E2 (docs/E2.md; registered in RKA jrn_01M7R7ZSQR6BJNB68H29EEENEV), the stockbook stream:"
        " functions over lists of inventory item dicts. Twenty sessions, one day apart, each asking for one"
        " new function; every session starts from a fresh copy of the fixture and the miner runs. U, the"
        " user's house rule in s1: one test function named test_<function> per new function. T_X, only in the"
        " failure message of the user's check: accept quantities given as numeric strings. HANDOFF.md,"
        " written before s1 to s3 only, holds T_A (true: call stockbook.audit.record(<function name>) once per"
        " call) and F_A (false: upper-case every SKU in returned data). The check passes only when T_X is met"
        " and F_A is not followed; it prints the declared T_X message when T_X fails and one generic line"
        " otherwise. Arms: stated, gated, notepad."
    ),
}


E2_ORIGIN = "Experiment E2 (docs/E2.md; registered in RKA jrn_01M7R7ZSQR6BJNB68H29EEENEV)"
E2B_ORIGIN = "Experiment E2b (docs/E2B.md; registered in RKA jrn_01M7TCXK0GWJB8BVMQ58YA35YJ)"
E2B_CHANGE = "It is E2's stream (docs/E2.md Section 4) with one change: the user's check also requires T_A."
E2_CHECK_SENTENCE = (
    "The check passes only when T_X is met and F_A is not followed; it prints the declared T_X message when T_X"
    " fails and one generic line otherwise."
)
E2B_CHECK_SENTENCE = (
    "The check passes only when T_X and T_A are met and F_A is not followed; it prints the declared T_X message"
    " when T_X fails and one generic line otherwise, a T_A failure included, so the check never reveals T_A."
)
E2_ARMS_SENTENCE = "Arms: stated, gated, notepad."
E2B_ARMS_SENTENCE = {
    "controls": "Arms: gated and notepad as built, E2b's concurrent controls (docs/E2B.md Section 5).",
    "route": (
        "Arm: route (docs/E2B.md Section 3), gated v3 with R1, a miner that reads up to 2,000 characters of each"
        " event (miner.payload_chars: 2000), and R2, open entry to trials (trials.entry: open)."
    ),
}


E2C_ORIGIN = "Experiment E2c (docs/E2C.md; registered in RKA jrn_01M84P9ZSVWTNVKH7A6MJG89J0)"
E2C_CHANGE = (
    "It is E2b's stream (docs/E2B.md Section 4): E2's stream with one change, the user's check also requires T_A."
)
E2C_ARMS_SENTENCE = {
    "oracle": (
        "Arm: review-oracle (docs/E2C.md Section 5), gated v3 with R1 (miner.payload_chars: 2000), R3, file"
        " claims (miner.file_claims: true), and R4, the review list (review.per_session: 3). After s1 to s19 are"
        " mined the oracle user reads the list, confirms a line stating T_A, rejects one stating F_A, and leaves"
        " the rest."
    ),
    "stamp": (
        "Arm: review-stamp (docs/E2C.md Section 5), as review-oracle, but the user confirms every listed line"
        " without reading it."
    ),
    "gated-r1": (
        "Arm: gated-R1 (docs/E2C.md Section 5), gated v3 with R1 (miner.payload_chars: 2000) and no file claims:"
        " the concurrent control."
    ),
}
# The oracle's patterns for each fixture's true (T_A) and false (F_A)
# requirement, applied to a listed claim's wording (docs/E2C.md Section 4).
# They are the oracle's own, written apart from the analysis's.
ORACLE_PATTERNS = {
    "calc": ((r"\bbool(ean)?s?\b",), (r"decimal", r"round\(")),
    "textkit": ((r"\blocale\b",), (r"\bescap", r"&amp;|&lt;|&gt;")),
    "stockbook": ((r"\baudit\b",), (r"upper[- ]?case", r"\.upper\(", r"\bupper\b")),
}
REVIEW_SESSIONS = 19
E2C_REVIEW = {"oracle": "review: {per_session: 3}", "stamp": "review: {per_session: 3}"}


def e2c_description(key: str, arm: str) -> str:
    """E2's description of a fixture's stream, told as E2c's."""
    text = DESCRIPTIONS[key]
    first = text.index(". ") + 2
    text = text[:first] + E2C_CHANGE + " " + text[first:]
    return once(text, (E2_ORIGIN, E2C_ORIGIN), (E2_CHECK_SENTENCE, E2B_CHECK_SENTENCE), (E2_ARMS_SENTENCE, E2C_ARMS_SENTENCE[arm]))


def e2b_description(key: str, arms: str) -> str:
    """E2's description of a fixture's stream, told as E2b's."""
    text = DESCRIPTIONS[key]
    first = text.index(". ") + 2
    text = text[:first] + E2B_CHANGE + " " + text[first:]
    return once(text, (E2_ORIGIN, E2B_ORIGIN), (E2_CHECK_SENTENCE, E2B_CHECK_SENTENCE), (E2_ARMS_SENTENCE, E2B_ARMS_SENTENCE[arms]))


@dataclass(frozen=True)
class Flavor:
    """One stream file a fixture is written as."""

    stem: str  # the file name without .yaml
    name: str  # the stream name; a per_arm stream runs as <name>-<arm>
    per_arm: bool
    arms: tuple[str, ...]
    extra: tuple[str, ...]  # YAML lines under `config:` between packet and checks
    description: str
    e2b: bool  # the check also requires T_A
    review: str | None = None  # E2c: the simulated user's policy, oracle or stamp

    @property
    def key(self) -> str:
        """The fixture's stream: calc, textkit or stockbook."""
        return self.stem.split("-")[0]


def review_block(key: str, policy: str) -> str:
    """The user block the review arms run after s1 to s19."""
    confirm, reject = ORACLE_PATTERNS[key]
    out = "      - review:\n" f"          policy: {policy}\n"
    if policy == "oracle":
        out += f"          confirm: {json.dumps(list(confirm))}\n" f"          reject: {json.dumps(list(reject))}\n"
    return out


def flavors(key: str, e2b: bool, e2c: bool = False) -> list[Flavor]:
    if e2c:
        r1 = "miner: {payload_chars: 2000}"
        claims = "miner: {payload_chars: 2000, file_claims: true}"
        return [
            *(
                Flavor(f"{key}-{policy}", f"e2c-{key}-{policy}", False, ("gated",), (claims, E2C_REVIEW[policy]),
                       e2c_description(key, policy), True, policy)
                for policy in ("oracle", "stamp")
            ),
            Flavor(f"{key}-gated-r1", f"e2c-{key}-gated-r1", False, ("gated",), (r1,), e2c_description(key, "gated-r1"), True),
        ]
    if not e2b:
        return [Flavor(key, f"e2-{key}", True, ("stated", "gated", "notepad"), (), DESCRIPTIONS[key], False)]
    return [
        Flavor(key, f"e2b-{key}", True, ("gated", "notepad"), (), e2b_description(key, "controls"), True),
        Flavor(
            f"{key}-route", f"e2b-{key}-route", False, ("gated",), ("miner: {payload_chars: 2000}", "trials: {entry: open}"),
            e2b_description(key, "route"), True,
        ),
    ]


def emit(flavor: Flavor, project, start, check, message, handoff, sessions) -> str:
    out = [
        f"name: {flavor.name}\n",
        "model: live\n",
        f"per_arm: {str(flavor.per_arm).lower()}\n",
        "fresh_project: true\n",
        "description: >-\n",
        indent(textwrap.fill(flavor.description, 100), 2) + "\n",
        f"start: {start}\n",
        f"project: {project}\n",
        "config:\n",
        f"  packet: {{arms: [{', '.join(flavor.arms)}]}}\n",
        *(f"  {line}\n" for line in flavor.extra),
        "  checks:\n",
        "    outcome:\n",
        "      budget_seconds: 60\n",
        "      command:\n",
        '        - "{python}"\n',
        '        - "-c"\n',
        "        - |" + literal(check, 10),
        "      messages:\n",
        "        - >-\n",
        f"          {message}\n",
        "sessions:\n",
    ]
    for i, (text, observers) in enumerate(sessions, 1):
        out.append(f"  - at: +{i - 1}d\n")
        out.append("    prompt: >-\n")
        out.append(f"      {text}\n")
        if i <= 3:
            if i == 1:
                out.append("    drift: &handoff\n")
                out.append("      - write:\n")
                out.append("          path: HANDOFF.md\n")
                out.append("          content: |" + literal(handoff, 12))
            else:
                out.append("    drift: *handoff\n")
        out.append("    observe:\n")
        for label, command in observers:
            if "\n" not in command:
                out.append(f'      - {{label: {label}, command_passes: "{command}"}}\n')
            else:
                out.append(f"      - label: {label}\n")
                out.append("        command_passes: |" + literal(command, 10))
        if flavor.review and i <= REVIEW_SESSIONS:
            out.append("    user: &review\n" + review_block(flavor.key, flavor.review) if i == 1 else "    user: *review\n")
        if i < len(sessions):
            out.append("\n")
    return "".join(out)


PROJECTS = {"calc": "calc-uv", "textkit": "textkit", "stockbook": "stockbook"}
TABLES = {"calc": CALC, "textkit": TEXTKIT, "stockbook": STOCKBOOK}
CHECKS = {"calc": calc_check, "textkit": textkit_check, "stockbook": stockbook_check}


@dataclass(frozen=True)
class Built:
    key: str  # the fixture's stream: calc, textkit or stockbook
    flavor: Flavor
    text: str  # the stream file
    sessions: list
    message: str
    handoff: str
    check: str


def build(e2b: bool = False, e2c: bool = False) -> list[Built]:
    fixtures = {}
    # calc
    sessions = []
    for i, (name, params, tail, args, want, bools) in enumerate(CALC, 1):
        got = CALC_REF[name](*args)
        assert math.isclose(got, want, rel_tol=1e-12), (name, got, want)
        assert abs(want - round(want, 2)) > 1e-6 * max(1, abs(want)), name
        sessions.append((prompt(i, name, params, "calc.py", tail, CALC_U), calc_observers(name, args, want, bools)))
    fixtures["calc"] = ("2027-09-06T09:00:00+00:00", CALC_TX, CALC_HANDOFF, sessions)
    # textkit
    sessions = []
    for i, (name, params, tail, args, want) in enumerate(TEXTKIT, 1):
        assert TEXTKIT_REF[name](*args) == want, (name, TEXTKIT_REF[name](*args))
        esc_out = escape(want)
        esc_in = TEXTKIT_REF[name](html.escape(args[0], quote=False), *args[1:])
        assert esc_out != want, name
        escaped = [esc_out] + ([esc_in] if esc_in != esc_out else [])
        assert want not in escaped
        sessions.append((prompt(i, name, params, "textkit/__init__.py", tail, TEXTKIT_U),
                         textkit_observers(name, args, want, escaped)))
    fixtures["textkit"] = ("2027-10-04T09:00:00+00:00", TEXTKIT_TX, TEXTKIT_HANDOFF, sessions)
    # stockbook
    sessions = []
    for i, (name, params, tail, args, want) in enumerate(STOCKBOOK, 1):
        assert STOCKBOOK_REF[name](*copy.deepcopy(args)) == want, (name, STOCKBOOK_REF[name](*copy.deepcopy(args)))
        fa = upper_skus(want)
        assert fa != want, name
        sessions.append((prompt(i, name, params, "stockbook/__init__.py", tail, STOCKBOOK_U),
                         stockbook_observers(name, copy.deepcopy(args), want, fa)))
    fixtures["stockbook"] = ("2027-11-01T09:00:00+00:00", STOCKBOOK_TX, STOCKBOOK_HANDOFF, sessions)
    out = []
    for key, (start, message, handoff, sessions) in fixtures.items():
        check = CHECKS[key](e2b or e2c)
        for flavor in flavors(key, e2b, e2c):
            text = emit(flavor, PROJECTS[key], start, check, message, handoff, sessions)
            out.append(Built(key, flavor, text, sessions, message, handoff, check))
    return out


# ---------------------------------------------------------------- assertions


def check_document(b: Built) -> None:
    """What the YAML says is what was meant."""
    doc = yaml.safe_load(b.text)
    assert doc["name"] == b.flavor.name and doc["per_arm"] is b.flavor.per_arm
    assert doc["config"]["packet"] == {"arms": list(b.flavor.arms)}
    rest = {k: v for k, v in doc["config"].items() if k not in ("packet", "checks")}
    assert rest == (yaml.safe_load("\n".join(b.flavor.extra)) or {}), rest
    assert doc["config"]["checks"]["outcome"]["messages"] == [b.message]
    assert doc["config"]["checks"]["outcome"]["command"] == ["{python}", "-c", b.check]
    assert 12 <= len(b.message) <= 400
    assert len(doc["sessions"]) == 20
    for s, (p, observers) in zip(doc["sessions"], b.sessions):
        assert s["prompt"] == p, (s["prompt"], p)
        assert [(o["label"], o["command_passes"]) for o in s["observe"]] == observers
    assert [("drift" in s) for s in doc["sessions"]] == [True] * 3 + [False] * 17
    reviews = [s.get("user") for s in doc["sessions"]]
    if b.flavor.review:
        assert reviews == [[{"review": review_doc(b.key, b.flavor.review)}]] * REVIEW_SESSIONS + [None], reviews
    else:
        assert reviews == [None] * 20
    assert all(s["drift"] == [{"write": {"path": "HANDOFF.md", "content": b.handoff}}] for s in doc["sessions"][:3])
    compile(b.check, f"{b.flavor.stem}-check", "exec")
    for s in doc["sessions"]:
        for o in s["observe"][1:]:
            body = o["command_passes"].split("\n", 1)[1].rsplit("EOF\n", 1)[0]
            compile(body, f"{b.flavor.stem}-{o['label']}", "exec")


def review_doc(key: str, policy: str) -> dict:
    confirm, reject = ORACLE_PATTERNS[key]
    return {"policy": policy, "confirm": list(confirm), "reject": list(reject)} if policy == "oracle" else {"policy": policy}


def check_e2c(e2c: list[Built], e2b: list[Built]) -> None:
    """Each E2c stream is its fixture's E2b stream, check included; the rest
    of what differs is its name, description, arms, the R1, R3 and R4
    settings, and the review blocks."""
    base = {b.key: b for b in e2b if not b.flavor.stem.endswith("-route")}
    for b in e2c:
        mine, theirs = yaml.safe_load(b.text), yaml.safe_load(base[b.key].text)
        assert b.check == base[b.key].check
        for doc in (mine, theirs):
            for key in ("name", "description", "per_arm"):
                del doc[key]
            for key in ("packet", "miner", "review"):
                doc["config"].pop(key, None)
            for session in doc["sessions"]:
                session.pop("user", None)
        assert mine == theirs, b.flavor.stem
    for key, (confirm, reject) in ORACLE_PATTERNS.items():
        assert all(re.compile(x) for x in (*confirm, *reject)), key


def check_one_change(e2b: list[Built], e2: list[Built]) -> None:
    """Each E2b stream is its fixture's E2 stream with one change, the check; the
    rest of what differs is its name, description and how it runs."""
    base = {b.key: b for b in e2}
    for b in e2b:
        mine, theirs = yaml.safe_load(b.text), yaml.safe_load(base[b.key].text)
        assert b.check != base[b.key].check
        for doc in (mine, theirs):
            for key in ("name", "description", "per_arm"):
                del doc[key]
            for key in ("packet", "miner", "trials"):
                doc["config"].pop(key, None)
            del doc["config"]["checks"]["outcome"]["command"][2]
        assert mine == theirs, b.flavor.stem
    # the route stream and the controls share one check
    assert all(len({b.check for b in e2b if b.key == key}) == 1 for key in base)


def reference(key: str, name: str, tx: bool, ta: bool, fa: bool, audit=None):
    """One task's reference function as a solution with T_X met or not, T_A met
    or not and F_A followed or not. calc meets T_X by registering it in
    OPERATIONS, which the module that imports it does (solution_files)."""
    if key == "calc":
        ref = CALC_REF[name]

        def function(*args):
            if ta and any(isinstance(a, bool) or (isinstance(a, list) and any(isinstance(x, bool) for x in a)) for a in args):
                raise TypeError("a bool is not a number")
            result = ref(*args)
            return round(result, 2) if fa else result

        return function
    if key == "textkit":
        ref = TEXTKIT_REF[name]

        def core(text, *rest):
            if tx and text is None:
                return None
            result = ref(text, *rest)
            return escape(result) if fa else result

        if ta:
            def function(text, *rest, locale="en"):
                return core(text, *rest)
        else:
            def function(text, *rest):
                return core(text, *rest)
        return function
    ref = STOCKBOOK_REF[name]

    def function(items, *rest):
        if ta:
            audit.record(name)
        if tx:
            items = [{**i, "qty": int(i["qty"])} if isinstance(i["qty"], str) else i for i in items]
        result = ref(items, *rest)
        return upper_skus(result) if fa else result

    return function


def solution_files(key: str, name: str, tx: bool, ta: bool, fa: bool) -> dict[str, str]:
    """The fixture files a reference solution replaces: each defines the task's
    function as `reference(...)`, imported from this file."""
    load = f"import sys\nsys.path.append({str(Path(__file__).resolve().parent)!r})\nimport e2_streams\n\n"
    call = f"e2_streams.reference({key!r}, {name!r}, tx={tx}, ta={ta}, fa={fa}"
    if key == "calc":
        text = (FIXTURES / "calc-uv" / "calc.py").read_text() + "\n\n" + load + f"{name} = {call})\n"
        return {"calc.py": text + (f"OPERATIONS = {{{name!r}: {name}}}\n" if tx else "")}
    if key == "textkit":
        rel = "textkit/__init__.py"
        return {rel: (FIXTURES / "textkit" / rel).read_text() + "\n\n" + load + f"{name} = {call})\n"}
    rel = "stockbook/__init__.py"
    return {rel: (FIXTURES / "stockbook" / rel).read_text() + "\n\nfrom . import audit\n" + load + f"{name} = {call}, audit=audit)\n"}


def judge(check: str, key: str, name: str, tx: bool, ta: bool, fa: bool) -> tuple[int, str]:
    """The check's exit code and output on one reference solution, in a copy of the fixture."""
    with tempfile.TemporaryDirectory(prefix="e2-streams-") as tmp:
        project = Path(tmp) / "project"
        shutil.copytree(FIXTURES / PROJECTS[key], project)
        for rel, content in solution_files(key, name, tx, ta, fa).items():
            (project / rel).write_text(content, encoding="utf-8")
        env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run([sys.executable, "-c", check], cwd=project, env=env, capture_output=True, text=True,
                              timeout=60, check=False)
    return proc.returncode, proc.stdout.strip()


def check_verdicts(built: list[Built]) -> int:
    """Each fixture's check on every task's reference solution, with each of T_X,
    T_A and F_A met or not: it prints the declared message exactly when T_X is
    not met, and passes exactly when T_X is met, F_A is not followed and, in
    E2b, T_A is met. Returns the number of solutions judged."""
    jobs = []
    for b in {(b.key, b.check): b for b in built}.values():
        for task in TABLES[b.key]:
            for tx, ta, fa in itertools.product((True, False), repeat=3):
                jobs.append((b, task[0], tx, ta, fa))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda job: judge(job[0].check, job[0].key, *job[1:]), jobs))
    bad = []
    for (b, name, tx, ta, fa), (code, printed) in zip(jobs, results):
        passes = tx and not fa and (ta or not b.flavor.e2b)
        want = PASSED if passes else b.message if not tx else FAILED
        if printed != want or (code == 0) != passes:
            bad.append(f"{b.flavor.stem} {name} T_X {tx} T_A {ta} F_A {fa}: exit {code}, printed {printed[-200:]!r}")
    assert not bad, "\n".join(bad)
    return len(jobs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--e2b", action="store_true", help="write E2b's streams (docs/E2B.md) instead of E2's")
    which.add_argument("--e2c", action="store_true", help="write E2c's streams (docs/E2C.md) instead of E2's")
    ap.add_argument("out_dir", nargs="?", type=Path, help="default workload/e2, workload/e2b or workload/e2c")
    args = ap.parse_args(argv)
    out = args.out_dir or REPO / "workload" / ("e2b" if args.e2b else "e2c" if args.e2c else "e2")
    built = build(args.e2b, args.e2c)
    for b in built:
        check_document(b)
    if args.e2b:
        check_one_change(built, build(False))
    if args.e2c:
        check_e2c(built, build(True))
    judged = check_verdicts(built)
    print(f"each check judged {judged} reference solutions as expected")
    out.mkdir(parents=True, exist_ok=True)
    for b in built:
        (out / f"{b.flavor.stem}.yaml").write_text(b.text, encoding="utf-8")
        print(f"wrote {out / (b.flavor.stem + '.yaml')}: {len(b.text.splitlines())} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
