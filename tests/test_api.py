"""API tests using FastAPI TestClient. The LLM and executor are mocked; no cluster needed."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db, executor, llm
from app.main import app
from app.worker import WorkerPool

FIZZ_TESTS = "from solution import fizzbuzz\ndef test_three():\n    assert fizzbuzz(3)[-1] == 'Fizz'\n"


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


@pytest.fixture
def fake_pipeline(monkeypatch):
    """Mock LLM returns distinct candidates; mock executor marks every run passed."""
    calls = {"llm": [], "executed": []}

    def fake_generate(prompt, n):
        calls["llm"].append((prompt, n))
        return [f"def fizzbuzz(n):\n    return {i}\n" for i in range(n)]

    async def fake_execute(run_id, code, tests):
        calls["executed"].append(run_id)
        return db.update_run(
            run_id,
            status="passed",
            exit_code=0,
            test_results_json=json.dumps({"tests": [], "passed": 1, "failed": 0, "truncated": False}),
            duration_ms=5,
        )

    monkeypatch.setattr(llm, "generate_candidates", fake_generate)
    monkeypatch.setattr(executor, "execute_run", fake_execute)
    return calls


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def create_task(client, n=3) -> str:
    resp = client.post("/tasks", json={"prompt": "write fizzbuzz(n)", "tests": FIZZ_TESTS, "n_candidates": n})
    assert resp.status_code == 202, resp.text
    return resp.json()["task_id"]


# ── Health / static ────────────────────────────────────────────────

def test_healthz_db_ok(client):
    """DB must be healthy; k8s may be unreachable in some environments."""
    data = client.get("/healthz").json()
    assert not any(e.startswith("db:") for e in data.get("errors", []))


def test_index_returns_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Code Annotation Runner" in resp.text


# ── Tasks ──────────────────────────────────────────────────────────

def test_post_tasks_generates_and_runs_candidates(client, fake_pipeline):
    task_id = create_task(client, n=3)

    assert fake_pipeline["llm"] == [("write fizzbuzz(n)", 3)]
    detail = client.get(f"/tasks/{task_id}").json()
    assert detail["task"]["prompt"] == "write fizzbuzz(n)"
    assert detail["generating"] is False
    assert len(detail["candidates"]) == 3
    assert len({c["code"] for c in detail["candidates"]}) == 3
    for c in detail["candidates"]:
        assert c["source"] == "llm"
        assert c["latest_run"]["status"] == "passed"
        assert c["latest_run"]["test_results"]["passed"] == 1
    assert sorted(fake_pipeline["executed"]) == sorted(c["latest_run"]["id"] for c in detail["candidates"])


def test_post_tasks_validates_n_candidates(client, fake_pipeline):
    resp = client.post("/tasks", json={"prompt": "p", "tests": FIZZ_TESTS, "n_candidates": 0})
    assert resp.status_code == 422
    assert fake_pipeline["llm"] == []


def test_list_tasks_summary_counts_and_hides_raw(client, fake_pipeline):
    task_id = create_task(client, n=2)
    client.post("/runs", json={"code": "x = 1", "tests": "def test_a():\n    pass\n"})

    tasks = client.get("/tasks").json()
    assert [t["id"] for t in tasks] == [task_id]
    summary = tasks[0]
    assert summary["candidate_count"] == 2
    assert summary["passed_count"] == 2
    assert summary["failed_count"] == 0
    assert summary["pending_count"] == 0
    assert summary["generating"] is False

    assert len(client.get("/tasks?include_raw=true").json()) == 2


def test_get_unknown_task_404(client):
    assert client.get("/tasks/nope").status_code == 404


def test_llm_failure_still_creates_candidates(client, monkeypatch):
    """A broken LLM must not fail the task: candidates hold the error and runs still happen."""
    executed = []

    async def fake_execute(run_id, code, tests):
        executed.append(code)
        return db.update_run(run_id, status="error", exit_code=2)

    monkeypatch.delenv("AQ_API_KEY", raising=False)
    monkeypatch.setattr(executor, "execute_run", fake_execute)

    task_id = create_task(client, n=2)
    detail = client.get(f"/tasks/{task_id}").json()
    assert len(detail["candidates"]) == 2
    assert all(c["code"].startswith("# LLM generation failed: AQ_API_KEY is not set") for c in detail["candidates"])
    assert len(executed) == 2


# ── Annotations / preferences ──────────────────────────────────────

def test_annotation_roundtrip_and_validation(client, fake_pipeline):
    task_id = create_task(client, n=2)
    other_task = create_task(client, n=1)
    cand = client.get(f"/tasks/{task_id}").json()["candidates"][0]["id"]
    foreign = client.get(f"/tasks/{other_task}").json()["candidates"][0]["id"]

    resp = client.post(f"/tasks/{task_id}/annotations", json={"candidate_id": cand, "label": "correct", "notes": "clean"})
    assert resp.status_code == 201
    assert resp.json()["label"] == "correct"

    annotations = client.get(f"/tasks/{task_id}").json()["annotations"]
    assert [(a["candidate_id"], a["label"], a["notes"]) for a in annotations] == [(cand, "correct", "clean")]

    assert client.post(f"/tasks/{task_id}/annotations", json={"candidate_id": cand, "label": "great"}).status_code == 422
    assert client.post(f"/tasks/{task_id}/annotations", json={"candidate_id": foreign, "label": "correct"}).status_code == 400
    assert client.post("/tasks/nope/annotations", json={"candidate_id": cand, "label": "correct"}).status_code == 404


def test_preference_one_row_per_rejected(client, fake_pipeline):
    task_id = create_task(client, n=3)
    a, b, c = (x["id"] for x in client.get(f"/tasks/{task_id}").json()["candidates"])

    resp = client.post(f"/tasks/{task_id}/preference", json={"chosen_candidate_id": a, "rejected_candidate_ids": [b, c]})
    assert resp.status_code == 201
    rows = resp.json()
    assert [(r["chosen_candidate_id"], r["rejected_candidate_id"]) for r in rows] == [(a, b), (a, c)]

    bad = client.post(f"/tasks/{task_id}/preference", json={"chosen_candidate_id": a, "rejected_candidate_ids": [a]})
    assert bad.status_code == 400


# ── Worker pool / recovery ─────────────────────────────────────────

def test_pool_never_exceeds_max_concurrent(monkeypatch):
    live = {"now": 0, "peak": 0, "done": []}

    async def fake_execute(run_id, code, tests):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.05)
        live["now"] -= 1
        live["done"].append(run_id)

    monkeypatch.setattr(executor, "execute_run", fake_execute)

    async def scenario():
        pool = WorkerPool(max_concurrent=3)
        tasks = [pool.submit(f"r{i}", "code", "tests") for i in range(10)]
        await asyncio.sleep(0.01)
        assert pool.stats()["in_flight"] == 3
        assert pool.stats()["waiting"] == 7
        await pool.join(tasks)
        return pool

    pool = asyncio.run(scenario())
    assert live["peak"] == 3
    assert pool.peak_in_flight == 3
    assert sorted(live["done"]) == sorted(f"r{i}" for i in range(10))
    assert pool.stats()["in_flight"] == 0


def test_max_concurrent_jobs_from_env(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_JOBS", "7")
    assert WorkerPool().max_concurrent == 7
    monkeypatch.setenv("MAX_CONCURRENT_JOBS", "nonsense")
    assert WorkerPool().max_concurrent == 4


def _seed_run(status: str, job_name: str | None = None) -> dict:
    task = db.insert_task("p", "def test_a():\n    pass\n")
    cand = db.insert_candidate(task["id"], "raw", "x = 1\n")
    run = db.insert_run(cand["id"])
    return db.update_run(run["id"], status=status, job_name=job_name)


def test_recovery_requeues_queued_and_marks_stale_running(monkeypatch):
    queued = _seed_run("queued")
    stale = _seed_run("running", job_name="run-gone0001")
    executed = []

    async def fake_execute(run_id, code, tests):
        executed.append((run_id, code, tests))
        db.update_run(run_id, status="passed")

    async def fake_exists(job_name):
        return False

    monkeypatch.setattr(executor, "execute_run", fake_execute)
    monkeypatch.setattr(executor, "job_exists", fake_exists)

    with TestClient(app) as client:
        assert app.state.recovery == {"requeued": 1, "reattached": 0, "stale": 1}
        deadline = time.monotonic() + 5
        while client.get(f"/runs/{queued['id']}").json()["status"] != "passed":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        stale_row = client.get(f"/runs/{stale['id']}").json()

    assert executed == [(queued["id"], "x = 1\n", "def test_a():\n    pass\n")]
    assert stale_row["status"] == "infra_error"
    assert "run-gone0001 no longer exists" in stale_row["stderr"]
    assert stale_row["finished_at"] is not None


def test_recovery_reattaches_running_with_live_job(monkeypatch):
    live = _seed_run("running", job_name="run-live0001")
    attached = []

    async def fake_exists(job_name):
        return True

    async def fake_attach(run_id, job_name, started_at=None):
        attached.append((run_id, job_name))
        db.update_run(run_id, status="passed")

    monkeypatch.setattr(executor, "job_exists", fake_exists)
    monkeypatch.setattr(executor, "attach_run", fake_attach)

    with TestClient(app) as client:
        assert app.state.recovery == {"requeued": 0, "reattached": 1, "stale": 0}
        deadline = time.monotonic() + 5
        while client.get(f"/runs/{live['id']}").json()["status"] != "passed":
            assert time.monotonic() < deadline
            time.sleep(0.05)

    assert attached == [(live["id"], "run-live0001")]


# ── LLM helpers ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,expected",
    [
        ("```python\ndef f():\n    return 1\n```", "def f():\n    return 1\n"),
        ("Sure! Here you go:\n```py\nx = 1\n```\nHope that helps.", "x = 1\n"),
        ("```\ny = 2\n```", "y = 2\n"),
        ("def g():\n    pass", "def g():\n    pass\n"),
        ("```python\ndef cut_off():\n    return", "def cut_off():\n    return\n"),
    ],
)
def test_strip_fences(text, expected):
    assert llm.strip_fences(text) == expected


def test_generate_candidates_records_api_errors(monkeypatch):
    class Boom:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("503 upstream unavailable")

    monkeypatch.setenv("AQ_API_KEY", "test-key")
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: Boom)
    out = llm.generate_candidates("write f", 2)
    assert out == ["# LLM generation failed: RuntimeError: 503 upstream unavailable\n"] * 2


def test_generate_candidates_uses_env_config_and_temperature(monkeypatch):
    seen = {}

    def create(**kwargs):
        seen.setdefault("calls", []).append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="```python\nx = 1\n```"))])

    def fake_openai(**kwargs):
        seen["client"] = kwargs
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setenv("AQ_API_KEY", "test-key")
    monkeypatch.setenv("AQ_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "some/model")
    monkeypatch.setattr("openai.OpenAI", fake_openai)

    assert llm.generate_candidates("write f", 3) == ["x = 1\n"] * 3
    assert seen["client"]["api_key"] == "test-key"
    assert seen["client"]["base_url"] == "https://example.invalid/v1"
    assert len(seen["calls"]) == 3
    assert all(c["model"] == "some/model" and c["temperature"] == 0.8 for c in seen["calls"])
    assert seen["calls"][0]["messages"][0] == {"role": "system", "content": llm.SYSTEM_PROMPT}
