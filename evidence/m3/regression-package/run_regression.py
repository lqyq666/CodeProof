#!/usr/bin/env python3
"""Standalone frozen-contract regression runner (stdlib only).

Usage: python run_regression.py <path-to-source-tree-containing-more_itertools>
Exit 0 if every sample passes, 1 if any fails, 2 on environment/import error.
"""
import json
import sys
from pathlib import Path


def observe(bucket_cls, data, operations):
    obj = bucket_cls(iter(data), key=lambda x: x[0])
    out = []
    for step in operations:
        kind = step["op"]
        if kind == "contains":
            out.append({"op": "contains", "key": step["key"],
                        "value": bool(step["key"] in obj)})
        elif kind == "read":
            out.append({"op": "read", "key": step["key"],
                        "value": list(obj[step["key"]])})
        elif kind == "keys":
            out.append({"op": "keys", "value": list(obj)})
    return out


def main():
    if len(sys.argv) != 2:
        print("usage: python run_regression.py <source-tree>")
        return 2
    tree = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(tree))
    try:
        from more_itertools import bucket
    except Exception as exc:
        print(f"IMPORT ERROR: {exc.__class__.__name__}: {exc}")
        return 2
    contract = json.loads(Path(__file__).with_name("contract.json").read_text("utf-8"))
    passed = failed = 0
    failures = []
    for sample in contract["samples"]:
        try:
            actual = observe(bucket, sample["input"], sample["operations"])
        except Exception as exc:
            failed += 1
            failures.append({"id": sample["id"], "error": f"{exc.__class__.__name__}: {exc}"})
            continue
        expected = sample["expected"]
        ok = len(actual) == len(expected)
        if ok:
            for exp, act in zip(expected, actual):
                if exp["op"] == "keys":
                    if set(exp["value"]) != set(act.get("value", [])):
                        ok = False
                elif exp != act:
                    ok = False
        if ok:
            passed += 1
        else:
            failed += 1
            failures.append({"id": sample["id"], "input": sample["input"],
                             "expected": expected, "actual": actual})
    print(f"contract={contract['contract_version']} samples={passed + failed} "
          f"passed={passed} failed={failed}")
    for f in failures[:10]:
        print("FAIL", json.dumps(f, ensure_ascii=False))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
