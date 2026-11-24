# Single-host benchmark emulation

**This is single-host emulation. Physical P1 is pending real hardware; P2 has not started.** The harness exercises separate worker processes, ordered output validation, bounded reference execution and restricted SSH. It does not measure distributed capacity or satisfy the two-Mac gate.

## Topology and scope

```text
One physical Mac
  direct reference: one or two endpoint-bound slots
  BatchDock coordinator + SQLite (loopback)
    worker A: separate credential/state/spool -> coordinator directly
    worker B: separate credential/state/spool -> SSH tunnel -> coordinator
  isolated unprivileged sshd (loopback only)
  fixture mode: two simulated Ollama HTTP endpoints
  real mode: both workers share the existing local Ollama runtime
```

Workers are separate processes with fresh enrollment credentials and state for each run. They share CPU, memory bandwidth, thermals, storage, power and the OS. Real mode also shares one Ollama scheduler/model cache. It cannot reproduce independent Apple Silicon GPUs, independent runtime capacity, a physical network, host sleep/failure isolation or collaborator onboarding.

The runner uses the existing CLI without changing application code. Local endpoint/SSH plumbing is prepared; remote orchestration, hardware qualification, equivalent-concurrency tuning and the cold/warm physical protocol remain pending.

## Reproduce

Requires macOS, Python 3.9+, the system OpenSSH tools, and a built `.build/debug/batchdock`. Run as your ordinary user, never with sudo. Use a checkout/output path without spaces. Each output directory must be new and under this checkout's ignored `.batchdock/`.

```sh
swift build --jobs 3
python3 scripts/test-benchmark-local.py
python3 scripts/benchmark-local.py --output-dir .batchdock/local-fixture --records 200 --repetitions 3
python3 scripts/verify-local-benchmark.py .batchdock/local-fixture
python3 scripts/test-verify-local-benchmark.py .batchdock/local-fixture
```

Optional real inference uses the **already installed** `gemma2:2b` at `127.0.0.1:11434`. It never starts Ollama, installs models or changes its settings. Do not run competing inference while collecting a report.

```sh
python3 scripts/benchmark-local.py --runtime ollama --output-dir .batchdock/local-real --records 200 --repetitions 3
python3 scripts/verify-local-benchmark.py .batchdock/local-real
```

Default inference is simulated. CI uses 200 records and one repetition per configuration with simulated inference; it needs no Ollama/model installation. A CI run is transport/output plumbing evidence only.

## Frozen local protocol

Before inference, `plan.json` freezes source/binary/lock hashes, hardware, model/runtime/options, workload/expected-answer hashes, seed and randomized schedule. There are four configurations: direct one slot, direct two slots, BatchDock one worker and BatchDock two workers with one over SSH. Each direct slot stays attached to one endpoint. The reference is bounded, not a tuned physical two-Mac baseline.

The 200 distinct input IDs cycle **three existing fixture prompts**, with exact expected products: kettle, desk lamp, hiking backpack. This deliberately small synthetic corpus validates wiring and output accounting. Repeated prompts/cache behavior make it unsuitable for a representative throughput claim. Schedule seed is 42; full local runs use three repetitions/configuration (12 runs, 2,400 outputs per inference mode).

Acceptance requires every ID exactly once in input order, successful state and the exact expected answer after case/whitespace normalization. Extra words fail. Direct responses must explicitly stop. Accepted worker attempts must match private `done=true` / `finish_reason=stop` observations. Both enrolled workers must receive accepted work in every two-worker run. No automatic exclusions or threshold revisions are performed.

Elapsed time uses one controller's monotonic clock, from submit/dispatch through received, validated output. BatchDock includes worker startup and excludes idle-exit grace. Real mode does not unload models or control cold starts. These times are diagnostic, not a valid P1 overhead/scaling comparison. No speedup or go/narrow/stop result is calculated. Reports require `single_host_emulation`, one physical host, `p1_verdict: null` and `p1_thresholds_evaluated: false`.

The separate verifier rechecks hashes, the complete configuration matrix, all outputs, accepted-attempt observations and worker participation against the coordinator database. It also checks recorded SSH restrictions and process cleanup. Keep the exact harness source and tested binary to verify an old report; rebuilding changes the binary hash. Hashes detect accidental artifact drift, not malicious evidence fabrication.

## Restricted SSH

The runner generates private temporary host/client/untrusted keys, pins the generated host public key in a private `known_hosts`, and starts an **unprivileged dedicated sshd on 127.0.0.1**. It does not use or change system SSH configuration, Remote Login, accounts, `~/.ssh`, or the SSH agent. Password and interactive authentication are disabled.

The effective config is checked using `sshd -T`: only local TCP forwarding to the coordinator's exact loopback address/port is allowed. `MaxSessions 0` denies shell/subsystem sessions; PTY, agent, X11, tunnel devices, user rc, remote TCP and Unix-socket forwarding are disabled. Positive forwarding is verified before worker B uses it. Negative checks require denial of shell commands, SFTP, PTY sessions, another destination, reverse TCP, reverse Unix socket, an untrusted client key and an incorrect host key. See upstream [sshd](https://man.openbsd.org/sshd.8) and [sshd_config](https://man.openbsd.org/sshd_config.5) semantics.

On the validated Mac, unprivileged sshd emitted nonfatal macOS BSM `setaudit_addr: Operation not permitted` diagnostics. Authentication/forwarding and all negative checks still passed; the server log is not diagnostic-free. This local daemon proves the tested policy only. Real remote access still needs explicit host preparation and validation; this is not a general deployment/access guide.

## Artifacts and cleanup

The runner stops its owned coordinators, workers, tunnels, SSH daemon and simulated runtimes on normal completion or handled failure. It leaves pre-existing Ollama running. The report records cleanup. A forced kill of the harness cannot guarantee cleanup; inspect only processes associated with its private run directory before manual cleanup.

Raw plans, outputs, database, logs, worker credentials, keys and observations stay ignored under `.batchdock/`. Do not publish raw SSH checks: key comments/logs can identify the local account or hostname. The committed sanitized summary contains only counts, generic environment/model identity, hashes and restriction outcomes.

See [local-emulation summary](local-emulation-summary.json) for measured validation evidence. These reports leave physical P1 pending and do not begin P2.
