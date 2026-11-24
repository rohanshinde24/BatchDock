#!/usr/bin/env python3
"""Small semantic tests for the local benchmark's output acceptance rule."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('benchmark_local', Path(__file__).with_name('benchmark-local.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class OutputAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.expected = {'one': 'desk lamp', 'two': 'kettle'}
        self.rows = [{'id': 'one', 'state': 'succeeded', 'output': 'Desk  lamp\n'},
                     {'id': 'two', 'state': 'succeeded', 'output': 'kettle'}]

    def test_normalization_is_limited_to_case_and_whitespace(self):
        module.validate(self.rows, self.expected)
        self.rows[0]['output'] = 'a desk lamp'
        with self.assertRaises(RuntimeError):
            module.validate(self.rows, self.expected)

    def test_missing_duplicate_and_reordered_ids_are_rejected(self):
        for bad in [self.rows[:1], [self.rows[0], self.rows[0]], list(reversed(self.rows))]:
            with self.subTest(rows=bad), self.assertRaises(RuntimeError):
                module.validate(bad, self.expected)

    def test_correct_answer_with_failed_state_is_not_success(self):
        self.rows[0]['state'] = 'failed'
        with self.assertRaises(RuntimeError):
            module.validate(self.rows, self.expected)

    def test_nontext_output_is_rejected(self):
        self.rows[0]['output'] = None
        with self.assertRaises(RuntimeError):
            module.validate(self.rows, self.expected)


if __name__ == '__main__':
    unittest.main()
