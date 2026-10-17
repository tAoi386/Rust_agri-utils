"""Collect Python cycles on the Qt GUI thread that owns our widgets.

Python can otherwise start automatic cyclic collection in a calculation worker.
PySide wrappers and connected callbacks in those cycles must be finalized on the
GUI thread. Reference counting stays enabled; this only schedules cyclic GC.
"""

from __future__ import annotations

import gc
import logging

from PySide6.QtCore import QObject, QThread, QTimer, Slot
from PySide6.QtWidgets import QApplication


LOGGER = logging.getLogger(__name__)
COLLECTION_INTERVAL_MS = 1500
_APP_MANAGER = '_rust_breeder_gui_collector'


class GuiGarbageCollector(QObject):
    def __init__(self, app):
        super().__init__(app)
        self._collecting = False
        self.active = False
        self.previously_enabled = False
        self.collections = 0
        self.objects_collected = 0
        self.last_collection_thread = None
        self.timer = QTimer(self)
        self.timer.setInterval(COLLECTION_INTERVAL_MS)
        self.timer.timeout.connect(self.collect)
        app.aboutToQuit.connect(self.restore)
        self.start()

    def start(self):
        if self.active:
            return
        self.previously_enabled = gc.isenabled()
        gc.disable()
        self.active = True
        self.timer.start()
        LOGGER.info('GUI-thread cyclic garbage collection enabled')

    @Slot()
    def collect(self):
        if (not self.active or self._collecting or
                QThread.currentThread() != self.thread()):
            return
        self._collecting = True
        try:
            self.last_collection_thread = QThread.currentThread()
            self.objects_collected += gc.collect()
            self.collections += 1
        finally:
            self._collecting = False

    @Slot()
    def restore(self):
        if not self.active:
            return
        self.timer.stop()
        self.active = False
        if self.previously_enabled:
            gc.enable()
        LOGGER.info('GUI garbage collector stopped: collections=%d objects=%d',
                    self.collections, self.objects_collected)


def install_gui_collector():
    app = QApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        raise RuntimeError('Install GUI garbage collection on the QApplication thread.')
    manager = getattr(app, _APP_MANAGER, None)
    if manager is None:
        manager = GuiGarbageCollector(app)
        setattr(app, _APP_MANAGER, manager)
    else:
        manager.start()
    return manager
