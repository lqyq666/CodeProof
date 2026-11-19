"""Controlled candidate application (M1 step 4).

A candidate is a change set restricted to the allowed production paths. It is
prechecked (no protected path, no traversal) and applied to a fresh copy of the
V0 snapshot, never to the upstream clone or the maintainer's working tree.
Protected digests must still match the baseline after application.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .workspace import Snapshot, file_digest, tree_digest


class PatchError(ValueError):
    """A candidate violates the controlled-write boundary."""


def _validate_rel(rel: str, allowed: list[str], protected: list[str]) -> None:
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise PatchError(f"unsafe candidate path: {rel!r}")
    norm = p.as_posix()
    if any(norm == a or norm.startswith(a.rstrip("/") + "/") for a in protected):
        raise PatchError(f"candidate touches protected path: {rel}")
    if not any(norm == a or norm.startswith(a.rstrip("/") + "/") for a in allowed):
        raise PatchError(f"candidate outside allowed paths: {rel}")


def precheck(changes: dict[str, bytes], snapshot: Snapshot) -> list[str]:
    """Validate every candidate path against the boundary; return sorted paths."""
    for rel in changes:
        _validate_rel(rel, snapshot.allowed_paths, snapshot.protected_paths)
    return sorted(changes)


def collect_changes(writer_dir: Path, snapshot: Snapshot) -> dict[str, bytes]:
    """Read the allowed files a Writer changed in its isolated cwd as a change set."""
    writer_dir = Path(writer_dir)
    changes: dict[str, bytes] = {}
    current = tree_digest(writer_dir, snapshot.allowed_paths)
    for rel, digest in current.items():
        if snapshot.allowed_digests.get(rel) != digest:
            changes[rel] = (writer_dir / rel).read_bytes()
    return changes


@dataclass
class Candidate:
    root: Path
    baseline_sha: str
    changed: list[str]
    protected_digests: dict[str, str] = field(default_factory=dict)
    candidate_digests: dict[str, str] = field(default_factory=dict)

    def receipt(self) -> dict[str, Any]:
        return {"root": str(self.root), "baseline_sha": self.baseline_sha,
                "changed": self.changed,
                "protected_digests": self.protected_digests,
                "candidate_digests": self.candidate_digests}


def apply_candidate(snapshot: Snapshot, changes: dict[str, bytes], dest: Path) -> Candidate:
    """Copy the V0 snapshot to dest and write only the prechecked allowed changes."""
    precheck(changes, snapshot)
    dest = Path(dest)
    if dest.exists():
        raise PatchError(f"candidate destination already exists: {dest}")
    shutil.copytree(snapshot.root, dest)
    for rel, content in changes.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    cand = Snapshot.create(dest, snapshot.baseline_sha)
    violations = cand.protected_violations()
    if violations:
        shutil.rmtree(dest, ignore_errors=True)
        raise PatchError(f"protected paths altered after apply: {violations}")
    changed = sorted(rel for rel in changes
                     if cand.allowed_digests.get(rel) != snapshot.allowed_digests.get(rel))
    return Candidate(root=dest, baseline_sha=snapshot.baseline_sha, changed=changed,
                     protected_digests=cand.protected_digests,
                     candidate_digests={rel: file_digest(dest / rel) for rel in changed})