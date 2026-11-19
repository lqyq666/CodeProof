"""Frozen bucket behavior contract: generator, trusted judge, minimizer.

Trusted side. Holds the original input and computes expectations OUT OF the
candidate process. The candidate adapter only returns public-interface
observations. This file is the frozen detector referenced by BUCKET_EVALUATION.

Contract (first round): default validator, non-empty string elements,
first-character key function, empty input legal. After querying a missing key
(membership or full read of its empty group), enumerating keys on the SAME
object must equal the set of first characters actually present in the input.
Enumeration order, caching strategy and internal structure may differ.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

CONTRACT_VERSION = "bucket-contract-1"
RECORDS = ["m0", "m1", "n0", "n1"]
P = "p"  # guaranteed missing, distinct from Q
Q = "q"
KEY_FN_DESC = "first character"

# Four pre-registered operation histories, each ending with key enumeration.
HISTORIES: dict[str, list[dict[str, str]]] = {
    "contains_p": [{"op": "contains", "key": P}, {"op": "keys"}],
    "read_p": [{"op": "read", "key": P}, {"op": "keys"}],
    "contains_p_twice_read_p": [
        {"op": "contains", "key": P}, {"op": "contains", "key": P},
        {"op": "read", "key": P}, {"op": "keys"}],
    "contains_p_read_q": [
        {"op": "contains", "key": P}, {"op": "read", "key": Q},
        {"op": "keys"}],
}

ADAPTER = Path(__file__).with_name("bucket_adapter.py")


def contract_digest() -> str:
    blob = json.dumps({"version": CONTRACT_VERSION, "records": RECORDS,
                       "p": P, "q": Q, "histories": HISTORIES},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def real_keys(data: list[str]) -> set[str]:
    return {x[0] for x in data}


def generate_discovery_set() -> list[dict[str, Any]]:
    """341 inputs (length 0..4 over 4 records) x 4 histories = 1364 samples."""
    samples = []
    idx = 0
    for length in range(0, 5):
        for combo in itertools.product(RECORDS, repeat=length):
            data = list(combo)
            for name, ops in HISTORIES.items():
                samples.append({"id": f"s{idx:04d}", "input": data,
                                "history": name, "operations": ops})
                idx += 1
    return samples


def expected_observations(data: list[str],
                          operations: list[dict[str, str]]) -> list[dict]:
    keys = real_keys(data)
    out = []
    for step in operations:
        kind = step["op"]
        if kind == "contains":
            out.append({"op": "contains", "key": step["key"],
                        "value": step["key"] in keys})
        elif kind == "read":
            out.append({"op": "read", "key": step["key"],
                        "value": [x for x in data if x[0] == step["key"]]})
        elif kind == "keys":
            out.append({"op": "keys", "value": sorted(keys)})
    return out


def judge_sample(sample: dict[str, Any], response: dict[str, Any]) -> dict:
    """Compare candidate observations to trusted expectations.

    Verdicts: pass | fail | error | timeout. The failure signature for this
    defect is a missing key appearing in the final enumerated key set.
    """
    if not response.get("ok"):
        err = response.get("error", "")
        verdict = "timeout" if response.get("timeout") else "error"
        return {"verdict": verdict, "signature": None, "error": err,
                "sample_id": sample["id"]}
    expected = expected_observations(sample["input"], sample["operations"])
    actual = response.get("observations", [])
    if len(actual) != len(expected):
        return {"verdict": "error", "signature": "protocol_length",
                "error": "observation count mismatch", "sample_id": sample["id"]}
    diffs = []
    signature = None
    for exp, act in zip(expected, actual):
        if exp["op"] == "keys":
            exp_set = set(exp["value"])
            act_set = set(act.get("value", []))
            if exp_set != act_set:
                leaked = sorted(act_set - exp_set)
                missing = sorted(exp_set - act_set)
                diffs.append({"op": "keys", "expected": sorted(exp_set),
                              "actual": sorted(act_set), "leaked": leaked,
                              "missing": missing})
                if leaked:
                    signature = "missing_key_in_enumeration"
        elif exp != act:
            diffs.append({"op": exp["op"], "expected": exp, "actual": act})
    if diffs:
        return {"verdict": "fail", "signature": signature or "value_mismatch",
                "diffs": diffs, "sample_id": sample["id"]}
    return {"verdict": "pass", "signature": None, "sample_id": sample["id"]}


def run_candidate(snapshot_dir: str | Path, sample: dict[str, Any],
                  timeout: float = 30.0,
                  python: str | None = None) -> dict[str, Any]:
    """Run the adapter in a fresh isolated candidate process via subprocess."""
    req = json.dumps({"snapshot_dir": str(snapshot_dir),
                      "input": sample["input"],
                      "operations": sample["operations"]}, ensure_ascii=False)
    exe = python or sys.executable
    try:
        proc = subprocess.run([exe, str(ADAPTER)], input=req, capture_output=True,
                              text=True, timeout=timeout, encoding="utf-8")
    except subprocess.TimeoutExpired:
        return {"ok": False, "timeout": True, "error": "candidate timeout"}
    if proc.returncode != 0 and not proc.stdout.strip():
        return {"ok": False, "error": f"exit {proc.returncode}: {proc.stderr.strip()[:500]}"}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"bad json: {proc.stdout[:200]} {proc.stderr[:200]}"}


def minimize(sample: dict[str, Any], snapshot_dir: str | Path,
             run_fn: Callable | None = None,
             timeout: float = 30.0) -> dict[str, Any]:
    """Shrink input first, then operations, keeping the failure signature.

    Each step re-runs on a fresh object. Returns the minimal sample, the
    reduction trace and the original/minimal actual outputs.
    """
    run_fn = run_fn or (lambda s: run_candidate(snapshot_dir, s, timeout))
    target_sig = "missing_key_in_enumeration"

    def fails(s: dict) -> tuple[bool, dict]:
        resp = run_fn(s)
        verdict = judge_sample(s, resp)
        return verdict["verdict"] == "fail" and verdict.get("signature") == target_sig, resp

    original_ok, original_resp = fails(sample)
    trace = [{"step": "original", "input": sample["input"],
              "operations": sample["operations"], "fails": original_ok}]
    if not original_ok:
        return {"minimal": sample, "trace": trace, "original_response": original_resp,
                "minimal_response": original_resp, "reduced": False}

    current = dict(sample)
    # 1) Remove input elements one at a time (prefer deleting input).
    i = 0
    while i < len(current["input"]):
        trial = dict(current, input=current["input"][:i] + current["input"][i + 1:])
        ok, _ = fails(trial)
        if ok:
            current = trial
            trace.append({"step": "drop_input", "input": current["input"], "fails": True})
        else:
            i += 1
    # 2) Remove operations (keep the final keys enumeration).
    ops = current["operations"]
    j = 0
    while j < len(ops) - 1:
        trial_ops = ops[:j] + ops[j + 1:]
        trial = dict(current, operations=trial_ops)
        ok, _ = fails(trial)
        if ok:
            ops = trial_ops
            current = dict(current, operations=ops)
            trace.append({"step": "drop_op", "operations": ops, "fails": True})
        else:
            j += 1
    _, minimal_resp = fails(current)
    return {"minimal": current, "trace": trace, "original_response": original_resp,
            "minimal_response": minimal_resp, "reduced": True}


def stability_check(sample: dict[str, Any], snapshot_dir: str | Path,
                    runs: int = 3, timeout: float = 30.0) -> dict[str, Any]:
    """Re-run the minimal sample in N fresh processes; flag instability."""
    signatures = []
    for _ in range(runs):
        resp = run_candidate(snapshot_dir, sample, timeout)
        verdict = judge_sample(sample, resp)
        signatures.append((verdict["verdict"], verdict.get("signature")))
    stable = len(set(signatures)) == 1
    return {"stable": stable, "runs": runs, "signatures": signatures}