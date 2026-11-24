#!/usr/bin/env python3
"""P0 contract probes on an ALREADY INSTALLED local model; no pulls or host changes.

These are single-host runtime observations, not a P1 benchmark. Raw artifacts
contain only the synthetic requests below. Probe results are evidence, not a
claim that a stop reason proves full-input processing or semantic correctness.
"""
import argparse
import hashlib
import json
import platform
from pathlib import Path
import socket
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def digest(data):
    return hashlib.sha256(data).hexdigest()


def capture(origin, path, body=None):
    request = Request(origin + path, data=None if body is None else json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
    start = time.monotonic()
    try:
        try:
            response = urlopen(request, timeout=90)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise RuntimeError("Probe response exceeded 2 MiB")
            return {"http_status": response.status, "elapsed_seconds": time.monotonic() - start,
                    "response": json.loads(raw)}
    except (URLError, TimeoutError, ConnectionError) as error:
        return {"transport_error": type(error).__name__, "elapsed_seconds": time.monotonic() - start}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="gemma2:2b")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    url = urlparse(args.origin)
    if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
        parser.error("Use a loopback HTTP origin")
    args.origin = args.origin.rstrip("/")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    version = capture(args.origin, "/api/version")
    tags = capture(args.origin, "/api/tags")
    installed = next((m for m in tags.get("response", {}).get("models", []) if m["name"] == args.model), None)
    if not installed:
        parser.error("Exact model tag is not installed; probes never pull models")
    show = capture(args.origin, "/api/show", {"model": args.model})
    cases = [
        ("natural_stop", "Return only the word kettle.", 512, 64, {}),
        ("output_limit", "Count upwards from one, continuing for one hundred numbers.", 512, 1, {}),
        ("small_context_request", "alpha " * 80 + "Return kettle.", 128, 8, {}),
        ("near_context_fit", "alpha " * 240 + "Return kettle.", 256, 1, {"truncate": False, "shift": False}),
        ("near_context_overflow", "alpha " * 248 + "Return kettle.", 256, 1, {"truncate": False, "shift": False}),
        ("oversized_default", "alpha " * 4500 + "Return kettle.", 128, 8, {}),
        ("oversized_reject_requested", "alpha " * 4500 + "Return kettle.", 128, 8, {"truncate": False, "shift": False}),
        ("structured_output", 'Return JSON with one key product and string value kettle.', 512, 64, {"format": "json"}),
    ]
    metadata = {
        "schema_version": 1, "run_id": args.output_dir.name,
        "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "source_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()),
        "lock_sha256": digest(Path("Package.resolved").read_bytes()),
        "source_hashes": {str(p): digest(p.read_bytes()) for p in sorted(Path("Sources").rglob("*.swift"))},
        "probe_script_sha256": digest(Path(__file__).read_bytes()),
        "swift": subprocess.check_output(["swift", "--version"], stderr=subprocess.STDOUT, text=True).strip(),
        "macos": platform.mac_ver()[0], "architecture": platform.machine(),
        "hardware_model": subprocess.check_output(["sysctl", "-n", "hw.model"], text=True).strip(),
        "ram_bytes": int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)),
        "topology": "single Mac / one existing Ollama service; sequential P0 probes, no throughput comparison",
        "runtime": version, "model": installed, "model_details": show,
        "cases": {},
    }
    for name, prompt, ctx, predict, extra in cases:
        body = {"model": args.model, "prompt": prompt, "stream": False,
                "options": {"temperature": 0, "seed": 42, "num_ctx": ctx, "num_predict": predict}, **extra}
        record = {"request": body, "request_sha256": digest(json.dumps(body, sort_keys=True).encode()),
                  **capture(args.origin, "/api/generate", body)}
        (args.output_dir / (name + ".json")).write_text(json.dumps(record, indent=2) + "\n")
        metadata["cases"][name] = {k: v for k, v in record.items() if k != "request"}
        if name == "natural_stop":
            metadata["cases"][name]["fixture_valid"] = " ".join(record.get("response", {}).get("response", "").split()).casefold() == "kettle"
        if name == "structured_output":
            try:
                metadata["cases"][name]["fixture_valid"] = json.loads(record["response"]["response"]) == {"product": "kettle"}
            except (KeyError, ValueError, TypeError):
                metadata["cases"][name]["fixture_valid"] = False
        print(name, record.get("http_status", "transport_error"), record.get("response", {}).get("done_reason"), flush=True)
    # A bound but non-listening loopback socket gives a safe unavailable endpoint.
    with socket.socket() as blocked:
        blocked.bind(("127.0.0.1", 0))
        metadata["unavailable"] = capture(f"http://127.0.0.1:{blocked.getsockname()[1]}", "/api/version")
    metadata["live_alias_mutation"] = "not performed: no installed model/tag is modified; identity drift is covered by simulated worker tests"
    (args.output_dir / "report.json").write_text(json.dumps(metadata, indent=2) + "\n")


if __name__ == "__main__":
    main()
