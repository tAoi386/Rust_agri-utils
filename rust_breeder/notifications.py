"""A brief, non-modal notice anchored to the action that produced it."""

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout
from .i18n import LocalizedLabel as QLabel


class ActionNotice(QFrame):
    def __init__(self, anchor, parent):
        super().__init__(parent)
        self.anchor = anchor
        self.setObjectName("actionNotice")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet("""
            QFrame#actionNotice { background:#293337; border:1px solid #465257;
                                  border-radius:7px; }
            QFrame#actionNotice QLabel { background:transparent; border:0; color:#ffffff; }
            QLabel#noticeTitle { font-size:21px; font-weight:600; }
            QLabel#noticeDetail { font-size:12px; color:#d7e0e2; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(5)
        self.title = QLabel()
        self.title.setObjectName("noticeTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail = QLabel()
        self.detail.setObjectName("noticeDetail")
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title)
        layout.addWidget(self.detail)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.hide)
        parent.installEventFilter(self)
        anchor.installEventFilter(self)
        self.hide()

    def show_message(self, title, detail=""):
        self.title.setText(title)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self.timer.start(5000)

    def dismiss(self):
        self.timer.stop()
        self.hide()

    def reposition(self):
        parent = self.parentWidget()
        origin = self.anchor.mapTo(parent, QPoint(0, 0))
        width = min(max(300, self.sizeHint().width()), max(1, parent.width() - 24))
        height = self.sizeHint().height()
        x = max(12, min(origin.x() + (self.anchor.width() - width) // 2,
                        parent.width() - width - 12))
        y = max(8, min(origin.y() + (self.anchor.height() - height) // 2,
                       parent.height() - height - 8))
        self.setGeometry(x, y, width, height)

    def eventFilter(self, watched, event):
        if self.isVisible() and event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self.reposition()
        return super().eventFilter(watched, event)
