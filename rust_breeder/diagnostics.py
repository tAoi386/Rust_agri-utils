"""Local exception and native-crash records, installed before importing Qt."""
from __future__ import annotations

import faulthandler
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import tempfile
import threading
import traceback

from . import __version__


def _directories(base_dir):
    yield Path(base_dir) / 'data' / 'logs'
    try:
        yield Path(tempfile.gettempdir()) / 'RustSeedLab' / 'logs'
    except OSError:
        return


class RuntimeDiagnostics:
    def __init__(self, base_dir: Path):
        self.logger = logging.getLogger('rust_breeder')
        self.previous_level = self.logger.level
        self.previous_propagate = self.logger.propagate
        self.previous_exception_hook = sys.excepthook
        self.previous_thread_hook = threading.excepthook
        self.exception_hook = self._exception
        self.thread_hook = self._thread_exception
        self.handler = None
        self.native_file = None
        self.qt_previous = None
        self.qt_handler = None
        self.native_enabled = False
        self.directory = None
        for directory in _directories(base_dir):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(directory / 'runtime.log', maxBytes=1024 * 1024,
                                              backupCount=2, encoding='utf-8')
            except OSError:
                continue
            self.handler = handler
            self.directory = directory
            break
        if self.handler is None:
            return
        self.handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s [%(threadName)s] %(message)s'))
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        sys.excepthook = self.exception_hook
        threading.excepthook = self.thread_hook
        self._log(logging.INFO, 'Application start version=%s python=%s frozen=%s', __version__, sys.version.split()[0],
                  bool(getattr(sys, 'frozen', False)))
        try:
            path = self.directory / 'native-crash.log'
            if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
                path.replace(self.directory / 'native-crash.previous.log')
            self.native_file = path.open('a', encoding='utf-8', buffering=1)
            self.native_file.write(f'\n--- Application start {__version__} ---\n')
            self.native_file.flush()
            faulthandler.enable(file=self.native_file, all_threads=True)
            self.native_enabled = True
        except (OSError, RuntimeError, ValueError) as exc:
            self._log(logging.WARNING, 'Native crash recording unavailable: %s', exc)

    def _log(self, level, message, *args, **kwargs):
        try:
            self.logger.log(level, message, *args, **kwargs)
        except Exception:
            pass

    def _exception(self, kind, value, tb):
        self._log(logging.ERROR, 'Unhandled Python exception', exc_info=(kind, value, tb))
        if sys.stderr is not None:
            try:
                traceback.print_exception(kind, value, tb, file=sys.stderr)
            except Exception:
                pass

    def _thread_exception(self, args):
        self._log(logging.ERROR, 'Unhandled thread exception: %s', getattr(args.thread, 'name', 'unknown'),
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    def install_qt_handler(self):
        if self.handler is None or self.qt_handler is not None:
            return
        from PySide6 import __version__ as qt_version
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        def receive(kind, context, message):
            level = logging.ERROR if kind in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg) else logging.WARNING
            if kind in (QtMsgType.QtDebugMsg, QtMsgType.QtInfoMsg):
                level = logging.INFO
            self._log(level, 'Qt: %s', message)
            if kind == QtMsgType.QtFatalMsg and self.native_file is not None:
                try:
                    faulthandler.dump_traceback(file=self.native_file, all_threads=True)
                except (OSError, RuntimeError, ValueError):
                    pass
            if self.qt_previous is not None:
                try:
                    self.qt_previous(kind, context, message)
                except Exception:
                    pass

        self.qt_handler = receive
        self.qt_previous = qInstallMessageHandler(receive)
        self._log(logging.INFO, 'Qt initialized version=%s', qt_version)

    def record_exit(self, code):
        if self.handler is not None:
            self._log(logging.INFO, 'Application normal exit code=%s', code)
            try:
                self.handler.flush()
            except Exception:
                pass

    def close(self):
        """Restore hooks for isolated tests; normal startup keeps them until exit."""
        if self.qt_handler is not None:
            from PySide6.QtCore import qInstallMessageHandler
            qInstallMessageHandler(self.qt_previous)
            self.qt_handler = None
        if sys.excepthook is self.exception_hook:
            sys.excepthook = self.previous_exception_hook
        if threading.excepthook is self.thread_hook:
            threading.excepthook = self.previous_thread_hook
        if self.native_enabled:
            faulthandler.disable()
            self.native_enabled = False
        if self.native_file is not None:
            self.native_file.close()
        if self.handler is not None:
            self.logger.removeHandler(self.handler)
            self.handler.close()
            self.logger.setLevel(self.previous_level)
            self.logger.propagate = self.previous_propagate
