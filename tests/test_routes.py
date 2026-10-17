"""Physical-inventory oracle checks for the guided multi-round planner."""

from collections import Counter
from itertools import combinations
import random
import unittest

from rust_breeder.engine import plan
from rust_breeder.models import InventoryItem, Target
from rust_breeder.routes import plan_route


REAL_69_GENES = """YYGYYH WHYXGW WYYXYW XHXXHH XYHXYW WGYXYH XYYWYX WGYXYW XYGHYX XYYXYX
    XYYXHW XYYWYH XYYXXW XYYXYW WYGXYH GYYWHW XYYYWW GYYXYW XXGGGW WYHHYG WYYXGH GYYWYH
    HYYYYW GHYXYH WHGXGG HYGWXG WYGYYG WHXWGH XXYWGW XHHYXX XHGWYX WYHYYH XGGXYH XGHXHW
    YHYWHW WYYYGH WGXXGH WXGXGW XHHXGW XYWXGX WYHWXG WGGYHW WHWGGW XGGWHW HYYYYG XYGWHH
    WXGXGX WYGWYX WHHWYW XHGWWX GYYWHX XGHWGY XGGWGX WHGXGW WWHXHX WYYWHH WHWWHY XGYXHG
    HYHWHW WGHYYW XHHXGH GYWXXY WGGWYH WHYWYY WHYXYW XYYXGX XYGWYW WYHYGX WYGWHH""".split()


def oracle_outcome(center, donors):
    result = []
    for index, own in enumerate(center):
        votes = Counter()
        for donor in donors:
            gene = donor[index]
            votes[gene] += 10 if gene in "WX" else 6
        maximum = max(votes.values(), default=0)
        if maximum <= (10 if own in "WX" else 6):
            result.append(own)
        else:
            options = [gene for gene, score in votes.items() if score == maximum]
            if len(options) != 1:
                return None
            result.append(options[0])
    return "".join(result)


def oracle_route(rows, center_id, target, rounds=2, max_donors=4, clone_yield=1, objective="plants"):
    central = next(row for row in rows if row.id == center_id)
    stock = [row.genes for row in rows for _ in range(row.quantity - int(row.id == center_id))]
    if target.matches(central.genes):
        return (0, 0)
    frontier = [(central.genes, tuple(sorted(stock)), 0)]
    wins = []
    for depth in range(1, rounds + 1):
        successors = {}
        for center, available, spent in frontier:
            for count in range(0 if clone_yield > 1 else 1, min(max_donors, len(available)) + 1):
                choices = set(combinations(range(len(available)), count))
                for chosen in choices:
                    donors = tuple(available[i] for i in chosen)
                    outcome = oracle_outcome(center, donors)
                    if outcome is None:
                        continue
                    cost = spent + count + 1
                    if target.matches(outcome):
                        wins.append((cost, depth))
                        continue
                    remaining = tuple(sorted([gene for i, gene in enumerate(available) if i not in chosen]
                                             + [outcome] * (clone_yield - 1)))
                    key = outcome, remaining
                    successors[key] = min(successors.get(key, 10**9), cost)
        frontier = [(center, stock, cost) for (center, stock), cost in successors.items()]
    return min(wins, key=(lambda item: item if objective == "plants" else item[::-1])) if wins else None


class RouteTests(unittest.TestCase):
    def search(self, rows, target, **kwargs):
        options = dict(center_id=rows[0].id, max_rounds=2, max_donors=4,
                       beam_width=10_000, donor_pool_limit=100,
                       max_combinations=100_000, max_seconds=20)
        options.update(kwargs)
        return plan_route(rows, target, **options)

    def assert_physical_inventory(self, report, rows):
        stock = {row.id: [row.genes, row.quantity] for row in rows}
        last_output = None
        for step in report.steps:
            recipe = step.recipe
            if last_output:
                self.assertEqual(recipe.center_id, last_output)
            spent = Counter((recipe.center_id, *recipe.donor_ids))
            for item in (recipe.center, *recipe.donors):
                self.assertEqual(item.genes, stock[item.id][0])
            for item_id, amount in spent.items():
                self.assertIn(item_id, stock)
                self.assertLessEqual(amount, stock[item_id][1])
                stock[item_id][1] -= amount
            self.assertEqual(step.expected_genes, oracle_outcome(recipe.center.genes, [row.genes for row in recipe.donors]))
            self.assertNotIn(step.output_id, stock)
            stock[step.output_id] = [step.expected_genes, step.clone_yield]
            last_output = step.output_id

    def test_selected_center_and_same_genotype_rows_are_preserved(self):
        rows = [InventoryItem("first", "GGGGGG"), InventoryItem("selected", "GGGGGG"),
                InventoryItem("red", "XXXXXX")]
        report = self.search(rows, Target("XXXXXX", "exact"), center_id="selected")
        self.assertTrue(report.found)
        self.assertEqual(report.best.center_id, "selected")
        self.assertEqual(report.best.donor_ids, ("red",))
        single = plan(rows, Target("XXXXXX", "exact"), center_id="selected")
        self.assertEqual(single.best.center_id, "selected")
        self.assertEqual(single.best.donor_ids, ("red",))
        self.assert_physical_inventory(report, rows)

    def test_worse_intermediate_unlocks_second_round_without_inventing_clones(self):
        rows = [InventoryItem("center", "WYYGGY"), InventoryItem("a", "GYHXXX"),
                InventoryItem("b", "GHYXXX"), InventoryItem("c", "GGGGGY"),
                InventoryItem("d", "HHHGGY")]
        report = self.search(rows, Target("GGGYYY", "counts"))
        self.assertTrue(report.found, report.summary)
        self.assertTrue(report.minimum_proven)
        self.assertEqual(report.rounds, 2)
        self.assertEqual(report.plant_count, 6)
        self.assertEqual([step.expected_genes for step in report.steps], ["GYYXXX", "GYYGGY"])
        self.assertEqual(set(report.steps[0].recipe.donor_ids), {"a", "b"})
        self.assertEqual(set(report.steps[1].recipe.donor_ids), {"c", "d"})
        self.assert_physical_inventory(report, rows)
        insufficient = self.search(rows[:-1], Target("GGGYYY", "counts"))
        self.assertFalse(insufficient.found)
        self.assertTrue(insufficient.complete)

    def test_already_target_is_zero_rounds_and_lowercase_normalizes(self):
        rows = [InventoryItem("center", "wggyyy")]
        report = self.search(rows, Target("WGGYYY", "exact"))
        self.assertTrue(report.found)
        self.assertTrue(report.minimum_proven)
        self.assertEqual(report.reason, "already_target")
        self.assertEqual(report.rounds, 0)
        self.assertEqual(report.plant_count, 0)
        self.assertEqual(report.best.center.genes, "WGGYYY")

    def test_noop_is_not_an_actionable_fallback(self):
        rows = [InventoryItem("center", "GGGGGG"), InventoryItem("single", "YYYYYY")]
        report = self.search(rows, Target("HHHHHH", "exact"))
        self.assertFalse(report.found)
        self.assertIsNone(report.best)
        self.assertTrue(report.complete)

    def test_automatic_center_is_chosen_with_its_compatible_donors(self):
        rows = [InventoryItem("first", "GGHGGG"), InventoryItem("right", "GGGGGH"),
                InventoryItem("helper", "GGYGGG")]
        target = Target("GGGGGG", "exact")
        auto = self.search(rows, target, center_id=None)
        self.assertTrue(auto.found)
        self.assertEqual(auto.center_mode, "auto")
        self.assertEqual(auto.center_id, "right")
        self.assertEqual(set(auto.best.donor_ids), {"first", "helper"})
        self.assertFalse(self.search(rows, target).found)
        self.assert_physical_inventory(auto, rows)

    def test_fewer_plants_can_choose_more_rounds(self):
        rows = [InventoryItem(str(i), genes) for i, genes in enumerate([
            "XYXXGG", "YGXHGG", "YXXHGG", "XYYGGG", "YGHYGG", "XYYXGG", "GGHXGG", "GXHYGG",
        ])]
        target = Target("XXHXGG", "exact")
        plants = self.search(rows, target, max_donors=7, objective="plants")
        rounds = self.search(rows, target, max_donors=7, objective="rounds")
        self.assertEqual((plants.plant_count, plants.rounds), (5, 2))
        self.assertEqual((rounds.plant_count, rounds.rounds), (7, 1))
        self.assertTrue(plants.minimum_proven)
        self.assertTrue(rounds.minimum_proven)
        self.assert_physical_inventory(plants, rows)
        self.assert_physical_inventory(rounds, rows)

    def test_large_real_genotype_set_finds_complementary_four_donors(self):
        # Genotypes from the user's 96-row inventory, with private IDs/names
        # removed. Every type has just one physical copy in this regression.
        genes = """GHGXXH GHYXYH GYWXXY GYYWHW GYYWYH GYYXYW HYGWXG HYHWHW HYWHHH HYYWYW
            HYYYYG HYYYYW WGGWYH WGGXHH WGGXHX WGGYHW WGGYWH WGHYYW WGXXGH WGYXYH WGYXYW
            WHGXGG WHGXGW WHWGGW WHWWHY WHXWGH WHXXYW WHYWGH WHYWYY WHYXGW WHYXYW WWHXHX
            WWXXYX WXGXGW WXGXGX WXYWXX WYGGYH WYGWGH WYGWHH WYGWYG WYGWYX WYGXYH WYGYYG
            WYHHYG WYHWXG WYHXXW WYHYGX WYHYYH WYYWHH WYYWHW WYYWYH WYYXGH WYYXHH WYYXYW
            WYYYGH WYYYYW XGGWHW XGGXYH XGHXHW XGYXHG XHGWYX XHHXGH XHHXGW XHHXYW XHHYXX
            XHWWWX XHWYHH XHXXHH XHYWHW XHYWYY XWWXHH XXGGGW XXYWGY XYGHYX XYGWHH XYGXGH
            XYHXYW XYWXGX XYYWYH XYYWYX XYYXGX XYYXHW XYYXXW XYYXYW XYYXYX XYYYWW YHGXYW
            YHYWHW YXHYGX YYGWYH YYGXHX YYGYYH""".split()
        rows = [InventoryItem(str(i), genes) for i, genes in enumerate(genes)]
        report = self.search(rows, Target("GGGYYY", "counts"), center_id=None,
                             max_rounds=3, max_donors=8, donor_pool_limit=12,
                             beam_width=24, max_combinations=6_000, max_seconds=8)
        self.assertTrue(report.found, report.summary)
        self.assertEqual((report.rounds, report.plant_count), (1, 5))
        self.assertEqual(report.steps[0].expected_genes, "YYGYGG")
        self.assertFalse(report.minimum_proven)
        self.assertLessEqual(report.evaluated, 6_000)
        self.assert_physical_inventory(report, rows)

    def test_real_69_library_keeps_a_worse_intermediate_outside_the_beam(self):
        rows = [InventoryItem(str(i), genes) for i, genes in enumerate(REAL_69_GENES)]
        target = Target("YYYGGG", "counts")
        report = self.search(rows, target, center_id=None, max_rounds=3, max_donors=8,
                             donor_pool_limit=14, beam_width=24,
                             max_combinations=120_000, max_seconds=12)
        self.assertTrue(report.found, report.summary)
        self.assertEqual((report.rounds, report.plant_count), (2, 8))
        score = lambda genes: sum((Counter(genes) & Counter(target.genes)).values())
        self.assertLess(score(report.steps[0].expected_genes), score(report.best.center.genes))
        self.assertTrue(target.matches(report.steps[-1].expected_genes))
        self.assertFalse(report.minimum_proven)
        self.assertLessEqual(report.evaluated, 120_000)
        self.assert_physical_inventory(report, rows)

    def test_real_actual_result_can_continue_with_five_donors_and_reserved_final_pair(self):
        # A hypothetical confirmed outcome after the old three-plant round.
        # The fixture is independent of the user's saved project or actual input.
        consumed = {"WHGXGG", "WYGYYG", "WYYYGH"}
        rows = [InventoryItem(str(i), genes) for i, genes in enumerate(REAL_69_GENES)
                if genes not in consumed]
        rows.append(InventoryItem("actual", "WYGYGG"))
        target = Target("YYYGGG", "counts")
        report = self.search(rows, target, center_id="actual", max_rounds=3, max_donors=8,
                             donor_pool_limit=14, beam_width=24,
                             max_combinations=120_000, max_seconds=12)
        self.assertTrue(report.found, report.summary)
        self.assertEqual(report.best.center_id, "actual")
        self.assertEqual((report.rounds, report.plant_count), (2, 9))
        self.assertEqual([step.recipe.donor_count for step in report.steps], [5, 2])
        self.assertTrue(target.matches(report.steps[-1].expected_genes))
        self.assertFalse(report.minimum_proven)
        self.assertLessEqual(report.evaluated, 120_000)
        self.assert_physical_inventory(report, rows)

    def test_backward_route_respects_rounds_donors_clone_yield_and_reserved_stock(self):
        genes = "WYGYGG GHYXYH GYWXXY WYHHYG XXGGGW HYGWXG HYYYYG YYGYYH".split()
        rows = [InventoryItem(str(i), genes) for i, genes in enumerate(genes)]
        target = Target("YYYGGG", "counts")
        for max_rounds, max_donors, clone_yield in ((1, 5, 1), (2, 4, 1), (2, 5, 3)):
            with self.subTest(rounds=max_rounds, donors=max_donors, clones=clone_yield):
                expected = oracle_route(rows, "0", target, rounds=max_rounds,
                                        max_donors=max_donors, clone_yield=clone_yield)
                report = self.search(rows, target, max_rounds=max_rounds,
                                     max_donors=max_donors, clone_yield=clone_yield)
                self.assertTrue(report.complete, report.limits)
                self.assertEqual(report.found, expected is not None)
                if expected:
                    self.assertEqual((report.plant_count, report.rounds), expected)
                self.assertTrue(all(step.recipe.donor_count <= max_donors for step in report.steps))
                self.assertTrue(all(step.clone_yield == clone_yield for step in report.steps))
                self.assert_physical_inventory(report, rows)

    def test_cancellation_and_limits_never_claim_minimum(self):
        rows = [InventoryItem("center", "XXXXXX"), InventoryItem("g", "GGGGGG", 4),
                InventoryItem("y", "YYYYYY", 4)]
        cancelled = self.search(rows, Target("GGGYYY"), cancel=lambda: True)
        self.assertTrue(cancelled.cancelled)
        self.assertEqual(cancelled.evaluated, 0)
        self.assertFalse(cancelled.complete)
        calls = 0

        def cancel_during_search():
            nonlocal calls
            calls += 1
            return calls >= 25

        stopped = self.search(rows, Target("GGGYYY"), cancel=cancel_during_search)
        self.assertTrue(stopped.cancelled)
        self.assertFalse(stopped.minimum_proven)
        self.assertFalse(stopped.complete)
        limited = self.search(rows, Target("GGGYYY"), max_combinations=1)
        self.assertFalse(limited.minimum_proven)
        self.assertFalse(limited.complete)
        self.assertLessEqual(limited.evaluated, 1)
        self.assertIn("未穷尽", limited.summary)

    def test_pool_and_beam_limits_are_reported(self):
        rows = [InventoryItem("center", "WYYGGY"), InventoryItem("a", "GYHXXX"),
                InventoryItem("b", "GHYXXX"), InventoryItem("c", "GGGGGY"),
                InventoryItem("d", "HHHGGY")]
        report = self.search(rows, Target("HHHHHH", "exact"), donor_pool_limit=2, beam_width=1)
        self.assertFalse(report.complete)
        self.assertFalse(report.minimum_proven)
        self.assertIn("donor_pool", report.limits)

    def test_ties_cannot_be_used_as_guaranteed_intermediate(self):
        rows = [InventoryItem("center", "XXXXXX"), InventoryItem("g", "GGGGGG", 2),
                InventoryItem("y", "YYYYYY", 2)]
        report = self.search(rows, Target("GGGYYY", "exact"))
        self.assertFalse(report.found)
        self.assertTrue(report.complete)
        self.assertFalse(report.steps)

    def test_small_physical_copy_oracle_with_both_objectives_and_clone_yield(self):
        rng = random.Random(9438)
        for case in range(12):
            # Three varying slots make reachable multi-round outcomes common;
            # the remaining positions still exercise full six-slot model data.
            rows = [InventoryItem(str(i), "".join(rng.choice("GYHX") for _ in range(3)) + "GGG",
                                  rng.randint(1, 2)) for i in range(3)]
            target = Target("".join(rng.choice("GYHX") for _ in range(3)) + "GGG", "exact")
            for objective in ("plants", "rounds"):
                clone_yield = 1 + case % 2
                with self.subTest(case=case, objective=objective):
                    expected = oracle_route(rows, "0", target, max_donors=3,
                                            clone_yield=clone_yield, objective=objective)
                    report = self.search(rows, target, max_donors=3,
                                         clone_yield=clone_yield, objective=objective)
                    self.assertTrue(report.complete, report.limits)
                    self.assertEqual(report.found, expected is not None)
                    if expected:
                        self.assertEqual((report.plant_count, report.rounds), expected)
                        self.assertTrue(report.minimum_proven)
                    self.assert_physical_inventory(report, rows)

    def test_invalid_settings(self):
        rows = [InventoryItem("center", "GGGGGG")]
        for options in ({"clone_yield": 0}, {"max_rounds": 0}, {"beam_width": 0},
                        {"donor_pool_limit": 0}, {"max_combinations": 0},
                        {"max_seconds": 0}, {"objective": "luck"}, {"center_id": "missing"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.search(rows, Target(), **options)


if __name__ == "__main__":
    unittest.main()
