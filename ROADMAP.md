# Production roadmap

**Planning only: do not start implementation, services, workloads or deployment until execution is explicitly resumed.** BatchDock is prioritized; GatePass work is deferred. All gates below remain unaccepted. These are proposed requirements, not delivered production capabilities.

Scope remains a small trusted team, one coordinator/local SQLite and enrolled Mac workers. Users select installed models; strict mode pins model digest, runtime version and generation settings. Whole prompts execute on individual workers. High availability, public workers, tensor parallelism and training remain out of scope.

## Milestones in execution order

| Gate | Deliverable | Acceptance evidence |
| --- | --- | --- |
| P0 — Baseline/contracts | Reproduce checks and establish runtime/output semantics | Fresh-checkout build/tests; versioned reports; controlled natural-stop, output-limit, oversized-input and context-boundary probes. Independently validated short benchmark outputs; ambiguous truncation cannot count as useful throughput. |
| P1 — Early two-Mac feasibility | Throughput-only experiment before substantial hardening | Two physical Macs process at least 200 validated short inputs with both receiving work. Compare each Mac alone, a tuned two-Mac script, tuned single-Mac BatchDock and two-Mac BatchDock at equivalent concurrency; at least three runs/configuration, separating cold/warm starts. Record a go/narrow/stop decision. |
| P2 — Correctness and worker lifecycle | Resolve known execution, retry, recovery and claim-selection gaps | Targeted regressions for incomplete output, overflow policy, repeated503s, runtime restart, long transport outages/spool replay, model mismatch and bounded task versus infrastructure budgets. Worker resumes supported transient failures automatically. Claim selection no longer materializes every queued prompt. |
| P3 — Layered faults/physical recovery | Fast invariant checker plus focused real failures | Seeded in-process store/reference-model checks with fake clock; roughly 5–8 coverage-driven HTTP/process scenario groups; physical two-Mac disconnect/reconnect, worker kill and coordinator restart. No duplicate accepted results, stale successful writes or lost acknowledged results under tested process-crash conditions. |
| P4 — Bounded operability | Proportionate release, restore and access procedures | Isolated restore verifies at least 100 backed-up accepted outputs plus pending work; upgrade/rollback, clean installation, credential boundaries and diagnostic procedures pass. Explicit durability tiers and host resource limits. |
| P5 — Optional resource optimization | Profile before changing admission/scheduling | Instruments/telemetry identify a bottleneck; repeated before/after measurements show a declared improvement within memory and latency guardrails. Preserve P3 invariants under load. Can defer for a fixed-slot release. |
| P6 — Pilot/release acceptance | Independent reuse and supported release | Two independent collaborators, two datasets, three completed batches and one repeat user. Record setup/help/rejection, validate supported extraction/output contracts and complete release smoke/installation checks. |

Sequence: **P0 → P1 feasibility/decision → P2 fixes → P3 validation → P4 operability → P6 pilots**. P5 is optional after profiling. Fix only a minimal execution-contract blocker before P1 if needed to make its outputs valid. A missing second Mac leaves P1 blocked; multiple processes on one Mac do not satisfy it.

## Feasibility hypotheses and transport

Provisional diagnostic thresholds: the tuned two-Mac script reaches at least 80% of comparable combined single-Mac throughput; BatchDock makespan stays within 10% of that script at equivalent concurrency. These are hypotheses, not universal release laws. Investigate hardware/workload effects and explain any threshold revision before rerunning. Passing demonstrates feasibility and overhead, not unique product value. Continuing after a poor result needs a concrete optimization hypothesis or user evidence for recovery/accounting value.

Keep loopback binding and SSH host-key validation. New collaborators need restricted forwarding-only access or another reviewed transport/access design, not unrestricted shell access solely for a worker token. An owner-operated experiment with existing broad SSH trust is not proof of collaborator onboarding. Tailscale is an option to evaluate; it does not automatically satisfy application transport/authorization policy. No access or host-configuration changes are authorized by this plan.

## Execution contract and accepted corrections

- Decode and preserve finish reason and bounded partial output. Extraction defaults to rejecting length-limited/incomplete generation. Natural stop does not prove semantic correctness; define JSON/schema validation for the pilot workload. Chat endpoint support follows demonstrated need.
- Verify overflow behavior for supported runtime versions. Prompt/output token-count sums alone cannot prove that original input was not truncated. Unknown behavior must stay explicit.
- Separate task-error budgets from infrastructure outages and worker incompatibility. Persist bounded recovery/backoff policy with an overall deadline or visible blocked state; avoid both rapid exhaustion and invisible endless retry.
- Runtime discovery and completion reconciliation belong in a recoverable lifecycle. A supported runtime restart or long transport outage must not require manual worker restart or deleting a completion spool.
- Preserve strict identity; a mismatched worker leaves work available for compatible workers. Record observed versus expected identity. Before/after model checks detect some accidental changes; they do not attest to executed weights.
- Bound/index compatible claim selection. On a declared 10,000-job fixture it must not load all queued prompts; measure heartbeat interference and require p99 healthy heartbeat acknowledgment below half the configured lease duration.
- Provide worker freshness/inspection and specific blocked reasons, or label health unknown. Retain the existing fencing fields, completion spools, atomic results and failure receipts.

## Fault and measurement design

Use the store-level checker for most combinatorial exploration. A starting reproducibility budget is 1,000 seeded scenarios across 10,000 synthetic jobs; report transition/fault coverage, seeds and minimized failures. Counts alone do not establish quality. There is no per-class quota and no requirement for 1,000 HTTP scenarios.

Focused process/network cases cover dropped post-commit responses, crashes, runtime unavailability, real disconnect/reconnect, spool replay, cancellation and revocation. Inject storage failures in isolation without changing host clocks, filling disks or disrupting unrelated networking. Every input must have one ordered terminal export row, including explicit failures; never fabricate success.

For healthy replacement-worker tests, target p95 reassignment ≤ lease duration + polling interval + 2 seconds, measured from acknowledged kill injection to replacement claim acknowledgment. Measure recovery after network/runtime restoration and inference completion separately. Publish sample counts and outliers; these are experiment targets, not availability SLAs.

Record revision/lock/workload hashes, model/digest/quantization/runtime/options, hardware/RAM, topology, concurrency, stage durations and exact commands. Correlated monotonic harness events can establish initial timings; P0 does not require a database timestamp migration. Separate simulated inference, actual local inference and physical multi-Mac evidence. Never compare process-local monotonic instants across restarts or hosts.

P5 provisional targets: ≥20% lower p95 queue wait without >5% throughput regression, or ≥15% higher throughput inside declared memory budgets without >5% p95 end-to-end latency regression. Report all runs, thermal context, instrumentation overhead and failed hypotheses. Waiting must not merely move into Ollama's queue. Multiple worker identities sharing one runtime share one host budget; a slot-count schema change alone is insufficient resource control.

## Durability, pilots and current evidence

Separate process-crash, consistent-backup and power-loss guarantees. Use a stopped coordinator or consistent SQLite backup API, never just a live WAL-mode main file. State the backup recovery point; later unbacked work is outside it. Review macOS database/spool flush behavior before any power-loss claim; FULL synchronous mode alone is not proof of every hardware guarantee.

Pilot onboarding targets ≤10 minutes from prerequisites-ready to first valid output, with installation/access provisioning and assistance recorded separately. Report all outcomes with explicit denominators. Small pilots do not establish 99.9% availability or success; illustrative resume numbers are not observed results.

Historical MVP checks passed 16 Swift tests, real HTTP recovery and a transient-worker retry case. Local gemma2:2b inference and a 12-input/two-process emulation succeeded. Source review also identified the unresolved P2 gaps above. These passing cases do not establish general self-recovery, truncation safety, physical multi-Mac performance or production readiness. See [README validation](README.md#validation-and-reproduction).

The earlier 20–40 focused-hour estimate describes a bounded technical beta, not this full production contract and pilot calendar. Re-estimate after P0 and P1. Hardware access and failure discoveries affect engineering effort; real reuse requires collaborator time. Keep private data/credentials out of public reports.
