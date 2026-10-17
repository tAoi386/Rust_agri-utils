"""Exercise six-gene bulk entry using Qt's real keyboard and paste actions."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_DEPS = PROJECT_ROOT / ".deps"
if LOCAL_DEPS.exists():
    sys.path.insert(0, str(LOCAL_DEPS))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:
    QT_AVAILABLE = False
else:
    QT_AVAILABLE = True
    from rust_breeder.widgets import GeneBatchEdit


@unittest.skipUnless(QT_AVAILABLE, "Install PySide6 to run Qt keyboard checks.")
class BulkGeneNewlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.editor = GeneBatchEdit()
        self.editor.show()
        self.editor.setFocus()
        self.qt.processEvents()

    def tearDown(self):
        self.editor.close()
        self.editor.deleteLater()
        self.qt.processEvents()

    def test_sixth_gene_moves_cursor_to_next_line(self):
        QTest.keyClicks(self.editor, "YYGYY")
        self.assertEqual(self.editor.toPlainText(), "YYGYY")
        QTest.keyClicks(self.editor, "H")
        self.assertEqual(self.editor.toPlainText(), "YYGYYH\n")
        self.assertEqual(self.editor.textCursor().blockNumber(), 1)
        self.assertEqual(self.editor.textCursor().positionInBlock(), 0)

    def test_continuous_typing_creates_one_line_per_seed(self):
        QTest.keyClicks(self.editor, "YYGYYHYYYGGG")
        self.assertEqual(self.editor.toPlainText(), "YYGYYH\nYYYGGG\n")
        self.assertEqual(self.editor.textCursor().blockNumber(), 2)

    def test_lowercase_genes_also_advance_after_six_characters(self):
        QTest.keyClicks(self.editor, "yygyyh")
        self.assertEqual(self.editor.toPlainText().upper(), "YYGYYH\n")

    def test_disabled_mode_allows_quantity_and_label_on_same_line(self):
        self.editor.set_auto_newline(False)
        QTest.keyClicks(self.editor, "YYGYYH 3 mother")
        self.assertEqual(self.editor.toPlainText(), "YYGYYH 3 mother")

    def test_metadata_line_is_not_split_by_sixth_gene_letter(self):
        self.editor.setPlainText("YYGYY 3 mother")
        cursor = self.editor.textCursor()
        cursor.setPosition(5)
        self.editor.setTextCursor(cursor)
        QTest.keyClicks(self.editor, "H")
        self.assertEqual(self.editor.toPlainText(), "YYGYYH 3 mother")

    def test_editing_inside_gene_line_does_not_insert_newline(self):
        self.editor.setPlainText("YYGYY")
        cursor = self.editor.textCursor()
        cursor.setPosition(2)
        self.editor.setTextCursor(cursor)
        QTest.keyClicks(self.editor, "H")
        self.assertEqual(self.editor.toPlainText(), "YYHGYY")

    def test_completing_existing_line_uses_next_line_without_blank_row(self):
        self.editor.setPlainText("YYGYY\nYYYGGG 2 saved")
        cursor = self.editor.textCursor()
        cursor.setPosition(5)
        self.editor.setTextCursor(cursor)
        QTest.keyClicks(self.editor, "H")
        self.assertEqual(self.editor.toPlainText(), "YYGYYH\nYYYGGG 2 saved")
        self.assertEqual(self.editor.textCursor().blockNumber(), 1)
        self.assertEqual(self.editor.textCursor().positionInBlock(), 0)

    def test_pasting_existing_multiline_inventory_preserves_metadata(self):
        source = "YYGYYH 3 母株\nYYYGGG 12 理想苗\nGYHWXY 2 备用"
        previous_clipboard = self.qt.clipboard().text()
        try:
            self.qt.clipboard().setText(source)
            QTest.keyClick(self.editor, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(self.editor.toPlainText(), source)
        finally:
            self.qt.clipboard().setText(previous_clipboard)

    def test_sixth_gene_and_automatic_newline_undo_together(self):
        QTest.keyClicks(self.editor, "YYGYY")
        QTest.keyClicks(self.editor, "H")
        self.assertEqual(self.editor.toPlainText(), "YYGYYH\n")
        QTest.keyClick(self.editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.editor.toPlainText(), "YYGYY")


if __name__ == "__main__":
    unittest.main()
