#!/bin/sh
set -eu

# The coordinator must already be running. No models are installed by this script.
batchdock_model=${1:-gemma2:2b}
batchdock_state=${BATCHDOCK_STATE_DIR:-.batchdock}
batchdock_bin=${BATCHDOCK_BIN:-.build/debug/batchdock}
batchdock_coordinator=${BATCHDOCK_COORDINATOR:-http://127.0.0.1:8787}
batchdock_ollama=${BATCHDOCK_OLLAMA_URL:-http://127.0.0.1:11434}

"$batchdock_bin" doctor --ollama-url "$batchdock_ollama" --model "$batchdock_model" --write-manifest "$batchdock_state/smoke-manifest.json" --max-output-tokens 24
if [ ! -f "$batchdock_state/smoke-worker.token" ]; then
    "$batchdock_bin" worker add --coordinator "$batchdock_coordinator" --state-dir "$batchdock_state" --name smoke-worker --write-token "$batchdock_state/smoke-worker.token"
fi
batchdock_submission=$("$batchdock_bin" submit --coordinator "$batchdock_coordinator" --state-dir "$batchdock_state" --manifest "$batchdock_state/smoke-manifest.json" --input Fixtures/prompts.jsonl)
batchdock_batch_id=$(printf '%s' "$batchdock_submission" | python3 -c 'import json,sys; print(json.load(sys.stdin)["batch_id"])')
"$batchdock_bin" worker run --coordinator "$batchdock_coordinator" --ollama-url "$batchdock_ollama" --state-dir "$batchdock_state" --token-file "$batchdock_state/smoke-worker.token" --stop-when-idle
"$batchdock_bin" status --coordinator "$batchdock_coordinator" --state-dir "$batchdock_state" "$batchdock_batch_id"
"$batchdock_bin" export --coordinator "$batchdock_coordinator" --state-dir "$batchdock_state" "$batchdock_batch_id" --output "$batchdock_state/smoke-results.jsonl" --require-success
printf 'Exported accepted results to %s/smoke-results.jsonl\n' "$batchdock_state"
