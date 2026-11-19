"""Acceptance tests for the usage ledger (M1 task book, section 3).

All numbers here are synthetic. They verify accounting rules, not real spend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeproof.ledger import (
    Event,
    Ledger,
    first_feedback,
    summarize_credits,
    summarize_tokens,
)


def req(run, dedup, inp, out, credits=None, phase="model_call", **usage):
    u = {"input_tokens": inp, "output_tokens": out}
    if credits is not None:
        u["credits"] = credits
    u.update(usage)
    return Event(run_id=run, purpose="product_run", phase=phase, attempt=1,
                 kind="model_request", dedup_key=dedup, source="sdk",
                 payload={"usage": u})


def snap(run, sid, role, total):
    return Event(run_id=run, purpose="product_run", phase="model_call", attempt=1,
                 kind="session_credits", dedup_key=f"{sid}:{role}:{total}",
                 source="sdk", payload={"session_id": sid, "role": role,
                                        "total_credits": total})


def test_synthetic_session_delta_and_tokens():
    """Start 4, requests 1 and 2, end 7 -> 3 Credits; tokens 160/30."""
    run = "r1"
    evs = [
        snap(run, "s1", "start", 4),
        req(run, "req-a", 100, 20, credits=1),
        req(run, "req-b", 60, 10, credits=2),
        snap(run, "s1", "end", 7),
    ]
    credits = summarize_credits(evs)
    assert credits.total_credits == 3
    assert credits.method == "session_delta"
    assert credits.status == "measured"
    tokens = summarize_tokens(evs)
    assert tokens.input_tokens == 160
    assert tokens.output_tokens == 30
    assert tokens.status == "measured"


def test_reread_same_result_does_not_add():
    run = "r1"
    evs = [
        snap(run, "s1", "start", 4),
        req(run, "req-a", 100, 20, credits=1),
        req(run, "req-b", 60, 10, credits=2),
        snap(run, "s1", "end", 7),
        snap(run, "s1", "end", 7),  # re-read same Result
    ]
    assert summarize_credits(evs).total_credits == 3


def test_real_zero_is_zero():
    run = "r0"
    evs = [snap(run, "s1", "start", 5), snap(run, "s1", "end", 5)]
    c = summarize_credits(evs)
    assert c.total_credits == 0
    assert c.status == "measured"


def test_missing_field_is_unknown_not_zero():
    run = "rm"
    evs = [req(run, "req-a", None, 20)]  # input missing
    t = summarize_tokens(evs)
    assert t.input_tokens is None
    assert t.output_tokens == 20
    assert t.status == "partial"
    # No credit info at all -> unknown, not 0.
    assert summarize_credits(evs).total_credits is None
    assert summarize_credits(evs).status == "unknown"


def test_streaming_fragments_are_not_consumption():
    run = "rs"
    evs = [
        snap(run, "s1", "start", 0),
        req(run, "req-a", 100, 20, credits=1),
        Event(run_id=run, purpose="product_run", phase="model_call", attempt=1,
              kind="stream_fragment", dedup_key="frag-1", source="sdk",
              payload={"usage": {"input_tokens": 999, "output_tokens": 999}}),
        snap(run, "s1", "end", 1),
    ]
    t = summarize_tokens(evs)
    assert t.input_tokens == 100
    assert t.output_tokens == 20
    assert summarize_credits(evs).total_credits == 1


def test_duplicate_events_after_restart(tmp_path: Path):
    path = tmp_path / "ledger.jsonl"
    led = Ledger(path)
    led.record(run_id="r1", purpose="product_run", phase="model_call", attempt=1,
               kind="model_request", dedup_key="req-a", source="sdk",
               payload={"usage": {"input_tokens": 100, "output_tokens": 20,
                                  "credits": 1}})
    led.record(run_id="r1", purpose="product_run", phase="model_call", attempt=1,
               kind="session_credits", dedup_key="s1:start:4", source="sdk",
               payload={"session_id": "s1", "role": "start", "total_credits": 4})
    led.record(run_id="r1", purpose="product_run", phase="model_call", attempt=1,
               kind="session_credits", dedup_key="s1:end:5", source="sdk",
               payload={"session_id": "s1", "role": "end", "total_credits": 5})
    # Simulate restart: rebuild from disk, then replay the same events.
    rebuilt = Ledger.rebuild(path)
    for ev in led.events():
        assert rebuilt.append(ev) is False  # dedup rejects replays
    evs = rebuilt.events("r1")
    assert summarize_tokens(evs).input_tokens == 100
    assert summarize_credits(evs).total_credits == 1


def test_new_session_cumulative_restarts():
    run = "rn"
    evs = [
        snap(run, "s1", "start", 4),
        snap(run, "s1", "end", 7),   # delta 3
        snap(run, "s2", "start", 0),  # new session starts from 0
        snap(run, "s2", "end", 2),   # delta 2
    ]
    c = summarize_credits(evs)
    assert c.total_credits == 5
    assert len(c.sessions) == 2


def test_two_parallel_phases_tokens_sum():
    run = "rp"
    evs = [
        req(run, "req-a", 100, 20, phase="baseline"),
        req(run, "req-b", 60, 10, phase="test"),
    ]
    t = summarize_tokens(evs)
    assert t.input_tokens == 160
    assert t.output_tokens == 30


def test_request_sum_fallback_when_no_cumulative():
    run = "rf"
    evs = [req(run, "req-a", 100, 20, credits=1),
           req(run, "req-b", 60, 10, credits=2)]
    c = summarize_credits(evs)
    assert c.total_credits == 3
    assert c.method == "request_sum"
    assert c.status == "partial"


def test_count_regression_marks_pending():
    run = "rc"
    evs = [snap(run, "s1", "start", 7), snap(run, "s1", "end", 4)]
    c = summarize_credits(evs)
    assert c.total_credits is None
    assert c.pending_reconciliation is True


def test_cache_fields_separate_from_input():
    run = "rca"
    evs = [req(run, "req-a", 100, 20, cache_read_tokens=50, cache_write_tokens=10)]
    t = summarize_tokens(evs)
    assert t.input_tokens == 100
    assert t.cache_read_tokens == 50
    assert t.cache_write_tokens == 10


def test_first_feedback_latency_and_first_round_pass():
    run = "rff"
    evs = [
        Event(run_id=run, purpose="product_run", phase="model_call", attempt=1,
              kind="candidate_submitted", dedup_key="c1", source="controller",
              ts_utc="2026-11-19T10:00:00+00:00"),
        Event(run_id=run, purpose="product_run", phase="test", attempt=1,
              kind="feedback_actionable", dedup_key="f1", source="verifier",
              ts_utc="2026-11-19T10:00:02+00:00"),
    ]
    fb = first_feedback(evs)
    assert fb["feedback_latency_ms"] == 2000.0
    assert fb["first_round_pass"] is False

    pass_evs = [
        Event(run_id=run, purpose="product_run", phase="model_call", attempt=1,
              kind="candidate_submitted", dedup_key="c1", source="controller",
              ts_utc="2026-11-19T10:00:00+00:00"),
        Event(run_id=run, purpose="product_run", phase="test", attempt=1,
              kind="verification_completed", dedup_key="v1", source="verifier",
              ts_utc="2026-11-19T10:00:03+00:00"),
    ]
    fb2 = first_feedback(pass_evs)
    assert fb2["first_round_pass"] is True
    assert fb2["feedback_latency_ms"] is None


def test_ledger_rebuild_matches_in_memory(tmp_path: Path):
    path = tmp_path / "l.jsonl"
    led = Ledger(path)
    for i in range(5):
        led.record(run_id="r1", purpose="product_run", phase="model_call",
                   attempt=1, kind="model_request", dedup_key=f"req-{i}",
                   source="sdk", payload={"usage": {"input_tokens": 10,
                                                    "output_tokens": 2}})
    rebuilt = Ledger.rebuild(path)
    assert len(rebuilt.events()) == 5
    assert summarize_tokens(rebuilt.events()).input_tokens == 50