#!/usr/bin/env python3
"""Create a sanitized descriptive summary only after independent artifact verification."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import statistics

spec = importlib.util.spec_from_file_location('verifier', Path(__file__).with_name('verify-single-mac.py'))
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def metrics(rows, engine):
    names = {'total_duration': 'total_duration_ns', 'load_duration': 'load_duration_ns',
             'eval_duration': 'eval_duration_ns', 'prompt_eval_count': 'prompt_tokens', 'eval_count': 'output_tokens'}
    result = {}
    for key, batch_key in names.items():
        values = [r['metrics'].get(key if engine == 'direct' else batch_key) for r in rows]
        if all(v is not None for v in values):
            scale = 1e9 if 'duration' in key else 1
            result[key + ('_seconds' if 'duration' in key else '')] = {
                'sum': sum(values) / scale, 'median': statistics.median(values) / scale,
                'max': max(values) / scale}
    return result


def summarize(root):
    verified = verifier.verify(root)
    plan = json.loads((root / 'plan.json').read_text())
    report = json.loads((root / 'report.json').read_text())
    groups, profiles, perturbation = {}, [], []
    for run in report['runs']:
        key = (run['phase'], run['engine'], run['slots'], run['regime'], run['observer'])
        groups.setdefault(key, []).append(run)
        if run['observer']:
            events = json.loads((root / run['folder'] / 'events.json').read_text())
            profiles.append({'repetition': run['repetition'], 'elapsed_seconds': run['elapsed_seconds'],
                             'submission_seconds': run['stages']['submission_seconds'],
                             'export_seconds': run['stages']['export_seconds'],
                             **run['stages']['external_profile'],
                             'runtime_reported_generation_seconds': sum(e.get('metrics', {}).get('total_duration', 0) for e in events if e['path'] == '/api/generate') / 1e9})
    for rep in range(plan['repetitions']):
        pair = {r['observer']: r for r in report['runs'] if r['phase'] == 'profile' and r['repetition'] == rep}
        perturbation.append({'repetition': rep, 'off_seconds': pair[False]['elapsed_seconds'],
                             'on_seconds': pair[True]['elapsed_seconds'],
                             'on_minus_off_seconds': pair[True]['elapsed_seconds'] - pair[False]['elapsed_seconds']})
    configs = []
    for (phase, engine, slots, regime, observed), runs in groups.items():
        times = [r['elapsed_seconds'] for r in runs]
        configs.append({'phase': phase, 'engine': engine, 'slots': slots, 'regime': regime,
                        'observer': observed, 'records_per_run': runs[0]['records'],
                        'run_seconds': times, 'median_seconds': statistics.median(times),
                        'min_seconds': min(times), 'max_seconds': max(times),
                        'median_valid_inputs_per_second': statistics.median(r['valid_outputs'] / r['elapsed_seconds'] for r in runs),
                        'runtime_metrics_by_repetition': [metrics(json.loads((root / r['folder'] / 'results.json').read_text()), engine) for r in runs],
                        'worker_accepted_counts': [sorted(r['worker_participation'].values()) if r['worker_participation'] else None for r in runs]})
    free, thermal_available = [], True
    for run in report['runs']:
        for snapshot in (run['host_before'], run['host_after']):
            match = re.search(r'System-wide memory free percentage: (\d+)%', snapshot['memory']['text'])
            if match:
                free.append(int(match[1]))
            thermal_available &= snapshot['thermal']['returncode'] == 0
    return {'label': 'single_host_emulation', 'experiment': plan['experiment'],
            'physical_hosts': 1, 'p1_status': 'pending_real_hardware', 'p1_verdict': None,
            'p1_thresholds_evaluated': False, 'p2_started': False, 'runtime_kind': plan['runtime_kind'],
            'source_revision': plan['source_revision'], 'source_dirty': plan['source_dirty'],
            'summary_generator_sha256': verifier.sha(Path(__file__)),
            'hashes': plan['hashes'], 'binary_sha256': plan['binary_sha256'],
            'hardware': plan['hardware'], 'manifest': plan['manifest'],
            'verification': verified, 'configurations': configs, 'profiles': profiles,
            'observer_pairs': perturbation, 'owned_processes_stopped': report['owned_processes_stopped'],
            'model_residency_restored': report['model_residency_restored'],
            'host_context': {'system_memory_free_percentage_snapshot_range': [min(free), max(free)] if free else None,
                             'thermal_snapshot_commands_succeeded': thermal_available,
                             'continuous_gpu_thermal_or_peak_memory_measurement': False},
            'limitations': ['Synthetic extraction corpus; not a customer/model-quality benchmark.',
                            'One shared Mac/runtime; offered slots do not establish physical parallelism.',
                            'Model-unloaded does not mean cold OS/filesystem cache; within-run prefix caching remains.',
                            'Startup probes use12 inputs; baseline batches use200. Do not compare makespans as equivalent.',
                            'Profile runs use60 inputs and external observers; calibration pairs are separate from baseline.',
                            'Outside observed HTTP is not a disk or SQLite attribution.',
                            'Runtime-reported durations can overlap across slots and are not additive wall-clock overhead.',
                            'Other apps/clients, power state and continuous GPU thermals are uncontrolled.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(summarize(args.directory.resolve()), indent=2) + '\n')
