#!/usr/bin/env python3
"""Exercise the real CLI/worker against an explicitly simulated Ollama runtime."""

import http.server
import json
import os
from pathlib import Path
import socket
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
            self.reply(200, {"models": [{"name": "fixture:tiny", "digest": DIGEST}]})
        else:
            self.reply(404, {"error": "not_found"})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).attempts += 1
        if type(self).attempts == 1:
            self.reply(503, {"error": "simulated transient runtime outage"})
        else:
            self.reply(200, {"response": "fixture-output", "done": True, "eval_count": 1})


def unused_port():
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def main():
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
                submitted = json.loads(cli("submit", *owner, "--manifest", manifest, "--input", records).stdout)
                cli("worker", "run", "--state-dir", state, "--coordinator", origin, "--ollama-url", runtime_origin,
                    "--token-file", token, "--stop-when-idle", "--idle-grace-seconds", "2")
                status = json.loads(cli("status", *owner, submitted["batch_id"]).stdout)
                assert status["counts"].get("succeeded") == 1, status
                assert SimulatedOllama.attempts == 2, SimulatedOllama.attempts
                exported = cli("export", *owner, submitted["batch_id"], "--require-success").stdout.splitlines()
                assert len(exported) == 1
                assert json.loads(exported[0])["output"] == "fixture-output"
                print("PASS: real worker retried a simulated Ollama 503 and accepted one result before idle exit.")
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
    runtime.shutdown()


if __name__ == "__main__":
    main()
