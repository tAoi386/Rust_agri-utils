"""Screen observations are deduplicated independently from seed quantities."""

from itertools import islice, product
import unittest
from unittest.mock import patch

from rust_breeder.capture_import import CaptureSession, MAX_QUANTITY, plan_capture_import
from rust_breeder.models import InventoryItem
from rust_breeder.storage import MAX_INVENTORY_ITEMS


class CaptureSessionTests(unittest.TestCase):
    def test_repeated_frames_record_one_candidate_with_observation_count(self):
        session = CaptureSession()
        self.assertEqual(session.recognize_many(["YYYGGG", "YYYGGG"]), 1)
        self.assertEqual(session.recognize_many(["yyy ggg"] * 25), 0)
        self.assertEqual(len(session.candidates), 1)
        self.assertEqual(session.candidates[0].genes, "YYYGGG")
        self.assertEqual(session.candidates[0].occurrences, 27)
        imported = plan_capture_import([], [item.genes for item in session.candidates])
        self.assertEqual(imported.items[0].quantity, 1)

    def test_identity_preserves_gene_order_and_first_seen_order(self):
        session = CaptureSession()
        session.recognize_many(["YYYGGG", "GGGYYY", "YYYGGG", "GYYGGY"])
        self.assertEqual([item.genes for item in session.candidates], ["YYYGGG", "GGGYYY", "GYYGGY"])
        self.assertEqual([item.occurrences for item in session.candidates], [2, 1, 1])

    def test_invalid_batch_does_not_partially_change_session(self):
        session = CaptureSession()
        session.recognize_many(["YYYGGG"])
        before = session.candidates
        for batch in (["YYYGGG", "GGGYYY", "BAD"], ["GGGYYY", None], "YYYGGG"):
            with self.subTest(batch=batch), self.assertRaises(ValueError):
                session.recognize_many(batch)
            self.assertEqual(session.candidates, before)

    def test_failed_input_iterator_does_not_partially_change_session(self):
        session = CaptureSession()

        def broken():
            yield "YYYGGG"
            raise RuntimeError("frame read failed")

        with self.assertRaises(RuntimeError):
            session.recognize_many(broken())
        self.assertEqual(session.candidates, ())

    def test_remove_clear_and_candidate_snapshots(self):
        session = CaptureSession()
        session.recognize_many(["YYYGGG", "GGGYYY"])
        snapshot = session.candidates
        self.assertTrue(session.remove("yyy ggg"))
        self.assertFalse(session.remove("YYYGGG"))
        session.clear()
        self.assertEqual(session.candidates, ())
        self.assertEqual(len(snapshot), 2)

    def test_candidate_capacity_is_atomic_but_duplicates_are_still_allowed(self):
        session = CaptureSession()
        genes = ["".join(chars) for chars in islice(product("GYHWX", repeat=6), MAX_INVENTORY_ITEMS + 1)]
        session.recognize_many(genes[:-1])
        self.assertEqual(session.recognize_many([genes[0]]), 0)
        before = session.candidates
        with self.assertRaisesRegex(ValueError, str(MAX_INVENTORY_ITEMS)):
            session.recognize_many([genes[0], genes[-1]])
        self.assertEqual(session.candidates, before)


class CaptureImportTests(unittest.TestCase):
    def test_existing_genes_are_skipped_without_merging_or_changing_ids(self):
        original = [InventoryItem("a", "YYYGGG", 7, "原株"), InventoryItem("b", "YYYGGG", 2)]
        before = list(original)
        result = plan_capture_import(original, ["YYYGGG", "GGGYYY", "GGGYYY", "YYYGGG"])
        self.assertEqual(original, before)
        self.assertIs(result.inventory[0], original[0])
        self.assertIs(result.inventory[1], original[1])
        self.assertEqual(result.skipped_genes, ("YYYGGG",))
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.added_count, 1)
        self.assertEqual(result.items[0].genes, "GGGYYY")
        self.assertEqual(result.items[0].quantity, 1)
        self.assertEqual([item.quantity for item in result.inventory[:2]], [7, 2])
        # Re-running the same import is idempotent against the updated library.
        again = plan_capture_import(result.inventory, ["YYYGGG", "GGGYYY"])
        self.assertEqual(again.inventory, result.inventory)
        self.assertEqual(again.items, ())

    def test_order_matters_and_explicit_quantity_is_applied_once(self):
        result = plan_capture_import([], ["YYYGGG", "GGGYYY", "YYYGGG"], quantity=12)
        self.assertEqual([item.genes for item in result.items], ["YYYGGG", "GGGYYY"])
        self.assertEqual([item.quantity for item in result.items], [12, 12])
        self.assertEqual(len({item.id for item in result.items}), 2)

    def test_invalid_sequences_never_mutate_inventory(self):
        inventory = [InventoryItem("a", "YYYGGG", 4)]
        before = list(inventory)
        with self.assertRaises(ValueError):
            plan_capture_import(inventory, ["GGGYYY", "invalid"])
        self.assertEqual(inventory, before)

    def test_quantity_boundaries(self):
        self.assertEqual(plan_capture_import([], ["YYYGGG"], quantity=MAX_QUANTITY).items[0].quantity, MAX_QUANTITY)
        for quantity in (0, -1, True, 1.5, "2", MAX_QUANTITY + 1):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                plan_capture_import([], ["YYYGGG"], quantity=quantity)
        with self.assertRaises(ValueError):
            plan_capture_import([InventoryItem("a", "YYYGGG", MAX_QUANTITY + 1)], [])

    def test_capacity_boundary_and_existing_duplicates_at_capacity(self):
        inventory = [InventoryItem(f"seed-{index}", "YYYGGG") for index in range(MAX_INVENTORY_ITEMS - 1)]
        result = plan_capture_import(inventory, ["GGGYYY", "GGGYYY"])
        self.assertEqual(len(result.inventory), MAX_INVENTORY_ITEMS)
        skipped = plan_capture_import(result.inventory, ["GGGYYY", "YYYGGG"])
        self.assertEqual(skipped.items, ())
        with self.assertRaisesRegex(ValueError, str(MAX_INVENTORY_ITEMS)):
            plan_capture_import(result.inventory, ["YYYYYY"])
        self.assertEqual(len(result.inventory), MAX_INVENTORY_ITEMS)

    def test_generated_ids_do_not_collide_with_existing_ids(self):
        class FakeUuid:
            def __init__(self, value):
                self.hex = value

        inventory = [InventoryItem("a" * 12, "YYYGGG")]
        with patch("rust_breeder.capture_import.uuid.uuid4", side_effect=[FakeUuid("a" * 32), FakeUuid("b" * 32)]):
            result = plan_capture_import(inventory, ["GGGYYY"])
        self.assertEqual(result.items[0].id, "b" * 12)
        self.assertEqual(inventory[0].id, "a" * 12)

    def test_duplicate_existing_ids_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "重复编号"):
            plan_capture_import([InventoryItem("same", "YYYGGG"), InventoryItem("same", "GGGYYY")], [])


if __name__ == "__main__":
    unittest.main()
