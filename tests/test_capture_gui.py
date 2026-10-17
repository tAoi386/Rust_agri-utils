"""Exercise actual Qt capture review, asynchronous OCR, and project import."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if (PROJECT_ROOT / ".deps").exists():
    sys.path.insert(0, str(PROJECT_ROOT / ".deps"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    from PySide6.QtCore import QObject, QPoint, QRect, Qt, QThread, QTimer, Signal
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QWidget
    from shiboken6 import isValid
    from rust_breeder import app as application
    from rust_breeder import capture_dialog as capture
    from rust_breeder.engine import predict
    from rust_breeder.journey import JourneyState, prepare_round
    from rust_breeder.models import InventoryItem, Recipe
    from rust_breeder.storage import load_project
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, "Install PySide6 to run Qt integration checks.")
class CaptureGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialogs = []
        self.releases = []
        self.windows = []
        self.temporary = tempfile.TemporaryDirectory(prefix=".capture-gui-", dir=PROJECT_ROOT)
        self.autosave = Path(self.temporary.name) / "autosave.json"
        self.autosave_patch = patch.object(application, "AUTOSAVE", self.autosave)
        self.autosave_patch.start()

    def tearDown(self):
        for release in self.releases:
            release.set()
        for dialog in reversed(self.dialogs):
            if not isValid(dialog):
                continue
            dialog.stop_capture()
            self.wait_until(lambda: dialog.worker is None)
            dialog.reject()
            dialog.deleteLater()
        for window in reversed(self.windows):
            if isinstance(window, application.MainWindow):
                window.autosave_timer.stop()
                window.dirty = False
            window.close()
            window.deleteLater()
        self.qt.processEvents()
        self.autosave_patch.stop()
        self.temporary.cleanup()

    def wait_until(self, predicate, seconds=3):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.qt.processEvents()
        self.assertTrue(predicate(), "Qt operation did not finish before timeout")

    def new_dialog(self, inventory=(), parent=None, *, advanced=False):
        dialog = capture.ScreenCaptureDialog(tuple(inventory), "大麻 / Hemp", parent)
        self.dialogs.append(dialog)
        if advanced:
            dialog.source_mode.setCurrentIndex(dialog.source_mode.findData("region"))
        return dialog

    def image(self):
        image = QImage(160, 40, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        return image

    def visible_capture(self):
        owner = QWidget()
        owner.resize(900, 700)
        owner.show()
        self.windows.append(owner)
        dialog = self.new_dialog(parent=owner)
        dialog.capture_screen = QApplication.primaryScreen()
        dialog.region = QRect(1, 1, 100, 30)
        dialog.start_button.setEnabled(True)
        dialog.show()
        self.qt.processEvents()
        return owner, dialog

    def delayed_recognizer(self, result=("YYYGGG",), error=None):
        started = threading.Event()
        release = threading.Event()
        self.releases.append(release)

        def recognize(png, **kwargs):
            self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
            started.set()
            if not release.wait(3):
                raise RuntimeError("Test did not release OCR worker")
            if error is not None:
                raise error
            return result

        return recognize, started, release

    def test_close_during_selection_delay_cancels_selector_and_restores_owner(self):
        owner, dialog = self.visible_capture()
        with patch.object(capture, "RegionSelector") as selector:
            dialog.select_region()
            self.assertTrue(dialog.selecting)
            self.assertFalse(owner.isVisible())
            dialog.reject()
            self.assertTrue(owner.isVisible())
            self.assertFalse(dialog.isVisible())
            self.assertFalse(dialog.selecting)
            QTest.qWait(400)
            selector.assert_not_called()
            self.assertFalse(dialog.isVisible())

    def test_delete_during_selection_delay_does_not_leave_a_free_callback(self):
        owner, dialog = self.visible_capture()
        with patch.object(capture, "RegionSelector") as selector:
            dialog.select_region()
            dialog.reject()
            dialog.deleteLater()
            QTest.qWait(400)
            self.assertFalse(isValid(dialog))
            selector.assert_not_called()
            self.assertTrue(owner.isVisible())

    def test_close_inside_active_selector_does_not_reopen_capture_dialog(self):
        owner, dialog = self.visible_capture()
        selectors = []

        class ClosingSelector(QDialog):
            def __init__(self, screen):
                super().__init__()
                selectors.append(self)

            def exec(self):
                QTimer.singleShot(0, dialog.reject)
                return super().exec()

        with patch.object(capture, "RegionSelector", ClosingSelector):
            dialog.select_region()
            dialog.selection_timer.stop()
            dialog.finish_selection()
        self.assertEqual(len(selectors), 1)
        self.assertFalse(dialog.isVisible())
        self.assertTrue(owner.isVisible())
        self.assertFalse(dialog.selecting)
        self.assertIsNone(dialog.region_selector)
        self.assertIsNone(dialog.selection_context)

    def test_review_edits_selection_duplicates_and_quantity_are_applied_once(self):
        original = InventoryItem("original", "YYYGGG", 7, "已种的苗")
        dialog = self.new_dialog([original])
        dialog.receive_result(("YYYGGG", "GGGYYY", "GYGYGY", "WWWWWW"))
        self.assertEqual(dialog.table.item(0, 0).checkState(), Qt.CheckState.Unchecked)
        dialog.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        dialog.table.item(1, 1).setText("g,y,y,g,g,y")
        dialog.table.item(2, 1).setText("GYYGGY")
        self.assertIn("重复", dialog.table.item(2, 3).text())
        # An unchecked earlier occurrence cannot hide a selected later one.
        dialog.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
        dialog.table.item(3, 1).setText("BAD")
        self.assertFalse(dialog.import_button.isEnabled())
        dialog.table.item(3, 0).setCheckState(Qt.CheckState.Unchecked)
        self.assertTrue(dialog.import_button.isEnabled())
        dialog.quantity.setValue(6)
        dialog.import_button.click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        result = dialog.import_plan
        self.assertIs(result.inventory[0], original)
        self.assertEqual(original.quantity, 7)
        self.assertEqual([(item.genes, item.quantity) for item in result.items], [("GYYGGY", 6)])
        self.assertEqual(result.skipped_genes, ("YYYGGG",))

    def test_live_frames_need_consecutive_observations_and_never_inflate_quantity(self):
        dialog = self.new_dialog()
        token = dialog.generation
        dialog.receive_result(("YYYGGG", "YYYGGG"), live=True, token=token)
        self.assertEqual(dialog.table.rowCount(), 0)
        dialog.receive_result((), live=True, token=token)
        dialog.receive_result(("YYYGGG",), live=True, token=token)
        self.assertEqual(dialog.table.rowCount(), 0)
        dialog.receive_result(("YYYGGG",), live=True, token=token)
        for _ in range(5):
            dialog.receive_result(("YYYGGG", "YYYGGG"), live=True, token=token)
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(dialog.session.candidates[0].occurrences, 6)
        dialog.commit_import()
        self.assertEqual(dialog.import_plan.items[0].quantity, 1)

    def test_screenshot_ocr_runs_off_gui_thread_and_allows_event_processing(self):
        dialog = self.new_dialog()
        recognize, started, release = self.delayed_recognizer(("YYYGGG", "YYYGGG"))
        delivered_threads = []
        original_receive = dialog.receive_result

        def receive(*args, **kwargs):
            delivered_threads.append(QThread.currentThread())
            return original_receive(*args, **kwargs)

        heartbeat = []
        with patch.object(capture, "recognize_gene_image", recognize), patch.object(dialog, "receive_result", receive):
            dialog.recognize_image(self.image())
            self.assertTrue(started.wait(2))
            self.assertTrue(dialog.worker.isRunning())
            self.assertFalse(dialog.file_button.isEnabled())
            QTimer.singleShot(0, lambda: heartbeat.append(True))
            self.wait_until(lambda: bool(heartbeat))
            self.assertIsNotNone(dialog.worker)
            release.set()
            self.wait_until(lambda: dialog.worker is None)
        self.assertEqual(delivered_threads, [self.qt.thread()])
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(dialog.session.candidates[0].occurrences, 2)
        self.assertTrue(dialog.import_button.isEnabled())

    def test_pause_discards_inflight_result_then_new_screenshot_can_succeed(self):
        dialog = self.new_dialog()
        recognize, started, release = self.delayed_recognizer()
        dialog.running = True
        with patch.object(capture, "recognize_gene_image", recognize):
            dialog.recognize_image(self.image(), live=True)
            self.assertTrue(started.wait(2))
            dialog.stop_capture()
            release.set()
            self.wait_until(lambda: dialog.worker is None)
        self.assertEqual(dialog.session.candidates, ())
        self.assertFalse(dialog.timer.isActive())
        with patch.object(capture, "recognize_gene_image", return_value=("GGGYYY",)):
            dialog.recognize_image(self.image())
            self.wait_until(lambda: dialog.worker is None)
        self.assertEqual([item.genes for item in dialog.session.candidates], ["GGGYYY"])

    def test_ocr_failure_stops_recording_and_supports_retry(self):
        dialog = self.new_dialog()
        dialog.running = True
        with patch.object(capture, "recognize_gene_image", side_effect=RuntimeError("OCR language unavailable")):
            dialog.recognize_image(self.image(), live=True)
            self.wait_until(lambda: dialog.worker is None)
        self.assertFalse(dialog.running)
        self.assertFalse(dialog.timer.isActive())
        self.assertIn("OCR language unavailable", dialog.status.text())
        self.assertTrue(dialog.file_button.isEnabled())
        with patch.object(capture, "recognize_gene_image", return_value=("YYYGGG",)):
            dialog.recognize_image(self.image())
            self.wait_until(lambda: dialog.worker is None)
        self.assertTrue(dialog.import_button.isEnabled())

    def test_close_defers_rejection_until_worker_stops_without_importing(self):
        dialog = self.new_dialog()
        dialog.show()
        recognize, started, release = self.delayed_recognizer()
        rejected = []
        dialog.rejected.connect(lambda: rejected.append(True))
        with patch.object(capture, "recognize_gene_image", recognize):
            dialog.recognize_image(self.image())
            self.assertTrue(started.wait(2))
            dialog.reject()
            self.assertTrue(dialog.close_pending)
            self.assertEqual(rejected, [])
            self.assertIsNotNone(dialog.worker)
            release.set()
            self.wait_until(lambda: dialog.worker is None and bool(rejected))
        self.assertFalse(dialog.isVisible())
        self.assertEqual(dialog.session.candidates, ())
        self.assertIsNone(dialog.import_plan)

    def test_null_screenshot_reports_error_and_clear_preserves_library(self):
        original = InventoryItem("existing", "YYYGGG", 4)
        dialog = self.new_dialog([original])
        with patch.object(capture, "recognize_gene_image") as recognize:
            dialog.recognize_image(QImage())
        recognize.assert_not_called()
        self.assertIsNone(dialog.worker)
        self.assertIn("为空", dialog.status.text())
        dialog.receive_result(("GGGYYY",))
        dialog.clear_button.click()
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertEqual(dialog.inventory, (original,))
        self.assertFalse(dialog.import_button.isEnabled())

    def test_region_capture_uses_screen_local_coordinates_on_offset_high_dpi_screen(self):
        calls = []
        image = self.image()
        image.setDevicePixelRatio(2)

        class Screen:
            def geometry(self):
                return QRect(-1920, -200, 1920, 1080)

            def grabWindow(self, *args):
                calls.append(args)
                return QPixmap.fromImage(image)

        screen = Screen()
        with patch.object(QApplication, "screens", return_value=[screen]):
            dialog = self.new_dialog(advanced=True)
            dialog.capture_screen = screen
            dialog.region = QRect(170, 90, 160, 40)
            dialog.running = True
            with patch.object(QApplication, "applicationState", return_value=Qt.ApplicationState.ApplicationInactive), \
                    patch.object(dialog, "recognize_image") as recognize:
                dialog.capture_frame()
            self.assertEqual(calls, [(0, 170, 90, 160, 40)])
            self.assertEqual(recognize.call_args.kwargs, {"live": True})
            self.assertEqual(recognize.call_args.args[0].devicePixelRatio(), 2)
            dialog.region = QRect(1900, 90, 160, 40)
            with patch.object(dialog, "recognize_image") as recognize:
                dialog.capture_frame()
            recognize.assert_not_called()
            self.assertFalse(dialog.running)
            self.assertIn("重新框选", dialog.status.text())

    def test_region_selector_normalizes_reverse_drag_and_escape_cancels(self):
        selector = capture.RegionSelector(QApplication.primaryScreen())
        self.windows.append(selector)
        selector.show()
        QTest.mousePress(selector, Qt.MouseButton.LeftButton, pos=QPoint(200, 120))
        QTest.mouseMove(selector, QPoint(60, 40))
        QTest.mouseRelease(selector, Qt.MouseButton.LeftButton, pos=QPoint(60, 40))
        self.assertEqual(selector.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(selector.selection, QRect(QPoint(200, 120), QPoint(60, 40)).normalized())
        selector.show()
        QTest.keyClick(selector, Qt.Key.Key_Escape)
        self.assertEqual(selector.result(), QDialog.DialogCode.Rejected)

    def test_changing_screen_discards_old_region_and_disables_capture(self):
        class Screen:
            def __init__(self, x):
                self.x = x

            def geometry(self):
                return QRect(self.x, 0, 1920, 1080)

        first, second = Screen(0), Screen(1920)
        with patch.object(QApplication, "screens", return_value=[first, second]):
            dialog = self.new_dialog(advanced=True)
            dialog.capture_screen = first
            dialog.region = QRect(100, 100, 160, 40)
            dialog.start_button.setEnabled(True)
            token = dialog.generation
            dialog.screen_combo.setCurrentIndex(1)
            self.assertIsNone(dialog.capture_screen)
            self.assertTrue(dialog.region.isEmpty())
            self.assertFalse(dialog.start_button.isEnabled())
            self.assertGreater(dialog.generation, token)
            self.assertIn("切换屏幕", dialog.status.text())

    def test_geometry_or_dpi_change_invalidates_even_a_region_still_in_bounds(self):
        class Screen(QObject):
            geometryChanged = Signal(QRect)
            logicalDotsPerInchChanged = Signal(float)
            physicalDotsPerInchChanged = Signal(float)

            def geometry(self):
                return QRect(-1920, 0, 1920, 1080)

        screen = Screen()
        with patch.object(QApplication, "screens", return_value=[screen]):
            for name, value in (("geometryChanged", QRect(-1920, 0, 2560, 1440)),
                                ("logicalDotsPerInchChanged", 120.0),
                                ("physicalDotsPerInchChanged", 144.0)):
                with self.subTest(signal=name):
                    dialog = self.new_dialog(advanced=True)
                    dialog.capture_screen = screen
                    dialog.region = QRect(100, 100, 160, 40)
                    dialog.start_button.setEnabled(True)
                    getattr(screen, name).emit(value)
                    self.assertIsNone(dialog.capture_screen)
                    self.assertTrue(dialog.region.isEmpty())
                    self.assertFalse(dialog.start_button.isEnabled())
                    self.assertIn("重新框选", dialog.status.text())

    def test_capture_detects_dpi_change_without_screen_signal(self):
        class Screen:
            ratio = 1.0

            def geometry(self):
                return QRect(1920, 0, 1920, 1080)

            def devicePixelRatio(self):
                return self.ratio

        screen = Screen()
        with patch.object(QApplication, "screens", return_value=[screen]):
            dialog = self.new_dialog(advanced=True)
            dialog.capture_screen = screen
            dialog.region = QRect(100, 100, 160, 40)
            dialog.capture_signature = dialog.screen_signature(screen)
            dialog.running = True
            screen.ratio = 1.5
            with patch.object(dialog, "recognize_image") as recognize:
                dialog.capture_frame()
            recognize.assert_not_called()
            self.assertFalse(dialog.running)
            self.assertTrue(dialog.region.isEmpty())
            self.assertFalse(dialog.start_button.isEnabled())

    def test_default_automatic_source_starts_without_a_region(self):
        owner, dialog = self.visible_capture()
        dialog.region = QRect()
        self.assertEqual(dialog.source_mode.currentData(), "detail")
        self.assertFalse(dialog.select_button.isVisible())
        self.assertTrue(dialog.start_button.isEnabled())
        with patch.object(capture, "RegionSelector") as selector:
            dialog.start_button.click()
        selector.assert_not_called()
        self.assertTrue(dialog.running)
        self.assertTrue(dialog.region.isEmpty())
        self.assertFalse(owner.isVisible())
        self.assertFalse(dialog.isVisible())
        self.assertTrue(dialog.control.isVisible())
        self.assertTrue(dialog.timer.isActive())

    def test_default_detail_bounds_cover_central_genes_and_use_under_ten_percent_of_screen(self):
        screen = QRect(-1919, 200, 1919, 1079)
        region = capture.central_detail_region(screen)
        self.assertEqual(region, QRect(671, 269, 538, 357))
        self.assertTrue(region.contains(QRect(822, 416, 130, 15)))
        self.assertLess(region.width() * region.height(), screen.width() * screen.height() * 0.1)
        for width, height in ((1536, 864), (2560, 1440), (3440, 1440)):
            with self.subTest(width=width, height=height):
                bounds = capture.central_detail_region(QRect(0, 0, width, height))
                self.assertTrue(QRect(0, 0, width, height).contains(bounds))
                self.assertTrue(bounds.contains(QPoint(round(width * 0.46), round(height * 0.39))))

    def test_default_detail_capture_grabs_only_small_screen_local_bounds(self):
        calls = []

        class Screen:
            def geometry(self):
                return QRect(-1919, 200, 1919, 1079)

            def grabWindow(self, *args):
                calls.append(args)
                image = QImage(args[3] * 2, args[4] * 2, QImage.Format.Format_RGB32)
                image.fill(Qt.GlobalColor.white)
                image.setDevicePixelRatio(2)
                return QPixmap.fromImage(image)

        screen = Screen()
        with patch.object(QApplication, "screens", return_value=[screen]), \
                patch.object(QApplication, "topLevelWindows", return_value=[]):
            dialog = self.new_dialog()
            dialog.running = True
            with patch.object(dialog, "recognize_image") as recognize:
                dialog.capture_frame()
        self.assertEqual(calls, [(0, 671, 269, 538, 357)])
        self.assertEqual(recognize.call_args.args[0].width(), 1076)
        self.assertEqual(recognize.call_args.args[0].height(), 714)
        self.assertEqual(recognize.call_args.args[0].devicePixelRatio(), 2)

    def test_timer_capture_or_mask_error_expands_and_preserves_candidates_and_inventory(self):
        image = self.image()

        class Screen:
            def geometry(self):
                return QRect(0, 0, 1920, 1080)

            def availableGeometry(self):
                return self.geometry()

            def grabWindow(self, *args):
                return QPixmap.fromImage(image)

        screen = Screen()
        original = InventoryItem("preserved", "YYYYGG", 96)
        with patch.object(QApplication, "screens", return_value=[screen]):
            for phase in ("capture", "mask"):
                with self.subTest(phase=phase):
                    owner = QWidget()
                    owner.show()
                    self.windows.append(owner)
                    dialog = self.new_dialog([original], parent=owner)
                    dialog.receive_result(("YYYGGG",))
                    before = dialog.session.candidates
                    dialog.show()
                    self.qt.processEvents()
                    target = screen if phase == "capture" else capture
                    name = "grabWindow" if phase == "capture" else "mask_application_windows"
                    with patch.object(target, name, side_effect=RuntimeError("capture backend unavailable")):
                        dialog.start_button.click()
                        self.assertFalse(owner.isVisible())
                        self.assertFalse(dialog.isVisible())
                        # Exercise the actual single-shot timer callback.
                        dialog.timer.start(0)
                        self.wait_until(lambda: not dialog.running)
                    self.assertFalse(dialog.timer.isActive())
                    self.assertTrue(owner.isVisible())
                    self.assertTrue(dialog.isVisible())
                    self.assertFalse(dialog.control.isVisible())
                    self.assertIsNone(dialog.worker)
                    self.assertIn("无法读取当前画面", dialog.status.text())
                    self.assertIn("切回游戏后重试", dialog.status.text())
                    self.assertEqual(dialog.session.candidates, before)
                    self.assertEqual(dialog.inventory, (original,))
                    self.assertTrue(dialog.import_button.isEnabled())
                    dialog.reject()

    def test_exact_cache_requires_second_independent_grab_and_skips_ocr_and_table_refresh(self):
        image = self.image()

        class Screen:
            grabs = 0

            def geometry(self):
                return QRect(0, 0, 1920, 1080)

            def grabWindow(self, *args):
                self.grabs += 1
                return QPixmap.fromImage(image)

        screen = Screen()
        with patch.object(QApplication, "screens", return_value=[screen]), \
                patch.object(QApplication, "topLevelWindows", return_value=[]):
            dialog = self.new_dialog()
            dialog.running = True
            with patch.object(dialog.timer, "start"), \
                    patch.object(capture, "recognize_gene_image", return_value=("WHYXGW",)) as recognize, \
                    patch.object(dialog, "sync_table", wraps=dialog.sync_table) as sync:
                dialog.capture_frame()
                self.wait_until(lambda: dialog.worker is None)
                self.assertEqual(screen.grabs, 1)
                self.assertEqual(dialog.table.rowCount(), 0)
                self.assertFalse(dialog.cached_sampled)
                self.assertEqual(recognize.call_count, 1)
                dialog.capture_frame()
                self.assertEqual(screen.grabs, 2)
                self.assertEqual(dialog.table.rowCount(), 1)
                self.assertEqual(dialog.session.candidates[0].genes, "WHYXGW")
                self.assertTrue(dialog.cached_sampled)
                self.assertEqual(recognize.call_count, 1)
                table_updates = sync.call_count
                dialog.capture_frame()
                self.assertEqual(screen.grabs, 3)
                self.assertEqual(recognize.call_count, 1)
                self.assertEqual(sync.call_count, table_updates)
                self.assertIn("已确认 WHYXGW", dialog.control.status.text())

    def test_even_one_changed_pixel_invalidates_cache_and_new_gene_needs_two_samples(self):
        first = self.image()
        second = first.copy()
        second.setPixelColor(80, 20, Qt.GlobalColor.black)
        frames = [first, first, second, second]

        class Screen:
            grabs = 0

            def geometry(self):
                return QRect(0, 0, 1920, 1080)

            def grabWindow(self, *args):
                image = frames[self.grabs]
                self.grabs += 1
                return QPixmap.fromImage(image)

        screen = Screen()
        with patch.object(QApplication, "screens", return_value=[screen]), \
                patch.object(QApplication, "topLevelWindows", return_value=[]):
            dialog = self.new_dialog()
            dialog.running = True
            with patch.object(dialog.timer, "start"), \
                    patch.object(capture, "recognize_gene_image", side_effect=[("WHYXGW",), ("YYYGGG",)]) as recognize:
                dialog.capture_frame()
                self.wait_until(lambda: dialog.worker is None)
                dialog.capture_frame()
                self.assertEqual([row.genes for row in dialog.session.candidates], ["WHYXGW"])
                dialog.capture_frame()
                self.wait_until(lambda: dialog.worker is None)
                self.assertEqual(recognize.call_count, 2)
                self.assertEqual([row.genes for row in dialog.session.candidates], ["WHYXGW"])
                self.assertFalse(dialog.cached_sampled)
                dialog.capture_frame()
                self.assertEqual([row.genes for row in dialog.session.candidates], ["WHYXGW", "YYYGGG"])
                self.assertEqual(recognize.call_count, 2)
                self.assertEqual(screen.grabs, 4)

    def test_changing_source_discards_cached_pixels_and_previous_confirmation(self):
        image = self.image()

        class Screen:
            def geometry(self):
                return QRect(0, 0, 1920, 1080)

            def grabWindow(self, *args):
                return QPixmap.fromImage(image)

        screen = Screen()
        with patch.object(QApplication, "screens", return_value=[screen]), \
                patch.object(QApplication, "topLevelWindows", return_value=[]):
            dialog = self.new_dialog()
            dialog.running = True
            with patch.object(dialog.timer, "start"), \
                    patch.object(capture, "recognize_gene_image", return_value=("WHYXGW",)) as recognize:
                dialog.capture_frame()
                self.wait_until(lambda: dialog.worker is None)
                self.assertIsNotNone(dialog.cached_frame)
                dialog.source_mode.setCurrentIndex(dialog.source_mode.findData("auto"))
                self.assertIsNone(dialog.cached_frame)
                self.assertEqual(dialog.previous_frame, set())
                dialog.running = True
                dialog.capture_frame()
                self.wait_until(lambda: dialog.worker is None)
                self.assertEqual(recognize.call_count, 2)
                self.assertEqual(dialog.table.rowCount(), 0)

    def test_last_confirmed_gene_remains_visible_while_new_image_is_processing(self):
        dialog = self.new_dialog()
        token = dialog.generation
        dialog.receive_result(("WHYXGW",), live=True, token=token)
        dialog.receive_result(("WHYXGW",), live=True, token=token)
        recognize, started, release = self.delayed_recognizer()
        with patch.object(capture, "recognize_gene_image", recognize):
            dialog.recognize_image(self.image(), live=True)
            self.assertTrue(started.wait(2))
            self.assertIn("正在处理", dialog.control.status.text())
            self.assertIn("最近确认 WHYXGW", dialog.control.status.text())
            release.set()
            self.wait_until(lambda: dialog.worker is None)

    def test_default_screen_follows_cursor_and_switching_screen_needs_no_region(self):
        class Screen:
            def __init__(self, x):
                self.x = x

            def geometry(self):
                return QRect(self.x, 0, 1920, 1080)

        first, second = Screen(0), Screen(1920)
        with patch.object(QApplication, "screens", return_value=[first, second]), \
                patch.object(QApplication, "screenAt", return_value=second):
            dialog = self.new_dialog()
            self.assertEqual(dialog.screen_combo.currentIndex(), 1)
            self.assertIs(dialog.capture_screen, second)
            self.assertTrue(dialog.start_button.isEnabled())
            dialog.previous_frame = {"YYYGGG"}
            dialog.screen_combo.setCurrentIndex(0)
            self.assertIs(dialog.capture_screen, first)
            self.assertTrue(dialog.region.isEmpty())
            self.assertTrue(dialog.start_button.isEnabled())
            self.assertEqual(dialog.previous_frame, set())

    def test_automatic_capture_grabs_whole_screen_and_adapts_dpi_and_size(self):
        calls = []

        class Screen:
            bounds = QRect(-160, 80, 160, 80)
            ratio = 1.0

            def geometry(self):
                return self.bounds

            def devicePixelRatio(self):
                return self.ratio

            def grabWindow(self, *args):
                calls.append(args)
                image = QImage(self.bounds.width() * 2, self.bounds.height() * 2, QImage.Format.Format_RGB32)
                image.fill(Qt.GlobalColor.white)
                image.setDevicePixelRatio(2)
                return QPixmap.fromImage(image)

        screen = Screen()
        with patch.object(QApplication, "screens", return_value=[screen]), \
                patch.object(QApplication, "topLevelWindows", return_value=[]):
            dialog = self.new_dialog()
            dialog.source_mode.setCurrentIndex(dialog.source_mode.findData("auto"))
            dialog.capture_signature = dialog.screen_signature(screen)
            dialog.running = True
            with patch.object(dialog, "recognize_image") as recognize:
                dialog.capture_frame()
            self.assertEqual(calls, [(0,)])
            self.assertEqual(recognize.call_args.kwargs, {"live": True})
            self.assertEqual(recognize.call_args.args[0].size().width(), 320)
            self.assertEqual(recognize.call_args.args[0].devicePixelRatio(), 2)
            dialog.previous_frame = {"YYYGGG"}
            generation = dialog.generation
            screen.bounds = QRect(0, -200, 240, 120)
            screen.ratio = 1.5
            with patch.object(dialog, "recognize_image") as recognize:
                dialog.capture_frame()
            self.assertEqual(calls, [(0,), (0,)])
            self.assertEqual(recognize.call_args.args[0].width(), 480)
            self.assertTrue(dialog.running)
            self.assertTrue(dialog.start_button.isEnabled())
            self.assertEqual(dialog.previous_frame, set())
            self.assertGreater(dialog.generation, generation)
            self.assertEqual(dialog.capture_signature, dialog.screen_signature(screen))

    def test_automatic_screen_signal_adapts_instead_of_requesting_a_region(self):
        class Screen(QObject):
            logicalDotsPerInchChanged = Signal(float)

            def geometry(self):
                return QRect(-1920, 0, 1920, 1080)

        screen = Screen()
        with patch.object(QApplication, "screens", return_value=[screen]):
            dialog = self.new_dialog()
            dialog.running = True
            dialog.previous_frame = {"YYYGGG"}
            screen.logicalDotsPerInchChanged.emit(120.0)
            self.assertTrue(dialog.running)
            self.assertIs(dialog.capture_screen, screen)
            self.assertEqual(dialog.previous_frame, set())
            self.assertTrue(dialog.start_button.isEnabled())
            self.assertIn("自动适配", dialog.status.text())
            self.assertNotIn("框选", dialog.status.text())

    def test_mask_excludes_all_visible_app_windows_using_actual_pixel_scale(self):
        class Window:
            def __init__(self, rect, *, visible=True, minimized=False):
                self.rect = rect
                self.visible = visible
                self.minimized = minimized

            def isVisible(self):
                return self.visible

            def windowState(self):
                return Qt.WindowState.WindowMinimized if self.minimized else Qt.WindowState.WindowNoState

            def frameGeometry(self):
                return self.rect

        image = QImage(400, 200, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        # Deliberately differ from width/sourceWidth: do not blindly use DPR.
        image.setDevicePixelRatio(1.5)
        windows = [Window(QRect(-180, 110, 20, 10)), Window(QRect(-210, 90, 20, 20)),
                   Window(QRect(-120, 140, 20, 10), visible=False),
                   Window(QRect(-100, 140, 20, 10), minimized=True)]
        with patch.object(QApplication, "topLevelWindows", return_value=windows):
            result = capture.mask_application_windows(image, QRect(-200, 100, 200, 100))
        self.assertEqual(result.pixelColor(45, 25).name(), "#202020")
        self.assertEqual(result.pixelColor(5, 5).name(), "#202020")
        self.assertEqual(result.pixelColor(100, 25).name(), "#ffffff")
        self.assertEqual(result.pixelColor(165, 85).name(), "#ffffff")
        self.assertEqual(result.pixelColor(205, 85).name(), "#ffffff")
        self.assertEqual(result.devicePixelRatio(), 1.5)
        self.assertEqual(image.pixelColor(45, 25).name(), "#ffffff")

    def test_only_live_automatic_source_requires_colored_gene_groups(self):
        for advanced, live, expected in ((False, True, {"colored_only": True}),
                                         (False, False, {}), (True, True, {})):
            with self.subTest(advanced=advanced, live=live):
                dialog = self.new_dialog(advanced=advanced)
                with patch.object(capture, "recognize_gene_image", return_value=()) as recognize:
                    dialog.recognize_image(self.image(), live=live)
                    self.wait_until(lambda: dialog.worker is None)
                self.assertEqual(recognize.call_args.kwargs, expected)

    def test_png_encoding_runs_off_gui_thread_and_finish_expands_while_encoding(self):
        owner, dialog = self.visible_capture()
        dialog.start_button.click()
        dialog.timer.stop()
        dialog.previous_frame = {"YYYGGG"}
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)
        encoding_threads = []
        original_encode = capture.image_png

        def slow_encode(image):
            encoding_threads.append(QThread.currentThread())
            started.set()
            if not release.wait(3):
                raise RuntimeError("Test did not release PNG encoding")
            return original_encode(image)

        with patch.object(capture, "image_png", slow_encode), \
                patch.object(capture, "recognize_gene_image", return_value=("YYYGGG",)):
            dialog.recognize_image(self.image(), live=True)
            self.assertTrue(started.wait(2))
            self.assertTrue(dialog.worker.isRunning())
            dialog.control.finish_button.click()
            self.assertTrue(owner.isVisible())
            self.assertTrue(dialog.isVisible())
            self.assertFalse(dialog.control.isVisible())
            self.assertFalse(dialog.import_button.isEnabled())
            release.set()
            self.wait_until(lambda: dialog.worker is None)
        self.assertEqual(len(encoding_threads), 1)
        self.assertNotEqual(encoding_threads[0], self.qt.thread())
        self.assertEqual([row.genes for row in dialog.session.candidates], ["YYYGGG"])
        self.assertFalse(dialog.timer.isActive())

    def test_ocr_worker_still_accepts_preencoded_png(self):
        png = capture.image_png(self.image())
        worker = capture.OcrWorker(png)
        try:
            with patch.object(capture, "image_png") as encode, \
                    patch.object(capture, "recognize_gene_image", return_value=()) as recognize:
                worker.start()
                self.assertTrue(worker.wait(2000))
            encode.assert_not_called()
            recognize.assert_called_once_with(png)
        finally:
            worker.deleteLater()

    def test_floating_feedback_distinguishes_missing_first_and_confirmed_frames(self):
        dialog = self.new_dialog()
        token = dialog.generation
        dialog.receive_result((), live=True, token=token)
        self.assertIn("未识别到", dialog.control.status.text())
        dialog.receive_result(("YYYGGG",), live=True, token=token)
        self.assertIn("首次读到 YYYGGG", dialog.control.status.text())
        self.assertIn("再次确认", dialog.control.status.text())
        self.assertEqual(dialog.table.rowCount(), 0)
        dialog.receive_result(("YYYGGG",), live=True, token=token)
        self.assertIn("已确认 YYYGGG", dialog.control.status.text())
        self.assertIn("1 组", dialog.control.status.text())
        self.assertEqual(dialog.table.rowCount(), 1)

    def test_fast_capture_remains_serial_and_shows_processing_feedback(self):
        dialog = self.new_dialog()
        dialog.capture_screen = QApplication.primaryScreen()
        dialog.region = QRect(1, 1, 100, 30)
        dialog.toggle_capture()
        self.assertEqual(dialog.timer.interval(), capture.CAPTURE_INTERVAL_MS)
        dialog.timer.stop()
        recognize, started, release = self.delayed_recognizer()
        with patch.object(capture, "recognize_gene_image", recognize):
            dialog.recognize_image(self.image(), live=True)
            self.assertTrue(started.wait(2))
            worker = dialog.worker
            self.assertIn("正在处理", dialog.control.status.text())
            dialog.capture_frame()
            dialog.recognize_image(self.image(), live=True)
            self.assertIs(dialog.worker, worker)
            self.assertEqual(dialog.timer.interval(), capture.CAPTURE_INTERVAL_MS)
            dialog.timer.stop()
            release.set()
            self.wait_until(lambda: dialog.worker is None)
        self.assertEqual(dialog.timer.interval(), capture.CAPTURE_INTERVAL_MS)
        self.assertIn("首次读到", dialog.control.status.text())

    def test_candidate_limit_stops_worker_loop_and_keeps_previous_review(self):
        from rust_breeder import capture_import

        dialog = self.new_dialog()
        with patch.object(capture_import, "MAX_INVENTORY_ITEMS", 2):
            dialog.receive_result(("YYYGGG", "GGGYYY"))
            before = dialog.session.candidates
            dialog.running = True
            dialog.previous_frame = {"HHHHHH"}
            with patch.object(capture, "recognize_gene_image", return_value=("HHHHHH",)):
                dialog.recognize_image(self.image(), live=True)
                self.wait_until(lambda: dialog.worker is None)
        self.assertFalse(dialog.running)
        self.assertFalse(dialog.timer.isActive())
        self.assertEqual(dialog.session.candidates, before)
        self.assertEqual(dialog.table.rowCount(), 2)
        self.assertIn("最多保留 2", dialog.status.text())
        self.assertTrue(dialog.import_button.isEnabled())

    def test_start_disables_import_immediately_and_pause_restores_it(self):
        dialog = self.new_dialog()
        dialog.capture_screen = QApplication.primaryScreen()
        dialog.region = QRect(1, 1, 100, 30)
        dialog.receive_result(("YYYGGG",))
        self.assertTrue(dialog.import_button.isEnabled())
        dialog.toggle_capture()
        self.assertTrue(dialog.running)
        self.assertFalse(dialog.import_button.isEnabled())
        dialog.toggle_capture()
        self.assertFalse(dialog.running)
        self.assertTrue(dialog.import_button.isEnabled())

    def test_recording_hides_workbench_and_finish_button_expands_preserving_review(self):
        owner, dialog = self.visible_capture()
        owner.showMaximized()
        original_state = owner.windowState()
        dialog.receive_result(("YYYGGG", "GGGYYY"))
        dialog.table.item(1, 1).setText("GYYGGY")
        dialog.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
        dialog.quantity.setValue(7)
        candidates = dialog.session.candidates

        dialog.start_button.click()
        self.assertTrue(dialog.running)
        self.assertTrue(dialog.collapsed)
        self.assertFalse(owner.isVisible())
        self.assertFalse(dialog.isVisible())
        self.assertTrue(dialog.control.isVisible())
        self.assertTrue(dialog.control.finish_button.isVisible())
        self.assertIn("2 组", dialog.control.status.text())
        self.assertFalse(dialog.import_button.isEnabled())

        dialog.control.finish_button.click()
        self.assertFalse(dialog.running)
        self.assertFalse(dialog.collapsed)
        self.assertFalse(dialog.timer.isActive())
        self.assertFalse(dialog.control.isVisible())
        self.assertTrue(owner.isVisible())
        self.assertTrue(dialog.isVisible())
        self.assertEqual(owner.windowState(), original_state)
        self.assertEqual(dialog.session.candidates, candidates)
        self.assertEqual(dialog.table.item(1, 1).text(), "GYYGGY")
        self.assertEqual(dialog.table.item(1, 0).checkState(), Qt.CheckState.Unchecked)
        self.assertEqual(dialog.quantity.value(), 7)
        self.assertTrue(dialog.import_button.isEnabled())

    def test_finish_button_expands_and_keeps_confirmed_inflight_frame(self):
        owner, dialog = self.visible_capture()
        dialog.receive_result(("GGGYYY",))
        before = dialog.session.candidates
        recognize, started, release = self.delayed_recognizer(("YYYGGG",))
        dialog.start_button.click()
        dialog.timer.stop()
        dialog.previous_frame = {"YYYGGG"}
        with patch.object(capture, "recognize_gene_image", recognize):
            dialog.recognize_image(self.image(), live=True)
            self.assertTrue(started.wait(2))
            dialog.control.finish_button.click()
            self.assertTrue(owner.isVisible())
            self.assertTrue(dialog.isVisible())
            self.assertFalse(dialog.control.isVisible())
            self.assertIsNotNone(dialog.worker)
            self.assertFalse(dialog.import_button.isEnabled())
            release.set()
            self.wait_until(lambda: dialog.worker is None)
        self.assertEqual([row.genes for row in dialog.session.candidates], ["GGGYYY", "YYYGGG"])
        self.assertEqual(dialog.session.candidates[0], before[0])
        self.assertEqual(dialog.table.rowCount(), 2)
        self.assertIn("最后一帧已处理", dialog.status.text())
        self.assertFalse(dialog.finishing_capture)
        self.assertFalse(dialog.timer.isActive())
        self.assertTrue(dialog.import_button.isEnabled())
        self.assertTrue(owner.isVisible())
        self.assertTrue(dialog.isVisible())

    def test_finish_does_not_accept_a_single_unconfirmed_inflight_observation(self):
        owner, dialog = self.visible_capture()
        recognize, started, release = self.delayed_recognizer()
        dialog.start_button.click()
        dialog.timer.stop()
        with patch.object(capture, "recognize_gene_image", recognize):
            dialog.recognize_image(self.image(), live=True)
            self.assertTrue(started.wait(2))
            dialog.finish_capture()
            self.assertTrue(owner.isVisible())
            self.assertTrue(dialog.isVisible())
            self.assertFalse(dialog.start_button.isEnabled())
            release.set()
            self.wait_until(lambda: dialog.worker is None)
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertIn("尚未通过连续两次确认", dialog.status.text())
        self.assertFalse(dialog.timer.isActive())

    def test_ocr_error_automatically_expands_hidden_windows_without_losing_candidates(self):
        owner, dialog = self.visible_capture()
        dialog.receive_result(("GGGYYY",))
        before = dialog.session.candidates
        dialog.quantity.setValue(4)
        recognize, started, release = self.delayed_recognizer(error=RuntimeError("local OCR failed"))
        dialog.start_button.click()
        dialog.timer.stop()
        with patch.object(capture, "recognize_gene_image", recognize):
            dialog.recognize_image(self.image(), live=True)
            self.assertTrue(started.wait(2))
            self.assertFalse(owner.isVisible())
            self.assertFalse(dialog.isVisible())
            release.set()
            self.wait_until(lambda: dialog.worker is None)
        self.assertFalse(dialog.running)
        self.assertFalse(dialog.collapsed)
        self.assertFalse(dialog.control.isVisible())
        self.assertTrue(owner.isVisible())
        self.assertTrue(dialog.isVisible())
        self.assertIn("local OCR failed", dialog.status.text())
        self.assertEqual(dialog.session.candidates, before)
        self.assertEqual(dialog.quantity.value(), 4)
        self.assertTrue(dialog.import_button.isEnabled())

    def test_reject_collapsed_capture_restores_owner_without_reopening_dialog(self):
        owner, dialog = self.visible_capture()
        dialog.receive_result(("GGGYYY",))
        before = dialog.session.candidates
        dialog.start_button.click()
        dialog.reject()
        self.assertFalse(dialog.running)
        self.assertFalse(dialog.collapsed)
        self.assertFalse(dialog.control.isVisible())
        self.assertFalse(dialog.isVisible())
        self.assertTrue(owner.isVisible())
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertEqual(dialog.session.candidates, before)
        self.assertIsNone(dialog.import_plan)

    def test_reject_collapsed_pending_worker_never_reopens_dialog_after_completion(self):
        owner, dialog = self.visible_capture()
        dialog.receive_result(("GGGYYY",))
        before = dialog.session.candidates
        recognize, started, release = self.delayed_recognizer(("YYYGGG",))
        dialog.start_button.click()
        dialog.timer.stop()
        with patch.object(capture, "recognize_gene_image", recognize):
            dialog.recognize_image(self.image(), live=True)
            self.assertTrue(started.wait(2))
            dialog.reject()
            self.assertTrue(dialog.close_pending)
            self.assertTrue(owner.isVisible())
            self.assertFalse(dialog.control.isVisible())
            self.assertFalse(dialog.isVisible())
            release.set()
            self.wait_until(lambda: dialog.worker is None and not dialog.close_pending)
        self.assertTrue(owner.isVisible())
        self.assertFalse(dialog.isVisible())
        self.assertFalse(dialog.control.isVisible())
        self.assertEqual(dialog.session.candidates, before)
        self.assertIsNone(dialog.import_plan)

    def test_main_window_one_click_import_preserves_guided_draft_and_autosaves(self):
        window = application.MainWindow(restore=False)
        self.windows.append(window)
        original = InventoryItem("center", "YYYGGG", 4, "原中央苗")
        donor = InventoryItem("donor", "GYYGGG", 2, "原供体")
        window.inventory = [original, donor]
        window.refresh_inventory()
        window.guided.actual_edit.setText("YGGYYY")
        window.guided.actual_quantity.setValue(2)
        before = window.snapshot()

        def accept_review(dialog):
            self.dialogs.append(dialog)
            dialog.receive_result(("YYYGGG", "GYYGGG", "GGGYYY", "GGGYYY"))
            dialog.quantity.setValue(3)
            dialog.commit_import()
            return dialog.result()

        with patch.object(application.ScreenCaptureDialog, "exec", new=accept_review):
            window.guided.capture_button.click()
        self.assertEqual(window.inventory[:2], [original, donor])
        self.assertEqual([(item.genes, item.quantity) for item in window.inventory[2:]], [("GGGYYY", 3)])
        after = window.snapshot()
        for key in ("journey", "guided", "crop", "target", "target_mode", "parallel", "parallel_ui"):
            self.assertEqual(after[key], before[key], key)
        window.autosave_timer.stop()
        window.autosave()
        self.assertEqual(load_project(self.autosave), window.snapshot())

    def test_pending_guided_round_blocks_capture_import_without_changing_assignment(self):
        window = application.MainWindow(restore=False)
        self.windows.append(window)
        window.inventory = [InventoryItem("center", "GYYGGG"),
                            InventoryItem("a", "YHYGGG"), InventoryItem("b", "YGGGGG")]
        center, *donors = window.inventory
        recipe = Recipe(center, tuple(donors), predict(center.genes, [row.genes for row in donors]), window.target())
        window.guided.state = prepare_round(JourneyState(target=window.target(), center_id=center.id), tuple(window.inventory), recipe)
        window.refresh_inventory()
        window.guided.actual_edit.setText("YYYGGG")
        before = window.snapshot()
        self.assertFalse(window.guided.capture_button.isEnabled())
        with patch.object(application.ScreenCaptureDialog, "exec") as opened:
            window.guided.capture_button.click()
            window.capture_genes()
        opened.assert_not_called()
        self.assertEqual(window.snapshot(), before)
        self.assertIsNotNone(window.guided.state.pending)

    def test_main_window_cancel_leaves_project_unchanged(self):
        window = application.MainWindow(restore=False)
        self.windows.append(window)
        window.inventory = [InventoryItem("seed", "YYYGGG", 5)]
        before = window.snapshot()

        def cancel_review(dialog):
            self.dialogs.append(dialog)
            dialog.receive_result(("GGGYYY",))
            dialog.reject()
            return QDialog.DialogCode.Rejected

        with patch.object(application.ScreenCaptureDialog, "exec", new=cancel_review):
            window.guided.capture_button.click()
        self.assertEqual(window.snapshot(), before)

    def test_main_window_primary_button_automatically_hides_and_reads_without_region(self):
        window = application.MainWindow(restore=False)
        self.windows.append(window)
        original = InventoryItem("existing", "YYYGGG", 96)
        window.inventory = [original]
        window.refresh_inventory()
        window.show()
        before = window.snapshot()
        state = {"completed": False, "errors": []}

        def check_started():
            try:
                dialog = window.findChild(capture.ScreenCaptureDialog)
                if dialog is None or not dialog.running:
                    QTimer.singleShot(10, check_started)
                    return
                self.dialogs.append(dialog)
                self.assertTrue(dialog.automatic_source())
                self.assertTrue(dialog.region.isEmpty())
                self.assertFalse(window.isVisible())
                self.assertFalse(dialog.isVisible())
                self.assertTrue(dialog.control.isVisible())
                dialog.timer.stop()
                dialog.control.finish_button.click()
                self.assertTrue(window.isVisible())
                self.assertTrue(dialog.isVisible())
                dialog.reject()
                state["completed"] = True
            except Exception as exc:
                state["errors"].append(repr(exc))
                dialog = window.findChild(capture.ScreenCaptureDialog)
                if dialog is not None:
                    dialog.reject()

        def timeout():
            state["errors"].append("Automatic reading did not start and finish")
            dialog = window.findChild(capture.ScreenCaptureDialog)
            if dialog is not None:
                dialog.reject()

        watchdog = QTimer(window)
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(timeout)
        watchdog.start(2000)
        QTimer.singleShot(0, check_started)
        with patch.object(capture, "RegionSelector") as selector:
            window.guided.capture_button.click()
        watchdog.stop()
        self.assertEqual(state["errors"], [])
        self.assertTrue(state["completed"])
        selector.assert_not_called()
        self.assertEqual(window.snapshot(), before)
        self.assertEqual(window.inventory, [original])

    def test_real_exec_keeps_main_call_open_through_hidden_selection_and_recording(self):
        for finish in ("accept", "reject"):
            with self.subTest(finish=finish):
                window = application.MainWindow(restore=False)
                self.windows.append(window)
                original = InventoryItem("existing", "YYYGGG", 5)
                window.inventory = [original]
                window.refresh_inventory()
                window.show()
                before = window.snapshot()
                state = {"returned": False, "completed": False, "errors": [], "phases": []}

                def safely(callback):
                    def run():
                        try:
                            callback()
                        except Exception as error:
                            state["errors"].append(repr(error))
                            state["completed"] = True
                            dialog = state.get("dialog")
                            if dialog is not None:
                                dialog.reject()
                    return run

                def finish_review():
                    dialog = state["dialog"]
                    state["phases"].append(("expanded", state["returned"],
                                             window.isVisible(), dialog.isVisible(), dialog.control.isVisible()))
                    if finish == "accept":
                        dialog.import_button.click()
                    else:
                        dialog.reject()
                    state["completed"] = True

                def finish_recording():
                    dialog = state["dialog"]
                    state["phases"].append(("collapsed", state["returned"],
                                             window.isVisible(), dialog.isVisible(), dialog.control.isVisible()))
                    dialog.control.finish_button.click()
                    QTimer.singleShot(10, safely(finish_review))

                def restore_selection_and_start():
                    dialog = state["dialog"]
                    state["phases"].append(("hidden", state["returned"],
                                             window.isVisible(), dialog.isVisible(), dialog.control.isVisible()))
                    # Region selection restores these windows before recording.
                    window.show()
                    dialog.show()
                    dialog.capture_screen = QApplication.primaryScreen()
                    dialog.region = QRect(1, 1, 100, 30)
                    dialog.start_button.setEnabled(True)
                    dialog.start_button.click()
                    QTimer.singleShot(10, safely(finish_recording))

                def hide_for_selection():
                    dialog = window.findChild(capture.ScreenCaptureDialog)
                    if dialog is None:
                        raise AssertionError("The real capture dialog was not opened")
                    self.dialogs.append(dialog)
                    state["dialog"] = dialog
                    dialog.auto_start = False
                    dialog.receive_result(("GGGYYY",))
                    dialog.quantity.setValue(2)
                    # QDialog.exec used to return here, before region selection
                    # or the floating recording controls had finished.
                    dialog.hide()
                    window.hide()
                    QTimer.singleShot(10, safely(restore_selection_and_start))

                def timeout():
                    state["errors"].append("The real capture event loop did not finish")
                    state["completed"] = True
                    dialog = window.findChild(capture.ScreenCaptureDialog)
                    if dialog is not None:
                        dialog.reject()

                watchdog = QTimer(window)
                watchdog.setSingleShot(True)
                watchdog.timeout.connect(timeout)
                watchdog.start(2000)
                QTimer.singleShot(0, safely(hide_for_selection))
                # This exercises the actual dialog exec and MainWindow method.
                window.guided.capture_button.click()
                state["returned"] = True
                self.wait_until(lambda: state["completed"])
                watchdog.stop()
                self.assertEqual(state["errors"], [])
                self.assertEqual(state["phases"], [
                    ("hidden", False, False, False, False),
                    ("collapsed", False, False, False, True),
                    ("expanded", False, True, True, False),
                ])
                if finish == "accept":
                    self.assertEqual(window.inventory[0], original)
                    self.assertEqual([(item.genes, item.quantity) for item in window.inventory[1:]], [("GGGYYY", 2)])
                else:
                    self.assertEqual(window.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
