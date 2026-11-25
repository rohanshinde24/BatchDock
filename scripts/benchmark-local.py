#!/usr/bin/env python3
"""Validate benchmark plumbing on ONE Mac. Never evaluates physical P1 gates.

Default inference is simulated at two loopback endpoints. --runtime ollama uses
an already-running local Ollama shared by both workers; it never pulls models.
An isolated unprivileged sshd is loopback-only and all owned processes stop.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.server
import json
import os
from pathlib import Path
import pwd
import queue
import random
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from urllib.request import Request, urlopen
from ssh_policy import require_restricted

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / '.build/debug/batchdock'
LABEL = 'single_host_emulation'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.write_bytes(json.dumps(value, indent=2, sort_keys=True).encode() + b'\n')
    path.chmod(0o600)


def port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def api(origin, path, body=None):
    request = Request(origin + path, data=None if body is None else encoded(body),
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=90) as response:
        data = response.read(512 * 1024 + 1)
        if len(data) > 512 * 1024:
            raise RuntimeError('Response too large')
        return json.loads(data)


def validate(rows, expected):
    if [r['id'] for r in rows] != list(expected):
        raise RuntimeError('Missing, duplicated or reordered input IDs')
    for row in rows:
        if row.get('state') != 'succeeded' or not isinstance(row.get('output'), str):
            raise RuntimeError('Unsuccessful or invalid row')
        if ' '.join(row['output'].split()).casefold() != expected[row['id']]:
            raise RuntimeError('Exact normalized output validation failed')


class Processes:
    def __init__(self, root):
        self.root, self.items = root, []

    def start(self, args, name):
        log = (self.root / (name + '.log')).open('wb')
        process = subprocess.Popen(list(map(str, args)), cwd=ROOT, stdout=log, stderr=log)
        self.items.append((process, log))
        return process

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def close(self):
        for process, log in reversed(self.items):
            self.stop(process)
            log.close()


def ready(origin, process):
    end = time.monotonic() + 15
    while time.monotonic() < end:
        if process.poll() is not None:
            raise RuntimeError('Owned service exited; inspect private logs')
        try:
            api(origin, '/health')
            return
        except OSError:
            time.sleep(.05)
    raise RuntimeError('Owned service did not become ready')


def ssh_setup(root, coordinator_port, processes):
    keys = root / 'ssh'
    keys.mkdir(mode=0o700)
    for name in ('host', 'worker', 'untrusted'):
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(keys / name)], check=True)
    (keys / 'authorized_keys').write_text((keys / 'worker.pub').read_text())
    (keys / 'authorized_keys').chmod(0o600)
    ssh_port, forward_port = port(), port()
    config = f'''Port {ssh_port}
ListenAddress 127.0.0.1
HostKey {keys}/host
PidFile {keys}/sshd.pid
AuthorizedKeysFile {keys}/authorized_keys
AllowUsers {pwd.getpwuid(os.getuid()).pw_name}
AuthenticationMethods publickey
PubkeyAuthentication yes
HostbasedAuthentication no
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
StrictModes yes
AllowTcpForwarding local
PermitOpen 127.0.0.1:{coordinator_port}
PermitListen none
AllowStreamLocalForwarding no
AllowAgentForwarding no
X11Forwarding no
PermitTTY no
PermitTunnel no
PermitUserRC no
MaxSessions 0
ForceCommand /usr/bin/false
UseDNS no
'''
    (keys / 'sshd_config').write_text(config)
    (keys / 'sshd_config').chmod(0o600)
    effective = subprocess.check_output(['/usr/sbin/sshd', '-T', '-f', str(keys / 'sshd_config')], text=True)
    require_restricted(effective, coordinator_port)
    (keys / 'effective-config.txt').write_text(effective)
    daemon = processes.start(['/usr/sbin/sshd', '-D', '-e', '-f', keys / 'sshd_config'], 'sshd')
    # Trust is bootstrapped from our generated host key, never blind keyscan acceptance.
    host = (keys / 'host.pub').read_text().split()
    known = keys / 'known_hosts'
    known.write_text(f'[127.0.0.1]:{ssh_port} {host[0]} {host[1]}\n')
    base = ['ssh', '-F', '/dev/null', '-i', str(keys / 'worker'), '-p', str(ssh_port),
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
            '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={known}',
            '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=3',
            '-o', 'ExitOnForwardFailure=yes']
    target = pwd.getpwuid(os.getuid()).pw_name + '@127.0.0.1'
    tunnel = processes.start(base + ['-N', '-T', '-L', f'127.0.0.1:{forward_port}:127.0.0.1:{coordinator_port}', target], 'tunnel')
    forwarded = f'http://127.0.0.1:{forward_port}'
    ready(forwarded, tunnel)
    if daemon.poll() is not None:
        raise RuntimeError('Isolated SSH server stopped')

    def denied(args, label):
        try:
            result = subprocess.run(args, capture_output=True, timeout=6, text=True)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError('SSH restriction was not enforced: ' + label) from error
        if result.returncode == 0:
            raise RuntimeError('SSH restriction allowed ' + label)
        return {'denied': True, 'returncode': result.returncode, 'stderr': result.stderr.strip()}

    wrong = base.copy()
    wrong[wrong.index(str(keys / 'worker'))] = str(keys / 'untrusted')
    checks = {'allowed_forward': True, 'strict_host_key_pinned': True}
    checks['shell'] = denied(base + [target, 'true'], 'shell')
    checks['sftp'] = denied(base + ['-s', target, 'sftp'], 'sftp')
    checks['pty'] = denied(base + ['-tt', target, 'true'], 'pty')
    unrelated = port()
    checks['other_destination'] = denied(base + ['-W', f'127.0.0.1:{unrelated}', target], 'unrelated destination')
    if 'administratively prohibited' not in checks['other_destination']['stderr']:
        raise RuntimeError('Other destination failed for a reason other than forwarding policy')
    checks['reverse_tcp'] = denied(base + ['-N', '-R', f'127.0.0.1:{port()}:127.0.0.1:{coordinator_port}', target], 'reverse TCP')
    checks['reverse_socket'] = denied(base + ['-N', '-R', f'{keys}/denied.sock:127.0.0.1:{coordinator_port}', target], 'reverse socket')
    fake_host = (keys / 'untrusted.pub').read_text().split()
    wrong_known = keys / 'wrong_known_hosts'
    wrong_known.write_text(f'[127.0.0.1]:{ssh_port} {fake_host[0]} {fake_host[1]}\n')
    wrong_host = base.copy()
    wrong_host[wrong_host.index(f'UserKnownHostsFile={known}')] = f'UserKnownHostsFile={wrong_known}'
    checks['wrong_host_key'] = denied(wrong_host + ['-W', f'127.0.0.1:{coordinator_port}', target], 'wrong host key')
    if 'Host key verification failed' not in checks['wrong_host_key']['stderr']:
        raise RuntimeError('Wrong host key failed for an unrelated reason')
    checks['untrusted_key'] = denied(wrong + ['-W', f'127.0.0.1:{coordinator_port}', target], 'untrusted key')
    fingerprint = subprocess.check_output(['ssh-keygen', '-lf', str(keys / 'host.pub')], text=True).strip()
    checks['host_fingerprint'] = fingerprint
    save(root / 'ssh-checks.json', checks)
    return forwarded, checks


def fixture_runtime():
    class Fixture(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def reply(self, body):
            data = encoded(body)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            self.reply({'version': 'fixture-runtime'} if self.path == '/api/version' else
                       {'models': [{'name': 'fixture:tiny', 'digest': 'a' * 64}]})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if body.get('truncate') is not False or body.get('shift') is not False:
                self.send_error(400)
                return
            time.sleep(.02)  # Give both real worker processes a meaningful chance to claim.
            prompt = body['prompt'].casefold()
            answer = 'kettle' if 'kettle' in prompt else 'desk lamp' if 'lamp' in prompt else 'hiking backpack'
            self.reply({'response': answer, 'done': True, 'done_reason': 'stop', 'eval_count': 2})
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--runtime', choices=['fixture', 'ollama'], default='fixture')
    parser.add_argument('--records', type=int, default=200)
    parser.add_argument('--repetitions', type=int, default=3)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Run as your ordinary user, never with sudo")
    if not 12 <= args.records <= 1000 or not 1 <= args.repetitions <= 5:
        parser.error('Use 12..1000 records and 1..5 repetitions')
    root = args.output_dir.resolve()
    if not root.is_relative_to((ROOT / '.batchdock').resolve()):
        parser.error('Private output must be under this checkout .batchdock')
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    processes, runtimes = Processes(root), []
    report = {'label': LABEL, 'physical_hosts': 1, 'p1_status': 'pending_real_hardware',
              'p1_verdict': None, 'p1_thresholds_evaluated': False, 'runs': []}

    def cli(*arguments):
        return subprocess.run([str(BINARY), *map(str, arguments)], cwd=ROOT, check=True,
                              capture_output=True, text=True, timeout=120).stdout

    try:
        if args.runtime == 'fixture':
            runtimes = [fixture_runtime(), fixture_runtime()]
            origins = [f'http://127.0.0.1:{r.server_port}' for r in runtimes]
            model = 'fixture:tiny'
        else:
            origins, model = ['http://127.0.0.1:11434'] * 2, 'gemma2:2b'
        cli('doctor', '--ollama-url', origins[0], '--model', model, '--write-manifest', root / 'manifest.json', '--max-output-tokens', '64')
        manifest = json.loads((root / 'manifest.json').read_text())
        version_b = api(origins[1], '/api/version')['version']
        tags_b = api(origins[1], '/api/tags')['models']
        if version_b != manifest['model']['ollama_version'] or not any(m['name'] == model and m['digest'] == manifest['model']['digest'] for m in tags_b):
            raise RuntimeError('Runtime/model identities differ')
        base = [json.loads(x) for x in (ROOT / 'Fixtures/prompts.jsonl').read_text().splitlines()]
        products = ['kettle', 'desk lamp', 'hiking backpack']
        records = [{'id': f'item-{i:04d}', 'prompt': base[i % 3]['prompt']} for i in range(args.records)]
        expected = {r['id']: products[i % 3] for i, r in enumerate(records)}
        input_bytes = b''.join(encoded(r) + b'\n' for r in records)
        (root / 'inputs.jsonl').write_bytes(input_bytes)
        seed, configs = 42, ['direct_one_slot', 'direct_two_slots', 'batchdock_one_worker', 'batchdock_two_workers_ssh']
        schedule = [(rep, name) for rep in range(args.repetitions) for name in configs]
        random.Random(seed).shuffle(schedule)
        plan = {'schema_version': 1, 'label': LABEL, 'runtime_kind': args.runtime,
                'lock_sha256': sha((ROOT / 'Package.resolved').read_bytes()),
                'hardware': {'model': subprocess.check_output(['sysctl', '-n', 'hw.model'], text=True).strip(),
                             'ram_bytes': int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'], text=True)),
                             'macos': subprocess.check_output(['sw_vers', '-productVersion'], text=True).strip()},
                'physical_hosts': 1, 'shared_real_ollama': args.runtime == 'ollama',
                'model': manifest, 'records': args.records, 'distinct_prompts': 3,
                'workload_sha256': sha(input_bytes), 'expected_sha256': sha(encoded(expected)),
                'repetitions': args.repetitions, 'schedule_seed': seed, 'schedule': schedule,
                'source_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                'source_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip()),
                'harness_sha256': sha(Path(__file__).read_bytes()),
                'ssh_policy_sha256': sha((ROOT / 'scripts/ssh_policy.py').read_bytes()),
                'binary_sha256': sha(BINARY.read_bytes()),
                'physical_p1_criteria': {'script_capacity_fraction': .8, 'max_batchdock_makespan_ratio': 1.1,
                                         'evaluated_here': False},
                'timing': 'single-controller monotonic submit/dispatch to complete validated results; BatchDock worker startup included, idle grace excluded',
                'cache_regime': 'synthetic' if args.runtime == 'fixture' else 'shared-runtime, no forced cold starts; warm/cold P1 experiment not performed',
                'baseline': 'bounded threaded reference, no claim of tuned physical two-Mac baseline',
                'output_rule': 'exact product after whitespace/case normalization; explicit stop only',
                'p1_verdict': None, 'p2_work': False}
        save(root / 'expected.json', expected)
        save(root / 'plan.json', plan)  # Frozen before requests that execute inference.
        plan_hash = sha((root / 'plan.json').read_bytes())
        state = root / 'coordinator'
        cli('init', '--state-dir', state)
        coordinator_port = port()
        origin = f'http://127.0.0.1:{coordinator_port}'
        coordinator = processes.start([BINARY, 'serve', '--state-dir', state, '--port', coordinator_port], 'coordinator')
        ready(origin, coordinator)
        forwarded, ssh_checks = ssh_setup(root, coordinator_port, processes)
        report['ssh_checks'] = ssh_checks
        # All keys/state are fresh and ignored; each named process gets its own worker token/spool.
        for rep, config in schedule:
            name = f'{rep}-{config}'
            if config.startswith('direct'):
                slots = 1 if config == 'direct_one_slot' else 2
                def generate(index, record):
                    body = {'model': model, 'prompt': record['prompt'], 'system': manifest['generation']['system'],
                            'stream': False, 'truncate': False, 'shift': False,
                            'options': manifest['generation']['options']}
                    result = api(origins[index % slots], '/api/generate', body)
                    if result.get('done') is not True or result.get('done_reason') != 'stop':
                        raise RuntimeError('Reference generation incomplete')
                    return {'id': record['id'], 'output': result['response'], 'state': 'succeeded'}
                pending = queue.Queue()
                for record in records:
                    pending.put(record)
                def slot(index):
                    finished = []
                    while True:
                        try:
                            record = pending.get_nowait()
                        except queue.Empty:
                            return finished
                        finished.append(generate(index, record))
                start = time.monotonic()
                with ThreadPoolExecutor(max_workers=slots) as executor:
                    chunks = list(executor.map(slot, range(slots)))
                by_id = {row['id']: row for chunk in chunks for row in chunk}
                if sum(len(chunk) for chunk in chunks) != len(records):
                    raise RuntimeError('Reference output cardinality changed')
                rows = [by_id[record['id']] for record in records]
                validate(rows, expected)
                elapsed = time.monotonic() - start
                participation = None
            else:
                count = 1 if config == 'batchdock_one_worker' else 2
                credentials = []
                for index in range(count):
                    token = root / (name + f'-worker-{index}.token')
                    cli('worker', 'add', '--state-dir', state, '--coordinator', origin,
                        '--name', name + f'-worker-{index}', '--write-token', token)
                    credentials.append(token)
                start = time.monotonic()
                batch = json.loads(cli('submit', '--state-dir', state, '--coordinator', origin,
                                       '--manifest', root / 'manifest.json', '--input', root / 'inputs.jsonl',
                                       '--request-id', uuid.uuid4()))['batch_id']
                workers = [processes.start([BINARY, 'worker', 'run', '--coordinator', origin if index == 0 else forwarded,
                           '--ollama-url', origins[index], '--state-dir', root / (name + f'-state-{index}'),
                           '--token-file', token, '--stop-when-idle', '--idle-grace-seconds', '1'], name + f'-worker-{index}')
                           for index, token in enumerate(credentials)]
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    status = json.loads(cli('status', '--state-dir', state, '--coordinator', origin, batch))
                    if status['state'] != 'running':
                        break
                    if any(worker.poll() not in (None, 0) for worker in workers):
                        raise RuntimeError('Worker failed; inspect private logs')
                    time.sleep(.05)
                else:
                    raise RuntimeError('Local emulation exceeded deadline')
                rows = [json.loads(x) for x in cli('export', '--state-dir', state, '--coordinator', origin,
                                                  batch, '--require-success').splitlines()]
                validate(rows, expected)
                elapsed = time.monotonic() - start
                observations = [json.loads(p.read_text()) for index in range(count)
                                for p in (root / (name + f'-state-{index}')).glob('workers/*/generation-*.json')]
                if {(o['attempt_id'], o['identity']['job_id']) for o in observations} != {(r['accepted_attempt_id'], r['id']) for r in rows} or not all(o['result']['done'] and o['result'].get('finish_reason') == 'stop' for o in observations):
                    raise RuntimeError('Accepted attempts lack matching stop observations')
                with sqlite3.connect('file:' + str(state / 'queue.sqlite') + '?mode=ro', uri=True) as db:
                    participation = dict(db.execute('SELECT w.name,count(*) FROM results r JOIN attempts a ON a.id=r.attempt_id JOIN workers w ON w.id=a.worker_id WHERE r.batch_id=? GROUP BY w.name', (batch,)))
                if len(participation) != count:
                    raise RuntimeError('Not every worker participated; emulation is not validated')
                for worker in workers:
                    worker.wait(timeout=15)
                    if worker.returncode != 0:
                        raise RuntimeError('Worker did not exit cleanly')
            save(root / (name + '-results.json'), rows)
            report['runs'].append({'configuration': config, 'repetition': rep, 'label': LABEL,
                                   'accepted_rows': len(rows), 'elapsed_seconds': elapsed,
                                   'worker_participation': participation, 'outputs_valid': True})
            if sha((root / 'plan.json').read_bytes()) != plan_hash:
                raise RuntimeError('Frozen plan changed during execution')
            save(root / 'report.json', report)
            print(f'PASS {LABEL}: {name}, {len(rows)} checked outputs', flush=True)
        report['validated'] = True
        report['plan_sha256'] = plan_hash
        report['owned_processes_stopped'] = False
    except BaseException as error:
        report['validated'] = False
        report['failure_type'] = type(error).__name__
        raise
    finally:
        processes.close()
        for runtime in runtimes:
            runtime.shutdown()
            runtime.server_close()
        report['owned_processes_stopped'] = all(p.poll() is not None for p, _ in processes.items)
        save(root / 'report.json', report)


if __name__ == '__main__':
    main()
