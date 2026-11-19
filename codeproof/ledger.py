"""Append-only event ledger and reconcilable usage accounting for CodeProof.

Design rules (M1 task book, section 3):
- Every event carries run_id, purpose, phase, attempt, UTC time, source, kind
  and a stable dedup key. Replaying the same event never adds consumption.
- Credits settle from a dedicated session's cumulative start/end snapshots.
  Request-level credits are attribution only and are never added to the total.
- Tokens sum over completed, deduplicated requests; cache read/write are kept
  separate from input/output. Streaming fragments and repeated Results are not
  new consumption.
- A missing field is "unknown" (None), never coerced to 0. An explicit 0 from a
  source, or a provable absence of calls, is a real 0.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PURPOSES = ("development", "product_run", "evaluation")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    run_id: str
    purpose: str
    phase: str
    attempt: int
    kind: str
    dedup_key: str
    source: str
    ts_utc: str = field(default_factory=utc_now)
    mono_ms: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> "Event":
        return cls(**json.loads(line))


class Ledger:
    """Thread-safe append-only ledger persisted as JSONL, rebuildable from disk."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self._events: list[Event] = []
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._mono_origin = time.monotonic()
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            ev = Event.from_json(line)
            if ev.dedup_key in self._seen:
                continue
            self._seen.add(ev.dedup_key)
            self._events.append(ev)

    def mono_ms(self) -> float:
        return (time.monotonic() - self._mono_origin) * 1000.0

    def append(self, ev: Event) -> bool:
        """Append an event. Returns False if the dedup key was already seen."""
        if ev.purpose not in PURPOSES:
            raise ValueError(f"unknown purpose: {ev.purpose}")
        with self._lock:
            if ev.dedup_key in self._seen:
                return False
            self._seen.add(ev.dedup_key)
            self._events.append(ev)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(ev.to_json() + "\n")
            return True

    def record(self, **kwargs: Any) -> bool:
        if "mono_ms" not in kwargs or kwargs["mono_ms"] is None:
            kwargs["mono_ms"] = self.mono_ms()
        return self.append(Event(**kwargs))

    def events(self, run_id: str | None = None) -> list[Event]:
        with self._lock:
            if run_id is None:
                return list(self._events)
            return [e for e in self._events if e.run_id == run_id]

    @classmethod
    def rebuild(cls, path: Path) -> "Ledger":
        return cls(path)


def _num(value: Any) -> float | None:
    """Return a number only when the source actually provided one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


@dataclass
class TokenUsage:
    input_tokens: float | None = None
    output_tokens: float | None = None
    cache_read_tokens: float | None = None
    cache_write_tokens: float | None = None
    requests_counted: int = 0
    status: str = "unknown"  # measured | partial | unknown

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CreditUsage:
    total_credits: float | None = None
    method: str = "unknown"  # session_delta | request_sum | unknown
    sessions: list[dict[str, Any]] = field(default_factory=list)
    pending_reconciliation: bool = False
    status: str = "unknown"  # measured | partial | unknown

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize_tokens(events: Iterable[Event]) -> TokenUsage:
    """Sum input/output over completed, deduplicated model requests.

    Cache fields are kept separate. A request missing a token field leaves that
    aggregate partial/unknown rather than fabricating a zero.
    """
    seen: set[str] = set()
    inp = out = cr = cw = 0.0
    have_in = have_out = have_cr = have_cw = False
    any_missing = False
    counted = 0
    for ev in events:
        if ev.kind != "model_request":
            continue
        if ev.dedup_key in seen:
            continue
        seen.add(ev.dedup_key)
        usage = ev.payload.get("usage", {})
        counted += 1
        i = _num(usage.get("input_tokens"))
        o = _num(usage.get("output_tokens"))
        r = _num(usage.get("cache_read_tokens"))
        w = _num(usage.get("cache_write_tokens"))
        if i is None or o is None:
            any_missing = True
        if i is not None:
            inp += i
            have_in = True
        if o is not None:
            out += o
            have_out = True
        if r is not None:
            cr += r
            have_cr = True
        if w is not None:
            cw += w
            have_cw = True
    result = TokenUsage(
        input_tokens=inp if have_in else None,
        output_tokens=out if have_out else None,
        cache_read_tokens=cr if have_cr else None,
        cache_write_tokens=cw if have_cw else None,
        requests_counted=counted,
    )
    if counted == 0:
        result.status = "unknown"
    elif any_missing or not (have_in and have_out):
        result.status = "partial"
    else:
        result.status = "measured"
    return result


def summarize_credits(events: Iterable[Event]) -> CreditUsage:
    """Settle Credits from session cumulative start/end deltas.

    Falls back to a request sum only when requests are complete and deduplicable
    and no valid cumulative interval exists. Request credits are never added on
    top of a session delta.
    """
    events = list(events)
    snapshots: dict[str, dict[str, float]] = {}
    order: list[str] = []
    for ev in events:
        if ev.kind != "session_credits":
            continue
        sid = ev.payload.get("session_id")
        role = ev.payload.get("role")
        val = _num(ev.payload.get("total_credits"))
        if sid is None or role not in ("start", "end") or val is None:
            continue
        if sid not in snapshots:
            snapshots[sid] = {}
            order.append(sid)
        # Keep the first start and the last end for each session interval.
        if role == "start" and "start" not in snapshots[sid]:
            snapshots[sid]["start"] = val
        if role == "end":
            snapshots[sid]["end"] = val

    sessions: list[dict[str, Any]] = []
    total = 0.0
    any_valid = False
    any_pending = False
    for sid in order:
        snap = snapshots[sid]
        start = snap.get("start")
        end = snap.get("end")
        if start is not None and end is not None:
            if end < start:
                # Count regression: keep both observations, mark pending.
                sessions.append({"session_id": sid, "start": start, "end": end,
                                 "delta": None, "pending": True})
                any_pending = True
            else:
                delta = end - start
                sessions.append({"session_id": sid, "start": start, "end": end,
                                 "delta": delta, "pending": False})
                total += delta
                any_valid = True
        else:
            sessions.append({"session_id": sid, "start": start, "end": end,
                             "delta": None, "pending": True})
            any_pending = True

    if any_valid:
        return CreditUsage(
            total_credits=total,
            method="session_delta",
            sessions=sessions,
            pending_reconciliation=any_pending,
            status="partial" if any_pending else "measured",
        )

    # Fallback: complete, deduplicable request records, no cumulative interval.
    seen: set[str] = set()
    req_total = 0.0
    have_any = False
    complete = True
    for ev in events:
        if ev.kind != "model_request":
            continue
        if ev.dedup_key in seen:
            continue
        seen.add(ev.dedup_key)
        c = _num(ev.payload.get("usage", {}).get("credits"))
        if c is None:
            complete = False
            continue
        req_total += c
        have_any = True
    if have_any and complete:
        return CreditUsage(total_credits=req_total, method="request_sum",
                           sessions=sessions, status="partial")
    return CreditUsage(total_credits=None, method="unknown", sessions=sessions,
                       pending_reconciliation=any_pending, status="unknown")


def first_feedback(events: Iterable[Event]) -> dict[str, Any]:
    """Compute candidate feedback latency from persisted timestamps."""
    events = list(events)
    submitted = next((e for e in events if e.kind == "candidate_submitted"), None)
    actionable = next((e for e in events if e.kind == "feedback_actionable"), None)
    verified = next((e for e in events if e.kind == "verification_completed"), None)
    out: dict[str, Any] = {
        "first_candidate_ts": submitted.ts_utc if submitted else None,
        "first_actionable_ts": actionable.ts_utc if actionable else None,
        "first_verification_ts": verified.ts_utc if verified else None,
        "feedback_latency_ms": None,
        "first_round_pass": False,
    }
    if submitted and actionable:
        t0 = datetime.fromisoformat(submitted.ts_utc)
        t1 = datetime.fromisoformat(actionable.ts_utc)
        out["feedback_latency_ms"] = (t1 - t0).total_seconds() * 1000.0
    elif submitted and verified and not actionable:
        out["first_round_pass"] = True
    return out