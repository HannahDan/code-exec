# NOTES — Code Annotation Runner

## M1 Decisions

- **IDs**: `uuid4().hex[:16]` — 16 hex chars, collision-safe for this scale.
- **DB**: stdlib `sqlite3` with WAL mode, single connection per call, `asyncio.to_thread` for async compat.
- **Timestamps**: ISO 8601 UTC via `datetime.now(timezone.utc).isoformat()`.
- **K8s healthcheck**: uses `kubernetes.client.VersionApi().get_code()` to verify cluster reachability.
- **Static files**: served via `FileResponse` for `GET /`, no framework build step.

## M2 Decisions

- **Runner delivery**: `app/runner_template.py` is a plain stdlib script; `executor.py` reads the file
  verbatim into the ConfigMap as `runner.py`. What's on disk is exactly what runs in the pod.
- **Naming**: Job and ConfigMap share the name `run-<8 hex>`. The DB run id (16 hex) is the `run-id`
  label, used to find the pod by label selector.
- **ConfigMap GC**: after the Job is created, the ConfigMap is patched with an `ownerReference` to it
  (verified on the cluster: `Job/run-...`). `blockOwnerDeletion: false` so no extra RBAC is needed.
- **stdout vs stderr**: Kubernetes merges a container's stdout and stderr into one log stream, so they
  can't be separated after the fact. `runs.stdout` holds the pod log (minus the `__RESULT__` line);
  `runs.stderr` holds diagnostics: runner collection errors, timeout/OOM reasons, infra messages.
- **Truncation flag**: the runs table has no `truncated` column, so it lives inside
  `test_results_json` as `"truncated": bool`. When no `__RESULT__` line is found the object also has
  `"result_missing": true`. If the log head is truncated, a second `tail_lines=1` read recovers the
  result line.
- **Timeout classification**: `DeadlineExceeded` → `timeout`, unless the container never started and the
  pod was stuck pulling the image, in which case it's `infra_error`.
- **Wait cap**: besides the 10s `activeDeadlineSeconds`, the executor gives up after 60s
  (`EXECUTOR_WAIT_CAP_SECONDS`) and records `infra_error`, so a stuck scheduler can't hang a run.
- **duration_ms**: wall-clock from Job creation to terminal state, as seen by the executor. It includes
  pod scheduling and container start (~3-4s on Docker Desktop), not just test execution time.
- **Added `GET /runs/{id}`**: not in the spec's API list, but `POST /runs` returns immediately with
  `queued`, so callers need a way to poll.
- **`POST /runs` returns 202** with the queued run; execution happens in a FastAPI background task
  (replaced by the bounded worker pool in M5).

### Issues hit

- `V1OwnerReference.to_dict()` emits snake_case keys (`api_version`), which the API server rejects with
  422. The patch body uses camelCase keys directly.
- With `kubernetes==36.0.3`, `read_namespaced_pod_log` returned the Python repr of a bytes object
  (`"b'...\\n'"`), so the `__RESULT__` line was never matched. Logs are read with
  `_preload_content=False` and decoded manually.
- `.gitignore` originally excluded `artifacts/*.txt|json|jsonl`, which contradicts the spec (artifacts
  are committed evidence). Removed.
- `make test` piped through `tee`, which masked pytest failures. The Makefile now uses `pipefail`.
