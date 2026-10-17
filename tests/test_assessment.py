"""Checks of entered observations, independent of breeding predictions."""

from collections import Counter
from itertools import product
import unittest

from rust_breeder.assessment import assess_observed
from rust_breeder.models import Target


class AssessmentTests(unittest.TestCase):
    def test_counts_accepts_different_order(self) -> None:
        result = assess_observed("GYGYGY", Target("YYYGGG"))
        self.assertEqual(result.genes, "GYGYGY")
        self.assertTrue(result.matched)
        self.assertEqual(result.matched_count, 6)
        self.assertEqual(result.missing, {})
        self.assertEqual(result.excess, {})
        self.assertEqual(result.wrong_positions, ())
        self.assertIn("符合当前目标", result.headline)
        self.assertIn("顺序不限", result.detail)

    def test_counts_reports_one_gene_short(self) -> None:
        result = assess_observed("GGGGYY", Target("YYYGGG"))
        self.assertFalse(result.matched)
        self.assertEqual(result.matched_count, 5)
        self.assertEqual(result.missing, {"Y": 1})
        self.assertEqual(result.excess, {"G": 1})
        self.assertEqual(result.wrong_positions, ())
        self.assertIn("尚未达标", result.headline)
        self.assertIn("还缺：1Y", result.detail)
        self.assertIn("多出：1G", result.detail)

    def test_counts_reports_all_six_missing(self) -> None:
        result = assess_observed("XXXXXX", Target("YYYYYY"))
        self.assertFalse(result.matched)
        self.assertEqual(result.matched_count, 0)
        self.assertEqual(result.missing, {"Y": 6})
        self.assertEqual(result.excess, {"X": 6})

    def test_reports_only_nonzero_differences_in_novice_order(self) -> None:
        result = assess_observed("WWWXXX", Target("YYGGHH"))
        self.assertEqual(list(result.missing.items()), [("Y", 2), ("G", 2), ("H", 2)])
        self.assertEqual(list(result.excess.items()), [("W", 3), ("X", 3)])
        self.assertIn("还缺：2Y、2G、2H", result.detail)
        self.assertIn("多出：3W、3X", result.detail)

    def test_exact_same_counts_in_wrong_order_is_not_a_match(self) -> None:
        result = assess_observed("GYGYGY", Target("YYYGGG", "exact"))
        self.assertFalse(result.matched)
        self.assertEqual(result.matched_count, 2)
        self.assertEqual(result.wrong_positions, (1, 3, 4, 6))
        self.assertEqual(result.missing, {})
        self.assertEqual(result.excess, {})
        self.assertIn("固定顺序", result.detail)
        for index, actual, desired in ((1, "G", "Y"), (3, "G", "Y"),
                                       (4, "Y", "G"), (6, "Y", "G")):
            self.assertIn(f"第 {index} 位：{actual}→{desired}", result.detail)
        self.assertNotIn("顺序不限", result.detail)

    def test_exact_reports_first_and_last_positions(self) -> None:
        result = assess_observed("GYYGGY", Target("YYYGGG", "exact"))
        self.assertEqual(result.wrong_positions, (1, 6))
        self.assertEqual(result.matched_count, 4)

    def test_exact_full_match(self) -> None:
        result = assess_observed("YYYYGG", Target("YYYYGG", "exact"))
        self.assertTrue(result.matched)
        self.assertEqual(result.matched_count, 6)
        self.assertEqual(result.wrong_positions, ())
        self.assertIn("6 个位置完全一致", result.detail)

    def test_exact_reports_quantity_differences_as_well_as_positions(self) -> None:
        result = assess_observed("GGGGYY", Target("YYYGGG", "exact"))
        self.assertEqual(result.missing, {"Y": 1})
        self.assertEqual(result.excess, {"G": 1})
        self.assertEqual(result.wrong_positions, (1, 2, 3, 5, 6))
        self.assertEqual(result.matched_count, 1)

    def test_custom_targets_can_include_any_supported_gene(self) -> None:
        for genes in ("WWXXXX", "HHHHHH", "YGHWXY", "XXXXXX"):
            for mode in ("exact", "counts"):
                with self.subTest(genes=genes, mode=mode):
                    result = assess_observed(genes, Target(genes, mode))
                    self.assertTrue(result.matched)
                    self.assertEqual(result.matched_count, 6)
                    self.assertEqual(result.missing, {})
                    self.assertEqual(result.excess, {})

    def test_spacing_and_supported_separators_are_normalized(self) -> None:
        result = assess_observed(" y, y，y/g、g - g\n", Target())
        self.assertEqual(result.genes, "YYYGGG")
        self.assertTrue(result.matched)

    def test_invalid_characters_are_not_silently_discarded(self) -> None:
        for genes in ("YYYGG?", "YYYGGG?", "YYYGGZ", "YYYGG1", "YYYGG🙂",
                      "YYY.GGG", "Y Y Y G G G!"):
            with self.subTest(genes=genes), self.assertRaises(ValueError):
                assess_observed(genes, Target())

    def test_incomplete_and_overlong_observations_are_rejected(self) -> None:
        for genes in ("", " ", "Y", "YYYGG", "YYYGGGG", "GGGGGGGGG"):
            with self.subTest(genes=genes), self.assertRaises(ValueError):
                assess_observed(genes, Target())

    def test_scores_and_differences_against_independent_oracle(self) -> None:
        # All 729 arrangements over three symbols include ordering errors,
        # partial matches, complete matches, and wholly unsuitable observations.
        target_genes = "YYGGHH"
        desired = Counter(target_genes)
        for chars in product("YGH", repeat=6):
            genes = "".join(chars)
            actual = Counter(genes)
            quantity_score = sum((actual & desired).values())
            for mode in ("counts", "exact"):
                with self.subTest(genes=genes, mode=mode):
                    result = assess_observed(genes, Target(target_genes, mode))
                    position_errors = tuple(i for i in range(1, 7)
                                            if genes[i - 1] != target_genes[i - 1])
                    expected_score = quantity_score if mode == "counts" else 6 - len(position_errors)
                    self.assertEqual(result.matched_count, expected_score)
                    self.assertEqual(result.matched, expected_score == 6)
                    self.assertEqual(result.missing, dict(desired - actual))
                    self.assertEqual(result.excess, dict(actual - desired))
                    self.assertEqual(result.wrong_positions,
                                     () if mode == "counts" else position_errors)


if __name__ == "__main__":
    unittest.main()
