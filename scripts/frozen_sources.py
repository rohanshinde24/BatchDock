"""Verify frozen source hashes using current bytes or the exact recorded Git blob."""
import hashlib
from pathlib import Path
import re
import subprocess


def frozen_sources(root, expected, revision, allow_published=False):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Require an exact frozen Git revision')
    archived = []
    for name, digest in expected.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError('Frozen source escaped repository')
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            continue
        revisions = [revision]
        if allow_published:
            history = subprocess.check_output(['git', 'log', '--all', '--format=%H', '--', name], cwd=root, text=True)
            revisions += [r for r in history.splitlines() if r != revision]
        for candidate in revisions:
            try:
                original = subprocess.check_output(['git', 'cat-file', 'blob', candidate + ':' + name],
                                                   cwd=root, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError:
                continue
            if hashlib.sha256(original).hexdigest() == digest:
                archived.append({'path': name, 'revision': candidate})
                break
        else:
            raise ValueError('Exact frozen Git source unavailable: ' + name)
    return archived
