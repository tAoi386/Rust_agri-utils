"""Inventory-safe, actual-result driven workspace for concurrent planters."""

from __future__ import annotations

from dataclasses import replace
import logging

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QComboBox,
    QLineEdit, QSpinBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QAbstractSpinBox, QProgressBar, QTabWidget,
    QScrollArea, QSizePolicy,
)

from .models import normalize_genes
from .notifications import ActionNotice
from .parallel_models import (
    ParallelState, prepare_wave, complete_wave, undo_wave, cancel_wave, current_wave,
)
from .parallel import validate_parallel_report
from .planner_process import plan_parallel_process
from .planter import positions_for
from .widgets import button, label
from .i18n import (LocalizedComboBox as QComboBox, LocalizedLineEdit as QLineEdit,
                   LocalizedTableWidget as QTableWidget, LocalizedTabWidget as QTabWidget)


LOGGER = logging.getLogger(__name__)


def _panel(layout):
    frame = QFrame()
    frame.setObjectName("panel")
    frame.setLayout(layout)
    layout.setContentsMargins(12, 11, 12, 11)
    layout.setSpacing(8)
    return frame


class ParallelWorker(QThread):
    result_ready = Signal(object)
    failed = Signal(str)
    progress_changed = Signal(int, int)

    def __init__(self, inventory, target, options, parent=None):
        super().__init__(parent)
        self.inventory = tuple(inventory)
        self.target = target
        self.options = dict(options)

    def run(self):
        try:
            report = plan_parallel_process(
                self.inventory, self.target, **self.options,
                cancel=self.isInterruptionRequested,
                progress=lambda count, wave: self.progress_changed.emit(count, wave),
            )
            self.result_ready.emit(report)
        except Exception as exc:
            self.failed.emit(str(exc))


class BoxPage(QWidget):
    """One planter's instructions and observations; no stock mutations."""

    def __init__(self, box_id, workspace):
        super().__init__(workspace)
        self.box_id = box_id
        self.workspace = workspace
        outer = QVBoxLayout(self)
        outer.setContentsMargins(9, 7, 9, 6)
        outer.setSpacing(5)
        self.purpose = label("等待一键智能安排。", "muted", True)
        self.purpose.setMinimumHeight(14)
        outer.addWidget(self.purpose)
        board = QGridLayout()
        board.setSpacing(6)
        self.cells = []
        for index in range(9):
            frame = QFrame()
            frame.setObjectName("inset")
            frame.setProperty("central", index == 4)
            frame.setProperty("occupied", False)
            frame.setMinimumSize(106, 68)
            frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            content = QVBoxLayout(frame)
            content.setContentsMargins(7, 6, 7, 6)
            content.setSpacing(3)
            number = label(f"{index + 1:02d}" + (" · 中央" if index == 4 else ""), "cellNumber")
            stock = label("", "cellNumber", True)
            stock.setAlignment(Qt.AlignmentFlag.AlignCenter)
            gene = label("留空", "boardGene", True)
            gene.setAlignment(Qt.AlignmentFlag.AlignCenter)
            detail = label("", "muted", True)
            detail.setProperty('i18nUserText', index != 4)
            detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
            content.addWidget(number)
            content.addStretch(1)
            content.addWidget(stock)
            content.addWidget(gene)
            content.addWidget(detail)
            content.addStretch(1)
            board.addWidget(frame, index // 3, index % 3)
            self.cells.append((frame, stock, gene, detail))
        outer.addLayout(board, 1)
        self.prediction = label("预测：等待规划", "prediction", True)
        outer.addWidget(self.prediction)
        actual = QHBoxLayout()
        actual.setSpacing(6)
        actual.addWidget(label("实测基因"))
        self.actual_edit = QLineEdit()
        self.actual_edit.setObjectName("geneInput")
        self.actual_edit.setPlaceholderText("完整六位基因")
        self.actual_edit.setMaxLength(32)
        self.actual_edit.textChanged.connect(workspace.draft_changed)
        actual.addWidget(self.actual_edit, 1)
        actual.addWidget(label("克隆"))
        self.actual_quantity = QSpinBox()
        self.actual_quantity.setRange(1, 9999)
        self.actual_quantity.setMaximumWidth(78)
        self.actual_quantity.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)
        self.actual_quantity.valueChanged.connect(workspace.draft_changed)
        actual.addWidget(self.actual_quantity)
        actual.addWidget(label("株"))
        outer.addLayout(actual)
        self.assessment = label("杂交完成后，填写该箱实际基因和取得的克隆数。", "muted", True)
        self.assessment.setMinimumHeight(14)
        outer.addWidget(self.assessment)

    def render(self, step, *, busy, main_box, usage):
        role = "主箱" if self.box_id == main_box else "副箱"
        self.setAccessibleName(f"{self.box_id} 号箱 · {role}")
        self.purpose.setText(usage if step else "本批不使用这个箱，保持空置。")
        items = [None] * 9
        if step:
            items[4] = step.recipe.center
            for position, item in zip(positions_for(step.recipe.donor_count), step.recipe.donors):
                items[position] = item
        for index, (frame, stock, gene, detail) in enumerate(self.cells):
            item = items[index]
            stock.setText(self.workspace.seed_label(item) if item else "")
            gene.setText(item.genes if item else "留空")
            if item and index == 4:
                detail.setText(f"预测 → {step.expected_genes}")
            elif item:
                detail.setText(item.label)
            else:
                detail.clear()
            occupied = item is not None
            if frame.property("occupied") != occupied:
                frame.setProperty("occupied", occupied)
                frame.style().unpolish(frame)
                frame.style().polish(frame)
        self.prediction.setText(
            f"中央预测：{step.recipe.prediction.result_pattern}  ·  本箱用苗 {step.recipe.plant_count} 株"
            if step else "本箱本批空置，不消耗用苗。")
        self.actual_edit.setEnabled(step is not None and not busy)
        self.actual_quantity.setEnabled(self.actual_edit.isEnabled())


class ParallelWorkspace(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.state = ParallelState()
        self.report = None
        self.worker = None
        self.syncing = False
        self._route_context = None
        self._inventory_signature = None
        self.box_pages = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 8)
        outer.setSpacing(9)

        setup = QHBoxLayout()
        setup.setSpacing(7)
        setup.addWidget(label("目标", "stepTitle"))
        self.target_edit = QLineEdit("YYYGGG")
        self.target_edit.setObjectName("geneInput")
        self.target_edit.setMaximumWidth(96)
        self.target_edit.textChanged.connect(self.change_target)
        setup.addWidget(self.target_edit)
        self.mode = QComboBox()
        self.mode.addItem("按数量 · 顺序不限", "counts")
        self.mode.addItem("逐位 · 固定顺序", "exact")
        self.mode.currentIndexChanged.connect(self.change_target)
        setup.addWidget(self.mode)
        setup.addSpacing(5)
        setup.addWidget(label("种植箱"))
        self.box_count = QSpinBox()
        self.box_count.setRange(2, 6)
        self.box_count.setValue(3)
        self.box_count.setMaximumWidth(62)
        self.box_count.valueChanged.connect(self.box_count_changed)
        setup.addWidget(self.box_count)
        setup.addWidget(label("主箱"))
        self.main_box = QComboBox()
        self.main_box.setMinimumWidth(104)
        self.main_box.setMaximumWidth(112)
        self.main_box.currentIndexChanged.connect(self.options_changed)
        setup.addWidget(self.main_box)
        self.objective = QComboBox()
        self.objective.addItem("优先杂交批数最少", "rounds")
        self.objective.addItem("优先总用苗最少", "plants")
        self.objective.addItem("智能匹配（基于已有基因库）", "stock")
        self.objective.setCurrentIndex(self.objective.findData("stock"))
        self.objective.currentIndexChanged.connect(self.options_changed)
        setup.addWidget(self.objective)
        setup.addStretch(1)
        self.summary = label("3 个箱共同规划，按批次确认实测。", "muted", True)
        setup.addWidget(self.summary, 1)
        setup_panel = _panel(setup)
        setup.setContentsMargins(12, 7, 12, 7)
        outer.addWidget(setup_panel)
        self.spacing_notice = label(
            "各箱需拉开距离：任何其他箱的植株都不能进入本箱中央株的杂交影响范围。"
            "各箱先种中央，接近杂交时再放供体；箱号不会改变基因权重。",
            "tipText", True)
        self.spacing_notice.setStyleSheet("background:#faf5e9; color:#765a28; padding:6px 10px; border-radius:4px;")
        outer.addWidget(self.spacing_notice)

        columns = QHBoxLayout()
        columns.setSpacing(10)
        inventory = QVBoxLayout()
        inventory_head = QHBoxLayout()
        inventory_head.addWidget(label("共享基因库", "sectionTitle"))
        inventory_head.addStretch(1)
        self.inventory_count = label("", "muted")
        inventory_head.addWidget(self.inventory_count)
        inventory.addLayout(inventory_head)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("搜索苗号 / 基因 / 名称")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self.filter_rows)
        inventory.addWidget(self.filter_edit)
        self.inventory_table = QTableWidget(0, 3)
        self.inventory_table.setProperty('i18nColumns', [0])
        self.inventory_table.setHorizontalHeaderLabels(["苗号 / 基因", "株数", "名称"])
        self.inventory_table.verticalHeader().hide()
        self.inventory_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.inventory_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.inventory_table.setAlternatingRowColors(True)
        self.inventory_table.setShowGrid(False)
        self.inventory_table.verticalHeader().setDefaultSectionSize(33)
        self.inventory_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.inventory_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.inventory_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        inventory.addWidget(self.inventory_table, 1)
        stock_actions = QHBoxLayout()
        self.add_button = button("添加", self.add_seed, "small")
        self.bulk_button = button("批量录入", self.bulk_add, "small")
        self.edit_button = button("编辑数量", self.edit_selected, "small")
        for control in (self.add_button, self.bulk_button, self.edit_button):
            stock_actions.addWidget(control)
        inventory.addLayout(stock_actions)
        self.capture_button = button("从游戏读取基因", self.capture_genes)
        inventory.addWidget(self.capture_button)
        inventory.addWidget(label("与顺序培育共用库存。确认整批完成时，才统一扣苗并加入实测克隆。", "muted", True))
        inventory_frame = _panel(inventory)
        inventory_frame.setMinimumWidth(238)
        inventory_frame.setMaximumWidth(275)
        columns.addWidget(inventory_frame)

        middle = QVBoxLayout()
        settings = QHBoxLayout()
        settings.setSpacing(24)
        batch_setting = QVBoxLayout()
        batch_setting.setSpacing(6)
        batch_setting.addWidget(label("最多批次", "muted", True))
        self.max_waves = QSpinBox()
        self.max_waves.setRange(1, 5)
        self.max_waves.setValue(3)
        self.max_waves.setFixedWidth(96)
        self.max_waves.valueChanged.connect(self.options_changed)
        batch_setting.addWidget(self.max_waves, 0, Qt.AlignmentFlag.AlignLeft)
        settings.addLayout(batch_setting, 1)
        clone_setting = QVBoxLayout()
        clone_setting.setSpacing(6)
        clone_setting.addWidget(label("每箱预留克隆", "muted", True))
        self.clone_yield = QSpinBox()
        self.clone_yield.setRange(1, 9)
        self.clone_yield.setValue(1)
        self.clone_yield.setFixedWidth(96)
        self.clone_yield.valueChanged.connect(self.options_changed)
        clone_setting.addWidget(self.clone_yield, 0, Qt.AlignmentFlag.AlignLeft)
        settings.addLayout(clone_setting, 1)
        middle.addLayout(settings)
        self.plan_button = button("一键智能 · 调配所有种植箱", self.start_planning, "primary")
        self.plan_button.setMinimumHeight(34)
        middle.addWidget(self.plan_button)
        self.action_notice = ActionNotice(self.plan_button, self)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(4)
        self.progress.setTextVisible(False)
        self.progress.hide()
        middle.addWidget(self.progress)
        self.status = label("主箱冲刺最终目标，副箱按完整路线培育所需供体。", "muted", True)
        self.status.setMaximumHeight(36)
        middle.addWidget(self.status)
        self.box_tabs = QTabWidget()
        self.box_tabs.setDocumentMode(True)
        middle.addWidget(self.box_tabs, 1)
        middle_frame = _panel(middle)
        middle_frame.setMinimumWidth(430)
        columns.addWidget(middle_frame, 2)

        right = QVBoxLayout()
        right.addWidget(label("当前批次", "sectionTitle"))
        self.wave_label = label("第 1 批 · 等待规划", "stepTitle", True)
        right.addWidget(self.wave_label)
        self.wave_summary = label("先设置箱数和主箱，再点击一键智能。", "muted", True)
        right.addWidget(self.wave_summary)
        self.checklist = label("每个有安排的箱都会显示预测和实测输入框。", "tipText", True)
        self.checklist.setMinimumHeight(70)
        right.addWidget(self.checklist)
        self.commit_button = button("所有箱核对完成后确认", self.complete_current_wave, "primary")
        self.commit_button.setMinimumHeight(36)
        right.addWidget(self.commit_button)
        right.addWidget(label("仅在本批所有箱实测都符合各自预测后，才可统一确认、进入下一批。", "muted", True))
        self.route_title = label("后续供苗关系", "sectionTitle")
        right.addWidget(self.route_title)
        self.route_scroll = QScrollArea()
        self.route_scroll.setWidgetResizable(True)
        self.route_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.route_scroll.setMinimumHeight(70)
        self.route_label = label("完整路线找到后，会显示哪箱产物供给后续哪箱。", "muted", True)
        self.route_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.route_scroll.setWidget(self.route_label)
        right.addWidget(self.route_scroll, 1)
        self.history_label = label("尚无已完成批次", "muted", True)
        self.history_label.setMaximumHeight(80)
        right.addWidget(self.history_label)
        self.undo_button = button("撤回最近一批，恢复用苗", self.undo_completed_wave, "secondary")
        right.addWidget(self.undo_button)
        self.reset_button = button("撤销本批安排（库存不变）", self.reset_pending, "small")
        right.addWidget(self.reset_button)
        right_frame = _panel(right)
        right_frame.setMinimumWidth(255)
        right_frame.setMaximumWidth(310)
        columns.addWidget(right_frame, 1)
        outer.addLayout(columns, 1)
        self._rebuild_boxes()
        self.refresh_from_owner()

    def is_busy(self):
        return self.worker is not None

    def _other_busy(self):
        callback = getattr(self.owner, "work_busy", None)
        return bool(callback(exclude=self)) if callback else bool(self.owner.is_searching())

    def _can_plan(self):
        callback = getattr(self.owner, "can_plan", None)
        return bool(callback(self)) if callback else not self._other_busy()

    def _can_edit_stock(self):
        callback = getattr(self.owner, "can_edit_stock", None)
        return bool(callback()) if callback else not self.is_busy() and self.state.pending is None

    def seed_label(self, item):
        if item is None:
            return ""
        callback = getattr(self.owner, "seed_number", None)
        if callback:
            return f"{callback(item):03d} 号苗"
        for index, row in enumerate(self.owner.inventory, 1):
            if row.id == item.id:
                return f"{index:03d} 号苗"
        return item.label

    def _rebuild_boxes(self):
        selected = self.box_tabs.currentIndex()
        previous = {key: (page.actual_edit.text(), page.actual_quantity.value())
                    for key, page in self.box_pages.items()}
        while self.box_tabs.count():
            widget = self.box_tabs.widget(0)
            self.box_tabs.removeTab(0)
            widget.deleteLater()
        self.box_pages = {}
        main = self.main_box.currentData() or 1
        for number in range(1, self.box_count.value() + 1):
            page = BoxPage(number, self)
            page.actual_quantity.setValue(self.clone_yield.value())
            if number in previous:
                page.actual_edit.setText(previous[number][0])
                page.actual_quantity.setValue(previous[number][1])
            self.box_pages[number] = page
            self.box_tabs.addTab(page, f"{number} 号 · " + ("主箱" if number == main else "副箱"))
        self.box_tabs.setCurrentIndex(max(0, min(selected, self.box_tabs.count() - 1)))
        old = self.main_box.currentData() or 1
        blocked = self.main_box.blockSignals(True)
        self.main_box.clear()
        for number in range(1, self.box_count.value() + 1):
            self.main_box.addItem(f"{number} 号箱", number)
        self.main_box.setCurrentIndex(max(0, self.main_box.findData(old)))
        self.main_box.blockSignals(blocked)

    def box_count_changed(self, *_):
        if self.syncing:
            return
        if self.state.pending is not None:
            return
        self.syncing = True
        self._rebuild_boxes()
        self.syncing = False
        self.options_changed()

    def options_changed(self, *_):
        if self.syncing or self.is_busy() or self.state.pending is not None:
            return
        self.report = None
        self.state = replace(self.state, box_count=self.box_count.value(), main_box=self.main_box.currentData() or 1)
        self.render_wave()
        self.owner.changed()

    def change_target(self, *_):
        if self.syncing or self.state.pending is not None or self.is_busy():
            return
        self.owner.target_edit.setText(self.target_edit.text())
        self.owner.mode_combo.setCurrentIndex(self.owner.mode_combo.findData(self.mode.currentData()))
        self.report = None

    def add_seed(self):
        if self._can_edit_stock():
            self.owner.add_seed()

    def bulk_add(self):
        if self._can_edit_stock():
            self.owner.bulk_add()

    def capture_genes(self):
        if self._can_edit_stock():
            self.owner.capture_genes()

    def edit_selected(self):
        if not self._can_edit_stock():
            return
        row = self.inventory_table.currentRow()
        if row >= 0:
            self.owner.edit_seed(self.inventory_table.item(row, 0).data(Qt.ItemDataRole.UserRole))

    def filter_rows(self):
        text = self.filter_edit.text().strip().upper()
        for row in range(self.inventory_table.rowCount()):
            self.inventory_table.setRowHidden(row, bool(text) and not any(
                text in self.inventory_table.item(row, col).text().upper() for col in (0, 2)))

    def refresh_from_owner(self):
        self.syncing = True
        try:
            target = self.owner.target()
            if self.state.target != target:
                had_pending = self.state.pending is not None
                self.state = replace(self.state, target=target, pending=None)
                self.report = None
                if had_pending:
                    self.status.setText("目标已变化，旧的多箱安排已撤销；请重新规划。")
        except ValueError:
            pass
        self.target_edit.setText(self.owner.target_edit.text())
        self.mode.setCurrentIndex(self.mode.findData(self.owner.mode_combo.currentData()))
        signature = tuple(self.owner.inventory)
        if signature != self._inventory_signature:
            self._inventory_signature = signature
            self.inventory_table.setRowCount(len(signature))
            for row, item in enumerate(signature):
                number = getattr(self.owner, 'seed_number', lambda _item: row + 1)(item)
                gene = QTableWidgetItem(f"#{number:03d}  {item.genes}")
                gene.setData(Qt.ItemDataRole.UserRole, item.id)
                self.inventory_table.setItem(row, 0, gene)
                self.inventory_table.setItem(row, 1, QTableWidgetItem(str(item.quantity)))
                self.inventory_table.setItem(row, 2, QTableWidgetItem(item.label))
            self.filter_rows()
        self.inventory_count.setText(f"{len(signature)} 组 · {sum(item.quantity for item in signature)} 株")
        self.syncing = False
        self.render_wave()

    def context(self):
        return (tuple(self.owner.inventory), self.owner.target(), self.owner.project_crop,
                self.box_count.value(), self.main_box.currentData() or 1,
                self.max_waves.value(), self.clone_yield.value(), self.objective.currentData())

    def start_planning(self):
        if self.is_busy():
            self.worker.requestInterruption()
            self.status.setText("正在停止多箱计算…")
            return
        if not self._can_plan() or self._other_busy():
            self.status.setText("请先完成或撤销另一种培育模式的安排。")
            return
        if self.state.pending is not None:
            self.status.setText("本批已有安排，请先记录所有箱实测，或撤销本批安排。")
            return
        self.action_notice.dismiss()
        try:
            context = self.context()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        if not context[0]:
            self.status.setText("先添加或从游戏读取已有克隆，再一键智能调配。")
            self.action_notice.show_message("样本不足", "请先录入可用克隆")
            return
        self.report = None
        self.state = replace(self.state, target=context[1], box_count=context[3], main_box=context[4])
        self._route_context = context
        options = dict(box_count=context[3], main_box=context[4], max_waves=context[5], clone_yield=context[6],
                       objective=context[7], max_combinations=120000, max_seconds=12)
        self.worker = ParallelWorker(context[0], context[1], options, self)
        self.worker.result_ready.connect(self.route_result_ready, Qt.ConnectionType.QueuedConnection)
        self.worker.failed.connect(self.route_failed, Qt.ConnectionType.QueuedConnection)
        self.worker.progress_changed.connect(self.route_progress, Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self.search_finished, Qt.ConnectionType.QueuedConnection)
        self.status.setText("正在全局核对用苗，搜索可同时开工的完整路线…")
        self.set_busy(True)
        self.worker.start()

    @Slot(object)
    def route_result_ready(self, report):
        if self.sender() is self.worker and self.worker is not None:
            self.accept_route(report, self._route_context)

    @Slot(str)
    def route_failed(self, error):
        if self.sender() is self.worker and self.worker is not None:
            LOGGER.warning("Parallel planning failed: %s", error)
            self.status.setText(f"多箱计算未完成：{error}")

    @Slot(int, int)
    def route_progress(self, count, wave):
        if self.sender() is self.worker and self.worker is not None:
            self.status.setText(f"已比较 {count:,} 组 · 正在看第 {wave} 批")

    def accept_route(self, report, context=None):
        try:
            if context is not None and context != self.context():
                self.status.setText("库存、目标或箱数已变化，旧方案未应用，请重新计算。")
                return
        except ValueError:
            return
        self.report = report
        self.status.setText(
            f"找到 {report.rounds} 批完整方案，累计用苗 {report.plant_count} 株；请逐箱核对摆法。"
            if report.found and report.waves else report.summary)
        self.status.setToolTip(report.summary)
        if report.found and report.waves:
            try:
                validate_parallel_report(report, tuple(self.owner.inventory), self.state.target)
                self.state = prepare_wave(self.state, tuple(self.owner.inventory), report.waves[0])
            except ValueError as exc:
                self.status.setText(f"方案未应用：{exc}")
                self.report = None
            self._clear_drafts()
        elif not report.found and not report.cancelled:
            self.action_notice.show_message("暂未找到完整多箱方案", "可补充样本或增加规划批数后重试")
        self.render_wave()
        self.owner.changed()

    @Slot()
    def search_finished(self):
        old = self.sender()
        if old is None:
            return
        old.deleteLater()
        if old is not self.worker:
            return
        self.worker = None
        self._route_context = None
        self.set_busy(False)

    def set_busy(self, busy):
        self.progress.setVisible(busy)
        self.plan_button.setText("停止计算" if busy else "一键智能 · 调配所有种植箱")
        self.owner.set_busy(busy)
        self.render_wave()

    def _current_steps(self):
        try:
            wave = current_wave(self.state, tuple(self.owner.inventory))
        except ValueError as exc:
            self.state = cancel_wave(self.state)
            self.report = None
            self.status.setText(f"库存与本批安排不符：{exc} 请核对库存后重新规划。")
            return ()
        return wave.steps if wave is not None else ()

    def _usage(self, step):
        if self.state.target.matches(step.expected_genes):
            return "本箱产物符合最终目标；完成后保存实测克隆。"
        uses = []
        if self.report is not None:
            for number, wave in enumerate(self.report.waves[1:], 2):
                for later in wave.steps:
                    roles = []
                    if later.recipe.center_id == step.output_id:
                        roles.append("中央株")
                    if step.output_id in later.recipe.donor_ids:
                        roles.append("供体")
                    if roles:
                        uses.append(f"第 {len(self.state.waves) + number} 批 · {later.box_id} 号箱" + "/".join(roles))
        return "产物供给：" + "、".join(uses) if uses else "本箱培育中间克隆，确认实测后按实际库存继续调配。"

    def render_wave(self):
        if not self.box_pages:
            return
        busy = self.is_busy() or self._other_busy()
        steps = self._current_steps()
        pending = self.state.pending is not None
        by_box = {step.box_id: step for step in steps}
        main = self.main_box.currentData() or 1
        for box_id, page in self.box_pages.items():
            self.box_tabs.setTabText(box_id - 1, f"{box_id} 号 · " + ("主箱" if box_id == main else "副箱"))
            page.render(by_box.get(box_id), busy=busy, main_box=main,
                        usage=self._usage(by_box[box_id]) if box_id in by_box else "")
        number = len(self.state.waves) + 1
        self.wave_label.setText(f"第 {number} 批 · " + (f"{len(steps)} 箱同时培育" if pending else "等待规划"))
        if pending:
            self.summary.setText(f"第 {number} 批 · {len(steps)} 箱同时开工")
            self.wave_summary.setText("按各箱图摆苗，杂交完成后分别填写实测。所有箱核对成功，再一次确认。")
        elif self.report and self.report.cancelled:
            self.summary.setText("已停止计算")
            self.wave_label.setText(f"第 {number} 批 · 计算已停止")
            self.wave_summary.setText("库存未更改。需要继续时，重新点击一键智能。")
        elif self.report and self.report.found and not self.report.waves:
            self.summary.setText("库存已有目标苗，无需再培育")
            self.wave_summary.setText("已有克隆符合目标，可直接保留。")
        elif self.report and not self.report.found:
            self.summary.setText("尚未找到完整多箱路线")
            self.wave_summary.setText("库存未扣除；可增加批数、补充样本后重新计算。")
        else:
            self.summary.setText(f"{self.box_count.value()} 个箱共同规划，按批次确认实测。")
            self.wave_summary.setText("主箱用于最终目标，副箱用于所需中间苗；无需每箱都种满。")
        if self.report and self.report.found and self.report.waves:
            lines = [f"预计 {self.report.rounds} 批 · 累计用苗 {self.report.plant_count} 株"]
            base = len(self.state.waves)
            for offset, wave in enumerate(self.report.waves, 1):
                for step in wave.steps:
                    lines.append(f"第 {base + offset} 批 · {step.box_id} 号箱：{step.recipe.center.genes} → {step.expected_genes}")
                    if offset == 1:
                        lines.append(self._usage(step))
            lines.append("搜索有范围限制，未证明全局最优。每批按实际克隆重新调配。")
            self.route_label.setText("\n".join(lines))
        elif self.report:
            self.route_label.setText(self.report.summary)
        elif pending:
            self.route_label.setText("已恢复上次本批安排；完成后将按所有箱实测重新调配。")
        else:
            self.route_label.setText("找到完整路线后，会显示预计批数和各箱的供苗关系。")
        for control in (self.target_edit, self.mode, self.objective, self.box_count, self.main_box, self.max_waves, self.clone_yield):
            control.setEnabled(not busy and not pending and self._can_plan())
        edit_stock = self._can_edit_stock()
        for control in (self.add_button, self.bulk_button, self.edit_button, self.capture_button):
            control.setEnabled(edit_stock)
        self.plan_button.setEnabled(self.is_busy() or (not busy and not pending and self._can_plan()))
        self.reset_button.setEnabled(pending and not busy)
        undo_reason = "没有可撤回的已完成批次。"
        if self.state.waves:
            try:
                undo_wave(self.state, tuple(self.owner.inventory))
                undo_reason = ""
            except ValueError as exc:
                undo_reason = str(exc)
        self.undo_button.setEnabled(not busy and not undo_reason)
        self.undo_button.setToolTip(undo_reason or "撤回最近一批完成，恢复该批用苗并重新填写实测。")
        self.history_label.setText(
            f"已完成 {len(self.state.waves)} 批。最近一次记录可撤回；只更正软件库存，不操作游戏。"
            if self.state.waves else "尚无已完成批次。")
        self.update_assessment()

    def _matches(self, step, value):
        try:
            actual = normalize_genes(value)
        except ValueError:
            return False
        return all(gene in options for gene, options in zip(actual, step.recipe.prediction.candidates))

    def draft_changed(self, *_):
        if not self.syncing:
            self.update_assessment()
            self.owner.changed()

    def update_assessment(self):
        steps = self._current_steps()
        lines = []
        all_match = bool(steps)
        by_box = {step.box_id: step for step in steps}
        for box_id, page in self.box_pages.items():
            step = by_box.get(box_id)
            if step is None:
                page.assessment.setText("本箱本批空置。")
                continue
            value = page.actual_edit.text()
            matches = self._matches(step, value)
            all_match = all_match and matches
            if matches:
                page.assessment.setText("实测与本箱预测一致；请核对实际取得的克隆数量。")
                lines.append(f"✓ {box_id} 号箱  {normalize_genes(value)} · {page.actual_quantity.value()} 株")
            elif value:
                try:
                    normalize_genes(value)
                    message = "实测与预测不一致，不能确认本批。请核对杂交是否完成及本箱用苗。"
                except ValueError:
                    message = "请填写完整六位基因，仅限 G、Y、H、W、X。"
                page.assessment.setText(message)
                lines.append(f"○ {box_id} 号箱 · 尚未核对成功")
            else:
                page.assessment.setText("等中央完成杂交、取得克隆后，填写实际基因和数量。")
                lines.append(f"○ {box_id} 号箱 · 等待实测 {step.recipe.prediction.result_pattern}")
        self.checklist.setText("\n".join(lines) if lines else "每个有安排的箱都会显示预测和实测输入框。")
        self.commit_button.setText(
            f"第 {len(self.state.waves) + 1} 批核对成功，确认并继续" if all_match else "所有箱核对完成后确认")
        self.commit_button.setEnabled(all_match and not self.is_busy() and not self._other_busy() and self._can_plan())

    def _clear_drafts(self):
        self.syncing = True
        for page in self.box_pages.values():
            page.actual_edit.clear()
            page.actual_quantity.setValue(self.clone_yield.value())
        self.syncing = False

    def complete_current_wave(self):
        if self.is_busy() or self._other_busy() or not self._can_plan() or self.state.pending is None:
            return
        steps = self._current_steps()
        if not steps or not all(self._matches(step, self.box_pages[step.box_id].actual_edit.text()) for step in steps):
            self.status.setText("本批所有箱实测都须与各自预测一致，尚未记账。")
            self.update_assessment()
            return
        actuals = {step.box_id: (self.box_pages[step.box_id].actual_edit.text(),
                                self.box_pages[step.box_id].actual_quantity.value()) for step in steps}
        try:
            result = complete_wave(self.state, tuple(self.owner.inventory), actuals, token=self.state.pending.token)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.state = result.state
        self.report = None
        self.owner.inventory = list(result.inventory)
        self._clear_drafts()
        self.owner.refresh_inventory()
        self.owner.changed(invalidate=True)
        self.refresh_from_owner()
        if any(self.state.target.matches(genes) for genes, _ in actuals.values()):
            self.status.setText(f"第 {len(self.state.waves)} 批成功，实测已达最终目标，克隆已保存。")
            self.summary.setText("实测已达目标，保留克隆")
            self.wave_label.setText(f"已完成 {len(self.state.waves)} 批 · 实测达标")
        else:
            self.status.setText(f"第 {len(self.state.waves)} 批成功，正在按实际克隆重新调配下一批。")
            self.start_planning()

    def reset_pending(self):
        if self.is_busy() or self._other_busy():
            return
        self.state = cancel_wave(self.state)
        self.report = None
        self._clear_drafts()
        self.status.setText("本批安排已撤销，库存未扣除。若已在游戏用苗，请核对实际余量。")
        self.render_wave()
        self.owner.changed()

    def undo_completed_wave(self):
        if self.is_busy() or self._other_busy() or not self._can_plan():
            return
        try:
            result = undo_wave(self.state, tuple(self.owner.inventory))
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.state = result.state
        self.report = None
        self.owner.inventory = list(result.inventory)
        self._clear_drafts()
        self.owner.refresh_inventory()
        self.owner.changed(invalidate=True)
        self.refresh_from_owner()
        self.status.setText("已撤回最近一批完成，恢复本批用苗。请重新核对所有箱实测。")

    def project_fields(self):
        return {"parallel": self.state.to_dict(), "parallel_ui": {
            "box_count": self.box_count.value(), "main_box": self.main_box.currentData() or 1,
            "max_waves": self.max_waves.value(), "clone_yield": self.clone_yield.value(),
            "objective": self.objective.currentData(),
            "selected_box": self.box_tabs.currentIndex() + 1,
            "drafts": {str(box_id): {"genes": page.actual_edit.text(), "quantity": page.actual_quantity.value()}
                       for box_id, page in self.box_pages.items()},
        }}

    def restore_project(self, data):
        self.syncing = True
        try:
            self.state = ParallelState.from_dict(data["parallel"]) if "parallel" in data else ParallelState(target=self.owner.target())
            options = data.get("parallel_ui", {})
            if not isinstance(options, dict):
                options = {}
            count = self.state.box_count if self.state.pending else options.get("box_count", self.state.box_count)
            self.box_count.setValue(count if type(count) is int else 3)
            self._rebuild_boxes()
            main = self.state.main_box if self.state.pending else options.get("main_box", self.state.main_box)
            self.main_box.setCurrentIndex(max(0, self.main_box.findData(main)))
            for name in ("max_waves", "clone_yield"):
                value = options.get(name, 3 if name == "max_waves" else 1)
                if type(value) is int:
                    getattr(self, name).setValue(value)
            self.objective.setCurrentIndex(max(0, self.objective.findData(options.get("objective", "stock"))))
            selected = options.get("selected_box", 1)
            if type(selected) is int:
                self.box_tabs.setCurrentIndex(max(0, min(selected - 1, self.box_tabs.count() - 1)))
            drafts = options.get("drafts", {})
            for box_id, page in self.box_pages.items():
                draft = drafts.get(str(box_id), {}) if isinstance(drafts, dict) else {}
                if isinstance(draft, dict):
                    page.actual_edit.setText(str(draft.get("genes", ""))[:32])
                    quantity = draft.get("quantity", self.clone_yield.value())
                    if type(quantity) is int:
                        page.actual_quantity.setValue(quantity)
        except (ValueError, TypeError) as exc:
            self.state = ParallelState(target=self.owner.target())
            self.status.setText(f"多线记录无法恢复：{exc}；共享库存已保留，请重新规划。")
        self.report = None
        self._inventory_signature = None
        self._route_context = None
        self.syncing = False
        self.refresh_from_owner()
