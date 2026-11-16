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
    root.mkdir(exist_ok=True)
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
