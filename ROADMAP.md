# Production roadmap

**Status: planning only. Production implementation has not started.** The current release is a working CLI MVP. This roadmap defines the next acceptance gates for a small trusted team; it does not describe completed production features.

The deployment boundary remains one coordinator with local SQLite and enrolled Mac workers using existing SSH access. Users select an installed model; each batch pins its digest, Ollama version and generation settings. Workers execute whole independent prompts. High availability, public workers, tensor parallelism, training and multi-tenancy are outside this release.

## Milestones

| Gate | Deliverable | Acceptance evidence |
| --- | --- | --- |
| P0 — Baseline | Reproducible build, tests and measurement definitions | Fresh-checkout checks pass; report source/lock/workload hashes, model/runtime/options, hardware, topology and exact commands. Separate simulated, local-model and physical multi-Mac runs. |
| P1 — Operability | Supported setup, shutdown, backup/restore, upgrades and credential operations | Restore at least 100 accepted synthetic outputs plus pending work in isolation; verify backed-up hashes and documented recovery. Test old-schema upgrade, reject duplicate coordinators before state mutation, and preserve secrets in logs/files. |
| P2 — Fault campaign | Reproducible correctness checks across crashes, ambiguity and overload failures | At least 1,000 seeded scenarios / 10,000 synthetic jobs, with at least 20 scenarios per named fault class. Zero duplicate accepted results, stale successful writes or lost acknowledged results under process crashes; every input has one ordered terminal export row. |
| P3 — Physical Macs | Real SSH onboarding, recovery and controlled benchmarks | Two physical Macs complete 200 inputs and recover a worker disconnect; both receive work. Evaluate two supported installed model identities separately. Compare a script, tuned single-Mac BatchDock and two Macs over at least three runs per configuration, separating cold/warm starts. |
| P4 — Resource controls | Profiling-led admission/backpressure experiment | Instruments traces identify a bottleneck. Repeated comparisons target ≥20% lower p95 queue wait without >5% throughput regression, or ≥15% higher throughput within declared memory budgets without >5% p95 completion-latency regression. Preserve correctness under load. |
| P5 — Pilot/release | Independent reuse and a reproducible release | Two independent collaborators, two real datasets, three completed batches and at least one repeat user; record onboarding and assistance. Verify clean installation, checksums, compatibility, rollback and release smoke tests. |

All gates are **not yet accepted**. P4 is optional for a fixed-slot release; it is required before claiming adaptive scheduling or its performance improvements. A failed experiment must be reported, and does not justify shipping an unproven scheduler.

## Correctness and recovery requirements

- Exercise lost claim/completion/failure responses, duplicate/reordered requests, competing incarnations, worker/coordinator crashes, delayed or partitioned transport, cancellation races, revocation, model changes, clock/sleep behavior, runtime errors and isolated storage/write failures.
- Accepted results must match their immutable manifest. Identical retries replay their outcome; changed payloads conflict. Restarting a worker cannot bypass ownership or attempt budgets.
- In the controlled replacement-worker test, target p95 reassignment within lease duration + poll interval + 2 seconds. Measure from acknowledged kill injection to replacement claim acknowledgment; inference completion is separate. This is an experiment target, not a production SLA.
- Process-crash durability is distinct from backup recovery. Restore guarantees cover the documented backup boundary, not all subsequent acknowledged work. Use a stopped coordinator or consistent SQLite backup API; copying only a live WAL-mode main file is unsupported.
- Fault injection must remain isolated: no filling the host disk, changing host clocks, disabling SSH host-key checks or altering unrelated network services.

## Benchmark and evidence contract

Record workload hashes, model tag/full digest, quantization, context/output limits, generation settings, runtime version, Mac hardware/RAM, concurrency, topology and source revision. Report all runs, sample sizes, medians, p95 timings, throughput, retries, failures and memory measurements. Separate queue wait, inference and end-to-end completion; improvements must not simply move waiting into Ollama.

Speedup uses the tuned one-Mac baseline. Two processes sharing one Mac/Ollama instance establish coordination behavior, not multi-Mac performance. There is no predetermined speedup requirement. If additional Macs do not help, the report must explain the result and the product decision.

Each acceptance item needs a status, reproduction command, environment, expected/observed result and evidence artifact. Publish sanitized reports and synthetic fixtures; keep credentials and real datasets private. Resume-style figures such as 2.8x speedup or 99.9% success are illustrative possibilities, not current results or release guarantees.

## Current evidence and next execution

The MVP passed 16 Swift tests, real HTTP recovery checks and a transient-worker retry check. Local gemma2:2b inference succeeded, including a 12-input run split across two processes on one Mac. Physical multi-Mac operation, production usage and performance improvements remain unverified. See [README validation](README.md#validation-and-reproduction).

Sequence: P0 → P1 → P2 → P3, then profiling-driven P4 if justified and P5 release/pilot acceptance. The next implementation session must begin with P0 only after execution is resumed. GatePass work is deferred while BatchDock is prioritized.
