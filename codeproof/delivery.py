"""Layered verification and delivery decision (M1 step 5).

A candidate is deliverable only when the protected boundary is intact, the
independent behavior contract passes, AND the upstream test/coverage gate passes.
Missing evidence or an infrastructure failure blocks delivery rather than being
silently treated as a pass. The behavior layer is cheap and runs first so a
target counterexample can route to repair before the expensive upstream layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import bucket_contract as bc
from .behavior import BehaviorResult, run_set
from .patch import Candidate
from .verifier import UpstreamResult, run_upstream
from .workspace import Snapshot


def decide(protected_ok: bool, behavior_ok: bool | None,
           upstream_status: str | None) -> tuple[str, list[str]]:
    """Return (decision, reasons). decision is 'deliver' or 'block'."""
    reasons: list[str] = []
    if not protected_ok:
        reasons.append("protected_boundary_violated")
    if behavior_ok is None:
        reasons.append("behavior_evidence_missing")
    elif not behavior_ok:
        reasons.append("behavior_contract_failed")
    if upstream_status is None:
        reasons.append("upstream_evidence_missing")
    elif upstream_status != "passed":
        reasons.append(f"upstream_{upstream_status}")
    return ("deliver" if not reasons else "block"), reasons


@dataclass
class DeliveryReport:
    run_id: str
    baseline_sha: str
    candidate_root: str
    changed: list[str]
    decision: str
    reasons: list[str]
    protected_ok: bool
    contract_version: str
    contract_digest: str
    behavior: dict[str, Any] | None = None
    upstream: dict[str, Any] | None = None
    candidate_digests: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def deliver(snapshot: Snapshot, candidate: Candidate, *, run_id: str,
            samples: list[dict[str, Any]] | None = None,
            out_dir: Path | None = None,
            behavior_fn: Callable | None = None,
            upstream_fn: Callable | None = None,
            stop_on_first_fail: bool = True) -> DeliveryReport:
    samples = samples if samples is not None else bc.generate_discovery_set()
    behavior_fn = behavior_fn or (lambda: run_set(
        candidate.root, samples, stop_on_first_fail=stop_on_first_fail))
    upstream_fn = upstream_fn or (lambda: run_upstream(candidate.root))

    protected_ok = candidate.protected_digests == snapshot.protected_digests
    behavior: BehaviorResult = behavior_fn()
    # Cheap layer first: a behavior failure blocks before paying upstream cost.
    upstream: UpstreamResult | None = None
    if protected_ok and behavior.ok:
        upstream = upstream_fn()
    upstream_status = upstream.status if upstream is not None else None
    decision, reasons = decide(protected_ok, behavior.ok, upstream_status)

    report = DeliveryReport(
        run_id=run_id, baseline_sha=snapshot.baseline_sha,
        candidate_root=str(candidate.root), changed=candidate.changed,
        decision=decision, reasons=reasons, protected_ok=protected_ok,
        contract_version=bc.CONTRACT_VERSION, contract_digest=bc.contract_digest(),
        behavior=behavior.as_dict(),
        upstream=upstream.as_dict() if upstream is not None else None,
        candidate_digests=candidate.candidate_digests)
    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "delivery.json").write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return report