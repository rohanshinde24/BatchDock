#!/usr/bin/env python3
"""Tamper checks against a credential-free copy of completed baseline artifacts."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

spec = importlib.util.spec_from_file_location('verify', Path(__file__).with_name('verify-single-mac.py'))
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    source = parser.parse_args().directory.resolve()
    verifier.verify(source)
    with tempfile.TemporaryDirectory(prefix='single-mac-verifier-') as name:
        root = Path(name)
        for path in source.glob('*.json'):
            shutil.copy2(path, root / path.name)
        for folder in source.glob('run-*'):
            if not folder.is_dir():
                continue
            copied = root / folder.name
            copied.mkdir()
            for path in folder.glob('*.json*'):
                shutil.copy2(path, copied / path.name)
            for observation in folder.glob('worker-*/workers/*/generation-*.json'):
                target = root / observation.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(observation, target)
            db = folder / 'coordinator/queue.sqlite'
            if db.exists():
                target = copied / 'coordinator/queue.sqlite'
                target.parent.mkdir()
                with sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True) as live, sqlite3.connect(target) as clone:
                    live.backup(clone)
        verifier.verify(root)

        def reject(path, change, label):
            original = path.read_bytes()
            try:
                value = json.loads(original)
                change(value)
                path.write_text(json.dumps(value))
                try:
                    verifier.verify(root)
                except ValueError:
                    print('PASS rejected: ' + label)
                else:
                    raise RuntimeError('Accepted corrupted ' + label)
            finally:
                path.write_bytes(original)

        reject(root / 'report.json', lambda x: x.update(p1_verdict='go'), 'physical P1 verdict')
        reject(root / 'plan.json', lambda x: x['schedule'].pop(), 'missing scheduled run')
        reject(root / 'dataset.json', lambda x: x[0].update(expected='BD-00000'), 'changed ground truth')
        reject(next(root.glob('run-*/results.json')), lambda x: x[0].update(output='wrong'), 'wrong answer')
        reject(next(root.glob('run-*/worker-*/workers/*/generation-*.json')), lambda x: x['result'].update(finish_reason='length'), 'accepted incomplete generation')
        observed = next(p for p in root.glob('run-*/events.json') if json.loads(p.read_text()))
        reject(observed, lambda x: x[0].update(end=x[0]['end'] + 1), 'modified profile event')
        verifier.verify(root)
    print('PASS original artifacts unchanged')


if __name__ == '__main__':
    main()
