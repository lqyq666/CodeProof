# Frozen bucket regression package

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
