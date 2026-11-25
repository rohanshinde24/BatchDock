#!/usr/bin/env python3
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from benchmark_dataset import generate, load, oracle

spec = importlib.util.spec_from_file_location('controlled', Path(__file__).with_name('benchmark-single-mac.py'))
controlled = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controlled)


class BenchmarkContracts(unittest.TestCase):
    def test_committed_dataset_is_reproducible_and_covers_all_categories(self):
        records = load(controlled.ROOT / 'Fixtures/benchmark-v2.jsonl')
        self.assertEqual(records, generate())
        self.assertEqual(len({(r['field'], r['length_band']) for r in records}), 12)
        self.assertEqual(len({r['expected'] for r in records}), 200)

    def test_oracle_rejects_wrong_and_ambiguous_ground_truth(self):
        r = generate()[0]
        wrong = dict(r, expected='BD-00000')
        ambiguous = dict(r, prompt=r['prompt'] + '\n' + r['field'] + ': ' + r['expected'])
        for value in (wrong, ambiguous):
            with self.assertRaises(ValueError):
                oracle(value)

    def test_output_rule_rejects_extra_text_case_changes_failed_and_reordered_rows(self):
        records = generate()[:2]
        rows = [{'id': r['id'], 'state': 'succeeded', 'output': r['expected']} for r in records]
        controlled.rows_valid(rows, records)
        for changed in ('code: ' + rows[0]['output'], rows[0]['output'].lower()):
            bad = copy.deepcopy(rows)
            bad[0]['output'] = changed
            with self.assertRaises(ValueError):
                controlled.rows_valid(bad, records)
        for bad in (list(reversed(rows)), rows[:1], [rows[0], rows[0]], [dict(rows[0], state='failed'), rows[1]]):
            with self.assertRaises(ValueError):
                controlled.rows_valid(bad, records)

    def test_profile_uses_interval_union_and_never_adds_overlap_as_wall_time(self):
        events = [{'kind': 'coordinator', 'path': '/v1/claims', 'start': 1., 'end': 3.},
                  {'kind': 'runtime', 'path': '/api/version', 'start': 2., 'end': 4.},
                  {'kind': 'runtime', 'path': '/api/generate', 'start': 5., 'end': 6.}]
        result = controlled.stage_summary(events, 0., 7.)
        self.assertEqual(result['observed_http_union_seconds'], 4.)
        self.assertEqual(result['outside_observed_http_seconds'], 3.)


if __name__ == '__main__':
    unittest.main()
