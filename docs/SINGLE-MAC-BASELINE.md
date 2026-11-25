# Controlled single-Mac baseline

**Single-host emulation only. Physical P1 is pending real hardware; P2 has not started.** This experiment measures a stronger synthetic workload and externally profiles existing behavior. It changes no production Swift, schema, dependencies, retry policy or model installation.

## Workload and frozen protocol

[benchmark-v2.jsonl](../Fixtures/benchmark-v2.jsonl) contains 200 unique prompts and 200 unique exact answers, across four operational SKU-extraction fields and three record lengths. Distracting previous/alternative SKUs, shuffled field positions and longer histories exercise field selection. A separate parser requires the target field exactly once and checks the committed answer against it. This is generated diagnostic data, not a real customer corpus or evidence of general model quality.

The installed `gemma2:2b` is pinned by digest and Ollama version. Context 4096, output limit 32, temperature 0, seed 42 and system prompt are fixed. Only outer whitespace is ignored: wrong case, additional words, missing/duplicate/reordered IDs, failed states and non-stop generations are rejected. Worker outputs must match saved accepted-attempt observations. A 12-case preflight covers every field/length category and is excluded from measured counts.

Freeze the complete plan, source/workload/lock/binary hashes, randomized schedule, generation settings and decision rules before inference. No failed run is selectively excluded. A validation failure stops the experiment, retains its outputs and prevents acceptance; changes require a new identified experiment. No physical P1 threshold or go/narrow/stop decision is evaluated.

Three repetitions each:

| Phase | Configurations | Records/run | Purpose |
| --- | --- | --- | --- |
| Baseline | Direct script and BatchDock, each at 1 and 2 slots | 200 | Uninstrumented model-resident end-to-end timing |
| Startup probe | Same four configurations | 12 | Model-unloaded startup observations; not full-batch makespan comparisons |
| Profile | BatchDock one worker, matched observer off/on pairs | 60 | External stage attribution and observer perturbation |

Full run: 30 runs, 2,904 outputs plus 12 excluded preflight outputs. Resident baselines and shorter startup probes must not be compared as equal-size batch makespans. Cold/warm model-loading observations use runtime metrics; the startup probe is not a representative full-batch throughput result.

Before every run, unload only the selected model through Ollama, and check `/api/ps`. Residency control uses the documented [Ollama API](https://github.com/ollama/ollama/blob/main/docs/api.md) empty-prompt/keep_alive mechanism, verified against the installed runtime. For resident runs, preload it and execute a fixed excluded READY warmup. This resets the selected model between runs while leaving natural within-run prefix caching possible. “Model-unloaded” means model residency, **not cold OS/filesystem caches**. Initial loaded/unloaded state is restored afterward; the existing Ollama service stays running. Other loaded models cause a refusal to proceed.

The harness starts no competing inference. Other apps, clients, power/thermal conditions and runtime parallelism are not controlled. Two slots share one runtime; this is offered concurrency, not proof of two active GPU sequences. Memory/VM/thermal snapshots and system load are captured around runs; they are not continuous peak-memory/GPU telemetry. No system-wide settings, sleep inhibition or SSH changes are made.

## Timings and profiling

Uninstrumented baseline timing includes submission, worker launch/startup, processing, terminal detection, export and validation; idle shutdown is excluded. Enrollment/coordinator setup and model preparation are outside the interval. Worker state and coordinator database are fresh per run. Owner status is checked directly every 250 ms rather than repeatedly launching a status CLI. This differs from the older emulation harness, so cross-version elapsed times are not interchangeable.

Separate profile runs route worker HTTP through external observers, leaving application code unchanged. The raw events record route intervals and runtime generation metrics. Their interval union avoids double-counting overlap. Submission, process launch, terminal detection and export are measured separately. Runtime metadata, claims, generation and completion HTTP calls can be identified; time outside observed HTTP still includes local work, scheduling, startup and controller overhead. It cannot be labeled SQLite or filesystem time without further evidence.

Observer-off/on pairs use the same 60 inputs, preparation and one worker. Their randomized order is frozen. The proxy changes HTTP connections and adds measurement work; ambient variation also affects paired differences, so profile timings are never mixed into the baseline. Runtime-reported durations, proxy wall intervals and controller durations have different boundaries and must not be summed as independent components. No privileged tracing or runtime configuration changes are required. A separate 60-input diagnostic may use macOS sample on its owned worker and coordinator PIDs for 8 seconds each at 10 ms intervals after the timed experiment. It is excluded from baseline and observer-calibration counts. Wall-stack samples include blocked threads and are not CPU percentages or causal disk/DB durations. Raw samples stay private.

## Reproduce

Requires the existing built CLI and installed model/runtime; use a new ignored output directory. Do not run multiple real-inference experiments concurrently. Residency resets can affect other clients of the same Ollama instance, so use an idle runtime.

```sh
python3 scripts/test-single-mac.py
python3 scripts/benchmark-single-mac.py --runtime fixture --repetitions 1 --output-dir .batchdock/baseline-fixture
python3 scripts/verify-single-mac.py .batchdock/baseline-fixture
python3 scripts/test-single-mac-artifacts.py .batchdock/baseline-fixture
python3 scripts/benchmark-single-mac.py --runtime ollama --output-dir .batchdock/baseline-real
python3 scripts/verify-single-mac.py .batchdock/baseline-real
```

Private artifacts include immutable plan, dataset copy, manifests, per-run inputs/outputs, observations, consistent result state, resource snapshots, external events and cleanup results. The verifier independently checks the prescribed matrix, exact outputs, hashes, observed finish reasons, worker participation, residency checks and profile derivation. The tested binary must still match its recorded hash. Source verification accepts matching working files or exact recorded Git blobs, and lists which archived revisions were used. For historical dirty-tree fixtures only, it can find a later published blob with the exact recorded hash; that is publication evidence, not a rewrite of the original source revision. Required Git history must be available. Forced termination cannot guarantee cleanup or residency restoration.

Prior SSH harness evidence remains in [LOCAL-BENCHMARK.md](LOCAL-BENCHMARK.md). This baseline uses loopback directly to isolate local execution overhead; remote transport and physical P1 acceptance remain pending.

Sanitized results are generated only after verification with `scripts/summarize-single-mac.py`; native sampling reproduction uses `scripts/profile-worker-native.py --reference-plan <private-plan> --output-dir <new-private-directory>`.

Measured results and interpretation: [single-Mac results](SINGLE-MAC-RESULTS.md).
