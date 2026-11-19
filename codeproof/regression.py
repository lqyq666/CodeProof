"""Transferable regression package export (M3 step 3.4).

Produces a self-contained directory another developer can run in a clean
environment with only the Python standard library and a candidate source tree.
It embeds the frozen contract samples and their trusted expected observations,
plus a standalone runner that exercises the public bucket interface. No
CodeProof import, no model key, no network. V0 must fail; a correct fix passes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import bucket_contract as bc

_RUNNER = '''#!/usr/bin/env python3
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
'''

_README = '''# Frozen bucket regression package

Self-contained replay of the frozen `bucket` behavior contract. Requires only
Python 3.12+ standard library and a source tree that contains `more_itertools`.
No CodeProof, no model key, no network.

## Run

```bash
python run_regression.py /path/to/source-tree
```

- The defective V0 tree FAILS (a queried missing key leaks into key enumeration).
- A correct fix PASSES every sample.

Exit codes: 0 all pass, 1 any fail, 2 import/environment error.

`contract.json` holds the frozen samples and their trusted expected
observations. `manifest.json` records the contract digest and file hashes.
'''


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_regression_package(dest: Path, samples: list[dict[str, Any]] | None = None,
                              v0_dir: Path | None = None,
                              fixed_dir: Path | None = None) -> dict[str, Any]:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    samples = samples if samples is not None else bc.generate_discovery_set()
    embedded = [{"id": s["id"], "input": s["input"], "history": s.get("history"),
                 "operations": s["operations"],
                 "expected": bc.expected_observations(s["input"], s["operations"])}
                for s in samples]
    contract = {"contract_version": bc.CONTRACT_VERSION,
                "contract_digest": bc.contract_digest(),
                "key_function": bc.KEY_FN_DESC, "samples": embedded}
    (dest / "contract.json").write_text(
        json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    (dest / "run_regression.py").write_text(_RUNNER, encoding="utf-8")
    (dest / "README.md").write_text(_README, encoding="utf-8")

    self_test: dict[str, Any] = {}
    for label, tree in (("v0", v0_dir), ("fixed", fixed_dir)):
        if tree is not None:
            self_test[label] = _run_package(dest, Path(tree))

    manifest = {"contract_version": bc.CONTRACT_VERSION,
                "contract_digest": bc.contract_digest(),
                "sample_count": len(embedded),
                "files": {name: _sha(dest / name)
                          for name in ("contract.json", "run_regression.py", "README.md")},
                "self_test": self_test}
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _run_package(pkg_dir: Path, tree: Path) -> dict[str, Any]:
    import subprocess
    import sys
    proc = subprocess.run(
        [sys.executable, str(pkg_dir / "run_regression.py"), str(tree)],
        capture_output=True, text=True, encoding="utf-8", timeout=600)
    head = proc.stdout.strip().splitlines()
    return {"exit_code": proc.returncode,
            "summary": head[0] if head else "",
            "passed": proc.returncode == 0}