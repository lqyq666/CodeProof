from pathlib import Path

import pytest

from codeproof import bucket_contract as bc
from codeproof.behavior import run_set

BUGGY = '''
from collections import defaultdict, deque
class bucket:
    def __init__(self, iterable, key):
        self._key = key
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
        self._key = key
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


def make_pkg(root: Path, src: str) -> Path:
    pkg = root / "more_itertools"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(src, encoding="utf-8")
    return root


def small_samples():
    out = []
    for i, (name, ops) in enumerate(bc.HISTORIES.items()):
        out.append({"id": f"t{i}", "input": ["m0"], "history": name, "operations": ops})
    out.append({"id": "empty", "input": [], "history": "contains_p",
                "operations": bc.HISTORIES["contains_p"]})
    return out


def test_buggy_fails_with_signature(tmp_path):
    root = make_pkg(tmp_path / "buggy", BUGGY)
    res = run_set(root, small_samples())
    assert not res.ok
    assert res.failed >= 1
    assert res.signatures.get("missing_key_in_enumeration", 0) >= 1
    assert res.counterexamples


def test_fixed_passes_full_small_set(tmp_path):
    root = make_pkg(tmp_path / "fixed", FIXED)
    res = run_set(root, small_samples())
    assert res.ok
    assert res.passed == res.total == len(small_samples())
    assert res.counterexamples == []


def test_stop_on_first_fail(tmp_path):
    root = make_pkg(tmp_path / "buggy", BUGGY)
    res = run_set(root, small_samples(), stop_on_first_fail=True)
    assert res.stopped_early
    assert res.total < len(small_samples())


def test_import_error_is_error_not_pass(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    res = run_set(empty, small_samples()[:1])
    assert not res.ok
    assert res.error == 1
    assert res.passed == 0