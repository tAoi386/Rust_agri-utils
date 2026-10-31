"""Real-route discovery, conservative impossibility and isolated budgets."""
from dataclasses import replace
import json
import random
import subprocess
from threading import enumerate as active_threads
from time import monotonic
import unittest
from unittest.mock import patch

from rust_breeder.engine import predict
from rust_breeder.models import InventoryItem, Recipe, Target
from rust_breeder.parallel_models import ParallelReport, ParallelStep, ParallelWave
from rust_breeder.parallel import validate_parallel_report
from rust_breeder.planner_process import plan_targets_process
from rust_breeder.routes import RouteReport
from rust_breeder.target_discovery import (TargetDiscoveryReport, TargetOption,
    green_targets, plan_targets)


class TargetDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.rows = (InventoryItem('center', 'GYYGGG'),
                     InventoryItem('one', 'YHYGGG'),
                     InventoryItem('two', 'YGGGGG'))

    def options(self, report):
        return {x.target.genes.count('Y'): x for x in report.options}

    def test_every_yg_count_is_listed_once_and_owned_seeds_cost_no_rounds(self):
        rows = tuple(InventoryItem(str(n), 'Y' * n + 'G' * (6 - n), 2) for n in range(7))
        for mode in ('sequential', 'parallel'):
            with self.subTest(mode=mode):
                report = plan_targets(rows, mode=mode)
                self.assertEqual(len(report.options), 7)
                self.assertEqual({x.target.genes.count('Y') for x in report.options}, set(range(7)))
                self.assertTrue(all(x.status == 'already_owned' and x.selectable for x in report.options))
                self.assertTrue(all(x.rounds == 0 and x.plant_count == 0 for x in report.options))
                self.assertEqual(report.evaluated, 0)
                self.assertEqual(tuple(rows), rows)

    def test_one_click_returns_real_complete_recipe_without_spending_stock(self):
        for mode in ('sequential', 'parallel'):
            with self.subTest(mode=mode):
                report = plan_targets(self.rows, mode=mode, max_seconds=2,
                    max_combinations=12000, max_donors=2,
                    **({'max_rounds': 2} if mode == 'sequential' else {'max_waves': 2}))
                chosen = self.options(report)[3]
                self.assertEqual(chosen.status, 'reachable')
                self.assertTrue(chosen.selectable)
                self.assertEqual(chosen.rounds, 1)
                self.assertEqual(chosen.plant_count, 3)
                if mode == 'parallel':
                    validate_parallel_report(chosen.report, self.rows, chosen.target)
                    step = chosen.report.waves[0].steps[0]
                else:
                    step = chosen.report.steps[0]
                self.assertTrue(chosen.target.matches(step.expected_genes))
                self.assertEqual(sum(x.quantity for x in self.rows), 3)
                self.assertEqual(self.options(report)[2].status, 'already_owned')

    def test_missing_initial_green_locus_proves_all_targets_unreachable(self):
        rows = (InventoryItem('w', 'WYYYYY', 10), InventoryItem('x', 'XGGGGG', 10))
        with patch('rust_breeder.target_discovery.plan_route') as planner:
            report = plan_targets(rows)
        planner.assert_not_called()
        self.assertTrue(all(x.status == 'unreachable' and x.reason == 'missing_green_position'
                            and not x.selectable for x in report.options))

    def test_missing_yg_position_counts_are_not_confused_with_search_limits(self):
        report = plan_targets(self.rows, max_seconds=.05, max_combinations=1)
        options = self.options(report)
        self.assertEqual(options[4].status, 'unreachable')
        self.assertEqual(options[4].reason, 'missing_gene_positions')
        self.assertEqual(options[0].status, 'unknown')
        self.assertFalse(options[0].selectable)

    def test_bounded_exhaustive_failure_is_still_unknown_not_permanent_impossibility(self):
        rows = (InventoryItem('y', 'YYYYYH'), InventoryItem('g', 'GGGGGH'),
                InventoryItem('hy', 'HYYYYY'), InventoryItem('hg', 'HGGGGG'))
        failed = RouteReport((), (), False, True, 1, 'exhausted')
        with patch('rust_breeder.target_discovery.plan_route', return_value=failed):
            report = plan_targets(rows, objective='rounds')
        self.assertTrue(all(x.status == 'unknown' and not x.selectable for x in report.options))

    def test_seven_searches_share_one_time_and_combination_budget_fairly(self):
        rows = (InventoryItem('y', 'YYYYYH'), InventoryItem('g', 'GGGGGH'),
                InventoryItem('hy', 'HYYYYY'), InventoryItem('hg', 'HGGGGG'))
        clock = [100.0]
        calls = []
        progress = []
        def search(inventory, target, **options):
            calls.append((target, options))
            clock[0] += options['max_seconds']
            return RouteReport((), (), False, False, options['max_combinations'], 'limit')
        with patch('rust_breeder.target_discovery.monotonic', side_effect=lambda: clock[0]), \
                patch('rust_breeder.target_discovery.plan_route', side_effect=search):
            report = plan_targets(rows, max_seconds=14, max_combinations=100,
                                  progress=lambda n, total: progress.append((n, total)))
        self.assertEqual(len(calls), 7)
        self.assertTrue(all(abs(options['max_seconds'] - 2.0) < .000001 for _, options in calls))
        self.assertEqual(sum(options['max_combinations'] for _, options in calls), 100)
        self.assertEqual(report.evaluated, 100)
        self.assertAlmostEqual(report.elapsed_seconds, 14)
        self.assertEqual(progress[-1], (7, 7))
        self.assertEqual(sorted(n for n, _ in progress), [n for n, _ in progress])

    def test_reports_roundtrip_actual_owned_and_reachable_routes(self):
        for mode in ('sequential', 'parallel'):
            with self.subTest(mode=mode):
                report = plan_targets(self.rows, mode=mode, max_seconds=2,
                    max_combinations=12000, max_donors=2)
                payload = json.loads(json.dumps(report.to_dict()))
                self.assertEqual(TargetDiscoveryReport.from_dict(payload), report)

    def test_status_requires_complete_found_report_and_exactly_seven_unique_counts(self):
        with self.assertRaises(ValueError):
            TargetOption(Target(), 'reachable')
        with self.assertRaises(ValueError):
            TargetOption(Target('HHHGGG'), 'unknown')
        with self.assertRaises(ValueError):
            TargetDiscoveryReport(tuple(TargetOption(Target(), 'unknown') for _ in range(7)))
        for options in ({'max_seconds': float('nan')}, {'max_seconds': True},
                        {'max_combinations': 0}, {'max_rounds': 0}, {'objective': 'wrong'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                plan_targets(self.rows, **options)
        with self.assertRaises(ValueError):
            plan_targets(self.rows, mode='parallel', center_id='center')

    def test_already_owned_survives_fixed_center_preference_but_new_route_obeys_it(self):
        report = plan_targets(self.rows, center_id='two', max_donors=2,
                              max_rounds=1, max_seconds=2, max_combinations=10000)
        options = self.options(report)
        self.assertEqual(options[2].status, 'already_owned')
        if options[3].selectable:
            self.assertEqual(options[3].report.steps[0].recipe.center_id, 'two')
        with self.assertRaises(ValueError):
            plan_targets(self.rows, center_id='missing')

    def test_deterministic_first_round_suggestion_can_fill_another_target_card(self):
        rows = (InventoryItem('center', 'HYYYYY'), InventoryItem('a', 'YHYYYY'),
                InventoryItem('b', 'YYYYYH'), InventoryItem('g', 'GGGGGH'),
                InventoryItem('hg', 'HGGGGG'))
        calls = []
        def search(inventory, target, **options):
            calls.append(target.genes.count('Y'))
            recipe = Recipe(rows[0], rows[1:3], predict(rows[0].genes, [x.genes for x in rows[1:3]]), target)
            return RouteReport((), (recipe,), False, False, 1, 'limit')
        with patch('rust_breeder.target_discovery.plan_route', side_effect=search):
            report = plan_targets(rows, max_seconds=2)
        chosen = self.options(report)[6]
        self.assertTrue(chosen.selectable)
        self.assertEqual(chosen.reason, 'verified_prefix')
        self.assertEqual(chosen.rounds, 1)
        self.assertEqual(chosen.report.steps[0].expected_genes, 'YYYYYY')
        self.assertEqual(chosen.report.steps[0].recipe.target, chosen.target)
        self.assertFalse(chosen.report.minimum_proven)
        self.assertNotIn(6, calls)
        self.assertEqual(sum(x.quantity for x in rows), 5)

    def test_parallel_main_box_prefix_is_rebound_and_replayed_for_another_goal(self):
        rows = (InventoryItem('center', 'HYYYYY'), InventoryItem('a', 'YHYYYY'),
                InventoryItem('b', 'YYYYYH'), InventoryItem('c', 'GGGHYH'),
                InventoryItem('d', 'GGGYHY'))
        target = Target('YYYGGG')
        first = Recipe(rows[0], rows[1:3], predict(rows[0].genes, [x.genes for x in rows[1:3]]), target)
        produced = InventoryItem('intermediate', 'YYYYYY')
        second = Recipe(produced, rows[3:], predict(produced.genes, [x.genes for x in rows[3:]]), target)
        full = ParallelReport((
            ParallelWave(1, (ParallelStep(1, first, 'YYYYYY', 'intermediate'),)),
            ParallelWave(2, (ParallelStep(1, second, 'GGGYYY', 'result'),)),
        ), True, False, 2, 'found')
        def search(inventory, requested, **options):
            return full if requested.genes.count('Y') == 3 else ParallelReport((), False, False, 1, 'limit')
        with patch('rust_breeder.parallel.plan_parallel', side_effect=search):
            report = plan_targets(rows, mode='parallel', max_seconds=2)
        chosen = self.options(report)[6]
        self.assertTrue(chosen.selectable)
        self.assertEqual(chosen.rounds, 1)
        self.assertEqual(chosen.reason, 'verified_prefix')
        self.assertTrue(all(step.recipe.target == chosen.target for wave in chosen.report.waves for step in wave.steps))
        validate_parallel_report(chosen.report, rows, chosen.target)
        self.assertEqual(sum(x.quantity for x in rows), 5)

    def test_cancel_before_direct_search_is_reported_without_using_stock(self):
        report = plan_targets(self.rows, cancel=lambda: True)
        self.assertTrue(report.cancelled)
        self.assertTrue(all(x.status == 'unknown' for x in report.options))
        self.assertEqual(sum(x.quantity for x in self.rows), 3)


class TargetDiscoveryProcessTests(unittest.TestCase):
    def test_real_child_returns_selectable_full_routes_for_both_modes_and_exits(self):
        rows = (InventoryItem('a', 'GYYGGG'), InventoryItem('b', 'YHYGGG'), InventoryItem('c', 'YGGGGG'))
        for mode in ('sequential', 'parallel'):
            opened = []
            real_popen = subprocess.Popen
            def record(*args, **kwargs):
                opened.append(real_popen(*args, **kwargs))
                return opened[-1]
            with self.subTest(mode=mode), patch('rust_breeder.planner_process.subprocess.Popen', side_effect=record):
                report = plan_targets_process(rows, mode=mode, max_seconds=2,
                                               max_combinations=12000, max_donors=2)
                self.assertTrue(next(x for x in report.options if x.target.genes.count('Y') == 3).selectable)
                self.assertEqual(len(opened), 1)
                self.assertEqual(opened[0].poll(), 0)
                self.assertTrue(opened[0].stdin.closed and opened[0].stdout.closed)

    def test_child_cancel_before_and_during_large_search_reaps_process(self):
        with patch('rust_breeder.planner_process.subprocess.Popen') as start:
            report = plan_targets_process((), cancel=lambda: True)
            self.assertTrue(report.cancelled)
            start.assert_not_called()
        rng = random.Random(5585)
        rows = tuple(InventoryItem(str(i), ''.join(rng.choices('GYHWX', k=6))) for i in range(150))
        opened = []
        real_popen = subprocess.Popen
        def record(*args, **kwargs):
            opened.append(real_popen(*args, **kwargs))
            return opened[-1]
        started = monotonic()
        with patch('rust_breeder.planner_process.subprocess.Popen', side_effect=record):
            report = plan_targets_process(rows, max_seconds=12, max_combinations=200000,
                                           cancel=lambda: monotonic() - started > .2)
        self.assertTrue(report.cancelled)
        self.assertLess(monotonic() - started, 2.5)
        self.assertEqual(len(opened), 1)
        self.assertIsNotNone(opened[0].poll())
        self.assertFalse(any(t.name in ('route-result-reader', 'route-request-writer') for t in active_threads()))

    def test_child_invalid_settings_fail_without_stock_mutation(self):
        rows = (InventoryItem('a', 'YYYYYY'),)
        with self.assertRaisesRegex(RuntimeError, '参数|模式'):
            plan_targets_process(rows, mode='wrong')
        self.assertEqual(rows[0].quantity, 1)


if __name__ == '__main__':
    unittest.main()
