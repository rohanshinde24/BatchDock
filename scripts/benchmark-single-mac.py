#!/usr/bin/env python3
"""Controlled single-Mac baseline and external HTTP profiling; never accepts P1."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import queue
import random
import statistics
import subprocess
import threading
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from benchmark_dataset import load, oracle

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / '.build/debug/batchdock'
spec = importlib.util.spec_from_file_location('local', ROOT / 'scripts/benchmark-local.py')
local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local)
LABEL = 'single_host_emulation'
METRICS = ('total_duration', 'load_duration', 'prompt_eval_duration', 'eval_duration', 'prompt_eval_count', 'eval_count')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    local.save(path, value)


def request(origin, path, body=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = Request(origin + path, data=None if body is None else local.encoded(body), headers=headers)
    with urlopen(req, timeout=180) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise RuntimeError('Oversized response')
        return json.loads(raw) if raw else None


def host_snapshot():
    result = {'load_average': list(os.getloadavg())}
    for name, command in [('thermal', ['pmset', '-g', 'therm']),
                          ('memory', ['memory_pressure', '-Q']), ('vm', ['vm_stat'])]:
        p = subprocess.run(command, capture_output=True, text=True, timeout=10)
        result[name] = {'returncode': p.returncode, 'text': p.stdout.strip(), 'stderr': p.stderr.strip()}
    return result


def rows_valid(rows, records):
    if [r['id'] for r in rows] != [r['id'] for r in records]:
        raise ValueError('Missing, duplicated or reordered outputs')
    for result, record in zip(rows, records):
        if result.get('state') != 'succeeded' or not isinstance(result.get('output'), str):
            raise ValueError('Non-success output')
        if result['output'].strip() != oracle(record):
            raise ValueError('Exact source-field answer mismatch')


def observations_valid(folder, rows, slots):
    observations = [json.loads(p.read_text()) for i in range(slots)
                    for p in (folder / f'worker-{i}').glob('workers/*/generation-*.json')]
    by_attempt = {o['attempt_id']: o for o in observations}
    if len(observations) != len(rows) or set(by_attempt) != {r['accepted_attempt_id'] for r in rows}:
        raise ValueError('Accepted observations missing or duplicated')
    for r in rows:
        o = by_attempt[r['accepted_attempt_id']]
        if o['identity']['job_id'] != r['id'] or o['result']['done'] is not True or o['result'].get('finish_reason') != 'stop' or o['result']['output'] != r['output'] or o['config_hash'] != r['config_hash']:
            raise ValueError('Invalid accepted stop observation')


def fixture():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def reply(self, value):
            raw = local.encoded(value)
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            if self.path == '/api/version':
                self.reply({'version': 'controlled-fixture'})
            elif self.path == '/api/ps':
                self.reply({'models': [{'name': 'fixture:tiny'}] if self.server.loaded else []})
            else:
                self.reply({'models': [{'name': 'fixture:tiny', 'digest': 'b' * 64}]})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            prompt = body.get('prompt', '')
            self.server.loaded = body.get('keep_alive') != 0
            if not prompt:
                self.reply({'done': True, 'done_reason': 'load'})
                return
            if 'operations record' in prompt:
                import re
                field = re.search(r'exact value of (\w+)', prompt)[1]
                answer = re.search(r'^' + field + r': (BD-\d+)$', prompt, re.M)[1]
            else:
                answer = 'READY'
            time.sleep(.02)
            self.reply({'response': answer, 'done': True, 'done_reason': 'stop',
                        'total_duration': 20000000, 'load_duration': 0, 'eval_count': 5,
                        'eval_duration': 10000000, 'prompt_eval_count': len(prompt.split()),
                        'prompt_eval_duration': 10000000})
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.loaded = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Observer:
    """External diagnostic proxy. Baseline runs do not use this proxy."""
    def __init__(self, upstream, kind, events, lock):
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_):
                pass
            def do_GET(self):
                self.forward()
            def do_POST(self):
                self.forward()
            def forward(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                headers = {k: self.headers[k] for k in ('Authorization', 'Content-Type') if k in self.headers}
                start = time.perf_counter()
                req = Request(upstream + self.path, data=raw if self.command == 'POST' else None,
                              headers=headers, method=self.command)
                try:
                    response = urlopen(req, timeout=180)
                except HTTPError as error:
                    response = error
                with response:
                    data, status = response.read(16 * 1024 * 1024 + 1), response.status
                end = time.perf_counter()
                event = {'kind': kind, 'method': self.command, 'path': self.path,
                         'start': start, 'end': end, 'status': status}
                if data:
                    decoded = json.loads(data)
                    if isinstance(decoded, dict):
                        event['attempt_id'] = decoded.get('attempt_id')
                        if self.path == '/api/generate':
                            event['metrics'] = {k: decoded[k] for k in METRICS if k in decoded}
                            event['done'] = decoded.get('done')
                            event['done_reason'] = decoded.get('done_reason')
                with lock:
                    events.append(event)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
    def close(self):
        self.server.shutdown()
        self.server.server_close()


def stage_summary(events, start, end):
    # Union intervals: never sum overlapping HTTP requests into wall-clock overhead.
    selected = [e for e in events if e['start'] >= start and e['end'] <= end]
    groups = {}
    for event in selected:
        key = event['kind'] + ' ' + event['path']
        if '/v1/attempts/' in key:
            key = event['kind'] + ' /v1/attempts/*/' + event['path'].rsplit('/', 1)[-1]
        groups.setdefault(key, []).append(event['end'] - event['start'])
    intervals = sorted((e['start'], e['end']) for e in selected)
    merged = []
    for a, b in intervals:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(b, merged[-1][1])
        else:
            merged.append([a, b])
    covered = sum(b - a for a, b in merged)
    return {'route_intervals': {k: {'count': len(v), 'sum_seconds': sum(v),
                                   'median_seconds': statistics.median(v)} for k, v in groups.items()},
            'observed_http_union_seconds': covered,
            'outside_observed_http_seconds': end - start - covered,
            'outside_observed_http_includes': 'worker startup, local validation/files, HTTP client scheduling, controller polling/export; not a disk/DB attribution'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--runtime', choices=['fixture', 'ollama'], default='fixture')
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--pilot-only', action='store_true')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run as your ordinary user, never sudo')
    root = args.output_dir.resolve()
    if not root.is_relative_to((ROOT / '.batchdock').resolve()) or not 1 <= args.repetitions <= 3:
        parser.error('Use a new .batchdock output and 1..3 repetitions')
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    processes, servers = local.Processes(root), []
    report = {'label': LABEL, 'experiment': 'controlled_single_mac_v2', 'physical_hosts': 1,
              'p1_status': 'pending_real_hardware', 'p1_verdict': None, 'p1_thresholds_evaluated': False,
              'p2_started': False, 'runs': [], 'validated': False}
    origin = None
    initially_loaded = False
    residency_control_started = False
    model = 'gemma2:2b' if args.runtime == 'ollama' else 'fixture:tiny'

    def cli(*a):
        return subprocess.check_output([str(BINARY), *map(str, a)], cwd=ROOT, text=True, timeout=180)

    try:
        records = load(ROOT / 'Fixtures/benchmark-v2.jsonl')
        if args.runtime == 'fixture':
            server = fixture()
            servers.append(server)
            origin = f'http://127.0.0.1:{server.server_port}'
        else:
            origin = 'http://127.0.0.1:11434'
        initial_models = request(origin, '/api/ps')['models']
        if any(m['name'] != model for m in initial_models):
            raise RuntimeError('Other loaded models; do not disturb this runtime')
        initially_loaded = bool(initial_models)
        cli('doctor', '--ollama-url', origin, '--model', model, '--write-manifest', root / 'manifest.json', '--max-output-tokens', '32')
        manifest = json.loads((root / 'manifest.json').read_text())
        schedule = [{'phase': 'baseline' if regime == 'model_resident' else 'cold_start', 'regime': regime, 'engine': engine, 'slots': slots,
                     'repetition': rep, 'observer': False, 'records': 200 if regime == 'model_resident' else 12}
                    for rep in range(args.repetitions) for regime in ('model_unloaded', 'model_resident')
                    for engine in ('direct', 'batchdock') for slots in (1, 2)]
        random.Random(7183).shuffle(schedule)
        # Matched off/on observer pairs, fixed before seeing timings. One worker avoids overlapping attribution.
        profile = []
        for rep in range(args.repetitions):
            pair = [False, True]
            random.Random(900 + rep).shuffle(pair)
            profile.extend({'phase': 'profile', 'regime': 'model_resident', 'engine': 'batchdock',
                            'slots': 1, 'repetition': rep, 'observer': observed, 'records': 60} for observed in pair)
        schedule += profile
        sources = ['scripts/benchmark-single-mac.py', 'scripts/benchmark_dataset.py',
                   'scripts/benchmark-local.py', 'scripts/verify-single-mac.py', 'scripts/test-single-mac.py',
                   'Fixtures/benchmark-v2.jsonl', 'Package.resolved']
        plan = {'label': LABEL, 'experiment': report['experiment'], 'physical_hosts': 1,
                'p1_status': report['p1_status'], 'p1_verdict': None, 'p1_thresholds_evaluated': False,
                'p2_started': False, 'runtime_kind': args.runtime, 'manifest': manifest,
                'source_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                'source_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip()),
                'hashes': {p: sha(ROOT / p) for p in sources}, 'binary_sha256': sha(BINARY),
                'hardware': {'model': subprocess.check_output(['sysctl', '-n', 'hw.model'], text=True).strip(),
                             'ram_bytes': int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'], text=True)),
                             'macos': subprocess.check_output(['sw_vers', '-productVersion'], text=True).strip()},
                'schedule': [] if args.pilot_only else schedule, 'repetitions': args.repetitions,
                'initially_loaded': initially_loaded,
                'protocol': {'output_rule': 'exact source field, outer whitespace only; done=true/reason=stop',
                             'exclusions': 'none; failed outputs retained and whole experiment not accepted',
                             'cold': '12-record startup probes: selected model unloaded before run, checked via /api/ps; OS file cache uncontrolled',
                             'warm': 'unload, preload and fixed excluded READY warmup before every run',
                             'concurrency': [1, 2], 'options': manifest['generation'],
                             'poll_seconds': .25, 'cooldown_seconds': 2,
                             'timing': 'submit/dispatch to fully exported and validated output; worker startup included, idle grace excluded',
                             'profile': 'separate 60-record matched observer-off/on pairs; observer changes HTTP connections; do not mix into baseline',
                             'host_control': 'no competing work started by harness; other users/apps and GPU thermals not controlled',
                             'decision': 'descriptive single-Mac evidence only; no P1 go/narrow/stop'}}
        save(root / 'plan.json', plan)
        report['plan_sha256'] = sha(root / 'plan.json')
        save(root / 'dataset.json', records)

        def generate(record, endpoint):
            start = time.perf_counter()
            result = request(endpoint, '/api/generate', {'model': model, 'prompt': record['prompt'],
                             'system': manifest['generation']['system'], 'stream': False,
                             'truncate': False, 'shift': False, 'options': manifest['generation']['options']})
            return {'id': record['id'], 'state': 'succeeded' if result.get('done') is True and result.get('done_reason') == 'stop' else 'failed',
                    'output': result.get('response'), 'done': result.get('done'), 'done_reason': result.get('done_reason'),
                    'metrics': {k: result[k] for k in METRICS if k in result},
                    'request_seconds': time.perf_counter() - start}

        def prepare(regime):
            request(origin, '/api/generate', {'model': model, 'stream': False, 'keep_alive': 0})
            if request(origin, '/api/ps')['models']:
                raise RuntimeError('Model unload not observed; other runtime activity or unsupported control')
            if regime == 'model_resident':
                request(origin, '/api/generate', {'model': model, 'stream': False, 'keep_alive': '10m',
                                                   'options': manifest['generation']['options']})
                warmup = generate({'id': 'warmup', 'prompt': 'Return only READY.'}, origin)
                if warmup['state'] != 'succeeded' or not request(origin, '/api/ps')['models']:
                    raise RuntimeError('Warm preparation failed')
            return request(origin, '/api/ps')

        # One example per family/length band, excluded from measured runs; immutable dataset already frozen.
        seen, pilot = set(), []
        for record in records:
            group = record['field'], record['length_band']
            if group not in seen:
                pilot.append(record)
                seen.add(group)
        residency_control_started = True
        prepare('model_resident')
        pilot_rows = [generate(r, origin) for r in pilot]
        save(root / 'pilot-results.json', pilot_rows)
        rows_valid(pilot_rows, pilot)
        report['pilot_valid'] = True
        for index, config in enumerate(plan['schedule']):
            folder = root / f'run-{index:02d}'
            folder.mkdir(mode=0o700)
            selected = records[:config['records']]
            (folder / 'inputs.jsonl').write_bytes(b''.join(local.encoded({'id': r['id'], 'prompt': r['prompt']}) + b'\n' for r in selected))
            events, lock, observers, workers = [], threading.Lock(), [], []
            stage = {}
            state = folder / 'coordinator'
            if config['engine'] == 'batchdock':
                cli('init', '--state-dir', state)
                coordinator_origin = f'http://127.0.0.1:{local.port()}'
                coordinator = processes.start([BINARY, 'serve', '--state-dir', state, '--port', coordinator_origin.rsplit(':', 1)[1]], f'run-{index:02d}-coordinator')
                local.ready(coordinator_origin, coordinator)
                credentials = []
                for i in range(config['slots']):
                    token = folder / f'worker-{i}.token'
                    cli('worker', 'add', '--state-dir', state, '--coordinator', coordinator_origin,
                        '--name', f'worker-{i}', '--write-token', token)
                    credentials.append(token)
                owner = (state / 'owner-token').read_text().strip()
            else:
                coordinator_origin = None
            runtime_endpoint = origin
            worker_endpoint = coordinator_origin
            if config['observer']:
                for upstream, kind in [(origin, 'runtime'), (coordinator_origin, 'coordinator')]:
                    observer = Observer(upstream, kind, events, lock)
                    observers.append(observer)
                runtime_endpoint, worker_endpoint = observers[0].origin, observers[1].origin
            time.sleep(2)
            residency = prepare(config['regime'])
            before = host_snapshot()
            identity = manifest['model']
            if request(origin, '/api/version')['version'] != identity['ollama_version'] or not any(m['name'] == model and m['digest'] == identity['digest'] for m in request(origin, '/api/tags')['models']):
                raise RuntimeError('Runtime identity changed')
            start = time.perf_counter()
            try:
                if config['engine'] == 'direct':
                    pending = queue.Queue()
                    for record in selected:
                        pending.put(record)
                    def slot(_):
                        output = []
                        while True:
                            try:
                                record = pending.get_nowait()
                            except queue.Empty:
                                return output
                            output.append(generate(record, runtime_endpoint))
                    with ThreadPoolExecutor(max_workers=config['slots']) as executor:
                        chunks = list(executor.map(slot, range(config['slots'])))
                    by_id = {r['id']: r for chunk in chunks for r in chunk}
                    rows = [by_id[r['id']] for r in selected]
                else:
                    submitted = json.loads(cli('submit', '--state-dir', state, '--coordinator', coordinator_origin,
                                               '--manifest', root / 'manifest.json', '--input', folder / 'inputs.jsonl',
                                               '--request-id', uuid.uuid4()))['batch_id']
                    stage['submission_seconds'] = time.perf_counter() - start
                    for i, token in enumerate(credentials):
                        workers.append(processes.start([BINARY, 'worker', 'run', '--coordinator', worker_endpoint,
                            '--ollama-url', runtime_endpoint, '--state-dir', folder / f'worker-{i}',
                            '--token-file', token, '--stop-when-idle', '--idle-grace-seconds', '1'], f'run-{index:02d}-worker-{i}'))
                    stage['worker_launch_seconds'] = time.perf_counter() - start - stage['submission_seconds']
                    deadline = time.perf_counter() + 900
                    while request(coordinator_origin, f'/v1/batches/{submitted}', token=owner)['state'] == 'running':
                        if any(w.poll() not in (None, 0) for w in workers) or time.perf_counter() > deadline:
                            raise RuntimeError('Worker exited or batch exceeded deadline')
                        time.sleep(.25)
                    stage['terminal_detected_seconds'] = time.perf_counter() - start
                    export_start = time.perf_counter()
                    rows = [json.loads(x) for x in cli('export', '--state-dir', state, '--coordinator', coordinator_origin, submitted).splitlines()]
                    stage['export_seconds'] = time.perf_counter() - export_start
                error = None
                try:
                    rows_valid(rows, selected)
                    if config['engine'] == 'batchdock':
                        observations_valid(folder, rows, config['slots'])
                except ValueError as failure:
                    error = str(failure)
                end = time.perf_counter()
                if config['observer']:
                    stage['external_profile'] = stage_summary(events, start, end)
                save(folder / 'results.json', rows)
                save(folder / 'events.json', events)
                if config['engine'] == 'batchdock':
                    import sqlite3
                    with sqlite3.connect('file:' + str(state / 'queue.sqlite') + '?mode=ro', uri=True) as db:
                        participation = dict(db.execute('SELECT w.name,count(*) FROM results r JOIN attempts a ON a.id=r.attempt_id JOIN workers w ON w.id=a.worker_id GROUP BY w.name'))
                    if len(participation) != config['slots']:
                        raise RuntimeError('Not all workers accepted work')
                else:
                    participation = None
                run = dict(config, folder=folder.name, elapsed_seconds=end - start, clock_start=start, clock_end=end, stages=stage,
                           outputs_valid=error is None, validation_error=error, accepted_outputs=len(rows),
                           valid_outputs=sum(r.get('state') == 'succeeded' and isinstance(r.get('output'), str) and r['output'].strip() == oracle(expected) for r, expected in zip(rows, selected)),
                           worker_participation=participation, residency_before=residency,
                           host_before=before, host_after=host_snapshot())
                report['runs'].append(run)
                save(root / 'report.json', report)
                print(f"{('PASS' if error is None else 'FAIL')} {LABEL}: {index + 1}/{len(plan['schedule'])} {config['phase']} {config['regime']} {config['engine']} slots={config['slots']} observer={config['observer']} seconds={end-start:.2f}", flush=True)
                if request(origin, '/api/version')['version'] != identity['ollama_version'] or not any(m['name'] == model and m['digest'] == identity['digest'] for m in request(origin, '/api/tags')['models']):
                    raise RuntimeError('Runtime identity changed')
                if error:
                    raise RuntimeError('Invalid evidence retained; do not revise this experiment in place')
            finally:
                for worker in workers:
                    processes.stop(worker)
                for observer in observers:
                    observer.close()
                if config['engine'] == 'batchdock':
                    processes.stop(coordinator)
            if sha(root / 'plan.json') != report['plan_sha256'] or any(sha(ROOT / p) != h for p, h in plan['hashes'].items()) or sha(BINARY) != plan['binary_sha256']:
                raise RuntimeError('Frozen plan/source/binary changed')
        report['validated'] = not args.pilot_only
        report['pilot_only'] = args.pilot_only
    except BaseException as failure:
        report['failure_type'] = type(failure).__name__
        raise
    finally:
        processes.close()
        report['owned_processes_stopped'] = all(p.poll() is not None for p, _ in processes.items)
        if origin and residency_control_started:
            request(origin, '/api/generate', {'model': model, 'stream': False, 'keep_alive': '5m' if initially_loaded else 0})
            report['model_residency_restored'] = bool(request(origin, '/api/ps')['models']) == initially_loaded
        for server in servers:
            server.shutdown()
            server.server_close()
        save(root / 'report.json', report)


if __name__ == '__main__':
    main()
