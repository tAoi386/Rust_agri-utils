"""Regression tests for the conservative, single-generation breeding model."""

from __future__ import annotations

from collections import Counter
from itertools import combinations, islice, product
import random
import unittest

from rust_breeder.engine import plan, predict
from rust_breeder.models import InventoryItem, Target, normalize_genes


def oracle_candidates(center: str, donors: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Small direct specification, independent of engine totals/search helpers."""
    result = []
    for position, own_gene in enumerate(center):
        votes: dict[str, int] = {}
        for donor in donors:
            gene = donor[position]
            votes[gene] = votes.get(gene, 0) + (10 if gene in "WX" else 6)
        largest = max(votes.values(), default=0)
        defense = 10 if own_gene in "WX" else 6
        result.append(
            tuple(sorted(gene for gene, vote in votes.items() if vote == largest))
            if largest > defense else (own_gene,)
        )
    return tuple(result)


def oracle_matches(genes: str, target: Target) -> bool:
    return genes == target.genes if target.mode == "exact" else Counter(genes) == Counter(target.genes)


def oracle_minimum(inventory: list[InventoryItem], target: Target, max_donors: int) -> int | None:
    """Enumerate physical plants, including duplicates, instead of genotypes."""
    plants = [item.genes for item in inventory for _ in range(item.quantity)]
    for donor_count in range(min(max_donors, len(plants) - 1) + 1):
        for center_index, center in enumerate(plants):
            remaining = [index for index in range(len(plants)) if index != center_index]
            for chosen in combinations(remaining, donor_count):
                options = oracle_candidates(center, tuple(plants[index] for index in chosen))
                if all(oracle_matches("".join(outcome), target) for outcome in product(*options)):
                    return donor_count + 1
    return None


class PredictionTests(unittest.TestCase):
    def test_six_slot_input_and_convenient_separators(self) -> None:
        self.assertEqual(normalize_genes("y-y, y / g_g g"), "YYYGGG")
        for invalid in ("YYY", "YYYYGGG", "YYYGGZ", "YYYGG?"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_genes(invalid)

    def test_empty_soil_has_no_vote(self) -> None:
        prediction = predict("GYHWXG", [])
        self.assertEqual(prediction.deterministic_result, "GYHWXG")
        self.assertTrue(all(sum(slot.weights.values()) == 0 for slot in prediction.slots))
        self.assertTrue(all(not slot.overwritten for slot in prediction.slots))

    def test_recipient_does_not_vote(self) -> None:
        # Including the recipient would make G and Y tie at 1.2.
        prediction = predict("GGGGGG", ["GGGGGG", "YYYYYY", "YYYYYY"])
        self.assertEqual(prediction.deterministic_result, "YYYYYY")
        self.assertEqual(prediction.slots[0].weights["G"], 6)
        self.assertEqual(prediction.slots[0].weights["Y"], 12)

    def test_replacement_requires_strictly_greater_total(self) -> None:
        cases = (
            ("GGGGGG", ["YYYYYY"], "GGGGGG"),
            ("XXXXXX", ["WWWWWW"], "XXXXXX"),
            ("GGGGGG", ["XXXXXX"], "XXXXXX"),
            ("XXXXXX", ["GGGGGG", "GGGGGG"], "GGGGGG"),
            ("HHHHHH", ["GGGGGG", "YYYYYY"], "HHHHHH"),
        )
        for center, donors, expected in cases:
            with self.subTest(center=center, donors=donors):
                self.assertEqual(predict(center, donors).deterministic_result, expected)

    def test_two_matching_green_donors_outvote_one_red(self) -> None:
        prediction = predict("XXXXXX", ["GGGGGG", "GGGGGG", "WWWWWW"])
        self.assertEqual(prediction.deterministic_result, "GGGGGG")
        self.assertEqual(prediction.slots[0].weights["G"], 12)
        self.assertEqual(prediction.slots[0].weights["W"], 10)

    def test_ties_are_alternatives_without_invented_probability(self) -> None:
        prediction = predict("XXXXXX", ["GGGGGG"] * 2 + ["YYYYYY"] * 2)
        self.assertIsNone(prediction.deterministic_result)
        self.assertEqual(prediction.possible_outcome_count, 64)
        self.assertEqual(prediction.uncertain_slots, 6)
        self.assertTrue(all(set(slot.candidates) == {"G", "Y"} for slot in prediction.slots))
        self.assertEqual(len(prediction.outcomes(3)), 3)
        self.assertEqual(prediction.outcomes(0), ())
        self.assertTrue(prediction.possible_for(Target("GGGYYY", "exact")))
        self.assertFalse(prediction.guaranteed_for(Target("GGGYYY", "exact")))
        self.assertEqual(prediction.match_range(Target("GGGYYY", "exact")), (0, 6))
        # A tied result could be six G or six Y, so 3G+3Y is not guaranteed.
        self.assertEqual(prediction.match_range(Target("GGGYYY", "counts")), (3, 6))
        self.assertFalse(prediction.guaranteed_for(Target("GGGYYY", "counts")))

    def test_eight_donors_can_create_exact_red_green_weight_tie(self) -> None:
        prediction = predict("XXXXXX", ["GGGGGG"] * 5 + ["WWWWWW"] * 3)
        self.assertEqual(prediction.slots[0].weights["G"], 30)
        self.assertEqual(prediction.slots[0].weights["W"], 30)
        self.assertEqual(set(prediction.slots[0].candidates), {"G", "W"})
        with self.assertRaises(ValueError):
            predict("XXXXXX", ["GGGGGG"] * 9)

    def test_exact_and_count_targets_have_different_meanings(self) -> None:
        genes = "GYGYGY"
        self.assertTrue(Target("YYYGGG", "counts").matches(genes))
        self.assertFalse(Target("YYYGGG", "exact").matches(genes))
        self.assertTrue(predict(genes, []).guaranteed_for(Target("YYYGGG", "counts")))
        self.assertFalse(predict(genes, []).possible_for(Target("YYYGGG", "exact")))

    def test_prediction_and_match_ranges_against_cartesian_oracle(self) -> None:
        rng = random.Random(1729)
        for case in range(30):
            center = "".join(rng.choice("GYHWX") for _ in range(6))
            donors = tuple("".join(rng.choice("GYHWX") for _ in range(6)) for _ in range(rng.randrange(7)))
            expected = oracle_candidates(center, donors)
            prediction = predict(center, donors)
            self.assertEqual(tuple(map(set, prediction.candidates)), tuple(map(set, expected)), case)
            outcomes = ["".join(value) for value in product(*expected)]
            for mode in ("exact", "counts"):
                target = Target("".join(rng.choice("GYHWX") for _ in range(6)), mode)
                if mode == "exact":
                    scores = [sum(a == b for a, b in zip(value, target.genes)) for value in outcomes]
                else:
                    desired = Counter(target.genes)
                    scores = [sum((Counter(value) & desired).values()) for value in outcomes]
                self.assertEqual(prediction.match_range(target), (min(scores), max(scores)), (case, mode))


class SearchTests(unittest.TestCase):
    def assert_inventory_respected(self, recipes, inventory) -> None:
        available = {item.id: item.quantity for item in inventory}
        for recipe in recipes:
            required = Counter([recipe.center_id, *recipe.donor_ids])
            self.assertLessEqual(recipe.donor_count, 8)
            self.assertEqual(recipe.plant_count, len(recipe.donors) + 1)
            self.assertTrue(all(count <= available[item_id] for item_id, count in required.items()))

    def test_existing_target_requires_zero_donors(self) -> None:
        inventory = [InventoryItem("ideal", "GYGYGY"), InventoryItem("spare", "XXXXXX", 5)]
        report = plan(inventory, Target("YYYGGG", "counts"))
        self.assertTrue(report.complete)
        self.assertTrue(report.minimum_proven)
        self.assertTrue(report.found)
        self.assertEqual(report.best.center_id, "ideal")
        self.assertEqual(report.best.donor_count, 0)
        self.assertEqual(report.best.plant_count, 1)

    def test_two_donor_minimal_recipe_keeps_recipient_slots(self) -> None:
        inventory = [
            InventoryItem("center", "GYYGGG"),
            InventoryItem("a", "YHYGGG"),
            InventoryItem("b", "YGGGGG"),
        ]
        report = plan(inventory, Target("YYYGGG", "exact"))
        self.assertTrue(report.minimum_proven)
        self.assertEqual(report.best.center_id, "center")
        self.assertEqual(set(report.best.donor_ids), {"a", "b"})
        self.assertEqual(report.best.prediction.deterministic_result, "YYYGGG")
        self.assertEqual(report.best.plant_count, 3)
        self.assert_inventory_respected(report.recipes, inventory)
        insufficient = plan(inventory, Target("YYYGGG", "exact"), max_donors=1)
        self.assertTrue(insufficient.complete)
        self.assertFalse(insufficient.found)

    def test_center_consumes_one_copy_and_duplicate_rows_remain_allocatable(self) -> None:
        inventory = [
            InventoryItem("first-copy", "GGGGGG", 1),
            InventoryItem("second-copy", "GGGGGG", 2),
            InventoryItem("red", "XXXXXX", 2),
        ]
        report = plan(inventory, Target("HHHHHH", "exact"), max_donors=4, max_results=100)
        self.assertEqual(report.reason, "exhausted")
        self.assert_inventory_respected(report.recipes, inventory)
        single = plan([InventoryItem("only", "XXXXXX")], Target("GGGGGG", "exact"))
        self.assertEqual(single.evaluated, 1)
        self.assertEqual(single.searched_max_donors, 0)
        self.assertEqual(single.best.donor_ids, ())
        self.assertFalse(single.found)

    def test_unavailable_copies_are_not_assumed(self) -> None:
        inventory = [InventoryItem("red", "XXXXXX"), InventoryItem("green", "GGGGGG")]
        report = plan(inventory, Target("HHHHHH", "exact"), max_results=100)
        self.assertTrue(report.complete)
        self.assertEqual(report.searched_max_donors, 1)
        self.assert_inventory_respected(report.recipes, inventory)
        self.assertTrue(all(recipe.plant_count <= 2 for recipe in report.recipes))

    def test_empty_inventory_and_invalid_search_inputs(self) -> None:
        report = plan([], Target())
        self.assertEqual(report.reason, "empty_inventory")
        self.assertEqual(report.recipes, ())
        self.assertTrue(report.complete)
        self.assertFalse(report.minimum_proven)
        for quantity in (0, -1, True, 1.5):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                InventoryItem("bad", "GGGGGG", quantity)
        for max_donors in (-1, 9):
            with self.subTest(max_donors=max_donors), self.assertRaises(ValueError):
                plan([], Target(), max_donors=max_donors)
        for kwargs in ({"max_combinations": 0}, {"max_results": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                plan([], Target(), **kwargs)
        with self.assertRaises(ValueError):
            plan([InventoryItem("same", "GGGGGG"), InventoryItem("same", "YYYYYY")], Target())

    def test_bounded_search_keeps_results_without_claiming_minimum(self) -> None:
        inventory = [InventoryItem("g", "GGGGGG", 3), InventoryItem("y", "YYYYYY", 3)]
        report = plan(inventory, Target("GGGGGG", "exact"), max_combinations=1)
        self.assertEqual(report.reason, "limit")
        self.assertEqual(report.evaluated, 1)
        self.assertFalse(report.complete)
        self.assertFalse(report.minimum_proven)
        self.assertIsNotNone(report.best)
        self.assertIn("未证明最少", report.summary)

    def test_large_inventory_reaches_search_cap_without_recursion_failure(self) -> None:
        # All genotypes begin with G; no zero/one-donor plan can become XXXXXX.
        # After all 1100 zero-donor cases, the first recipient must examine more
        # than 1000 different donor choices before the explicit cap is reached.
        inventory = [
            InventoryItem(str(index), "".join(genes))
            for index, genes in enumerate(islice(product("GYHWX", repeat=6), 1100))
        ]
        self.assertGreater(len({item.genes for item in inventory}), 1000)
        report = plan(inventory, Target("XXXXXX", "exact"), max_donors=1,
                      max_combinations=2200)
        self.assertEqual(report.reason, "limit")
        self.assertEqual(report.evaluated, 2200)
        self.assertEqual(report.searched_max_donors, 1)
        self.assertFalse(report.complete)
        self.assertFalse(report.minimum_proven)
        self.assert_inventory_respected(report.recipes, inventory)

    def test_cancel_before_work_and_mid_search_are_honest(self) -> None:
        inventory = [InventoryItem("g", "GGGGGG", 4), InventoryItem("y", "YYYYYY", 4)]
        report = plan(inventory, Target("HHHHHH", "exact"), cancel=lambda: True)
        self.assertTrue(report.cancelled)
        self.assertEqual(report.evaluated, 0)
        self.assertFalse(report.complete)
        self.assertFalse(report.minimum_proven)

        calls = 0

        def cancel_after_four() -> bool:
            nonlocal calls
            calls += 1
            return calls > 4

        report = plan(inventory, Target("HHHHHH", "exact"), cancel=cancel_after_four)
        self.assertTrue(report.cancelled)
        self.assertEqual(report.evaluated, 4)
        self.assertIsNotNone(report.best)
        self.assertFalse(report.complete)
        self.assertFalse(report.minimum_proven)
        self.assertIn("未证明最少", report.summary)

    def test_small_inventory_search_matches_independent_physical_copy_oracle(self) -> None:
        rng = random.Random(54321)
        examples = [
            ([InventoryItem("c", "GYYGGG"), InventoryItem("a", "YHYGGG"), InventoryItem("b", "YGGGGG")], Target("YYYGGG", "exact")),
            ([InventoryItem("a", "GGXYYY"), InventoryItem("b", "XGGYWY"), InventoryItem("c", "GWGYYH"), InventoryItem("d", "XXXXXX")], Target("GGGYYY", "counts")),
        ]
        for case in range(12):
            inventory = [
                InventoryItem(str(index), "".join(rng.choice("GYHX") for _ in range(6)), rng.randint(1, 2))
                for index in range(3)
            ]
            target = Target("".join(rng.choice("GYHX") for _ in range(6)), "exact" if case % 2 else "counts")
            examples.append((inventory, target))
        for case, (inventory, target) in enumerate(examples):
            with self.subTest(case=case, target=target):
                minimum = oracle_minimum(inventory, target, max_donors=3)
                report = plan(inventory, target, max_donors=3, max_combinations=100_000)
                self.assertTrue(report.complete)
                self.assertEqual(report.found, minimum is not None)
                self.assertEqual(report.minimum_proven, minimum is not None)
                if minimum is not None:
                    self.assertEqual(report.best.plant_count, minimum)
                self.assert_inventory_respected(report.recipes, inventory)


if __name__ == "__main__":
    unittest.main()
