"""Rule 2 as refined by the PI (RKA jrn_01M7GZQRQSGV7C8A2ACZVZ8C00), written
before the code: a session's test results are weak only if it changed or
removed existing test content, or changed test configuration at all.
Adding tests is not a change: only a change to an existing check can make a
failing check pass. An added autouse fixture or pytest hook is a change,
since it alters how existing tests run without touching their lines."""

from pathlib import Path

from rka_memory import config as config_mod
from rka_memory.ledger.checks import changes, snapshot

TEST = "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n"


def project(tmp_path: Path, **files: str) -> Path:
    root = tmp_path / "proj"
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        path = root / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def compare(tmp_path: Path, before: dict[str, str], after: dict[str, str]) -> tuple[list[str], list[str]]:
    cfg = config_mod.load()
    root = project(tmp_path, **before)
    first = snapshot(root, cfg)
    for name in before:
        if name not in after:
            (root / name.replace("__", "/")).unlink()
    project(tmp_path, **after)
    changed, added = changes(first, snapshot(root, cfg))
    return sorted(str(Path(p).relative_to(root)) for p in changed), sorted(str(Path(p).relative_to(root)) for p in added)


def test_adding_a_test_function_is_an_addition(tmp_path):
    after = TEST + "\n\ndef test_multiply():\n    assert calc.multiply(2, 3) == 6\n"
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": after}) == ([], ["test_calc.py"])


def test_inserting_a_test_between_existing_ones_is_an_addition(tmp_path):
    after = TEST.replace("def test_add", "def test_multiply():\n    assert calc.multiply(2, 3) == 6\n\n\ndef test_add")
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": after}) == ([], ["test_calc.py"])


def test_a_new_test_file_is_an_addition(tmp_path):
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": TEST, "tests__test_new.py": TEST}) == ([], ["tests/test_new.py"])


def test_changing_an_existing_assertion_is_a_change(tmp_path):
    after = TEST.replace("== 5", "== 5 or True")
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": after}) == (["test_calc.py"], [])


def test_removing_a_test_file_is_a_change(tmp_path):
    assert compare(tmp_path, {"test_calc.py": TEST, "test_old.py": TEST}, {"test_calc.py": TEST}) == (["test_old.py"], [])


def test_adding_an_autouse_fixture_or_a_pytest_hook_is_a_change(tmp_path):
    fixture = TEST + "\n\nimport pytest\n\n\n@pytest.fixture(autouse=True)\ndef _patch(monkeypatch):\n    monkeypatch.setattr(calc, 'add', lambda a, b: 5)\n"
    hook = TEST + "\n\ndef pytest_collection_modifyitems(items):\n    items.clear()\n"
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": fixture}) == (["test_calc.py"], [])
    assert compare(tmp_path / "b", {"test_calc.py": TEST}, {"test_calc.py": hook}) == (["test_calc.py"], [])


def test_any_change_to_test_configuration_is_a_change(tmp_path):
    before = {"test_calc.py": TEST, "pyproject.toml": "[project]\nname = 'calc'\n"}
    after = {**before, "pyproject.toml": before["pyproject.toml"] + "\n[tool.pytest.ini_options]\naddopts = '-k not slow'\n"}
    assert compare(tmp_path, before, after) == (["pyproject.toml"], [])
    assert compare(tmp_path / "b", {"test_calc.py": TEST}, {"test_calc.py": TEST, "conftest.py": "import pytest\n"}) == (["conftest.py"], [])


# Added after a security review of the line-diff version (it took any
# insertion for an addition): the comparison reads the syntax tree and
# fails closed. Only new, uniquely named tests are additions.


def test_inserting_into_an_existing_test_is_a_change(tmp_path):
    after = TEST.replace("def test_add():\n", "def test_add():\n    return\n")
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": after}) == (["test_calc.py"], [])


def test_module_level_code_added_to_a_test_file_is_a_change(tmp_path):
    after = TEST + "\ncalc.add = lambda a, b: 5\n"
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": after}) == (["test_calc.py"], [])


def test_a_new_fixture_or_a_redefined_test_is_a_change(tmp_path):
    fixture = TEST + "\n\nimport pytest\n\n\n@pytest.fixture\ndef numbers():\n    return (2, 3)\n"
    redefined = TEST + "\n\ndef test_add():\n    pass\n"
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": fixture}) == (["test_calc.py"], [])
    assert compare(tmp_path / "b", {"test_calc.py": TEST}, {"test_calc.py": redefined}) == (["test_calc.py"], [])


def test_new_tests_with_imports_marks_or_a_test_class_are_additions(tmp_path):
    after = (
        "import pytest\n" + TEST
        + "\n\n@pytest.mark.parametrize('a,b', [(1, 2)])\ndef test_more(a, b):\n    assert calc.add(a, b) == 3\n"
        + "\n\nclass TestDivide:\n    def test_zero(self):\n        with pytest.raises(ZeroDivisionError):\n            calc.divide(1, 0)\n"
    )
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": after}) == ([], ["test_calc.py"])


def test_a_new_test_file_with_module_level_code_is_a_change(tmp_path):
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": TEST, "test_more.py": "import calc\n\ncalc.add = max\n"}) == (["test_more.py"], [])


def test_a_helper_module_under_tests_is_configuration(tmp_path):
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": TEST, "tests__helpers.py": "VALUE = 1\n"}) == (["tests/helpers.py"], [])


def test_an_unparseable_test_file_is_a_change(tmp_path):
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": TEST + "\ndef test_broken(:\n"}) == (["test_calc.py"], [])


def test_outside_python_only_a_new_test_file_is_an_addition(tmp_path):
    js = "test('adds', () => { expect(add(2, 3)).toBe(5) })\n"
    assert compare(tmp_path, {"calc.test.js": js}, {"calc.test.js": js + "test('more', () => {})\n"}) == (["calc.test.js"], [])
    assert compare(tmp_path / "b", {"calc.test.js": js}, {"calc.test.js": js, "extra.test.js": js}) == ([], ["extra.test.js"])


# Added after a security review of the syntax-tree version (9f8d7c5): an
# added import could rebind a name existing tests use or run agent-written
# code when the file loads, and a new test's defaults, decorator arguments
# and annotations run at import too. Fail closed on all of them.


def test_an_added_import_that_rebinds_a_name_the_old_file_uses_is_a_change(tmp_path):
    rebinding = TEST + "\nimport math as calc\n"
    free_name = "import calc\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    data = TEST + "\ntest_data = [1, 2]\n"
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": rebinding}) == (["test_calc.py"], [])
    assert compare(tmp_path / "b", {"test_calc.py": free_name}, {"test_calc.py": "from calc import add\n" + free_name}) == (["test_calc.py"], [])
    assert compare(tmp_path / "c", {"test_calc.py": data}, {"test_calc.py": data + "\n\ndef test_data():\n    pass\n"}) == (["test_calc.py"], [])


def test_repeating_an_existing_import_is_not_a_change(tmp_path):
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": "import calc\n" + TEST}) == ([], ["test_calc.py"])


def test_an_added_import_of_a_module_no_existing_test_imports_is_a_change(tmp_path):
    helper = "import helpers\n\n\ndef test_ok():\n    assert helpers.ok()\n"
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": TEST, "test_more.py": helper}) == (["test_more.py"], [])
    assert compare(tmp_path / "b", {"test_calc.py": TEST}, {"test_calc.py": TEST + "\n" + helper}) == (["test_calc.py"], [])


def test_added_imports_of_pytest_the_standard_library_or_what_tests_already_import_are_additions(tmp_path):
    more = (
        "import math\nimport pytest\nimport calc\nfrom unittest import mock\n\n\n"
        "def test_sum():\n    assert calc.add(1, 2) == math.fsum([1, 2])\n"
    )
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": TEST, "test_more.py": more}) == ([], ["test_more.py"])


def test_an_added_import_binding_a_test_or_hook_name_a_builtin_a_dunder_or_a_future_is_a_change(tmp_path):
    for i, line in enumerate((
        "from calc import add as test_add_again",
        "from math import floor as pytest_configure",
        "from os import sep as pytest_plugins",
        "from operator import abs",
        "from math import pi as __builtins__",
        "from __future__ import annotations",
    )):
        after = line + "\n" + TEST
        assert compare(tmp_path / str(i), {"test_calc.py": TEST}, {"test_calc.py": after}) == (["test_calc.py"], []), line


def test_code_in_a_new_tests_defaults_decorators_or_annotations_is_a_change(tmp_path):
    for i, new in enumerate((
        "def test_d(x=setattr(calc, 'add', lambda a, b: 5)):\n    pass\n",
        "def test_k(*, x=calc.patch()):\n    pass\n",
        "import pytest\n\n\n@pytest.mark.skipif(calc.patch(), reason='')\ndef test_s():\n    pass\n",
        "def test_a(x: calc.patch() = 1):\n    pass\n",
        "def test_r() -> calc.patch():\n    pass\n",
        "class TestM:\n    def test_m(self, x=calc.patch()):\n        pass\n",
    )):
        after = TEST + "\n\n" + new
        assert compare(tmp_path / str(i), {"test_calc.py": TEST}, {"test_calc.py": after}) == (["test_calc.py"], []), new


def test_literal_defaults_mark_arguments_and_simple_annotations_are_additions(tmp_path):
    after = (
        "import pytest\n" + TEST
        + "\n\n@pytest.mark.slow\n@pytest.mark.parametrize('a,b', [(1, 2), (-1, 0)])\n"
        + "def test_p(a: int, b: 'int' = 0, *, c: calc.Number = None) -> None:\n    assert calc.add(a, b) is not None\n"
    )
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": after}) == ([], ["test_calc.py"])


# Added after a third review (ebd2cc7) and the PI's decision on shared state
# (RKA jrn_01M7H11MZ72N1FEQ01P1D5JQ2Y). Pytest and unittest act on some module
# names that nothing in the file mentions, and pytest registers every fixture
# object it finds in a test module, under any name. So a from-import from a
# module outside pytest and the standard library is a change (it may bind a
# fixture); the earlier expectation that allowed `from calc import add` in a
# new test file is withdrawn.


def test_an_added_import_binding_a_name_pytest_or_unittest_acts_on_is_a_change(tmp_path):
    for i, name in enumerate(("setup_module", "teardown_function", "setup", "pytestmark", "load_tests", "setUpModule", "collect_ignore")):
        after = f"from math import floor as {name}\n" + TEST
        assert compare(tmp_path / str(i), {"test_calc.py": TEST}, {"test_calc.py": after}) == (["test_calc.py"], []), name


def test_an_added_import_binding_a_fixture_an_existing_test_requests_is_a_change(tmp_path):
    param = "import calc\n\n\ndef test_add(numbers):\n    assert calc.add(*numbers) == 5\n"
    marked = "import calc\nimport pytest\n\n\n@pytest.mark.usefixtures('db')\ndef test_add():\n    assert calc.add(2, 3) == 5\n"
    dynamic = "import calc\n\n\ndef test_add(request):\n    assert calc.add(*request.getfixturevalue('pair')) == 5\n"
    for i, (old, name) in enumerate(((param, "numbers"), (marked, "db"), (dynamic, "pair"))):
        after = f"from math import floor as {name}\n" + old
        assert compare(tmp_path / str(i), {"test_calc.py": old}, {"test_calc.py": after}) == (["test_calc.py"], []), name


def test_a_from_import_of_a_project_module_is_a_change(tmp_path):
    more = "from calc import add\n\n\ndef test_sum():\n    assert add(1, 2) == 3\n"
    assert compare(tmp_path, {"test_calc.py": TEST}, {"test_calc.py": TEST, "test_more.py": more}) == (["test_more.py"], [])
