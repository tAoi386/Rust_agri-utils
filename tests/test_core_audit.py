"""Regression cases found while reviewing executable breeding routes."""

from collections import Counter
from dataclasses import replace
import unittest
from unittest.mock import patch

from rust_breeder.engine import predict
from rust_breeder.journey import JourneyState, prepare_round
from rust_breeder.models import InventoryItem, Recipe, Target
from rust_breeder.parallel import plan_parallel, validate_parallel_report
from rust_breeder.parallel_models import ParallelState, complete_wave, prepare_wave
from rust_breeder.routes import RouteReport, RouteStep, plan_route


class ExecutableRouteAuditTests(unittest.TestCase):
    def test_clone_estimate_does_not_offer_unexecutable_zero_donor_round(self):
        rows = (InventoryItem('c', 'GGGGGH'), InventoryItem('a', 'WGGGGG'),
                InventoryItem('b', 'GYGGGG'))
        target = Target('GGGGGG', 'exact')
        # Previously reported a full route beginning with no donors, which
        # prepare_round necessarily rejected in the sequential workspace.
        report = plan_route(rows, target, center_id='c', max_rounds=2,
                            clone_yield=2, max_donors=3, beam_width=1000,
                            donor_pool_limit=100, max_combinations=10000, max_seconds=4)
        self.assertFalse(report.found)
        self.assertFalse(report.steps)
        self.assertTrue(report.complete)
        self.assertEqual(rows[0].quantity, 1)
        # Keeping a plant that already meets the target remains a zero-round
        # operation and needs no donor allocation.
        ready = plan_route((InventoryItem('ready', target.genes),), target,
                           clone_yield=2, max_donors=0)
        self.assertTrue(ready.found)
        self.assertEqual(ready.rounds, 0)
        self.assertEqual(ready.best.donor_count, 0)

    def test_positive_donor_round_can_prepare_real_extra_clones(self):
        rows = (InventoryItem('c', 'GGGGGH'), InventoryItem('a', 'WGGGGG'),
                InventoryItem('b', 'GYGGGG', 2))
        target = Target('GGGGGG', 'exact')
        report = plan_route(rows, target, center_id='c', max_rounds=2,
                            clone_yield=2, max_donors=3, beam_width=1000,
                            donor_pool_limit=100, max_combinations=10000, max_seconds=4)
        self.assertTrue(report.found)
        self.assertEqual((report.rounds, report.plant_count), (2, 6))
        self.assertTrue(all(step.recipe.donor_count > 0 for step in report.steps))
        self.assertEqual(report.steps[0].expected_genes, rows[0].genes)
        pending = prepare_round(JourneyState(target, 'c'), rows, report.steps[0].recipe)
        self.assertIsNotNone(pending.pending)

    def test_parallel_replanning_three_batches_rebinds_every_future_clone_id(self):
        rows = tuple(InventoryItem(str(i), genes) for i, genes in enumerate(
            ('GHHYGY', 'YHGYGH', 'GHGHYH', 'HGHYHG', 'YYHGYY', 'HGGGHG', 'HYGHYH')))
        target = Target()
        state = ParallelState(target)
        stages = (
            ('HGGGHG', ('YHGYGH', 'HGHYHG')),
            ('HGGYHG', ('GHHYGY', 'GHGHYH')),
            ('GHGYHG', ('YYHGYY', 'HYGHYH')),
        )
        original = rows
        emitted_ids = set()

        def known_suffix(stock, goal, **options):
            # Keep the actual gene model and stock debits, while isolating the
            # fallback adapter from the bounded search's optional candidates.
            available = {row.id: row for row in stock}
            occupied = set(available)
            steps = []
            start = len(state.waves)
            center = next(row for row in available.values() if row.genes == stages[start][0])
            for number, (_, donor_genes) in enumerate(stages[start:], 1):
                donors = tuple(next(row for row in available.values() if row.genes == genes)
                               for genes in donor_genes)
                prediction = predict(center.genes, [row.genes for row in donors])
                outcome = prediction.deterministic_result
                self.assertIsNotNone(outcome)
                recipe = Recipe(center, donors, prediction, goal)
                output = f'__route_clone_{number}'
                while output in occupied:
                    output += '_'
                spent = Counter((center.id, *(row.id for row in donors)))
                available = {seed_id: replace(row, quantity=row.quantity - spent[seed_id])
                             for seed_id, row in available.items() if row.quantity > spent[seed_id]}
                center = InventoryItem(output, outcome)
                available[output] = center
                steps.append(RouteStep(recipe, outcome, output, 1))
            self.assertTrue(goal.matches(steps[-1].expected_genes))
            return RouteReport(tuple(steps), (), True, True, 1, 'found')

        for batch in range(1, 4):
            with patch('rust_breeder.parallel.plan_route', side_effect=known_suffix):
                report = plan_parallel(rows, target, max_combinations=1, max_seconds=1)
            self.assertTrue(report.found)
            self.assertEqual(report.rounds, 4 - batch)
            validate_parallel_report(report, rows, target)
            for previous, following in zip(report.waves, report.waves[1:]):
                self.assertEqual(previous.steps[0].output_id, following.steps[0].recipe.center_id)
            first = report.waves[0]
            self.assertNotIn(first.steps[0].output_id, emitted_ids)
            emitted_ids.add(first.steps[0].output_id)
            state = prepare_wave(state, rows, first)
            done = complete_wave(state, rows, {step.box_id: (step.expected_genes, 1) for step in first.steps})
            state, rows = done.state, done.inventory
            state = ParallelState.from_dict(state.to_dict())
        self.assertEqual(len(state.waves), 3)
        self.assertTrue(any(target.matches(row.genes) for row in rows))
        self.assertEqual(original[0].quantity, 1)
        self.assertEqual(len(emitted_ids), 3)


if __name__ == '__main__':
    unittest.main()
