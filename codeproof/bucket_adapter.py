"""Candidate-side adapter for the frozen bucket behavior contract.

This script runs in an ISOLATED candidate process. It receives raw input and an
operation sequence, exercises ONLY the public bucket interface, and returns
observations. It never computes expected values and never sees the judge. The
trusted validator (bucket_contract.py) holds expectations out of process.

Protocol (stdin JSON -> stdout JSON):
  request:  {"snapshot_dir": str, "input": [str, ...], "operations": [op, ...]}
  op:       {"op": "contains"|"read"|"keys", "key": str?}
  response: {"ok": true, "observations": [...]} or {"ok": false, "error": str}

Each sample uses a fresh bucket object; operations act on that same object; the
object is not enumerated before the queries. The final operation is "keys".
"""

from __future__ import annotations

import json
import sys


def run(snapshot_dir: str, data: list, operations: list) -> dict:
    sys.path.insert(0, snapshot_dir)
    try:
        from more_itertools import bucket  # noqa: PLC0415
    except Exception as exc:  # import failure is an environment/protocol error
        return {"ok": False, "error": f"import: {exc.__class__.__name__}: {exc}"}

    try:
        obj = bucket(iter(data), key=lambda x: x[0])
        observations = []
        for step in operations:
            kind = step["op"]
            if kind == "contains":
                observations.append({"op": "contains", "key": step["key"],
                                     "value": bool(step["key"] in obj)})
            elif kind == "read":
                observations.append({"op": "read", "key": step["key"],
                                     "value": list(obj[step["key"]])})
            elif kind == "keys":
                observations.append({"op": "keys", "value": list(obj)})
            else:
                return {"ok": False, "error": f"unknown op: {kind}"}
        return {"ok": True, "observations": observations}
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}


def main() -> None:
    try:
        req = json.loads(sys.stdin.read())
        result = run(req["snapshot_dir"], req["input"], req["operations"])
    except Exception as exc:
        result = {"ok": False, "error": f"protocol: {exc.__class__.__name__}: {exc}"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()