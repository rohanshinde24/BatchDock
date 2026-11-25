#!/usr/bin/env python3
"""Separate wall-stack diagnostic of an owned worker; never part of baseline timing."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import time
import uuid

spec = importlib.util.spec_from_file_location('baseline', Path(__file__).with_name('benchmark-single-mac.py'))
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--reference-plan', type=Path, required=True)
    args = parser.parse_args()
    root = args.output_dir.resolve()
    if not root.is_relative_to((baseline.ROOT / '.batchdock').resolve()):
        parser.error('Use a new ignored .batchdock directory')
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    reference = json.loads(args.reference_plan.read_text())
    if baseline.sha(baseline.BINARY) != reference['binary_sha256']:
        raise ValueError('Binary differs from baseline')
    records = baseline.load(baseline.ROOT / 'Fixtures/benchmark-v2.jsonl')[:60]
    (root / 'inputs.jsonl').write_bytes(b''.join(baseline.local.encoded({'id': r['id'], 'prompt': r['prompt']}) + b'\n' for r in records))
    plan = {'label': 'single_host_emulation', 'purpose': 'separate_native_wall_stack_diagnostic',
            'physical_hosts': 1, 'p1_status': 'pending_real_hardware', 'p1_verdict': None,
            'p2_started': False, 'records': 60, 'sample_duration_seconds': 8, 'sample_interval_ms': 10,
            'sample_targets': ['worker', 'coordinator'],
            'reference_plan_sha256': baseline.sha(args.reference_plan),
            'script_sha256': baseline.sha(Path(__file__)), 'binary_sha256': reference['binary_sha256'],
            'workload_sha256': baseline.sha(root / 'inputs.jsonl'), 'manifest': reference['manifest']}
    baseline.save(root / 'plan.json', plan)
    runtime, model = 'http://127.0.0.1:11434', reference['manifest']['model']['tag']
    initial = baseline.request(runtime, '/api/ps')['models']
    if any(m['name'] != model for m in initial):
        raise RuntimeError('Other loaded models; refusing diagnostic')
    processes = baseline.local.Processes(root)
    result = {'label': plan['label'], 'p1_status': plan['p1_status'], 'p1_verdict': None, 'p2_started': False}
    controlled = False

    def cli(*a):
        return subprocess.check_output([str(baseline.BINARY), *map(str, a)], cwd=baseline.ROOT, text=True, timeout=180)

    try:
        cli('doctor', '--model', model, '--write-manifest', root / 'manifest.json', '--max-output-tokens', '32')
        if json.loads((root / 'manifest.json').read_text()) != reference['manifest']:
            raise ValueError('Runtime/model/configuration changed')
        controlled = True
        baseline.request(runtime, '/api/generate', {'model': model, 'keep_alive': 0, 'stream': False})
        if baseline.request(runtime, '/api/ps')['models']:
            raise ValueError('Unload not observed')
        baseline.request(runtime, '/api/generate', {'model': model, 'stream': False, 'prompt': 'Return only READY.',
                         'system': reference['manifest']['generation']['system'], 'truncate': False, 'shift': False,
                         'options': reference['manifest']['generation']['options']})
        state = root / 'coordinator'
        cli('init', '--state-dir', state)
        port = baseline.local.port()
        coordinator_origin = f'http://127.0.0.1:{port}'
        coordinator = processes.start([baseline.BINARY, 'serve', '--state-dir', state, '--port', port], 'coordinator')
        baseline.local.ready(coordinator_origin, coordinator)
        token = root / 'worker.token'
        cli('worker', 'add', '--state-dir', state, '--coordinator', coordinator_origin,
            '--name', 'native-profile', '--write-token', token)
        batch = json.loads(cli('submit', '--state-dir', state, '--coordinator', coordinator_origin,
                              '--manifest', root / 'manifest.json', '--input', root / 'inputs.jsonl',
                              '--request-id', uuid.uuid4()))['batch_id']
        worker = processes.start([baseline.BINARY, 'worker', 'run', '--coordinator', coordinator_origin,
                                  '--state-dir', root / 'worker-0', '--token-file', token,
                                  '--stop-when-idle', '--idle-grace-seconds', '1'], 'worker')
        deadline = time.monotonic() + 180
        while not list((root / 'worker-0').glob('workers/*/generation-*.json')):
            if worker.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Worker did not produce first observation')
            time.sleep(.05)
        samples = {}
        for name, process in [('worker', worker), ('coordinator', coordinator)]:
            sampled = subprocess.run(['/usr/bin/sample', str(process.pid), '8', '10', '-mayDie', '-file', str(root / (name + '.sample.txt'))],
                                     capture_output=True, text=True, timeout=45)
            (root / (name + '-sample-command.log')).write_text(sampled.stdout + sampled.stderr)
            samples[name] = {'returncode': sampled.returncode,
                             'available': sampled.returncode == 0 and (root / (name + '.sample.txt')).exists()}
        result['samples'] = samples
        owner = (state / 'owner-token').read_text().strip()
        while baseline.request(coordinator_origin, f'/v1/batches/{batch}', token=owner)['state'] == 'running':
            if time.monotonic() > deadline:
                raise RuntimeError('Diagnostic exceeded deadline')
            time.sleep(.25)
        rows = [json.loads(x) for x in cli('export', '--state-dir', state, '--coordinator', coordinator_origin, batch).splitlines()]
        baseline.rows_valid(rows, records)
        baseline.observations_valid(root, rows, 1)
        baseline.save(root / 'results.json', rows)
        result['validated_outputs'] = len(rows)
        result['sample_available'] = all(s['available'] for s in samples.values())
        result['interpretation'] = 'Wall-stack samples can include blocked threads; not CPU percentages or causal disk/DB timing. Separate from all baseline/profile calibration runs.'
    finally:
        processes.close()
        result['owned_processes_stopped'] = all(p.poll() is not None for p, _ in processes.items)
        if controlled:
            baseline.request(runtime, '/api/generate', {'model': model, 'stream': False, 'keep_alive': '5m' if initial else 0})
            result['model_residency_restored'] = bool(baseline.request(runtime, '/api/ps')['models']) == bool(initial)
        baseline.save(root / 'report.json', result)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
