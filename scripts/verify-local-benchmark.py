#!/usr/bin/env python3
"""Verify complete private local-emulation artifacts. Never produces a P1 verdict."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = {'direct_one_slot', 'direct_two_slots', 'batchdock_one_worker', 'batchdock_two_workers_ssh'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def verify(root):
    plan_bytes = (root / 'plan.json').read_bytes()
    plan, report = json.loads(plan_bytes), json.loads((root / 'report.json').read_text())
    if plan.get('label') != 'single_host_emulation' or report.get('label') != 'single_host_emulation':
        raise ValueError('Missing emulation labels')
    if plan.get('physical_hosts') != 1 or report.get('physical_hosts') != 1:
        raise ValueError('This verifier only accepts single-host evidence')
    if report.get('p1_status') != 'pending_real_hardware' or report.get('p1_verdict') is not None or plan.get('p1_verdict') is not None or report.get('p1_thresholds_evaluated') is not False or plan['physical_p1_criteria'].get('evaluated_here') is not False:
        raise ValueError('Single-host evidence cannot evaluate physical P1')
    if report.get('validated') is not True or report.get('owned_processes_stopped') is not True:
        raise ValueError('Incomplete validation/cleanup')
    checks = report['ssh_checks']
    if checks.get('allowed_forward') is not True or checks.get('strict_host_key_pinned') is not True:
        raise ValueError('Positive SSH checks missing')
    for name in ('shell', 'sftp', 'pty', 'other_destination', 'reverse_tcp', 'reverse_socket', 'untrusted_key', 'wrong_host_key'):
        if checks[name].get('denied') is not True or checks[name].get('returncode') == 0:
            raise ValueError('SSH restriction missing: ' + name)
    if report.get('plan_sha256') != sha(plan_bytes):
        raise ValueError('Frozen plan changed')
    if sha((root / 'inputs.jsonl').read_bytes()) != plan['workload_sha256']:
        raise ValueError('Frozen inputs changed')
    expected = json.loads((root / 'expected.json').read_text())
    if sha(canonical(expected)) != plan['expected_sha256']:
        raise ValueError('Frozen expected answers changed')
    if json.loads((root / 'manifest.json').read_text()) != plan['model']:
        raise ValueError('Frozen manifest changed')
    if sha((ROOT / 'scripts/benchmark-local.py').read_bytes()) != plan['harness_sha256']:
        raise ValueError('Use the exact harness source captured by the plan')
    if sha((ROOT / '.build/debug/batchdock').read_bytes()) != plan['binary_sha256']:
        raise ValueError('Use the exact tested binary')
    if sha((ROOT / 'Package.resolved').read_bytes()) != plan['lock_sha256']:
        raise ValueError('Dependency lock changed')
    schedule = [(int(rep), name) for rep, name in plan['schedule']]
    required = Counter((rep, name) for rep in range(plan['repetitions']) for name in CONFIGS)
    if Counter(schedule) != required or Counter((r['repetition'], r['configuration']) for r in report['runs']) != required:
        raise ValueError('Missing, duplicated or unexpected configuration runs')
    for run in report['runs']:
        if run['label'] != 'single_host_emulation' or run.get('outputs_valid') is not True or run['accepted_rows'] != plan['records']:
            raise ValueError('Run is not validated single-host evidence')
        rows = json.loads((root / f"{run['repetition']}-{run['configuration']}-results.json").read_text())
        if [row['id'] for row in rows] != list(expected) or len(rows) != plan['records']:
            raise ValueError('Output cardinality/order failed')
        if any(row.get('state') != 'succeeded' or not isinstance(row.get('output'), str) or ' '.join(row['output'].split()).casefold() != expected[row['id']] for row in rows):
            raise ValueError('Output answer validation failed')
        if run['configuration'].startswith('batchdock'):
            counts = run['worker_participation']
            worker_count = 2 if run['configuration'] == 'batchdock_two_workers_ssh' else 1
            if len(counts) != worker_count or any(v <= 0 for v in counts.values()) or sum(counts.values()) != plan['records']:
                raise ValueError('Worker participation failed')
            name = f"{run['repetition']}-{run['configuration']}"
            observations = [json.loads(p.read_text()) for index in range(worker_count)
                            for p in (root / f'{name}-state-{index}').glob('workers/*/generation-*.json')]
            by_attempt = {o['attempt_id']: o for o in observations}
            if len(observations) != len(rows) or set(by_attempt) != {r['accepted_attempt_id'] for r in rows}:
                raise ValueError('Accepted attempt observations missing or duplicated')
            for row in rows:
                observation = by_attempt[row['accepted_attempt_id']]
                result = observation['result']
                if observation['identity']['job_id'] != row['id'] or result.get('done') is not True or result.get('finish_reason') != 'stop' or result.get('output') != row['output'] or observation['config_hash'] != row['config_hash']:
                    raise ValueError('Accepted output and stop observation disagree')
            batches = {o['identity']['batch_id'] for o in observations}
            if len(batches) != 1:
                raise ValueError('Run mixes batch identities')
            with sqlite3.connect('file:' + str(root / 'coordinator/queue.sqlite') + '?mode=ro', uri=True) as db:
                actual = dict(db.execute('SELECT w.name,count(*) FROM results r JOIN attempts a ON a.id=r.attempt_id JOIN workers w ON w.id=a.worker_id WHERE r.batch_id=? GROUP BY w.name', (next(iter(batches)),)))
            if actual != counts:
                raise ValueError('Worker participation disagrees with coordinator database')
    return {'label': 'single_host_emulation', 'artifacts_verified': True, 'runs': len(schedule),
            'checked_outputs': len(schedule) * plan['records'], 'p1_status': 'pending_real_hardware',
            'p1_verdict': None, 'plan_sha256': sha(plan_bytes),
            'verifier_sha256': sha(Path(__file__).read_bytes())}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    result = verify(args.directory.resolve())
    target = args.directory / 'verification.json'
    target.write_text(json.dumps(result, indent=2) + '\n')
    target.chmod(0o600)
    print(json.dumps(result, indent=2))
