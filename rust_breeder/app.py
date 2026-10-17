"""Shared stock, local projects and the sequential/parallel desktop workspaces."""
from __future__ import annotations

from dataclasses import asdict
import logging
from pathlib import Path
import re
import shutil
import sys
import uuid

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QFont, QFontDatabase, QIcon, QPixmap, QPainter, QColor, QPen
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
    QFrame, QLineEdit, QComboBox, QDialog, QDialogButtonBox, QMessageBox,
    QInputDialog, QCheckBox, QListWidget, QLabel,
)

from . import __version__
from .models import InventoryItem, Target, normalize_genes
from .storage import load_project, save_project, validate_project, ProjectError
from .project_library import ProjectLibrary
from .capture_dialog import ScreenCaptureDialog
from .guidance import GuidedWorkspace
from .parallel_guidance import ParallelWorkspace
from .gui_runtime import install_gui_collector
from .i18n import (LANGUAGES, get_language, set_language, tr, install_localization,
                   localize_tree, language_manager, LocalizedComboBox, LocalizedTabWidget)
from .preferences import preferred_language, save_language
from .widgets import STYLE, SeedDialog, GeneBatchEdit, button, label
from PySide6.QtWidgets import QTabWidget

BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
AUTOSAVE = BASE_DIR / 'data' / 'autosave.json'
CROPS = [('hemp', '大麻 / Hemp'), ('blue', '蓝浆果'), ('red', '红浆果'), ('yellow', '黄浆果'),
         ('green', '绿浆果'), ('white', '白浆果'), ('black', '黑浆果'), ('corn', '玉米'),
         ('pumpkin', '南瓜'), ('potato', '土豆')]
LOGGER = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, restore=True):
        super().__init__()
        install_gui_collector()
        self.setWindowTitle(f'Rust 种养殖小工具 V{__version__}')
        self.resize(1280, 800)
        self.setMinimumSize(900, 600)
        self.inventory = []
        self.seed_numbers = {}
        self.slots, self.marked, self.step, self.observed_genes = [None] * 9, set(), 0, ''
        self.project_crop = 'hemp'
        self.project_path = None
        self.current_project_id = ''
        self.worker = None
        self.loading, self.dirty = True, False
        self._pages = []
        self._last_target = Target()
        self.library_error = ''
        self._restore_error = ''
        try:
            self.library = ProjectLibrary(AUTOSAVE.parent)
        except (OSError, ValueError) as exc:
            self.library = None
            self.library_error = str(exc)
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setSingleShot(True)
        self.autosave_timer.timeout.connect(self.autosave)
        self.make_ui()
        self.loading = False
        if restore:
            self.restore_local_work()
        self.refresh_inventory()
        self.update_locks()
        localize_tree(self)

    def make_ui(self):
        self.setStyleSheet(STYLE)
        # The two pages share these authoritative values; there is no hidden
        # legacy workbench or duplicate inventory-card tree.
        authority = QWidget(self)
        authority.hide()
        self.target_edit = QLineEdit('YYYGGG', authority)
        self.mode_combo = LocalizedComboBox(authority)
        self.mode_combo.addItem('按数量 · 顺序不限', 'counts')
        self.mode_combo.addItem('逐位 · 固定顺序', 'exact')
        self.crop_combo = LocalizedComboBox(authority)
        for key, title in CROPS:
            self.crop_combo.addItem(title, key)
        self.target_edit.textChanged.connect(self.target_changed)
        self.mode_combo.currentIndexChanged.connect(self.target_changed)
        self.crop_combo.currentIndexChanged.connect(self.crop_changed)
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(16, 8, 16, 2)
        self.project_name_label = label('软件内项目 · 默认项目', 'muted')
        header.addWidget(self.project_name_label)
        header.addStretch()
        self.language_combo = QComboBox()
        self.language_combo.setProperty('i18nSkip', True)
        self.language_combo.setAccessibleName('Language / Язык / 语言')
        for code, title in LANGUAGES:
            self.language_combo.addItem(title, code)
        self.language_combo.setCurrentIndex(self.language_combo.findData(get_language()))
        self.language_combo.currentIndexChanged.connect(self.language_changed)
        language_manager().changed.connect(self.sync_language_selection)
        header.addWidget(self.language_combo)
        self.new_button = button('新建项目', self.new_local_project, 'small')
        self.open_button = button('打开项目', self.open_local_project, 'small')
        self.save_button = button('立即保存', self.save_local_project, 'small')
        for control in (self.new_button, self.open_button, self.save_button):
            header.addWidget(control)
        outer.addLayout(header)
        self.workspaces = LocalizedTabWidget()
        self.guided = GuidedWorkspace(self)
        self._pages.append(self.guided)
        self.parallel = ParallelWorkspace(self)
        self._pages.append(self.parallel)
        for title, page in (('顺序培育', self.guided), ('多线培育', self.parallel)):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(page)
            self.workspaces.addTab(scroll, title)
        self.workspaces.currentChanged.connect(self.mode_changed)
        outer.addWidget(self.workspaces, 1)
        self.setCentralWidget(root)
        self.save_status = label('自动保存到软件内', 'muted')
        self.statusBar().addWidget(self.save_status, 1)
        self.author_label = QLabel('作者 i386tAo · <a href="mailto:2027883286@qq.com">2027883286@qq.com</a>')
        self.author_label.setOpenExternalLinks(True)
        self.statusBar().addPermanentWidget(self.author_label)
        self.open_action = QAction('打开软件内项目', self)
        self.open_action.setShortcut('Ctrl+O')
        self.open_action.triggered.connect(self.open_local_project)
        self.new_action = QAction('新建软件内项目', self)
        self.new_action.setShortcut('Ctrl+N')
        self.new_action.triggered.connect(self.new_local_project)
        self.save_action = QAction('立即保存', self)
        self.save_action.setShortcut('Ctrl+S')
        self.save_action.triggered.connect(self.save_local_project)
        self.addActions([self.open_action, self.new_action, self.save_action])

    def language_changed(self, index):
        code = self.language_combo.itemData(index)
        if not code:
            return
        set_language(code)
        localize_tree(self)
        try:
            save_language(AUTOSAVE.parent / 'settings.json', code)
        except OSError as exc:
            LOGGER.warning('Could not save the language preference: %s', exc)
            self.statusBar().showMessage(tr('语言已切换，但偏好保存失败。'), 5000)

    def sync_language_selection(self, code):
        self.language_combo.blockSignals(True)
        self.language_combo.setCurrentIndex(self.language_combo.findData(code))
        self.language_combo.blockSignals(False)

    def work_busy(self, exclude=None):
        return any(page is not exclude and getattr(page, 'worker', None) is not None for page in self._pages)

    def is_searching(self):
        return self.work_busy(exclude=getattr(self, 'guided', None))

    def can_plan(self, page):
        return not self.work_busy(exclude=page) and not any(
            other is not page and other.state.pending is not None for other in self._pages)

    def can_edit_stock(self, exclude=None):
        return not self.work_busy() and not any(
            page is not exclude and page.state.pending is not None for page in self._pages)

    def update_locks(self):
        busy = self.work_busy()
        pending_pages = [page for page in self._pages if page.state.pending is not None]
        can_switch_project = not busy and not pending_pages
        for control in (self.new_button, self.open_button, self.new_action, self.open_action):
            control.setEnabled(can_switch_project)
        for index, page in enumerate(self._pages):
            self.workspaces.setTabEnabled(index, not self.work_busy(exclude=page)
                                           and not any(other is not page for other in pending_pages))
        self.crop_combo.setEnabled(not self.inventory and not busy and not pending_pages)

    def set_busy(self, busy):
        self.update_locks()

    def mode_changed(self, index):
        if not self.loading:
            self.changed()

    def by_id(self, item_id):
        return next((item for item in self.inventory if item.id == item_id), None)

    def seed_number(self, item_or_id):
        key = item_or_id.id if hasattr(item_or_id, 'id') else str(item_or_id)
        if key not in self.seed_numbers:
            self.seed_numbers[key] = max(self.seed_numbers.values(), default=0) + 1
        return self.seed_numbers[key]

    def target(self):
        return Target(self.target_edit.text(), self.mode_combo.currentData())

    def changed(self, invalidate=False):
        if self.loading:
            return
        self.dirty = True
        if invalidate:
            for page in self._pages:
                page.refresh_from_owner()
        self.update_locks()
        self.save_status.setText('正在自动保存…')
        self.autosave_timer.start(650)

    def target_changed(self, *_):
        if self.loading:
            return
        if not self.can_edit_stock():
            self.target_edit.blockSignals(True)
            self.mode_combo.blockSignals(True)
            self.target_edit.setText(self._last_target.genes)
            self.mode_combo.setCurrentIndex(self.mode_combo.findData(self._last_target.mode))
            self.target_edit.blockSignals(False)
            self.mode_combo.blockSignals(False)
            return
        try:
            self._last_target = self.target()
        except ValueError:
            pass
        self.changed(invalidate=True)

    def crop_changed(self, *_):
        if self.loading:
            return
        if self.inventory or not self.can_edit_stock():
            self.crop_combo.blockSignals(True)
            self.crop_combo.setCurrentIndex(self.crop_combo.findData(self.project_crop))
            self.crop_combo.blockSignals(False)
            return
        self.project_crop = self.crop_combo.currentData()
        self.changed(invalidate=True)

    def refresh_inventory(self):
        for item in self.inventory:
            self.seed_number(item)
        for page in self._pages:
            page.refresh_from_owner()
        self.update_locks()

    def refresh_board(self):
        self.update_locks()

    def reset_observation(self):
        self.slots, self.marked, self.step, self.observed_genes = [None] * 9, set(), 0, ''

    def snapshot(self):
        target = self.target()
        data = {'version': 1, 'inventory': [asdict(item) for item in self.inventory],
                'target': target.genes, 'target_mode': target.mode, 'crop': self.project_crop,
                'slots': [{'seed_id': value, 'remove': index in self.marked} if value else None
                          for index, value in enumerate(self.slots)],
                'step': self.step, 'observed_genes': self.observed_genes,
                'seed_numbers': dict(self.seed_numbers),
                'workspace': 'parallel' if self.workspaces.currentIndex() == 1 else 'sequential'}
        for page in self._pages:
            data.update(page.project_fields())
        if self.current_project_id:
            data['local_project_id'] = self.current_project_id
        return data

    def apply_project(self, data):
        # Validate the entire document before changing any live widgets or stock.
        data = validate_project(data)
        numbers = data.get('seed_numbers', {})
        rows = [InventoryItem(**{key: row[key] for key in ('id', 'genes', 'quantity', 'name')})
                for row in data['inventory']]
        target = Target(data['target'], data.get('target_mode', 'counts'))
        self.loading = True
        try:
            self.inventory, self.seed_numbers = rows, dict(numbers)
            self._last_target = target
            self.target_edit.setText(target.genes)
            self.mode_combo.setCurrentIndex(self.mode_combo.findData(target.mode))
            self.crop_combo.setCurrentIndex(max(0, self.crop_combo.findData(data.get('crop', 'hemp'))))
            self.project_crop = self.crop_combo.currentData()
            self.slots = [slot['seed_id'] if slot else None for slot in data.get('slots', [None] * 9)]
            self.marked = {i for i, slot in enumerate(data.get('slots', [None] * 9)) if slot and slot.get('remove')}
            self.step = data.get('step', 0)
            self.observed_genes = data.get('observed_genes', '')
            for item in self.inventory:
                self.seed_number(item)
            for page in self._pages:
                page.restore_project(data)
            mode = 1 if data.get('workspace') == 'parallel' else 0
            for index, page in enumerate(self._pages):
                if page.state.pending is not None:
                    mode = index
            self.workspaces.setCurrentIndex(mode)
        finally:
            self.loading = False
        self.refresh_inventory()

    def restore_local_work(self):
        try:
            if self.library is not None and self.library.current_id:
                project_id = self.library.current_id
                data = self.library.load(project_id)
                self.apply_project(data)
                self.current_project_id = project_id
            elif AUTOSAVE.exists():
                self.apply_project(load_project(AUTOSAVE))
                if self.library is not None:
                    self.current_project_id = self.library.create('默认项目 · ' + self.crop_combo.currentText(), self.snapshot())
            if self.library is None:
                raise ProjectError(self.library_error or '软件内项目列表不可用。')
            if self.library.entries and not self.current_project_id and getattr(self.library, 'recovery_messages', []):
                raise ProjectError('请选择要继续的项目；恢复的项目列表已保留。')
            self.library.import_legacy(sorted(BASE_DIR.glob('*.json')))
            self.update_project_name()
            if self.current_project_id:
                self.autosave()
            messages = getattr(self.library, 'recovery_messages', [])
            self.statusBar().showMessage(tr('已从备份恢复项目，原文件已保留。' if messages else
                                           '已恢复自动保存；项目在软件内管理。'), 8000)
        except (OSError, ValueError) as exc:
            self._restore_error = str(exc)
            self.autosave_timer.stop()
            self.statusBar().showMessage(tr(f'项目恢复未完成：{exc}'))
            self.save_status.setText('恢复异常，已暂停保存；请打开其他项目或新建项目')

    def preserve_failed_autosave(self):
        if self._restore_error and AUTOSAVE.is_file():
            backup = AUTOSAVE.with_name(f'autosave.recovery-{uuid.uuid4().hex}.json')
            shutil.copy2(AUTOSAVE, backup)

    def update_project_name(self):
        name = self.library.name(self.current_project_id) if self.library and self.current_project_id else '默认项目'
        self.project_name_label.setText('软件内项目 · ' + name)

    def autosave(self):
        if self._restore_error:
            self.save_status.setText('恢复异常，已暂停保存；请打开其他项目或新建项目')
            self.statusBar().showMessage(tr(f'项目恢复未完成：{self._restore_error}'))
            return False
        try:
            data = self.snapshot()
            AUTOSAVE.parent.mkdir(parents=True, exist_ok=True)
            if self.library is None:
                raise ProjectError(self.library_error or '软件内项目列表不可用。')
            if not self.current_project_id:
                self.current_project_id = self.library.create('默认项目 · ' + self.crop_combo.currentText(), data)
                data['local_project_id'] = self.current_project_id
            else:
                self.library.save(self.current_project_id, data)
            save_project(AUTOSAVE, data)
            self.dirty = False
            self.update_project_name()
            self.save_status.setText('已自动保存到软件内')
            return True
        except (OSError, ValueError) as exc:
            self.save_status.setText('尚未保存，请检查项目')
            self.statusBar().showMessage(tr(f'自动保存失败：{exc}'))
            return False

    def save_local_project(self):
        self.autosave_timer.stop()
        return self.autosave()

    def new_local_project(self):
        if self.work_busy() or any(page.state.pending is not None for page in self._pages):
            return
        name, accepted = QInputDialog.getText(self, '新建软件内项目', '项目名称', text='新的种植项目')
        if not accepted or not name.strip():
            return
        if self.library is None:
            try:
                self.library = ProjectLibrary(AUTOSAVE.parent, recover_empty=True)
            except (OSError, ValueError) as exc:
                self.show_notice('新建失败', str(exc))
                return
        if not self._restore_error and not self.autosave():
            return
        blank = {'version': 1, 'inventory': [], 'target': 'YYYGGG', 'target_mode': 'counts',
                 'crop': 'hemp', 'slots': [None] * 9, 'seed_numbers': {}}
        try:
            self.preserve_failed_autosave()
            project_id = self.library.create(name, blank)
            self.apply_project(blank)
            self.current_project_id = project_id
            self._restore_error = ''
            self.changed()
            self.autosave()
        except (OSError, ValueError) as exc:
            self.show_notice('新建失败', str(exc))

    def open_local_project(self):
        if self.work_busy() or any(page.state.pending is not None for page in self._pages):
            return
        if self.library is None:
            self.show_notice('软件内项目列表不可用。', self.library_error)
            return
        if not self._restore_error and not self.autosave():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('打开软件内项目')
        dialog.resize(440, 350)
        col = QVBoxLayout(dialog)
        col.addWidget(label('全部项目自动保存在软件内，无需选择文件夹。', 'muted', True))
        listing = QListWidget()
        rows = sorted(self.library.entries, key=lambda row: row.get('updated', ''), reverse=True)
        for row in rows:
            listing.addItem(row['name'] + '   ' + row.get('updated', '').replace('T', ' '))
        listing.setCurrentRow(0)
        col.addWidget(listing)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        listing.itemDoubleClicked.connect(lambda _: dialog.accept())
        col.addWidget(actions)
        try:
            if dialog.exec() and 0 <= listing.currentRow() < len(rows):
                project_id = rows[listing.currentRow()]['id']
                data = self.library.load(project_id)
                self.preserve_failed_autosave()
                self.apply_project(data)
                self.current_project_id = project_id
                self._restore_error = ''
                self.changed()
                self.autosave()
        except (OSError, ValueError) as exc:
            self.show_notice('打开失败', str(exc))
        finally:
            dialog.deleteLater()

    # Compatibility for callers of the old project actions, all now local.
    new_project = new_local_project
    open_project = open_local_project
    save_project_as = save_local_project

    def closeEvent(self, event):
        workers = [page.worker for page in self._pages if page.worker is not None]
        for worker in workers:
            worker.requestInterruption()
        for worker in workers:
            worker.wait(2000)
            if worker.isRunning():
                event.ignore()
                self.statusBar().showMessage(tr('正在停止后台计算，请稍后再关闭。'))
                return
        self.autosave_timer.stop()
        if self.dirty and not self.autosave():
            choice = QMessageBox.question(self, '尚有未保存内容', '自动保存未完成，仍要退出吗？')
            if choice != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


    def add_seed(self):
        if not self.can_edit_stock():
            return
        dialog = SeedDialog(self)
        try:
            if dialog.exec():
                name = dialog.name_edit.text().strip() or f"苗 {len(self.inventory) + 1:02d}"
                self.inventory.append(InventoryItem(uuid.uuid4().hex[:12], dialog.result_genes, dialog.quantity_edit.value(), name))
                self.refresh_inventory()
                self.changed(invalidate=True)
        finally:
            dialog.deleteLater()

    def capture_genes(self):
        if not self.can_edit_stock():
            return
        if self.is_searching():
            return
        dialog = ScreenCaptureDialog(tuple(self.inventory), self.crop_combo.currentText(), self)
        dialog.auto_start = True
        try:
            if dialog.exec() and dialog.import_plan is not None:
                self.inventory = list(dialog.import_plan.inventory)
                self.refresh_inventory()
                self.changed(invalidate=True)
                self.statusBar().showMessage(tr(
                    f"已从画面加入 {dialog.import_plan.added_count} 组新基因；重复基因已跳过，原有数量保持不变。"))
        finally:
            # Parented dialogs otherwise live until the main window closes,
            # retaining every review table, preview and screen-signal receiver.
            dialog.stop_capture(restore=False)
            dialog.hide()
            worker = dialog.worker
            if worker is not None:
                # An exceptional exit may leave OCR in flight. Keep its parent
                # alive until it finishes instead of destroying a running thread.
                worker.finished.connect(dialog.deleteLater)
            if worker is None or not worker.isRunning():
                dialog.deleteLater()

    def edit_seed(self, item_id):
        if not self.can_edit_stock():
            return
        old = self.by_id(item_id)
        if old is None:
            return
        dialog = SeedDialog(self, old)
        try:
            if dialog.exec():
                used = self.slots.count(item_id)
                if dialog.quantity_edit.value() < used:
                    self.show_notice("数量不足", f"箱子已安排 {used} 株，请先清空部分格子再减少数量。")
                    return
                replacement = InventoryItem(item_id, dialog.result_genes, dialog.quantity_edit.value(), dialog.name_edit.text().strip() or old.name)
                self.inventory[self.inventory.index(old)] = replacement
                self.reset_observation()
                self.refresh_inventory()
                self.refresh_board()
                self.changed(invalidate=True)
        finally:
            dialog.deleteLater()

    def delete_seed(self, item_id):
        if not self.can_edit_stock():
            return
        self.inventory = [item for item in self.inventory if item.id != item_id]
        self.slots = [None if value == item_id else value for value in self.slots]
        self.marked = {i for i in self.marked if self.slots[i]}
        self.reset_observation()
        self.refresh_inventory()
        self.refresh_board()
        self.changed(invalidate=True)

    def bulk_add(self):
        if not self.can_edit_stock():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("批量录入克隆苗")
        dialog.resize(500, 400)
        col = QVBoxLayout(dialog)
        col.addWidget(label("每行一组：六位基因 [数量] [名称]", "sectionTitle"))
        col.addWidget(label("连续输入：每满 6 位基因自动换行，数量默认 1。", "muted"))
        auto_newline = QCheckBox("满 6 位自动换行（填写数量、名称时可关闭）")
        auto_newline.setChecked(True)
        col.addWidget(auto_newline)
        text = GeneBatchEdit()
        auto_newline.toggled.connect(text.set_auto_newline)
        text.setPlaceholderText("GYYGGG 1 A01\nYHYGGG 2 A02\nYGGGGG 1 A03")
        col.addWidget(text)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        col.addWidget(actions)
        actions.rejected.connect(dialog.reject)
        def accept():
            try:
                pending = []
                for number, line in enumerate(text.toPlainText().splitlines(), 1):
                    if not line.strip():
                        continue
                    match = re.fullmatch(r"\s*([GYHWXgyhwx]{6})(?:[\s,，;；]+(\d+))?(?:[\s,，;；]+(.+?))?\s*", line)
                    if not match:
                        raise ValueError(f"第 {number} 行格式不正确，请写六位基因、数量和名称。")
                    genes, qty, name = match.groups()
                    quantity = int(qty or 1)
                    if not 1 <= quantity <= 9999:
                        raise ValueError(f"第 {number} 行数量应为 1–9999。")
                    pending.append(InventoryItem(uuid.uuid4().hex[:12], genes, quantity, name or f"苗 {len(self.inventory)+len(pending)+1:02d}"))
                if not pending:
                    raise ValueError("请先填写至少一行。")
                self.inventory.extend(pending)
                dialog.accept()
            except ValueError as exc:
                QMessageBox.warning(dialog, "请检查录入内容", str(exc))
        actions.accepted.connect(accept)
        try:
            if dialog.exec():
                self.refresh_inventory()
                self.changed(invalidate=True)
        finally:
            dialog.deleteLater()

    def show_notice(self, title, text):
        QMessageBox.information(self, tr(title), tr(text))


def make_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#2e6343"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
    painter.setPen(QPen(QColor("#e3efcf"), 4))
    painter.drawLine(32, 47, 32, 22)
    painter.setBrush(QColor("#cfe0af"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(16, 19, 17, 12)
    painter.drawEllipse(32, 12, 17, 13)
    painter.end()
    return QIcon(pixmap)


def run():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setPalette(app.style().standardPalette())
    app.setApplicationName('RustGrowTool')
    app.setOrganizationName('Tao')
    install_localization(app)
    if '--smoke-test' not in sys.argv:
        set_language(preferred_language(AUTOSAVE.parent / 'settings.json'))
    if not QFontDatabase.families():
        for filename in ('msyh.ttc', 'msyhbd.ttc', 'segoeui.ttf', 'consola.ttf'):
            path = Path('C:/Windows/Fonts') / filename
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))
    app.setFont(QFont('Microsoft YaHei UI', 9))
    app.setWindowIcon(make_icon())
    smoke = '--smoke-test' in sys.argv
    window = MainWindow(restore=not smoke)
    if app.primaryScreen():
        available = app.primaryScreen().availableGeometry()
        window.resize(min(1280, max(900, available.width() - 60)),
                      min(800, max(600, available.height() - 80)))
    window.show()
    if smoke:
        window.autosave_timer.blockSignals(True)
        from .smoke_checks import check_package
        QTimer.singleShot(100, lambda: check_package(window, app, BASE_DIR))
    return app.exec()
