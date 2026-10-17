"""Search equivalence, cancellation and isolation for the desktop worker."""

from dataclasses import asdict
import random
import subprocess
import sys
from threading import enumerate as active_threads
from time import monotonic
import unittest
from unittest.mock import patch

from rust_breeder.engine import predict, _donor_totals, _deterministic_from_totals
from rust_breeder.models import InventoryItem, Target
from rust_breeder.planner_process import (plan_parallel_process, plan_route_process, _report_from_dict,
                                         _worker_command, MESSAGE_LIMIT)
from rust_breeder.routes import plan_route


class PlannerProcessTests(unittest.TestCase):
    def setUp(self):
        self.rows = [InventoryItem('center', 'GGGGGH', 1, '中央·小麦'),
                     InventoryItem('one', 'GGHGGG', 1, '供体一'),
                     InventoryItem('two', 'GGYGGG', 1, '供体二')]
        self.target = Target('GGGGGG', 'exact')
        self.options = dict(max_rounds=2, max_seconds=3, max_combinations=10000,
                            beam_width=24, donor_pool_limit=14)

    def test_complete_report_roundtrip_includes_stock_ties_and_unicode(self):
        report = plan_route(self.rows, self.target, **self.options)
        self.assertEqual(_report_from_dict(asdict(report)), report)
        uncertain = plan_route([InventoryItem('x', 'XXXXXX'),
                                InventoryItem('g', 'GGGGGG', 2),
                                InventoryItem('y', 'YYYYYY', 2)], Target('GGGYYY'),
                               max_rounds=1, max_seconds=3, max_combinations=10000)
        self.assertEqual(_report_from_dict(asdict(uncertain)), uncertain)

    def test_child_matches_in_process_model_and_reaps_process(self):
        opened = []
        real_popen = subprocess.Popen

        def record(*args, **kwargs):
            opened.append(real_popen(*args, **kwargs))
            return opened[-1]

        original = tuple(self.rows)
        progress = []
        expected = plan_route(self.rows, self.target, **self.options)
        with patch('rust_breeder.planner_process.subprocess.Popen', record):
            actual = plan_route_process(self.rows, self.target, **self.options,
                                        progress=lambda n, depth: progress.append((n, depth)))
        self.assertEqual(tuple(self.rows), original)
        expected_data, actual_data = asdict(expected), asdict(actual)
        expected_data.pop('elapsed_seconds')
        actual_data.pop('elapsed_seconds')
        self.assertEqual(actual_data, expected_data)
        self.assertEqual(actual.best.center.name, '中央·小麦')
        self.assertTrue(progress)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].poll(), 0)
        self.assertTrue(opened[0].stdin.closed)
        self.assertTrue(opened[0].stdout.closed)

    def test_cancel_before_launch_and_during_search(self):
        with patch('rust_breeder.planner_process.subprocess.Popen') as start:
            result = plan_route_process(self.rows, self.target, cancel=lambda: True)
            self.assertTrue(result.cancelled)
            start.assert_not_called()
        rng = random.Random(9032)
        rows = [InventoryItem(str(index), ''.join(rng.choices('GYHWX', k=6)))
                for index in range(120)]
        opened = []
        real_popen = subprocess.Popen

        def record(*args, **kwargs):
            opened.append(real_popen(*args, **kwargs))
            return opened[-1]

        started = monotonic()
        with patch('rust_breeder.planner_process.subprocess.Popen', record):
            result = plan_route_process(rows, Target('HHHHHH'), max_seconds=12,
                                        max_combinations=2_000_000,
                                        cancel=lambda: monotonic() - started > .2)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.complete)
        self.assertFalse(result.minimum_proven)
        self.assertLess(monotonic() - started, 2.5)
        self.assertEqual(len(opened), 1)
        self.assertIsNotNone(opened[0].poll())

    def test_large_request_to_unresponsive_child_remains_cancellable_and_reaped(self):
        # More than a pipe buffer: the child deliberately never reads stdin.
        rows = tuple(InventoryItem(str(index), 'GGGGGH', 1, 'x' * 1000)
                     for index in range(1000))
        real_popen = subprocess.Popen
        for planner in (plan_route_process, plan_parallel_process):
            with self.subTest(planner=planner.__name__):
                opened = []

                def record(*args, **kwargs):
                    opened.append(real_popen(*args, **kwargs))
                    return opened[-1]

                started = monotonic()
                with patch('rust_breeder.planner_process._worker_command',
                           return_value=[sys.executable, '-c', 'import time; time.sleep(30)']), \
                        patch('rust_breeder.planner_process.subprocess.Popen', record):
                    result = planner(rows, self.target, max_seconds=12,
                                     cancel=lambda: monotonic() - started > .15)
                self.assertTrue(result.cancelled)
                self.assertLess(monotonic() - started, 2.5)
                self.assertIsNotNone(opened[0].poll())
                self.assertTrue(opened[0].stdin.closed)
                self.assertTrue(opened[0].stdout.closed)
                self.assertFalse(any(thread.name in ('route-request-writer', 'route-result-reader')
                                     for thread in active_threads()))
        self.assertEqual(rows[0].quantity, 1)

    def test_child_exiting_before_large_request_has_readable_error_and_full_cleanup(self):
        rows = tuple(InventoryItem(str(index), 'GGGGGH', 1, 'x' * 1000)
                     for index in range(1000))
        opened = []
        real_popen = subprocess.Popen

        def record(*args, **kwargs):
            opened.append(real_popen(*args, **kwargs))
            return opened[-1]

        with patch('rust_breeder.planner_process._worker_command',
                   return_value=[sys.executable, '-c', 'import os; os._exit(23)']), \
                patch('rust_breeder.planner_process.subprocess.Popen', record):
            with self.assertRaisesRegex(RuntimeError, '意外结束.*种子库未更改'):
                plan_route_process(rows, self.target)
        self.assertEqual(opened[0].poll(), 23)
        self.assertTrue(opened[0].stdin.closed)
        self.assertTrue(opened[0].stdout.closed)
        self.assertFalse(any(thread.name in ('route-request-writer', 'route-result-reader')
                             for thread in active_threads()))

    def test_non_object_worker_reply_is_rejected_and_child_is_reaped(self):
        with patch('rust_breeder.planner_process._worker_command', return_value=[
                sys.executable, '-c',
                'import sys; sys.stdin.readline(); print("[]", flush=True)']):
            with self.assertRaisesRegex(RuntimeError, '计算失败'):
                plan_route_process(self.rows, self.target)

    def test_worker_validation_error_is_reported_and_child_exits(self):
        with self.assertRaisesRegex(RuntimeError, '轮数'):
            plan_route_process(self.rows, self.target, max_rounds=0)
        with self.assertRaises(ValueError):
            plan_route_process(self.rows, self.target, not_an_option=True)

    def test_worker_entry_has_no_qt_import_and_rejects_non_json(self):
        # The worker must be reachable even before Qt startup, including in a
        # packaged windowed executable; importing GUI widgets here would bring
        # GIL and ownership issues back into the calculation process.
        result = subprocess.run(_worker_command(), input=b'not json\n',
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'"type":"error"', result.stdout)
        self.assertLess(len(result.stdout), MESSAGE_LIMIT)

    def test_compact_search_kernel_matches_independent_slot_vote_rule(self):
        rng = random.Random(10839)
        for count in range(9):
            for _ in range(80):
                center = ''.join(rng.choices('GYHWX', k=6))
                donors = tuple(''.join(rng.choices('GYHWX', k=6)) for _ in range(count))
                expected = []
                for position, own in enumerate(center):
                    votes = {gene: sum(10 if gene in 'WX' else 6 for row in donors
                                       if row[position] == gene) for gene in 'GYHWX'}
                    maximum = max(votes.values())
                    choices = ([gene for gene, weight in votes.items() if weight == maximum]
                               if maximum > (10 if own in 'WX' else 6) else [own])
                    if len(choices) != 1:
                        expected = None
                        break
                    expected.append(choices[0])
                expected = ''.join(expected) if expected is not None else None
                with self.subTest(center=center, donors=donors):
                    self.assertEqual(_deterministic_from_totals(center, _donor_totals(donors)), expected)
                    self.assertEqual(predict(center, donors).deterministic_result, expected)


if __name__ == '__main__':
    unittest.main()
