"""Real main-window target discovery, cached selection and stock safety."""
from __future__ import annotations

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
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
except ImportError:
    QT_AVAILABLE = False
else:
    QT_AVAILABLE = True
    from rust_breeder import app as application, target_controller
    from rust_breeder.engine import predict
    from rust_breeder.i18n import get_language, localize_tree, set_language
    from rust_breeder.models import InventoryItem, Recipe, Target
    from rust_breeder.parallel_models import ParallelReport, ParallelStep, ParallelWave
    from rust_breeder.routes import RouteReport, RouteStep
    from rust_breeder.target_discovery import TargetDiscoveryReport, TargetOption, green_targets


@unittest.skipUnless(QT_AVAILABLE, "Install PySide6 for target-discovery integration checks.")
class TargetDiscoveryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="target-integration-", dir=ROOT / "artifacts")
        self.autosave_patch = patch.object(application, "AUTOSAVE", Path(self.temp.name) / "autosave.json")
        self.autosave_patch.start()
        self.previous_language = get_language()
        set_language("zh_CN")
        self.window = application.MainWindow(restore=False)
        self.rows = (InventoryItem("center", "GYYGGG", 1),
                     InventoryItem("a", "YHYGGG", 1),
                     InventoryItem("b", "YGGGGG", 1))
        self.window.inventory = list(self.rows)
        self.window.refresh_inventory()
        self.window.autosave_timer.stop()
        self.controller = self.window.target_discovery
        self.releases = []
        self.calls = []
        self.planner_patch = patch.object(target_controller, "plan_targets_process", self.discover)
        self.planner_patch.start()

    def tearDown(self):
        for release in self.releases:
            release.set()
        for page in self.window._pages:
            if page.worker is not None:
                page.worker.requestInterruption()
                page.worker.wait(5000)
        if self.controller.worker is not None:
            self.controller.worker.requestInterruption()
            self.controller.worker.wait(5000)
        self.qt.processEvents()
        if self.controller.dialog is not None:
            self.controller.dialog.reject()
        self.window.autosave_timer.stop()
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        set_language(self.previous_language)
        self.planner_patch.stop()
        self.autosave_patch.stop()
        self.temp.cleanup()

    def wait_finished(self):
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: loop.quit() if self.controller.worker is None else None)
        timer.start()
        QTimer.singleShot(5000, loop.quit)
        if self.controller.worker is not None:
            loop.exec()
        timer.stop()
        self.qt.processEvents()
        self.assertIsNone(self.controller.worker)

    def route(self, target, mode, inventory):
        if target.matches("GYYGGG") or target.matches("YGGGGG"):
            center = next(row for row in inventory if target.matches(row.genes))
            if mode == "parallel":
                return ParallelReport((), True, True, 0, "already_target")
            recipe = Recipe(center, (), predict(center.genes, ()), target)
            return RouteReport((), (recipe,), True, True, 0, "already_target")
        by_id = {row.id: row for row in inventory}
        center, donors = by_id["center"], (by_id["a"], by_id["b"])
        prediction = predict(center.genes, tuple(row.genes for row in donors))
        self.assertEqual(prediction.deterministic_result, "YYYGGG")
        recipe = Recipe(center, donors, prediction, target)
        if mode == "parallel":
            step = ParallelStep(1, recipe, "YYYGGG", "output", 1)
            return ParallelReport((ParallelWave(1, (step,)),), True, True, 12, "found")
        step = RouteStep(recipe, "YYYGGG", "output", 1)
        return RouteReport((step,), (recipe,), True, True, 12, "exhausted")

    def discover(self, inventory, *, mode, **settings):
        self.calls.append((tuple(inventory), mode, settings))
        settings.get("progress", lambda *_: None)(7, 7)
        options = []
        for target in green_targets():
            y = target.genes.count("Y")
            if y in (1, 2, 3):
                options.append(TargetOption(target, "reachable" if y == 3 else "already_owned",
                                            self.route(target, mode, inventory)))
            else:
                options.append(TargetOption(target, "unknown", reason="budget"))
        return TargetDiscoveryReport(tuple(options), mode, evaluated=12)

    def open_and_finish(self, page=None):
        page = page or self.window.guided
        dialog = self.window.open_target_selector(page)
        self.assertIsNotNone(dialog)
        self.wait_finished()
        self.assertEqual(len(dialog.cards), 7)
        return dialog

    def choose(self, y_count):
        return next(option for option in self.controller.report.options
                    if option.target.genes.count("Y") == y_count)

    def test_filter_is_read_only_and_caches_a_complete_route(self):
        before = self.window.snapshot()
        dialog = self.open_and_finish()
        self.assertEqual(self.window.inventory, list(self.rows))
        self.assertEqual(self.window.snapshot(), before)
        self.assertIsNone(self.window.guided.state.pending)
        self.assertIsNone(self.window.parallel.state.pending)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], "sequential")
        self.assertEqual(sum(card.action_button is not None for card in dialog.cards), 3)

    def test_choose_card_changes_count_target_atomically_and_reuses_route(self):
        self.window.target_edit.setText("GGGGGG")
        self.window.mode_combo.setCurrentIndex(self.window.mode_combo.findData("exact"))
        self.open_and_finish()
        option = self.choose(3)
        with patch.object(self.window.guided, "start_planning", side_effect=AssertionError("Cached selection must not search again")):
            self.assertTrue(self.controller.apply_option(option))
        self.assertEqual(self.window.target(), Target("YYYGGG", "counts"))
        self.assertIs(self.window.guided.report, option.report)
        self.assertIsNotNone(self.window.guided.state.pending)
        self.assertEqual(self.window.guided.current_recipe().prediction.deterministic_result, "YYYGGG")
        self.assertEqual(self.window.inventory, list(self.rows))
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.controller.dialog)

    def test_parallel_card_applies_first_batch_without_consuming_stock(self):
        self.open_and_finish(self.window.parallel)
        option = self.choose(3)
        with patch.object(self.window.parallel, "start_planning", side_effect=AssertionError("Must use cached parallel route")):
            self.assertTrue(self.controller.apply_option(option))
        self.assertEqual(self.calls[0][1], "parallel")
        self.assertIs(self.window.parallel.report, option.report)
        self.assertIsNotNone(self.window.parallel.state.pending)
        self.assertIsNone(self.window.guided.state.pending)
        self.assertEqual(self.window.inventory, list(self.rows))
        self.assertEqual(self.window.target(), Target("YYYGGG", "counts"))

    def test_inventory_change_rejects_a_stale_card(self):
        self.open_and_finish()
        option = self.choose(3)
        self.window.inventory[0] = replace(self.window.inventory[0], quantity=2)
        self.window.refresh_inventory()
        before = self.window.snapshot()
        self.assertFalse(self.controller.apply_option(option))
        self.assertEqual(self.window.snapshot(), before)
        self.assertIsNone(self.window.guided.state.pending)
        self.assertIn("变化", self.controller.dialog.status.text())

    def test_planning_setting_change_rejects_a_stale_card(self):
        self.open_and_finish()
        option = self.choose(3)
        self.window.guided.clone_yield.setValue(2)
        self.assertFalse(self.controller.apply_option(option))
        self.assertIsNone(self.window.guided.state.pending)
        self.assertEqual(self.window.inventory, list(self.rows))

    def test_project_and_workspace_changes_reject_stale_cards(self):
        for change in (lambda: setattr(self.window, "current_project_id", "another-project"),
                       lambda: self.window.workspaces.setCurrentIndex(1)):
            self.window.current_project_id = ""
            self.window.workspaces.setCurrentIndex(0)
            self.open_and_finish()
            option = self.choose(3)
            change()
            self.assertFalse(self.controller.apply_option(option))
            self.assertIsNone(self.window.guided.state.pending)
        self.window.current_project_id = ""

    def test_pending_assignment_blocks_filter_and_late_selection(self):
        self.open_and_finish()
        option = self.choose(3)
        self.window.guided.accept_route(option.report)
        before = self.window.snapshot()
        self.assertFalse(self.controller.apply_option(option))
        self.assertIsNone(self.window.open_target_selector(self.window.parallel))
        self.assertEqual(self.window.snapshot(), before)
        self.assertEqual(len(self.calls), 1)

    def test_language_switch_preserves_options_and_the_chosen_route(self):
        dialog = self.open_and_finish()
        option = self.choose(3)
        original_report = self.controller.report
        for language in ("en", "ru", "zh_CN"):
            set_language(language)
            localize_tree(self.window)
            self.qt.processEvents()
            self.assertIs(self.controller.report, original_report)
            self.assertEqual(len(dialog.cards), 7)
            self.assertEqual(self.window.inventory, list(self.rows))
        self.assertTrue(self.controller.apply_option(option))
        self.assertIs(self.window.guided.report, option.report)

    def test_owned_parallel_combination_uses_no_batch_and_keeps_stock(self):
        self.open_and_finish(self.window.parallel)
        option = self.choose(2)
        self.assertEqual(option.status, "already_owned")
        self.assertTrue(self.controller.apply_option(option))
        self.assertEqual(self.window.target(), option.target)
        self.assertIsNone(self.window.parallel.state.pending)
        self.assertEqual(self.window.inventory, list(self.rows))
        self.assertEqual(self.window.parallel.report.rounds, 0)

    def test_owned_sequential_combination_requires_no_crossbreeding_confirmation(self):
        self.open_and_finish()
        option = self.choose(2)
        self.assertEqual(option.status, "already_owned")
        self.assertTrue(self.controller.apply_option(option))
        self.assertEqual(self.window.target(), option.target)
        self.assertIsNone(self.window.guided.state.pending)
        self.assertEqual(self.window.inventory, list(self.rows))
        self.assertEqual(self.window.guided.report.rounds, 0)
        self.assertTrue(self.window.can_edit_stock())

    def test_reachable_card_button_uses_the_connected_cached_route(self):
        dialog = self.open_and_finish()
        option = self.choose(3)
        card = next(card for card in dialog.cards if card.option is option)
        card.action_button.click()
        self.assertIs(self.window.guided.report, option.report)
        self.assertIsNotNone(self.window.guided.state.pending)
        self.assertEqual(self.window.inventory, list(self.rows))
        self.assertIsNone(self.controller.dialog)

    def test_mismatched_target_route_is_rejected_before_mutating_the_target(self):
        original = self.discover

        def mismatch(inventory, *, mode, **settings):
            report = original(inventory, mode=mode, **settings)
            route = next(option.report for option in report.options if option.target.genes.count("Y") == 3)
            options = tuple(TargetOption(option.target, "reachable", route)
                            if option.target.genes.count("Y") == 4 else option for option in report.options)
            return replace(report, options=options)

        with patch.object(target_controller, "plan_targets_process", mismatch):
            self.open_and_finish()
        before = self.window.snapshot()
        self.assertFalse(self.controller.apply_option(self.choose(4)))
        self.assertEqual(self.window.snapshot(), before)
        self.assertIsNone(self.window.guided.state.pending)

    def test_unknown_or_copied_options_cannot_be_applied(self):
        self.open_and_finish()
        option = self.choose(3)
        self.assertFalse(self.controller.apply_option(replace(option)))
        self.assertFalse(self.controller.apply_option(self.choose(0)))
        self.assertIsNone(self.window.guided.state.pending)
        self.assertEqual(self.window.inventory, list(self.rows))

    def test_search_locks_stock_until_queued_finished_and_closes_safely(self):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def delayed(inventory, *, mode, **settings):
            started.set()
            release.wait(3)
            return self.discover(inventory, mode=mode, **settings)

        with patch.object(target_controller, "plan_targets_process", delayed):
            dialog = self.window.open_target_selector(self.window.guided)
            worker = self.controller.worker
            self.assertTrue(started.wait(2))
            self.assertTrue(self.window.work_busy())
            self.assertFalse(self.window.can_edit_stock())
            self.assertFalse(self.window.guided.capture_button.isEnabled())
            self.assertEqual(dialog.cards, [])
            dialog.reject()
            self.assertTrue(worker.isInterruptionRequested())
            release.set()
            self.assertTrue(worker.wait(5000))
            self.assertIs(self.controller.worker, worker)
            self.assertTrue(self.window.work_busy())
            self.wait_finished()
            self.assertIsNone(self.controller.dialog)
            self.assertTrue(self.window.can_edit_stock())
            self.assertEqual(self.window.inventory, list(self.rows))

    def test_empty_library_shows_help_without_starting_a_worker(self):
        self.window.inventory.clear()
        self.window.refresh_inventory()
        dialog = self.window.open_target_selector(self.window.guided)
        self.assertIsNotNone(dialog)
        self.assertIsNone(self.controller.worker)
        self.assertEqual(dialog.cards, [])
        self.assertIn("基因库为空", dialog.status.text())
        self.assertEqual(self.calls, [])
        self.assertTrue(self.window.can_edit_stock())

    def test_worker_failure_unlocks_stock_and_allows_a_fresh_search(self):
        with patch.object(target_controller, "plan_targets_process", side_effect=RuntimeError("calculation failed")), \
                self.assertLogs(target_controller.LOGGER, level="ERROR"):
            dialog = self.window.open_target_selector(self.window.guided)
            self.wait_finished()
        self.assertEqual(dialog.cards, [])
        self.assertEqual(dialog.status.text(), "calculation failed")
        self.assertFalse(dialog.searching)
        self.assertTrue(self.window.can_edit_stock())
        self.assertEqual(self.window.inventory, list(self.rows))
        self.open_and_finish()
        self.assertIsNotNone(self.controller.report)

    def test_old_target_inputs_are_hidden_in_both_workspaces(self):
        self.window.show()
        self.qt.processEvents()
        for page in self.window._pages:
            self.assertTrue(page.target_edit.isHidden())
            self.assertTrue(page.mode.isHidden())
            self.assertFalse(page.select_target_button.isHidden())


if __name__ == "__main__":
    unittest.main()
