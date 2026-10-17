"""Inventory-sensitive matching must compare complete, payable routes only."""

from dataclasses import replace
from fractions import Fraction
import unittest
from unittest.mock import patch

from rust_breeder.engine import predict
from rust_breeder.models import InventoryItem, Recipe, Target
from rust_breeder.routes import RouteReport, RouteStep, plan_route
from rust_breeder.stock_planning import plan_stock_route, stock_route_cost


class StockPlanningTests(unittest.TestCase):
    def setUp(self):
        self.target = Target("YYYGGG", "counts")
        self.rows = (InventoryItem("center", "XXXXXX", 10),
                     InventoryItem("rare-a", "HGGYYY"),
                     InventoryItem("rare-b", "GHGYYY"),
                     InventoryItem("rare-c", "GGHYYY"),
                     InventoryItem("many-a", "HYGYGY", 10),
                     InventoryItem("many-b", "GHGYGY", 10),
                     InventoryItem("many-c", "GYHYGY", 10))

    def candidate(self, donor_ids, evaluated=5):
        by_id = {row.id: row for row in self.rows}
        center = by_id["center"]
        donors = tuple(by_id[key] for key in donor_ids)
        prediction = predict(center.genes, [row.genes for row in donors])
        self.assertIsNotNone(prediction.deterministic_result)
        recipe = Recipe(center, donors, prediction, self.target)
        step = RouteStep(recipe, prediction.deterministic_result, "__new_clone", 1)
        return RouteReport((step,), (recipe,), True, True, evaluated, "found", minimum_proven=True)

    def test_matching_prefers_payable_abundant_genes_between_complete_candidates(self):
        rare = self.candidate(("rare-a", "rare-b", "rare-c"))
        abundant = self.candidate(("many-a", "many-b", "many-c"))
        self.assertEqual(stock_route_cost(rare.steps, self.rows), Fraction(31, 10))
        self.assertEqual(stock_route_cost(abundant.steps, self.rows), Fraction(4, 10))
        reports = {"plants": rare, "rounds": abundant}
        result = plan_stock_route(self.rows, self.target, center_id="center",
                                 _planner=lambda *args, objective, **kw: reports[objective])
        self.assertEqual(result.steps, abundant.steps)
        self.assertEqual(result.objective, "stock")
        self.assertIn("stock_comparison", result.limits)
        self.assertFalse(result.minimum_proven)
        self.assertFalse(result.shortest_rounds_proven)
        self.assertFalse(result.complete)
        self.assertIn("智能匹配（基于已有基因库）", result.summary)
        self.assertNotIn("按库存数量", result.summary)

    def test_same_genotype_rows_share_denominator_and_new_clones_are_not_charged_twice(self):
        rows = (InventoryItem("c", "XXXXXX", 10), InventoryItem("g1", "GGGGGG", 2),
                InventoryItem("g2", "GGGGGG", 3))
        recipe = Recipe(rows[0], (rows[1], rows[1], rows[2]),
                        predict(rows[0].genes, ("GGGGGG",) * 3), Target("GGGGGG"))
        generated = InventoryItem("__new_clone", "GGGGGG", 3)
        clone_recipe = Recipe(generated, (), predict(generated.genes, ()), recipe.target)
        self.assertEqual(stock_route_cost((recipe, clone_recipe), rows), Fraction(7, 10))

    def test_cost_rejects_spending_an_original_row_more_than_available(self):
        report = self.candidate(("rare-a", "rare-a", "rare-b", "rare-b", "rare-c"))
        self.assertTrue(self.target.matches(report.steps[0].expected_genes))
        with self.assertRaises(ValueError):
            stock_route_cost(report.steps, self.rows)
        result = plan_stock_route(self.rows, self.target, center_id="center",
                                 _planner=lambda *args, **kw: report)
        self.assertFalse(result.found)
        self.assertEqual(result.steps, ())
        self.assertIn("invalid_candidate", result.limits)

    def test_partial_or_falsely_labelled_found_route_cannot_replace_a_complete_route(self):
        complete = self.candidate(("many-a", "many-b", "many-c"))
        broken = replace(complete, steps=(replace(complete.steps[0], expected_genes="YYYYYY"),))
        reports = {"plants": broken, "rounds": complete}
        result = plan_stock_route(self.rows, self.target, center_id="center",
                                 _planner=lambda *args, objective, **kw: reports[objective])
        self.assertEqual(result.steps, complete.steps)
        self.assertTrue(result.found)
        self.assertIn("invalid_candidate", result.limits)

    def test_ambiguous_prediction_never_becomes_a_future_clone(self):
        rows = (InventoryItem("center", "YYYYYY"), InventoryItem("g", "GGGGGG", 2),
                InventoryItem("h", "HHHHHH", 2))
        donors = (rows[1], rows[1], rows[2], rows[2])
        recipe = Recipe(rows[0], donors, predict(rows[0].genes, [row.genes for row in donors]), self.target)
        self.assertIsNone(recipe.prediction.deterministic_result)
        report = RouteReport((RouteStep(recipe, None, "output", 1),), (recipe,), True, True, 5, "found")
        result = plan_stock_route(rows, self.target, center_id="center", _planner=lambda *args, **kw: report)
        self.assertFalse(result.found)
        self.assertEqual(result.steps, ())
        self.assertIn("invalid_candidate", result.limits)

    def test_passes_share_total_budget_time_and_monotone_progress(self):
        report = self.candidate(("many-a", "many-b", "many-c"))
        clock = [100.0]
        calls = []
        progress = []

        def planner(*args, objective, max_combinations, max_seconds, progress, **kw):
            calls.append((max_combinations, max_seconds))
            count = max_combinations // 2 if objective == "plants" else max_combinations
            progress(0, 1)
            progress(count, 1)
            clock[0] += max_seconds
            return replace(report, evaluated=count)

        with patch("rust_breeder.stock_planning.monotonic", side_effect=lambda: clock[0]):
            result = plan_stock_route(self.rows, self.target, center_id="center", max_combinations=120,
                                     max_seconds=12, _planner=planner,
                                     progress=lambda count, depth: progress.append(count))
        self.assertEqual(calls, [(60, 6.0), (90, 6.0)])
        self.assertEqual(result.evaluated, 120)
        self.assertEqual(result.elapsed_seconds, 12)
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(progress[-1], 120)

    def test_cancelled_comparison_never_applies_a_previously_found_candidate(self):
        report = self.candidate(("many-a", "many-b", "many-c"))
        cancelled = [False]

        def planner(*args, **kw):
            cancelled[0] = True
            return report

        result = plan_stock_route(self.rows, self.target, center_id="center", _planner=planner,
                                 cancel=lambda: cancelled[0])
        self.assertTrue(result.cancelled)
        self.assertFalse(result.found)
        self.assertEqual(result.steps, ())
        self.assertEqual(result.evaluated, report.evaluated)

    def test_real_route_entry_and_existing_target_use_stock_objective(self):
        rows = (InventoryItem("c", "GYYGGG"), InventoryItem("a", "YHYGGG", 2),
                InventoryItem("b", "YGGGGG", 2))
        result = plan_route(rows, Target("YYYGGG"), center_id="c", max_rounds=1, objective="stock")
        self.assertTrue(result.found)
        self.assertEqual(result.steps[-1].expected_genes, "YYYGGG")
        self.assertEqual(result.objective, "stock")
        self.assertLessEqual(result.evaluated, 60_000)
        existing = plan_route((InventoryItem("goal", "YYYGGG"),), Target("YYYGGG"), objective="stock")
        self.assertTrue(existing.found)
        self.assertEqual(existing.rounds, 0)
        self.assertEqual(existing.reason, "already_target")


if __name__ == "__main__":
    unittest.main()
