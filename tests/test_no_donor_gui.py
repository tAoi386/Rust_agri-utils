"""No-route candidates and existing target clones cannot invent a harvest."""
from __future__ import annotations
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if (PROJECT_ROOT / '.deps').exists():
    sys.path.insert(0, str(PROJECT_ROOT / '.deps'))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    QT_AVAILABLE = False
else:
    QT_AVAILABLE = True
    from rust_breeder import app as application
    from rust_breeder.models import InventoryItem
    from rust_breeder.routes import plan_route


@unittest.skipUnless(QT_AVAILABLE, 'Install PySide6 to run Qt integration checks.')
class NoDonorWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='no-donor-', dir=PROJECT_ROOT / 'artifacts')
        self.autosave_patch = patch.object(application, 'AUTOSAVE', Path(self.temporary.name) / 'autosave.json')
        self.autosave_patch.start()
        self.windows = []
        self.window = self.new_window()
        self.guided = self.window.guided

    def tearDown(self):
        for window in reversed(self.windows):
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
        self.autosave_patch.stop()
        self.temporary.cleanup()

    def new_window(self):
        window = application.MainWindow(restore=False)
        window.autosave_timer.stop()
        self.windows.append(window)
        return window

    def arrange(self, genes=('YYGWYY',)):
        self.window.inventory = [InventoryItem(f'seed-{index}', value, 1, f'苗 {index}') for index, value in enumerate(genes)]
        self.window.refresh_inventory()
        report = self.calculate()
        self.guided.accept_route(report)
        self.window.autosave_timer.stop()
        return report

    def calculate(self):
        return plan_route(self.window.inventory, self.window.target(), max_rounds=1, max_seconds=3)

    def try_commit_target(self):
        before = tuple(self.window.inventory)
        self.guided.actual_edit.setText('YYYGGG')
        self.guided.actual_quantity.setValue(99)
        self.guided.completed_check.setCurrentIndex(1)
        self.guided.complete_current_round()
        self.assertEqual(tuple(self.window.inventory), before)
        self.assertEqual(self.guided.state.rounds, ())

    def test_single_incomplete_seed_cannot_become_a_planting_plan(self):
        report = self.arrange()
        self.assertFalse(report.found)
        self.assertIsNone(self.guided.state.pending)
        self.assertIsNone(self.guided.current_recipe())
        self.assertFalse(self.guided.actual_edit.isEnabled())
        self.assertFalse(self.guided.commit_button.isEnabled())
        self.assertTrue(all(genes.text() == '留空' for _, genes, _ in self.guided.cells))
        self.assertIn('尚无完整路线', self.guided.prediction_label.text())
        self.try_commit_target()

    def test_seven_seed_inventory_candidate_is_not_misrepresented_as_a_full_route(self):
        report = self.arrange(('XYHYYW', 'WGYXHH', 'XYGGHX', 'YYGWYY', 'XWGWGH', 'HHWWGW', 'YYWGHX'))
        self.assertTrue(report.complete)
        self.assertFalse(report.found)
        self.assertIsNotNone(report.best)
        self.assertIsNone(self.guided.current_recipe())
        self.assertIsNone(self.guided.state.pending)
        self.assertFalse(self.guided.commit_button.isEnabled())
        self.assertIn('未找到完整', self.guided.summary.text())
        self.try_commit_target()

    def test_observation_alone_cannot_confirm_a_nonexistent_crossbreed(self):
        self.arrange()
        self.try_commit_target()
        self.assertIn('符合当前目标', self.guided.actual_assessment.text())
        self.assertFalse(self.guided.commit_button.isEnabled())
        self.assertIsNone(self.guided.state.pending)

    def test_existing_target_clone_needs_no_donors_or_inventory_mutation(self):
        report = self.arrange(('YYYGGG',))
        self.assertTrue(report.found)
        self.assertEqual(report.best.donor_count, 0)
        self.assertEqual(self.guided.current_recipe().center.genes, 'YYYGGG')
        self.assertIn('直接保留', self.guided.instruction_title.text())
        self.assertIn('无需', self.guided.instruction_body.text())
        self.assertFalse(self.guided.actual_edit.isEnabled())
        self.assertFalse(self.guided.commit_button.isEnabled())
        self.try_commit_target()

    def test_changing_target_to_retained_clone_finds_existing_goal_without_breeding(self):
        self.arrange()
        self.window.target_edit.setText('YYGWYY')
        self.guided.accept_route(self.calculate())
        self.assertEqual(self.guided.current_recipe().donor_count, 0)
        self.assertTrue(self.guided.current_recipe().guaranteed)
        self.assertIn('直接保留', self.guided.instruction_title.text())
        self.assertFalse(self.guided.commit_button.isEnabled())

    def test_withdrawing_existing_goal_then_changing_target_clears_old_prediction(self):
        self.arrange(('YYYGGG',))
        self.guided.reset_pending()
        self.window.target_edit.setText('YYYYGG')
        self.guided.accept_route(self.calculate())
        self.assertIsNone(self.guided.current_recipe())
        self.assertIsNone(self.guided.state.pending)
        self.assertFalse(self.guided.actual_edit.isEnabled())
        self.assertFalse(self.guided.commit_button.isEnabled())
        self.assertIn('等待规划', self.guided.round_prediction.text())

    def test_adding_real_donors_unlocks_only_after_a_verified_full_route(self):
        self.arrange(('GYYGGG',))
        self.assertIsNone(self.guided.state.pending)
        self.window.inventory.extend((InventoryItem('a', 'YHYGGG'), InventoryItem('b', 'YGGGGG')))
        self.window.refresh_inventory()
        self.assertIsNone(self.guided.state.pending)
        report = self.calculate()
        self.assertTrue(report.found)
        self.guided.accept_route(report)
        self.assertIsNotNone(self.guided.state.pending)
        self.assertTrue(self.guided.actual_edit.isEnabled())
        self.guided.actual_edit.setText('YYYGGG')
        self.guided.completed_check.setCurrentIndex(1)
        self.assertTrue(self.guided.commit_button.isEnabled())

    def test_old_reading_or_cleanup_metadata_cannot_manufacture_a_pending_round(self):
        self.arrange()
        original = self.window.snapshot()
        for old_step, observed in ((2, ''), (2, 'YYGWYY'), (3, 'YYYGGG'), (4, 'YYGWYY')):
            with self.subTest(old_step=old_step, observed=observed):
                data = dict(original, step=old_step, observed_genes=observed, observed_input=observed)
                data['slots'] = [None] * 9
                data['slots'][4] = {'seed_id': 'seed-0', 'remove': True}
                data['guided'] = {'stage': old_step, 'actual_input': observed, 'completed': True}
                restored = self.new_window()
                restored.apply_project(data)
                before = tuple(restored.inventory)
                self.assertIsNone(restored.guided.state.pending)
                self.assertFalse(restored.guided.commit_button.isEnabled())
                self.assertFalse(restored.guided.actual_edit.isEnabled())
                self.assertEqual(restored.guided.state.rounds, ())
                restored.guided.complete_current_round()
                self.assertEqual(tuple(restored.inventory), before)

    def test_unverified_report_cannot_hide_a_candidate_inside_planting_layout(self):
        report = self.arrange(('GYYGGG', 'YHYGGG', 'YGGGGG'))
        self.assertTrue(report.found)
        self.guided.reset_pending()
        incomplete = replace(report, found=False, complete=False, reason='budget')
        self.guided.accept_route(incomplete)
        self.assertIsNone(self.guided.current_recipe())
        self.assertTrue(all(genes.text() == '留空' for _, genes, _ in self.guided.cells))
        self.assertFalse(self.guided.actual_edit.isEnabled())
        self.assertFalse(self.guided.commit_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
