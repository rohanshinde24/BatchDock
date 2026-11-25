#!/usr/bin/env python3
"""Independent verification of controlled single-Mac artifacts; no P1 decision."""
from collections import Counter
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
from benchmark_dataset import load, oracle

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(root):
    plan = json.loads((root / 'plan.json').read_text())
    report = json.loads((root / 'report.json').read_text())
    for item in (plan, report):
        if item['label'] != 'single_host_emulation' or item['physical_hosts'] != 1 or item['p1_status'] != 'pending_real_hardware' or item['p1_verdict'] is not None or item['p1_thresholds_evaluated'] is not False or item['p2_started'] is not False:
            raise ValueError('Invalid single-Mac scope')
    if not report['validated'] or not report['owned_processes_stopped'] or not report['model_residency_restored'] or not report['pilot_valid']:
        raise ValueError('Incomplete experiment or cleanup')
    if sha(root / 'plan.json') != report['plan_sha256']:
        raise ValueError('Frozen plan changed')
    if any(sha(ROOT / name) != value for name, value in plan['hashes'].items()) or sha(ROOT / '.build/debug/batchdock') != plan['binary_sha256']:
        raise ValueError('Frozen source/workload/binary changed')
    records = load(ROOT / 'Fixtures/benchmark-v2.jsonl')
    if json.loads((root / 'dataset.json').read_text()) != records:
        raise ValueError('Copied dataset changed')
    if json.loads((root / 'manifest.json').read_text()) != plan['manifest']:
        raise ValueError('Manifest changed')
    keys = ('phase', 'regime', 'engine', 'slots', 'repetition', 'observer', 'records')
    signature = lambda x: tuple(x[k] for k in keys)
    if Counter(map(signature, plan['schedule'])) != Counter(map(signature, report['runs'])):
        raise ValueError('Missing, duplicate or extra runs')
    if len({r['folder'] for r in report['runs']}) != len(report['runs']):
        raise ValueError('Reused result directory')
    # Validate prescribed matrix independently of the recorded schedule.
    repetitions = plan['repetitions']
    required = []
    for rep in range(repetitions):
        for engine in ('direct', 'batchdock'):
            for slots in (1, 2):
                required.append(('baseline', 'model_resident', engine, slots, rep, False, 200))
                required.append(('cold_start', 'model_unloaded', engine, slots, rep, False, 12))
        for observed in (False, True):
            required.append(('profile', 'model_resident', 'batchdock', 1, rep, observed, 60))
    if Counter(map(signature, plan['schedule'])) != Counter(required):
        raise ValueError('Protocol matrix mismatch')
    for run in report['runs']:
        folder = root / run['folder']
        if not folder.resolve().is_relative_to(root.resolve()):
            raise ValueError('Result path escaped artifact directory')
        selected = records[:run['records']]
        inputs = [json.loads(line) for line in (folder / 'inputs.jsonl').read_text().splitlines()]
        if inputs != [{'id': r['id'], 'prompt': r['prompt']} for r in selected]:
            raise ValueError('Run workload changed')
        rows = json.loads((folder / 'results.json').read_text())
        if [r['id'] for r in rows] != [r['id'] for r in selected] or not run['outputs_valid'] or run['valid_outputs'] != len(selected):
            raise ValueError('Incomplete valid output set')
        for row, source in zip(rows, selected):
            if row.get('state') != 'succeeded' or not isinstance(row.get('output'), str) or row['output'].strip() != oracle(source):
                raise ValueError('Exact extraction failed')
            if run['engine'] == 'direct' and (row.get('done') is not True or row.get('done_reason') != 'stop'):
                raise ValueError('Incomplete direct generation')
        if run['engine'] == 'batchdock':
            observations = [json.loads(p.read_text()) for i in range(run['slots'])
                            for p in (folder / f'worker-{i}').glob('workers/*/generation-*.json')]
            by_attempt = {o['attempt_id']: o for o in observations}
            if len(observations) != len(rows) or set(by_attempt) != {r['accepted_attempt_id'] for r in rows}:
                raise ValueError('Missing accepted observations')
            for row in rows:
                o = by_attempt[row['accepted_attempt_id']]
                if o['identity']['job_id'] != row['id'] or o['result']['done'] is not True or o['result'].get('finish_reason') != 'stop' or o['result']['output'] != row['output'] or o['config_hash'] != row['config_hash']:
                    raise ValueError('Accepted observation mismatch')
            with sqlite3.connect('file:' + str(folder / 'coordinator/queue.sqlite') + '?mode=ro', uri=True) as db:
                actual = dict(db.execute('SELECT w.name,count(*) FROM results r JOIN attempts a ON a.id=r.attempt_id JOIN workers w ON w.id=a.worker_id GROUP BY w.name'))
            if actual != run['worker_participation'] or len(actual) != run['slots'] or sum(actual.values()) != len(rows):
                raise ValueError('Participation mismatch')
        if bool(run['residency_before']['models']) != (run['regime'] == 'model_resident'):
            raise ValueError('Residency control mismatch')
        events = json.loads((folder / 'events.json').read_text())
        if run['observer']:
            generated = [e for e in events if e['kind'] == 'runtime' and e['path'] == '/api/generate']
            if len(generated) != len(rows) or any(e.get('done') is not True or e.get('done_reason') != 'stop' for e in generated):
                raise ValueError('Profile generation coverage mismatch')
            module_spec = importlib.util.spec_from_file_location('controlled', ROOT / 'scripts/benchmark-single-mac.py')
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
            calculated = module.stage_summary(events, run['clock_start'], run['clock_end'])
            if calculated != run['stages']['external_profile']:
                raise ValueError('Derived profile does not match events')
        elif events:
            raise ValueError('Unexpected instrumentation in unobserved run')
    return {'label': 'single_host_emulation', 'artifacts_verified': True,
            'runs': len(report['runs']), 'checked_outputs': sum(r['records'] for r in report['runs']),
            'p1_status': 'pending_real_hardware', 'p1_verdict': None,
            'plan_sha256': report['plan_sha256'], 'verifier_sha256': sha(Path(__file__))}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    result = verify(args.directory.resolve())
    target = args.directory / 'verification.json'
    target.write_text(json.dumps(result, indent=2) + '\n')
    target.chmod(0o600)
    print(json.dumps(result, indent=2))
