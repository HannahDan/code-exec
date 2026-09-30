"""FastAPI app — routes and static file serving."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import db


@asynccontextmanager
async def lifespan(application: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Code Annotation Runner", lifespan=lifespan)


# ── Health ─────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    errors = []

    # Check DB
    try:
        await db.async_call(db.list_tasks)
    except Exception as exc:
        errors.append(f"db: {exc}")

    # Check K8s API
    try:
        from kubernetes import client, config

        def _check_k8s():
            config.load_kube_config()
            v1 = client.VersionApi()
            v1.get_code()

        await db.async_call(_check_k8s)
    except Exception as exc:
        errors.append(f"k8s: {exc}")

    if errors:
        return JSONResponse({"status": "unhealthy", "errors": errors}, status_code=503)
    return {"status": "ok"}


# ── Static UI ──────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse("static/index.html")
