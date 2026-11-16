"""The runner's checks, run where the project lives and logged before any
label reads them (DESIGN_V2 4.1, 8).

- Citations and falsifiers: a record names a file and words in it; the
  check reads the file and logs whether the words are there (a citation) or
  absent (a falsifier). The model chose the words, so a passing check is no
  evidence; a failing one withdraws the record (its cited basis is gone).
  The paths come from the model, so a path that is not a relative path
  inside the project is never read; its check is logged as failed.
- Modification: the test files and test configuration, taken at session
  start and compared after each tool call (step_check) and at session end.
  A session that changed existing test content, or any test configuration,
  makes its own test and exit results weak; one that only added tests does
  not (rule 2 as the PI refined it), provided every test still passes when
  run alone (isolation_event): a new test can change state other tests
  share.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import hashlib
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Iterable

from rka_memory import anchors
from rka_memory.config import Config
from rka_memory.domain import Actor, Event, Kind, State
from rka_memory.kernel.evidence import _matches
from rka_memory.world import RecordState

MAX_BYTES = 1_000_000
BUILTIN_NAMES = frozenset(dir(builtins))
# Module-level names pytest or unittest act on though nothing mentions them.
CONVENTION_NAMES = frozenset({
    "setup", "teardown", "setup_module", "teardown_module", "setup_function", "teardown_function",
    "setUpModule", "tearDownModule", "load_tests", "pytestmark", "pytest_plugins", "collect_ignore", "collect_ignore_glob",
})
# Calls that request a fixture by a name given as a string.
FIXTURE_LOOKUPS = frozenset({"usefixtures", "getfixturevalue"})
SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})


def _inside(root: Path, rel: str) -> Path | None:
    if not rel or PurePosixPath(rel).is_absolute():
        return None
    target = (root / rel).resolve()
    return target if target.is_relative_to(root.resolve()) else None


def _read(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            return fh.read(MAX_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


def citation_events(records: Iterable[RecordState], state: State, session_id: str) -> list[Event]:
    """One check per citation of each record, run against the project of
    `state`."""
    root = Path(state.project) if state.project else None
    if root is None or not root.is_dir():
        return []
    out = []
    for rec in records:
        for cite in rec.spec.cites:
            target = _inside(root, cite.path)
            text = _read(target)
            found = text is not None and " ".join(cite.quote.split()) in " ".join(text.split())
            passed = text is not None and (not found if cite.absent else found)
            out.append(
                Event.new(
                    Kind.CHECK,
                    Actor.RUNNER,
                    session_id,
                    {
                        "check": cite.check,
                        "record_id": rec.id,
                        "passed": passed,
                        "paths": [str(target)] if target is not None else [],
                        "anchors": [a for a in [anchors.file_anchor(str(root / cite.path), state.project)] if a] if target is not None else [],
                        "path": cite.path,
                        "quote": cite.quote,
                    },
                    state_hash=state.hash(),
                )
            )
    return out


def kind_of(path: str, cfg: Config) -> str | None:
    """"test" for a test file (by name), "config" for test configuration or
    any other file under a test directory, None otherwise."""
    if _matches(path, cfg.check_test_globs):
        return "test"
    if _matches(path, cfg.check_config_globs):
        return "config"
    return None


def entry(path: Path, cfg: Config) -> dict | None:
    """One file's snapshot: its kind and content hash, and for a test file
    its text, so a later snapshot can tell an added test from a changed one."""
    kind = kind_of(str(path), cfg)
    if kind is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    out: dict = {"kind": kind, "hash": hashlib.sha256(data).hexdigest()}
    if kind == "test" and len(data) <= MAX_BYTES:
        out["text"] = data.decode("utf-8", errors="replace")
    return out


def snapshot(root: Path, cfg: Config) -> dict[str, dict]:
    """Every test file and test configuration in the project."""
    out: dict[str, dict] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            e = entry(path, cfg)
            if e is not None:
                out[str(path)] = e
    return out


def _literal(node: ast.AST | None) -> bool:
    """Evaluating it runs no code: a constant or a container of constants."""
    if node is None:
        return True
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False
    return True


def _simple_annotation(node: ast.AST | None) -> bool:
    """None, a string, a name or a dotted name: looked up, never called."""
    if node is None or (isinstance(node, ast.Constant) and isinstance(node.value, (str, type(None)))):
        return True
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name)


def _plain_marks(node: ast.AST) -> bool:
    """Decorated only with pytest marks (pytest.mark.<name>, called or not,
    with literal arguments)."""
    for dec in getattr(node, "decorator_list", ()):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Attribute)
            and target.value.attr == "mark"
            and isinstance(target.value.value, ast.Name)
            and target.value.value.id == "pytest"
        ):
            return False
        if isinstance(dec, ast.Call) and not (
            all(_literal(a) for a in dec.args) and all(k.arg is not None and _literal(k.value) for k in dec.keywords)
        ):
            return False
    return not getattr(node, "type_params", None)


def _signature_runs_nothing(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Defining it runs no code: literal defaults, simple annotations."""
    a = fn.args
    params = [*a.posonlyargs, *a.args, *a.kwonlyargs, *(p for p in (a.vararg, a.kwarg) if p)]
    return (
        all(_literal(d) for d in [*a.defaults, *a.kw_defaults])
        and all(_simple_annotation(p.annotation) for p in params)
        and _simple_annotation(fn.returns)
    )


def _test_function(node: ast.AST) -> bool:
    return (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
        and _plain_marks(node)
        and _signature_runs_nothing(node)
    )


def _docstring(node: ast.AST) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _new_test(node: ast.AST, taken: set[str]) -> bool:
    """A new, uniquely named test function, or a test class holding only test
    methods; no fixture, hook, other decorator, or code that runs when the
    file loads."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name not in taken and _test_function(node)
    if isinstance(node, ast.ClassDef):
        return (
            node.name.startswith("Test")
            and node.name not in taken
            and not node.bases
            and not node.keywords
            and _plain_marks(node)
            and all(_docstring(b) or _test_function(b) for b in node.body)
        )
    return False


def _bound(alias: ast.alias, node: ast.Import | ast.ImportFrom) -> str:
    if alias.asname:
        return alias.asname
    return alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name


def _names(tree: ast.AST) -> set[str]:
    """Every name the file binds or mentions, at any depth, with its
    parameters (pytest fills them with fixtures of that name) and the fixture
    names it passes as strings to usefixtures or getfixturevalue."""
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", None)) in FIXTURE_LOOKUPS:
            out.update(a.value for a in n.args if isinstance(a, ast.Constant) and isinstance(a.value, str))
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out.update(_bound(a, n) for a in n.names)
        elif isinstance(n, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and n.name:
            out.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
    return out


def known_imports(snap: dict[str, dict]) -> frozenset[str]:
    """The top-level packages some test file in the snapshot imports by an
    absolute import, anywhere in it."""
    out: set[str] = set()
    for path, e in snap.items():
        if e.get("kind") != "test" or "text" not in e or not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(e["text"])
        except (SyntaxError, ValueError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                out.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
                out.add(n.module.split(".")[0])
    return frozenset(out)


def _import_adds_nothing(node: ast.Import | ast.ImportFrom, old_imports: set[str], taken: set[str], known: frozenset[str]) -> bool:
    """An added import that cannot change how existing tests run: it repeats
    one the file had, or it binds only fresh, ordinary names and loads a
    module that holds no fixture or that existing tests already load. Pytest
    registers every fixture object a test module holds, whatever its name, so
    names may be imported only from pytest and the standard library; a
    project module may be imported only whole (`import calc`), and only if
    existing tests import it."""
    if ast.dump(node) in old_imports:
        return True
    safe = {"pytest"} | sys.stdlib_module_names
    if isinstance(node, ast.ImportFrom) and (node.level or node.module == "__future__" or (node.module or "").split(".")[0] not in safe):
        return False
    for alias in node.names:
        name = _bound(alias, node)
        if (
            alias.name == "*"
            or name in taken
            or name.startswith(("test", "Test", "pytest_"))
            or name in CONVENTION_NAMES
            or (name.startswith("__") and name.endswith("__"))
            or name in BUILTIN_NAMES
        ):
            return False
        if isinstance(node, ast.Import) and alias.name.split(".")[0] not in safe | known:
            return False
    return True


def _python_test_changed(old: str | None, new: str, known: frozenset[str] = frozenset()) -> bool:
    """Whether a Python test file changed beyond added tests. Fails closed:
    every top-level statement of the old file must still be there, unchanged
    (test bodies and decorators included), and every new one must be a new
    test (_new_test), a docstring, or an import that adds nothing
    (_import_adds_nothing; `known` holds the modules existing tests load).
    Anything else, and anything that does not parse, is a change."""
    try:
        old_tree = ast.parse(old) if old is not None else ast.Module(body=[], type_ignores=[])
        new_tree = ast.parse(new)
    except (SyntaxError, ValueError):
        return True
    named = [n.name for n in new_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if len(named) != len(set(named)):
        return True
    remaining = Counter(ast.dump(n) for n in old_tree.body)
    added = []
    for node in new_tree.body:
        key = ast.dump(node)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            added.append(node)
    if +remaining:
        return True
    taken = _names(old_tree)
    old_imports = {ast.dump(n) for n in ast.walk(old_tree) if isinstance(n, (ast.Import, ast.ImportFrom))}
    for node in added:
        if _new_test(node, taken) or _docstring(node):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)) and _import_adds_nothing(node, old_imports, taken, known):
            continue
        return True
    return False


def changes(before: dict[str, dict], after: dict[str, dict]) -> tuple[list[str], list[str]]:
    """(changed, added) between two snapshots. Test configuration created,
    edited or removed is a change; a test file removed is a change; a Python
    test file that changed beyond added tests (_python_test_changed, with the
    imports of `before`'s test files as known) is a change; outside Python,
    only a brand-new test file is an addition."""
    changed, added = [], []
    known = known_imports(before)
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old is not None and new is not None and old["hash"] == new["hash"]:
            continue
        if (new or old)["kind"] == "config" or new is None or "text" not in new:
            changed.append(path)
        elif path.endswith(".py"):
            if old is not None and "text" not in old:
                changed.append(path)
            else:
                (changed if _python_test_changed(old["text"] if old else None, new["text"], known) else added).append(path)
        else:
            (added if old is None else changed).append(path)
    return changed, added


def step_check(start: dict[str, dict], seen: list[dict[str, dict]], session_id: str, state: State, on: str, event_id: str | None = None) -> Event | None:
    """A failed modification check for what one tool call changed in the
    tests or test configuration, logged as the call returns, so a change
    reverted before the session ends is still on record. `seen` holds the
    snapshots the runner took, oldest first: how it last saw them, then, for
    a single-file write, the file just before the write, then how they stand
    after the call. Each path that moved from one to the next is compared
    with the session's start, as at session end, so the session may edit a
    test it added and import what that test uses. None when the call only
    added tests or touched neither. `on` says how the runner saw the call:
    "write" for a single-file write, "shell" for a whole-project comparison
    after any other tool that may write."""
    paths: list[str] = []
    for prev, cur in zip(seen, seen[1:]):
        if cur == prev:
            continue
        changed, _ = changes(start, cur)
        paths += [p for p in changed if cur.get(p) != prev.get(p) and p not in paths]
    if not paths:
        return None
    return Event.new(
        Kind.CHECK,
        Actor.RUNNER,
        session_id,
        {"check": "modification", "passed": False, "paths": paths, "on": on},
        state_hash=state.hash(),
        id=event_id,
    )


def modification_event(
    before: dict[str, dict], after: dict[str, dict], session_id: str, state: State, isolation_due: bool = False
) -> Event:
    """The session's tests at its end against its start. `isolation_due`
    marks that an isolation re-run follows; logged before it starts, so a
    re-run that never logs its result still leaves the session weak
    (evidence.modified_sessions)."""
    changed, added = changes(before, after)
    payload = {"check": "modification", "passed": not changed, "paths": changed, "added": added}
    if isolation_due:
        payload["isolation"] = "due"
    return Event.new(Kind.CHECK, Actor.RUNNER, session_id, payload, state_hash=state.hash())


UNITTEST_IDS = (
    "import unittest\n"
    "def ids(suite):\n"
    "    for t in suite:\n"
    "        yield from ids(t) if isinstance(t, unittest.TestSuite) else [t.id()]\n"
    "print('\\n'.join(ids(unittest.defaultTestLoader.discover('.'))))\n"
)


# What a confined test run keeps of the hook's environment.
ENV_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")


class _Runner:
    """Runs commands through the confinement command, with only `run`
    writable and a minimal environment, until the budget runs out; each in
    its own process group, killed whole when it ends or time is up."""

    def __init__(self, iso, run: Path):
        self.deadline = time.monotonic() + iso.budget_seconds
        self.prefix = [*iso.confine, "--bind", str(run), str(run), "--"]
        tmp = run / "tmp"
        self.env = {k: os.environ[k] for k in ENV_KEEP if k in os.environ} | {
            "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(tmp), "HOME": str(tmp),
        }

    def run(self, cmd: list[str], cwd: Path) -> tuple[int | None, str]:
        """(exit code, stdout); None when the budget ran out."""
        left = self.deadline - time.monotonic()
        if left <= 0:
            return None, ""
        with tempfile.TemporaryFile() as out:
            try:
                proc = subprocess.Popen(
                    [*self.prefix, *cmd], cwd=cwd, env=self.env, stdin=subprocess.DEVNULL,
                    stdout=out, stderr=subprocess.DEVNULL, start_new_session=True,
                )
            except OSError as err:
                return 1, str(err)
            try:
                code: int | None = proc.wait(timeout=left)
            except subprocess.TimeoutExpired:
                code = None
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            out.seek(0)
            return code, out.read().decode("utf-8", errors="replace")


def _framework(python: str, choice: str, work: Path, runner: _Runner) -> str:
    if choice != "auto":
        return choice
    code, _ = runner.run([python, "-c", "import pytest"], work)
    return "pytest" if code == 0 else "unittest"


def _collect(python: str, framework: str, work: Path, runner: _Runner) -> tuple[list[str] | None, str | None]:
    if framework == "pytest":
        code, out = runner.run([python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"], work)
        if code == 5:
            return [], None
        ids = [line.strip() for line in out.splitlines() if "::" in line]
    else:
        code, out = runner.run([python, "-c", UNITTEST_IDS], work)
        ids = [line.strip() for line in out.splitlines() if line.strip()]
    if code is None:
        return None, "ran out of time collecting the tests"
    if code != 0:
        return None, f"collecting the tests exited with {code}"
    return ids, None


def _alone(python: str, framework: str, index: int, test_id: str, work: Path, runner: _Runner) -> bool | None:
    if framework == "pytest":
        basetemp = str(work.parent / "tmp" / f"bt-{index}")
        cmd = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp", basetemp, test_id]
    else:
        cmd = [python, "-m", "unittest", "-q", test_id]
    code, _ = runner.run(cmd, work)
    return None if code is None else code == 0


# The project's copy, made inside the confinement with the agent's rights, so
# a file swapped for a link to something the agent cannot read is not copied:
# the project without caches, venvs or links that lead out of it.
COPY_PROJECT = (
    "import json, shutil, sys\n"
    "from pathlib import Path\n"
    "root, work, skip = Path(sys.argv[1]), Path(sys.argv[2]), set(json.loads(sys.argv[3]))\n"
    "real = root.resolve()\n"
    "def inside(p):\n"
    "    try:\n"
    "        return p.resolve().is_relative_to(real)\n"
    "    except (OSError, RuntimeError):\n"
    "        return False\n"
    "def ignore(d, names):\n"
    "    return {n for n in names if n in skip or ((Path(d) / n).is_symlink() and not inside(Path(d) / n))}\n"
    "shutil.copytree(root, work, ignore=ignore, symlinks=True)\n"
)


def _hand_to(run: Path, user: str) -> None:
    pw = pwd.getpwnam(user)
    for dirpath, dirnames, filenames in os.walk(run):
        for name in [".", *dirnames, *filenames]:
            os.chown(Path(dirpath) / name, pw.pw_uid, pw.pw_gid, follow_symlinks=False)


def _file_of(test_id: str, framework: str, work: Path) -> str | None:
    if framework == "pytest":
        return test_id.split("::")[0]
    parts = test_id.split(".")
    for i in range(len(parts), 0, -1):
        candidate = Path(*parts[:i]).with_suffix(".py")
        if (work / candidate).is_file():
            return str(candidate)
    return None


def isolation_event(
    root: Path, cfg: Config, session_id: str, state: State, added: Iterable[str] = (), event_id: str | None = None
) -> Event:
    """Rule 2's isolation re-run (RKA jrn_01M7H11MZ72N1FEQ01P1D5JQ2Y), for a
    session that added tests and whose last test run passed: every test runs
    alone, in a copy of the project, so no test can pass on state another
    left behind. A test that fails alone, a collection error, running out of
    the budget or any error of the runner fails the check. The tests are
    code the agent wrote, so they run only through the confinement command,
    in a fresh directory handed to `user` (cfg.isolation), and the project
    is copied there by a confined command too; without both an
    interpreter and that command nothing runs, and the check fails with the
    added tests (`added`) as its paths."""
    iso = cfg.isolation
    payload: dict = {"check": "modification", "on": "isolation", "passed": False, "tests": 0, "failed": [], "paths": []}
    if not (iso.python and iso.confine):
        payload |= {"paths": sorted(added), "error": "not configured"}
    else:
        error = None
        if iso.dir:
            Path(iso.dir).mkdir(mode=0o711, parents=True, exist_ok=True)
        run = Path(tempfile.mkdtemp(prefix="rkm-isolation-", dir=iso.dir))
        try:
            work = run / "project"
            (run / "tmp").mkdir()
            if iso.user:
                _hand_to(run, iso.user)
            runner = _Runner(iso, run)
            code, _ = runner.run([iso.python, "-I", "-c", COPY_PROJECT, str(root), str(work), json.dumps(sorted(SKIP_DIRS))], run)
            ids = None
            if code != 0:
                error = "ran out of time copying the project" if code is None else f"copying the project exited with {code}"
            else:
                framework = _framework(iso.python, iso.framework, work, runner)
                ids, error = _collect(iso.python, framework, work, runner)
            if ids is not None:
                with ThreadPoolExecutor(max_workers=iso.workers) as pool:
                    results = list(pool.map(lambda it: _alone(iso.python, framework, it[0], it[1], work, runner), enumerate(ids)))
                failed = [t for t, ok in zip(ids, results) if ok is False]
                if any(ok is None for ok in results):
                    error = f"ran out of time ({iso.budget_seconds:g}s) running the tests alone"
                files = {f for t in failed if (f := _file_of(t, framework, work))}
                payload |= {"tests": len(ids), "failed": failed[:50], "paths": sorted(str(root / f) for f in files)}
        except Exception as err:  # noqa: BLE001 - any failure of the re-run fails the check
            error = f"re-run failed: {type(err).__name__}: {err}"
        finally:
            shutil.rmtree(run, ignore_errors=True)
        payload["passed"] = error is None and not payload["failed"]
        if error:
            payload["error"] = error
    return Event.new(Kind.CHECK, Actor.RUNNER, session_id, payload, state_hash=state.hash(), id=event_id)
