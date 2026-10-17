"""Exercise the primary guided workflow through real Qt signals and workers."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_DEPS = PROJECT_ROOT / ".deps"
if LOCAL_DEPS.exists():
    sys.path.insert(0, str(LOCAL_DEPS))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from rust_breeder import app as application
    from rust_breeder import guidance
    from rust_breeder.engine import predict
    from rust_breeder.models import InventoryItem, Recipe
    from rust_breeder.routes import RouteReport, RouteStep
    from rust_breeder.storage import load_project, save_project
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, "Install PySide6 to run Qt integration checks.")
class GuidedWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        artifacts = PROJECT_ROOT / "artifacts"
        artifacts.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="guided-tests-", dir=artifacts)
        self.autosave_patch = patch.object(application, "AUTOSAVE", Path(self.temporary.name) / "autosave.json")
        self.autosave_patch.start()
        self.windows = []
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = []
        self.planner_patch = patch.object(guidance, "plan_route", self.fake_route)
        self.planner_patch.start()
        self.window = self.new_window()
        self.window.inventory = [
            InventoryItem("center", "GYYGGG", 3, "原中央"),
            InventoryItem("a", "YHYGGG", 2, "供体 A"),
            InventoryItem("b", "YGGGGG", 2, "供体 B"),
            InventoryItem("spare", "WGGYYY", 5, "已有同基因克隆"),
        ]
        self.window.mode_combo.setCurrentIndex(self.window.mode_combo.findData("exact"))
        self.window.refresh_inventory()
        self.window.refresh_board()
        self.window.autosave_timer.stop()
        self.guided = self.window.guided

    def tearDown(self):
        self.release.set()
        for window in reversed(self.windows):
            workspace = window.guided
            if workspace.is_busy():
                workspace.worker.requestInterruption()
                workspace.worker.wait(5000)
            self.qt.processEvents()
            window.autosave_timer.stop()
            window.dirty = False
            window.close()
            window.deleteLater()
        self.qt.processEvents()
        self.planner_patch.stop()
        self.autosave_patch.stop()
        self.temporary.cleanup()

    def new_window(self):
        window = application.MainWindow(restore=False)
        self.windows.append(window)
        return window

    def fake_route(self, inventory, target, *, center_id=None, **options):
        """Return a valid stock-based recommendation; search internals have unit tests."""
        self.calls.append((tuple(inventory), target, center_id, options))
        by_id = {item.id: item for item in inventory}
        center = by_id[center_id or "center"]
        donors = () if target.matches(center.genes) else tuple(by_id[key] for key in ("a", "b"))
        prediction = predict(center.genes, [item.genes for item in donors])
        recipe = Recipe(center, donors, prediction, target)
        steps = ()
        if donors and recipe.guaranteed:
            steps = (RouteStep(recipe, prediction.deterministic_result, "planned-output", 1),)
        return RouteReport(steps, (recipe,), recipe.guaranteed, True, 12,
                           "already_target" if not donors else "exhausted")

    def wait_finished(self, workspace=None):
        workspace = workspace or self.guided
        deadline = time.monotonic() + 5
        while workspace.is_busy() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.qt.processEvents()
        self.assertFalse(workspace.is_busy())

    def plan(self):
        self.guided.plan_button.click()
        self.wait_finished()
        self.assertIsNotNone(self.guided.current_recipe())

    def reach_actual_entry(self):
        self.plan()
        self.assertEqual(self.guided.stage, 2)

    def reach_legacy_actual_entry(self, center_id="center", donor_ids=("a",)):
        """Restore an already planted non-target round without inventing a result."""
        by_id = {item.id: item for item in self.window.inventory}
        center = by_id[center_id]
        donors = tuple(by_id[key] for key in donor_ids)
        recipe = Recipe(center, donors, predict(center.genes, [item.genes for item in donors]), self.window.target())
        self.guided.state = guidance.prepare_round(
            replace(self.guided.state, center_id=center.id), tuple(self.window.inventory), recipe)
        self.guided.stage = 2
        self.guided.render_round()
        return recipe

    def fill_actual(self, genes="YYYGGG", quantity=4):
        self.guided.actual_edit.setText(genes)
        self.guided.actual_quantity.setValue(quantity)
        self.guided.completed_check.setCurrentIndex(1)

    def test_primary_workspace_starts_in_auto_mode_and_one_click_places_every_plant(self):
        before = tuple(self.window.inventory)
        self.assertEqual(self.window.workspaces.currentIndex(), 0)
        self.assertEqual(self.window.workspaces.count(), 2)
        self.assertFalse(self.window.workspaces.tabBar().isHidden())
        self.assertEqual(self.guided.center_combo.currentData(), "")
        self.plan()
        self.assertIsNone(self.calls[0][2])
        recipe = self.guided.current_recipe()
        self.assertEqual(recipe.center_id, "center")
        self.assertEqual(Counter(recipe.donor_ids), {"a": 1, "b": 1})
        self.assertEqual(self.guided.cells[4][1].text(), "GYYGGG")
        self.assertEqual(sum(genes.text() != "留空" for _, genes, _ in self.guided.cells), 3)
        self.assertEqual(tuple(self.window.inventory), before)
        self.assertEqual(len(self.guided.state.rounds), 0)

    def test_optional_fixed_center_is_passed_to_worker_and_drawn_in_middle(self):
        self.guided.center_combo.setCurrentIndex(self.guided.center_combo.findData("center"))
        self.plan()
        self.assertEqual(self.calls[0][2], "center")
        self.assertEqual(self.guided.current_recipe().center_id, "center")
        self.assertEqual(self.guided.cells[4][1].text(), "GYYGGG")

    def test_planter_identifies_seed_number_and_keeps_prediction_separate_from_planted_gene(self):
        self.plan()
        recipe = self.guided.current_recipe()
        number = self.window.seed_number(recipe.center)
        self.assertEqual(self.guided.cells[4][1].text(), "GYYGGG")
        self.assertIn(f"#{number:02d} 苗", self.guided.cells[4][2].text())
        self.assertEqual(self.guided.cell_predictions[4].text(), "预测 → YYYGGG")
        self.assertIn("YYYGGG", self.guided.round_prediction.text())
        self.assertEqual(self.guided.actual_edit.text(), "")
        self.assertFalse(self.guided.commit_button.isEnabled())
        self.assertIn(f"#{number:02d}", self.guided.inventory_table.item(0, 2).text())

    def test_old_click_through_stages_restore_directly_to_actual_entry_without_spending(self):
        self.plan()
        snapshot = self.window.snapshot()
        before = tuple(self.window.inventory)
        for old_stage in (0, 1, 2):
            with self.subTest(stage=old_stage):
                snapshot["guided"]["stage"] = old_stage
                restored = self.new_window()
                restored.apply_project(snapshot)
                workspace = restored.guided
                self.assertEqual(workspace.stage, 2)
                self.assertTrue(workspace.actual_edit.isEnabled())
                self.assertFalse(workspace.commit_button.isEnabled())
                self.assertEqual(tuple(restored.inventory), before)
                self.assertEqual(workspace.state.rounds, ())

    def test_fewest_rounds_reaches_worker_with_auto_center_and_counts_target(self):
        self.guided.mode.setCurrentIndex(self.guided.mode.findData("counts"))
        self.guided.objective.setCurrentIndex(self.guided.objective.findData("rounds"))
        self.plan()
        _, target, center_id, options = self.calls[-1]
        self.assertEqual(options["objective"], "rounds")
        self.assertIsNone(center_id)
        self.assertEqual((target.genes, target.mode), ("YYYGGG", "counts"))
        self.assertTrue(target.matches("GGGYYY"))
        self.assertEqual(self.guided.center_combo.currentData(), "")
        self.assertEqual(self.guided.objective.currentData(), "rounds")
        self.assertEqual(self.window.target(), target)

    def test_inventory_matching_option_reaches_worker_and_restores_without_changing_choice(self):
        self.guided.objective.setCurrentIndex(self.guided.objective.findData("stock"))
        self.assertEqual(self.guided.objective.currentText(), "智能匹配（基于已有基因库）")
        self.plan()
        self.assertEqual(self.calls[-1][3]["objective"], "stock")
        restored = self.new_window()
        restored.apply_project(self.window.snapshot())
        self.assertEqual(restored.guided.objective.currentData(), "stock")
        self.assertEqual(restored.guided.state.pending, self.guided.state.pending)

    def test_existing_target_still_displays_the_plant_without_spending_donors(self):
        self.guided.target_edit.setText("WGGYYY")
        self.guided.center_combo.setCurrentIndex(self.guided.center_combo.findData("spare"))
        before = tuple(self.window.inventory)
        self.plan()
        self.assertTrue(self.guided.report.found)
        self.assertEqual(self.guided.report.reason, "already_target")
        self.assertEqual(self.guided.current_recipe().donor_count, 0)
        self.assertIn("已经有符合目标", self.guided.instruction_title.text())
        self.assertEqual(tuple(self.window.inventory), before)

    def test_unproven_fallback_never_starts_planting_or_selects_its_center(self):
        self.window.mode_combo.setCurrentIndex(self.window.mode_combo.findData("counts"))
        before = tuple(self.window.inventory)
        by_id = {item.id: item for item in before}
        center = by_id["spare"]
        donors = (by_id["a"], by_id["b"])
        target = self.window.target()
        recipe = Recipe(center, donors, predict(center.genes, [item.genes for item in donors]), target)
        self.assertFalse(recipe.guaranteed)
        for reason in ("limit", "cancelled", "exhausted"):
            with self.subTest(reason=reason):
                report = RouteReport((), (recipe,), False, reason == "exhausted", 120000, reason)
                with patch.object(guidance, "plan_route", return_value=report):
                    self.guided.plan_button.click()
                    self.wait_finished()
                self.assertIsNone(self.guided.state.pending)
                self.assertEqual(self.guided.state.center_id, "")
                self.assertIsNone(self.guided.current_recipe())
                self.assertTrue(all(genes.text() == "留空" for _, genes, _ in self.guided.cells))
                self.assertFalse(self.guided.actual_edit.isEnabled())
                self.assertFalse(self.guided.commit_button.isEnabled())
                self.assertEqual(not self.guided.action_notice.isHidden(), reason != "cancelled")
                if reason != "cancelled":
                    self.assertEqual(self.guided.action_notice.title.text(), "暂未找到方案")
                self.assertEqual(tuple(self.window.inventory), before)
                if reason == "cancelled":
                    self.assertEqual(self.guided.instruction_title.text(), "已停止计算")
                else:
                    self.assertIn(target.label, self.guided.instruction_title.text())
                    self.assertIn("未找到", self.guided.instruction_title.text())
                self.assertEqual(self.guided.route_label.text(), report.summary)
                # Search completion and ordinary refresh must retain the reason.
                self.guided.refresh_from_owner()
                self.assertIn("已停止计算" if reason == "cancelled" else "未找到完整达标路线",
                              self.guided.summary.text())
                self.assertEqual(self.guided.route_label.text(), report.summary)

    def test_cancelled_search_is_not_displayed_as_insufficient_samples(self):
        before = tuple(self.window.inventory)
        report = RouteReport((), (), False, False, 4321, "cancelled")
        with patch.object(guidance, "plan_route", return_value=report):
            self.guided.plan_button.click()
            self.wait_finished()
        self.guided.refresh_from_owner()
        self.assertEqual(self.guided.summary.text(), "已停止计算")
        self.assertEqual(self.guided.instruction_title.text(), "已停止计算")
        self.assertIn("库存未更改", self.guided.instruction_body.text())
        self.assertIn("尚未安排用苗", self.guided.prediction_label.text())
        self.assertNotIn("建议", self.guided.route_label.text())
        self.assertIn("可重新自动选苗", self.guided.route_label.text())
        self.assertTrue(self.guided.action_notice.isHidden())
        self.assertTrue(self.guided.plan_button.isEnabled())
        self.assertIsNone(self.guided.state.pending)
        self.assertEqual(tuple(self.window.inventory), before)

    def test_empty_inventory_feedback_requires_samples_and_never_starts_a_worker(self):
        self.window.inventory = []
        self.window.refresh_inventory()
        self.guided.plan_button.click()
        self.assertIsNone(self.guided.worker)
        self.assertFalse(self.guided.action_notice.isHidden())
        self.assertEqual(self.guided.action_notice.title.text(), "样本不足")
        self.assertEqual(self.guided.action_notice.timer.interval(), 5000)

    def test_recommendation_immediately_accepts_actual_entry_and_locks_stock(self):
        before = tuple(self.window.inventory)
        self.plan()
        self.assertEqual(self.guided.stage, 2)
        self.assertTrue(self.guided.actual_edit.isEnabled())
        self.assertFalse(hasattr(self.guided, "advance_button"))
        self.assertFalse(hasattr(self.guided, "previous_button"))
        for widget in (self.guided.center_combo, self.guided.plan_button, self.guided.add_button,
                       self.guided.capture_button, self.guided.target_edit):
            self.assertFalse(widget.isEnabled())
        self.assertFalse(self.guided.commit_button.isEnabled())
        self.assertEqual(tuple(self.window.inventory), before)
        self.fill_actual("YYYGGG", 4)
        self.assertTrue(self.guided.commit_button.isEnabled())
        self.assertIn("第 1 轮成功", self.guided.commit_button.text())
        self.assertIn("完成培育", self.guided.commit_button.text())
        self.assertEqual(len(self.guided.state.rounds), 0)

    def test_unexpected_actual_result_cannot_complete_even_when_marked_finished(self):
        self.reach_actual_entry()
        predicted = self.guided.current_recipe().prediction.deterministic_result
        self.assertEqual(predicted, "YYYGGG")
        before_inventory = tuple(self.window.inventory)
        before_state = self.guided.state
        before_calls = len(self.calls)
        self.fill_actual("wggyyy", 4)
        self.assertFalse(self.guided.commit_button.isEnabled())
        self.assertIn("预测", self.guided.actual_assessment.text())
        self.guided.commit_button.click()
        # A direct handler call must enforce the same check as its button.
        self.guided.complete_current_round()
        self.qt.processEvents()
        self.assertEqual(tuple(self.window.inventory), before_inventory)
        self.assertEqual(self.guided.state, before_state)
        self.assertEqual(len(self.calls), before_calls)
        self.assertEqual(self.guided.stage, 2)
        self.assertEqual(len(self.guided.state.rounds), 0)
        self.assertFalse(self.guided.is_busy())
        # Correcting the input re-enables the ordinary, once-only completion.
        self.guided.actual_edit.setText("yyyggg")
        self.assertTrue(self.guided.commit_button.isEnabled())
        self.guided.commit_button.click()
        self.qt.processEvents()
        self.assertEqual(len(self.guided.state.rounds), 1)
        self.assertEqual(self.guided.state.rounds[0].actual_genes, "YYYGGG")
        self.assertEqual(len(self.calls), before_calls)

    def test_counts_target_does_not_accept_a_different_order_than_predicted(self):
        self.window.mode_combo.setCurrentIndex(self.window.mode_combo.findData("counts"))
        self.reach_actual_entry()
        self.assertEqual(self.guided.current_recipe().prediction.deterministic_result, "YYYGGG")
        self.assertTrue(self.window.target().matches("GGGYYY"))
        before_inventory = tuple(self.window.inventory)
        before_state = self.guided.state
        before_calls = len(self.calls)
        self.fill_actual("GGGYYY", 2)
        self.assertFalse(self.guided.commit_button.isEnabled())
        self.guided.complete_current_round()
        self.assertEqual(tuple(self.window.inventory), before_inventory)
        self.assertEqual(self.guided.state, before_state)
        self.assertEqual(len(self.calls), before_calls)

    def test_matching_intermediate_result_merges_once_then_reselects_automatically(self):
        recipe = self.reach_legacy_actual_entry()
        self.assertEqual(recipe.prediction.deterministic_result, "GYYGGG")
        self.fill_actual("gyyggg", 4)
        self.assertTrue(self.guided.commit_button.isEnabled())
        self.assertIn("第 1 轮成功，确认并进入下一轮", self.guided.commit_button.text())
        self.guided.commit_button.click()
        self.wait_finished()
        quantities = {item.id: item.quantity for item in self.window.inventory}
        self.assertEqual(quantities, {"center": 6, "a": 1, "b": 2, "spare": 5})
        self.assertEqual(len(self.guided.state.rounds), 1)
        self.assertEqual(self.guided.state.rounds[0].actual_genes, "GYYGGG")
        self.assertEqual(self.guided.state.rounds[0].clone_quantity, 4)
        self.assertEqual(self.guided.state.center_id, "center")
        self.assertIsNone(self.calls[-1][2])
        self.assertEqual(self.guided.center_preference, "")
        self.assertEqual(self.guided.center_combo.currentData(), "")
        replanned = next(row for row in self.calls[-1][0] if row.id == "center")
        self.assertEqual((replanned.genes, replanned.quantity), ("GYYGGG", 6))
        self.assertIsNotNone(self.guided.current_recipe())
        self.assertIsNotNone(self.guided.state.pending)
        self.assertTrue(self.guided.report.found)
        self.assertEqual(self.guided.actual_edit.text(), "")
        self.assertEqual(self.guided.completed_check.currentIndex(), 0)
        # A repeated click after the new plan is shown cannot spend it.
        self.guided.complete_current_round()
        self.assertEqual({item.id: item.quantity for item in self.window.inventory}, quantities)
        self.assertEqual(len(self.guided.state.rounds), 1)

    def test_auto_replanning_receives_actual_stock_and_new_clone_without_pinning_it(self):
        self.guided.target_edit.setText("GGGGGG")
        self.reach_legacy_actual_entry(donor_ids=("a", "b"))
        self.fill_actual("YYYGGG", 4)
        self.guided.commit_button.click()
        self.wait_finished()
        record = self.guided.state.rounds[-1]
        new_id = record.result_center_id
        self.assertNotIn(new_id, {"center", "a", "b", "spare"})
        searched_stock, target, selected_center, _ = self.calls[-1]
        self.assertIsNone(selected_center)
        self.assertEqual(target.genes, "GGGGGG")
        self.assertEqual(searched_stock, tuple(self.window.inventory))
        self.assertEqual({item.id: item.quantity for item in searched_stock}, {
            "center": 2, "a": 1, "b": 1, "spare": 5, new_id: 4})
        self.assertEqual(next(item.genes for item in searched_stock if item.id == new_id), "YYYGGG")
        self.assertEqual(self.guided.center_preference, "")
        self.assertEqual(self.guided.center_combo.currentData(), "")

    def test_explicit_fixed_center_continues_from_its_new_actual_clone(self):
        self.guided.target_edit.setText("GGGGGG")
        self.guided.center_combo.setCurrentIndex(self.guided.center_combo.findData("center"))
        self.reach_legacy_actual_entry(donor_ids=("a", "b"))
        self.fill_actual("YYYGGG", 4)
        self.guided.commit_button.click()
        self.wait_finished()
        new_id = self.guided.state.rounds[-1].result_center_id
        self.assertNotEqual(new_id, "center")
        self.assertEqual(self.calls[-1][2], new_id)
        self.assertEqual(self.guided.center_preference, new_id)
        self.assertEqual(self.guided.center_combo.currentData(), new_id)
        self.assertEqual(next(item.genes for item in self.calls[-1][0] if item.id == new_id), "YYYGGG")

    def test_auto_center_mode_survives_save_after_completion_and_restore(self):
        self.guided.target_edit.setText("GGGGGG")
        self.reach_legacy_actual_entry(donor_ids=("a", "b"))
        self.fill_actual("YYYGGG", 4)
        self.guided.commit_button.click()
        self.wait_finished()
        snapshot = self.window.snapshot()
        self.assertEqual(snapshot["guided"]["center_mode"], "")
        path = Path(self.temporary.name) / "auto-after-completion.json"
        save_project(path, snapshot)
        restored = self.new_window()
        restored.apply_project(load_project(path))
        self.assertEqual(tuple(restored.inventory), tuple(self.window.inventory))
        self.assertEqual(restored.guided.state.rounds, self.guided.state.rounds)
        self.assertEqual(restored.guided.center_preference, "")
        self.assertEqual(restored.guided.center_combo.currentData(), "")
        restored.guided.plan_button.click()
        self.wait_finished(restored.guided)
        self.assertIsNone(self.calls[-1][2])

    def test_legacy_prediction_candidates_allow_only_a_possible_result_at_each_position(self):
        self.window.inventory.extend([
            InventoryItem("ambiguous-center", "YYYYYY", 1, "旧版中央"),
            InventoryItem("ambiguous-g", "GGGGGG", 2, "供体 G"),
            InventoryItem("ambiguous-h", "HHHHHH", 2, "供体 H"),
        ])
        self.window.refresh_inventory()
        recipe = self.reach_legacy_actual_entry(
            "ambiguous-center", ("ambiguous-g", "ambiguous-g", "ambiguous-h", "ambiguous-h"))
        self.assertIsNone(recipe.prediction.deterministic_result)
        self.assertTrue(all(set(options) == {"G", "H"} for options in recipe.prediction.candidates))
        before = tuple(self.window.inventory)
        self.fill_actual("XGHGHG", 2)
        self.assertFalse(self.guided.commit_button.isEnabled())
        self.guided.complete_current_round()
        self.assertEqual(tuple(self.window.inventory), before)
        self.assertEqual(len(self.guided.state.rounds), 0)
        self.guided.actual_edit.setText("GGHGHG")
        self.assertTrue(self.guided.commit_button.isEnabled())
        self.guided.commit_button.click()
        self.wait_finished()
        self.assertEqual(len(self.guided.state.rounds), 1)
        self.assertEqual(self.guided.state.rounds[0].actual_genes, "GGHGHG")

    def test_old_mismatching_history_can_restore_and_be_undone_without_being_resubmitted(self):
        self.reach_actual_entry()
        before_inventory = tuple(self.window.inventory)
        before_pending = self.guided.state.pending
        # Versions before the UI guard accepted a different real observation.
        # The pure data API remains able to read those existing saved rounds.
        completion = guidance.complete_round(
            self.guided.state, before_inventory, "WGGYYY", 4, token=before_pending.token)
        snapshot = self.window.snapshot()
        snapshot["journey"] = completion.state.to_dict()
        snapshot["inventory"] = [dict(id=row.id, genes=row.genes, quantity=row.quantity, name=row.name)
                                 for row in completion.inventory]
        snapshot["guided"].update(stage=0, actual_input="", completed=False, center_mode=completion.center_id)
        path = Path(self.temporary.name) / "old-mismatching-completion.json"
        save_project(path, snapshot)
        restored = self.new_window()
        restored.apply_project(load_project(path))
        workspace = restored.guided
        self.assertEqual(len(workspace.state.rounds), 1)
        self.assertEqual(workspace.state.rounds[0].actual_genes, "WGGYYY")
        self.assertEqual(tuple(restored.inventory), completion.inventory)
        self.assertFalse(workspace.commit_button.isEnabled())
        self.assertTrue(workspace.undo_button.isEnabled())
        workspace.undo_button.click()
        self.assertEqual(tuple(restored.inventory), before_inventory)
        self.assertEqual(workspace.state.pending, before_pending)
        self.assertEqual(workspace.stage, 2)
        self.assertEqual(workspace.actual_edit.text(), "")
        self.assertEqual(workspace.state.rounds, ())
        self.assertFalse(workspace.commit_button.isEnabled())

    def test_actual_target_match_stops_route_search_and_keeps_new_clones(self):
        self.reach_actual_entry()
        self.fill_actual("YYYGGG", 6)
        before_calls = len(self.calls)
        self.guided.commit_button.click()
        self.qt.processEvents()
        self.assertEqual(len(self.calls), before_calls)
        self.assertFalse(self.guided.is_busy())
        self.assertIsNone(self.guided.state.pending)
        self.assertEqual(len(self.guided.state.rounds), 1)
        row = next(item for item in self.window.inventory if item.id == self.guided.state.center_id)
        self.assertEqual((row.genes, row.quantity), ("YYYGGG", 6))
        self.assertIn("已达到最终目标", self.guided.summary.text())

    def test_invalid_or_unconfirmed_actual_input_does_not_spend_inventory(self):
        self.reach_actual_entry()
        before = tuple(self.window.inventory)
        self.fill_actual("YYY", 3)
        self.assertFalse(self.guided.commit_button.isEnabled())
        self.guided.complete_current_round()
        self.assertEqual(tuple(self.window.inventory), before)
        self.assertEqual(len(self.guided.state.rounds), 0)
        self.guided.actual_edit.setText("YYYGGG")
        self.guided.completed_check.setCurrentIndex(0)
        self.guided.complete_current_round()
        self.assertEqual(tuple(self.window.inventory), before)
        self.assertEqual(len(self.guided.state.rounds), 0)

    def test_project_restores_pending_round_stage_and_unsubmitted_actual_draft(self):
        self.reach_actual_entry()
        self.fill_actual("WGGYYY", 4)
        snapshot = self.window.snapshot()
        path = Path(self.temporary.name) / "round.json"
        save_project(path, snapshot)
        restored = self.new_window()
        restored.apply_project(load_project(path))
        workspace = restored.guided
        self.assertEqual(workspace.state, self.guided.state)
        self.assertEqual(workspace.stage, 2)
        self.assertEqual(workspace.actual_edit.text(), "WGGYYY")
        self.assertEqual(workspace.actual_quantity.value(), 4)
        self.assertFalse(workspace.commit_button.isEnabled())
        before = tuple(restored.inventory)
        workspace.complete_current_round()
        self.assertEqual(tuple(restored.inventory), before)
        self.assertEqual(len(workspace.state.rounds), 0)
        workspace.actual_edit.setText("YYYGGG")
        self.assertTrue(workspace.commit_button.isEnabled())
        self.assertEqual(workspace.center_combo.currentData(), "")
        self.assertEqual(tuple(restored.inventory), tuple(self.window.inventory))
        self.assertEqual(workspace.current_recipe(), self.guided.current_recipe())

    def test_legacy_partial_round_already_planted_can_restore_and_record_actual(self):
        by_id = {item.id: item for item in self.window.inventory}
        center = by_id["spare"]
        donors = (by_id["a"], by_id["b"])
        recipe = Recipe(center, donors, predict(center.genes, [item.genes for item in donors]), self.window.target())
        self.assertFalse(recipe.guaranteed)
        self.guided.state = guidance.prepare_round(
            replace(self.guided.state, center_id=center.id), tuple(self.window.inventory), recipe)
        self.guided.stage = 2
        self.guided.render_round()
        self.assertEqual(recipe.prediction.deterministic_result, "YGGGGG")
        self.fill_actual("YGGGGG", 6)
        snapshot = self.window.snapshot()
        restored = self.new_window()
        restored.apply_project(snapshot)
        workspace = restored.guided
        self.assertEqual(workspace.stage, 2)
        self.assertEqual(workspace.current_recipe(), recipe)
        self.assertTrue(workspace.commit_button.isEnabled())
        self.assertIsNone(workspace.report)
        workspace.commit_button.click()
        self.wait_finished(workspace)
        quantities = {item.id: item.quantity for item in restored.inventory}
        self.assertEqual({key: quantities[key] for key in by_id}, {"center": 3, "a": 1, "b": 7, "spare": 4})
        self.assertEqual(len(workspace.state.rounds), 1)
        self.assertFalse(workspace.state.rounds[0].target_matched)
        self.assertEqual(workspace.state.rounds[0].actual_genes, "YGGGGG")
        self.assertIsNone(self.calls[-1][2])
        self.assertEqual(workspace.center_preference, "")
        self.assertIsNotNone(workspace.state.pending)
        self.assertEqual(workspace.current_recipe().center_id, "center")
        self.assertTrue(workspace.report.found)
        after = tuple(restored.inventory)
        workspace.complete_current_round()
        self.assertEqual(tuple(restored.inventory), after)

    def test_project_restores_completed_history_without_spending_stock_again(self):
        self.reach_actual_entry()
        self.fill_actual("YYYGGG", 3)
        self.guided.commit_button.click()
        snapshot = self.window.snapshot()
        restored = self.new_window()
        restored.apply_project(snapshot)
        self.assertEqual(restored.guided.state, self.guided.state)
        self.assertEqual(tuple(restored.inventory), tuple(self.window.inventory))
        self.assertEqual(len(restored.guided.state.rounds), 1)
        self.assertIsNone(restored.guided.state.pending)
        self.assertIn("已达到最终目标", restored.guided.summary.text())
        self.assertIn("已经达标", restored.guided.instruction_title.text())

    def test_undo_completed_result_restores_waiting_round_and_prevents_repeat(self):
        self.reach_actual_entry()
        before_inventory = tuple(self.window.inventory)
        before_pending = self.guided.state.pending
        self.fill_actual("YYYGGG", 4)
        self.guided.commit_button.click()
        self.wait_finished()
        self.assertEqual(len(self.guided.state.rounds), 1)
        self.assertTrue(self.guided.undo_button.isEnabled())
        self.guided.undo_button.click()
        self.assertEqual(tuple(self.window.inventory), before_inventory)
        self.assertEqual(self.guided.state.pending, before_pending)
        self.assertEqual(self.guided.state.center_id, "center")
        self.assertEqual(self.guided.center_preference, "")
        self.assertEqual(self.guided.center_combo.currentData(), "")
        self.assertEqual(len(self.guided.state.rounds), 0)
        self.assertEqual(self.guided.stage, 2)
        self.assertEqual(self.guided.current_recipe().center_id, "center")
        self.assertEqual(Counter(self.guided.current_recipe().donor_ids), {"a": 1, "b": 1})
        self.assertEqual(self.guided.cells[4][1].text(), "GYYGGG")
        self.assertEqual(self.guided.actual_edit.text(), "")
        self.assertIs(self.guided.completed_check.currentData(), False)
        self.assertFalse(self.guided.commit_button.isEnabled())
        self.assertFalse(self.guided.undo_button.isEnabled())
        restored_state = self.guided.state
        self.guided.undo_button.click()
        self.guided.undo_completed_round()
        self.assertEqual(self.guided.state, restored_state)
        self.assertEqual(tuple(self.window.inventory), before_inventory)

    def test_fixed_center_undo_restores_the_original_recipient_preference(self):
        self.guided.center_combo.setCurrentIndex(self.guided.center_combo.findData("center"))
        self.reach_actual_entry()
        before_inventory = tuple(self.window.inventory)
        before_pending = self.guided.state.pending
        self.fill_actual("YYYGGG", 4)
        self.guided.commit_button.click()
        new_id = self.guided.state.rounds[-1].result_center_id
        self.assertNotEqual(new_id, "center")
        self.assertEqual(self.guided.center_preference, new_id)
        self.guided.undo_button.click()
        self.assertEqual(tuple(self.window.inventory), before_inventory)
        self.assertEqual(self.guided.state.pending, before_pending)
        self.assertEqual(self.guided.center_preference, "center")
        self.assertEqual(self.guided.center_combo.currentData(), "center")
        self.assertEqual(self.guided.stage, 2)

    def test_undo_checkpoint_survives_saved_project_and_restores_before_stock(self):
        self.reach_actual_entry()
        before_inventory = tuple(self.window.inventory)
        before_pending = self.guided.state.pending
        self.fill_actual("YYYGGG", 6)
        self.guided.commit_button.click()
        path = Path(self.temporary.name) / "completed-with-undo.json"
        save_project(path, self.window.snapshot())
        restored = self.new_window()
        restored.apply_project(load_project(path))
        workspace = restored.guided
        self.assertEqual(len(workspace.state.rounds), 1)
        self.assertTrue(workspace.undo_button.isEnabled())
        workspace.undo_button.click()
        self.assertEqual(tuple(restored.inventory), before_inventory)
        self.assertEqual(workspace.state.pending, before_pending)
        self.assertEqual(workspace.stage, 2)
        self.assertEqual(workspace.actual_edit.text(), "")
        self.assertFalse(workspace.commit_button.isEnabled())
        self.assertFalse(workspace.undo_button.isEnabled())

    def test_undo_latest_completion_preserves_earlier_history(self):
        self.reach_legacy_actual_entry()
        self.fill_actual("GYYGGG", 4)
        self.guided.commit_button.click()
        self.wait_finished()
        self.assertEqual(len(self.guided.state.rounds), 1)
        first_history = self.guided.state.rounds
        before_second = tuple(self.window.inventory)
        second_pending = self.guided.state.pending
        self.assertIsNotNone(second_pending)
        self.fill_actual("YYYGGG", 1)
        self.guided.commit_button.click()
        self.assertEqual(len(self.guided.state.rounds), 2)
        self.assertTrue(self.guided.undo_button.isEnabled())
        self.guided.undo_button.click()
        # Exhausted donor rows may be re-appended; every row and quantity must
        # be restored while the original pending token still binds this round.
        self.assertEqual({row.id: row for row in self.window.inventory}, {row.id: row for row in before_second})
        self.assertEqual(self.guided.state.rounds, first_history)
        self.assertEqual(self.guided.state.pending, second_pending)
        self.assertEqual(self.guided.stage, 2)
        self.assertFalse(self.guided.undo_button.isEnabled())
        self.assertFalse(self.guided.commit_button.isEnabled())

    def test_stock_change_after_completion_disables_undo_and_never_overwrites_edit(self):
        self.reach_actual_entry()
        self.fill_actual("YYYGGG", 6)
        self.guided.commit_button.click()
        self.assertTrue(self.guided.undo_button.isEnabled())
        self.window.inventory[0] = replace(self.window.inventory[0], quantity=99)
        self.window.refresh_inventory()
        changed_inventory = tuple(self.window.inventory)
        completed_state = self.guided.state
        self.assertFalse(self.guided.undo_button.isEnabled())
        self.guided.undo_completed_round()
        self.assertEqual(tuple(self.window.inventory), changed_inventory)
        self.assertEqual(self.guided.state, completed_state)
        self.assertEqual(len(self.guided.state.rounds), 1)

    def test_route_worker_disables_undo_until_search_has_finished(self):
        self.reach_actual_entry()
        self.fill_actual("YYYGGG", 6)
        self.guided.commit_button.click()
        completed_inventory = tuple(self.window.inventory)
        completed_state = self.guided.state
        self.assertTrue(self.guided.undo_button.isEnabled())
        original = self.fake_route

        def delayed(*args, **kwargs):
            self.started.set()
            if not self.release.wait(5):
                raise RuntimeError("Test did not release route worker")
            return original(*args, **kwargs)

        with patch.object(guidance, "plan_route", delayed):
            self.guided.start_planning()
            self.assertTrue(self.started.wait(2))
            self.assertTrue(self.guided.is_busy())
            self.assertFalse(self.guided.undo_button.isEnabled())
            searching_state = self.guided.state
            self.assertEqual(searching_state.rounds, completed_state.rounds)
            self.guided.undo_completed_round()
            self.assertEqual(tuple(self.window.inventory), completed_inventory)
            self.assertEqual(self.guided.state, searching_state)
            self.release.set()
            self.wait_finished()
        self.assertTrue(self.guided.undo_button.isEnabled())

    def test_pending_round_rejects_programmatic_target_edit_and_keeps_visible_authority_in_sync(self):
        self.plan()
        before = tuple(self.window.inventory)
        pending = self.guided.state.pending
        self.guided.target_edit.setText("YYYYGG")
        self.assertEqual(self.window.target().genes, "YYYGGG")
        self.assertEqual(self.guided.target_edit.text(), "YYYGGG")
        self.assertEqual(self.guided.state.target.genes, "YYYGGG")
        self.assertEqual(self.guided.state.pending, pending)
        self.assertEqual(tuple(self.window.inventory), before)
        self.guided.reset_button.click()
        self.guided.target_edit.setText("YYYYGG")
        self.assertEqual(self.window.target().genes, "YYYYGG")
        self.assertEqual(self.guided.state.target.genes, "YYYYGG")

    def test_inventory_change_invalidates_saved_plan_without_losing_completed_history(self):
        self.reach_legacy_actual_entry()
        self.fill_actual("GYYGGG", 4)
        self.guided.commit_button.click()
        self.wait_finished()
        history = self.guided.state.rounds
        self.window.inventory[0] = replace(self.window.inventory[0], quantity=1)
        self.window.refresh_inventory()
        self.assertIsNone(self.guided.state.pending)
        self.assertEqual(self.guided.stage, 0)
        self.assertEqual(self.guided.state.rounds, history)
        self.assertIn("库存已经变化", self.guided.status.text())
        self.assertEqual(self.window.inventory[0].quantity, 1)

    def test_restoring_stale_pending_recommendation_preserves_completed_history(self):
        self.reach_legacy_actual_entry()
        self.fill_actual("GYYGGG", 4)
        self.guided.commit_button.click()
        self.wait_finished()
        snapshot = self.window.snapshot()
        snapshot["inventory"][0]["quantity"] = 1
        restored = self.new_window()
        restored.apply_project(snapshot)
        self.assertEqual(restored.guided.state.rounds, self.guided.state.rounds)
        self.assertIsNone(restored.guided.state.pending)
        self.assertEqual(restored.guided.stage, 0)
        self.assertEqual(restored.inventory[0].quantity, 1)

    def test_reset_unexecuted_recommendation_preserves_all_inventory(self):
        self.plan()
        before = tuple(self.window.inventory)
        self.guided.reset_button.click()
        self.assertEqual(tuple(self.window.inventory), before)
        self.assertIsNone(self.guided.state.pending)
        self.assertEqual(self.guided.stage, 0)
        self.assertEqual(len(self.guided.state.rounds), 0)

    def test_stock_change_while_worker_runs_discards_stale_recommendation(self):
        original = self.fake_route

        def delayed(*args, **kwargs):
            self.started.set()
            if not self.release.wait(5):
                raise RuntimeError("Test did not release route worker")
            return original(*args, **kwargs)

        with patch.object(guidance, "plan_route", delayed):
            self.guided.plan_button.click()
            self.assertTrue(self.started.wait(2))
            self.assertTrue(self.guided.is_busy())
            for widget in (self.guided.target_edit, self.guided.center_combo, self.guided.add_button,
                           self.guided.capture_button, self.guided.actual_edit):
                self.assertFalse(widget.isEnabled())
            self.window.inventory[0] = replace(self.window.inventory[0], quantity=1)
            self.window.refresh_inventory()
            self.release.set()
            self.wait_finished()
        self.assertIsNone(self.guided.state.pending)
        self.assertIn("旧结果未应用", self.guided.status.text())


if __name__ == "__main__":
    unittest.main()
