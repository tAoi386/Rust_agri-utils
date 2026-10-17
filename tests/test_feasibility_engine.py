"""Independent, reproducible checks of the planner's mathematical claims.

The oracle uses physical plant subsets rather than the planner's genotype groups.
It does not call predict(), match_range(), Target.matches(), or import production
gene weights. It intentionally specifies the same proposed game rules; passing
these checks establishes model consistency, not verification inside Rust itself.
"""

from __future__ import annotations

from collections import Counter
from itertools import combinations, product
import random
import unittest

from rust_breeder.engine import _multisets, plan
from rust_breeder.models import InventoryItem, Target


def physical_options(plants: list[str], chosen: tuple[int, ...], recipient: int):
    """Specify each slot directly, counting only selected donor plants."""
    options = []
    for slot in range(6):
        histogram = Counter(plants[index][slot] for index in chosen if index != recipient)
        votes = {
            gene: histogram[gene] * (10 if gene in "WX" else 6)
            for gene in "GYHWX"
        }
        maximum = max(votes.values())
        own = plants[recipient][slot]
        defense = 10 if own in "WX" else 6
        options.append(
            tuple(gene for gene in "GYHWX" if votes[gene] == maximum)
            if maximum > defense else (own,)
        )
    return tuple(options)


def physical_minimum(plants: list[str], target_genes: str, mode: str) -> int | None:
    """Search actual plant subsets of up to one recipient plus four donors."""
    wanted = Counter(target_genes)
    for size in range(1, min(5, len(plants)) + 1):
        for chosen in combinations(range(len(plants)), size):
            for recipient in chosen:
                outcomes = product(*physical_options(plants, chosen, recipient))
                if all(
                    ("".join(outcome) == target_genes if mode == "exact"
                     else Counter(outcome) == wanted)
                    for outcome in outcomes
                ):
                    return size
    return None


class FeasibilityEngineTests(unittest.TestCase):
    def test_bounded_multisets_against_exhaustive_cartesian_counts(self) -> None:
        checked = 0
        for group_count in range(6):
            for capacities in product(range(3), repeat=group_count):
                for total in range(min(8, sum(capacities)) + 2):
                    with self.subTest(capacities=capacities, total=total):
                        expected = {
                            counts
                            for counts in product(*(range(capacity + 1) for capacity in capacities))
                            if sum(counts) == total
                        }
                        actual = list(_multisets(capacities, total))
                        self.assertEqual(len(actual), len(set(actual)), "Duplicate combination")
                        self.assertEqual(set(actual), expected)
                    checked += 1
        self.assertEqual(checked, 2362)

    def test_minimum_against_independent_physical_plant_subsets(self) -> None:
        rng = random.Random(20261017)
        solved = 0
        for case in range(120):
            plant_count = rng.randint(3, 7)
            pool = [
                "".join(rng.choice("GYHWX") for _ in range(6))
                for _ in range(max(2, plant_count // 2))
            ]
            plants = [rng.choice(pool) for _ in range(plant_count)]
            recipient = rng.randrange(plant_count)
            donors = rng.sample(
                [index for index in range(plant_count) if index != recipient],
                rng.randrange(min(4, plant_count)),
            )
            options = physical_options(plants, (recipient, *donors), recipient)
            # Construct targets from real model outcomes to exercise achievable
            # cases, rather than mostly impossible random six-letter targets.
            target_genes = "".join(rng.choice(list(product(*options))))
            mode = "counts" if case % 2 else "exact"

            # Include duplicate inventory rows and quantities greater than one.
            inventory = []
            for genes, quantity in Counter(plants).items():
                if quantity > 1:
                    inventory.append(InventoryItem(str(len(inventory)), genes, quantity - 1))
                inventory.append(InventoryItem(str(len(inventory)), genes))

            with self.subTest(case=case, target=target_genes, mode=mode):
                expected = physical_minimum(plants, target_genes, mode)
                report = plan(inventory, Target(target_genes, mode), max_donors=4,
                              max_combinations=100_000)
                self.assertTrue(report.complete)
                self.assertEqual(report.found, expected is not None)
                self.assertEqual(report.minimum_proven, expected is not None)
                if expected is not None:
                    solved += 1
                    self.assertEqual(report.best.plant_count, expected)
                available = {item.id: item.quantity for item in inventory}
                for recipe in report.recipes:
                    used = Counter([recipe.center_id, *recipe.donor_ids])
                    self.assertTrue(all(count <= available[row_id] for row_id, count in used.items()))
                    self.assertLessEqual(recipe.donor_count, 4)
        self.assertEqual(solved, 119)


if __name__ == "__main__":
    unittest.main()
