"""FastAPI app — routes and static file serving."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app import db, executor, llm
from app.worker import WorkerPool

STATIC_DIR = Path(__file__).parent.parent / "static"
RAW_TASK_PROMPT = "raw submission"
MAX_CANDIDATES = 10

# Task ids whose LLM generation is still in flight (in-memory; lost on restart).
GENERATING: set[str] = set()


@asynccontextmanager
async def lifespan(application: FastAPI):
    db.init_db()
    pool = WorkerPool()
    application.state.pool = pool
    application.state.recovery = await pool.recover()
    yield
    await pool.shutdown()


def pool() -> WorkerPool:
    return app.state.pool


app = FastAPI(title="Code Annotation Runner", lifespan=lifespan)


class RunRequest(BaseModel):
    code: str
    tests: str


class TaskRequest(BaseModel):
    prompt: str = Field(min_length=1)
    tests: str = Field(min_length=1)
    n_candidates: int = Field(default=3, ge=1, le=MAX_CANDIDATES)


class AnnotationRequest(BaseModel):
    candidate_id: str
    label: Literal["correct", "incorrect", "partial"]
    notes: str | None = None


class PreferenceRequest(BaseModel):
    chosen_candidate_id: str
    rejected_candidate_ids: list[str] = Field(min_length=1)


class RerunRequest(BaseModel):
    tests: str | None = Field(default=None, min_length=1)


class EditedCandidateRequest(BaseModel):
    code: str = Field(min_length=1)
    parent_candidate_id: str | None = None


def serialize_run(run: dict | None) -> dict | None:
    if run is None:
        return None
    out = dict(run)
    raw = out.pop("test_results_json", None)
    out["test_results"] = json.loads(raw) if raw else None
    return out


async def _require_task(task_id: str) -> dict:
    task = await db.async_call(db.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


async def _require_candidate_in_task(task_id: str, candidate_id: str) -> dict:
    candidate = await db.async_call(db.get_candidate, candidate_id)
    if candidate is None or candidate["task_id"] != task_id:
        raise HTTPException(status_code=400, detail=f"candidate {candidate_id} does not belong to task {task_id}")
    return candidate


async def _require_idle(task_id: str, action: str) -> list[dict]:
    """Reject changes while generation or runs are in flight, so a worker never writes into a
    deleted row or races a rerun. Returns the task's candidates."""
    if task_id in GENERATING:
        raise HTTPException(status_code=409, detail=f"cannot {action} while candidates are generating")
    candidates = await db.async_call(db.get_candidates_for_task, task_id)
    for c in candidates:
        run = await db.async_call(db.get_latest_run_for_candidate, c["id"])
        if run and run["status"] in ("queued", "running"):
            raise HTTPException(status_code=409, detail=f"cannot {action} while runs are in flight")
    return candidates


# ── Health ─────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    errors = []

    try:
        await db.async_call(db.list_tasks)
    except Exception as exc:
        errors.append(f"db: {exc}")

    try:
        from kubernetes import client

        def _check_k8s():
            executor._load_config()
            client.VersionApi().get_code()

        await db.async_call(_check_k8s)
    except Exception as exc:
        errors.append(f"k8s: {exc}")

    if errors:
        return JSONResponse({"status": "unhealthy", "errors": errors, "pool": pool().stats()}, status_code=503)
    return {"status": "ok", "pool": pool().stats()}


# ── Runs ───────────────────────────────────────────────────────────

@app.post("/runs", status_code=202)
async def create_run(req: RunRequest):
    task = await db.async_call(db.insert_task, RAW_TASK_PROMPT, req.tests)
    candidate = await db.async_call(db.insert_candidate, task["id"], "raw", req.code)
    run = await db.async_call(db.insert_run, candidate["id"])
    pool().submit(run["id"], req.code, req.tests)
    return serialize_run(run)


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = await db.async_call(db.get_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return serialize_run(run)


# ── Tasks ──────────────────────────────────────────────────────────

async def generate_and_run(task_id: str, prompt: str, tests: str, n: int) -> None:
    """Background: generate candidates, persist them with queued runs, then execute."""
    queued = []
    try:
        codes = await asyncio.to_thread(llm.generate_candidates, prompt, n)
        for code in codes:
            candidate = await db.async_call(db.insert_candidate, task_id, "llm", code)
            run = await db.async_call(db.insert_run, candidate["id"])
            queued.append((run["id"], code))
    finally:
        GENERATING.discard(task_id)
    await pool().join([pool().submit(run_id, code, tests) for run_id, code in queued])


@app.post("/tasks", status_code=202)
async def create_task(req: TaskRequest, background_tasks: BackgroundTasks):
    task = await db.async_call(db.insert_task, req.prompt, req.tests)
    GENERATING.add(task["id"])
    background_tasks.add_task(generate_and_run, task["id"], req.prompt, req.tests, req.n_candidates)
    return {"task_id": task["id"], "n_candidates": req.n_candidates, "status": "generating"}


@app.get("/tasks")
async def list_tasks(include_raw: bool = False):
    tasks = await db.async_call(db.list_tasks, None if include_raw else RAW_TASK_PROMPT)
    for t in tasks:
        t["generating"] = t["id"] in GENERATING
    return tasks


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    task = await _require_task(task_id)
    candidates = await db.async_call(db.get_candidates_for_task, task_id)
    for c in candidates:
        c["latest_run"] = serialize_run(await db.async_call(db.get_latest_run_for_candidate, c["id"]))
    return {
        "task": task,
        "generating": task_id in GENERATING,
        "candidates": candidates,
        "annotations": await db.async_call(db.get_annotations_for_task, task_id),
        "preferences": await db.async_call(db.get_preferences_for_task, task_id),
    }


@app.post("/tasks/{task_id}/annotations", status_code=201)
async def create_annotation(task_id: str, req: AnnotationRequest):
    await _require_task(task_id)
    await _require_candidate_in_task(task_id, req.candidate_id)
    return await db.async_call(db.insert_annotation, task_id, req.candidate_id, req.label, req.notes)


@app.post("/tasks/{task_id}/preference", status_code=201)
async def create_preference(task_id: str, req: PreferenceRequest):
    await _require_task(task_id)
    if req.chosen_candidate_id in req.rejected_candidate_ids:
        raise HTTPException(status_code=400, detail="chosen candidate cannot also be rejected")
    for cid in [req.chosen_candidate_id, *req.rejected_candidate_ids]:
        await _require_candidate_in_task(task_id, cid)
    rows = []
    for rejected in dict.fromkeys(req.rejected_candidate_ids):
        rows.append(await db.async_call(db.insert_preference, task_id, req.chosen_candidate_id, rejected))
    return rows


@app.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: str):
    await _require_task(task_id)
    await _require_idle(task_id, "delete")
    await db.async_call(db.delete_task, task_id)
    return Response(status_code=204)


@app.post("/tasks/{task_id}/rerun", status_code=202)
async def rerun_task(task_id: str, req: RerunRequest | None = None):
    """Queue a fresh run for every candidate, optionally replacing the hidden tests first.
    Earlier runs are kept; the UI and export use each candidate's latest run."""
    task = await _require_task(task_id)
    candidates = await _require_idle(task_id, "rerun")
    tests = task["tests"]
    if req and req.tests is not None:
        tests = req.tests
        await db.async_call(db.update_task_tests, task_id, tests)
    runs = []
    for c in candidates:
        run = await db.async_call(db.insert_run, c["id"])
        pool().submit(run["id"], c["code"], tests)
        runs.append({"candidate_id": c["id"], "run_id": run["id"]})
    return {"task_id": task_id, "tests_updated": bool(req and req.tests is not None), "runs": runs}


@app.post("/tasks/{task_id}/candidates", status_code=202)
async def create_edited_candidate(task_id: str, req: EditedCandidateRequest):
    """Run edited code as a new candidate so annotations and preferences on the original
    keep pointing at the code that was actually judged."""
    task = await _require_task(task_id)
    if req.parent_candidate_id:
        await _require_candidate_in_task(task_id, req.parent_candidate_id)
    candidate = await db.async_call(db.insert_candidate, task_id, "edited", req.code, req.parent_candidate_id)
    run = await db.async_call(db.insert_run, candidate["id"])
    pool().submit(run["id"], req.code, task["tests"])
    return {"candidate": candidate, "run": serialize_run(run)}


# ── Export ─────────────────────────────────────────────────────────

EXPORT_RUN_FIELDS = ("id", "status", "exit_code", "duration_ms", "test_results", "stdout", "stderr", "job_name")


async def _export_run(candidate_id: str) -> dict | None:
    run = serialize_run(await db.async_call(db.get_latest_run_for_candidate, candidate_id))
    return {k: run.get(k) for k in EXPORT_RUN_FIELDS} if run else None


@app.get("/export/preferences.jsonl")
async def export_preferences():
    lines = []
    for p in await db.async_call(db.get_all_preferences):
        lines.append(json.dumps({
            "task_id": p["task_id"],
            "prompt": p["prompt"],
            "chosen": {"candidate_id": p["chosen_candidate_id"], "code": p["chosen_code"],
                       "run": await _export_run(p["chosen_candidate_id"])},
            "rejected": {"candidate_id": p["rejected_candidate_id"], "code": p["rejected_code"],
                         "run": await _export_run(p["rejected_candidate_id"])},
            "identical_code": p["chosen_code"] == p["rejected_code"],
            "preference_id": p["id"],
            "created_at": p["created_at"],
        }))
    body = "".join(line + "\n" for line in lines)
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="preferences.jsonl"'},
    )


# ── Static UI ──────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
