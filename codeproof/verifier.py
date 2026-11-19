"""Out-of-process verification: upstream tests in Docker + behavior checks.

The candidate runs in an isolated Linux/CPython 3.12 container with no network
and no host mounts. The trusted judge holds expectations; the candidate only
produces observations and test output. Environment failures, coverage failures,
no-tests, abnormal skips and behavior failures are reported separately.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IMAGE_TAG = "codeproof-verifier:0.1"
DOCKERFILE = Path(__file__).resolve().parent.parent / "docker" / "verifier.Dockerfile"

# Faithful reproduction of V0 `make coverage` (requirements pre-installed).
RUN_SCRIPT = r"""
set -e
cd /work
python -VV
pip install --no-build-isolation --no-deps --editable . >/tmp/install.log 2>&1 || { cat /tmp/install.log; exit 90; }
coverage run --include="more_itertools/*.py" -m unittest 2>&1
rc=$?
coverage report --show-missing --fail-under=99 2>&1
cov_rc=$?
echo "UNITTEST_EXIT=$rc"
echo "COVERAGE_EXIT=$cov_rc"
exit $rc
"""


@dataclass
class UpstreamResult:
    status: str  # passed | failed | coverage_failed | env_error | no_tests
    exit_code: int
    tests_run: int | None = None
    failures: int | None = None
    errors: int | None = None
    skipped: int | None = None
    coverage_pct: float | None = None
    raw_output: str = ""
    python_version: str | None = None
    command: str = "make coverage (reproduced)"

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def build_image(tag: str = IMAGE_TAG) -> dict[str, Any]:
    proc = subprocess.run(
        ["docker", "build", "-t", tag, "-f", str(DOCKERFILE),
         str(DOCKERFILE.parent)],
        capture_output=True, text=True, encoding="utf-8")
    digest = ""
    if proc.returncode == 0:
        insp = subprocess.run(["docker", "image", "inspect", tag,
                               "--format", "{{.Id}}"],
                              capture_output=True, text=True)
        digest = insp.stdout.strip()
    return {"ok": proc.returncode == 0, "tag": tag, "image_id": digest,
            "log": proc.stdout[-2000:] + proc.stderr[-2000:]}


def _parse(output: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    m = re.search(r"Ran (\d+) tests?", output)
    if m:
        out["tests_run"] = int(m.group(1))
    m = re.search(r"FAILED \(([^)]*)\)", output)
    if m:
        for part in m.group(1).split(","):
            part = part.strip()
            kv = re.match(r"(\w+)=(\d+)", part)
            if kv:
                out[kv.group(1)] = int(kv.group(2))
    m = re.search(r"OK(?: \(skipped=(\d+)\))?", output)
    if m:
        out.setdefault("failures", 0)
        out.setdefault("errors", 0)
        if m.group(1):
            out["skipped"] = int(m.group(1))
    m = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", output)
    if m:
        out["coverage_pct"] = float(m.group(1))
    m = re.search(r"Python (\d+\.\d+\.\d+)", output)
    if m:
        out["python_version"] = m.group(1)
    return out


def run_upstream(snapshot_dir: str | Path, tag: str = IMAGE_TAG,
                 timeout: float = 600.0) -> UpstreamResult:
    """Copy the candidate into a fresh no-network container and run tests."""
    snapshot_dir = Path(snapshot_dir)
    name = None
    try:
        # Isolation: no network, no host mounts, ephemeral container removed
        # after the run. In-container root is used only so the faithful
        # `pip install --editable .` step can write site-packages; it grants no
        # host access because nothing is mounted and the network is disabled.
        create = subprocess.run(
            ["docker", "create", "--network", "none", "--user", "root",
             "--tmpfs", "/tmp:rw,size=256m",
             "--memory", "1g", "--cpus", "2", tag,
             "bash", "-c", RUN_SCRIPT],
            capture_output=True, text=True, encoding="utf-8")
        if create.returncode != 0:
            return UpstreamResult(status="env_error", exit_code=create.returncode,
                                  raw_output=create.stderr)
        name = create.stdout.strip()
        # Copy candidate into the container's writable /work tmpfs.
        with tempfile.TemporaryDirectory() as td:
            staged = Path(td) / "work"
            shutil.copytree(snapshot_dir, staged)
            cp = subprocess.run(["docker", "cp", str(staged) + "/.", f"{name}:/work"],
                                capture_output=True, text=True)
            if cp.returncode != 0:
                return UpstreamResult(status="env_error", exit_code=cp.returncode,
                                      raw_output=cp.stderr)
        run = subprocess.run(["docker", "start", "-a", name],
                             capture_output=True, text=True, timeout=timeout,
                             encoding="utf-8")
        output = run.stdout + run.stderr
        parsed = _parse(output)
        ue = re.search(r"UNITTEST_EXIT=(\d+)", output)
        ce = re.search(r"COVERAGE_EXIT=(\d+)", output)
        unit_rc = int(ue.group(1)) if ue else run.returncode
        cov_rc = int(ce.group(1)) if ce else None
        if parsed.get("tests_run", 0) == 0:
            status = "no_tests"
        elif unit_rc != 0:
            status = "failed"
        elif cov_rc not in (0, None):
            status = "coverage_failed"
        else:
            status = "passed"
        return UpstreamResult(status=status, exit_code=unit_rc,
                              tests_run=parsed.get("tests_run"),
                              failures=parsed.get("failures"),
                              errors=parsed.get("errors"),
                              skipped=parsed.get("skipped"),
                              coverage_pct=parsed.get("coverage_pct"),
                              raw_output=output,
                              python_version=parsed.get("python_version"))
    except subprocess.TimeoutExpired:
        return UpstreamResult(status="env_error", exit_code=-1,
                              raw_output="upstream test timeout")
    finally:
        if name:
            subprocess.run(["docker", "rm", "-f", name],
                           capture_output=True, text=True)