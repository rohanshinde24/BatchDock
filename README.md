# BatchDock

Run a recoverable JSONL inference batch across participating Macs using their local Ollama models. A Swift coordinator tracks durable work, expiring leases, and one accepted result per input.

## Quick start

Requires Swift 6.2+, macOS 14+, and a running Ollama instance with an installed model.

```sh
swift build
swift run batchdock init
swift run batchdock doctor --model gemma2:2b --write-manifest .batchdock/manifest.json --max-output-tokens 32
```

Start `swift run batchdock serve` in another terminal. Then enroll a worker, submit the sample inputs, and run it:

```sh
swift run batchdock worker add --name mac-a --write-token .batchdock/mac-a.token
swift run batchdock submit --manifest .batchdock/manifest.json --input Fixtures/prompts.jsonl
swift run batchdock worker run --token-file .batchdock/mac-a.token --stop-when-idle
swift run batchdock status <batch-id>
swift run batchdock export <batch-id> --output .batchdock/results.jsonl --require-success
```

Use an exact installed model tag. Repeating an identical submission reuses its saved request identity; pass a new `--request-id` to intentionally create a new run.

Each worker has its own credential. Remote workers reach the loopback coordinator through an SSH forward; models stay on each worker. Each job runs entirely on one Mac.

```sh
swift test
python3 scripts/acceptance.py
```

This MVP uses a single coordinator. Multiple-machine speedup and live pilot usage must be measured on the participating hardware.
