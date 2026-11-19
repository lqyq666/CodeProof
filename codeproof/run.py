"""Run lifecycle: manifest, idempotent submission, cancellation, recovery.

A run is the unit of work and accounting. The manifest freezes the inputs
(SDK/CLI version, model, task, repo, baseline, environment, budget, allowed
tools) before any model call. Submission is idempotent on a request
fingerprint; an explicit "new run" forces a fresh ID. Cancellation stops new
calls from being scheduled. State is recoverable from the persisted ledger.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .ledger import Ledger, utc_now

# Run states. A cancelled or finished run never schedules new model calls.
ACTIVE = "active"
CANCELLED = "cancelled"
FINISHED = "finished"
TERMINAL = (CANCELLED, FINISHED)


@dataclass
class RunManifest:
    run_id: str
    purpose: str
    task: str
    repo: str
    baseline_sha: str
    sdk_version: str
    cli_version: str
    model: str
    environment: dict[str, Any]
    budget: dict[str, Any]
    allowed_tools: list[str]
    request_fingerprint: str
    created_utc: str = field(default_factory=utc_now)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


def fingerprint(task: str, repo: str, baseline_sha: str,
                config: dict[str, Any]) -> str:
    blob = json.dumps({"task": task, "repo": repo, "baseline": baseline_sha,
                       "config": config}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class RunManager:
    """Persists run manifests and derives lifecycle state from the ledger."""

    def __init__(self, ledger: Ledger, manifest_dir: Path):
        self.ledger = ledger
        self.manifest_dir = Path(manifest_dir)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        self._manifests: dict[str, RunManifest] = {}
        self._fp_index: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        for path in sorted(self.manifest_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            m = RunManifest(**data)
            self._manifests[m.run_id] = m
            self._fp_index.setdefault(m.request_fingerprint, m.run_id)

    def _state(self, run_id: str) -> str:
        for ev in self.ledger.events(run_id):
            if ev.kind == "run_cancelled":
                return CANCELLED
            if ev.kind == "run_finished":
                return FINISHED
        return ACTIVE

    def state(self, run_id: str) -> str:
        return self._state(run_id)

    def model_call_count(self, run_id: str) -> int:
        seen: set[str] = set()
        for ev in self.ledger.events(run_id):
            if ev.kind == "model_request":
                seen.add(ev.dedup_key)
        return len(seen)

    def submit(self, *, purpose: str, task: str, repo: str, baseline_sha: str,
               sdk_version: str, cli_version: str, model: str,
               environment: dict[str, Any], budget: dict[str, Any],
               allowed_tools: list[str], config: dict[str, Any] | None = None,
               force_new: bool = False) -> tuple[RunManifest, bool]:
        """Return (manifest, created). Idempotent unless force_new is set."""
        config = config or {}
        fp = fingerprint(task, repo, baseline_sha, config)
        if not force_new and fp in self._fp_index:
            existing = self._manifests[self._fp_index[fp]]
            if self._state(existing.run_id) == ACTIVE:
                return existing, False
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        m = RunManifest(run_id=run_id, purpose=purpose, task=task, repo=repo,
                        baseline_sha=baseline_sha, sdk_version=sdk_version,
                        cli_version=cli_version, model=model,
                        environment=environment, budget=budget,
                        allowed_tools=allowed_tools, request_fingerprint=fp)
        path = self.manifest_dir / f"{run_id}.json"
        path.write_text(m.to_json(), encoding="utf-8")
        self._manifests[run_id] = m
        self._fp_index.setdefault(fp, run_id)
        self.ledger.record(run_id=run_id, purpose=purpose, phase="queue",
                           attempt=0, kind="run_created",
                           dedup_key=f"{run_id}:created", source="controller",
                           payload={"manifest": json.loads(m.to_json())})
        return m, True

    def get(self, run_id: str) -> RunManifest | None:
        return self._manifests.get(run_id)

    def can_schedule_call(self, run_id: str) -> bool:
        return self._state(run_id) == ACTIVE

    def cancel(self, run_id: str) -> bool:
        if self._state(run_id) != ACTIVE:
            return False
        self.ledger.record(run_id=run_id, purpose=self._manifests[run_id].purpose,
                           phase="queue", attempt=0, kind="run_cancelled",
                           dedup_key=f"{run_id}:cancelled", source="controller",
                           payload={"model_calls_at_cancel":
                                    self.model_call_count(run_id)})
        return True

    def finish(self, run_id: str, result: str) -> bool:
        if self._state(run_id) != ACTIVE:
            return False
        self.ledger.record(run_id=run_id, purpose=self._manifests[run_id].purpose,
                           phase="delivery", attempt=0, kind="run_finished",
                           dedup_key=f"{run_id}:finished", source="controller",
                           payload={"result": result})
        return True

    def list_runs(self) -> list[dict[str, Any]]:
        out = []
        for run_id, m in self._manifests.items():
            out.append({"run_id": run_id, "state": self._state(run_id),
                        "task": m.task, "baseline_sha": m.baseline_sha,
                        "model_calls": self.model_call_count(run_id),
                        "created_utc": m.created_utc})
        return out