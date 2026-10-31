"""Choice cards must never turn bounded failure into an actionable route."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / '.deps').exists():
    sys.path.insert(0, str(ROOT / '.deps'))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

try:
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtWidgets import QApplication, QDialog, QLabel
except ImportError:
    QT_AVAILABLE = False
else:
    QT_AVAILABLE = True
    from rust_breeder.engine import predict
    from rust_breeder.i18n import clear_translation_cache, localize_tree, set_language
    from rust_breeder.models import InventoryItem, Recipe, Target
    from rust_breeder.parallel_models import ParallelReport, ParallelStep, ParallelWave
    from rust_breeder.routes import RouteReport, RouteStep
    from rust_breeder.target_discovery import TargetOption, green_targets
    from rust_breeder.target_selector import TargetSelectorDialog
    from rust_breeder.translation_catalog import TRANSLATIONS, TEMPLATES


@unittest.skipUnless(QT_AVAILABLE, 'Install PySide6 for target-card UI checks.')
class TargetSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        for name in ('msyh.ttc', 'msyhbd.ttc', 'segoeui.ttf'):
            path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / name
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))

    def setUp(self):
        self.dialogs = []
        set_language('zh_CN')

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.set_searching(False)
            dialog.close()
            dialog.deleteLater()
        self.qt.processEvents()
        set_language('zh_CN')

    def dialog(self, mode='sequential'):
        dialog = TargetSelectorDialog(mode=mode)
        self.dialogs.append(dialog)
        return dialog

    def option(self, genes, *, status='reachable', donors=2, parallel=False):
        target = Target(genes, 'counts')
        if status not in {'reachable', 'already_owned'}:
            return TargetOption(target, status)
        if status == 'already_owned':
            report = (ParallelReport((), True, True, 0, 'already_target') if parallel else
                      RouteReport((), (), True, True, 0, 'already_target'))
        else:
            center = InventoryItem('center-' + genes, 'HHHHHH', 1)
            donor = InventoryItem('donor-' + genes, genes, donors)
            recipe = Recipe(center, (donor,) * donors,
                            predict(center.genes, [genes] * donors), target)
            self.assertEqual(recipe.prediction.deterministic_result, genes)
            if parallel:
                step = ParallelStep(1, recipe, genes, 'output-' + genes)
                report = ParallelReport((ParallelWave(1, (step,)),), True, False, 1, 'found')
            else:
                step = RouteStep(recipe, genes, 'output-' + genes, 1)
                report = RouteReport((step,), (recipe,), True, False, 1, 'found')
        return TargetOption(target, status, report)

    def test_all_seven_targets_are_vertical_and_only_reachable_have_actions(self):
        dialog = self.dialog()
        options = [self.option(target.genes, status='unknown') for target in green_targets()]
        options[0] = self.option('YYYGGG')
        options[1] = self.option('YYYYGG', status='already_owned')
        options[2] = self.option('YYGGGG', status='unreachable')
        dialog.set_options(options)
        dialog.show()
        self.qt.processEvents()
        self.assertEqual(len(dialog.cards), 7)
        self.assertEqual([card.option.status for card in dialog.cards[:2]], ['already_owned', 'reachable'])
        self.assertEqual({card.title.text() for card in dialog.cards},
                         {f'{count}Y + {6 - count}G' for count in range(7)})
        self.assertEqual(len([card for card in dialog.cards if card.action_button is not None]), 2)
        for before, after in zip(dialog.cards, dialog.cards[1:]):
            self.assertLess(before.geometry().bottom(), after.geometry().top())
        unknown = next(card for card in dialog.cards if card.option.status == 'unknown')
        self.assertIn('暂未找到', unknown.status_label.text())
        self.assertNotIn('不可达', unknown.status_label.text())
        self.assertIsNone(unknown.action_button)
        self.assertEqual(dialog.scroll.horizontalScrollBar().maximum(), 0)

    def test_selection_emits_the_original_report_without_closing_or_spending_stock(self):
        option = self.option('YYYGGG')
        ingredients_before = (option.report.steps[0].recipe.center, option.report.steps[0].recipe.donors)
        dialog = self.dialog()
        dialog.set_options((option,))
        selected = []
        dialog.option_selected.connect(selected.append)
        dialog.show()
        self.qt.processEvents()
        dialog.cards[0].action_button.click()
        self.assertEqual(selected, [option])
        self.assertIs(selected[0], option)
        self.assertIs(selected[0].report, option.report)
        self.assertTrue(dialog.isVisible())
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual((option.report.steps[0].recipe.center, option.report.steps[0].recipe.donors), ingredients_before)

    def test_searching_disables_choices_and_clears_old_results_on_rerun(self):
        dialog = self.dialog()
        option = self.option('YYYGGG')
        dialog.set_options((option,))
        stale = dialog.cards[0].action_button
        selected = []
        dialog.option_selected.connect(selected.append)
        dialog.set_searching(True)
        stale.click()
        self.assertEqual(dialog.options, ())
        self.assertEqual(dialog.cards, [])
        self.assertEqual(selected, [])
        dialog.set_options((option,))
        self.assertFalse(dialog.cards[0].action_button.isEnabled())
        dialog.cards[0].action_button.click()
        self.assertEqual(selected, [])
        dialog.set_searching(False)
        dialog.cards[0].action_button.click()
        self.assertEqual(selected, [option])

    def test_empty_library_and_error_never_leave_old_action_buttons(self):
        dialog = self.dialog()
        dialog.set_options((self.option('YYYGGG'),))
        dialog.set_empty_inventory()
        self.assertEqual(dialog.cards, [])
        self.assertIn('基因库为空', dialog.status.text())
        self.assertFalse(dialog.searching)
        dialog.set_options((self.option('YYYGGG'),))
        dialog.show_error('测试失败，库存未改变')
        self.assertEqual(dialog.cards, [])
        self.assertEqual(dialog.status.text(), '测试失败，库存未改变')

    def test_fake_success_without_a_found_report_has_no_action(self):
        option = SimpleNamespace(target=Target('YYYGGG'), status='reachable', selectable=True,
                                 report=SimpleNamespace(found=False), rounds=2, plant_count=7)
        dialog = self.dialog()
        dialog.set_options((option,))
        self.assertIsNone(dialog.cards[0].action_button)
        self.assertIn('暂未找到', dialog.cards[0].status_label.text())

    def test_cancel_and_close_request_interruption_once_without_thread_ownership(self):
        dialog = self.dialog()
        requests = []
        dialog.cancel_requested.connect(lambda: requests.append(True))
        dialog.set_searching(True)
        dialog.update_progress(3, 7)
        self.assertEqual(dialog.progress.value(), 3)
        dialog.stop_button.click()
        self.assertFalse(dialog.stop_button.isEnabled())
        dialog.update_progress(4, 7)
        self.assertIn('正在停止', dialog.status.text())
        dialog.reject()
        self.assertEqual(requests, [True])
        second = self.dialog()
        second.cancel_requested.connect(lambda: requests.append(True))
        second.set_searching(True)
        second.show()
        self.qt.processEvents()
        second.close()
        self.assertEqual(requests, [True, True])

    def test_parallel_cards_use_batches_and_existing_stock_needs_no_breeding(self):
        dialog = self.dialog('parallel')
        dialog.set_options((self.option('YYYGGG', parallel=True),
                            self.option('YYYYGG', status='already_owned', parallel=True)))
        self.assertIn('无需杂交', dialog.cards[0].status_label.text())
        self.assertIn('预计 1 批', dialog.cards[1].detail_label.text())
        self.assertIn('累计用苗 3 株', dialog.cards[1].detail_label.text())

    def test_only_valid_count_targets_are_accepted(self):
        dialog = self.dialog()
        for target in (Target('YYYGGG', 'exact'), Target('YYHGGG')):
            option = SimpleNamespace(target=target, status='unknown')
            with self.subTest(target=target), self.assertRaises(ValueError):
                dialog.set_options((option,))
        with self.assertRaises(ValueError):
            dialog.set_options((self.option('YYYGGG'), self.option('GYGYGY')))

    def test_long_russian_text_wraps_without_overlapping_actions_or_changing_targets(self):
        # Test layout independently of when the shared catalog receives this
        # feature's strings; production uses exactly that same catalog path.
        strings = {
            '当前可以培育哪些优选组合': 'Выберите сочетание для дальнейшего выращивания растений',
            '选用并自动选苗': 'Выбрать и расставить',
            '可达成 · 已找到完整路线': 'Достижимо · найден полный путь выращивания',
            '暂未找到完整路线': 'Полный путь для этого сочетания пока не найден',
        }
        template = {'预计 {rounds} 轮 · 累计用苗 {plants} 株':
                    'Примерно {rounds} раундов · расход {plants} растений'}
        with patch.dict(TRANSLATIONS['ru'], strings), patch.dict(TEMPLATES['ru'], template):
            clear_translation_cache()
            dialog = self.dialog()
            options = (self.option('YYYGGG'), self.option('YYYYGG', status='unknown'))
            dialog.set_options(options)
            dialog.resize(560, 420)
            dialog.show()
            set_language('ru')
            localize_tree(dialog)
            self.qt.processEvents()
            self.assertEqual(dialog.options, options)
            self.assertEqual(dialog.cards[0].title.text(), '3Y + 3G')
            self.assertIn('Примерно', dialog.cards[0].detail_label.text())
            self.assertEqual(dialog.scroll.horizontalScrollBar().maximum(), 0)
            card = dialog.cards[0]
            text_left = card.detail_label.mapTo(card, card.detail_label.rect().topLeft()).x()
            text_right = text_left + card.detail_label.width()
            self.assertLess(text_right, card.action_button.geometry().left())
            self.assertLessEqual(card.action_button.geometry().right(), card.width())
        clear_translation_cache()


if __name__ == '__main__':
    unittest.main()
