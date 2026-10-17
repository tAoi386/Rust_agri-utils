"""Transient search feedback must never interrupt the planting workflow."""

import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".deps"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QPushButton, QWidget
    from rust_breeder.notifications import ActionNotice
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, "Qt is needed for notification checks.")
class NotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_feedback_lasts_five_seconds_without_disabling_the_action(self):
        parent = QWidget()
        parent.resize(600, 300)
        button = QPushButton("自动选苗", parent)
        button.setGeometry(150, 120, 300, 36)
        notice = ActionNotice(button, parent)
        parent.show()
        notice.show_message("暂未找到方案", "可补充样本或增加规划轮数")
        self.assertTrue(notice.isVisible())
        self.assertEqual(notice.timer.interval(), 5000)
        self.assertTrue(notice.timer.isSingleShot())
        self.assertTrue(notice.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        self.assertTrue(button.isEnabled())
        self.assertTrue(parent.rect().contains(notice.geometry()))
        QTest.qWait(4000)
        self.assertTrue(notice.isVisible())
        QTest.qWait(1350)
        self.assertFalse(notice.isVisible())
        notice.show_message("样本不足", "请先录入可用克隆")
        self.assertTrue(notice.isVisible())
        notice.dismiss()
        self.assertFalse(notice.timer.isActive())
        parent.close()
        parent.deleteLater()
