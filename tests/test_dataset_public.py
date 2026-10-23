#!/usr/bin/env python3
"""Tests for dataset checksums, task counts, and training-record fields."""
import collections
import hashlib
import json
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / 'data' / 'train.jsonl'
PAPER_SHA256 = ('67a59da3b63f37701960fe3c8e732bda94dc245879a0a4ba60d'
                '71d403a1c3e28')


class DatasetIdentity(unittest.TestCase):
    def test_exact_paper_file_sha(self):
        got = hashlib.sha256(TRAIN.read_bytes()).hexdigest()
        self.assertEqual(got, PAPER_SHA256)

    def test_counts_and_task_distribution(self):
        tasks = collections.Counter()
        ids = set()
        n = 0
        for line in open(TRAIN, encoding='utf-8'):
            if not line.strip():
                continue
            n += 1
            rec = json.loads(line)
            tasks[rec['task']] += 1
            ids.add(rec['sample_id'])
        self.assertEqual(n, 3000)
        self.assertEqual(len(ids), 3000)
        self.assertEqual(dict(tasks), {'Interpretation': 600, 'AD': 600,
                                       'RCA': 600, 'SR': 600, 'Parsing': 600})

    def test_frozen_student_schema_and_identity(self):
        for line in open(TRAIN, encoding='utf-8'):
            rec = json.loads(line)
            self.assertEqual(
                set(rec),
                {'instruction', 'input', 'output', 'sample_id', 'task',
                 'parent_seed_id'} | ({'reasoning'} if 'reasoning' in rec
                                      else set()))
            self.assertTrue(rec['sample_id'].startswith('syn-'))
            self.assertTrue(rec['parent_seed_id'])
            for k in ('instruction', 'input', 'output', 'task'):
                self.assertIsInstance(rec[k], str)
                self.assertTrue(rec[k].strip())

    def test_student_export_mapping_matches_schema(self):
        from lr.cli import student_export_row
        rec = {'instruction': 'i', 'input': 'x', 'output': 'o',
               'sample_id': 'syn-abc', 'task': 'AD',
               'parent_seed_id': 'demo-ad-0002',
               'reasoning': None, 'generation_operator': 'none',
               'source_label_version': 'train-view-v1'}
        row = student_export_row(rec)
        self.assertEqual(set(row), {'instruction', 'input', 'output',
                                    'sample_id', 'task', 'parent_seed_id'})

    def test_source_provenance_disclosed(self):
        readme = (ROOT / 'data' / 'README.md').read_text(encoding='utf-8')
        for k in ('R-Log', '67a59da3b63f37701960fe3c8e732bda94dc245',
                 'license'):
            self.assertIn(k.lower(), readme.lower())


if __name__ == '__main__':
    unittest.main(verbosity=2)
