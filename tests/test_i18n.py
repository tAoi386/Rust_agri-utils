"""Live language changes must preserve breeding state and editable data."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.deps'))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6.QtCore import QBuffer, QCoreApplication, QIODevice, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel,
                                QTableWidgetItem, QVBoxLayout)

from rust_breeder import app as application
from rust_breeder.engine import predict
from rust_breeder.i18n import (LocalizedComboBox, LocalizedLabel, LocalizedLineEdit,
                               LocalizedTableWidget, install_localization,
                               localize_tree, set_language, tr)
from rust_breeder.journey import JourneyState, prepare_round
from rust_breeder.models import InventoryItem, Recipe, Target
from rust_breeder.parallel_models import ParallelReport, ParallelState, prepare_wave
from rust_breeder.parallel import plan_parallel
from rust_breeder.widgets import button, label


class LocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        install_localization(cls.qt)

    def setUp(self):
        set_language('zh_CN')
        self.temp = tempfile.TemporaryDirectory(prefix='i18n-', dir=ROOT / 'artifacts')
        self.path_patch = patch.object(application, 'AUTOSAVE', Path(self.temp.name) / 'autosave.json')
        self.path_patch.start()
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            if hasattr(window, 'autosave_timer'):
                window.autosave_timer.stop()
                window.dirty = False
            window.close()
            window.deleteLater()
        self.qt.processEvents()
        set_language('zh_CN')
        self.path_patch.stop()
        self.temp.cleanup()

    def window(self):
        window = application.MainWindow(restore=False)
        self.windows.append(window)
        window.autosave_timer.stop()
        return window

    def test_three_languages_translate_static_and_dynamic_preserving_codes(self):
        for code in ('en', 'ru'):
            set_language(code)
            self.assertNotEqual(tr('顺序培育'), '顺序培育')
            actual = tr('第 12 批 · 2 箱同时开工')
            self.assertNotIn('批', actual)
            self.assertIn('12', actual)
            self.assertIn('2', actual)
            actual = tr('本轮预测：G Y G Y G Y')
            self.assertNotIn('预测', actual)
            self.assertIn('G Y G Y G Y', actual)
        set_language('zh_CN')
        self.assertEqual(tr('顺序培育'), '顺序培育')

    def test_unknown_qt_translations_keep_text_and_automatic_image_decoding(self):
        # A null QString means "not translated". An empty QString suppresses
        # Qt internals too, including its automatic image-format detection.
        image = QImage(12, 8, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.green)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer, 'PNG'))
        encoded = bytes(buffer.data())
        for code in ('zh_CN', 'en', 'ru'):
            set_language(code)
            self.assertEqual(QCoreApplication.translate('QImageReader', 'original-untranslated-value'),
                             'original-untranslated-value')
            decoded = QImage.fromData(encoded)
            self.assertFalse(decoded.isNull(), code)
            self.assertEqual(decoded.size(), image.size())

    def test_live_controls_drafts_and_combo_values_are_unchanged(self):
        combo = LocalizedComboBox()
        edit = LocalizedLineEdit('自定义苗名 留空 GYYGGG')
        self.windows.extend((combo, edit))
        combo.addItem('按数量 · 顺序不限', 'counts')
        combo.addItem('逐位 · 固定顺序', 'exact')
        combo.setCurrentIndex(1)
        edit.setPlaceholderText('完整六位基因')
        edit.setSelection(2, 4)
        changes = []
        combo.currentIndexChanged.connect(lambda *_: changes.append('combo'))
        edit.textChanged.connect(lambda *_: changes.append('draft'))
        text, selection = edit.text(), edit.selectedText()
        for code in ('en', 'ru', 'zh_CN'):
            set_language(code)
            self.assertEqual(combo.currentData(), 'exact')
            self.assertEqual(edit.text(), text)
            self.assertEqual(edit.selectedText(), selection)
        self.assertEqual(changes, [])

    def test_cleared_labels_and_ocr_images_do_not_reappear_or_disappear(self):
        text = label('等待规划')
        self.windows.append(text)
        text.clear()
        image = QPixmap(30, 20)
        image.fill(Qt.GlobalColor.green)
        preview = label('等待规划')
        self.windows.append(preview)
        preview.setPixmap(image)
        cache_key = preview.pixmap().cacheKey()
        for code in ('en', 'ru', 'zh_CN'):
            set_language(code)
            self.assertEqual(text.text(), '')
            self.assertEqual(preview.pixmap().cacheKey(), cache_key)

    def test_user_name_labels_and_inventory_cells_are_never_translated(self):
        name = label('留空')
        name.setProperty('i18nUserText', True)
        table = LocalizedTableWidget(1, 3)
        table.setHorizontalHeaderLabels(['基因', '株数', '名称'])
        table.setItem(0, 0, QTableWidgetItem('GYYGGG'))
        table.setItem(0, 1, QTableWidgetItem('2'))
        table.setItem(0, 2, QTableWidgetItem('留空'))
        self.windows.extend((name, table))
        set_language('ru')
        localize_tree(table)
        self.assertEqual(name.text(), '留空')
        self.assertEqual([table.item(0, x).text() for x in range(3)], ['GYYGGG', '2', '留空'])
        self.assertNotEqual(table.horizontalHeaderItem(2).text(), '名称')

    def test_dialog_standard_buttons_and_title_translate_on_show_and_switch(self):
        dialog = QDialog()
        self.windows.append(dialog)
        dialog.setWindowTitle('编辑克隆苗')
        layout = QVBoxLayout(dialog)
        title = QLabel('六位基因')
        layout.addWidget(title)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(actions)
        set_language('en')
        dialog.show()
        self.qt.processEvents()
        self.assertEqual(title.text(), tr('六位基因'))
        self.assertEqual(actions.button(QDialogButtonBox.StandardButton.Cancel).text(), 'Cancel')
        set_language('ru')
        self.assertEqual(actions.button(QDialogButtonBox.StandardButton.Cancel).text(), 'Отмена')
        set_language('zh_CN')
        self.assertEqual(dialog.windowTitle(), '编辑克隆苗')

    def test_pending_round_actual_draft_and_inventory_survive_language_switch(self):
        window = self.window()
        target = Target('GGGGGG', 'exact')
        window.target_edit.setText(target.genes)
        window.mode_combo.setCurrentIndex(window.mode_combo.findData('exact'))
        rows = [InventoryItem('c', 'GGGGGH', 1, '留空'), InventoryItem('a', 'GGHGGG'), InventoryItem('b', 'GGYGGG')]
        window.inventory = rows
        window.refresh_inventory()
        recipe = Recipe(rows[0], tuple(rows[1:]), predict(rows[0].genes, [x.genes for x in rows[1:]]), target)
        page = window.guided
        page.state = prepare_round(JourneyState(target, 'c'), rows, recipe)
        page.render_round()
        page.actual_edit.setText('ggg')
        page.actual_quantity.setValue(3)
        page.filter_edit.setText('留空')
        token, state = page.state.pending.token, page.state.to_dict()
        for code in ('en', 'ru', 'zh_CN'):
            set_language(code)
            localize_tree(window)
            self.qt.processEvents()
            self.assertEqual(page.actual_edit.text(), 'ggg')
            self.assertEqual(page.actual_quantity.value(), 3)
            self.assertEqual(page.filter_edit.text(), '留空')
            self.assertEqual(page.state.pending.token, token)
            self.assertEqual(page.state.to_dict(), state)
            self.assertEqual(window.inventory, rows)

    def test_crop_sources_survive_a_window_created_in_another_language(self):
        set_language('en')
        window = self.window()
        source = window.crop_combo.itemSourceText(window.crop_combo.findData('blue'))
        self.assertEqual(source, '蓝浆果')
        for code in ('ru', 'zh_CN', 'en'):
            set_language(code)
            localize_tree(window)
            index = window.guided.crop.findData('blue')
            self.assertEqual(window.guided.crop.itemText(index), tr('蓝浆果'))
            self.assertEqual(window.guided.crop.itemSourceText(index), '蓝浆果')
            self.assertEqual(window.guided.crop.currentData(), 'hemp')

    def test_cancelled_parallel_search_is_stopped_not_no_solution(self):
        window = self.window()
        page = window.parallel
        page.report = ParallelReport((), False, False, 1200, 'cancelled')
        page.render_wave()
        for code in ('zh_CN', 'en', 'ru'):
            set_language(code)
            self.assertEqual(page.summary.text(), tr('已停止计算'))
            self.assertNotEqual(page.summary.text(), tr('尚未找到完整多箱路线'))
            self.assertEqual(page.state.pending, None)

    def test_headers_created_in_english_can_switch_to_russian_and_chinese(self):
        set_language('en')
        table = LocalizedTableWidget(0, 2)
        self.windows.append(table)
        table.setHorizontalHeaderLabels(['基因', '名称'])
        localize_tree(table)
        for code in ('ru', 'zh_CN', 'en'):
            set_language(code)
            localize_tree(table)
            self.assertEqual(table.horizontalHeaderItem(0).text(), tr('基因'))

    def test_parallel_pending_box_drafts_and_multiline_dependencies_survive(self):
        window = self.window()
        rows = [InventoryItem(str(i), genes, name='留空') for i, genes in enumerate(
            ('HGYGHG', 'HGGGYY', 'HYYGYG', 'YHYYHH', 'GYHYYY', 'YYHYYY', 'YHYYHG'))]
        window.inventory = rows
        window.refresh_inventory()
        report = plan_parallel(rows, window.target(), max_donors=2, max_waves=2, max_seconds=3)
        page = window.parallel
        page.report = report
        page.state = prepare_wave(ParallelState(window.target()), rows, report.waves[0])
        page.render_wave()
        boxes = tuple(x.box_id for x in report.waves[0].steps)
        for box_id in boxes:
            page.box_pages[box_id].actual_edit.setText('gGy')
            page.box_pages[box_id].actual_quantity.setValue(box_id + 2)
        before = page.state.to_dict()
        for code in ('en', 'ru', 'zh_CN'):
            set_language(code)
            localize_tree(window)
            self.assertEqual(page.state.to_dict(), before)
            self.assertEqual(window.inventory, rows)
            for box_id in boxes:
                self.assertEqual(page.box_pages[box_id].actual_edit.text(), 'gGy')
                self.assertEqual(page.box_pages[box_id].actual_quantity.value(), box_id + 2)
            if code != 'zh_CN':
                self.assertNotIn('号箱', page.route_label.text())


if __name__ == '__main__':
    unittest.main()
