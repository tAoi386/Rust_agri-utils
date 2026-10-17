"""A guided journey spends stock only when real game results are accepted."""

from __future__ import annotations

from dataclasses import replace
import json
import unittest

from rust_breeder.engine import predict
from rust_breeder.journey import (
    JourneyState, MAX_ITEMS, MAX_QUANTITY, PendingRound, PlantUse,
    attach_last_round_checkpoint, complete_round, pending_recipe, prepare_round,
    start_journey, undo_last_round,
)
from rust_breeder.models import InventoryItem, Recipe, Target
from rust_breeder.planter import positions_for


class JourneyTests(unittest.TestCase):
    def setUp(self):
        self.target = Target("YYYGGG", "exact")
        self.inventory = (
            InventoryItem("center", "GYYGGG", 2, "中央苗"),
            InventoryItem("a", "YHYGGG", 1, "供体 A"),
            InventoryItem("b", "YGGGGG", 2, "供体 B"),
            InventoryItem("spare", "WGGYYY", 5, "备用苗"),
        )
        self.recipe = Recipe(self.inventory[0], self.inventory[1:3],
                             predict("GYYGGG", ["YHYGGG", "YGGGGG"]), self.target)
        self.state = start_journey("center", self.target)

    def prepared(self):
        return prepare_round(self.state, self.inventory, self.recipe)

    def test_preparing_keeps_stock_and_recomputes_prediction(self):
        forged = replace(self.recipe, prediction=predict("XXXXXX", []))
        ready = prepare_round(self.state, self.inventory, forged)
        self.assertIsNone(self.state.pending)
        self.assertEqual(self.inventory[0].quantity, 2)
        self.assertEqual(ready.pending.prediction.deterministic_result, "YYYGGG")
        self.assertEqual(ready.pending.center_id, "center")
        self.assertEqual(ready.pending.donor_ids, ("a", "b"))
        self.assertEqual(pending_recipe(ready, self.inventory), self.recipe)

    def test_real_result_drives_new_center_even_if_prediction_was_different(self):
        ready = self.prepared()
        result = complete_round(ready, self.inventory, "wggyyy", 3, token=ready.pending.token)
        rows = {row.id: row for row in result.inventory}
        self.assertEqual(rows["center"].quantity, 1)
        self.assertNotIn("a", rows)
        self.assertEqual(rows["b"].quantity, 1)
        self.assertEqual(rows["spare"].quantity, 8)
        self.assertEqual(result.center_id, "spare")
        self.assertEqual(result.state.center_id, "spare")
        self.assertIsNone(result.state.pending)
        self.assertEqual(result.record.observed_genes, "WGGYYY")
        self.assertFalse(result.record.target_matched)
        self.assertEqual(result.record.center_genes, "GYYGGG")
        self.assertEqual(result.record.donor_genes, ("YHYGGG", "YGGGGG"))
        self.assertEqual(result.record.prediction_pattern, "Y Y Y G G G")
        self.assertIn("实测 WGGYYY", result.record.summary)
        self.assertEqual(len(ready.rounds), 0)
        self.assertEqual(self.inventory[3].quantity, 5)

    def test_new_genotype_is_added_with_actual_clone_count(self):
        result = complete_round(self.prepared(), self.inventory, "YYYGGG", 4)
        actual = next(row for row in result.inventory if row.id == result.center_id)
        self.assertEqual(actual.genes, "YYYGGG")
        self.assertEqual(actual.quantity, 4)
        self.assertTrue(result.record.target_matched)
        self.assertEqual(len(result.state.rounds), 1)
        self.assertTrue(all(row.id != result.center_id for row in self.inventory))

    def test_matching_counts_with_different_order_does_not_merge_stock(self):
        result = complete_round(self.prepared(), self.inventory, "YYYGGW", 2)
        self.assertNotEqual(result.center_id, "spare")
        self.assertEqual(next(row for row in result.inventory if row.id == "spare").quantity, 5)

    def test_second_round_follows_actual_center_and_keeps_history(self):
        first = complete_round(self.prepared(), self.inventory, "WGGYYY", 3)
        center = next(row for row in first.inventory if row.id == first.center_id)
        donor = next(row for row in first.inventory if row.id == "center")
        recipe = Recipe(center, (donor,), predict(center.genes, [donor.genes]), self.target)
        ready = prepare_round(first.state, first.inventory, recipe)
        second = complete_round(ready, first.inventory, "GGGYYY", 2)
        self.assertEqual(len(second.state.rounds), 2)
        self.assertEqual(second.record.number, 2)
        self.assertEqual(second.record.center_genes, "WGGYYY")
        self.assertEqual(second.state.rounds[0], first.record)
        self.assertFalse(second.record.target_matched)

    def test_accepting_same_round_twice_is_rejected(self):
        ready = self.prepared()
        result = complete_round(ready, self.inventory, "YYYGGG", 3)
        with self.assertRaisesRegex(ValueError, "已经记账"):
            complete_round(result.state, result.inventory, "YYYGGG", 3, token=ready.pending.token)
        self.assertEqual(len(result.state.rounds), 1)

    def test_old_token_cannot_consume_a_new_pending_round(self):
        ready = self.prepared()
        replaced = prepare_round(ready, self.inventory, self.recipe)
        self.assertNotEqual(ready.pending.token, replaced.pending.token)
        with self.assertRaisesRegex(ValueError, "旧轮次"):
            complete_round(replaced, self.inventory, "YYYGGG", 3, token=ready.pending.token)

    def test_invalid_actual_or_quantity_does_not_change_stock_or_session(self):
        ready = self.prepared()
        snapshot = ready.to_dict()
        for genes, quantity in (("invalid", 3), ("YYYGGG", 0), ("YYYGGG", -1),
                                ("YYYGGG", True), ("YYYGGG", 1.5),
                                ("YYYGGG", MAX_QUANTITY + 1)):
            with self.subTest(genes=genes, quantity=quantity), self.assertRaises(ValueError):
                complete_round(ready, self.inventory, genes, quantity)
            self.assertEqual(ready.to_dict(), snapshot)
            self.assertEqual(self.inventory[0].quantity, 2)

    def test_changed_stock_blocks_completion_and_pending_restore(self):
        ready = self.prepared()
        for changed in (
            self.inventory[1:],
            (replace(self.inventory[0], quantity=1), *self.inventory[1:]),
            (*self.inventory[:3], replace(self.inventory[3], genes="XXXXXX")),
        ):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, "库存已经变化"):
                    complete_round(ready, changed, "YYYGGG", 3)
                with self.assertRaisesRegex(ValueError, "库存已经变化"):
                    pending_recipe(ready, changed)

    def test_renaming_and_reordering_inventory_is_safe(self):
        ready = self.prepared()
        changed = tuple(replace(row, name=f"新名称 {row.id}") for row in reversed(self.inventory))
        result = complete_round(ready, changed, "YYYGGG", 3)
        self.assertEqual(len(result.state.rounds), 1)
        self.assertEqual(result.inventory[0].name, "新名称 spare")

    def test_center_selection_target_and_quantities_are_validated(self):
        cases = (
            replace(self.recipe, center=self.inventory[3]),
            replace(self.recipe, target=Target("GGGYYY", "exact")),
            replace(self.recipe, donors=(self.inventory[1], self.inventory[1])),
            replace(self.recipe, center=replace(self.inventory[0], genes="XXXXXX")),
        )
        for recipe in cases:
            with self.subTest(recipe=recipe), self.assertRaises(ValueError):
                prepare_round(self.state, self.inventory, recipe)

    def test_same_id_as_center_and_multiple_donors_consumes_total(self):
        inventory = (InventoryItem("same", "GGGYYY", 4),)
        state = start_journey("same", Target("YYYGGG", "exact"))
        recipe = Recipe(inventory[0], (inventory[0], inventory[0]),
                        predict("GGGYYY", ["GGGYYY", "GGGYYY"]), state.target)
        ready = prepare_round(state, inventory, recipe)
        result = complete_round(ready, inventory, "GGGYYY", 2)
        self.assertEqual(result.center_id, "same")
        self.assertEqual(result.inventory[0].quantity, 3)  # 4 - 1 - 2 + 2

    def test_no_donor_unmet_target_cannot_become_a_round(self):
        recipe = replace(self.recipe, donors=(), prediction=predict("GYYGGG", []))
        with self.assertRaisesRegex(ValueError, "周围为空"):
            prepare_round(self.state, self.inventory, recipe)

    def test_json_roundtrip_restores_pending_then_completed_history(self):
        ready = self.prepared()
        restored = JourneyState.from_dict(json.loads(json.dumps(ready.to_dict())))
        self.assertEqual(restored, ready)
        self.assertEqual(pending_recipe(restored, self.inventory), self.recipe)
        result = complete_round(restored, self.inventory, "YYYGGG", 3)
        self.assertEqual(JourneyState.from_dict(result.state.to_dict()), result.state)

    def test_new_pending_uses_auto_and_preserves_layout_through_undo(self):
        ready = self.prepared()
        self.assertEqual(ready.pending.layout, "auto")
        self.assertEqual(positions_for(len(ready.pending.donor_ids), ready.pending.layout), (1, 7))
        result = complete_round(ready, self.inventory, "YYYGGG", 3)
        self.assertEqual(result.record.layout, "auto")
        restored = JourneyState.from_dict(json.loads(json.dumps(result.state.to_dict())))
        undone = undo_last_round(restored, result.inventory)
        self.assertEqual(undone.state.pending, ready.pending)
        recovered = attach_last_round_checkpoint(replace(restored, undo_checkpoint=None),
                                                 result.inventory, self.inventory)
        self.assertEqual(recovered.undo_checkpoint.pending.layout, "auto")

    def test_explicit_corner_pending_stays_corners_after_save_and_undo(self):
        ready = self.prepared()
        ready = replace(ready, pending=replace(ready.pending, layout="corners"))
        restored = JourneyState.from_dict(json.loads(json.dumps(ready.to_dict())))
        self.assertEqual(restored.pending.layout, "corners")
        self.assertEqual(positions_for(len(restored.pending.donor_ids), restored.pending.layout), (0, 8))
        result = complete_round(restored, self.inventory, "YYYGGG", 3)
        self.assertEqual(result.record.layout, "corners")
        undone = undo_last_round(JourneyState.from_dict(result.state.to_dict()), result.inventory)
        self.assertEqual(undone.state.pending, ready.pending)

    def test_old_pending_without_layout_keeps_original_cross_positions(self):
        data = self.prepared().to_dict()
        del data["pending"]["layout"]
        restored = JourneyState.from_dict(data)
        self.assertEqual(restored.pending.layout, "cross")
        self.assertEqual(positions_for(len(restored.pending.donor_ids), restored.pending.layout), (1, 3))
        again = JourneyState.from_dict(json.loads(json.dumps(restored.to_dict())))
        self.assertEqual(again, restored)
        result = complete_round(again, self.inventory, "YYYGGG", 3)
        self.assertEqual(result.record.layout, "cross")
        undone = undo_last_round(JourneyState.from_dict(result.state.to_dict()), result.inventory)
        self.assertEqual(undone.state.pending, restored.pending)

    def test_old_completed_checkpoint_and_recovery_keep_cross_layout(self):
        result = complete_round(self.prepared(), self.inventory, "YYYGGG", 3)
        data = result.state.to_dict()
        del data["rounds"][-1]["layout"]
        del data["undo_checkpoint"]["pending"]["layout"]
        restored = JourneyState.from_dict(data)
        self.assertEqual(restored.rounds[-1].layout, "cross")
        self.assertEqual(restored.undo_checkpoint.pending.layout, "cross")
        undone = undo_last_round(restored, result.inventory)
        self.assertEqual(undone.state.pending.layout, "cross")
        recovered = attach_last_round_checkpoint(replace(restored, undo_checkpoint=None),
                                                 result.inventory, self.inventory)
        self.assertEqual(recovered.undo_checkpoint.pending.layout, "cross")

    def test_saved_invalid_or_mixed_layout_is_rejected(self):
        ready = self.prepared()
        for layout in ("", "unknown", None, [], 1):
            data = ready.to_dict()
            data["pending"]["layout"] = layout
            with self.subTest(layout=layout), self.assertRaisesRegex(ValueError, "布局"):
                JourneyState.from_dict(data)
        result = complete_round(ready, self.inventory, "YYYGGG", 3)
        data = result.state.to_dict()
        data["undo_checkpoint"]["pending"]["layout"] = "cross"
        with self.assertRaisesRegex(ValueError, "最近完成"):
            JourneyState.from_dict(data)
        data = result.state.to_dict()
        data["rounds"][-1]["layout"] = "invalid"
        with self.assertRaisesRegex(ValueError, "布局"):
            JourneyState.from_dict(data)

    def test_invalid_saved_records_are_rejected(self):
        valid = self.prepared().to_dict()
        for mutation in (
            lambda value: value.update(version=True),
            lambda value: value.update(rounds="wrong"),
            lambda value: value.update(target_mode=[]),
            lambda value: value["pending"].update(inventory_fingerprint="bad"),
            lambda value: value["pending"]["center"].update(quantity=2),
            lambda value: value["pending"].update(donors=[]),
            lambda value: value.update(center_id="another"),
        ):
            data = json.loads(json.dumps(valid))
            mutation(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                JourneyState.from_dict(data)

    def test_completed_token_cannot_reappear_in_pending_or_history(self):
        ready = self.prepared()
        result = complete_round(ready, self.inventory, "YYYGGG", 3)
        with self.assertRaisesRegex(ValueError, "已经记账"):
            replace(result.state, center_id=ready.center_id, pending=ready.pending)
        with self.assertRaises(ValueError):
            replace(result.state, rounds=(result.record, result.record))

    def test_saved_forged_plant_reference_cannot_credit_free_clones(self):
        ready = self.prepared()
        bad_pending = replace(ready.pending, donors=(PlantUse("missing", "YHYGGG"),))
        state = replace(ready, pending=bad_pending)
        with self.assertRaisesRegex(ValueError, "不在库存"):
            complete_round(state, self.inventory, "YYYGGG", 3)

    def test_merge_quantity_overflow_is_atomic(self):
        inventory = (*self.inventory[:3], replace(self.inventory[3], quantity=MAX_QUANTITY))
        ready = prepare_round(self.state, inventory, self.recipe)
        with self.assertRaisesRegex(ValueError, "合并后的克隆数量"):
            complete_round(ready, inventory, "WGGYYY", 1)
        self.assertEqual(inventory[-1].quantity, MAX_QUANTITY)
        self.assertIsNotNone(ready.pending)

    def test_inventory_limits_are_enforced(self):
        too_many = tuple(InventoryItem(f"item-{i}", "GGGYYY") for i in range(MAX_ITEMS + 1))
        with self.assertRaisesRegex(ValueError, "最多 5000"):
            prepare_round(self.state, too_many, self.recipe)
        with self.assertRaisesRegex(ValueError, "可用数量"):
            prepare_round(self.state, (replace(self.inventory[0], quantity=MAX_QUANTITY + 1),), self.recipe)

    def test_fully_consumed_rows_make_space_at_inventory_limit(self):
        inventory = (replace(self.inventory[0], quantity=1), *self.inventory[1:],
                     *(InventoryItem(f"extra-{i}", "GGGYYY", 2) for i in range(MAX_ITEMS - 4)))
        ready = prepare_round(self.state, inventory, self.recipe)
        result = complete_round(ready, inventory, "YYYGGG", 2)
        self.assertEqual(len(result.inventory), MAX_ITEMS - 1)

    def assert_same_stock(self, actual, expected):
        self.assertEqual(sorted((row.id, row.genes, row.quantity) for row in actual),
                         sorted((row.id, row.genes, row.quantity) for row in expected))

    def test_undo_restores_consumed_plants_removes_new_result_and_restores_pending(self):
        ready = self.prepared()
        result = complete_round(ready, self.inventory, "YYYGGG", 4)
        undone = undo_last_round(result.state, result.inventory, token=result.record.token)
        self.assert_same_stock(undone.inventory, self.inventory)
        self.assertNotIn(result.center_id, {row.id for row in undone.inventory})
        self.assertEqual(undone.state, ready)
        self.assertEqual(undone.record, result.record)
        self.assertEqual(pending_recipe(undone.state, undone.inventory), self.recipe)
        self.assertEqual(len(result.state.rounds), 1)

    def test_undo_subtracts_merged_result_without_removing_existing_stock(self):
        ready = self.prepared()
        result = complete_round(ready, self.inventory, "WGGYYY", 3)
        undone = undo_last_round(result.state, result.inventory)
        self.assert_same_stock(undone.inventory, self.inventory)
        self.assertEqual(next(row for row in undone.inventory if row.id == "spare").quantity, 5)
        self.assertEqual(undone.state.pending, ready.pending)

    def test_undo_same_id_for_center_and_donors_restores_total_once(self):
        inventory = (InventoryItem("same", "GGGYYY", 4),)
        state = start_journey("same", self.target)
        recipe = Recipe(inventory[0], (inventory[0], inventory[0]),
                        predict("GGGYYY", ["GGGYYY", "GGGYYY"]), state.target)
        ready = prepare_round(state, inventory, recipe)
        result = complete_round(ready, inventory, "GGGYYY", 2)
        undone = undo_last_round(result.state, result.inventory)
        self.assertEqual(undone.inventory, inventory)
        self.assertEqual(undone.state, ready)

    def test_undo_fully_consumed_genotype_result_restores_original_id(self):
        inventory = (replace(self.inventory[0], quantity=1), *self.inventory[1:])
        ready = prepare_round(self.state, inventory, self.recipe)
        result = complete_round(ready, inventory, self.inventory[0].genes, 2)
        self.assertNotEqual(result.center_id, "center")
        undone = undo_last_round(result.state, result.inventory)
        self.assert_same_stock(undone.inventory, inventory)
        self.assertEqual(undone.state.center_id, "center")

    def test_undo_is_only_one_layer_and_cannot_repeat_or_double_charge(self):
        first = complete_round(self.prepared(), self.inventory, "WGGYYY", 3)
        by_id = {row.id: row for row in first.inventory}
        recipe = Recipe(by_id["spare"], (by_id["center"],),
                        predict("WGGYYY", ["GYYGGG"]), self.target)
        ready = prepare_round(first.state, first.inventory, recipe)
        second = complete_round(ready, first.inventory, "GGGYYY", 2)
        undone = undo_last_round(second.state, second.inventory)
        self.assert_same_stock(undone.inventory, first.inventory)
        self.assertEqual(undone.state.rounds, (first.record,))
        self.assertEqual(undone.state.pending, ready.pending)
        self.assertIsNone(undone.state.undo_checkpoint)
        with self.assertRaisesRegex(ValueError, "没有可撤回"):
            undo_last_round(undone.state, undone.inventory)
        corrected = complete_round(undone.state, undone.inventory, "YYYGGG", 1)
        self.assertEqual(len(corrected.state.rounds), 2)
        self.assertEqual(corrected.record.token, second.record.token)
        self.assertEqual(corrected.state.undo_checkpoint.inventory_before, undone.inventory)
        undo_corrected = undo_last_round(corrected.state, corrected.inventory)
        self.assert_same_stock(undo_corrected.inventory, first.inventory)

    def test_new_pending_preview_can_be_discarded_without_consuming_it(self):
        ready = self.prepared()
        result = complete_round(ready, self.inventory, "WGGYYY", 3)
        by_id = {row.id: row for row in result.inventory}
        recipe = Recipe(by_id["spare"], (by_id["b"],),
                        predict("WGGYYY", ["YGGGGG"]), self.target)
        preview = prepare_round(result.state, result.inventory, recipe)
        self.assertNotEqual(preview.pending.token, ready.pending.token)
        undone = undo_last_round(preview, result.inventory)
        self.assert_same_stock(undone.inventory, self.inventory)
        self.assertEqual(undone.state.pending, ready.pending)

    def test_stock_changes_block_undo_atomically(self):
        result = complete_round(self.prepared(), self.inventory, "WGGYYY", 3)
        snapshot = result.state.to_dict()
        variants = (
            result.inventory[1:],
            (replace(result.inventory[0], quantity=3), *result.inventory[1:]),
            (replace(result.inventory[0], genes="XXXXXX"), *result.inventory[1:]),
            (replace(result.inventory[0], id="changed-id"), *result.inventory[1:]),
            (*result.inventory, InventoryItem("new", "YYYGGG")),
        )
        for inventory in variants:
            with self.subTest(inventory=inventory), self.assertRaisesRegex(ValueError, "库存已经变化"):
                undo_last_round(result.state, inventory)
            self.assertEqual(result.state.to_dict(), snapshot)

    def test_name_and_order_edits_are_allowed_and_preserved_on_undo(self):
        result = complete_round(self.prepared(), self.inventory, "WGGYYY", 3)
        edited = tuple(replace(row, name=f"新名称 {row.id}") for row in reversed(result.inventory))
        undone = undo_last_round(result.state, edited)
        self.assert_same_stock(undone.inventory, self.inventory)
        self.assertEqual([row.id for row in undone.inventory], ["spare", "b", "center", "a"])
        self.assertEqual([row.name for row in undone.inventory],
                         ["新名称 spare", "新名称 b", "新名称 center", "供体 A"])
        self.assertEqual(pending_recipe(undone.state, undone.inventory).center.name, "新名称 center")

    def test_current_target_is_preserved_when_undoing_after_target_change(self):
        result = complete_round(self.prepared(), self.inventory, "YYYGGG", 2)
        changed = replace(result.state, target=Target("HHHGGG", "counts"))
        undone = undo_last_round(changed, result.inventory)
        self.assertEqual(undone.state.target, changed.target)
        self.assertEqual(pending_recipe(undone.state, undone.inventory).target, changed.target)
        self.assert_same_stock(undone.inventory, self.inventory)

    def test_changed_target_cannot_restore_an_inapplicable_no_donor_round(self):
        inventory = (InventoryItem("center", "YYYGGG"),)
        ready = prepare_round(start_journey("center", self.target), inventory,
                              Recipe(inventory[0], (), predict("YYYGGG", []), self.target))
        result = complete_round(ready, inventory, "YYYGGG", 1)
        changed = replace(result.state, target=Target("HHHGGG"))
        with self.assertRaisesRegex(ValueError, "目标已经变化"):
            undo_last_round(changed, result.inventory)
        self.assertIsNotNone(changed.undo_checkpoint)
        self.assertEqual(len(changed.rounds), 1)

    def test_stale_undo_token_is_rejected(self):
        result = complete_round(self.prepared(), self.inventory, "YYYGGG", 2)
        with self.assertRaisesRegex(ValueError, "旧轮次"):
            undo_last_round(result.state, result.inventory, token="outdated")

    def test_recovery_snapshot_roundtrips_and_none_is_omitted(self):
        self.assertNotIn("undo_checkpoint", self.prepared().to_dict())
        result = complete_round(self.prepared(), self.inventory, "YYYGGG", 3)
        restored = JourneyState.from_dict(json.loads(json.dumps(result.state.to_dict())))
        self.assertEqual(restored, result.state)
        undone = undo_last_round(restored, result.inventory)
        self.assert_same_stock(undone.inventory, self.inventory)
        self.assertNotIn("undo_checkpoint", undone.state.to_dict())

    def test_old_project_without_snapshot_loads_but_does_not_guess_stock(self):
        result = complete_round(self.prepared(), self.inventory, "YYYGGG", 3)
        old = result.state.to_dict()
        del old["undo_checkpoint"]
        restored = JourneyState.from_dict(old)
        self.assertIsNone(restored.undo_checkpoint)
        self.assertEqual(restored.rounds, result.state.rounds)
        with self.assertRaisesRegex(ValueError, "原库存备份"):
            undo_last_round(restored, result.inventory)

    def test_old_project_can_attach_independently_verified_before_stock(self):
        ready = self.prepared()
        result = complete_round(ready, self.inventory, "WGGYYY", 3)
        legacy = replace(result.state, undo_checkpoint=None)
        recovered = attach_last_round_checkpoint(legacy, result.inventory, self.inventory)
        self.assertIsNone(legacy.undo_checkpoint)
        undone = undo_last_round(recovered, result.inventory)
        self.assert_same_stock(undone.inventory, self.inventory)
        self.assertEqual(undone.state.pending, ready.pending)

    def test_old_project_recovery_checks_complete_after_stock_not_only_used_rows(self):
        result = complete_round(self.prepared(), self.inventory, "YYYGGG", 3)
        legacy = replace(result.state, undo_checkpoint=None)
        for before in (
            self.inventory[1:],
            (*self.inventory[:3], replace(self.inventory[3], quantity=6)),
            (*self.inventory[:3], replace(self.inventory[3], genes="XXXXXX")),
        ):
            with self.subTest(before=before), self.assertRaises(ValueError):
                attach_last_round_checkpoint(legacy, result.inventory, before)
        with self.assertRaisesRegex(ValueError, "库存不一致"):
            attach_last_round_checkpoint(legacy, (*result.inventory, InventoryItem("new", "HHHHHH")),
                                         self.inventory)
        self.assertIsNone(legacy.undo_checkpoint)
        with self.assertRaisesRegex(ValueError, "没有已完成"):
            attach_last_round_checkpoint(self.state, self.inventory, self.inventory)

    def test_tampered_recovery_snapshots_are_rejected_on_load(self):
        result = complete_round(self.prepared(), self.inventory, "YYYGGG", 3)
        valid = result.state.to_dict()
        for mutation in (
            lambda data: data["undo_checkpoint"].update(completed_token="other"),
            lambda data: data["undo_checkpoint"].update(after_inventory_fingerprint="0" * 64),
            lambda data: data["undo_checkpoint"].update(inventory_before=[]),
            lambda data: data["undo_checkpoint"]["inventory_before"][0].update(quantity=0),
            lambda data: data["undo_checkpoint"]["inventory_before"][0].update(id=None),
            lambda data: data.update(rounds=[]),
            lambda data: data["rounds"][-1].update(clone_quantity=1),
        ):
            changed = json.loads(json.dumps(valid))
            mutation(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                JourneyState.from_dict(changed)


if __name__ == "__main__":
    unittest.main()
