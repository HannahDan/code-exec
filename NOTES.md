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

- **ConfigMap ownerReference patch → 422.** `V1OwnerReference.to_dict()` returns the model's Python
  attribute names (`api_version`, `block_owner_deletion`, plus `controller: None`), not the wire format.
  The API server saw no `apiVersion` and rejected the patch. The wire format comes from
  `ApiClient().sanitize_for_serialization()`; the patch body now uses those camelCase keys directly.
- **`__RESULT__` never parsed.** In `kubernetes==36.0.3`, `ApiClient.__call_api` skips decoding when the
  response type is `"str"` (pod logs are), then `__deserialize_primitive` calls `str(bytes)`, which in
  Python 3 yields the repr `"b'...\\n'"`. Reproduced on a live pod: the default read returns
  `"b'__RESULT__ ..."`. Logs are read with `_preload_content=False` and decoded as UTF-8 manually.
- **Artifacts ignored by git.** `.gitignore` excluded `artifacts/*.txt|json|jsonl`, which contradicts the
  spec (artifacts are committed evidence). Removed; `git check-ignore` confirms they're tracked now.
- **`make test` hid failures.** `pytest | tee` returns tee's exit code. The first fix used
  `.SHELLFLAGS := -o pipefail -c`, but macOS ships GNU Make 3.81 and `.SHELLFLAGS` only exists from 3.82,
  so it was silently ignored (`make test` still exited 0 on a forced failure). The recipe now starts with
  `set -o pipefail;`, which works on any make; a forced failure exits 2.

## M3 Decisions

- **LLM calls**: `generate_candidates(prompt, n)` makes `n` separate chat-completion calls (temperature 0.8)
  in a small thread pool, rather than one call with the `n` parameter, which not every
  OpenAI-compatible provider supports. Client: `OpenAI(api_key=AQ_API_KEY, base_url=AQ_BASE_URL)`,
  60s timeout, 2 retries. Config is read from env at call time, never from files in the app.
- **Fence stripping**: takes the longest fenced block (```` ```python ````, ```` ```py ````, or bare
  ```` ``` ````). An unterminated opening fence (response cut off) is stripped too. No fence at all: the
  raw text is stored as-is.
- **LLM failures**: a failed call (or missing `AQ_API_KEY`) becomes a candidate whose code is
  `# LLM generation failed: <error>`. It still runs, fails with `error` (exit 2, `ImportError`), and shows
  up in the UI as data instead of failing the task.
- **`POST /tasks` flow**: returns 202 with `task_id` immediately; a background task generates candidates,
  persists each with a `queued` run, then executes them. Runs are unbounded here; M5 adds the semaphore.
- **"Generating" state**: the schema has no `n_candidates` column, so while generation is in flight the
  task id sits in an in-memory `GENERATING` set, exposed as `generating: true` on task responses. It's
  lost on restart; M5's recovery only covers runs, so a crash mid-generation leaves a task with fewer
  candidates than requested.
- **Summary counts** use each candidate's latest run (by `rowid`, since `started_at` is overwritten when a
  run starts). `failed_count` includes `infra_error`.
- **Raw tasks hidden by default**: `POST /runs` creates a task with prompt `raw submission`. `GET /tasks`
  hides those unless `?include_raw=true`, so the annotator list isn't flooded by test/concurrency runs.
- **Preference endpoint pulled forward from M6**: the M3 demo posts a preference, so
  `POST /tasks/{id}/preference` exists now (validates membership, rejects chosen-in-rejected, dedupes).
  The JSONL export is still M6; the demo skips it until then.
- **Collection-error tracebacks keep the tail**: the runner truncates import/collection tracebacks from
  the end, so the exception line (e.g. `ImportError: cannot import name 'fizzbuzz'`) survives the
  500-char limit. Per-test errors are short `Type: message` strings and keep the head.
- **Demo isolation**: `demo.sh` uses a fresh `demo.db` (via the new `CODE_EXEC_DB` env var) on port 8765,
  so committed artifacts don't include rows from earlier runs, and it doesn't clash with `make run` on 8000.
- **`.env` loading**: `demo.sh` and `make run` source `.env` if present. The app itself only reads env vars.

### Demo status

- `scripts/demo.sh` runs end to end against Docker Desktop with a real `AQ_API_KEY`
  (`artifacts/demo_output.txt`). The raw run passes (2/2 tests). All three `openai/gpt-4.1-mini` fizzbuzz
  candidates pass 3/3 tests (`artifacts/demo_task.json`); each run took ~4.4s wall-clock.
- **Candidate diversity**: only 2 of the 3 candidates were distinct — two were byte-identical despite
  temperature 0.8. Expected for a problem as small as fizzbuzz. In this run the duplicates were both on
  the rejected side, so no preference pair compares identical code, but that can happen. M6's export
  should flag or skip pairs where chosen and rejected code are identical, since they carry no signal.
- Checked that the API key does not appear anywhere in `artifacts/`.
