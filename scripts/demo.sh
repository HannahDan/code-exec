#!/usr/bin/env bash
# End-to-end demo: raw run + LLM task through the real cluster, evidence into artifacts/.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

PYTHON="${PYTHON:-python3}"
PORT="${DEMO_PORT:-8765}"
BASE="http://127.0.0.1:${PORT}"
ART="artifacts"
TERMINAL='["passed","failed_tests","error","timeout","oom","infra_error"]'
export CODE_EXEC_DB="${ROOT}/demo.db"

mkdir -p "$ART"
rm -f "$CODE_EXEC_DB" "$CODE_EXEC_DB-wal" "$CODE_EXEC_DB-shm"
exec > >(tee "$ART/demo_output.txt") 2>&1

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

SERVER_PID=""
cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# wait_for <description> <timeout_s> <command...>: retry until the command succeeds.
wait_for() {
  local what="$1" timeout="$2"; shift 2
  local deadline=$((SECONDS + timeout))
  until "$@"; do
    if (( SECONDS >= deadline )); then
      echo "timed out after ${timeout}s waiting for: $what" >&2
      return 1
    fi
    sleep 1
  done
}

log "kubectl context: $(kubectl config current-context)"
if [[ -z "${AQ_API_KEY:-}" ]]; then
  log "WARNING: AQ_API_KEY is not set; LLM candidates will record the error and fail in the sandbox."
fi

log "1. Applying k8s manifests"
kubectl apply -f k8s/

log "2. Starting server on :${PORT} (log: $ART/demo_server.log)"
"$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" > "$ART/demo_server.log" 2>&1 &
SERVER_PID=$!
healthy() { curl -sf "$BASE/healthz" > /dev/null; }
wait_for "server /healthz" 30 healthy
curl -s "$BASE/healthz"; echo

log "3a. Submitting raw run (POST /runs)"
RAW_TESTS=$'from solution import add\n\ndef test_small():\n    assert add(2, 3) == 5\n\ndef test_negative():\n    assert add(-1, -4) == -5\n'
RAW_RUN_ID=$(jq -n --arg code $'def add(a, b):\n    return a + b\n' --arg tests "$RAW_TESTS" '{code: $code, tests: $tests}' \
  | curl -sf -X POST "$BASE/runs" -H 'content-type: application/json' -d @- | jq -r .id)
echo "raw run id: $RAW_RUN_ID"

log "3b. Submitting LLM task (POST /tasks, fizzbuzz, n_candidates=3)"
FIZZ_PROMPT='Write a Python function `fizzbuzz(n)` that returns a list of strings for the numbers 1..n: "Fizz" for multiples of 3, "Buzz" for multiples of 5, "FizzBuzz" for multiples of both, otherwise the number as a string.'
FIZZ_TESTS=$'from solution import fizzbuzz\n\ndef test_first_five():\n    assert fizzbuzz(5) == ["1", "2", "Fizz", "4", "Buzz"]\n\ndef test_fifteen():\n    assert fizzbuzz(15)[-1] == "FizzBuzz"\n\ndef test_length_and_zero():\n    assert len(fizzbuzz(30)) == 30\n    assert fizzbuzz(0) == []\n'
TASK_ID=$(jq -n --arg p "$FIZZ_PROMPT" --arg t "$FIZZ_TESTS" '{prompt: $p, tests: $t, n_candidates: 3}' \
  | curl -sf -X POST "$BASE/tasks" -H 'content-type: application/json' -d @- | jq -r .task_id)
echo "task id: $TASK_ID"

log "4. Waiting for runs to finish"
raw_done() {
  curl -sf "$BASE/runs/$RAW_RUN_ID" | jq -e --argjson t "$TERMINAL" '.status as $s | $t | index($s)' > /dev/null
}
task_done() {
  curl -sf "$BASE/tasks/$TASK_ID" | jq -e --argjson t "$TERMINAL" '
    (.generating | not) and (.candidates | length > 0)
    and all(.candidates[]; .latest_run.status as $s | $t | index($s))' > /dev/null
}
wait_for "raw run $RAW_RUN_ID" 90 raw_done
curl -sf "$BASE/runs/$RAW_RUN_ID" | jq . > "$ART/demo_raw_run.json"
echo "raw run status: $(jq -r .status "$ART/demo_raw_run.json")"

wait_for "task $TASK_ID" 240 task_done
TASK_JSON=$(curl -sf "$BASE/tasks/$TASK_ID")
echo "$TASK_JSON" | jq -r '.candidates[] | "candidate \(.id): \(.latest_run.status) (\(.latest_run.test_results.passed // 0) passed, \(.latest_run.test_results.failed // 0) failed, \(.latest_run.duration_ms) ms)"'

log "4b. Posting annotations and a preference"
BEST=$(echo "$TASK_JSON" | jq -r '([.candidates[] | select(.latest_run.status == "passed")][0] // .candidates[0]).id')
for row in $(echo "$TASK_JSON" | jq -r '.candidates[] | "\(.id):\(.latest_run.status)"'); do
  cid="${row%%:*}"; status="${row#*:}"
  label="incorrect"; [[ "$status" == "passed" ]] && label="correct"
  jq -n --arg c "$cid" --arg l "$label" --arg n "demo: run status was $status" '{candidate_id: $c, label: $l, notes: $n}' \
    | curl -sf -X POST "$BASE/tasks/$TASK_ID/annotations" -H 'content-type: application/json' -d @- \
    | jq -c '{candidate_id, label, notes}'
done
REJECTED=$(echo "$TASK_JSON" | jq -c --arg best "$BEST" '[.candidates[].id | select(. != $best)]')
if [[ "$REJECTED" != "[]" ]]; then
  jq -n --arg c "$BEST" --argjson r "$REJECTED" '{chosen_candidate_id: $c, rejected_candidate_ids: $r}' \
    | curl -sf -X POST "$BASE/tasks/$TASK_ID/preference" -H 'content-type: application/json' -d @- \
    | jq -c '.[] | {chosen_candidate_id, rejected_candidate_id}'
fi

log "5. Saving evidence to $ART/"
curl -sf "$BASE/tasks/$TASK_ID" | jq . > "$ART/demo_task.json"
curl -sf "$BASE/tasks?include_raw=true" | jq . > "$ART/demo_tasks_list.json"
if curl -sf "$BASE/export/preferences.jsonl" -o "$ART/preferences.jsonl"; then
  echo "preferences export: $(wc -l < "$ART/preferences.jsonl" | tr -d ' ') line(s)"
else
  rm -f "$ART/preferences.jsonl"
  echo "preferences export endpoint not available yet (added in M6)"
fi

log "6. kubectl get jobs,pods -n sandbox -o wide"
kubectl get jobs,pods -n sandbox -o wide | tee "$ART/demo_kubectl.txt"

log "Done. task=$TASK_ID raw_run=$RAW_RUN_ID best=$BEST"
