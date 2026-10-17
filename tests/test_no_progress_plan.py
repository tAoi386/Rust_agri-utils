"""Distinguish keeping a good clone from a useful breeding calculation."""

from __future__ import annotations

import unittest

from rust_breeder.engine import plan, predict
from rust_breeder.models import InventoryItem, Target


FIRST_BATCH = (
    "XYHYYW", "WGYXHH", "XYGGHX", "YYGWYY", "XWGWGH", "HHWWGW", "YYWGHX",
)


class BeginnerInventoryTests(unittest.TestCase):
    def test_identical_clones_cannot_change_the_recipient(self):
        for count in range(9):
            with self.subTest(donors=count):
                prediction = predict("YYGWYY", ["YYGWYY"] * count)
                self.assertEqual(prediction.deterministic_result, "YYGWYY")
                self.assertFalse(any(slot.overwritten for slot in prediction.slots))

    def test_single_genotype_is_not_a_solution_to_a_different_target(self):
        for quantity in (1, 9):
            for target_genes in ("YYYGGG", "YYYYGG"):
                for mode in ("counts", "exact"):
                    with self.subTest(quantity=quantity, target=target_genes, mode=mode):
                        target = Target(target_genes, mode)
                        report = plan([InventoryItem("first", "YYGWYY", quantity)], target)
                        self.assertTrue(report.complete)
                        self.assertEqual(report.reason, "exhausted")
                        self.assertFalse(report.found)
                        self.assertFalse(report.minimum_proven)
                        self.assertTrue(all(not recipe.guaranteed for recipe in report.recipes))

    def test_an_existing_target_needs_no_donors(self):
        # Counts mode accepts any arrangement of the same three Y and three G.
        for genes, mode in (("YYYGGG", "exact"), ("GYYGYG", "counts")):
            with self.subTest(genes=genes, mode=mode):
                report = plan([InventoryItem("ready", genes)], Target("YYYGGG", mode))
                self.assertTrue(report.found)
                self.assertTrue(report.minimum_proven)
                self.assertEqual(report.best.donor_count, 0)
                self.assertTrue(report.best.guaranteed)

    def test_actual_first_batch_has_no_guaranteed_target_in_one_round(self):
        inventory = [InventoryItem(str(index), genes) for index, genes in enumerate(FIRST_BATCH)]
        report = plan(inventory, Target())
        self.assertTrue(report.complete)
        self.assertEqual(report.reason, "exhausted")
        self.assertFalse(report.found)
        self.assertEqual(report.best.worst_match, 4)
        self.assertEqual(report.best.best_match, 4)
        # The original quantity records are planning input, never consumed here.
        self.assertTrue(all(row.quantity == 1 for row in inventory))

    def test_expanding_the_same_first_batch_is_not_a_guaranteed_one_round_fix(self):
        # Nine of each includes every possible repetition within one nine-cell box.
        inventory = [InventoryItem(str(index), genes, 9) for index, genes in enumerate(FIRST_BATCH)]
        report = plan(inventory, Target(), max_combinations=100_000, max_results=1)
        self.assertTrue(report.complete)
        self.assertFalse(report.found)
        self.assertEqual(report.reason, "exhausted")
        self.assertLess(report.best.best_match, 6)

    def test_search_limit_does_not_prove_that_inventory_is_unsuitable(self):
        inventory = [InventoryItem(str(index), genes) for index, genes in enumerate(FIRST_BATCH)]
        report = plan(inventory, Target(), max_combinations=1)
        self.assertFalse(report.complete)
        self.assertEqual(report.reason, "limit")
        self.assertFalse(report.minimum_proven)


if __name__ == "__main__":
    unittest.main()
