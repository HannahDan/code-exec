# SPEC: Mini Code-Annotation Runner (2-hour practice build)

## What we're building

A service where:
1. A task (prompt + hidden tests) is submitted.
2. An LLM generates N candidate Python solutions.
3. Each candidate runs in an isolated, resource-limited Kubernetes Job.
4. Results (stdout, stderr, exit code, per-test pass/fail, duration, status) are stored.
5. A human annotator reviews candidates in a simple UI, labels them, and picks the best.
6. Labels export as preference data (JSONL) for model training.

## Environment and constraints

- Local Kubernetes via Docker Desktop (`kubectl config current-context` = `docker-desktop`).
- Python 3.11+, FastAPI, uvicorn, the official `kubernetes` Python client, `openai` client, SQLite (stdlib `sqlite3`), pytest.
- Sandbox image: `python:3.12-slim` with `imagePullPolicy: IfNotPresent`. No pip installs inside sandbox pods.
- LLM config comes **only** from env vars. Never hardcode or commit secrets:
  - `AQ_API_KEY`, `AQ_BASE_URL` (default `https://api.aqinference.com/v1`), `LLM_MODEL` (default `openai/gpt-4.1-mini`).
  - Commit `.env.example`; add `.env` to `.gitignore`.
- The executor must be usable **without** the LLM (raw code endpoint) so tests are deterministic.

## Repo layout

```
app/
  main.py          # FastAPI app, routes, serves static UI
  db.py            # SQLite schema + helpers
  llm.py           # candidate generation
  executor.py      # Kubernetes Job lifecycle: create, wait, collect, classify
  worker.py        # concurrency control (bounded pool)
  runner_template.py  # script mounted into the sandbox pod
static/index.html  # annotator UI (plain HTML + fetch, no build step)
k8s/
  namespace.yaml
  networkpolicy.yaml
tests/
  test_executor.py     # adversarial + happy-path cases (hit K8s for real)
  test_api.py
scripts/demo.sh        # end-to-end demo, writes output into artifacts/
artifacts/             # committed evidence: demo output, test logs, exports
Makefile               # setup, k8s-apply, run, test, demo
README.md
NOTES.md
.env.example
```

## Data model (SQLite)

- `tasks(id, prompt, tests, created_at)`
- `candidates(id, task_id, source ["llm"|"raw"], code, created_at)`
- `runs(id, candidate_id, job_name, status, exit_code, stdout, stderr, test_results_json, duration_ms, started_at, finished_at)`
- `annotations(id, task_id, candidate_id, label ["correct"|"incorrect"|"partial"], notes, created_at)`
- `preferences(id, task_id, chosen_candidate_id, rejected_candidate_id, created_at)`

Run `status` is one of: `queued`, `running`, `passed`, `failed_tests`, `error`, `timeout`, `oom`, `infra_error`.

## Sandbox execution contract

### Test format
`tests` is Python source containing top-level `test_*` functions that use plain `assert`. The solution is importable as module `solution`. Example:

```python
from solution import add
def test_basic():
    assert add(2, 3) == 5
```

### Runner (`runner_template.py`)
Mounted at `/workspace/runner.py` alongside `/workspace/solution.py` and `/workspace/tests.py`. It must:
1. Set resource limits with `resource.setrlimit`: `RLIMIT_NPROC` (e.g. 64), `RLIMIT_FSIZE` (e.g. 10 MB), `RLIMIT_CORE` 0.
2. Import `tests`, run each `test_*` function, catch exceptions per test (record name, passed, error message truncated to 500 chars).
3. Print a single final line: `__RESULT__ {json}` with `{"tests": [...], "passed": n, "failed": n}`.
4. Exit 0 if all tests pass, 1 if any fail, 2 if import/collection fails.

### Job spec (built in `executor.py`)
- Namespace `sandbox`; name `run-<8 hex chars>` (DNS-1123 safe).
- Code delivered via a ConfigMap mounted read-only at `/workspace`. After creating the Job, patch the ConfigMap with an `ownerReference` to the Job so it's garbage-collected with it.
- Job: `backoffLimit: 0`, `activeDeadlineSeconds: 10`, `ttlSecondsAfterFinished: 300`.
- Pod: `restartPolicy: Never`, `automountServiceAccountToken: false`, `enableServiceLinks: false`.
- Container: `command: ["python", "-u", "/workspace/runner.py"]`, `workingDir: /workspace`, env `PYTHONDONTWRITEBYTECODE=1`.
- Resources: requests and limits `cpu: 500m`, `memory: 128Mi`.
- Security context: `runAsNonRoot: true`, `runAsUser: 65534`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: ["ALL"]`, `seccompProfile.type: RuntimeDefault`.
- Writable `/tmp` via `emptyDir` with `sizeLimit: 16Mi`.
- Labels: `app=code-runner`, `run-id=<id>` (so pods can be found by label selector).

### Collecting results
- Poll job status every ~0.5s (run the sync K8s client via `asyncio.to_thread`).
- Classify:
  - Job condition `Failed` with reason `DeadlineExceeded` → `timeout`.
  - Container `terminated.reason == "OOMKilled"` → `oom`.
  - Exit 0 → `passed`; exit 1 → `failed_tests`; other → `error`.
  - Pod stuck in `ImagePullBackOff`/`ErrImagePull`, or any API exception → `infra_error` with message.
- Read logs with `read_namespaced_pod_log(..., limit_bytes=65536)`; mark `truncated: true` if at limit. Parse the `__RESULT__` line if present.
- Always record `duration_ms`. The executor must never raise to the API layer; failures become a run row with a status.

### Network isolation
`k8s/networkpolicy.yaml`: deny all ingress and egress for pods labeled `app=code-runner` in `sandbox`.
**Important:** Docker Desktop's default networking may not enforce NetworkPolicy. The test suite must actually attempt an outbound request and record what happened. Report the real result in README (enforced or not, and what would be needed in production, e.g. a CNI like Calico/Cilium or gVisor). Do not claim isolation that wasn't demonstrated.

## API

- `POST /runs` `{code, tests}` → create raw candidate + run, return run (used by tests; no LLM).
- `POST /tasks` `{prompt, tests, n_candidates=3}` → create task, generate candidates via LLM, enqueue runs, return task id immediately.
- `GET /tasks` → list with summary counts.
- `GET /tasks/{id}` → task, candidates, latest run per candidate, annotations.
- `POST /tasks/{id}/annotations` `{candidate_id, label, notes}`.
- `POST /tasks/{id}/preference` `{chosen_candidate_id, rejected_candidate_ids: [...]}` → one row per rejected.
- `GET /export/preferences.jsonl` → one line per pair: `{task_id, prompt, chosen: {code, run}, rejected: {code, run}}`.
- `GET /healthz` → checks DB and `kubectl`-equivalent API reachability.
- `GET /` → serves `static/index.html`.

## LLM generation (`llm.py`)

- System prompt: return only a single Python code block implementing the requested function(s), no explanation.
- Strip markdown fences robustly; if parsing fails, store the raw text anyway (it'll fail at runtime, which is useful data).
- Use temperature ~0.8 for N candidates so they differ. Handle API errors by recording a candidate with an error note rather than failing the whole task.

## Concurrency (`worker.py`)

- Bounded pool: at most `MAX_CONCURRENT_JOBS` (env, default 4) Jobs in flight via an `asyncio.Semaphore`.
- Runs are persisted as `queued` before execution so a crash doesn't lose them. On startup, re-enqueue any `queued`/`running` rows (mark stale `running` as `infra_error` if its Job no longer exists).

## Annotator UI (`static/index.html`)

Plain HTML/JS, no framework. Task list on the left; selected task shows prompt, and each candidate as a card with code, status badge, per-test results, stdout/stderr (collapsible). Per candidate: label buttons and notes field. A "pick best" control that submits preferences. A link to download the JSONL export. Auto-refresh while any run is `queued`/`running`.

## Tests (must actually run against the local cluster)

`tests/test_executor.py`, each asserting the final status:
- happy path → `passed`
- wrong answer → `failed_tests` with correct per-test detail
- syntax error → `error` (exit 2)
- `while True: pass` → `timeout`, and completes in < 20s wall time
- memory bomb (`x = bytearray(10**9)` or growing list) → `oom` (or `error` with MemoryError; accept either but record which)
- fork bomb via `os.fork` loop → contained (does not hang the node; status recorded)
- writing to `/etc/foo` → fails (read-only FS)
- huge stdout (`print("x"*10**7)`) → run completes, output truncated
- outbound network (`urllib.request.urlopen("https://example.com", timeout=3)`) → record result; test asserts it's blocked **only if** NetworkPolicy is enforced, otherwise marked `xfail` with reason
- concurrency: submit 12 raw runs at once → all reach a terminal state; peak in-flight Jobs never exceeds `MAX_CONCURRENT_JOBS`

`make test` runs pytest with `-v` and tees output to `artifacts/test_output.txt`.

## Demo and evidence

`scripts/demo.sh`:
1. Apply k8s manifests.
2. Start server in background.
3. Submit one raw run and one LLM task ("write `fizzbuzz(n)` returning a list of strings" with 3 tests).
4. Wait for completion, post an annotation and a preference.
5. Save `GET /tasks/{id}` JSON and the preferences export into `artifacts/`.
6. Also save `kubectl get jobs,pods -n sandbox -o wide` output into `artifacts/`.

Every claim in README must point to a file in `artifacts/` or a test.

## Milestones (time boxes)

| # | Time | Deliverable | Done when |
|---|------|-------------|-----------|
| M1 | 0:00–0:15 | Scaffold, Makefile, namespace, `.env.example`, DB schema | `make run` serves `/healthz` OK |
| M2 | 0:15–0:45 | Executor + runner + `POST /runs` | happy path, wrong answer, timeout tests pass |
| M3 | 0:45–1:00 | LLM generation + `POST /tasks` + annotations | `demo.sh` works end to end; commit artifacts |
| M4 | 1:00–1:20 | Hardening + full adversarial suite + NetworkPolicy check | `make test` green (or documented xfail) |
| M5 | 1:20–1:35 | Worker pool + restart recovery + concurrency test | concurrency test passes |
| M6 | 1:35–1:50 | Preferences + JSONL export + UI | export file in `artifacts/`; UI screenshot if possible |
| M7 | 1:50–2:00 | README: architecture, how to run, tradeoffs, limitations, evidence links | final commit |

## README must include

- One-paragraph overview and a short architecture diagram (ASCII or Mermaid).
- Exact commands to run everything from a fresh clone.
- Security model: what's enforced, what was tested, what isn't covered (shared kernel, no gVisor, NetworkPolicy enforcement status, `RLIMIT_NPROC` being per-UID).
- Tradeoffs: why ConfigMap over image build, why polling over watch, why SQLite.
- What I'd do next with more time (watch API, gVisor/Kata, per-language images, result caching, auth, horizontal workers with a real queue).
