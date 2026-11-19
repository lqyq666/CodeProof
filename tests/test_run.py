"""Acceptance tests for run lifecycle (M1 step 1 and step 6)."""

from __future__ import annotations

from pathlib import Path

from codeproof.ledger import Ledger
from codeproof.run import RunManager, ACTIVE, CANCELLED


def make_manager(tmp_path: Path):
    led = Ledger(tmp_path / "ledger.jsonl")
    return led, RunManager(led, tmp_path / "manifests")


def submit(mgr, **over):
    kwargs = dict(purpose="product_run", task="fix bucket", repo="more-itertools",
                  baseline_sha="b2f3aff", sdk_version="1.0.17", cli_version="0.5.14",
                  model="qoder-default", environment={"image": "python:3.12-slim"},
                  budget={"credits_cap": 50}, allowed_tools=["Read", "Grep"],
                  config={})
    kwargs.update(over)
    return mgr.submit(**kwargs)


def test_idempotent_submit_returns_same_id(tmp_path):
    _, mgr = make_manager(tmp_path)
    m1, created1 = submit(mgr)
    m2, created2 = submit(mgr)
    assert created1 is True
    assert created2 is False
    assert m1.run_id == m2.run_id


def test_force_new_gets_new_id(tmp_path):
    _, mgr = make_manager(tmp_path)
    m1, _ = submit(mgr)
    m2, created2 = submit(mgr, force_new=True)
    assert created2 is True
    assert m1.run_id != m2.run_id


def test_cancel_before_model_call_proves_zero_calls(tmp_path):
    led, mgr = make_manager(tmp_path)
    m, _ = submit(mgr)
    assert mgr.model_call_count(m.run_id) == 0
    assert mgr.cancel(m.run_id) is True
    assert mgr.state(m.run_id) == CANCELLED
    assert mgr.model_call_count(m.run_id) == 0
    assert mgr.can_schedule_call(m.run_id) is False
    # No model_request events were ever recorded.
    assert not [e for e in led.events(m.run_id) if e.kind == "model_request"]


def test_cancel_stops_new_calls(tmp_path):
    led, mgr = make_manager(tmp_path)
    m, _ = submit(mgr)
    led.record(run_id=m.run_id, purpose="product_run", phase="model_call",
               attempt=1, kind="model_request", dedup_key="req-a", source="sdk",
               payload={"usage": {"input_tokens": 10, "output_tokens": 2}})
    assert mgr.model_call_count(m.run_id) == 1
    mgr.cancel(m.run_id)
    assert mgr.can_schedule_call(m.run_id) is False


def test_recovery_after_restart(tmp_path):
    led, mgr = make_manager(tmp_path)
    m, _ = submit(mgr)
    mgr.cancel(m.run_id)
    # Simulate restart: fresh manager + ledger from disk.
    led2 = Ledger.rebuild(tmp_path / "ledger.jsonl")
    mgr2 = RunManager(led2, tmp_path / "manifests")
    assert mgr2.state(m.run_id) == CANCELLED
    assert mgr2.get(m.run_id).task == "fix bucket"
    # Re-submitting the same cancelled request creates a new run.
    m2, created = submit(mgr2)
    assert created is True
    assert m2.run_id != m.run_id


def test_double_submit_no_duplicate_billing(tmp_path):
    led, mgr = make_manager(tmp_path)
    m1, _ = submit(mgr)
    m2, _ = submit(mgr)
    created_events = [e for e in led.events() if e.kind == "run_created"]
    assert len(created_events) == 1
    assert m1.run_id == m2.run_id


def test_finish_transitions_to_terminal(tmp_path):
    _, mgr = make_manager(tmp_path)
    m, _ = submit(mgr)
    assert mgr.state(m.run_id) == ACTIVE
    assert mgr.finish(m.run_id, "delivered") is True
    assert mgr.finish(m.run_id, "delivered") is False  # already terminal
    assert mgr.can_schedule_call(m.run_id) is False