"""Aggregate runner for the frozen bucket behavior contract (M1 step 5 / M2 2.4).

Runs the trusted judge over a sample set against an isolated candidate snapshot
and tallies verdicts. Counterexamples carry the sample, verdict and raw response
so they can be minimized, routed to repair, or exported as a regression package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import bucket_contract as bc


@dataclass
class BehaviorResult:
    total: int = 0
    passed: int = 0
    failed: int = 0
    error: int = 0
    timeout: int = 0
    stopped_early: bool = False
    counterexamples: list[dict[str, Any]] = field(default_factory=list)
    signatures: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.failed == 0 and self.error == 0 and self.timeout == 0

    def as_dict(self) -> dict[str, Any]:
        return {"total": self.total, "passed": self.passed, "failed": self.failed,
                "error": self.error, "timeout": self.timeout,
                "stopped_early": self.stopped_early, "ok": self.ok,
                "signatures": self.signatures,
                "counterexamples": self.counterexamples}


def run_set(snapshot_dir: str | Path, samples: list[dict[str, Any]], *,
            timeout: float = 30.0, python: str | None = None,
            stop_on_first_fail: bool = False,
            run_fn: Callable | None = None) -> BehaviorResult:
    run_fn = run_fn or (lambda s: bc.run_candidate(snapshot_dir, s, timeout, python))
    field_for = {"pass": "passed", "fail": "failed", "error": "error", "timeout": "timeout"}
    res = BehaviorResult()
    for sample in samples:
        resp = run_fn(sample)
        verdict = bc.judge_sample(sample, resp)
        res.total += 1
        kind = verdict["verdict"]
        if kind == "pass":
            res.passed += 1
            continue
        attr = field_for[kind]
        setattr(res, attr, getattr(res, attr) + 1)
        sig = verdict.get("signature") or kind
        res.signatures[sig] = res.signatures.get(sig, 0) + 1
        res.counterexamples.append({"sample": sample, "verdict": verdict, "response": resp})
        if stop_on_first_fail:
            res.stopped_early = True
            break
    return res