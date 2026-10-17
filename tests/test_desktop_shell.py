"""Shared-stock editing, local project recovery and two-mode safety."""
from dataclasses import asdict
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.deps'))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QListWidget
from rust_breeder import app as application
from rust_breeder.engine import predict
from rust_breeder.journey import JourneyState, prepare_round
from rust_breeder.models import InventoryItem, Recipe
from rust_breeder.project_library import ProjectLibrary
from rust_breeder.storage import ProjectError, load_project, save_project
from rust_breeder.widgets import SeedDialog


class DesktopShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='desktop-shell-', dir=ROOT / 'artifacts')
        self.base = Path(self.temp.name)
        self.autosave = self.base / 'data/autosave.json'
        self.patches = [patch.object(application, 'AUTOSAVE', self.autosave),
                        patch.object(application, 'BASE_DIR', self.base)]
        for item in self.patches:
            item.start()
        self.windows = []
        self.window = self.new_window()
        self.window.inventory = [InventoryItem('center', 'GYYGGG', 1, '我的原苗'),
                                 InventoryItem('a', 'YHYGGG', 1), InventoryItem('b', 'YGGGGG', 1)]
        self.window.refresh_inventory()

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
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def new_window(self, restore=False):
        window = application.MainWindow(restore=restore)
        self.windows.append(window)
        return window

    def pending(self):
        inventory = tuple(self.window.inventory)
        target = self.window.target()
        recipe = Recipe(inventory[0], inventory[1:], predict(inventory[0].genes, [x.genes for x in inventory[1:]]), target)
        page = self.window.guided
        page.state = prepare_round(JourneyState(target=target, center_id='center'), inventory, recipe)
        page.stage = 2
        page.render_round()
        self.window.update_locks()

    def test_shell_has_two_modes_new_brand_and_smaller_default(self):
        self.assertEqual([self.window.workspaces.tabText(i) for i in range(2)], ['顺序培育', '多线培育'])
        self.assertIn('Rust 种养殖小工具 V', self.window.windowTitle())
        self.assertNotIn('苗圃', self.window.windowTitle())
        self.assertIn('作者 Tao', self.window.author_label.text())
        self.assertIn('2027883286@qq.com', self.window.author_label.text())
        self.assertEqual((self.window.width(), self.window.height()), (1280, 800))
        self.assertFalse(hasattr(self.window, '_legacy_workspace'))

    def test_add_edit_delete_actions_reach_real_dialog_and_keep_unique_numbers(self):
        original_number = self.window.seed_number('center')
        def add(dialog):
            dialog.result_genes = 'HHHGGG'
            dialog.name_edit.setText('新增中文苗')
            dialog.quantity_edit.setValue(5)
            return QDialog.DialogCode.Accepted
        with patch.object(SeedDialog, 'exec', add):
            self.window.add_seed()
        added = self.window.inventory[-1]
        self.assertEqual((added.genes, added.quantity, added.name), ('HHHGGG', 5, '新增中文苗'))
        number = self.window.seed_number(added)
        def edit(dialog):
            dialog.result_genes = 'HHHGGG'
            dialog.quantity_edit.setValue(7)
            return QDialog.DialogCode.Accepted
        with patch.object(SeedDialog, 'exec', edit):
            self.window.edit_seed(added.id)
        self.assertEqual(self.window.by_id(added.id).quantity, 7)
        self.window.delete_seed('center')
        self.assertEqual(self.window.seed_number(added), number)
        self.assertEqual(self.window.seed_numbers['center'], original_number)
        self.window.inventory.append(InventoryItem('next', 'YYYYGG'))
        self.window.refresh_inventory()
        self.assertGreater(self.window.seed_number('next'), number)

    def test_pending_locks_other_mode_and_all_inventory_mutation_handlers(self):
        self.pending()
        before = self.window.snapshot()
        self.assertFalse(self.window.workspaces.isTabEnabled(1))
        self.assertFalse(self.window.can_plan(self.window.parallel))
        with patch.object(SeedDialog, 'exec') as dialog:
            self.window.add_seed()
            self.window.edit_seed('center')
            self.window.delete_seed('center')
            self.window.capture_genes()
            dialog.assert_not_called()
        self.assertEqual(self.window.snapshot(), before)

    def test_stable_seed_numbers_and_draft_restore_from_internal_autosave(self):
        self.pending()
        page = self.window.guided
        page.actual_edit.setText('YYYGGG')
        page.actual_quantity.setValue(4)
        expected = self.window.snapshot()
        self.assertTrue(self.window.autosave())
        restored = self.new_window(restore=True)
        self.assertEqual(tuple(restored.inventory), tuple(self.window.inventory))
        self.assertEqual(restored.seed_numbers, expected['seed_numbers'])
        self.assertEqual(restored.guided.actual_edit.text(), 'YYYGGG')
        self.assertEqual(restored.guided.actual_quantity.value(), 4)
        self.assertEqual(restored.guided.state.pending, page.state.pending)
        self.assertFalse(restored.workspaces.isTabEnabled(1))

    def test_new_and_open_are_software_local_without_external_save_dialogs(self):
        self.assertTrue(self.window.autosave())
        first_id = self.window.current_project_id
        self.window.library.entries[0]['name'] = '保留的原项目'
        self.window.library._write_index()
        with patch.object(QFileDialog, 'getSaveFileName', side_effect=AssertionError('external save not expected')):
            with patch.object(application.QInputDialog, 'getText', return_value=('第二个项目', True)):
                self.window.new_local_project()
            self.assertEqual(self.window.inventory, [])
            self.assertNotEqual(self.window.current_project_id, first_id)
            def choose(dialog):
                listing = dialog.findChild(QListWidget)
                for index in range(listing.count()):
                    if listing.item(index).text().startswith('保留的原项目'):
                        listing.setCurrentRow(index)
                return QDialog.DialogCode.Accepted
            with patch.object(QDialog, 'exec', choose):
                self.window.open_local_project()
        self.assertEqual(self.window.current_project_id, first_id)
        self.assertEqual(self.window.inventory[0].name, '我的原苗')
        self.assertEqual(len(ProjectLibrary(self.autosave.parent).entries), 2)

    def test_cancel_new_project_never_changes_stock_or_current_project(self):
        before = self.window.snapshot()
        with patch.object(application.QInputDialog, 'getText', return_value=('', False)):
            self.window.new_local_project()
        self.assertEqual(self.window.snapshot(), before)

    def test_legacy_projects_migrate_into_internal_list_without_modifying_original(self):
        self.autosave.parent.mkdir(parents=True)
        data = self.window.snapshot()
        save_project(self.autosave, data)
        legacy = self.base / '以前保存的项目.json'
        save_project(legacy, data)
        original = legacy.read_bytes()
        restored = self.new_window(restore=True)
        self.assertEqual(legacy.read_bytes(), original)
        self.assertEqual(tuple(restored.inventory), tuple(self.window.inventory))
        self.assertIn('以前保存的项目', [x['name'] for x in restored.library.entries])
        again = self.new_window(restore=True)
        self.assertEqual(len(again.library.entries), len(restored.library.entries))

    def test_malformed_numbers_or_two_pending_modes_are_rejected_before_stock_changes(self):
        before = tuple(self.window.inventory)
        data = self.window.snapshot()
        data['seed_numbers'] = {'center': True}
        with self.assertRaises(ProjectError):
            self.window.apply_project(data)
        self.assertEqual(tuple(self.window.inventory), before)

    def test_crop_is_single_project_and_programmatic_change_cannot_bypass_stock_lock(self):
        self.window.crop_combo.setCurrentIndex(self.window.crop_combo.findData('blue'))
        self.assertEqual(self.window.project_crop, 'hemp')
        self.assertEqual(self.window.crop_combo.currentData(), 'hemp')

    def test_both_pages_show_same_number_and_current_stock(self):
        self.assertEqual(self.window.seed_number('center'), 1)
        self.assertEqual(self.window.guided.inventory_table.rowCount(), 3)
        self.assertEqual(self.window.parallel.inventory_table.rowCount(), 3)
        self.window.inventory.reverse()
        self.window.refresh_inventory()
        self.assertEqual(self.window.seed_number('center'), 1)
