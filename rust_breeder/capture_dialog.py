"""Automatic local screen recognition with an optional bounded region."""

from __future__ import annotations

from math import ceil, floor

from PySide6.QtCore import QByteArray, QBuffer, QEventLoop, QIODevice, QPoint, QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .capture_import import CaptureSession, plan_capture_import
from .models import normalize_genes
from .screen_ocr import recognize_gene_image
from .widgets import STYLE, button, label
from .i18n import (LocalizedComboBox as QComboBox,
                   LocalizedTableWidget as QTableWidget, set_translatable_item, tr)


CAPTURE_INTERVAL_MS = 120


def central_detail_region(screen_rect: QRect) -> QRect:
    """Screen-local logical bounds for Rust's central item detail panel."""
    width, height = screen_rect.width(), screen_rect.height()
    left, top = floor(width * 0.35), floor(height * 0.25)
    right, bottom = ceil(width * 0.63), ceil(height * 0.58)
    return QRect(left, top, right - left, bottom - top).intersected(QRect(0, 0, width, height))


def image_png(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if image.isNull() or not image.save(buffer, "PNG"):
        raise ValueError("画面为空，无法读取。请切回游戏后重试，或选择有效截图。")
    return bytes(data)


def mask_application_windows(image: QImage, source_rect: QRect) -> QImage:
    """Exclude our visible overlays using actual pixels per logical source unit."""
    if image.isNull() or source_rect.isEmpty():
        return image
    result = image.copy()
    ratio = result.devicePixelRatio()
    result.setDevicePixelRatio(1.0)
    scale_x = result.width() / source_rect.width()
    scale_y = result.height() / source_rect.height()
    painter = QPainter(result)
    for window in QApplication.topLevelWindows():
        if not window.isVisible() or window.windowState() == Qt.WindowState.WindowMinimized:
            continue
        overlap = window.frameGeometry().intersected(source_rect)
        if overlap.isEmpty():
            continue
        left = floor((overlap.x() - source_rect.x()) * scale_x)
        top = floor((overlap.y() - source_rect.y()) * scale_y)
        right = ceil((overlap.x() - source_rect.x() + overlap.width()) * scale_x)
        bottom = ceil((overlap.y() - source_rect.y() + overlap.height()) * scale_y)
        rect = QRect(left, top, right - left, bottom - top).intersected(result.rect())
        painter.fillRect(rect, QColor("#202020"))
    painter.end()
    result.setDevicePixelRatio(ratio)
    return result


class OcrWorker(QThread):
    result_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, image: QImage | bytes, parent=None, *, colored_only=False):
        super().__init__(parent)
        self.image = QImage(image) if isinstance(image, QImage) else None
        self.png = image if isinstance(image, bytes) else None
        self.colored_only = colored_only

    def run(self):
        try:
            png = self.png if self.png is not None else image_png(self.image)
            result = (recognize_gene_image(png, colored_only=True) if self.colored_only else
                      recognize_gene_image(png))
            self.result_ready.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class RegionSelector(QDialog):
    """Select in logical screen coordinates; QScreen handles device scaling."""

    def __init__(self, screen):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.screen = screen
        self.background = screen.grabWindow(0)
        self.selection = QRect()
        self.origin = QPoint()
        self.dragging = False
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self.background)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 130))
        if not self.selection.isEmpty():
            painter.save()
            painter.setClipRect(self.selection)
            painter.drawPixmap(self.rect(), self.background)
            painter.restore()
            painter.setPen(QPen(QColor("#9ee8b8"), 2))
            painter.drawRect(self.selection)
        painter.setPen(QColor("white"))
        painter.drawText(QRect(22, 20, self.width() - 44, 85), Qt.TextFlag.TextWordWrap,
                         "拖动框住完整六位基因，尽量避开其他文字。\n松开鼠标确认 · Esc 取消")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.position().toPoint()
            self.dragging = True
            self.selection = QRect(self.origin, self.origin)
            self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            self.reject()

    def mouseMoveEvent(self, event):
        if self.dragging:
            self.selection = QRect(self.origin, event.position().toPoint()).normalized().intersected(self.rect())
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.dragging:
            self.dragging = False
            self.selection = QRect(self.origin, event.position().toPoint()).normalized().intersected(self.rect())
            if self.selection.width() >= 30 and self.selection.height() >= 12:
                self.accept()
            else:
                self.selection = QRect()
                self.update()


class CaptureControl(QDialog):
    ended = Signal()

    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setStyleSheet(STYLE)
        self.setWindowTitle("苗圃 · 正在自动读取")
        col = QVBoxLayout(self)
        col.setContentsMargins(12, 10, 12, 10)
        self.status = label("已收录 0 组\n等待基因画面…", "stepTitle", True)
        self.status.setMinimumHeight(60)
        col.addWidget(self.status)
        self.finish_button = button("结束读取并展开", lambda: self.ended.emit(), "primary")
        col.addWidget(self.finish_button)
        self.setFixedSize(350, 150)

    def reject(self):
        self.ended.emit()

    def closeEvent(self, event):
        event.ignore()
        self.ended.emit()


class ScreenCaptureDialog(QDialog):
    def __init__(self, inventory, crop_label, parent=None):
        super().__init__(parent)
        self.setWindowTitle("自动读取基因 · 一键加入种子库")
        self.setStyleSheet(STYLE)
        self.resize(820, 690)
        self.setMinimumSize(740, 650)
        self.inventory = tuple(inventory)
        self.existing_genes = {item.genes for item in inventory}
        self.session = CaptureSession()
        self.import_plan = None
        self.worker = None
        self.auto_start = False
        self._exec_active = False
        self.running = False
        self.finishing_capture = False
        self.generation = 0
        self.previous_frame = set()
        self.cached_frame = None
        self.cached_genes = ()
        self.cached_generation = -1
        self.cached_sampled = False
        self.last_confirmed = ""
        self.region = QRect()
        self.capture_screen = None
        self.capture_signature = None
        self.selecting = False
        self.close_pending = False
        self.collapsed = False
        self.owner_was_visible = False
        self.dialog_was_visible = False
        self.control = CaptureControl(self)
        self.control.ended.connect(self.finish_capture)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.capture_frame)
        col = QVBoxLayout(self)
        col.setSpacing(12)
        col.addWidget(label("自动读取基因", "sectionTitle"))
        col.addWidget(label(f"当前作物：{crop_label}。自动寻找屏幕上的六位彩色基因，无需手动框选。", "muted", True))
        col.addWidget(label("① 开始自动读取   ② 选中或悬停种子   ③ 结束后核对、去重入库", "stepTitle", True))
        region_row = QHBoxLayout()
        self.source_mode = QComboBox()
        self.source_mode.addItem("中间物品详情 · 快速读取", "detail")
        self.source_mode.addItem("自动全屏定位", "auto")
        self.source_mode.addItem("高级 · 手动限定范围", "region")
        region_row.addWidget(self.source_mode)
        self.screen_combo = QComboBox()
        self.screens = list(QApplication.screens())
        for i, screen in enumerate(self.screens, 1):
            size = screen.geometry().size()
            self.screen_combo.addItem(f"屏幕 {i} · {size.width()} × {size.height()}")
        cursor_screen = QApplication.screenAt(QCursor.pos())
        if cursor_screen in self.screens:
            self.screen_combo.setCurrentIndex(self.screens.index(cursor_screen))
        if self.screens:
            self.capture_screen = self.screens[max(0, self.screen_combo.currentIndex())]
        region_row.addWidget(self.screen_combo)
        self.select_button = button("框选限定范围", self.select_region)
        self.select_button.hide()
        self.start_button = button("开始自动读取", self.toggle_capture, "primary")
        self.start_button.setEnabled(self.capture_screen is not None)
        region_row.addWidget(self.select_button)
        region_row.addWidget(self.start_button)
        col.addLayout(region_row)
        self.region_label = label("默认快速读取中间物品详情。选中箱内或背包种子，保持详情显示；位置不同时可切换「自动全屏定位」。", "muted", True)
        col.addWidget(self.region_label)
        self.preview = label("区域预览 / 截图预览", "muted")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedHeight(75)
        col.addWidget(self.preview)
        source_row = QHBoxLayout()
        self.file_button = button("导入截图", self.import_image)
        self.paste_button = button("粘贴截图", self.paste_image)
        self.clear_button = button("清空待录入", self.clear_candidates, "small")
        source_row.addWidget(self.file_button)
        source_row.addWidget(self.paste_button)
        source_row.addStretch()
        source_row.addWidget(self.clear_button)
        col.addLayout(source_row)
        self.status = label("只在本机识别。选中或悬停种子，等浮窗显示「已确认」再切换；需要连续两次读到相同基因。", "muted", True)
        col.addWidget(self.status)
        self.table = QTableWidget(0, 4)
        self.table.setProperty('i18nColumns', [3])
        self.table.setMinimumHeight(130)
        self.table.setHorizontalHeaderLabels(["录入", "六位基因 · 可修改", "识别次数", "去重状态"])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(self.update_summary)
        col.addWidget(self.table, 1)
        self.summary = label("待录入 0 组", "stepTitle", True)
        col.addWidget(self.summary)
        qty_row = QHBoxLayout()
        qty_row.addWidget(label("每组实际可用克隆数"))
        self.quantity = QSpinBox()
        self.quantity.setRange(1, 9999)
        self.quantity.setValue(1)
        qty_row.addWidget(self.quantity)
        qty_row.addStretch()
        col.addLayout(qty_row)
        col.addWidget(label("数量默认 1，请按实际库存填写。同基因反复出现只记一组；库内已有基因会跳过，需要增加数量可返回克隆库编辑。", "muted", True))
        actions = QHBoxLayout()
        actions.addWidget(button("关闭", self.reject))
        actions.addStretch()
        self.import_button = button("一键加入种子库", self.commit_import, "primary")
        self.import_button.setEnabled(False)
        actions.addWidget(self.import_button)
        col.addLayout(actions)
        self.source_mode.currentIndexChanged.connect(self.source_mode_changed)
        self.screen_combo.currentIndexChanged.connect(self.screen_choice_changed)
        for screen in self.screens:
            for name in ("geometryChanged", "logicalDotsPerInchChanged", "physicalDotsPerInchChanged"):
                signal = getattr(screen, name, None)
                if signal is not None:
                    signal.connect(self.screen_metrics_changed)
        QApplication.instance().screenRemoved.connect(self.screen_removed)

    def set_status(self, message, floating=None):
        self.status.setText(message)
        if floating is not None:
            recent = f"\n最近确认 {self.last_confirmed}" if self.last_confirmed and not floating.startswith("已确认") else ""
            self.control.status.setText(f"已收录 {len(self.session.candidates)} 组\n{floating}{recent}")

    def automatic_source(self):
        return self.source_mode.currentData() in ("detail", "auto")

    def capture_region(self, screen):
        if self.source_mode.currentData() == "detail":
            return central_detail_region(screen.geometry())
        if self.source_mode.currentData() == "auto":
            return QRect(QPoint(), screen.geometry().size())
        return QRect(self.region)

    def clear_frame_cache(self):
        self.cached_frame = None
        self.cached_genes = ()
        self.cached_generation = -1
        self.cached_sampled = False

    def update_source_hint(self):
        mode = self.source_mode.currentData()
        self.region_label.setText(
            "默认快速读取中间物品详情。选中箱内或背包种子，保持详情显示；位置不同时可切换「自动全屏定位」。" if mode == "detail" else
            "自动定位整个所选屏幕，无需手动框选。请在游戏或网页里选中或悬停要读取的种子。" if mode == "auto" else
            "高级限定范围：请在所选屏幕框住完整六位基因。")

    def selected_screen(self):
        index = self.screen_combo.currentIndex()
        if 0 <= index < len(self.screens) and self.screens[index] in QApplication.screens():
            return self.screens[index]
        return None

    def source_mode_changed(self, *_):
        self.stop_capture()
        self.region = QRect()
        self.capture_signature = None
        self.capture_screen = self.selected_screen() if self.automatic_source() else None
        self.select_button.setVisible(not self.automatic_source())
        self.update_source_hint()
        self.preview.clear()
        self.preview.setText("自动定位预览" if self.automatic_source() else "限定范围预览")
        self.set_source_busy(self.worker is not None)

    @staticmethod
    def screen_signature(screen):
        geometry = screen.geometry()
        ratio = getattr(screen, "devicePixelRatio", lambda: 1.0)()
        dpi = getattr(screen, "logicalDotsPerInch", lambda: 96.0)()
        return (geometry.x(), geometry.y(), geometry.width(), geometry.height(),
                round(ratio, 4), round(dpi, 4))

    def invalidate_region(self, message):
        self.stop_capture()
        self.region = QRect()
        self.capture_screen = None
        self.capture_signature = None
        self.start_button.setEnabled(False)
        self.region_label.setText(message)
        self.preview.clear()
        self.preview.setText("请选择可用屏幕后开始自动读取" if self.automatic_source() else "请重新框选当前屏幕上的六位基因")
        self.set_status(message, "读取屏幕不可用，请重新选择" if self.automatic_source() else "选区已失效，请重新框选")

    def screen_choice_changed(self, *_):
        if self.automatic_source():
            self.stop_capture()
            self.capture_screen = self.selected_screen()
            self.capture_signature = None
            self.region = QRect()
            self.update_source_hint()
            self.set_status("已切换读取屏幕，可以直接开始自动读取。")
            self.set_source_busy(self.worker is not None)
            return
        if self.capture_screen is not None or not self.region.isEmpty():
            self.invalidate_region("已切换屏幕，请在新屏幕重新框选基因区域。")

    def screen_metrics_changed(self, *_):
        screen = self.sender()
        if self.capture_screen is not None and (screen is None or screen is self.capture_screen):
            if self.automatic_source():
                self.adapt_automatic_screen()
                return
            self.invalidate_region("屏幕位置、分辨率或缩放已改变，请重新框选基因区域。")

    def adapt_automatic_screen(self):
        self.generation += 1
        self.previous_frame.clear()
        self.clear_frame_cache()
        self.last_confirmed = ""
        self.capture_signature = self.screen_signature(self.capture_screen)
        self.set_status("屏幕显示已变化，已自动适配；请保持当前种子详情。", "屏幕已自动适配，等待基因画面")
        if self.running and self.worker is None:
            self.timer.start(CAPTURE_INTERVAL_MS)
        self.set_source_busy(self.worker is not None)

    def screen_removed(self, screen):
        if screen is self.capture_screen:
            self.invalidate_region("所选屏幕已断开，请改选可用屏幕或重新打开自动读取。")

    def exec(self):
        # QDialog.exec() ends when the dialog is hidden. Screen selection and
        # compact recording deliberately hide it, so finish only on done().
        loop = QEventLoop(self)
        self.finished.connect(loop.quit)
        self.setModal(True)
        self.show()
        self._exec_active = True
        if self.auto_start:
            QTimer.singleShot(0, self.start_after_show)
        try:
            loop.exec()
            return self.result()
        finally:
            self._exec_active = False
            self.auto_start = False
            self.finished.disconnect(loop.quit)
            loop.deleteLater()

    def start_after_show(self):
        if self._exec_active and self.auto_start and self.isVisible():
            self.auto_start = False
            self.toggle_capture()

    def set_preview(self, image):
        self.preview.setPixmap(QPixmap.fromImage(image).scaled(
            700, 70, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def select_region(self):
        if self.selecting or self.worker is not None:
            return
        if self.automatic_source():
            self.source_mode.setCurrentIndex(self.source_mode.findData("region"))
        index = self.screen_combo.currentIndex()
        if not 0 <= index < len(self.screens) or self.screens[index] not in QApplication.screens():
            self.status.setText("所选屏幕已断开，请关闭后重新打开自动读取。")
            return
        self.stop_capture()
        self.selecting = True
        owner = self.parentWidget()
        owner_visible = owner is not None and owner.isVisible()
        self.hide()
        if owner_visible:
            owner.hide()
        # Let Windows remove our windows before taking the selection backdrop.
        QTimer.singleShot(350, lambda: self.finish_selection(self.screens[index], owner, owner_visible))

    def finish_selection(self, screen, owner, owner_visible):
        try:
            if screen not in QApplication.screens():
                self.status.setText("屏幕已断开，请重新选择。")
                return
            selector = RegionSelector(screen)
            if selector.exec():
                self.capture_screen = screen
                self.region = QRect(selector.selection)
                self.capture_signature = self.screen_signature(screen)
                self.start_button.setEnabled(True)
                self.region_label.setText(f"{self.screen_combo.currentText()} · 已框选 {self.region.width()} × {self.region.height()} 区域；移动基因面板后需重新框选。")
                self.set_preview(screen.grabWindow(0, self.region.x(), self.region.y(),
                                                  self.region.width(), self.region.height()).toImage())
        finally:
            self.selecting = False
            if owner_visible:
                owner.show()
            self.show()
            self.raise_()
            self.activateWindow()

    def toggle_capture(self):
        if self.running:
            self.finish_capture()
            return
        if self.worker is not None:
            self.set_status("正在处理最后一帧，请稍候再开始识别。")
            return
        if self.automatic_source():
            self.capture_screen = self.selected_screen()
        if self.capture_screen not in QApplication.screens() or (not self.automatic_source() and self.region.isEmpty()):
            self.start_button.setEnabled(False)
            if self.automatic_source():
                self.set_status("所选屏幕不可用，请选择可用屏幕后开始自动读取。")
                return
            self.status.setText("请先框选当前屏幕上的基因区域。")
            return
        signature = self.screen_signature(self.capture_screen)
        if not self.automatic_source() and self.capture_signature is not None and self.capture_signature != signature:
            self.invalidate_region("屏幕位置、分辨率或缩放已改变，请重新框选基因区域。")
            return
        self.capture_signature = signature
        self.running = True
        self.finishing_capture = False
        self.generation += 1
        self.previous_frame.clear()
        self.clear_frame_cache()
        self.last_confirmed = ""
        self.start_button.setText("结束读取并展开")
        self.screen_combo.setEnabled(False)
        self.select_button.setEnabled(False)
        self.file_button.setEnabled(False)
        self.paste_button.setEnabled(False)
        self.set_source_busy(False)
        self.set_status("正在快速读取中间物品详情。选中箱内或背包种子，保持详情显示，等浮窗显示「已确认」后再切换。" if self.source_mode.currentData() == "detail" else
                        "正在自动读取。选中或悬停种子，等浮窗显示「已确认」再切换；结束后展开核对入库。", "等待种子的六位彩色基因…")
        self.collapse_for_capture()
        self.timer.start(CAPTURE_INTERVAL_MS)

    def collapse_for_capture(self):
        owner = self.parentWidget()
        self.owner_was_visible = owner is not None and owner.isVisible()
        self.owner_window_state = owner.windowState() if owner is not None else Qt.WindowState.WindowNoState
        self.dialog_was_visible = self.isVisible()
        self.collapsed = True
        self.hide()
        if self.owner_was_visible:
            owner.hide()
        self.control.status.setText(f"已收录 {len(self.session.candidates)} 组\n等待基因画面…")
        screen = self.capture_screen
        if screen in QApplication.screens():
            capture_rect = self.capture_region(screen).translated(screen.geometry().topLeft())
            available = screen.availableGeometry()
            width, height = self.control.width(), self.control.height()
            corners = [QPoint(available.right() - width - 12, available.top() + 12),
                       QPoint(available.right() - width - 12, available.bottom() - height - 12),
                       available.topLeft() + QPoint(12, 12),
                       QPoint(available.left() + 12, available.bottom() - height - 12)]
            position = next((point for point in corners
                             if not QRect(point, self.control.size()).intersects(capture_rect)), corners[0])
            self.control.move(position)
        self.control.show()

    def restore_after_capture(self):
        self.control.hide()
        if not self.collapsed:
            return
        self.collapsed = False
        owner = self.parentWidget()
        if self.owner_was_visible and owner is not None:
            owner.setWindowState(self.owner_window_state)
            owner.show()
        if self.dialog_was_visible:
            self.show()
            self.raise_()
            self.activateWindow()

    def finish_capture(self):
        self.finishing_capture = self.worker is not None
        self.stop_capture(discard_pending=False)
        self.set_status("已结束采集并展开，正在处理已截图的最后一帧；完成后可核对入库。" if self.finishing_capture else
                        "已结束识别并展开。核对六位基因、勾选条目后，即可一键加入种子库。")

    def stop_capture(self, *, restore=True, discard_pending=True):
        self.running = False
        if discard_pending:
            self.generation += 1
            self.finishing_capture = False
            self.clear_frame_cache()
            self.last_confirmed = ""
        self.timer.stop()
        if discard_pending or self.worker is None:
            self.previous_frame.clear()
        self.start_button.setText("开始自动读取")
        self.set_source_busy(self.worker is not None)
        if restore:
            self.restore_after_capture()
        else:
            self.control.hide()
            owner = self.parentWidget()
            if self.collapsed and self.owner_was_visible and owner is not None:
                owner.setWindowState(self.owner_window_state)
                owner.show()
            self.collapsed = False

    def set_source_busy(self, busy):
        self.source_mode.setEnabled(not busy and not self.running)
        self.screen_combo.setEnabled(not busy and not self.running)
        ready = (self.selected_screen() is not None if self.automatic_source() else
                 self.capture_screen is not None and not self.region.isEmpty())
        self.start_button.setEnabled(self.running or (not busy and ready))
        self.select_button.setEnabled(not busy and not self.running)
        self.file_button.setEnabled(not busy and not self.running)
        self.paste_button.setEnabled(not busy and not self.running)
        self.clear_button.setEnabled(not busy and not self.running)
        self.update_summary()

    def capture_frame(self):
        if not self.running:
            return
        if self.worker is not None:
            self.timer.start(CAPTURE_INTERVAL_MS)
            return
        screen = self.capture_screen
        if screen not in QApplication.screens():
            self.invalidate_region("所选屏幕已断开，请改选可用屏幕后重新开始。")
            return
        if self.automatic_source():
            if self.capture_signature is not None and self.capture_signature != self.screen_signature(screen):
                self.adapt_automatic_screen()
        else:
            if not QRect(QPoint(), screen.geometry().size()).contains(self.region):
                self.invalidate_region("屏幕或分辨率已改变，请重新框选基因区域。")
                return
            if self.capture_signature is not None and self.capture_signature != self.screen_signature(screen):
                self.invalidate_region("屏幕位置、分辨率或缩放已改变，请重新框选基因区域。")
                return
        local_region = self.capture_region(screen)
        global_region = local_region.translated(screen.geometry().topLeft())
        if not self.automatic_source() and QApplication.applicationState() == Qt.ApplicationState.ApplicationActive:
            overlaps = any(w is not self.control and w.isVisible() and not w.isMinimized() and w.frameGeometry().intersects(global_region)
                           for w in QApplication.topLevelWidgets())
            if overlaps:
                self.previous_frame.clear()
                self.set_status("识别等待中：工作台遮住了区域，请切回游戏 / 网页，或将此窗口移开。", "等待：请切回游戏 / 网页")
                self.timer.start(CAPTURE_INTERVAL_MS)
                return
        try:
            pixmap = (screen.grabWindow(0) if self.source_mode.currentData() == "auto" else
                      screen.grabWindow(0, local_region.x(), local_region.y(), local_region.width(), local_region.height()))
            image = mask_application_windows(pixmap.toImage(), global_region)
        except Exception as exc:
            self.stop_capture()
            detail = str(exc).strip() or "截图失败"
            self.set_status(f"无法读取当前画面：{detail}。请切回游戏后重试。", "画面读取失败，已停止")
            return
        if (self.cached_generation == self.generation and self.cached_frame is not None and
                image == self.cached_frame):
            # This image came from a new screen grab, not a duplicate callback
            # from the first OCR. Its pixels independently confirm the sample.
            if not self.cached_sampled:
                self.receive_result(self.cached_genes, live=True, token=self.generation)
                self.cached_sampled = True
            elif self.cached_genes:
                self.set_status("已确认当前基因，可切换其他种子。", f"已确认 {' / '.join(self.cached_genes[:2])}")
            if self.running:
                self.timer.start(CAPTURE_INTERVAL_MS)
            return
        self.clear_frame_cache()
        self.recognize_image(image, live=True)

    def import_image(self):
        if self.worker is not None or self.running:
            return
        path, _ = QFileDialog.getOpenFileName(self, tr("选择基因截图"), "", tr("图片 (*.png *.jpg *.jpeg *.bmp *.webp)"))
        if path:
            self.recognize_image(QImage(path))

    def paste_image(self):
        if self.worker is None and not self.running:
            self.recognize_image(QApplication.clipboard().image())

    def recognize_image(self, image, *, live=False):
        if self.worker is not None:
            return
        if image.isNull():
            if live:
                self.stop_capture()
            self.set_status("画面为空，无法读取。请切回游戏后重试，或选择有效截图。",
                            ("画面为空，请切回游戏后重试" if self.automatic_source() else "画面为空，请重新框选") if live else None)
            return
        self.set_preview(image)
        token = self.generation
        worker = OcrWorker(image, self, colored_only=live and self.automatic_source())
        self.worker = worker
        worker.result_ready.connect(lambda genes, frame=QImage(image): self.receive_ocr_result(
            genes, frame, live=live, token=token))
        worker.failed.connect(lambda error: self.receive_error(error, token))
        worker.finished.connect(self.worker_finished)
        self.set_source_busy(True)
        self.set_status("正在本机识别基因…" if not live else "正在处理当前画面；请保持当前种子详情，等浮窗显示已确认。",
                        "正在处理当前画面…" if live else None)
        worker.start()

    def receive_ocr_result(self, genes, image, *, live, token):
        if token != self.generation:
            return
        if live:
            self.cached_frame = QImage(image)
            self.cached_genes = tuple(genes)
            self.cached_generation = token
            self.cached_sampled = False
        self.receive_result(genes, live=live, token=token)

    def receive_result(self, genes, *, live=False, token=None):
        if token is not None and token != self.generation:
            return
        observed = tuple(dict.fromkeys(genes))
        if live:
            current = set(observed)
            stable = tuple(gene for gene in observed if gene in self.previous_frame)
            self.previous_frame = current
            genes = stable
        try:
            added = self.session.recognize_many(genes)
        except ValueError as exc:
            self.stop_capture()
            self.set_status(str(exc), "识别已停止，请核对列表")
            return
        self.sync_table()
        floating = None
        if live:
            shown = " / ".join((genes or observed)[:2])
            if genes:
                self.last_confirmed = shown
                floating = f"已确认 {shown}"
            elif observed:
                floating = f"首次读到 {shown}，等待再次确认"
            else:
                floating = "未识别到基因，请对准六个字母"
        if added:
            message = f"新识别 {added} 组基因，已自动去重。可继续识别其他植株。"
        elif genes:
            message = "重复基因已跳过，数量保持不变。可继续切换其他植株。"
        elif live and observed:
            message = f"首次读到 {' / '.join(observed[:2])}，请保持当前种子详情，等待再次确认。"
        else:
            message = ("未识别到完整六位基因。请确认当前种子详情位于读取范围内，并保持画面清晰。" if live else
                       "未识别到完整六位基因。请裁剪基因区域、放大画面后重新导入。")
        if self.finishing_capture:
            message = "最后一帧已处理。" + (message if genes else "该帧尚未通过连续两次确认，未自动加入列表；请核对已有条目。")
        self.set_status(message, floating)

    def receive_error(self, error, token):
        if token != self.generation:
            return
        self.stop_capture()
        self.set_status(error, "识别出错，已停止")

    def worker_finished(self):
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.deleteLater()
        if self.finishing_capture:
            self.finishing_capture = False
            self.previous_frame.clear()
        self.set_source_busy(False)
        if self.close_pending:
            self.close_pending = False
            self.reject()
        elif self.running:
            self.timer.start(CAPTURE_INTERVAL_MS)

    def sync_table(self):
        self.table.blockSignals(True)
        rows = {self.table.item(row, 0).data(Qt.ItemDataRole.UserRole): row for row in range(self.table.rowCount())}
        for candidate in self.session.candidates:
            row = rows.get(candidate.genes)
            if row is None:
                row = self.table.rowCount()
                self.table.insertRow(row)
                check = QTableWidgetItem()
                check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                check.setData(Qt.ItemDataRole.UserRole, candidate.genes)
                check.setCheckState(Qt.CheckState.Unchecked if candidate.genes in self.existing_genes else Qt.CheckState.Checked)
                self.table.setItem(row, 0, check)
                self.table.setItem(row, 1, QTableWidgetItem(candidate.genes))
                for column in (2, 3):
                    item = QTableWidgetItem()
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    self.table.setItem(row, column, item)
            self.table.item(row, 2).setText(str(candidate.occurrences))
        self.table.blockSignals(False)
        self.update_summary()

    def update_summary(self, *_):
        self.table.blockSignals(True)
        seen = set(self.existing_genes)
        new = existing = invalid = 0
        for row in range(self.table.rowCount()):
            try:
                genes = normalize_genes(self.table.item(row, 1).text())
                duplicate = genes in seen
                selected = self.table.item(row, 0).checkState() == Qt.CheckState.Checked
                state = "库内已有 · 跳过" if genes in self.existing_genes else "重复 · 跳过" if duplicate else "待入库" if selected else "未勾选"
                if selected:
                    existing += int(duplicate)
                    new += int(not duplicate)
                    seen.add(genes)
            except ValueError:
                state = "请修正为六位基因"
                invalid += int(self.table.item(row, 0).checkState() == Qt.CheckState.Checked)
            set_translatable_item(self.table.item(row, 3), state)
        self.table.blockSignals(False)
        self.summary.setText(f"已识别 {self.table.rowCount()} 组 · 待入库 {new} 组 · 勾选重复 {existing} 组" + (f" · 待修正 {invalid} 组" if invalid else ""))
        self.import_button.setEnabled(bool(new) and not invalid and not self.running and self.worker is None)

    def clear_candidates(self):
        if self.running or self.worker is not None:
            return
        self.session.clear()
        self.previous_frame.clear()
        self.clear_frame_cache()
        self.table.setRowCount(0)
        self.update_summary()
        self.status.setText("待录入列表已清空，种子库未改变。")

    def commit_import(self):
        if self.running or self.worker is not None:
            self.status.setText("请先暂停识别，等当前画面识别结束后再入库。")
            return
        selected = [self.table.item(row, 1).text() for row in range(self.table.rowCount())
                    if self.table.item(row, 0).checkState() == Qt.CheckState.Checked]
        try:
            result = plan_capture_import(self.inventory, selected, quantity=self.quantity.value())
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        if not result.items:
            self.status.setText("没有可新增的基因。请选择新基因；库内已有的会自动跳过。")
            return
        self.import_plan = result
        self.accept()

    def done(self, result):
        self.stop_capture(restore=False)
        if self.worker is not None:
            self.close_pending = True
            self.status.setText("正在结束本次本机识别，完成后自动关闭…")
            return
        super().done(result)
