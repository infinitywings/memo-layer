"""Generate workload/e2/{calc,textkit,stockbook}.yaml (docs/E2.md Section 4).

Usage: .venv/bin/python scripts/e2_streams.py [out_dir]   (default workload/e2)

Expected results are written by hand here; each fixture's reference function in
this file must reproduce them (asserted), and scripts/e2_dry_run.py checks its
own, separately written, solutions against the generated streams.
"""

from __future__ import annotations

import codecs
import copy
import html
import math
import statistics
import sys
import textwrap
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "workload" / "e2"

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


def calc_check() -> str:
    by = {t[0]: t for t in CALC}
    rows = [f"({n!r}, {by[n][3]!r}, {by[n][4]!r})" for n in CALC_RANK]
    o1, o2 = split_message(CALC_TX)
    return fill(CALC_CHECK, MATH="import math\n", OPS1=o1, OPS2=o2, TASKS=table(rows))


def textkit_check() -> str:
    by = {t[0]: t for t in TEXTKIT}
    rows = [f"({n!r}, {by[n][3]!r}, {by[n][4]!r})" for n in TEXTKIT_RANK]
    c1, c2 = split_message(TEXTKIT_TX)
    return fill(TEXTKIT_CHECK, MATH="", CMS1=c1, CMS2=c2, TASKS=table(rows))


def stockbook_check() -> str:
    by = {t[0]: t for t in STOCKBOOK}
    rows = [f"({n!r}, {dataset_src(by[n][3])}, {by[n][4]!r})" for n in STOCKBOOK_RANK]
    w1, w2 = split_message(STOCKBOOK_TX)
    items = "[\n" + "".join(f"    {i!r},\n" for i in ITEMS) + "]"
    return fill(STOCKBOOK_CHECK, WH1=w1, WH2=w2, ITEMS=items, PRICEY_LAST=repr(PRICEY[-1]), TASKS=table(rows))


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


def emit(key, project, start, check, message, handoff, sessions) -> str:
    out = [
        f"name: e2-{key}\n",
        "model: live\n",
        "per_arm: true\n",
        "fresh_project: true\n",
        "description: >-\n",
        indent(textwrap.fill(DESCRIPTIONS[key], 100), 2) + "\n",
        f"start: {start}\n",
        f"project: {project}\n",
        "config:\n",
        "  packet: {arms: [stated, gated, notepad]}\n",
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
        if i < len(sessions):
            out.append("\n")
    return "".join(out)


def build():
    streams = {}
    # calc
    sessions = []
    for i, (name, params, tail, args, want, bools) in enumerate(CALC, 1):
        got = CALC_REF[name](*args)
        assert math.isclose(got, want, rel_tol=1e-12), (name, got, want)
        assert abs(want - round(want, 2)) > 1e-6 * max(1, abs(want)), name
        sessions.append((prompt(i, name, params, "calc.py", tail, CALC_U), calc_observers(name, args, want, bools)))
    streams["calc"] = (emit("calc", "calc-uv", "2027-09-06T09:00:00+00:00", calc_check(), CALC_TX, CALC_HANDOFF, sessions),
                       sessions, CALC_TX, CALC_HANDOFF, calc_check())
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
    streams["textkit"] = (emit("textkit", "textkit", "2027-10-04T09:00:00+00:00", textkit_check(), TEXTKIT_TX,
                               TEXTKIT_HANDOFF, sessions), sessions, TEXTKIT_TX, TEXTKIT_HANDOFF, textkit_check())
    # stockbook
    sessions = []
    for i, (name, params, tail, args, want) in enumerate(STOCKBOOK, 1):
        fa = upper_skus(want)
        assert fa != want, name
        sessions.append((prompt(i, name, params, "stockbook/__init__.py", tail, STOCKBOOK_U),
                         stockbook_observers(name, copy.deepcopy(args), want, fa)))
    streams["stockbook"] = (emit("stockbook", "stockbook", "2027-11-01T09:00:00+00:00", stockbook_check(), STOCKBOOK_TX,
                                 STOCKBOOK_HANDOFF, sessions), sessions, STOCKBOOK_TX, STOCKBOOK_HANDOFF, stockbook_check())
    return streams


def main():
    OUT.mkdir(exist_ok=True)
    for key, (text, sessions, message, handoff, check) in build().items():
        doc = yaml.safe_load(text)
        # what the YAML says is what was meant
        assert doc["config"]["checks"]["outcome"]["messages"] == [message]
        assert doc["config"]["checks"]["outcome"]["command"] == ["{python}", "-c", check]
        assert 12 <= len(message) <= 400
        assert len(doc["sessions"]) == 20
        for s, (p, observers) in zip(doc["sessions"], sessions):
            assert s["prompt"] == p, (s["prompt"], p)
            assert [(o["label"], o["command_passes"]) for o in s["observe"]] == observers
        assert [("drift" in s) for s in doc["sessions"]] == [True] * 3 + [False] * 17
        assert all(s["drift"] == [{"write": {"path": "HANDOFF.md", "content": handoff}}] for s in doc["sessions"][:3])
        compile(check, f"{key}-check", "exec")
        for s in doc["sessions"]:
            for o in s["observe"][1:]:
                body = o["command_passes"].split("\n", 1)[1].rsplit("EOF\n", 1)[0]
                compile(body, f"{key}-{o['label']}", "exec")
        (OUT / f"{key}.yaml").write_text(text, encoding="utf-8")
        print(f"wrote {OUT / (key + '.yaml')}: {len(text.splitlines())} lines")


if __name__ == "__main__":
    main()
