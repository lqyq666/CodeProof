from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codeproof.web import create_app

BUGGY = '''
from collections import defaultdict, deque
class bucket:
    def __init__(self, iterable, key):
        self._cache = defaultdict(deque)
        for x in iterable:
            self._cache[key(x)].append(x)
    def __contains__(self, key):
        return bool(self._cache[key])
    def __getitem__(self, key):
        return iter(self._cache[key])
    def __iter__(self):
        return iter(self._cache)
'''
FIXED = '''
from collections import deque
class bucket:
    def __init__(self, iterable, key):
        self._cache = {}
        for x in iterable:
            self._cache.setdefault(key(x), deque()).append(x)
    def __contains__(self, key):
        return key in self._cache
    def __getitem__(self, key):
        return iter(self._cache.get(key, ()))
    def __iter__(self):
        return iter(self._cache)
'''


def pkg(root: Path, src: str) -> Path:
    p = root / "more_itertools"
    p.mkdir(parents=True, exist_ok=True)
    (p / "__init__.py").write_text(src, encoding="utf-8")
    return root


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "state")
    return TestClient(app)


def submit(client, **kw):
    body = {"task": "bucket-fix", "repo": "more-itertools", "baseline_sha": "abc",
            "model": "test", **kw}
    return client.post("/runs", json=body)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["execution_available"] is False


def test_submit_idempotent_and_force_new(client):
    a = submit(client).json()
    b = submit(client).json()
    assert a["created"] is True and b["created"] is False
    assert a["run_id"] == b["run_id"]
    c = submit(client, force_new=True).json()
    assert c["created"] is True and c["run_id"] != a["run_id"]


def test_lifecycle_and_ledger(client):
    rid = submit(client).json()["run_id"]
    assert client.get(f"/runs/{rid}").json()["state"] == "active"
    assert client.post(f"/runs/{rid}/cancel").json()["cancelled"] is True
    assert client.get(f"/runs/{rid}").json()["state"] == "cancelled"
    assert client.post(f"/runs/{rid}/cancel").json()["cancelled"] is False
    led = client.get(f"/runs/{rid}/ledger").json()
    assert led["event_count"] >= 1
    assert led["credits"]["total_credits"] is None  # no model call happened
    assert led["tokens"]["requests_counted"] == 0


def test_get_missing_run_404(client):
    assert client.get("/runs/nope").status_code == 404


def test_verify_unavailable_503(client):
    r = client.post("/verify", json={})
    assert r.status_code == 503


def test_verify_buggy_and_fixed(tmp_path):
    v0 = pkg(tmp_path / "v0", BUGGY)
    fixed = pkg(tmp_path / "fixed", FIXED)
    app = create_app(tmp_path / "state", snapshot_dir=v0)
    c = TestClient(app)
    rb = c.post("/verify", json={"limit": 8}).json()
    assert rb["model_calls"] == 0
    assert rb["result"]["failed"] >= 1
    assert rb["verification_id"].startswith("verify-")
    rf = c.post("/verify", json={"snapshot_dir": str(fixed), "limit": 8}).json()
    assert rf["result"]["ok"] is True
    assert rf["verification_id"] != rb["verification_id"]


def test_recovery_across_instances(tmp_path):
    state = tmp_path / "state"
    c1 = TestClient(create_app(state))
    rid = submit(c1).json()["run_id"]
    c2 = TestClient(create_app(state))
    assert c2.get("/health").json()["runs_recovered"] >= 1
    assert c2.get(f"/runs/{rid}").json()["manifest"]["task"] == "bucket-fix"