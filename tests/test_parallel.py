"""Shared-stock DAGs, batch boundaries, actual results and recovery."""

from dataclasses import replace
from itertools import combinations
import json
from time import monotonic
import unittest
from unittest.mock import patch

from rust_breeder.engine import predict
from rust_breeder.models import InventoryItem, Recipe, Target
from rust_breeder.parallel import (plan_parallel, validate_parallel_report,
                                   ParallelState, ParallelStep, ParallelWave,
                                   ParallelReport, prepare_wave, complete_wave,
                                   current_wave, cancel_wave, undo_wave)
from rust_breeder.planner_process import plan_parallel_process


BRANCH_GENES = ('HGYGHG', 'HGGGYY', 'HYYGYG', 'YHYYHH',
                'GYHYYY', 'YYHYYY', 'YHYYHG')


def branch_stock():
    return tuple(InventoryItem(str(index), genes, name=f'{index + 1} 号苗')
                 for index, genes in enumerate(BRANCH_GENES))


def possible_two_donor_operations(rows):
    for center in rows:
        available = [x for x in rows if x.id != center.id]
        for donors in combinations(available, 2):
            outcome = predict(center.genes, [x.genes for x in donors]).deterministic_result
            if outcome is not None and outcome != center.genes:
                yield center, donors, outcome


class ParallelSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = branch_stock()
        cls.target = Target('YYYGGG')
        cls.report = plan_parallel(cls.rows, cls.target, max_waves=2,
                                   max_donors=2, max_seconds=3)

    def test_real_two_branch_merge_solves_any_order_3g3y(self):
        # This stock has no one-batch or two-batch central-chain solution.
        # It needs independent preparation of two donor clones.
        for center, donors, outcome in possible_two_donor_operations(self.rows):
            self.assertFalse(self.target.matches(outcome))
            used = {center.id, *(x.id for x in donors)}
            after = tuple(x for x in self.rows if x.id not in used) + (InventoryItem('made', outcome),)
            self.assertFalse(any(self.target.matches(result)
                                 for _, _, result in possible_two_donor_operations(after)))
        report = self.report
        self.assertTrue(report.found, report.summary)
        self.assertEqual(report.rounds, 2)
        self.assertEqual(len(report.waves[0].steps), 2)
        produced = {x.output_id for x in report.waves[0].steps}
        self.assertEqual(len(produced & set(report.waves[1].steps[0].recipe.donor_ids)), 2)
        self.assertEqual(report.plant_count, 9)
        final = validate_parallel_report(report, self.rows, self.target)
        self.assertTrue(any(self.target.matches(x.genes) for x in final))
        self.assertFalse(report.complete)
        self.assertFalse(report.minimum_proven)
        self.assertIn('intermediate_catalog', report.limits)

    def test_all_report_waves_replay_with_shared_inventory(self):
        rows = self.rows
        state = ParallelState(self.target)
        original = tuple(rows)
        for wave in self.report.waves:
            state = prepare_wave(state, rows, wave)
            self.assertEqual(current_wave(state, rows).number, state.next_wave)
            actuals = {x.box_id: (x.expected_genes, x.clone_yield) for x in wave.steps}
            done = complete_wave(state, rows, actuals, token=state.pending.token)
            state, rows = done.state, done.inventory
        self.assertEqual(len(state.waves), 2)
        self.assertTrue(any(self.target.matches(x.genes) for x in rows))
        self.assertEqual(self.rows, original)

    def test_main_box_is_respected_and_box_count_changes_schedule(self):
        report = plan_parallel(self.rows, self.target, main_box=3, max_waves=2,
                               max_donors=2, max_seconds=3)
        self.assertTrue(report.found)
        self.assertEqual(report.waves[-1].steps[0].box_id, 3)
        self.assertIn(3, {x.box_id for x in report.waves[0].steps})
        one = plan_parallel(self.rows, self.target, box_count=1, max_waves=2,
                            max_donors=2, max_seconds=3)
        self.assertFalse(one.found)

    def test_empty_already_target_cancel_and_budget_are_honest(self):
        empty = plan_parallel([], self.target)
        self.assertEqual(empty.reason, 'empty_inventory')
        self.assertTrue(empty.complete)
        target = plan_parallel([InventoryItem('ok', 'GYGYGY')], self.target)
        self.assertTrue(target.found)
        self.assertEqual(target.waves, ())
        self.assertEqual(target.reason, 'already_target')
        before = plan_parallel(self.rows, self.target, cancel=lambda: True)
        self.assertTrue(before.cancelled)
        limited = plan_parallel(self.rows, self.target, max_combinations=1, max_seconds=1)
        self.assertFalse(limited.found)
        self.assertFalse(limited.complete)
        self.assertLessEqual(limited.evaluated, 1)
        self.assertEqual(self.rows, branch_stock())

    def test_report_and_state_json_roundtrip_rebuild_predictions(self):
        restored = ParallelReport.from_dict(json.loads(json.dumps(self.report.to_dict(), ensure_ascii=False)))
        self.assertEqual(restored, self.report)
        state = prepare_wave(ParallelState(self.target), self.rows, self.report.waves[0])
        done = complete_wave(state, self.rows,
                             {x.box_id: (x.expected_genes, 2) for x in state.pending.wave.steps})
        saved = json.loads(json.dumps(done.state.to_dict(), ensure_ascii=False))
        self.assertEqual(ParallelState.from_dict(saved), done.state)
        saved['undo_checkpoint']['inventory_before'][0]['quantity'] += 1
        with self.assertRaises(ValueError):
            ParallelState.from_dict(saved)

    def test_intelligent_matching_protects_scarce_initial_gene_stock(self):
        rows = (InventoryItem('c', 'GGGGGH'), InventoryItem('a', 'GGHGGG'),
                InventoryItem('b', 'GGYGGG'), InventoryItem('pc', 'YGGGGG', 100),
                InventoryItem('pa', 'GGGGYG', 100), InventoryItem('pb', 'GGGGHG', 100))
        options = dict(max_waves=2, max_donors=2, max_seconds=2)
        quickest = plan_parallel(rows, Target('GGGGGG', 'exact'), objective='rounds', **options)
        intelligent = plan_parallel(rows, Target('GGGGGG', 'exact'), objective='stock', **options)
        self.assertTrue(intelligent.found)
        self.assertEqual(intelligent.objective, 'stock')
        self.assertEqual(intelligent.rounds, quickest.rounds)
        self.assertEqual(intelligent.plant_count, quickest.plant_count)
        self.assertLess(intelligent.stock_cost, quickest.stock_cost)
        self.assertAlmostEqual(intelligent.stock_cost, .03)
        self.assertIn('智能匹配（基于已有基因库）', intelligent.summary)

    def test_budget_during_search_and_unknown_objective_are_bounded(self):
        result = plan_parallel(self.rows, self.target, max_combinations=200,
                               max_seconds=.02, objective='stock')
        self.assertLessEqual(result.evaluated, 200)
        self.assertLess(result.elapsed_seconds, .5)
        self.assertFalse(result.minimum_proven)
        with self.assertRaises(ValueError):
            plan_parallel(self.rows, self.target, objective='unknown')


class ParallelTransactionTests(unittest.TestCase):
    def setUp(self):
        self.target = Target('GGGGGG', 'exact')
        self.rows = (InventoryItem('c', 'GGGGGH', 2, '中央'),
                     InventoryItem('a', 'GGHGGG', 2),
                     InventoryItem('b', 'GGYGGG', 2))
        c, a, b = self.rows
        recipe = Recipe(c, (a, b), predict(c.genes, (a.genes, b.genes)), self.target)
        self.wave = ParallelWave(1, (ParallelStep(1, recipe, 'GGGGGG', 'out1'),
                                     ParallelStep(2, recipe, 'GGGGGG', 'out2')))
        self.state = prepare_wave(ParallelState(self.target), self.rows, self.wave)

    def test_shared_inventory_cannot_be_spent_in_two_boxes_twice(self):
        too_few = tuple(replace(x, quantity=1) for x in self.rows)
        with self.assertRaisesRegex(ValueError, '共享库存'):
            prepare_wave(ParallelState(self.target), too_few, self.wave)
        self.assertEqual(too_few[0].quantity, 1)

    def test_same_batch_output_cannot_be_borrowed(self):
        step = self.wave.steps[0]
        clone = InventoryItem(step.output_id, step.expected_genes)
        recipe = Recipe(clone, step.recipe.donors,
                        predict(clone.genes, [x.genes for x in step.recipe.donors]), self.target)
        with self.assertRaisesRegex(ValueError, '下一批'):
            ParallelWave(1, (step, ParallelStep(2, recipe, 'GGGGGG', 'later')))

    def test_all_actuals_required_and_wrong_gene_blocks_atomically(self):
        for actuals in ({1: ('GGGGGG', 1)},
                        {1: ('GGGGGG', 1), 2: ('GGGGGH', 1)},
                        {1: ('GGGGGG', 0), 2: ('GGGGGG', 1)}):
            with self.assertRaises(ValueError):
                complete_wave(self.state, self.rows, actuals)
            self.assertEqual(self.state.waves, ())
            self.assertEqual(self.rows[0].quantity, 2)

    def test_token_duplicate_submission_stock_change_and_cancel(self):
        with self.assertRaisesRegex(ValueError, '旧结果'):
            complete_wave(self.state, self.rows, {1: ('GGGGGG', 1), 2: ('GGGGGG', 1)}, token='stale')
        changed = (replace(self.rows[0], quantity=3), *self.rows[1:])
        with self.assertRaisesRegex(ValueError, '已经变化'):
            current_wave(self.state, changed)
        cancelled = cancel_wave(self.state)
        self.assertIsNone(cancelled.pending)
        self.assertEqual(self.rows[0].quantity, 2)
        done = complete_wave(self.state, self.rows, {1: ('GGGGGG', 1), 2: ('GGGGGG', 3)})
        self.assertEqual({x.id: x.quantity for x in done.inventory}, {'out1': 1, 'out2': 3})
        with self.assertRaisesRegex(ValueError, '重复确认'):
            complete_wave(done.state, done.inventory, {1: ('GGGGGG', 1), 2: ('GGGGGG', 3)})

    def test_undo_restores_exact_stock_pending_and_blocks_later_stock_edits(self):
        done = complete_wave(self.state, self.rows, {1: ('GGGGGG', 1), 2: ('GGGGGG', 3)})
        recovered = undo_wave(done.state, done.inventory, token=self.state.pending.token)
        self.assertEqual(recovered.inventory, self.rows)
        self.assertEqual(recovered.state.pending, self.state.pending)
        self.assertEqual(recovered.state.waves, ())
        self.assertIsNone(recovered.state.undo_checkpoint)
        with self.assertRaises(ValueError):
            undo_wave(recovered.state, recovered.inventory)
        changed = (*done.inventory, InventoryItem('new', 'YYYYYY'))
        with self.assertRaisesRegex(ValueError, '库存已经变化'):
            undo_wave(done.state, changed)

    def test_mutated_saved_expected_gene_and_undo_forgery_rejected(self):
        data = self.state.to_dict()
        data['pending']['wave']['steps'][0]['expected_genes'] = 'YYYYYY'
        with self.assertRaisesRegex(ValueError, '预测'):
            ParallelState.from_dict(data)
        done = complete_wave(self.state, self.rows, {1: ('GGGGGG', 1), 2: ('GGGGGG', 1)})
        data = done.state.to_dict()
        data['waves'][0]['actuals'][0]['quantity'] = 9
        with self.assertRaisesRegex(ValueError, '校验'):
            ParallelState.from_dict(data)

    def test_actual_clone_count_replaces_estimate_and_future_stock_is_checked(self):
        predicted = replace(self.wave.steps[0], clone_yield=5)
        wave = ParallelWave(1, (predicted, self.wave.steps[1]))
        state = prepare_wave(ParallelState(self.target), self.rows, wave)
        done = complete_wave(state, self.rows, {1: ('GGGGGG', 1), 2: ('GGGGGG', 2)})
        self.assertEqual({x.id: x.quantity for x in done.inventory}, {'out1': 1, 'out2': 2})
        c = done.inventory[0]
        recipe = Recipe(c, (c,), predict(c.genes, [c.genes]), self.target)
        next_wave = ParallelWave(2, (ParallelStep(1, recipe, 'GGGGGG', 'next'),))
        with self.assertRaisesRegex(ValueError, '共享库存'):
            prepare_wave(done.state, done.inventory, next_wave)

    def test_full_report_replay_rejects_shared_intermediate_clone_double_spend(self):
        first = ParallelWave(1, (self.wave.steps[0],))
        clone = InventoryItem('out1', 'GGGGGG')
        recipe = Recipe(clone, (), predict(clone.genes, ()), self.target)
        second = ParallelWave(2, (ParallelStep(1, recipe, 'GGGGGG', 'final1'),
                                  ParallelStep(2, recipe, 'GGGGGG', 'final2')))
        report = ParallelReport((first, second), True, False, 3, 'found')
        with self.assertRaisesRegex(ValueError, '共享库存'):
            validate_parallel_report(report, self.rows, self.target)


class ParallelProcessTests(unittest.TestCase):
    def test_parallel_worker_roundtrip_and_route_compatibility(self):
        rows = branch_stock()
        options = dict(max_waves=2, max_donors=2, max_seconds=3,
                       max_combinations=120000, main_box=2)
        expected = plan_parallel(rows, Target(), **options)
        actual = plan_parallel_process(rows, Target(), **options)
        self.assertTrue(actual.found, actual.summary)
        left, right = expected.to_dict(), actual.to_dict()
        left.pop('elapsed_seconds')
        right.pop('elapsed_seconds')
        self.assertEqual(left, right)
        self.assertTrue(any(Target().matches(x.genes)
                            for x in validate_parallel_report(actual, rows, Target())))

    def test_parallel_cancel_and_bad_options_do_not_launch_or_change_stock(self):
        with patch('rust_breeder.planner_process.subprocess.Popen') as start:
            report = plan_parallel_process(branch_stock(), Target(), cancel=lambda: True)
            self.assertTrue(report.cancelled)
            start.assert_not_called()
        with self.assertRaises(ValueError):
            plan_parallel_process(branch_stock(), Target(), unsupported=True)
        with self.assertRaisesRegex(RuntimeError, '箱'):
            plan_parallel_process(branch_stock(), Target(), box_count=0)

    def test_cancellation_during_parallel_worker_reaps_child(self):
        import random
        import subprocess
        rng = random.Random(98)
        rows = [InventoryItem(str(index), ''.join(rng.choices('GYHWX', k=6))) for index in range(80)]
        opened = []
        real_popen = subprocess.Popen

        def start(*args, **kwargs):
            opened.append(real_popen(*args, **kwargs))
            return opened[-1]

        began = monotonic()
        with patch('rust_breeder.planner_process.subprocess.Popen', start):
            report = plan_parallel_process(rows, Target('HHHHHH'), max_seconds=12,
                                           cancel=lambda: monotonic() - began > .15)
        self.assertTrue(report.cancelled)
        self.assertLess(monotonic() - began, 2)
        self.assertEqual(len(opened), 1)
        self.assertIsNotNone(opened[0].poll())
        self.assertTrue(opened[0].stdout.closed)


if __name__ == '__main__':
    unittest.main()
