#!/usr/bin/env python3
"""Exercise the real CLI/worker against an explicitly simulated Ollama runtime."""

import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("BATCHDOCK_BIN", ROOT / ".build/debug/batchdock"))
DIGEST = "a" * 64


class SimulatedOllama(http.server.BaseHTTPRequestHandler):
    attempts = 0
    finish_case = "retry"
    worker_state = None
    queue_path = None

    def log_message(self, *_):
        pass

    def reply(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/api/version":
            self.reply(200, {"version": "fixture-runtime"})
        elif self.path == "/api/tags":
            self.reply(200, {"models": [{"name": "fixture:tiny", "digest": ("b" * 64 if type(self).finish_case == "model_changed" and type(self).attempts else DIGEST)}]})
        else:
            self.reply(404, {"error": "not_found"})

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        assert request["truncate"] is False and request["shift"] is False, request
        type(self).attempts += 1
        if type(self).finish_case == "observation_write_failure":
            # Isolated real rename failure: the destination is a directory.
            with sqlite3.connect("file:" + str(type(self).queue_path) + "?mode=ro", uri=True) as db:
                attempt = db.execute("SELECT id FROM attempts WHERE state='leased'").fetchone()[0]
            (type(self).worker_state / ("generation-" + attempt + ".json")).mkdir()
        if type(self).finish_case == "retry" and type(self).attempts == 1:
            self.reply(503, {"error": "simulated transient runtime outage"})
        else:
            case = type(self).finish_case
            body = {"response": "fixture-output", "done": case != "unfinished", "eval_count": 1}
            if case != "missing":
                body["done_reason"] = {"length": "length", "unknown": "future_reason", "empty": ""}.get(case, "stop")
            self.reply(200, body)


def unused_port():
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def main(finish_case="retry"):
    SimulatedOllama.attempts = 0
    SimulatedOllama.finish_case = finish_case
    runtime = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SimulatedOllama)
    threading.Thread(target=runtime.serve_forever, daemon=True).start()
    runtime_origin = f"http://127.0.0.1:{runtime.server_port}"
    port = unused_port()
    origin = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="batchdock-worker-retry-") as temporary:
        state = Path(temporary) / "state"

        def cli(*args):
            return subprocess.run([str(BINARY), *map(str, args)], cwd=ROOT, check=True, capture_output=True, text=True, timeout=45)

        cli("init", "--state-dir", state)
        with open(Path(temporary) / "server.log", "w") as log:
            server = subprocess.Popen([str(BINARY), "serve", "--state-dir", str(state), "--port", str(port)], stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try:
                        urllib.request.urlopen(origin + "/health", timeout=0.5).close()
                        break
                    except OSError:
                        if server.poll() is not None:
                            raise RuntimeError("Coordinator exited before health check")
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Coordinator did not become ready")
                manifest = Path(temporary) / "manifest.json"
                records = Path(temporary) / "prompts.jsonl"
                records.write_text(json.dumps({"id": "retry-me", "prompt": "fixture prompt"}) + "\n")
                cli("doctor", "--ollama-url", runtime_origin, "--model", "fixture:tiny", "--write-manifest", manifest)
                token = state / "worker.token"
                owner = ["--state-dir", state, "--coordinator", origin]
                cli("worker", "add", *owner, "--name", "retry-fixture", "--write-token", token)
                SimulatedOllama.queue_path = state / "queue.sqlite"
                token_hash = hashlib.sha256(token.read_text().strip().encode()).hexdigest()[:20]
                SimulatedOllama.worker_state = state / "workers" / token_hash
                submitted = json.loads(cli("submit", *owner, "--manifest", manifest, "--input", records).stdout)
                cli("worker", "run", "--state-dir", state, "--coordinator", origin, "--ollama-url", runtime_origin,
                    "--token-file", token, "--stop-when-idle", "--idle-grace-seconds", "2")
                status = json.loads(cli("status", *owner, submitted["batch_id"]).stdout)
                success = finish_case in ("retry", "stop")
                assert status["counts"].get("succeeded" if success else "failed") == 1, status
                assert SimulatedOllama.attempts == (2 if finish_case == "retry" else 3 if finish_case == "observation_write_failure" else 1), SimulatedOllama.attempts
                exported = cli("export", *owner, submitted["batch_id"], *(["--require-success"] if success else [])).stdout.splitlines()
                assert len(exported) == 1
                row = json.loads(exported[0])
                assert row["output"] == ("fixture-output" if success else None), row
                observations = [p for p in state.glob("workers/*/generation-*.json") if p.is_file()]
                if finish_case == "observation_write_failure":
                    assert row["error"]["code"] == "local_write", row
                    assert not observations
                    assert not list(state.glob("workers/*/completion-*.json"))
                elif finish_case == "model_changed":
                    assert row["error"]["code"] == "model_changed", row
                    assert not observations
                else:
                    assert len(observations) == 1
                    observed = json.loads(observations[0].read_text())["result"]
                    assert observed["output"] == "fixture-output"
                    expected_reason = {"length": "length", "unknown": "future_reason", "empty": "", "missing": None}.get(finish_case, "stop")
                    assert observed.get("finish_reason") == expected_reason, observed
                    if not success:
                        expected = {"length": "generation_length_limited", "unknown": "unknown_finish_reason",
                                    "empty": "unknown_finish_reason", "missing": "missing_finish_reason", "unfinished": "incomplete_inference"}[finish_case]
                        assert row["error"]["code"] == expected, row
                print(f"PASS real worker {finish_case}: {'one accepted result' if success else 'terminal failure, no accepted output'}; observation/replay contract checked.")
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
    runtime.shutdown()
    runtime.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-finish-cases", action="store_true")
    args = parser.parse_args()
    for case in (["retry", "stop", "length", "missing", "unknown", "empty", "unfinished", "model_changed", "observation_write_failure"] if args.all_finish_cases else ["retry"]):
        main(case)
