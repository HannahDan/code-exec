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

## M4 Findings (adversarial suite)

Observed on Docker Desktop, Kubernetes v1.25.2, single node with 4 CPUs / 7.5Gi. Per-case results are
in `artifacts/adversarial_results.json` (regenerated by every `make test`); the full log is
`artifacts/test_output.txt` (28 passed, 1 xfailed).

| Case | Status observed | Notes |
|---|---|---|
| Syntax error | `error`, exit 2 | `SyntaxError` in stderr, no tests collected |
| `while True: pass` | `timeout` | ~9.5s from Job start; partial stdout is still captured |
| `bytearray(10**9)` | `oom`, exit 137 | Cgroup OOM kill. Python never raised `MemoryError` |
| Growing list of 10MB chunks | `oom`, exit 137 | Same as above |
| Fork bomb (`os.fork()` loop) | `oom`, exit 137 | Contained in ~7-9s; a normal job right after passes |
| Fork until refused | `passed` | `RLIMIT_NPROC=64` refuses fork with `EAGAIN` (errno 11) after 62-63 children |
| Writes | `passed` | `/etc/foo` fails, `/var/tmp/foo` fails with `EROFS`, `/workspace/foo` fails with `EROFS`, `/tmp/foo` works |
| `print("x" * 10**7)` | `passed` | Log truncated at 64KiB, `truncated: true`; result line recovered via tail read |
| Nonexistent image | `infra_error` | `ErrImagePull` detected in ~3-4s with the registry message |
| Outbound HTTPS | `failed_tests`, test `xfail` | **Not blocked**: pod got `HTTP 200` from example.com |

- **Memory**: accepted either `oom` or `error`+`MemoryError`; both memory cases were OOM-killed. Linux
  overcommit lets the 1GB allocation succeed and the kill happens when pages are touched.
- **Fork bomb**: contained by two limits together. `RLIMIT_NPROC` caps it at 64 processes, then those 64
  busy Python processes exceed the 128Mi cgroup and the container is OOM-killed. The node stayed healthy
  (follow-up job passed). The kubelet has no `podPidsLimit` configured, so without the rlimit the only cap
  would be memory.
- **`RLIMIT_NPROC` is per UID, not per pod.** Every sandbox pod runs as UID 65534 on the same kernel, so
  they share one 64-process budget. Observed directly: the probe got 63 forks in one run and 62 in the
  next, because a process from the previous pod still held a slot. Under concurrency, one fork bomb can
  make `fork()` fail in other candidates' pods. Fix in production: distinct UIDs per pod, user namespaces,
  or the kubelet's `podPidsLimit`.
- **Read-only root FS is what's doing the work**: `/var/tmp` is mode 1777 in the image, so only
  `readOnlyRootFilesystem` can stop the write, and it fails with `EROFS`. `/etc` would fail for UID 65534
  anyway, so it isn't evidence on its own.
- **NetworkPolicy is not enforced on Docker Desktop.** The deny-all policy is applied
  (`kubectl get networkpolicy -n sandbox`), but the pod still reached the internet. Docker Desktop's
  networking doesn't implement NetworkPolicy. The test records the probe and marks itself `xfail`; it
  would pass unchanged on a cluster with an enforcing CNI (Calico, Cilium). The test also checks the host
  is online, so "blocked" can't be a false positive from the host having no internet.

### Executor hardening

- **Deadline before the container starts is now `infra_error`, not `timeout`.** `activeDeadlineSeconds`
  counts from Job start, so a pod stuck `Pending` (e.g. no CPU left: at 500m each only ~6-7 sandbox pods fit
  on this node) or pulling an image would have been reported as the code timing out. Any deadline hit where
  the container was never seen running is now `infra_error` with the last waiting reason / pod phase.
- Already in place from M2 and now covered by tests: log read failures don't raise, missing `__RESULT__`
  gives `result_missing: true`, truncation is flagged, and image pull errors are caught early.

## M5 Decisions (worker pool + recovery)

- **Bounded pool** (`app/worker.py`): an `asyncio.Semaphore(MAX_CONCURRENT_JOBS)` (env, default 4, invalid
  values fall back to 4). A run creates its Job only after acquiring a slot, so the semaphore is also the
  cap on in-flight Jobs from this process. `WorkerPool.stats()` (max, in flight, waiting, peak) is exposed
  on `GET /healthz` under `pool`.
- **Both paths use the pool**: `POST /runs` submits and returns immediately (no longer a FastAPI
  background task). `POST /tasks` still generates in a background task, then submits all candidates to
  the pool and awaits them.
- **Persist first**: rows are inserted as `queued` before `submit()`, and a run only flips to `running`
  (with its `job_name`) once it holds a slot. A crash at any point leaves a row that recovery can act on.
- **Recovery on startup** (before the first request is served):
  - `queued` → re-submitted.
  - `running` and the Job still exists → **re-attached**: the executor resumes polling that Job instead
    of creating a new one, so the code doesn't run twice. `duration_ms` is measured from the original
    `started_at`.
  - `running` and the Job is gone (or never created) → `infra_error` with a message.
  - If the Kubernetes API can't answer whether the Job exists, recovery attaches anyway; the executor then
    records `infra_error` if the Job really is gone.
- **Shutdown** cancels in-flight tasks without touching their rows, so they're recovered on next start.
  Jobs already created keep running in the cluster meanwhile, which is exactly the re-attach case.
- **Concurrency verified in the cluster, not just in the pool**: the test submits 12 runs with
  `MAX_CONCURRENT_JOBS=4` and a background thread counts this test's unfinished Jobs via the K8s API every
  0.3s. Result (`artifacts/adversarial_results.json` → `concurrency`): 12/12 `passed` in ~25s, pool peak
  4, cluster peak 4 over 77 samples. The test also asserts the peak reached 4, so the bound was actually
  exercised rather than trivially met.
- **Recovery verified against a real Job**: the test creates a Job by hand, marks the row `running` as a
  crashed server would leave it, then starts the app; it re-attaches and records `passed` with the
  original job name.

### Limitations

- The pool is per process. Two server processes would each allow `MAX_CONCURRENT_JOBS`. A real
  deployment needs a shared queue/lease (or a K8s ResourceQuota on the `sandbox` namespace as a backstop).
- The limit is only as good as the node's capacity: at 500m CPU per pod, this 4-CPU node fits ~6-7 sandbox
  pods, so values above that would leave pods `Pending` (now reported as `infra_error`, see M4).
- LLM generation (the `GENERATING` set) isn't recovered: a crash mid-generation leaves a task with fewer
  candidates than requested. Only runs are recovered.

## M6 Decisions (preferences, export, UI)

- **Export format** (`GET /export/preferences.jsonl`, `application/x-ndjson`): one line per
  preference row, with `task_id`, `prompt`, `chosen` and `rejected` (each holding `candidate_id`,
  `code`, and its latest `run`: status, exit code, duration, per-test results, stdout/stderr, job
  name), plus `identical_code`, `preference_id` and `created_at`. Runs are included so a consumer can
  filter pairs, e.g. drop pairs where both sides passed.
- **`identical_code` flag instead of dropping pairs**: M3 found the LLM often returns byte-identical
  candidates. Such pairs carry no signal, but whether to drop them is the consumer's call, so they're
  flagged, not filtered. The committed export has 9 pairs and 2 of them are flagged.
- **The export uses each candidate's latest run.** Since reruns can change a candidate's result, the
  export reflects the current state, not the state when the preference was made.
- **Preferences on `GET /tasks/{id}`**, so the UI can show what's already been submitted.
- **UI** (`static/index.html`, one file, no build): task list with status badges, a new-task form,
  candidate cards (code, status, per-test table, stdout/stderr, which open automatically on failure),
  label buttons with notes, a "best" radio, and a preference bar.
  - **XSS**: candidate code and outputs are untrusted. Everything goes through an `el()` helper that
    only creates text nodes. `test_ui_never_uses_innerhtml` guards against `.innerHTML`,
    `.outerHTML`, `insertAdjacentHTML` and `document.write`.
  - **Refresh**: every 2s while anything is generating, queued or running, and every 10s otherwise. It
    re-renders only when the JSON changed, and never while a textarea in the detail pane has focus, so
    typing isn't interrupted. Unsaved notes, code edits and the best-pick live in page state and
    survive re-renders.
- **Delete / rerun / edit** (added during M6 on request):
  - `DELETE /tasks/{id}` removes the task, its candidates, runs, annotations and preferences in one
    transaction.
  - `POST /tasks/{id}/rerun` queues a new run per candidate, optionally replacing the hidden tests
    first. Old runs are kept.
  - `POST /tasks/{id}/candidates` runs edited code as a **new** candidate (`source='edited'`,
    `parent_id`) instead of changing the original, because labels and preference pairs refer to
    candidate ids and the export reads code at export time.
  - Delete and rerun return **409 while generation or runs are in flight**, so a worker never writes
    into a deleted row and reruns don't overlap. Stuck runs clear within the executor's 60s wait cap.
- **Schema migration**: allowing `edited` needed a new CHECK constraint, which SQLite can't alter.
  `init_db()` rebuilds `candidates` (with foreign keys off) when the old constraint is present. It's
  idempotent, and `test_init_db_migrates_pre_edit_candidates_table` covers it. It ran on the real
  `demo.db` without losing rows.
- **Artifacts**: `artifacts/preferences_session.jsonl` is an export of a manual annotation session
  (fizzbuzz plus a slugify task with an edited, failing candidate). `artifacts/ui_screenshot.png` is
  from the same session. `artifacts/preferences.jsonl` is written by `make demo` (see M7).

### Issues hit

- **Committed artifacts were degraded once.** Rerunning only the concurrency test rewrote
  `adversarial_results.json` with just that case (the file is written by a module fixture from
  whatever ran), and `test_output.txt` held a run with a flake. Both got into the M6 commit and were
  regenerated by a clean `make test` in M7. Rule: only commit artifacts from a full `make test`.
- **Concurrency test flake**: one run, right after the fork-bomb and memory tests, had a trivial
  candidate hit the 10s deadline (`timeout`). `activeDeadlineSeconds` also counts pod start-up, which
  is slower while the node is still reclaiming the previous tests' pods. It passed alone and in the
  full rerun. Not fixed: raising the deadline would weaken the timeout guarantee. Something to watch.

## M7 (README)

- README covers the overview, a Mermaid architecture diagram, exact commands from a fresh clone, the
  API, the security model, tradeoffs, limitations, next steps, and an evidence table.
- Every security claim in the README is labelled as tested (naming the test in
  `tests/test_executor.py` and the recorded result in `artifacts/adversarial_results.json`) or as
  **configuration only** (CPU limit, capability drop, service-account token and service links
  disabled). Nothing is claimed as isolation without a demonstration: network egress is documented as
  **not blocked** on Docker Desktop.
- Final `make test`: 46 passed, 1 xfailed (network egress), 1m47s (`artifacts/test_output.txt`).
- Checked that the API key appears in no tracked file or artifact (only in the gitignored `.env`).
- **Final `make demo`** (all `artifacts/demo_*` files and `artifacts/preferences.jsonl`): raw run
  passed, and 3 `openai/gpt-4.1-mini` fizzbuzz candidates each passed 3/3 in ~5.0s wall-clock. All
  three were distinct this time, so neither exported pair is `identical_code` (compare M3, where two
  were byte-identical). The export step that M3 skipped now runs.
- **Demo vs. manual session**: `demo.sh` deletes and recreates `demo.db`, so the manual annotation
  session was moved to a separate `session.db` (gitignored like every `*.db`) before the final demo,
  and its export was renamed to `preferences_session.jsonl` so the demo couldn't overwrite it.
