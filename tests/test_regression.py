import json
from pathlib import Path

import pytest

from codeproof import bucket_contract as bc
from codeproof.regression import export_regression_package

BUGGY = '''
from collections import defaultdict, deque
class bucket:
    def __init__(self, iterable, key):
        self._cache = defaultdict(deque)
        for x in iterable:
            self._cache[key(x)].append(x)
    def __contains__(self, key):
        return bool(self._cache[key])
    def __getitem__(self, key):
        return iter(self._cache[key])
    def __iter__(self):
        return iter(self._cache)
'''
FIXED = '''
from collections import deque
class bucket:
    def __init__(self, iterable, key):
        self._cache = {}
        for x in iterable:
            self._cache.setdefault(key(x), deque()).append(x)
    def __contains__(self, key):
        return key in self._cache
    def __getitem__(self, key):
        return iter(self._cache.get(key, ()))
    def __iter__(self):
        return iter(self._cache)
'''


def pkg(root: Path, src: str) -> Path:
    p = root / "more_itertools"
    p.mkdir(parents=True, exist_ok=True)
    (p / "__init__.py").write_text(src, encoding="utf-8")
    return root


def samples():
    return [{"id": "mini", "input": [], "history": "contains_p",
             "operations": bc.HISTORIES["contains_p"]},
            {"id": "m0", "input": ["m0"], "history": "read_p",
             "operations": bc.HISTORIES["read_p"]}]


def test_export_files_and_self_test(tmp_path):
    v0 = pkg(tmp_path / "v0", BUGGY)
    fixed = pkg(tmp_path / "fixed", FIXED)
    dest = tmp_path / "pkg"
    manifest = export_regression_package(dest, samples(), v0_dir=v0, fixed_dir=fixed)
    for name in ("contract.json", "run_regression.py", "README.md", "manifest.json"):
        assert (dest / name).exists()
    assert manifest["sample_count"] == 2
    assert manifest["self_test"]["v0"]["exit_code"] == 1
    assert manifest["self_test"]["v0"]["passed"] is False
    assert manifest["self_test"]["fixed"]["exit_code"] == 0
    assert manifest["self_test"]["fixed"]["passed"] is True


def test_runner_is_stdlib_only(tmp_path):
    dest = tmp_path / "pkg"
    export_regression_package(dest, samples())
    text = (dest / "run_regression.py").read_text(encoding="utf-8")
    assert "codeproof" not in text
    assert "import json" in text and "import sys" in text


def test_contract_embeds_expected(tmp_path):
    dest = tmp_path / "pkg"
    export_regression_package(dest, samples())
    contract = json.loads((dest / "contract.json").read_text("utf-8"))
    assert contract["contract_digest"] == bc.contract_digest()
    assert all("expected" in s for s in contract["samples"])


def test_import_error_exit_2(tmp_path):
    dest = tmp_path / "pkg"
    export_regression_package(dest, samples())
    empty = tmp_path / "empty"
    empty.mkdir()
    from codeproof.regression import _run_package
    res = _run_package(dest, empty)
    assert res["exit_code"] == 2