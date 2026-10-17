"""Python cycles that contain Qt callbacks are collected on the GUI thread."""

from __future__ import annotations

import gc
import os
from pathlib import Path
import sys
import unittest
import weakref

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / '.deps').exists():
    sys.path.insert(0, str(ROOT / '.deps'))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

try:
    from PySide6.QtCore import QEventLoop, QThread, QTimer
    from PySide6.QtWidgets import QApplication
    from rust_breeder.gui_runtime import COLLECTION_INTERVAL_MS, install_gui_collector
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


class Cycle:
    def __init__(self):
        self.loop = self


@unittest.skipUnless(QT_AVAILABLE, 'Install PySide6 for GUI collection checks.')
class GuiCollectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.manager = install_gui_collector()
        self.previously_enabled = self.manager.previously_enabled

    def tearDown(self):
        self.manager.restore()
        (gc.enable if self.previously_enabled else gc.disable)()
        self.manager.start()
        self.manager.timer.setInterval(COLLECTION_INTERVAL_MS)

    def test_application_has_one_active_collector_with_auto_gc_disabled(self):
        self.assertIs(install_gui_collector(), self.manager)
        self.assertTrue(self.manager.active)
        self.assertTrue(self.manager.timer.isActive())
        self.assertFalse(gc.isenabled())
        self.assertEqual(self.manager.thread(), self.qt.thread())

    def test_explicit_collection_clears_cycles_on_gui_thread(self):
        cycle = Cycle()
        reference = weakref.ref(cycle)
        del cycle
        self.assertIsNotNone(reference())
        threads = []
        def gc_started(phase, info):
            if phase == 'start':
                threads.append(QThread.currentThread() == self.qt.thread())
        gc.callbacks.append(gc_started)
        try:
            self.manager.collect()
        finally:
            gc.callbacks.remove(gc_started)
        self.assertIsNone(reference())
        self.assertEqual(threads, [True])
        self.assertEqual(self.manager.last_collection_thread, self.qt.thread())

    def test_gui_timer_collects_while_worker_allocates_python_cycles(self):
        references = []
        class Allocator(QThread):
            def run(self):
                for _ in range(1000):
                    cycle = Cycle()
                    references.append(weakref.ref(cycle))
        worker = Allocator()
        worker.start()
        self.assertTrue(worker.wait(5000))
        self.assertTrue(any(reference() is not None for reference in references))
        before = self.manager.collections
        loop = QEventLoop()
        check = QTimer()
        check.setInterval(1)
        check.timeout.connect(lambda: loop.quit() if self.manager.collections > before else None)
        self.manager.timer.setInterval(5)
        check.start()
        QTimer.singleShot(2000, loop.quit)
        loop.exec()
        check.stop()
        self.assertGreater(self.manager.collections, before)
        self.assertTrue(all(reference() is None for reference in references))
        self.assertEqual(self.manager.last_collection_thread, self.qt.thread())

    def test_direct_worker_call_cannot_collect_or_install_gui_objects(self):
        manager = self.manager
        failures = []
        before = manager.collections
        class Worker(QThread):
            def run(self):
                manager.collect()
                try:
                    install_gui_collector()
                except RuntimeError:
                    failures.append(True)
        worker = Worker()
        worker.start()
        self.assertTrue(worker.wait(5000))
        self.assertEqual(manager.collections, before)
        self.assertEqual(failures, [True])

    def test_shutdown_restores_previous_enabled_or_disabled_gc_policy(self):
        for enabled in (True, False):
            self.manager.restore()
            (gc.enable if enabled else gc.disable)()
            self.manager.start()
            self.assertFalse(gc.isenabled())
            self.assertEqual(self.manager.previously_enabled, enabled)
            self.manager.restore()
            self.assertEqual(gc.isenabled(), enabled)
            self.assertFalse(self.manager.timer.isActive())


if __name__ == '__main__':
    unittest.main()
