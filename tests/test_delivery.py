from pathlib import Path

import pytest

from codeproof.behavior import BehaviorResult
from codeproof.delivery import decide, deliver
from codeproof.patch import apply_candidate
from codeproof.verifier import UpstreamResult
from codeproof.workspace import Snapshot


@pytest.fixture()
def snap(tmp_path: Path) -> Snapshot:
    root = tmp_path / "v0"
    (root / "more_itertools").mkdir(parents=True)
    (root / "more_itertools" / "more.py").write_bytes(b"VAL = 0\n")
    (root / "more_itertools" / "__init__.py").write_bytes(b"X = 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_more.py").write_bytes(b"assert True\n")
    return Snapshot.create(root, "base")


@pytest.fixture()
def cand(snap, tmp_path):
    return apply_candidate(snap, {"more_itertools/more.py": b"VAL = 1\n"},
                           tmp_path / "cand")


def good_behavior():
    return BehaviorResult(total=5, passed=5)


def bad_behavior():
    return BehaviorResult(total=5, passed=4, failed=1,
                          signatures={"missing_key_in_enumeration": 1})


@pytest.mark.parametrize("prot,beh,up,expected", [
    (True, True, "passed", "deliver"),
    (True, False, "passed", "block"),
    (True, True, "failed", "block"),
    (True, True, "coverage_failed", "block"),
    (True, True, "env_error", "block"),
    (True, True, "no_tests", "block"),
    (True, None, "passed", "block"),
    (True, True, None, "block"),
    (False, True, "passed", "block"),
])
def test_decide_truth_table(prot, beh, up, expected):
    decision, reasons = decide(prot, beh, up)
    assert decision == expected
    if expected == "block":
        assert reasons


def test_deliver_success(snap, cand, tmp_path):
    calls = {"up": 0}

    def up():
        calls["up"] += 1
        return UpstreamResult(status="passed", exit_code=0, tests_run=919,
                              coverage_pct=100.0)

    report = deliver(snap, cand, run_id="r1", samples=[],
                     out_dir=tmp_path / "out",
                     behavior_fn=good_behavior, upstream_fn=up)
    assert report.decision == "deliver"
    assert report.reasons == []
    assert calls["up"] == 1
    assert (tmp_path / "out" / "delivery.json").exists()


def test_behavior_failure_skips_upstream(snap, cand, tmp_path):
    calls = {"up": 0}

    def up():
        calls["up"] += 1
        return UpstreamResult(status="passed", exit_code=0)

    report = deliver(snap, cand, run_id="r2", samples=[],
                     behavior_fn=bad_behavior, upstream_fn=up)
    assert report.decision == "block"
    assert "behavior_contract_failed" in report.reasons
    assert calls["up"] == 0  # expensive layer not paid when cheap layer fails
    assert report.upstream is None


def test_upstream_env_error_blocks(snap, cand):
    report = deliver(snap, cand, run_id="r3", samples=[],
                     behavior_fn=good_behavior,
                     upstream_fn=lambda: UpstreamResult(status="env_error", exit_code=1))
    assert report.decision == "block"
    assert "upstream_env_error" in report.reasons


def test_protected_violation_blocks(snap, cand):
    cand.protected_digests = {"tests/test_more.py": "tampered"}
    report = deliver(snap, cand, run_id="r4", samples=[],
                     behavior_fn=good_behavior,
                     upstream_fn=lambda: UpstreamResult(status="passed", exit_code=0))
    assert report.decision == "block"
    assert "protected_boundary_violated" in report.reasons