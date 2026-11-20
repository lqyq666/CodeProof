"""Public workbench API (M1 step 6 / M4 4.4).

Exposes run submission/lifecycle, reconcilable usage accounting and a controlled
verification endpoint that replays the frozen behavior contract WITHOUT any model
call. State is recovered from disk on startup. When no candidate snapshot is
configured the execution endpoint reports an explicit unavailable status rather
than silently failing.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import bucket_contract as bc
from .behavior import run_set
from .ledger import Ledger, first_feedback, summarize_credits, summarize_tokens
from .run import RunManager


class SubmitBody(BaseModel):
    task: str
    repo: str
    baseline_sha: str
    model: str = "unknown"
    purpose: str = "product_run"
    sdk_version: str = "unknown"
    cli_version: str = "unknown"
    environment: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    force_new: bool = False


class VerifyBody(BaseModel):
    snapshot_dir: str | None = None
    full_set: bool = False
    limit: int = 20


def create_app(state_dir: Path, snapshot_dir: Path | None = None) -> FastAPI:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(state_dir / "ledger.jsonl")
    runs = RunManager(ledger, state_dir / "runs")
    default_snapshot = Path(snapshot_dir) if snapshot_dir else None

    app = FastAPI(title="CodeProof workbench", version="0.1.0")
    app.state.ledger = ledger
    app.state.runs = runs

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "runs_recovered": len(runs.list_runs()),
                "contract_version": bc.CONTRACT_VERSION,
                "contract_digest": bc.contract_digest(),
                "execution_available": default_snapshot is not None}

    @app.get("/contract")
    def contract() -> dict[str, Any]:
        return {"version": bc.CONTRACT_VERSION, "digest": bc.contract_digest(),
                "discovery_samples": len(bc.generate_discovery_set()),
                "histories": list(bc.HISTORIES)}

    @app.post("/runs")
    def submit(body: SubmitBody) -> dict[str, Any]:
        m, created = runs.submit(
            purpose=body.purpose, task=body.task, repo=body.repo,
            baseline_sha=body.baseline_sha, sdk_version=body.sdk_version,
            cli_version=body.cli_version, model=body.model,
            environment=body.environment, budget=body.budget,
            allowed_tools=body.allowed_tools, config=body.config,
            force_new=body.force_new)
        return {"run_id": m.run_id, "created": created, "state": runs.state(m.run_id)}

    @app.get("/runs")
    def list_runs() -> dict[str, Any]:
        return {"runs": runs.list_runs()}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        m = runs.get(run_id)
        if m is None:
            raise HTTPException(404, "run not found")
        events = ledger.events(run_id)
        return {"manifest": asdict(m),
                "state": runs.state(run_id),
                "model_calls": runs.model_call_count(run_id),
                "tokens": summarize_tokens(events).as_dict(),
                "credits": summarize_credits(events).as_dict(),
                "first_feedback": first_feedback(events)}

    @app.get("/runs/{run_id}/ledger")
    def run_ledger(run_id: str) -> dict[str, Any]:
        if runs.get(run_id) is None:
            raise HTTPException(404, "run not found")
        events = ledger.events(run_id)
        return {"event_count": len(events),
                "tokens": summarize_tokens(events).as_dict(),
                "credits": summarize_credits(events).as_dict()}

    @app.post("/runs/{run_id}/cancel")
    def cancel(run_id: str) -> dict[str, Any]:
        if runs.get(run_id) is None:
            raise HTTPException(404, "run not found")
        return {"cancelled": runs.cancel(run_id), "state": runs.state(run_id)}

    @app.post("/runs/{run_id}/finish")
    def finish(run_id: str, result: str = "delivered") -> dict[str, Any]:
        if runs.get(run_id) is None:
            raise HTTPException(404, "run not found")
        return {"finished": runs.finish(run_id, result), "state": runs.state(run_id)}

    @app.post("/verify")
    def verify(body: VerifyBody) -> dict[str, Any]:
        tree = Path(body.snapshot_dir) if body.snapshot_dir else default_snapshot
        verification_id = f"verify-{uuid.uuid4().hex[:12]}"
        if tree is None or not tree.exists():
            ledger.record(run_id=verification_id, purpose="product_run",
                          phase="verification", attempt=0, kind="verification_unavailable",
                          dedup_key=f"{verification_id}:unavailable", source="controller",
                          payload={"reason": "no candidate snapshot configured"})
            raise HTTPException(503, "execution endpoint unavailable: no snapshot")
        samples = bc.generate_discovery_set()
        if not body.full_set:
            samples = samples[: max(1, body.limit)]
        res = run_set(tree, samples, stop_on_first_fail=False)
        ledger.record(run_id=verification_id, purpose="product_run",
                      phase="verification", attempt=0, kind="verification_completed",
                      dedup_key=f"{verification_id}:completed", source="controller",
                      payload={"ok": res.ok, "total": res.total, "passed": res.passed,
                               "failed": res.failed, "model_calls": 0,
                               "contract_digest": bc.contract_digest()})
        return {"verification_id": verification_id, "model_calls": 0,
                "snapshot_dir": str(tree), "result": res.as_dict()}

    return app