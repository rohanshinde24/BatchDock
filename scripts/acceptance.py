#!/usr/bin/env python3
"""Exercise the real coordinator over HTTP with synthetic protocol workers.

This checks queue correctness and crash recovery. It does not run inference or
measure performance across Macs. Run the separate Ollama smoke for inference.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


MODEL = {"tag": "acceptance:synthetic", "digest": "a" * 64, "ollama_version": "acceptance"}
MANIFEST = {
    "protocol_version": 1,
    "model": MODEL,
    "generation": {
        "system": "Protocol verification only",
        "format": "text",
        "options": {"temperature": 0, "seed": 42, "num_ctx": 512, "num_predict": 16},
    },
    "attempt_limit_seconds": 30,
}


def new_id() -> str:
    return str(uuid.uuid4())


def check(condition: bool, description: str) -> None:
    if not condition:
        raise AssertionError(description)


class Coordinator:
    def __init__(self, binary: Path, root: Path) -> None:
        self.binary = binary
        self.root = root
        self.state = root / "state"
        self.process: subprocess.Popen | None = None
        self.log = (root / "coordinator.log").open("w+")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        subprocess.run(
            [str(binary), "init", "--state-dir", str(self.state)],
            check=True, stdout=self.log, stderr=self.log,
        )
        candidates = [self.state / "owner.token", self.state / "owner-token"]
        token_file = next((path for path in candidates if path.is_file()), None)
        if token_file is None:
            raise RuntimeError("Coordinator initialization did not create its owner credential")
        self.owner = token_file.read_text().strip()

    def start(self) -> None:
        self.process = subprocess.Popen(
            [str(self.binary), "serve", "--state-dir", str(self.state),
             "--port", str(self.port), "--lease-seconds", "2"],
            stdout=self.log, stderr=self.log,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Coordinator stopped before accepting requests")
            try:
                # Any HTTP response confirms that this process is serving.
                self.request("GET", "/v1/batches/not-a-batch", self.owner, expected=(404,))
                return
            except (URLError, ConnectionError, TimeoutError):
                time.sleep(0.05)
        raise TimeoutError("Coordinator did not become ready")

    def stop(self, crash: bool = False) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.kill() if crash else self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    def request(self, method: str, path: str, token: str | None,
                body: dict | None = None, expected: tuple[int, ...] = (200,),
                headers: dict | None = None):
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.origin + path, data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                status, payload = response.status, response.read()
        except HTTPError as error:
            status, payload = error.code, error.read()
        check(status in expected, f"{method} {path} returned {status}; expected {expected}")
        if not payload:
            return None
        if path.endswith("/export"):
            return [json.loads(line) for line in payload.splitlines() if line.strip()]
        return json.loads(payload)

    def worker(self, name: str) -> str:
        return self.request("POST", "/v1/workers", self.owner, {"name": name}, expected=(201,))["token"]

    def submit(self, ids: list[str], key: str | None = None) -> dict:
        return self.request(
            "POST", "/v1/batches", self.owner,
            {"manifest": MANIFEST, "records": [{"id": value, "prompt": value} for value in ids]},
            expected=(200, 201), headers={"Idempotency-Key": key or new_id()},
        )

    def claim(self, token: str, claim_id: str | None = None, incarnation: str | None = None):
        body = {"claim_id": claim_id or new_id(), "incarnation": incarnation or new_id(), "models": [MODEL]}
        return self.request("POST", "/v1/claims", token, body, expected=(200, 204, 409))

    @staticmethod
    def completion(lease: dict, output: str) -> dict:
        identity = {key: lease[key] for key in
                    ("batch_id", "job_id", "epoch", "generation", "worker_incarnation")}
        return {
            "identity": identity, "output": output,
            "output_hash": hashlib.sha256(output.encode()).hexdigest(),
            "model_digest": MODEL["digest"], "ollama_version": MODEL["ollama_version"],
            "metrics": {},
        }

    def complete(self, token: str, lease: dict, output: str, expected=(200,)):
        return self.request("POST", f"/v1/attempts/{lease['attempt_id']}/complete", token,
                            self.completion(lease, output), expected=expected)


def exercise(server: Coordinator) -> None:
    server.start()
    server.request("POST", "/v1/workers", None, {"name": "unauthorized"}, expected=(401,))
    tokens = [server.worker(f"synthetic-{index}") for index in range(8)]

    request_key = new_id()
    batch = server.submit(["race"], request_key)["batch_id"]
    check(server.submit(["race"], request_key)["batch_id"] == batch, "Submission replay created a new batch")
    server.request("POST", "/v1/batches", server.owner,
                   {"manifest": MANIFEST, "records": [{"id": "different", "prompt": "different"}]},
                   expected=(409,), headers={"Idempotency-Key": request_key})

    with ThreadPoolExecutor(max_workers=len(tokens)) as executor:
        claims = list(executor.map(server.claim, tokens))
    winners = [(token, lease) for token, lease in zip(tokens, claims)
               if lease and "attempt_id" in lease]
    check(len(winners) == 1, "Several workers claimed the same queued job")
    token, lease = winners[0]
    server.complete(token, lease, "accepted result")
    check(server.complete(token, lease, "accepted result")["replayed"], "Completion replay was not recognized")
    server.complete(token, lease, "changed result", expected=(409,))
    print("PASS concurrent claiming, submission idempotency and completion replay")

    batch2 = server.submit(["lease-recovery"])["batch_id"]
    old = server.claim(tokens[0])
    check(old and old.get("batch_id") == batch2, "Could not acquire recovery item")
    time.sleep(2.15)
    new = server.claim(tokens[1])
    check(new and new.get("generation", 0) > old["generation"], "Expired work was not reassigned")
    server.complete(tokens[0], old, "stale", expected=(409,))
    server.complete(tokens[1], new, "recovered")
    print("PASS lease expiry and rejection of stale worker results")

    batch3 = server.submit(["restart-recovery"])["batch_id"]
    old = server.claim(tokens[2])
    check(old and old.get("batch_id") == batch3, "Could not acquire restart item")
    server.stop(crash=True)
    server.start()
    new = server.claim(tokens[3])
    check(new and new["epoch"] > old["epoch"], "Coordinator restart did not advance epoch")
    server.complete(tokens[2], old, "old epoch", expected=(409,))
    server.complete(tokens[3], new, "after restart")
    rows = server.request("GET", f"/v1/batches/{batch}/export", server.owner)
    check(len(rows) == 1 and rows[0]["output"] == "accepted result", "Restart lost an accepted result")
    print("PASS coordinator crash recovery and durable accepted output")

    batch4 = server.submit(["cancel-first", "cancel-second"])["batch_id"]
    lease = server.claim(tokens[4])
    check(lease and lease.get("batch_id") == batch4, "Could not acquire cancellation item")
    server.request("POST", f"/v1/batches/{batch4}/cancel", server.owner, {})
    server.complete(tokens[4], lease, "too late", expected=(409,))
    rows = server.request("GET", f"/v1/batches/{batch4}/export", server.owner)
    check([row["id"] for row in rows] == ["cancel-first", "cancel-second"], "Export changed input order")
    check(all(row["state"] == "cancelled" and row.get("output") is None for row in rows),
          "Cancelled work acquired a new result")
    print("PASS cancellation and complete ordered export")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path(".build/debug/batchdock"))
    arguments = parser.parse_args()
    binary = arguments.binary.resolve()
    if not binary.is_file():
        parser.error("Build batchdock before running acceptance checks")
    with tempfile.TemporaryDirectory(prefix="batchdock-acceptance-") as directory:
        server = Coordinator(binary, Path(directory))
        try:
            exercise(server)
            print("All HTTP acceptance checks passed (synthetic protocol workers; no inference benchmark).")
        finally:
            server.stop()
            server.log.close()


if __name__ == "__main__":
    main()
