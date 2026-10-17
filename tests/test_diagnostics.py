"""Runtime records stay useful when a windowed process has no console."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if (PROJECT_ROOT / ".deps").exists():
    sys.path.insert(0, str(PROJECT_ROOT / ".deps"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from rust_breeder.diagnostics import RuntimeDiagnostics

try:
    from PySide6.QtCore import QtMsgType, qWarning
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


class RuntimeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        artifacts = PROJECT_ROOT / "artifacts"
        artifacts.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="diagnostics-", dir=artifacts)
        self.root = Path(self.temporary.name)
        self.instances = []

    def tearDown(self):
        for diagnostics in reversed(self.instances):
            diagnostics.close()
        self.temporary.cleanup()

    def new_diagnostics(self, directory=None):
        diagnostics = RuntimeDiagnostics(directory or self.root)
        self.instances.append(diagnostics)
        return diagnostics

    def log_text(self, diagnostics):
        if diagnostics.handler is not None:
            diagnostics.handler.flush()
        return (diagnostics.directory / "runtime.log").read_text(encoding="utf-8")

    def test_python_and_background_exceptions_record_without_stderr(self):
        diagnostics = self.new_diagnostics()
        with patch.object(sys, "stderr", None):
            try:
                raise ValueError("ordinary-error-marker")
            except ValueError:
                sys.excepthook(*sys.exc_info())
            def fail_in_background():
                raise RuntimeError("background-error-marker")
            thread = threading.Thread(target=fail_in_background, name="diagnostic-probe")
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        text = self.log_text(diagnostics)
        self.assertIn("Unhandled Python exception", text)
        self.assertIn("ordinary-error-marker", text)
        self.assertIn("Unhandled thread exception: diagnostic-probe", text)
        self.assertIn("background-error-marker", text)

    @unittest.skipUnless(QT_AVAILABLE, "Install PySide6 for Qt logging checks.")
    def test_qt_warning_is_recorded_and_handler_is_installed_once(self):
        diagnostics = self.new_diagnostics()
        diagnostics.install_qt_handler()
        first_handler = diagnostics.qt_handler
        diagnostics.install_qt_handler()
        self.assertIs(diagnostics.qt_handler, first_handler)
        qWarning("qt-warning-marker")
        text = self.log_text(diagnostics)
        self.assertIn("Qt: qt-warning-marker", text)
        self.assertEqual(text.count("Qt: qt-warning-marker"), 1)

    def test_native_output_file_stays_open_until_explicit_close(self):
        diagnostics = self.new_diagnostics()
        self.assertIsNotNone(diagnostics.native_file)
        native_file = diagnostics.native_file
        self.assertFalse(native_file.closed)
        diagnostics.record_exit(0)
        self.assertFalse(native_file.closed)
        self.assertIn("Application normal exit code=0", self.log_text(diagnostics))
        diagnostics.close()
        self.assertTrue(native_file.closed)
        self.instances.remove(diagnostics)

    def test_unwritable_primary_directory_uses_temporary_fallback(self):
        base = self.root / "primary"
        fallback = self.root / "fallback"
        original_mkdir = Path.mkdir
        def mkdir(path, *args, **kwargs):
            if path == base / "data" / "logs":
                raise PermissionError("primary log directory blocked")
            return original_mkdir(path, *args, **kwargs)
        with patch.object(Path, "mkdir", mkdir), patch("rust_breeder.diagnostics.tempfile.gettempdir", return_value=str(fallback)):
            diagnostics = self.new_diagnostics(base)
        self.assertEqual(diagnostics.directory, fallback / "RustSeedLab" / "logs")
        self.assertIn("Application start", self.log_text(diagnostics))

    def test_all_logging_locations_unwritable_does_not_block_startup(self):
        old_exception_hook = sys.excepthook
        old_thread_hook = threading.excepthook
        with patch("rust_breeder.diagnostics.RotatingFileHandler", side_effect=PermissionError("logs blocked")), \
                patch("rust_breeder.diagnostics.tempfile.gettempdir", return_value=str(self.root / "fallback")):
            diagnostics = self.new_diagnostics()
        self.assertIsNone(diagnostics.handler)
        self.assertIsNone(diagnostics.directory)
        self.assertIs(sys.excepthook, old_exception_hook)
        self.assertIs(threading.excepthook, old_thread_hook)
        diagnostics.install_qt_handler()
        diagnostics.record_exit(0)

    def test_unavailable_temporary_directory_does_not_block_primary_logs(self):
        with patch("rust_breeder.diagnostics.tempfile.gettempdir", side_effect=FileNotFoundError("No usable temporary directory")):
            diagnostics = self.new_diagnostics()
        self.assertEqual(diagnostics.directory, self.root / "data" / "logs")
        self.assertIn("Application start", self.log_text(diagnostics))

    @unittest.skipUnless(QT_AVAILABLE, "Install PySide6 for Qt logging checks.")
    def test_logging_failures_do_not_escape_qt_or_exception_handlers(self):
        diagnostics = self.new_diagnostics()
        diagnostics.install_qt_handler()
        with patch.object(diagnostics.logger, "log", side_effect=OSError("log storage unavailable")), \
                patch.object(diagnostics.logger, "error", side_effect=OSError("log storage unavailable")), \
                patch.object(sys, "stderr", None):
            diagnostics.qt_handler(QtMsgType.QtWarningMsg, None, "warning during disk failure")
            diagnostics.exception_hook(ValueError, ValueError("ordinary failure"), None)
            args = type("ThreadError", (), {"thread": None, "exc_type": RuntimeError,
                                            "exc_value": RuntimeError("thread failure"), "exc_traceback": None})()
            diagnostics.thread_hook(args)

    def test_flush_failure_does_not_turn_normal_exit_into_an_error(self):
        diagnostics = self.new_diagnostics()
        with patch.object(diagnostics.handler, "flush", side_effect=OSError("disk full")), \
                patch.object(sys, "stderr", None):
            diagnostics.record_exit(0)

    def test_log_write_failure_during_construction_does_not_block_startup(self):
        with patch.object(logging.getLogger("rust_breeder"), "log", side_effect=OSError("disk unavailable")):
            diagnostics = self.new_diagnostics()
        self.assertIsNotNone(diagnostics.handler)
        self.assertIs(sys.excepthook, diagnostics.exception_hook)

    def test_unwritable_stderr_does_not_break_exception_hook(self):
        diagnostics = self.new_diagnostics()
        class ClosedConsole:
            def write(self, message):
                raise OSError("console unavailable")
            def flush(self):
                raise OSError("console unavailable")
        with patch.object(sys, "stderr", ClosedConsole()):
            diagnostics.exception_hook(ValueError, ValueError("console-error-marker"), None)
        self.assertIn("console-error-marker", self.log_text(diagnostics))


if __name__ == "__main__":
    unittest.main()
