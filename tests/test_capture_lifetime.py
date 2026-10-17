"""Closed capture reviews release their widgets without killing active OCR."""

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
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication, QDialog
    from shiboken6 import isValid
    from rust_breeder import app as application
    from rust_breeder import capture_dialog as capture
    from rust_breeder.models import InventoryItem
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, "Install PySide6 for capture lifetime checks.")
class CaptureLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        artifacts = PROJECT_ROOT / "artifacts"
        artifacts.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="capture-lifetime-", dir=artifacts)
        self.autosave_patch = patch.object(application, "AUTOSAVE", Path(self.temporary.name) / "autosave.json")
        self.autosave_patch.start()
        self.window = application.MainWindow(restore=False)
        self.window.inventory = [InventoryItem("existing", "WGGYYY", 7, "已有苗")]
        self.window.refresh_inventory()
        self.release = threading.Event()

    def flush_deletes(self):
        self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def wait_for(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.flush_deletes()
            time.sleep(0.005)
        self.flush_deletes()
        self.assertTrue(condition())

    def tearDown(self):
        self.release.set()
        for dialog in self.window.findChildren(capture.ScreenCaptureDialog):
            worker = dialog.worker
            if worker is not None:
                worker.wait(5000)
        self.flush_deletes()
        self.window.autosave_timer.stop()
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.flush_deletes()
        self.autosave_patch.stop()
        self.temporary.cleanup()

    def test_fifty_closed_reviews_leave_no_dialogs_or_stock_changes(self):
        before = tuple(self.window.inventory)
        def cancelled(dialog):
            dialog.receive_result(("YYYGGG", "GYGYYG", "WGGYYY"))
            dialog.reject()
            return int(QDialog.DialogCode.Rejected)
        with patch.object(capture.ScreenCaptureDialog, "exec", cancelled):
            for _ in range(50):
                self.window.capture_genes()
                self.flush_deletes()
                self.assertEqual(self.window.findChildren(capture.ScreenCaptureDialog), [])
                self.assertEqual(tuple(self.window.inventory), before)

    def test_completed_import_is_copied_before_dialog_is_deleted(self):
        def imported(dialog):
            dialog.receive_result(("YYYGGG", "WGGYYY"))
            dialog.quantity.setValue(4)
            dialog.commit_import()
            return int(QDialog.DialogCode.Accepted)
        with patch.object(capture.ScreenCaptureDialog, "exec", imported):
            self.window.capture_genes()
        self.flush_deletes()
        self.assertEqual(self.window.findChildren(capture.ScreenCaptureDialog), [])
        stock = {row.genes: row.quantity for row in self.window.inventory}
        self.assertEqual(stock, {"WGGYYY": 7, "YYYGGG": 4})

    def test_exception_releases_review_and_preserves_inventory(self):
        before = tuple(self.window.inventory)
        with patch.object(capture.ScreenCaptureDialog, "exec", side_effect=RuntimeError("review failure")):
            with self.assertRaisesRegex(RuntimeError, "review failure"):
                self.window.capture_genes()
        self.flush_deletes()
        self.assertEqual(self.window.findChildren(capture.ScreenCaptureDialog), [])
        self.assertEqual(tuple(self.window.inventory), before)

    def test_exception_waits_for_active_ocr_before_releasing_dialog(self):
        started = threading.Event()
        dialogs = []
        before = tuple(self.window.inventory)
        def blocked_ocr(*args, **kwargs):
            started.set()
            if not self.release.wait(5):
                raise RuntimeError("test OCR timed out")
            return ("YYYGGG",)
        def failing_review(dialog):
            dialogs.append(dialog)
            image = QImage(200, 50, QImage.Format.Format_RGB32)
            image.fill(0)
            dialog.recognize_image(image)
            self.assertTrue(started.wait(2))
            raise RuntimeError("review failure while OCR runs")
        with patch.object(capture, "recognize_gene_image", blocked_ocr), \
                patch.object(capture.ScreenCaptureDialog, "exec", failing_review):
            with self.assertRaisesRegex(RuntimeError, "review failure while OCR runs"):
                self.window.capture_genes()
            self.flush_deletes()
            self.assertTrue(isValid(dialogs[0]))
            self.assertTrue(dialogs[0].worker.isRunning())
            self.assertFalse(dialogs[0].running)
            self.release.set()
            self.wait_for(lambda: not isValid(dialogs[0]))
        self.assertEqual(tuple(self.window.inventory), before)
        self.assertEqual(self.window.findChildren(capture.ScreenCaptureDialog), [])


if __name__ == "__main__":
    unittest.main()
