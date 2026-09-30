"""FastAPI app — routes and static file serving."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app import db, executor, llm

STATIC_DIR = Path(__file__).parent.parent / "static"
RAW_TASK_PROMPT = "raw submission"
MAX_CANDIDATES = 10

# Task ids whose LLM generation is still in flight (in-memory; lost on restart).
GENERATING: set[str] = set()


@asynccontextmanager
async def lifespan(application: FastAPI):
    db.init_db()
    yield


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
        return JSONResponse({"status": "unhealthy", "errors": errors}, status_code=503)
    return {"status": "ok"}


# ── Runs ───────────────────────────────────────────────────────────

@app.post("/runs", status_code=202)
async def create_run(req: RunRequest, background_tasks: BackgroundTasks):
    task = await db.async_call(db.insert_task, RAW_TASK_PROMPT, req.tests)
    candidate = await db.async_call(db.insert_candidate, task["id"], "raw", req.code)
    run = await db.async_call(db.insert_run, candidate["id"])
    background_tasks.add_task(executor.execute_run, run["id"], req.code, req.tests)
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
    await asyncio.gather(*(executor.execute_run(run_id, code, tests) for run_id, code in queued))


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


# ── Static UI ──────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
