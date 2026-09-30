"""FastAPI app — routes and static file serving."""

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app import db, executor

STATIC_DIR = Path(__file__).parent.parent / "static"
RAW_TASK_PROMPT = "raw submission"


@asynccontextmanager
async def lifespan(application: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Code Annotation Runner", lifespan=lifespan)


class RunRequest(BaseModel):
    code: str
    tests: str


def serialize_run(run: dict | None) -> dict | None:
    if run is None:
        return None
    out = dict(run)
    raw = out.pop("test_results_json", None)
    out["test_results"] = json.loads(raw) if raw else None
    return out


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


# ── Static UI ──────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
