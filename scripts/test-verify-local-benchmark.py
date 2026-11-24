#!/usr/bin/env python3
"""Exercise artifact rejection using a completed local-emulation run, in a copy."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

spec = importlib.util.spec_from_file_location('verifier', Path(__file__).with_name('verify-local-benchmark.py'))
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    source = args.directory.resolve()
    verifier.verify(source)
    # Avoid copying credentials/SSH keys/logs. Copy only verifier inputs.
    with tempfile.TemporaryDirectory(prefix='verifier-regressions-') as temporary:
        root = Path(temporary) / 'artifacts'
        root.mkdir(mode=0o700)
        for pattern in ('*.json', '*.jsonl', '*-state-*', 'coordinator'):
            for path in source.glob(pattern):
                if path.is_dir():
                    if path.name == 'coordinator':
                        target = root / path.name
                        target.mkdir()
                        with sqlite3.connect('file:' + str(path / 'queue.sqlite') + '?mode=ro', uri=True) as live, sqlite3.connect(target / 'queue.sqlite') as copied:
                            live.backup(copied)
                    else:
                        for observation in path.glob('workers/*/generation-*.json'):
                            target = root / observation.relative_to(source)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(observation, target)
                else:
                    shutil.copy2(path, root / path.name)
        verifier.verify(root)

        def reject(path, edit, name):
            original = path.read_bytes()
            try:
                value = json.loads(original)
                edit(value)
                path.write_text(json.dumps(value))
                try:
                    verifier.verify(root)
                except ValueError:
                    print('PASS rejected: ' + name)
                else:
                    raise RuntimeError('Verifier accepted: ' + name)
            finally:
                path.write_bytes(original)

        reject(root / 'report.json', lambda x: x.update(p1_verdict='go'), 'physical P1 verdict')
        reject(root / 'plan.json', lambda x: x.update(records=x['records'] + 1), 'changed frozen plan')
        reject(root / 'expected.json', lambda x: x.update({next(iter(x)): 'wrong'}), 'changed expected answers')
        result = next(root.glob('*-results.json'))
        reject(result, lambda x: x[0].update(output='wrong'), 'incorrect output')
        observation = next(root.glob('*-state-*/workers/*/generation-*.json'))
        reject(observation, lambda x: x['result'].update(finish_reason='length'), 'incomplete accepted generation')
        verifier.verify(root)
    print('PASS original artifacts unchanged')


if __name__ == '__main__':
    main()
