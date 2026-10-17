"""Real Qt checks for shared-stock concurrent planter instructions."""

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


ROOT = Path(__file__).resolve().parents[1]
if (ROOT / ".deps").exists():
    sys.path.insert(0, str(ROOT / ".deps"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QWidget, QLineEdit, QComboBox
except ImportError:
    QT_AVAILABLE = False
else:
    QT_AVAILABLE = True
    from rust_breeder import parallel_guidance as gui
    from rust_breeder.engine import predict
    from rust_breeder.models import InventoryItem, Recipe, Target
    from rust_breeder.parallel_models import ParallelReport, ParallelStep, ParallelWave
    from rust_breeder.parallel import plan_parallel
    from rust_breeder.widgets import STYLE
    from rust_breeder import app as application


if QT_AVAILABLE:
    class Owner(QWidget):
        def __init__(self):
            super().__init__()
            self.setStyleSheet(STYLE)
            self.inventory = [
                InventoryItem("center", "GYYGGG", 2, "中央样本"),
                InventoryItem("a", "YHYGGG", 2, "供体 A"),
                InventoryItem("b", "YGGGGG", 2, "供体 B"),
                InventoryItem("unused", "WHGXGG", 4, "备用苗"),
            ]
            self.project_crop = "hemp"
            self.target_edit = QLineEdit("YYYGGG", self)
            self.target_edit.hide()
            self.mode_combo = QComboBox(self)
            self.mode_combo.addItem("counts", "counts")
            self.mode_combo.addItem("exact", "exact")
            self.mode_combo.setCurrentIndex(1)
            self.mode_combo.hide()
            self.other_busy = False
            self.other_pending = False
            self.changes = 0
            self.edits = []
            self.parallel = None
            self.numbers = {item.id: number for number, item in enumerate(self.inventory, 10)}

        def target(self):
            return Target(self.target_edit.text(), self.mode_combo.currentData())

        def work_busy(self, exclude=None):
            return self.other_busy or (self.parallel is not None and exclude is not self.parallel and self.parallel.is_busy())

        def can_plan(self, page):
            return not self.other_busy and not self.other_pending

        def can_edit_stock(self):
            return not self.work_busy() and not self.other_pending and not (
                self.parallel and self.parallel.state.pending is not None)

        def seed_number(self, item):
            key = item.id if hasattr(item, "id") else item
            if key not in self.numbers:
                self.numbers[key] = len(self.numbers) + 10
            return self.numbers[key]

        def by_id(self, seed_id):
            return next((item for item in self.inventory if item.id == seed_id), None)

        def changed(self, **kwargs):
            self.changes += 1

        def set_busy(self, busy):
            pass

        def refresh_inventory(self):
            if self.parallel:
                self.parallel.refresh_from_owner()

        def add_seed(self):
            self.edits.append("add")

        def bulk_add(self):
            self.edits.append("bulk")

        def capture_genes(self):
            self.edits.append("capture")

        def edit_seed(self, seed_id):
            self.edits.append(seed_id)


@unittest.skipUnless(QT_AVAILABLE, "Install PySide6 for the parallel workspace checks.")
class ParallelGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        for filename in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "consola.ttf"):
            font = Path("C:/Windows/Fonts") / filename
            if font.exists():
                QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        self.owner = Owner()
        self.page = gui.ParallelWorkspace(self.owner)
        self.owner.parallel = self.page
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = []
        self.planner = patch.object(gui, "plan_parallel_process", self.fake_plan)
        self.planner.start()
        self.page.refresh_from_owner()

    def tearDown(self):
        self.release.set()
        if self.page.worker is not None:
            self.page.worker.requestInterruption()
            self.page.worker.wait(5000)
        self.qt.processEvents()
        self.planner.stop()
        self.owner.close()
        self.owner.deleteLater()
        self.qt.processEvents()

    def report(self, boxes=(1, 2)):
        rows = {item.id: item for item in self.owner.inventory}
        recipe = Recipe(rows["center"], (rows["a"], rows["b"]),
                        predict(rows["center"].genes, [rows["a"].genes, rows["b"].genes]), self.owner.target())
        steps = tuple(ParallelStep(box_id, recipe, "YYYGGG", f"out-{box_id}", 1) for box_id in boxes)
        return ParallelReport((ParallelWave(1, steps),), True, True, 24, "found", box_count=3)

    def fake_plan(self, inventory, target, **options):
        self.calls.append((tuple(inventory), target, options))
        self.started.set()
        while not self.release.wait(.01):
            if options["cancel"]():
                return ParallelReport((), False, False, 1, "cancelled")
        return self.report()

    def wait_finished(self):
        deadline = time.monotonic() + 5
        while self.page.is_busy() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.qt.processEvents()
        self.assertFalse(self.page.is_busy())

    def arrange(self):
        self.page.accept_route(self.report())
        self.assertIsNotNone(self.page.state.pending)

    def fill(self, box_id, genes="YYYGGG", quantity=1):
        box = self.page.box_pages[box_id]
        box.actual_edit.setText(genes)
        box.actual_quantity.setValue(quantity)

    def test_defaults_and_constant_spacing_warning(self):
        self.assertEqual(self.page.box_count.value(), 3)
        self.assertEqual(self.page.main_box.currentData(), 1)
        self.assertEqual(len(self.page.box_pages), 3)
        self.assertEqual(self.page.box_count.minimum(), 2)
        self.assertEqual(self.page.box_count.maximum(), 6)
        self.assertEqual(self.page.objective.currentData(), "stock")
        self.assertEqual([self.page.objective.itemData(i) for i in range(self.page.objective.count())],
                         ["rounds", "plants", "stock"])
        self.assertEqual(self.page.objective.itemText(2), "智能匹配（基于已有基因库）")
        self.assertIn("其他箱", self.page.spacing_notice.text())
        self.assertIn("影响范围", self.page.spacing_notice.text())
        self.assertNotIn("米", self.page.spacing_notice.text())

    def test_count_and_main_box_update_all_captions(self):
        self.page.box_count.setValue(5)
        self.page.main_box.setCurrentIndex(self.page.main_box.findData(4))
        self.assertEqual(self.page.box_tabs.count(), 5)
        self.assertIn("主箱", self.page.box_tabs.tabText(3))
        self.assertIn("副箱", self.page.box_tabs.tabText(0))
        self.page.box_count.setValue(2)
        self.assertEqual(self.page.main_box.currentData(), 1)
        self.assertEqual(len(self.page.box_pages), 2)

    def test_arrangement_shows_seed_number_prediction_and_idle_box(self):
        self.arrange()
        box = self.page.box_pages[1]
        self.assertEqual(box.cells[4][1].text(), "010 号苗")
        self.assertEqual(box.cells[4][2].text(), "GYYGGG")
        self.assertIn("YYYGGG", box.cells[4][3].text())
        self.assertTrue(box.actual_edit.isEnabled())
        self.assertFalse(self.page.box_pages[3].actual_edit.isEnabled())
        self.assertFalse(self.page.plan_button.isEnabled())
        self.assertEqual(box.actual_edit.text(), "")

    def test_all_boxes_must_match_before_any_stock_changes(self):
        before = tuple(self.owner.inventory)
        self.arrange()
        self.fill(1)
        self.assertFalse(self.page.commit_button.isEnabled())
        self.fill(2, "GYGXXG")
        self.assertFalse(self.page.commit_button.isEnabled())
        self.page.complete_current_wave()
        self.assertEqual(tuple(self.owner.inventory), before)
        self.assertEqual(len(self.page.state.waves), 0)
        self.assertIn("不一致", self.page.box_pages[2].assessment.text())

    def test_atomic_confirmation_uses_actual_quantities_and_cannot_repeat(self):
        self.arrange()
        self.fill(1, "yyyggg", 4)
        self.fill(2, quantity=2)
        self.assertTrue(self.page.commit_button.isEnabled())
        self.assertIn("第 1 批核对成功", self.page.commit_button.text())
        self.page.commit_button.click()
        rows = {item.id: item for item in self.owner.inventory}
        self.assertEqual(set(rows), {"out-1", "out-2", "unused"})
        self.assertEqual(rows["out-1"].quantity, 4)
        self.assertEqual(rows["out-2"].quantity, 2)
        self.assertEqual(len(self.page.state.waves), 1)
        self.assertIsNone(self.page.state.pending)
        after = tuple(self.owner.inventory)
        self.page.complete_current_wave()
        self.assertEqual(tuple(self.owner.inventory), after)

    def test_undo_restores_full_shared_stock_and_pending_batch(self):
        before = tuple(self.owner.inventory)
        self.arrange()
        self.fill(1)
        self.fill(2)
        self.page.complete_current_wave()
        self.page.undo_completed_wave()
        self.assertEqual(Counter(self.owner.inventory), Counter(before))
        self.assertEqual(len(self.page.state.waves), 0)
        self.assertIsNotNone(self.page.state.pending)
        self.assertEqual(self.page.box_pages[1].actual_edit.text(), "")
        self.assertFalse(self.page.commit_button.isEnabled())

    def test_reset_does_not_change_stock(self):
        before = tuple(self.owner.inventory)
        self.arrange()
        self.fill(1)
        self.page.reset_button.click()
        self.assertIsNone(self.page.state.pending)
        self.assertEqual(tuple(self.owner.inventory), before)
        self.assertEqual(self.page.box_pages[1].actual_edit.text(), "")

    def test_shared_mode_lock_guards_handlers(self):
        before = tuple(self.owner.inventory)
        self.owner.other_pending = True
        self.page.start_planning()
        self.page.add_seed()
        self.page.bulk_add()
        self.page.capture_genes()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.owner.edits, [])
        self.assertEqual(tuple(self.owner.inventory), before)

    def test_stock_edit_handlers_also_block_while_batch_pending(self):
        self.arrange()
        self.page.add_seed()
        self.page.bulk_add()
        self.page.capture_genes()
        self.page.inventory_table.selectRow(0)
        self.page.edit_selected()
        self.assertEqual(self.owner.edits, [])

    def test_worker_keeps_gui_responsive_and_cleans_ownership(self):
        ticks = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        before = tuple(self.owner.inventory)
        started = time.monotonic()
        self.page.plan_button.click()
        self.assertLess(time.monotonic() - started, .3)
        QTest.qWait(100)
        self.assertTrue(self.page.is_busy())
        self.assertGreater(len(ticks), 4)
        self.assertFalse(self.page.box_count.isEnabled())
        self.assertFalse(self.page.capture_button.isEnabled())
        self.assertFalse(self.page.objective.isEnabled())
        self.assertEqual(tuple(self.owner.inventory), before)
        self.release.set()
        self.wait_finished()
        timer.stop()
        self.assertIsNotNone(self.page.state.pending)
        self.assertEqual(tuple(self.owner.inventory), before)

    def test_stopping_does_not_arrange_or_consume_stock(self):
        before = tuple(self.owner.inventory)
        self.page.start_planning()
        QTest.qWait(30)
        self.page.start_planning()
        self.wait_finished()
        self.assertIsNone(self.page.state.pending)
        self.assertEqual(tuple(self.owner.inventory), before)

    def test_stale_context_does_not_apply_result(self):
        context = self.page.context()
        self.page.clone_yield.setValue(2)
        self.page.accept_route(self.report(), context)
        self.assertIsNone(self.page.state.pending)
        self.assertIn("旧方案未应用", self.page.status.text())

    def test_no_complete_route_shows_brief_notice_without_arranging(self):
        report = ParallelReport((), False, False, 120000, "budget", limits=("combinations",))
        self.page.accept_route(report)
        self.assertIsNone(self.page.state.pending)
        self.assertIn("暂未找到", self.page.action_notice.title.text())
        self.assertEqual(self.page.action_notice.timer.interval(), 5000)

    def test_project_roundtrip_preserves_pending_and_separate_drafts(self):
        self.page.objective.setCurrentIndex(self.page.objective.findData("stock"))
        self.arrange()
        self.fill(1, quantity=3)
        self.fill(2, "GYGXXG", 2)
        data = self.page.project_fields()
        self.page.reset_pending()
        self.page.restore_project(data)
        self.assertIsNotNone(self.page.state.pending)
        self.assertEqual(self.page.box_pages[1].actual_edit.text(), "YYYGGG")
        self.assertEqual(self.page.box_pages[1].actual_quantity.value(), 3)
        self.assertEqual(self.page.box_pages[2].actual_edit.text(), "GYGXXG")
        self.assertFalse(self.page.commit_button.isEnabled())
        self.assertEqual(self.page.objective.currentData(), "stock")
        self.assertFalse(self.page.objective.isEnabled())

    def test_stock_objective_is_sent_to_background_planner(self):
        self.page.objective.setCurrentIndex(self.page.objective.findData("stock"))
        self.release.set()
        self.page.start_planning()
        self.wait_finished()
        self.assertEqual(self.calls[0][2]["objective"], "stock")

    def test_old_project_without_objective_uses_stock_default(self):
        self.page.objective.setCurrentIndex(self.page.objective.findData("stock"))
        self.page.restore_project({})
        self.assertEqual(self.page.objective.currentData(), "stock")

    def test_full_future_stock_is_validated_before_arranging_first_wave(self):
        report = self.report()
        impossible_later = ParallelWave(2, report.waves[0].steps)
        bad = replace(report, waves=(*report.waves, impossible_later))
        self.page.accept_route(bad)
        self.assertIsNone(self.page.state.pending)
        self.assertIn("方案未应用", self.page.status.text())

    def test_changed_stock_invalidates_pending_without_overwriting_stock(self):
        self.arrange()
        self.owner.inventory[-1] = replace(self.owner.inventory[-1], quantity=9)
        before = tuple(self.owner.inventory)
        self.page.refresh_from_owner()
        self.assertIsNone(self.page.state.pending)
        self.assertEqual(tuple(self.owner.inventory), before)
        self.assertIn("库存", self.page.status.text())

    def test_target_can_be_changed_on_parallel_page(self):
        self.page.target_edit.setText("GGGYYY")
        self.page.mode.setCurrentIndex(self.page.mode.findData("counts"))
        self.assertEqual(self.owner.target().genes, "GGGYYY")
        self.assertEqual(self.owner.target().mode, "counts")

    def test_real_forked_route_names_which_box_receives_intermediate_clones(self):
        self.owner.inventory = [InventoryItem(str(index), genes) for index, genes in enumerate(
            ("HGYGHG", "HGGGYY", "HYYGYG", "YHYYHH", "GYHYYY", "YYHYYY", "YHYYHG"))]
        self.owner.mode_combo.setCurrentIndex(self.owner.mode_combo.findData("counts"))
        self.page.refresh_from_owner()
        report = plan_parallel(self.owner.inventory, self.owner.target(), max_donors=2,
                               max_waves=2, max_seconds=3)
        self.assertTrue(report.found)
        self.assertEqual([len(wave.steps) for wave in report.waves], [2, 1])
        self.page.accept_route(report)
        for step in report.waves[0].steps:
            self.assertIn("第 2 批", self.page.box_pages[step.box_id].purpose.text())
            self.assertIn("1 号箱供体", self.page.box_pages[step.box_id].purpose.text())
            self.fill(step.box_id, step.expected_genes)
        self.assertTrue(self.page.commit_button.isEnabled())
        with patch.object(self.page, "start_planning") as replan:
            self.page.complete_current_wave()
        replan.assert_called_once_with()
        remaining = {item.id: item for item in self.owner.inventory}
        self.assertIn("4", remaining)  # The later main-box recipient stays in stock.
        self.assertEqual(len(remaining), 3)
        self.assertEqual(len(self.page.state.waves), 1)


@unittest.skipUnless(QT_AVAILABLE, "Install PySide6 for main window integration checks.")
class MainWindowParallelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ParallelGuiTests.setUpClass()
        cls.qt = QApplication.instance()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="parallel-main-", dir=ROOT / "artifacts")
        self.autosave = Path(self.temporary.name) / "data" / "autosave.json"
        self.patch = patch.object(application, "AUTOSAVE", self.autosave)
        self.patch.start()
        self.owner = application.MainWindow(restore=False)
        self.windows = [self.owner]
        self.owner.inventory = [
            InventoryItem("center", "GYYGGG", 2, "中央样本"),
            InventoryItem("a", "YHYGGG", 2, "供体 A"),
            InventoryItem("b", "YGGGGG", 2, "供体 B"),
        ]
        self.owner.mode_combo.setCurrentIndex(self.owner.mode_combo.findData("exact"))
        self.owner.refresh_inventory()
        self.owner.workspaces.setCurrentIndex(1)
        self.page = self.owner.parallel
        self.owner.autosave_timer.stop()

    def tearDown(self):
        for window in self.windows:
            for page in window._pages:
                if page.worker is not None:
                    page.worker.requestInterruption()
                    page.worker.wait(5000)
            self.qt.processEvents()
            window.autosave_timer.stop()
            window.dirty = False
            window.close()
            window.deleteLater()
        self.qt.processEvents()
        self.patch.stop()
        self.temporary.cleanup()

    report = ParallelGuiTests.report
    arrange = ParallelGuiTests.arrange
    fill = ParallelGuiTests.fill

    def test_pending_batch_locks_other_mode_and_direct_handler(self):
        self.arrange()
        self.assertFalse(self.owner.workspaces.isTabEnabled(0))
        self.assertTrue(self.owner.workspaces.isTabEnabled(1))
        self.assertFalse(self.owner.new_button.isEnabled())
        self.assertFalse(self.owner.can_edit_stock())
        before = tuple(self.owner.inventory)
        self.owner.guided.start_planning()
        self.assertIsNone(self.owner.guided.worker)
        self.assertEqual(tuple(self.owner.inventory), before)
        self.page.reset_pending()
        self.assertTrue(self.owner.workspaces.isTabEnabled(0))
        self.assertTrue(self.owner.can_edit_stock())

    def test_internal_auto_save_restores_batch_drafts_mode_and_seed_numbers(self):
        self.arrange()
        self.fill(1, quantity=4)
        self.fill(2, "GYGXXG", 2)
        expected_numbers = dict(self.owner.seed_numbers)
        self.assertTrue(self.owner.save_local_project())
        self.assertTrue(self.autosave.exists())
        restored = application.MainWindow(restore=True)
        self.windows.append(restored)
        restored.autosave_timer.stop()
        self.assertEqual(restored.workspaces.currentIndex(), 1)
        self.assertIsNotNone(restored.parallel.state.pending)
        self.assertEqual(restored.parallel.box_pages[1].actual_edit.text(), "YYYGGG")
        self.assertEqual(restored.parallel.box_pages[1].actual_quantity.value(), 4)
        self.assertEqual(restored.parallel.box_pages[2].actual_edit.text(), "GYGXXG")
        self.assertFalse(restored.parallel.commit_button.isEnabled())
        self.assertFalse(restored.workspaces.isTabEnabled(0))
        self.assertEqual(restored.seed_numbers, expected_numbers)
        self.assertEqual(restored.inventory, self.owner.inventory)

    def test_default_window_keeps_all_parallel_inputs_visible(self):
        self.arrange()
        self.owner.resize(1280, 800)
        self.owner.show()
        QTest.qWait(30)
        self.assertEqual(self.owner.size().toTuple(), (1280, 800))
        scroll = self.owner.workspaces.widget(1)
        self.assertEqual(scroll.verticalScrollBar().maximum(), 0)
        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        self.owner.resize(1120, 700)
        QTest.qWait(20)
        self.assertEqual(self.owner.size().toTuple(), (1120, 700))
        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)

    def test_actual_isolated_process_delivers_forked_plan_without_blocking_window(self):
        self.owner.inventory = [InventoryItem(str(index), genes) for index, genes in enumerate(
            ("HGYGHG", "HGGGYY", "HYYGYG", "YHYYHH", "GYHYYY", "YYHYYY", "YHYYHG"))]
        self.owner.mode_combo.setCurrentIndex(self.owner.mode_combo.findData("counts"))
        self.owner.refresh_inventory()
        self.page.max_waves.setValue(2)
        before = tuple(self.owner.inventory)
        real_process = gui.plan_parallel_process

        def bounded(inventory, target, **options):
            return real_process(inventory, target, **{
                **options, "max_donors": 2, "max_seconds": 3, "max_waves": 2})

        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        with patch.object(gui, "plan_parallel_process", bounded):
            started = time.monotonic()
            self.page.start_planning()
            self.assertLess(time.monotonic() - started, .3)
            deadline = time.monotonic() + 8
            while self.page.is_busy() and time.monotonic() < deadline:
                QTest.qWait(10)
        timer.stop()
        self.qt.processEvents()
        self.assertFalse(self.page.is_busy())
        self.assertIsNotNone(self.page.state.pending, self.page.status.text())
        self.assertGreater(len(ticks), 4)
        self.assertLess(max(b - a for a, b in zip(ticks, ticks[1:])), .8)
        self.assertEqual(len(self.page.state.pending.wave.steps), 2)
        self.assertEqual(tuple(self.owner.inventory), before)
        self.assertFalse(self.owner.workspaces.isTabEnabled(0))


if __name__ == "__main__":
    unittest.main()
