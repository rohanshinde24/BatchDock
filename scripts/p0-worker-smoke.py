#!/usr/bin/env python3
"""Single-host P0 smoke with exact synthetic extraction checks; never runs P1.

Uses an already installed gemma2:2b and existing Ollama. Starts only its own
short-lived coordinator/worker. Private credentials and artifacts stay under
.batchdock; the output directory must be new. No models or services installed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"kettle": "kettle", "lamp": "desk lamp", "backpack": "hiking backpack"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    private_root = (ROOT / ".batchdock").resolve()
    if not output.is_relative_to(private_root):
        parser.error("Use a new directory under this checkout's .batchdock for private state")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    binary = ROOT / ".build/debug/batchdock"
    state = output / "state"
    records = ROOT / "Fixtures/prompts.jsonl"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"

    def cli(*arguments):
        return subprocess.run([str(binary), *map(str, arguments)], cwd=ROOT, check=True,
                              capture_output=True, text=True, timeout=120).stdout

    cli("init", "--state-dir", state)
    with (output / "coordinator.log").open("w") as log:
        server = subprocess.Popen([str(binary), "serve", "--state-dir", str(state), "--port", str(port)],
                                  cwd=ROOT, stdout=log, stderr=log)
        try:
            for _ in range(200):
                if server.poll() is not None:
                    raise RuntimeError("P0 coordinator exited before becoming ready")
                try:
                    urlopen(origin + "/health", timeout=0.5).close()
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError("P0 coordinator did not become ready")
            manifest = output / "manifest.json"
            cli("doctor", "--model", "gemma2:2b", "--write-manifest", manifest, "--max-output-tokens", "64")
            owner = ["--state-dir", state, "--coordinator", origin]
            credential = state / "worker.token"
            cli("worker", "add", *owner, "--name", "p0-local", "--write-token", credential)
            batch = json.loads(cli("submit", *owner, "--manifest", manifest, "--input", records))["batch_id"]
            cli("worker", "run", "--state-dir", state, "--coordinator", origin,
                "--token-file", credential, "--stop-when-idle", "--idle-grace-seconds", "1")
            status = json.loads(cli("status", *owner, batch))
            rows = [json.loads(line) for line in cli("export", *owner, batch, "--require-success").splitlines()]
            observations = [json.loads(p.read_text()) for p in state.glob("workers/*/generation-*.json")]
            checks = {
                "all_succeeded": status["counts"] == {"succeeded": 3},
                "ordered_ids": [row["id"] for row in rows] == list(EXPECTED),
                "exact_normalized_outputs": len(rows) == 3 and all(
                    row["id"] in EXPECTED and isinstance(row.get("output"), str)
                    and " ".join(row["output"].split()).casefold() == EXPECTED[row["id"]] for row in rows),
                "accepted_attempt_observations": len(observations) == 3 and {
                    (observation["attempt_id"], observation["identity"]["job_id"], observation["identity"]["batch_id"])
                    for observation in observations} == {(row["accepted_attempt_id"], row["id"], batch) for row in rows},
                "explicit_stop_observations": len(observations) == 3 and all(
                    observation["result"]["done"] is True and observation["result"].get("finish_reason") == "stop"
                    for observation in observations),
            }
            report = {
                "schema_version": 1, "run_id": output.name, "topology": "one local coordinator/worker; P0, not P1",
                "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "source_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "input_sha256": hashlib.sha256(records.read_bytes()).hexdigest(),
                "lock_sha256": hashlib.sha256((ROOT / "Package.resolved").read_bytes()).hexdigest(),
                "manifest": json.loads(manifest.read_text()), "status": status,
                "checks": checks, "outputs": rows, "observations": observations,
            }
            (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            if not all(checks.values()):
                raise RuntimeError("P0 extraction validation failed; inspect the private report")
            print("PASS P0 real worker: 3 exact normalized answers, ordered output and explicit stop observations")
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == "__main__":
    main()
