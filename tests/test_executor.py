"""Executor tests. These create real Jobs in the local cluster's `sandbox` namespace."""

import asyncio
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db, executor
from app.main import app

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

    assert result.status == "timeout", result
    assert wall < 20, f"timeout run took {wall:.1f}s"
    assert result.duration_ms > 0


def test_post_runs_end_to_end():
    with TestClient(app) as client:
        resp = client.post("/runs", json={"code": "def add(a, b):\n    return a + b\n", "tests": ADD_TESTS})
        assert resp.status_code == 202
        created = resp.json()
        assert created["status"] == "queued"

        # TestClient runs background tasks before returning, so the run is terminal here.
        run_row = client.get(f"/runs/{created['id']}").json()

    assert run_row["status"] == "passed", run_row
    assert run_row["job_name"].startswith("run-")
    assert run_row["test_results"]["passed"] == 3
    assert run_row["duration_ms"] > 0
    assert run_row["finished_at"] is not None
