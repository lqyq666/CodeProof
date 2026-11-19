from pathlib import Path

import pytest

from codeproof.patch import (
    PatchError,
    apply_candidate,
    collect_changes,
    precheck,
)
from codeproof.workspace import Snapshot


@pytest.fixture()
def snap(tmp_path: Path) -> Snapshot:
    root = tmp_path / "v0"
    (root / "more_itertools").mkdir(parents=True)
    (root / "more_itertools" / "more.py").write_text("VAL = 0\n", encoding="utf-8")
    (root / "more_itertools" / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_more.py").write_text("assert True\n", encoding="utf-8")
    (root / "Makefile").write_text("all:\n\techo hi\n", encoding="utf-8")
    return Snapshot.create(root, "deadbeef")


def test_precheck_accepts_allowed(snap):
    assert precheck({"more_itertools/more.py": b"VAL = 1\n"}, snap) == ["more_itertools/more.py"]


@pytest.mark.parametrize("rel", [
    "tests/test_more.py",
    "Makefile",
    "more_itertools/__init__.py",
    "../escape.py",
    "/abs/more.py",
    "more_itertools/recipes.py",
])
def test_precheck_rejects(snap, rel):
    with pytest.raises(PatchError):
        precheck({rel: b"x"}, snap)


def test_apply_isolated_and_protected_intact(snap, tmp_path):
    dest = tmp_path / "cand"
    cand = apply_candidate(snap, {"more_itertools/more.py": b"VAL = 1\n"}, dest)
    assert cand.changed == ["more_itertools/more.py"]
    assert (dest / "more_itertools" / "more.py").read_text() == "VAL = 1\n"
    # original snapshot untouched
    assert (snap.root / "more_itertools" / "more.py").read_text() == "VAL = 0\n"
    # protected files identical to baseline
    assert (dest / "tests" / "test_more.py").read_text() == "assert True\n"
    assert cand.protected_digests == snap.protected_digests
    assert "more_itertools/more.py" in cand.candidate_digests


def test_apply_refuses_existing_dest(snap, tmp_path):
    dest = tmp_path / "cand"
    dest.mkdir()
    with pytest.raises(PatchError):
        apply_candidate(snap, {"more_itertools/more.py": b"VAL = 1\n"}, dest)


def test_collect_changes_from_writer_dir(snap, tmp_path):
    writer = tmp_path / "writer"
    import shutil
    shutil.copytree(snap.root, writer)
    (writer / "more_itertools" / "more.py").write_bytes(b"VAL = 2\n")
    changes = collect_changes(writer, snap)
    assert changes == {"more_itertools/more.py": b"VAL = 2\n"}


def test_no_change_yields_empty_candidate(snap, tmp_path):
    cand = apply_candidate(snap, {}, tmp_path / "cand")
    assert cand.changed == []
    assert cand.candidate_digests == {}