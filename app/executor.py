"""Kubernetes Job lifecycle: create, wait, collect, classify.

The sync kubernetes client is called through asyncio.to_thread. Nothing in
this module raises to the API layer: every failure becomes a RunResult with
a status.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from app import db

NAMESPACE = os.getenv("SANDBOX_NAMESPACE", "sandbox")
IMAGE = os.getenv("SANDBOX_IMAGE", "python:3.12-slim")
ACTIVE_DEADLINE_SECONDS = 10
TTL_SECONDS_AFTER_FINISHED = 300
POLL_INTERVAL_SECONDS = 0.5
# Upper bound on the whole lifecycle (scheduling + image start + deadline).
WAIT_CAP_SECONDS = float(os.getenv("EXECUTOR_WAIT_CAP_SECONDS", "60"))
LOG_LIMIT_BYTES = 65536
RESULT_PREFIX = "__RESULT__ "
CONTAINER_NAME = "runner"

RUNNER_SOURCE = (Path(__file__).parent / "runner_template.py").read_text()

TERMINAL_STATUSES = {"passed", "failed_tests", "error", "timeout", "oom", "infra_error"}
IMAGE_PULL_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName"}
STUCK_WAITING_REASONS = IMAGE_PULL_REASONS | {"CreateContainerConfigError", "CreateContainerError"}


@dataclass
class RunResult:
    status: str
    job_name: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    test_results: dict = field(default_factory=dict)
    duration_ms: int = 0


@lru_cache(maxsize=1)
def _load_config() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def _core() -> client.CoreV1Api:
    _load_config()
    return client.CoreV1Api()


def _batch() -> client.BatchV1Api:
    _load_config()
    return client.BatchV1Api()


def new_job_name() -> str:
    return f"run-{uuid4().hex[:8]}"


# ── Manifests ──────────────────────────────────────────────────────

def build_configmap(job_name: str, run_id: str, code: str, tests: str) -> client.V1ConfigMap:
    return client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=job_name,
            labels={"app": "code-runner", "run-id": run_id},
        ),
        data={"solution.py": code, "tests.py": tests, "runner.py": RUNNER_SOURCE},
    )


def build_job(job_name: str, run_id: str) -> client.V1Job:
    labels = {"app": "code-runner", "run-id": run_id}
    container = client.V1Container(
        name=CONTAINER_NAME,
        image=IMAGE,
        image_pull_policy="IfNotPresent",
        command=["python", "-u", "/workspace/runner.py"],
        working_dir="/workspace",
        env=[client.V1EnvVar(name="PYTHONDONTWRITEBYTECODE", value="1")],
        resources=client.V1ResourceRequirements(
            requests={"cpu": "500m", "memory": "128Mi"},
            limits={"cpu": "500m", "memory": "128Mi"},
        ),
        security_context=client.V1SecurityContext(
            run_as_non_root=True,
            run_as_user=65534,
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=client.V1Capabilities(drop=["ALL"]),
            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
        ),
        volume_mounts=[
            client.V1VolumeMount(name="workspace", mount_path="/workspace", read_only=True),
            client.V1VolumeMount(name="tmp", mount_path="/tmp"),
        ],
    )
    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        automount_service_account_token=False,
        enable_service_links=False,
        containers=[container],
        volumes=[
            client.V1Volume(
                name="workspace",
                config_map=client.V1ConfigMapVolumeSource(name=job_name),
            ),
            client.V1Volume(
                name="tmp",
                empty_dir=client.V1EmptyDirVolumeSource(size_limit="16Mi"),
            ),
        ],
    )
    return client.V1Job(
        metadata=client.V1ObjectMeta(name=job_name, labels=labels),
        spec=client.V1JobSpec(
            backoff_limit=0,
            active_deadline_seconds=ACTIVE_DEADLINE_SECONDS,
            ttl_seconds_after_finished=TTL_SECONDS_AFTER_FINISHED,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=pod_spec,
            ),
        ),
    )


# ── Sync K8s operations (run via asyncio.to_thread) ────────────────

def _create_resources(job_name: str, run_id: str, code: str, tests: str) -> None:
    core, batch = _core(), _batch()
    core.create_namespaced_config_map(NAMESPACE, build_configmap(job_name, run_id, code, tests))
    try:
        job = batch.create_namespaced_job(NAMESPACE, build_job(job_name, run_id))
    except Exception:
        _delete_configmap(job_name)
        raise
    owner = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "name": job.metadata.name,
        "uid": job.metadata.uid,
        "blockOwnerDeletion": False,
    }
    core.patch_namespaced_config_map(job_name, NAMESPACE, {"metadata": {"ownerReferences": [owner]}})


def _delete_configmap(name: str) -> None:
    try:
        _core().delete_namespaced_config_map(name, NAMESPACE)
    except Exception:
        pass


def _delete_job(job_name: str) -> None:
    try:
        _batch().delete_namespaced_job(job_name, NAMESPACE, propagation_policy="Background")
    except Exception:
        pass


def _read_job(job_name: str) -> client.V1Job:
    return _batch().read_namespaced_job_status(job_name, NAMESPACE)


def _find_pod(run_id: str) -> client.V1Pod | None:
    pods = _core().list_namespaced_pod(NAMESPACE, label_selector=f"run-id={run_id}").items
    if not pods:
        return None
    return max(pods, key=lambda p: p.metadata.creation_timestamp or datetime.min.replace(tzinfo=timezone.utc))


def _fetch_log_bytes(pod_name: str, **kwargs) -> bytes:
    # _preload_content=False: some client versions return str(bytes) instead of decoded text.
    resp = _core().read_namespaced_pod_log(
        pod_name, NAMESPACE, container=CONTAINER_NAME, _preload_content=False, **kwargs
    )
    try:
        return resp.data
    finally:
        resp.release_conn()


def _read_logs(pod_name: str) -> tuple[str, bool]:
    """Return (log text, truncated). Recovers the result line if the head was cut off."""
    data = _fetch_log_bytes(pod_name, limit_bytes=LOG_LIMIT_BYTES)
    truncated = len(data) >= LOG_LIMIT_BYTES
    text = data.decode("utf-8", errors="replace")
    if truncated and RESULT_PREFIX not in text:
        try:
            tail = _fetch_log_bytes(pod_name, tail_lines=1, limit_bytes=LOG_LIMIT_BYTES)
            tail_text = tail.decode("utf-8", errors="replace")
            if tail_text.startswith(RESULT_PREFIX):
                text = text + "\n" + tail_text
        except Exception:
            pass
    return text, truncated


# ── Classification helpers ─────────────────────────────────────────

def _job_condition(job: client.V1Job) -> tuple[str | None, str | None]:
    """Return (type, reason) of the terminal condition, if any."""
    for cond in job.status.conditions or []:
        if cond.status == "True" and cond.type in ("Complete", "Failed"):
            return cond.type, cond.reason
    return None, None


def _container_status(pod: client.V1Pod | None) -> client.V1ContainerStatus | None:
    if pod is None or not pod.status or not pod.status.container_statuses:
        return None
    for cs in pod.status.container_statuses:
        if cs.name == CONTAINER_NAME:
            return cs
    return None


def parse_logs(raw: str) -> tuple[str, dict | None]:
    """Split pod logs into (output without result line, parsed result)."""
    result = None
    kept = []
    for line in raw.splitlines():
        if line.startswith(RESULT_PREFIX):
            try:
                result = json.loads(line[len(RESULT_PREFIX):])
                continue
            except json.JSONDecodeError:
                pass
        kept.append(line)
    return "\n".join(kept), result


def classify_exit(exit_code: int | None, reason: str | None) -> str:
    if reason == "OOMKilled":
        return "oom"
    if exit_code == 0:
        return "passed"
    if exit_code == 1:
        return "failed_tests"
    return "error"


# ── Lifecycle ──────────────────────────────────────────────────────

async def run_job(code: str, tests: str, run_id: str, job_name: str | None = None) -> RunResult:
    """Create a sandbox Job, wait for it, and classify the outcome. Never raises."""
    job_name = job_name or new_job_name()
    start = time.monotonic()
    result = RunResult(status="infra_error", job_name=job_name)

    try:
        await asyncio.to_thread(_create_resources, job_name, run_id, code, tests)
    except Exception as exc:
        result.stderr = f"failed to create job: {_describe(exc)}"
        result.duration_ms = _elapsed_ms(start)
        return result

    try:
        await _wait_and_collect(result, run_id, start)
    except Exception as exc:
        result.status = "infra_error"
        result.stderr = (result.stderr + "\n" if result.stderr else "") + f"executor error: {_describe(exc)}"
        await asyncio.to_thread(_delete_job, job_name)

    result.duration_ms = _elapsed_ms(start)
    return result


async def _wait_and_collect(result: RunResult, run_id: str, start: float) -> None:
    job_name = result.job_name
    last_waiting_reason = None
    saw_container_start = False
    pod = None

    while True:
        job = await asyncio.to_thread(_read_job, job_name)
        cond_type, cond_reason = _job_condition(job)

        pod = await asyncio.to_thread(_find_pod, run_id)
        cs = _container_status(pod)
        if cs and cs.state:
            if cs.state.running or cs.state.terminated:
                saw_container_start = True
            elif cs.state.waiting:
                last_waiting_reason = cs.state.waiting.reason

        if cond_type is not None:
            break

        if last_waiting_reason in STUCK_WAITING_REASONS and not saw_container_start:
            result.status = "infra_error"
            message = cs.state.waiting.message if cs and cs.state and cs.state.waiting else ""
            result.stderr = f"pod stuck in {last_waiting_reason}: {message}".strip()
            await asyncio.to_thread(_delete_job, job_name)
            return

        if time.monotonic() - start > WAIT_CAP_SECONDS:
            result.status = "infra_error"
            result.stderr = f"job did not finish within {WAIT_CAP_SECONDS:.0f}s wait cap"
            await asyncio.to_thread(_delete_job, job_name)
            return

        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    raw_logs, truncated = "", False
    if pod is not None:
        try:
            raw_logs, truncated = await asyncio.to_thread(_read_logs, pod.metadata.name)
        except Exception as exc:
            result.stderr = f"could not read logs: {_describe(exc)}"

    output, parsed = parse_logs(raw_logs)
    result.stdout = output
    result.test_results = _test_results(parsed, truncated)

    cs = _container_status(pod)
    terminated = cs.state.terminated if cs and cs.state else None
    if terminated is None and cs and cs.last_state:
        terminated = cs.last_state.terminated

    if cond_type == "Failed" and cond_reason == "DeadlineExceeded":
        # The deadline counts from Job start, so a pod that never ran (unschedulable,
        # image pull) hits it too; that's the cluster's failure, not the code's.
        if not saw_container_start:
            result.status = "infra_error"
            where = last_waiting_reason or (pod.status.phase if pod and pod.status else "no pod")
            _append_stderr(result, f"deadline exceeded before the container started ({where})")
        else:
            result.status = "timeout"
            _append_stderr(result, f"killed after activeDeadlineSeconds={ACTIVE_DEADLINE_SECONDS}")
        if terminated is not None:
            result.exit_code = terminated.exit_code
        return

    if terminated is None:
        result.status = "infra_error"
        _append_stderr(result, f"job {cond_type} ({cond_reason}) but no container termination state found")
        return

    result.exit_code = terminated.exit_code
    result.status = classify_exit(terminated.exit_code, terminated.reason)
    if result.status == "oom":
        _append_stderr(result, f"container OOMKilled (exit {terminated.exit_code}), memory limit 128Mi")
    if parsed and parsed.get("error"):
        _append_stderr(result, parsed["error"])


def _test_results(parsed: dict | None, truncated: bool) -> dict:
    if parsed is None:
        return {"tests": [], "passed": 0, "failed": 0, "truncated": truncated, "result_missing": True}
    return {
        "tests": parsed.get("tests", []),
        "passed": parsed.get("passed", 0),
        "failed": parsed.get("failed", 0),
        "truncated": truncated,
    }


def _append_stderr(result: RunResult, message: str) -> None:
    result.stderr = f"{result.stderr}\n{message}" if result.stderr else message


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _describe(exc: Exception) -> str:
    if isinstance(exc, ApiException):
        return f"{exc.status} {exc.reason}: {(exc.body or '')[:300]}"
    return f"{type(exc).__name__}: {exc}"


# ── Persisted execution ────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def execute_run(run_id: str, code: str, tests: str) -> dict | None:
    """Run a queued DB run through the sandbox and store the outcome. Never raises."""
    job_name = new_job_name()
    try:
        await db.async_call(
            db.update_run, run_id, status="running", job_name=job_name, started_at=_now()
        )
        result = await run_job(code, tests, run_id, job_name=job_name)
        return await db.async_call(db.update_run, run_id, **_result_columns(result))
    except Exception as exc:
        return await _store_failure(run_id, f"executor error: {_describe(exc)}")


def _job_exists(job_name: str) -> bool:
    try:
        _batch().read_namespaced_job(job_name, NAMESPACE)
        return True
    except ApiException as exc:
        if exc.status == 404:
            return False
        raise


async def job_exists(job_name: str | None) -> bool:
    """Whether the run's Job is still in the cluster. Raises if the API can't tell."""
    if not job_name:
        return False
    return await asyncio.to_thread(_job_exists, job_name)


async def attach_run(run_id: str, job_name: str, started_at: str | None = None) -> dict | None:
    """Resume watching a Job created before a restart, and store its outcome. Never raises."""
    start = time.monotonic()
    result = RunResult(status="infra_error", job_name=job_name)
    try:
        await _wait_and_collect(result, run_id, start)
    except Exception as exc:
        result.status = "infra_error"
        _append_stderr(result, f"executor error after restart: {_describe(exc)}")
    result.duration_ms = _ms_since(started_at) if started_at else _elapsed_ms(start)
    try:
        return await db.async_call(db.update_run, run_id, **_result_columns(result))
    except Exception as exc:
        return await _store_failure(run_id, f"could not store result: {_describe(exc)}")


async def _store_failure(run_id: str, message: str) -> dict | None:
    try:
        return await db.async_call(
            db.update_run, run_id, status="infra_error", stderr=message, finished_at=_now()
        )
    except Exception:
        return None


def _ms_since(iso: str) -> int:
    try:
        return max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() * 1000))
    except ValueError:
        return 0


def _result_columns(result: RunResult) -> dict:
    return {
        "status": result.status,
        "job_name": result.job_name,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "test_results_json": json.dumps(result.test_results),
        "duration_ms": result.duration_ms,
        "finished_at": _now(),
    }
