"""Both current planners own workers through queued finished delivery."""
from __future__ import annotations
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / '.deps').exists():
    sys.path.insert(0, str(ROOT / '.deps'))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
try:
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QThread, QTimer, Qt
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication
except ImportError:
    QT_AVAILABLE = False
else:
    QT_AVAILABLE = True
    from rust_breeder import app as application, guidance, parallel_guidance
    from rust_breeder.engine import predict
    from rust_breeder.models import InventoryItem, Recipe
    from rust_breeder.parallel_models import ParallelReport, ParallelWave, ParallelStep
    from rust_breeder.routes import RouteReport, RouteStep


@unittest.skipUnless(QT_AVAILABLE, 'Install PySide6 for worker lifecycle checks.')
class WorkerLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='worker-lifecycle-', dir=ROOT / 'artifacts')
        self.autosave_patch = patch.object(application, 'AUTOSAVE', Path(self.temporary.name) / 'autosave.json')
        self.autosave_patch.start()
        self.route_patch = patch.object(guidance, 'plan_route', self.route)
        self.route_patch.start()
        self.parallel_patch = patch.object(parallel_guidance, 'plan_parallel_process', self.parallel_route)
        self.parallel_patch.start()
        self.window = application.MainWindow(restore=False)
        self.window.inventory = [InventoryItem('center', 'GYYGGG', 1), InventoryItem('a', 'YHYGGG', 1), InventoryItem('b', 'YGGGGG', 1)]
        self.window.mode_combo.setCurrentIndex(self.window.mode_combo.findData('exact'))
        self.window.refresh_inventory()
        self.window.autosave_timer.stop()
        self.workspace, self.parallel = self.window.guided, self.window.parallel
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        for owner in self.window._pages:
            if owner.worker is not None:
                owner.worker.requestInterruption()
                owner.worker.wait(5000)
        self.qt.processEvents()
        self.window.autosave_timer.stop()
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.flush_deletes()
        self.parallel_patch.stop()
        self.route_patch.stop()
        self.autosave_patch.stop()
        self.temporary.cleanup()

    def recipe(self, inventory, target):
        rows = {row.id: row for row in inventory}
        center, donors = rows['center'], (rows['a'], rows['b'])
        return Recipe(center, donors, predict(center.genes, [row.genes for row in donors]), target)

    def route(self, inventory, target, **options):
        options.get('progress', lambda *_: None)(12, 1)
        recipe = self.recipe(inventory, target)
        return RouteReport((RouteStep(recipe, 'YYYGGG', 'output', 1),), (recipe,), True, True, 12, 'exhausted')

    def parallel_route(self, inventory, target, **options):
        options.get('progress', lambda *_: None)(12, 1)
        recipe = self.recipe(inventory, target)
        wave = ParallelWave(1, (ParallelStep(1, recipe, 'YYYGGG', 'output', 1),))
        return ParallelReport((wave,), True, True, 12, 'found')

    def flush_deletes(self):
        self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def wait_clean(self, owner):
        loop, check = QEventLoop(), QTimer()
        check.setInterval(1)
        check.timeout.connect(lambda: loop.quit() if owner.worker is None else None)
        check.start()
        QTimer.singleShot(5000, loop.quit)
        if owner.worker is not None:
            loop.exec()
        check.stop()
        self.assertIsNone(owner.worker)

    def remain_busy_through_finished(self, owner):
        owner.start_planning()
        worker = owner.worker
        self.assertTrue(worker.wait(5000))
        self.assertFalse(worker.isRunning())
        self.assertTrue(owner.is_busy())
        owner.start_planning()
        self.assertIs(owner.worker, worker)
        self.wait_clean(owner)
        self.assertFalse(owner.is_busy())
        self.assertIsNotNone(owner.state.pending)

    def test_route_remains_busy_until_queued_finished_is_handled(self):
        self.remain_busy_through_finished(self.workspace)

    def test_parallel_remains_busy_until_queued_finished_is_handled(self):
        self.remain_busy_through_finished(self.parallel)

    def stale_callbacks(self, parallel, failure):
        owner = self.parallel if parallel else self.workspace
        module = parallel_guidance if parallel else guidance
        name = 'plan_parallel_process' if parallel else 'plan_route'
        worker_class = parallel_guidance.ParallelWorker if parallel else guidance.RouteWorker
        calculate = self.parallel_route if parallel else self.route
        old = worker_class(tuple(self.window.inventory), self.window.target(), {}, owner)
        old.result_ready.connect(owner.route_result_ready, Qt.ConnectionType.QueuedConnection)
        old.failed.connect(owner.route_failed, Qt.ConnectionType.QueuedConnection)
        old.progress_changed.connect(owner.route_progress, Qt.ConnectionType.QueuedConnection)
        old.finished.connect(owner.search_finished, Qt.ConnectionType.QueuedConnection)
        owner.worker = old
        if failure:
            def fail(*args, **kwargs):
                kwargs['progress'](99, 2)
                raise ValueError('obsolete error')
            with patch.object(module, name, fail):
                old.start()
                self.assertTrue(old.wait(5000))
        else:
            old.start()
            self.assertTrue(old.wait(5000))
        release, started = threading.Event(), threading.Event()
        self.releases.append(release)
        def blocked(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise RuntimeError('Test did not release current worker')
            return calculate(*args, **kwargs)
        # Replace ownership before queued callbacks arrive to exercise the
        # identity checks independently of the overlapping-start guard.
        with patch.object(module, name, blocked):
            current = worker_class(tuple(self.window.inventory), self.window.target(), {}, owner)
            owner.worker = current
            owner.status.setText('current task status')
            current.start()
            self.assertTrue(started.wait(2))
            self.flush_deletes()
            self.assertIs(owner.worker, current)
            self.assertTrue(current.isRunning())
            self.assertEqual(owner.status.text(), 'current task status')
            self.assertIsNone(owner.report)
            self.assertIsNone(owner.state.pending)
            release.set()
            self.assertTrue(current.wait(5000))
        owner.worker = None
        current.deleteLater()

    def test_old_route_result_progress_and_finished_cannot_replace_current_task(self):
        self.stale_callbacks(parallel=False, failure=False)

    def test_old_route_failure_and_finished_cannot_replace_current_task(self):
        self.stale_callbacks(parallel=False, failure=True)

    def test_old_parallel_result_progress_and_finished_cannot_replace_current_task(self):
        self.stale_callbacks(parallel=True, failure=False)

    def test_old_parallel_failure_and_finished_cannot_replace_current_task(self):
        self.stale_callbacks(parallel=True, failure=True)

    def workers_do_not_accumulate(self, owner, worker_class):
        for _ in range(25):
            owner.start_planning()
            self.wait_clean(owner)
            self.flush_deletes()
            self.assertIsNotNone(owner.state.pending)
            owner.reset_pending()
        self.assertEqual(owner.findChildren(worker_class), [])

    def test_route_workers_do_not_accumulate_under_workspace(self):
        self.workers_do_not_accumulate(self.workspace, guidance.RouteWorker)

    def test_parallel_workers_do_not_accumulate_under_workspace(self):
        self.workers_do_not_accumulate(self.parallel, parallel_guidance.ParallelWorker)

    def test_close_accepts_completed_worker_awaiting_queued_cleanup(self):
        for owner in (self.workspace, self.parallel):
            with self.subTest(workspace=type(owner).__name__):
                owner.start_planning()
                self.assertTrue(owner.worker.wait(5000))
                self.assertIsNotNone(owner.worker)
                self.window.dirty = False
                event = QCloseEvent()
                self.window.closeEvent(event)
                self.assertTrue(event.isAccepted())
                self.wait_clean(owner)
                owner.reset_pending()

    def test_close_requests_cancellation_and_waits_for_running_workers(self):
        for owner, module, name in ((self.workspace, guidance, 'plan_route'), (self.parallel, parallel_guidance, 'plan_parallel_process')):
            with self.subTest(workspace=type(owner).__name__):
                started = threading.Event()
                def until_cancel(inventory, target, **options):
                    started.set()
                    deadline = time.monotonic() + 3
                    while not options['cancel']() and time.monotonic() < deadline:
                        time.sleep(.002)
                    return (ParallelReport((), False, False, 1, 'cancelled') if owner is self.parallel else RouteReport((), (), False, False, 1, 'cancelled'))
                with patch.object(module, name, until_cancel):
                    owner.start_planning()
                    self.assertTrue(started.wait(2))
                    worker = owner.worker
                    self.assertTrue(worker.isRunning())
                    self.window.dirty = False
                    event = QCloseEvent()
                    self.window.closeEvent(event)
                    self.assertTrue(event.isAccepted())
                    self.assertFalse(worker.isRunning())
                    self.wait_clean(owner)

    def test_route_and_parallel_results_are_delivered_on_gui_thread(self):
        received = []
        for owner in (self.workspace, self.parallel):
            original = owner.accept_route
            def accept(*args, **kwargs):
                received.append(QThread.currentThread() == self.qt.thread())
                return original(*args, **kwargs)
            with patch.object(owner, 'accept_route', accept):
                owner.start_planning()
                self.wait_clean(owner)
            owner.reset_pending()
        self.assertEqual(received, [True, True])


if __name__ == '__main__':
    unittest.main()
