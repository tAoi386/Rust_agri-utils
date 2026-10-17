from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QWidget, QLabel, QHBoxLayout, QVBoxLayout,
    QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QSpinBox,
    QMessageBox, QPlainTextEdit,
)
from .i18n import (LocalizedLabel, LocalizedPushButton,
                   LocalizedComboBox as QComboBox, LocalizedLineEdit as QLineEdit)

GENE_COLORS = {"G": "#26845b", "Y": "#a67b1f", "H": "#398394", "W": "#bc6260", "X": "#bc6260"}
GENE_BACKGROUNDS = {"G": "#e0f1e8", "Y": "#faf0cd", "H": "#e1f0f3", "W": "#fae8e6", "X": "#fae8e6"}
GENE_NAMES = {"G": "生长", "Y": "产量", "H": "耐性", "W": "耗水", "X": "空基因"}


def label(text, name="", wrap=False):
    widget = LocalizedLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text, callback=None, name="secondary"):
    widget = LocalizedPushButton(text)
    widget.setObjectName(name)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if callback:
        widget.clicked.connect(callback)
    return widget


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout():
            clear_layout(item.layout())
            item.layout().deleteLater()


class GeneBatchEdit(QPlainTextEdit):
    """Advance after six typed genes without rewriting pasted project rows."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.auto_newline = True

    def set_auto_newline(self, enabled):
        self.auto_newline = bool(enabled)

    def keyPressEvent(self, event):
        modifiers = event.modifiers()
        typed_gene = (len(event.text()) == 1 and event.text().upper() in GENE_NAMES
                      and not modifiers & (Qt.KeyboardModifier.ControlModifier
                                           | Qt.KeyboardModifier.AltModifier
                                           | Qt.KeyboardModifier.MetaModifier))
        if not self.auto_newline or not typed_gene:
            super().keyPressEvent(event)
            return
        edit = self.textCursor()
        edit.beginEditBlock()
        try:
            super().keyPressEvent(event)
            cursor = self.textCursor()
            row = cursor.block().text()
            if cursor.atBlockEnd() and len(row) == 6 and all(gene.upper() in GENE_NAMES for gene in row):
                if cursor.block().next().isValid():
                    cursor.movePosition(QTextCursor.MoveOperation.NextBlock)
                    cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                else:
                    cursor.insertBlock()
                self.setTextCursor(cursor)
        finally:
            edit.endEditBlock()


class GeneStrip(QWidget):
    def __init__(self, genes="", large=False, parent=None):
        super().__init__(parent)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(5 if large else 3)
        self.large = large
        self.set_genes(genes)

    def set_genes(self, genes):
        clear_layout(self.row)
        for gene in genes:
            text = str(gene)
            token = QLabel(text)
            token.setAlignment(Qt.AlignmentFlag.AlignCenter)
            token.setFixedSize(40 if self.large else 26, 43 if self.large else 28)
            key = text[0] if len(text) == 1 else "?"
            token.setStyleSheet(
                f"background:{GENE_BACKGROUNDS.get(key, '#eef0ed')};"
                f"color:{GENE_COLORS.get(key, '#69776e')};border-radius:6px;"
                f"font-size:{19 if self.large else 14}px;font-weight:700;"
            )
            token.setToolTip(GENE_NAMES.get(key, "并列基因；查看逐位解释"))
            self.row.addWidget(token)
        self.row.addStretch()


class SeedDialog(QDialog):
    def __init__(self, parent=None, item=None):
        super().__init__(parent)
        self.setWindowTitle("编辑克隆苗" if item else "添加已知基因的克隆苗")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.addWidget(label("把游戏中看到的六位基因填在这里", "sectionTitle"))
        layout.addWidget(label("数量指现在可用的克隆苗；普通种子请先种下读取。", "muted", True))
        form = QFormLayout()
        self.name_edit = QLineEdit(item.name if item else "")
        self.name_edit.setMaxLength(200)
        self.name_edit.setPlaceholderText("例如：A01 / 产量苗")
        self.genes_edit = QLineEdit(item.genes if item else "")
        self.genes_edit.setPlaceholderText("例如 GYYGGG")
        self.genes_edit.setMaxLength(40)
        self.quantity_edit = QSpinBox()
        self.quantity_edit.setRange(1, 9999)
        self.quantity_edit.setValue(item.quantity if item else 1)
        form.addRow("苗的名称", self.name_edit)
        form.addRow("六位基因", self.genes_edit)
        form.addRow("可用数量", self.quantity_edit)
        layout.addLayout(form)
        self.preview = GeneStrip("GGYYYY")
        self.genes_edit.textChanged.connect(self.update_preview)
        layout.addWidget(self.preview)
        layout.addWidget(label("G 生长  ·  Y 产量  ·  H 耐性  ·  W 耗水  ·  X 空基因", "muted", True))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.update_preview(self.genes_edit.text())

    def update_preview(self, value):
        self.preview.set_genes("".join(c for c in value.upper() if c in GENE_NAMES)[:6])

    def accept(self):
        from .models import normalize_genes
        try:
            self.result_genes = normalize_genes(self.genes_edit.text())
        except ValueError as exc:
            QMessageBox.warning(self, "基因还没填对", str(exc))
            return
        super().accept()


STYLE = """
* { font-family: 'Microsoft YaHei UI', 'Microsoft YaHei', 'Segoe UI'; font-size: 12px; color: #25292e; }
QMainWindow, QDialog { background: #f5f6f7; }
QTabWidget { background: transparent; }
QTabWidget::pane { border: 0; background: transparent; }
QTabBar { background: #f5f6f7; qproperty-drawBase: false; }
QTabBar::tab { background: #edf0f2; color: #4c5964; border: 0; border-bottom: 2px solid transparent; padding: 9px 16px; }
QTabBar::tab:selected { background: #f5f6f7; color: #2d6c50; border-bottom: 2px solid #39785a; }
QTabBar::tab:hover:!selected { background: #e4ebe7; color: #344c40; }
QTabBar::tab:disabled { background: #f0f2f4; color: #a2aab1; }
QLabel { background: transparent; }
QToolTip { background: #25292e; color: white; padding: 7px; border: 0; }
QFrame#sidebar { background: #edf0f2; border: 0; }
QLabel#brand { color: #25292e; font-size: 22px; font-weight: 700; }
QLabel#navCaption { color: #777f88; font-size: 10px; }
QPushButton#nav { background: #e4e8eb; color: #333b43; border: none; border-radius: 4px; padding: 12px 7px; }
QPushButton#nav:hover { background: #d9dfe3; }
QLabel#eyebrow { color: #747c84; font-size: 10px; font-weight: 600; }
QLabel#pageTitle { font-size: 20px; font-weight: 600; color: #20252a; }
QLabel#subtitle, QLabel#muted { color: #737b84; font-size: 11px; }
QLabel#sectionTitle { font-size: 13px; font-weight: 600; }
QLabel#statValue { font-size: 22px; font-weight: 600; color: #39785a; }
QLabel#badge { background: #edf0f2; color: #59636c; border-radius: 4px; padding: 4px 8px; font-size: 10px; }
QFrame#panel { background: white; border: 1px solid #dfe3e7; border-radius: 5px; }
QFrame#inset { background: #f8f9fa; border: 1px solid #e3e6e9; border-radius: 4px; }
QFrame#inset[occupied=true] { background: #ffffff; border-color: #ccd3d9; }
QFrame#inset[central=true] { border-color: #b9c6bc; }
QFrame#inset[central=true][occupied=true] { background: #eff6f1; border: 2px solid #7a9f86; }
QFrame#divider { background: #e7eaed; border: 0; }
QFrame#tip { background: #f3f6f4; border: 1px solid #dfe6e1; border-radius: 4px; }
QLabel#tipText { color: #59646c; font-size: 11px; }
QLabel#boardGene { font-family: 'Consolas', 'Microsoft YaHei UI'; font-size: 17px; font-weight: 600; color: #2b333b; }
QLabel#boardGene:disabled { font-size: 14px; font-weight: 400; color: #9aa2aa; }
QLabel#prediction { background: #f4f6f7; border: 1px solid #e2e6e9; border-radius: 4px; padding: 8px; font-size: 12px; font-weight: 600; }
QPushButton { border: 1px solid #d5dbe0; border-radius: 4px; padding: 6px 10px; background: white; }
QPushButton:hover { background: #f2f4f5; border-color: #a9b3bb; }
QPushButton:pressed { background: #e8ecef; }
QPushButton:disabled { color: #a5acb2; background: #f4f5f6; border-color: #e5e8eb; }
QPushButton#primary { background: #39785a; border: 1px solid #39785a; color: white; font-weight: 600; padding: 7px 11px; }
QPushButton#primary:hover { background: #30674d; border-color: #30674d; }
QPushButton#primary:disabled { color: #a0a8a3; background: #eaf0ec; border-color: #e2e9e4; }
QPushButton#small { padding: 4px 7px; font-size: 11px; }
QPushButton#textButton { padding: 2px 0; border: 0; color: #59646e; background: transparent; text-align: left; font-size: 11px; }
QPushButton#textButton:hover { color: #2c6549; }
QPushButton#danger { color: #ae6660; padding: 4px 7px; border: none; }
QPushButton#recipe { text-align: left; padding: 9px; font-size: 11px; }
QPushButton#recipe:checked { background: #eff6f1; border: 1px solid #7c9f88; }
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit { background: white; border: 1px solid #d5dbe0; border-radius: 4px; padding: 5px 7px; selection-background-color: #719e83; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus { border-color: #648c74; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { background: #f7f8f9; color: #8e969d; border-color: #e3e7ea; }
QLineEdit#geneInput { font-family: 'Consolas', 'Microsoft YaHei UI'; font-size: 14px; }
QComboBox::drop-down { border: 0; width: 20px; }
QComboBox QAbstractItemView { background: white; selection-background-color: #e8efe9; selection-color: #25292e; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { width: 7px; background: transparent; margin: 0; }
QScrollBar::handle:vertical { background: #cbd2d7; border-radius: 3px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QFrame#planterCell { background: #f8f9fa; border: 1px solid #dfe4e8; border-radius: 4px; }
QFrame#planterCell:hover { border-color: #98b09f; background: #f3f7f4; }
QFrame#planterCell[target=true] { background: #eff6f1; border: 2px solid #7c9f87; }
QFrame#planterCell[removed=true] { background: #fcf2ef; border: 1px dashed #cb8b7b; }
QLabel#cellNumber { color: #8b959d; font-size: 11px; font-weight: 500; }
QLabel#cellRole { color: #828d94; font-size: 9px; }
QLabel#cellTitle { color: #58656d; font-size: 11px; font-weight: 600; }
QLabel#cellGenes { font-family: 'Consolas'; color: #4e6455; font-size: 13px; font-weight: 600; }
QLabel#cellCross { color: #bd6c5c; font-size: 11px; font-weight: 600; }
QPushButton#cellRemove { border: 0; color: #a69b8b; padding: 0; font-size: 17px; background: transparent; }
QPushButton#cellRemove:hover { color: #bc5e4e; background: #f6dfd5; }
QLabel#stepNumber { background: #edf1ee; border-radius: 4px; color: #617b69; font-size: 12px; font-weight: 600; }
QLabel#stepNumberActive { background: #39785a; border-radius: 4px; color: white; font-size: 12px; font-weight: 600; }
QLabel#stepTitle { font-weight: 600; font-size: 12px; }
QLabel#stepBody { font-size: 11px; color: #737d84; }
QLabel#statusGood { color: #39754b; background: #eff6f1; border-radius: 4px; padding: 7px; }
QLabel#statusWarn { color: #986b38; background: #fff6e9; border-radius: 4px; padding: 7px; }
QProgressBar { border: 0; background: #e8ece9; border-radius: 2px; height: 4px; }
QProgressBar::chunk { background: #71947b; border-radius: 2px; }
QStatusBar { background: #f5f6f7; color: #777f87; font-size: 10px; }
QTextBrowser { background: white; border: none; padding: 8px; }
QTableWidget { background: white; alternate-background-color: #fafbfc; border: 1px solid #e1e5e9; gridline-color: #edf0f2; selection-background-color: #e7efe9; selection-color: #26372c; outline: 0; }
QTableWidget::item { padding: 3px 4px; border-bottom: 1px solid #f0f2f4; }
QHeaderView::section { background: #f5f7f8; color: #646f78; border: 0; border-bottom: 1px solid #e1e5e9; padding: 6px; font-size: 11px; font-weight: 500; }
"""
