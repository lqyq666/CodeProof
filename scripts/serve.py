"""Launch the CodeProof workbench API.

Usage: uv run python scripts/serve.py [--port 8000] [--snapshot PATH]
Defaults the execution snapshot to the local V0 snapshot when present.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codeproof.web import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SNAPSHOT = ROOT.parent / "codeproof-workspaces" / "v0-snapshot"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--state-dir", default=str(ROOT / "runs" / "workbench"))
    ap.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    args = ap.parse_args()
    snapshot = Path(args.snapshot) if args.snapshot else None
    if snapshot is not None and not snapshot.exists():
        snapshot = None
    app = create_app(Path(args.state_dir), snapshot_dir=snapshot)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()