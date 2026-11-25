#!/usr/bin/env python3
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from frozen_sources import frozen_sources


class FrozenSourceTests(unittest.TestCase):
    def test_changed_working_file_uses_exact_historical_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            source = root / 'source.txt'
            source.write_bytes(b'original')
            subprocess.run(['git', 'add', 'source.txt'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=Artifact Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'Frozen fixture'], cwd=root, check=True)
            revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
            expected = {'source.txt': hashlib.sha256(b'original').hexdigest()}
            self.assertEqual(frozen_sources(root, expected, revision), [])
            later = root / 'later.txt'
            later.write_bytes(b'published')
            subprocess.run(['git', 'add', 'later.txt'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=Artifact Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'Publish fixture'], cwd=root, check=True)
            publication = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
            later.write_bytes(b'changed')
            expected_later = {'later.txt': hashlib.sha256(b'published').hexdigest()}
            with self.assertRaises(ValueError):
                frozen_sources(root, expected_later, revision)
            self.assertEqual(frozen_sources(root, expected_later, revision, allow_published=True),
                             [{'path': 'later.txt', 'revision': publication}])
            source.write_bytes(b'changed')
            self.assertEqual(frozen_sources(root, expected, revision), [{'path': 'source.txt', 'revision': revision}])
            with self.assertRaises(ValueError):
                frozen_sources(root, {'source.txt': '0' * 64}, revision)

    def test_invalid_revision_and_source_escape_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                frozen_sources(root, {}, 'HEAD')
            with self.assertRaises(ValueError):
                frozen_sources(root, {'../outside': '0' * 64}, 'a' * 40)


if __name__ == '__main__':
    unittest.main()
