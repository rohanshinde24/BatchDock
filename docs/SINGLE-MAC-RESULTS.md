# Controlled single-Mac results

**Single-host emulation. Physical P1 is pending real hardware; P2 has not started.**

The frozen experiment used commit `3bb6b86` from a clean working tree, the existing debug CLI, Ollama 0.33.3 and installed gemma2:2b. Hardware was Mac15,12 with 16 GiB RAM and macOS 26.3.1. Workload and source/binary/lock hashes, generation settings and schedule were fixed before measurement. No production Swift, schema, dependency or recovery behavior changed.

## What passed

- 200 distinct synthetic extraction records and answers, four target fields, three lengths and distracting fields; ground truth independently parsed from each source record.
- 30 runs / 2,904 exact validated outputs: 12 full resident-model baselines, 12 shorter model-unloaded startup probes and six separate calibration/profile runs. The 12-case preflight is excluded.
- Ordered IDs/cardinality, exact answers, explicit natural stop, matching accepted-attempt observations and participation by every enrolled worker.
- Independent artifact verification and six tamper rejections. Frozen plans and original results were preserved.
- Separate native diagnostic: 60 validated outputs, worker/coordinator wall-stack samples, owned-process cleanup and initial model residency restored. It is excluded from the 30-run timing matrix.

Reproduction and boundaries: [protocol](SINGLE-MAC-BASELINE.md). All measured runs and hashes: [sanitized baseline data](single-mac-v2-summary.json). Native evidence: [sanitized diagnostic](native-diagnostic-summary.json). Raw outputs, observations, resource snapshots, databases and stack samples remain private.

## Full resident-model baseline

End-to-end seconds per 200 records, including submission/worker startup, export and validation; enrollment/coordinator/model preparation and idle shutdown excluded.

| Configuration | Individual runs (s) | Median (s) | Range (s) |
| --- | --- | --- | --- |
| Direct, one slot | 220.11, 173.52, 220.53 | 220.11 | 173.52–220.53 |
| BatchDock, one worker | 172.37, 261.76, 223.21 | 223.21 | 172.37–261.76 |
| Direct, two slots | 189.22, 206.54, 248.73 | 206.54 | 189.22–248.73 |
| BatchDock, two workers | 215.65, 254.39, 248.18 | 248.18 | 215.65–254.39 |

The one-worker medians are close; the two-worker BatchDock median is slower than the direct two-slot median. Neither comparison establishes a precise causal overhead percentage: ranges overlap substantially, the host was shared and actual runtime parallelism was not configured or measured. Two workers are offered concurrency on one shared runtime, not extra hardware capacity. No physical P1 threshold or go/narrow/stop decision was evaluated.

The 12-record startup probes confirm model-unloaded preparation and preserve load-duration metrics. Per-run maximum runtime-reported load duration ranged from 0.817–1.706 seconds in startup probes versus 0.009–0.118 seconds in resident baselines. These are runtime counters, not external disk-I/O timings. Their shorter makespans are not comparable with the 200-record baselines. Model-unloaded means runtime residency, not a cold OS/filesystem cache. All probes remain in the JSON report.

## Where time went

External observers were enabled only for separate 60-record, one-worker profiles. Interval unions avoid double-counting overlap.

| Observed component | Time across each 60-record profile |
| --- | --- |
| Inference HTTP | 71.06 / 70.29 / 63.10 s; 95.36% / 95.43% / 96.75% of elapsed time |
| Coordinator claim + completion HTTP | 0.91 / 0.85 / 0.54 s |
| Runtime version + model-list HTTP | 0.66 / 0.68 / 0.44 s |
| Outside observed HTTP | 1.90 / 1.84 / 1.14 s |

Each profile recorded 60 inference calls, 60 completions, 61 claims and 182 version plus 182 model-list calls. The worker performs six metadata requests per job, plus startup/idle discovery. These checks are measurable but small here. Inference HTTP includes runtime scheduling/loading/prefill/generation and transport; it is not a GPU-compute measurement.

Unobserved time includes local observation/spool writes, client scheduling, startup, controller detection, export and validation. It cannot be assigned wholesale to SQLite or disk. Native samples showed worker CFNetwork/platform waits, occasional `LocalState.atomicWrite` publication activity and coordinator event-loop/kqueue waits. These sparse wall-stack samples include blocked threads; they establish neither CPU percentages nor a storage bottleneck.

Observer-on minus paired observer-off time was **−4.19 / −3.26 / −6.75 seconds**. This is not negative observer cost: connection behavior and ambient variation remain confounded. Do not subtract those differences from the baseline or present the instrumented proportions as exact native overhead shares. The consistent inference-dominated traces are diagnostic evidence for this one-worker workload, not an explanation of the two-worker gap.

## Limits and next boundary

System-load snapshots ranged from 3.17 to 36.22; unrelated system/virtualization CPU activity was observed without interrupting it. System-wide memory-free snapshots ranged from 26% to 66%. Thermal snapshot commands succeeded, but there was no continuous GPU-temperature or peak-memory measurement. The protocol controlled model residency/options/workload/order; it did not isolate the machine. All results are retained without post-hoc exclusions.

The useful conclusion is that the stronger healthy workflow validates and inference dominates the observed single-worker path. This dataset does not establish real-user usefulness, general model quality, recovery under faults, physical scaling or a production performance claim. A quieter matched rerun and two-worker stage traces would be needed to resolve the two-worker timing gap. Physical P1 still requires a second Mac and its separately frozen complete protocol; P2 remains untouched.

SSH CI uncovered a checker portability issue: newer OpenSSH prints mixed-case effective directive names. The corrected parser normalizes names only, keeps values/destinations exact and retains all eight negative checks. A fresh local 800-output simulated SSH run, source/archive regressions and tamper checks passed. This is benchmark tooling, not an application transport-policy change. Compiler macro-plugin shutdown diagnostics remain the documented [P0 toolchain caveat](P0.md); successful tests do not imply diagnostic-free logs.

[Final hosted CI](https://github.com/rohanshinde24/BatchDock/actions/runs/45661990816) passed the fresh 20-test Swift build, HTTP/worker regressions, policy/source tests, 800-output SSH harness and 968-output controlled harness with artifact/tamper checks. The earlier macro-shutdown diagnostic did not recur in this final run; the historical P0 caveat remains documented.
