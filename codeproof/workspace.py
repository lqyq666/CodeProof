"""Isolated workspace snapshots and protection boundaries (M1 step 2).

The runtime Writer only ever sees a V0 snapshot exported WITHOUT .git history,
so it cannot read the upstream fix from later commits. The controller keeps the
full clone separately. Protected paths (tests, CI, build config, judge) are
digest-pinned; any change to them blocks a candidate.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# For the bucket maintenance task: only the production implementation may change.
ALLOWED_PATHS = ["more_itertools/more.py"]
PROTECTED_PATHS = [
    "tests",
    ".github",
    "Makefile",
    "setup.cfg",
    "pyproject.toml",
    "docs",
    "more_itertools/__init__.py",
    "more_itertools/recipes.py",
]


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_digest(root: Path, rel_paths: list[str] | None = None) -> dict[str, str]:
    """Digest every file under root (or under the given relative paths)."""
    root = Path(root)
    out: dict[str, str] = {}
    if rel_paths is None:
        files = sorted(p for p in root.rglob("*") if p.is_file())
    else:
        files = []
        for rel in rel_paths:
            p = root / rel
            if p.is_file():
                files.append(p)
            elif p.is_dir():
                files.extend(sorted(q for q in p.rglob("*") if q.is_file()))
    for p in files:
        out[p.relative_to(root).as_posix()] = file_digest(p)
    return out


def export_snapshot(upstream_dir: Path, baseline_sha: str, dest: Path) -> dict:
    """Export a clean V0 tree (no .git) via git archive into dest."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", str(upstream_dir), "archive", "--format=tar", baseline_sha],
        capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tar:
        tar.extractall(dest, filter="data")
    return {"baseline_sha": baseline_sha, "dest": str(dest),
            "file_count": sum(1 for p in dest.rglob("*") if p.is_file())}


@dataclass
class Snapshot:
    root: Path
    baseline_sha: str
    allowed_paths: list[str] = field(default_factory=lambda: list(ALLOWED_PATHS))
    protected_paths: list[str] = field(default_factory=lambda: list(PROTECTED_PATHS))
    protected_digests: dict[str, str] = field(default_factory=dict)
    allowed_digests: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(cls, root: Path, baseline_sha: str) -> "Snapshot":
        root = Path(root)
        snap = cls(root=root, baseline_sha=baseline_sha)
        snap.protected_digests = tree_digest(root, snap.protected_paths)
        snap.allowed_digests = tree_digest(root, snap.allowed_paths)
        return snap

    def protected_violations(self) -> list[str]:
        current = tree_digest(self.root, self.protected_paths)
        bad = []
        for rel, digest in self.protected_digests.items():
            if current.get(rel) != digest:
                bad.append(rel)
        for rel in current:
            if rel not in self.protected_digests:
                bad.append(rel)  # new file under a protected path
        return bad

    def changed_allowed(self) -> list[str]:
        current = tree_digest(self.root, self.allowed_paths)
        return [rel for rel, d in current.items()
                if self.allowed_digests.get(rel) != d]

    def manifest(self) -> dict[str, Any]:
        return {"root": str(self.root), "baseline_sha": self.baseline_sha,
                "allowed_paths": self.allowed_paths,
                "protected_paths": self.protected_paths,
                "protected_digests": self.protected_digests,
                "allowed_digests": self.allowed_digests}