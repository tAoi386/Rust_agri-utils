"""Placement instructions preserve old plans and spread new donors apart."""

import unittest
from itertools import combinations

from rust_breeder.planter import CORNER_FIRST, LEGACY_CROSS_FIRST, positions_for


class PlanterTests(unittest.TestCase):
    def test_two_donors_use_opposite_edges(self):
        self.assertEqual(positions_for(2), (1, 7))

    def test_four_donors_use_only_corners(self):
        self.assertEqual(set(positions_for(4)), {0, 2, 6, 8})

    def test_larger_plans_extend_without_moving_the_first_four(self):
        for count in range(5, 9):
            with self.subTest(count=count):
                positions = positions_for(count)
                self.assertEqual(positions, CORNER_FIRST[:count])
                self.assertEqual(len(set(positions)), count)
                self.assertNotIn(4, positions)
                self.assertEqual(positions[:4], positions_for(4))
        self.assertEqual(set(positions_for(8)), set(range(9)) - {4})

    def test_empty_and_single_donor_plans_are_valid(self):
        self.assertEqual(positions_for(0), ())
        self.assertEqual(positions_for(1), (1,))

    def test_explicit_corner_layout_preserves_saved_positions(self):
        for count in range(9):
            with self.subTest(count=count):
                self.assertEqual(positions_for(count, "corners"), CORNER_FIRST[:count])

    def test_automatic_layout_minimizes_donor_neighbors_for_each_count(self):
        # Independent grid geometry: touching includes the four diagonals.
        def adjacent(first, second):
            r1, c1 = divmod(first, 3)
            r2, c2 = divmod(second, 3)
            return max(abs(r1 - r2), abs(c1 - c2)) == 1

        def neighbor_pairs(positions):
            return sum(adjacent(first, second) for first, second in combinations(positions, 2))

        peripheral = tuple(index for index in range(9) if index != 4)
        for count in range(1, 9):
            with self.subTest(count=count):
                chosen = positions_for(count)
                self.assertEqual(len(set(chosen)), count)
                self.assertTrue(all(adjacent(index, 4) for index in chosen))
                minimum_pairs = min(neighbor_pairs(candidate)
                                    for candidate in combinations(peripheral, count))
                self.assertEqual(neighbor_pairs(chosen), minimum_pairs)

    def test_legacy_layout_keeps_cross_first_order(self):
        self.assertEqual(positions_for(8, "cross"), LEGACY_CROSS_FIRST)
        self.assertEqual(positions_for(2, "cross"), (1, 3))

    def test_invalid_count_and_layout_are_rejected(self):
        for count in (-1, 9, True, 1.0, None, "2"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                positions_for(count)
        for layout in ("", "corner", "cross ", None, [], 1):
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                positions_for(2, layout)


if __name__ == "__main__":
    unittest.main()
