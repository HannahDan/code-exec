"""Executor tests. These create real Jobs in the local cluster's `sandbox` namespace."""

import asyncio
import json
import threading
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db, executor
from app.main import app

ARTIFACT = Path(__file__).parent.parent / "artifacts" / "adversarial_results.json"
OBSERVED: dict[str, dict] = {}

ADD_TESTS = """
from solution import add

def test_small():
    assert add(2, 3) == 5

def test_negative():
    assert add(-1, -4) == -5

def test_zero():
    assert add(0, 0) == 0
"""


def run(code: str, tests: str = ADD_TESTS) -> executor.RunResult:
    return asyncio.run(executor.run_job(code, tests, run_id=uuid4().hex[:16]))


def record(name: str, result: executor.RunResult, **extra) -> None:
    """Keep what actually happened so it lands in artifacts/ as evidence."""
    OBSERVED[name] = {
        "status": result.status,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "stdout_bytes": len(result.stdout.encode()),
        "truncated": result.test_results.get("truncated"),
        "tests": [{k: t[k] for k in ("name", "passed", "error")} for t in result.test_results.get("tests", [])],
        "stderr_tail": result.stderr[-300:],
        **extra,
    }


@pytest.fixture(scope="module", autouse=True)
def _write_observations():
    yield
    if OBSERVED:
        ARTIFACT.write_text(json.dumps(OBSERVED, indent=2, sort_keys=True) + "\n")


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def test_happy_path():
    result = run("def add(a, b):\n    return a + b\n")

    assert result.status == "passed", result
    assert result.exit_code == 0
    assert result.test_results["passed"] == 3
    assert result.test_results["failed"] == 0
    assert result.duration_ms > 0
    assert result.job_name.startswith("run-") and len(result.job_name) == 12


def test_wrong_answer():
    result = run("def add(a, b):\n    return a - b\n")

    assert result.status == "failed_tests", result
    assert result.exit_code == 1
    by_name = {t["name"]: t for t in result.test_results["tests"]}
    assert set(by_name) == {"test_small", "test_negative", "test_zero"}
    assert by_name["test_small"]["passed"] is False
    assert "AssertionError" in by_name["test_small"]["error"]
    assert by_name["test_negative"]["passed"] is False
    assert by_name["test_zero"]["passed"] is True
    assert result.test_results["passed"] == 1
    assert result.test_results["failed"] == 2


def test_timeout():
    started = time.monotonic()
    result = run("def add(a, b):\n    while True:\n        pass\n")
    wall = time.monotonic() - started

    record("timeout", result, wall_seconds=round(wall, 1))
    assert result.status == "timeout", result
    assert wall < 20, f"timeout run took {wall:.1f}s"
    assert result.duration_ms > 0


def wait_terminal(client, run_ids, timeout=120.0) -> dict[str, dict]:
    deadline = time.monotonic() + timeout
    rows = {}
    while True:
        rows = {rid: client.get(f"/runs/{rid}").json() for rid in run_ids}
        if all(r["status"] in executor.TERMINAL_STATUSES for r in rows.values()):
            return rows
        assert time.monotonic() < deadline, {rid: r["status"] for rid, r in rows.items()}
        time.sleep(0.5)


def test_post_runs_end_to_end():
    with TestClient(app) as client:
        resp = client.post("/runs", json={"code": "def add(a, b):\n    return a + b\n", "tests": ADD_TESTS})
        assert resp.status_code == 202
        created = resp.json()
        assert created["status"] == "queued"
        run_row = wait_terminal(client, [created["id"]])[created["id"]]

    assert run_row["status"] == "passed", run_row
    assert run_row["job_name"].startswith("run-")
    assert run_row["test_results"]["passed"] == 3
    assert run_row["duration_ms"] > 0
    assert run_row["finished_at"] is not None


# ── Concurrency / recovery ─────────────────────────────────────────

def _unfinished_jobs(run_ids: set[str]) -> int:
    jobs = executor._batch().list_namespaced_job(executor.NAMESPACE, label_selector="app=code-runner").items
    return sum(
        1
        for j in jobs
        if (j.metadata.labels or {}).get("run-id") in run_ids and executor._job_condition(j)[0] is None
    )


def test_concurrency_twelve_runs_bounded(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_JOBS", "4")
    code = "import time\n\ndef add(a, b):\n    time.sleep(1)\n    return a + b\n"
    observed = []
    stop = threading.Event()
    run_ids: set[str] = set()

    def watch_cluster():
        while not stop.is_set():
            try:
                observed.append(_unfinished_jobs(run_ids))
            except Exception:
                pass
            stop.wait(0.3)

    started = time.monotonic()
    with TestClient(app) as client:
        watcher = threading.Thread(target=watch_cluster, daemon=True)
        watcher.start()
        for _ in range(12):
            run_ids.add(client.post("/runs", json={"code": code, "tests": ADD_TESTS}).json()["id"])
        rows = wait_terminal(client, run_ids, timeout=180)
        stats = client.get("/healthz").json()["pool"]
        stop.set()
        watcher.join()
    wall = time.monotonic() - started

    statuses = sorted(r["status"] for r in rows.values())
    OBSERVED["concurrency"] = {
        "runs": len(rows),
        "statuses": statuses,
        "max_concurrent_jobs": stats["max_concurrent"],
        "pool_peak_in_flight": stats["peak_in_flight"],
        "cluster_peak_unfinished_jobs": max(observed, default=0),
        "cluster_samples": len(observed),
        "wall_seconds": round(wall, 1),
    }
    assert len(rows) == 12
    assert statuses == ["passed"] * 12, statuses
    assert stats["max_concurrent"] == 4
    assert stats["peak_in_flight"] == 4, "pool never reached the limit, so the bound wasn't exercised"
    assert max(observed) <= 4, f"cluster had {max(observed)} unfinished jobs at once"
    assert max(observed) >= 2, "watcher never saw overlapping jobs"


def test_recovery_reattaches_job_after_restart():
    """Simulate a crash mid-run: the Job exists, the DB says running, no process is watching."""
    task = db.insert_task("raw submission", ADD_TESTS)
    cand = db.insert_candidate(task["id"], "raw", "def add(a, b):\n    return a + b\n")
    run_row = db.insert_run(cand["id"])
    job_name = executor.new_job_name()
    executor._create_resources(job_name, run_row["id"], cand["code"], ADD_TESTS)
    db.update_run(run_row["id"], status="running", job_name=job_name, started_at=executor._now())

    with TestClient(app) as client:
        assert app.state.recovery["reattached"] == 1
        final = wait_terminal(client, [run_row["id"]])[run_row["id"]]

    OBSERVED["recovery_reattach"] = {"status": final["status"], "job_name": final["job_name"], "duration_ms": final["duration_ms"]}
    assert final["status"] == "passed", final
    assert final["job_name"] == job_name
    assert final["test_results"]["passed"] == 3


# ── Adversarial ────────────────────────────────────────────────────

def test_syntax_error():
    result = run("def add(a, b)\n    return a + b\n")

    record("syntax_error", result)
    assert result.status == "error", result
    assert result.exit_code == 2
    assert "SyntaxError" in result.stderr
    assert result.test_results["tests"] == []


def test_memory_bomb():
    result = run("x = bytearray(10**9)\n\ndef add(a, b):\n    return a + b\n")

    record("memory_bomb", result)
    # 1 GB against a 128Mi limit: either the cgroup OOM-kills the container, or the
    # allocation fails in Python and the import error surfaces as exit 2.
    assert result.status in ("oom", "error"), result
    if result.status == "error":
        assert "MemoryError" in result.stderr, result
    else:
        assert result.exit_code == 137


def test_memory_growing_list():
    code = "chunks = []\nwhile True:\n    chunks.append(b'x' * 10_000_000)\n\ndef add(a, b):\n    return a + b\n"
    result = run(code)

    record("memory_growing_list", result)
    assert result.status in ("oom", "error"), result
    if result.status == "error":
        assert "MemoryError" in result.stderr, result


FORK_BOMB = """
import os

def add(a, b):
    while True:
        try:
            os.fork()
        except OSError:
            pass
"""


def test_fork_bomb_is_contained():
    started = time.monotonic()
    result = run(FORK_BOMB)
    wall = time.monotonic() - started

    # Must end within the deadline machinery, and the node must still run jobs afterwards.
    after = run("def add(a, b):\n    return a + b\n")
    record("fork_bomb", result, wall_seconds=round(wall, 1), follow_up_status=after.status)
    assert result.status in executor.TERMINAL_STATUSES - {"infra_error"}, result
    assert wall < 30, f"fork bomb run took {wall:.1f}s"
    assert after.status == "passed", after


NPROC_PROBE = """
import os, time, resource

def probe():
    n = 0
    try:
        while n < 500:
            pid = os.fork()
            if pid == 0:
                time.sleep(20)
                os._exit(0)
            n += 1
        stopped = "none"
    except OSError as exc:
        stopped = f"errno={exc.errno}"
    print(f"NPROC_PROBE forked={n} stopped_by={stopped}", flush=True)
    return n, stopped
"""


def test_nproc_limit_caps_forks():
    tests = "from solution import probe\n\ndef test_capped():\n    n, stopped = probe()\n    assert stopped == 'errno=11', stopped\n    assert n < 64, n\n"
    result = run(NPROC_PROBE, tests)
    probe_line = next((l for l in result.stdout.splitlines() if l.startswith("NPROC_PROBE")), "")

    record("nproc_limit", result, probe=probe_line)
    assert result.status == "passed", result


def test_missing_image_is_infra_error(monkeypatch):
    monkeypatch.setattr(executor, "IMAGE", "python:0.0-does-not-exist")
    started = time.monotonic()
    result = run("def add(a, b):\n    return a + b\n")

    record("missing_image", result, wall_seconds=round(time.monotonic() - started, 1))
    assert result.status == "infra_error", result
    assert any(reason in result.stderr for reason in ("ErrImagePull", "ImagePullBackOff", "before the container started"))


READONLY_TESTS = """
import errno
from solution import write

def test_etc_is_not_writable():
    try:
        write("/etc/foo")
    except OSError as exc:
        return
    raise AssertionError("wrote to /etc/foo")

def test_world_writable_dir_on_root_fs_is_read_only():
    # /var/tmp is mode 1777 in the image, so only readOnlyRootFilesystem can stop this write.
    try:
        write("/var/tmp/foo")
    except OSError as exc:
        assert exc.errno == errno.EROFS, exc
        return
    raise AssertionError("wrote to /var/tmp/foo")

def test_workspace_mount_is_read_only():
    try:
        write("/workspace/foo")
    except OSError as exc:
        assert exc.errno == errno.EROFS, exc
        return
    raise AssertionError("wrote to /workspace/foo")

def test_tmp_is_writable():
    write("/tmp/foo")
"""


def test_read_only_filesystem():
    code = (
        "import errno\n"
        "def write(path):\n"
        "    with open(path, 'w') as f:\n"
        "        f.write('x')\n"
        "    return path\n"
    )
    result = run(code, READONLY_TESTS)

    record("read_only_fs", result)
    assert result.status == "passed", result
    assert result.test_results["passed"] == 4


def test_huge_stdout_is_truncated():
    code = "def add(a, b):\n    print('x' * 10**7)\n    return a + b\n"
    result = run(code)

    record("huge_stdout", result)
    assert result.status == "passed", result
    assert result.test_results["truncated"] is True
    assert len(result.stdout.encode()) <= executor.LOG_LIMIT_BYTES
    # Result line is recovered even though the head of the log hit the limit.
    assert result.test_results["passed"] == 3


NETWORK_TESTS = """
from solution import probe

def test_outbound_blocked():
    outcome = probe()
    print("NETWORK_PROBE", outcome, flush=True)
    assert outcome.startswith("blocked"), outcome
"""

NETWORK_CODE = """
import urllib.request

def probe():
    try:
        with urllib.request.urlopen("https://example.com", timeout=3) as resp:
            return f"reachable: HTTP {resp.status}"
    except Exception as exc:
        return f"blocked: {type(exc).__name__}: {exc}"
"""


def _host_can_reach_example() -> bool:
    try:
        with urllib.request.urlopen("https://example.com", timeout=5):
            return True
    except Exception:
        return False


def test_outbound_network():
    host_online = _host_can_reach_example()
    result = run(NETWORK_CODE, NETWORK_TESTS)
    probe_line = next((l for l in result.stdout.splitlines() if l.startswith("NETWORK_PROBE")), "")
    enforced = result.status == "passed"

    record("outbound_network", result, host_online=host_online, probe=probe_line, policy_enforced=enforced)
    assert result.status in ("passed", "failed_tests"), result
    if not enforced:
        pytest.xfail(
            "NetworkPolicy not enforced: sandbox pod reached example.com "
            f"({probe_line}). Docker Desktop's CNI ignores NetworkPolicy; needs Calico/Cilium."
        )
    if not host_online:
        pytest.skip(f"inconclusive: host itself cannot reach example.com ({probe_line})")
