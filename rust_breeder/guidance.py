"""Primary, actual-result driven breeding workspace."""

from __future__ import annotations

from dataclasses import replace
import logging

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea,
    QComboBox, QLineEdit, QSpinBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QProgressBar, QSizePolicy, QAbstractSpinBox,
)

from .assessment import assess_observed
from .journey import JourneyState, prepare_round, pending_recipe, complete_round, undo_last_round
from .models import normalize_genes
from .planner_process import plan_route_process as plan_route
from .notifications import ActionNotice
from .planter import positions_for
from .widgets import button, label, clear_layout
from .i18n import (LocalizedComboBox as QComboBox, LocalizedLineEdit as QLineEdit,
                   LocalizedTableWidget as QTableWidget, tr)


LOGGER = logging.getLogger(__name__)


def card(layout):
    frame = QFrame()
    frame.setObjectName("panel")
    frame.setLayout(layout)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    return frame


class RouteWorker(QThread):
    result_ready = Signal(object)
    failed = Signal(str)
    progress_changed = Signal(int, int)

    def __init__(self, inventory, target, options, parent=None):
        super().__init__(parent)
        self.inventory, self.target, self.options = tuple(inventory), target, dict(options)

    def run(self):
        try:
            report = plan_route(self.inventory, self.target, **self.options,
                                cancel=self.isInterruptionRequested,
                                progress=lambda n, r: self.progress_changed.emit(n, r))
            self.result_ready.emit(report)
        except Exception as exc:
            self.failed.emit(str(exc))


class GuidedWorkspace(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.state = JourneyState()
        self.report = None
        self.worker = None
        self.stage = 0
        self.syncing = False
        self._inventory_signature = None
        self._route_context = None
        self._shown_token = None
        self.center_preference = ""
        outer = QVBoxLayout(self)
        self._history_signature = None
        outer.setContentsMargins(16, 10, 16, 8)
        outer.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(label("种植工作台", "pageTitle"))
        head.addStretch(1)
        self.crop = QComboBox()
        self.crop.setMinimumWidth(115)
        for i in range(owner.crop_combo.count()):
            self.crop.addItem(owner.crop_combo.itemSourceText(i), owner.crop_combo.itemData(i))
        self.crop.currentIndexChanged.connect(self.change_crop)
        head.addWidget(self.crop)
        outer.addLayout(head)

        setup = QHBoxLayout()
        setup.setSpacing(8)
        setup.addWidget(label("目标基因", "stepTitle"))
        self.target_edit = QLineEdit("YYYGGG")
        self.target_edit.setMaximumWidth(105)
        self.target_edit.setObjectName("geneInput")
        self.target_edit.setPlaceholderText("例如 YYYGGG")
        self.target_edit.textChanged.connect(self.change_target)
        setup.addWidget(self.target_edit)
        self.mode = QComboBox()
        self.mode.addItem("按数量 · 顺序不限", "counts")
        self.mode.addItem("逐位 · 固定顺序", "exact")
        self.mode.currentIndexChanged.connect(self.change_target)
        setup.addWidget(self.mode)
        self.objective = QComboBox()
        self.objective.addItem("优先总用苗最少", "plants")
        self.objective.addItem("优先杂交轮数最少", "rounds")
        self.objective.addItem("智能匹配（基于已有基因库）", "stock")
        self.objective.setItemData(2, "根据已有基因组合和实际可用苗比较后续完整路线；结果需逐轮实测确认。",
                                   Qt.ItemDataRole.ToolTipRole)
        self.objective.setCurrentIndex(self.objective.findData('stock'))
        self.objective.currentIndexChanged.connect(self.options_changed)
        setup.addWidget(self.objective)
        setup.addStretch()
        self.summary = label("先录入已有克隆，再点自动选苗。", "muted", True)
        setup.addWidget(self.summary, 1)
        outer.addWidget(card(setup))

        columns = QHBoxLayout()
        columns.setSpacing(10)
        inventory = QVBoxLayout()
        inventory_head = QHBoxLayout()
        inventory_head.addWidget(label("基因库", "sectionTitle"))
        inventory_head.addStretch()
        self.inventory_count = label("0 组 · 0 株", "muted")
        inventory_head.addWidget(self.inventory_count)
        inventory.addLayout(inventory_head)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("搜索基因 / 名称")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self.filter_rows)
        inventory.addWidget(self.filter_edit)
        self.inventory_table = QTableWidget(0, 3)
        self.inventory_table.setHorizontalHeaderLabels(["基因", "株数", "名称"])
        self.inventory_table.verticalHeader().hide()
        self.inventory_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.inventory_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.inventory_table.setAlternatingRowColors(True)
        self.inventory_table.setShowGrid(False)
        self.inventory_table.verticalHeader().setDefaultSectionSize(29)
        self.inventory_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.inventory_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.inventory_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.inventory_table.cellDoubleClicked.connect(lambda row, _: self.use_selected_center(row))
        inventory.addWidget(self.inventory_table, 1)
        self.pin_button = button("将选中苗设为中央", self.use_selected_center, "small")
        self.pin_button.setToolTip("也可保留自动选择，让软件一起比较中央株和供体。")
        inventory.addWidget(self.pin_button)
        stock_actions = QHBoxLayout()
        self.add_button = button("添加", owner.add_seed, "small")
        self.bulk_button = button("批量录入", owner.bulk_add, "small")
        self.edit_button = button("编辑数量", self.edit_selected, "small")
        for control in (self.add_button, self.bulk_button, self.edit_button):
            stock_actions.addWidget(control)
        inventory.addLayout(stock_actions)
        self.capture_button = button("从游戏读取基因", owner.capture_genes)
        inventory.addWidget(self.capture_button)
        inventory.addWidget(label("数量填写实际可用克隆；基因按原顺序保存。", "muted", True))
        inventory_frame = card(inventory)
        inventory_frame.setMaximumWidth(255)
        inventory_frame.setMinimumWidth(215)
        columns.addWidget(inventory_frame)

        middle = QVBoxLayout()
        board_head = QHBoxLayout()
        board_head.addWidget(label("种植布局", "sectionTitle"))
        board_head.addStretch()
        self.layout_label = label("3 × 3 · 中央为 5 号位", "muted")
        board_head.addWidget(self.layout_label)
        middle.addLayout(board_head)
        self.center_combo = QComboBox()
        self.center_combo.setToolTip(
            "自动模式会在每轮实测后，根据实际库存和新克隆重新选择中央株与供体。\n"
            "手动固定中央后，会沿这株的实测克隆继续培育。")
        self.center_combo.currentIndexChanged.connect(self.center_changed)
        middle.addWidget(self.center_combo)
        settings = QHBoxLayout()
        settings.setSpacing(6)
        settings.addWidget(label("规划"))
        self.max_rounds = QSpinBox()
        self.max_rounds.setRange(1, 5)
        self.max_rounds.setValue(3)
        self.max_rounds.setMaximumWidth(63)
        self.max_rounds.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)
        self.max_rounds.valueChanged.connect(self.options_changed)
        settings.addWidget(self.max_rounds)
        settings.addWidget(label("轮  ·  每轮保留"))
        self.clone_yield = QSpinBox()
        self.clone_yield.setRange(1, 9)
        self.clone_yield.setValue(1)
        self.clone_yield.setMaximumWidth(63)
        self.clone_yield.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)
        self.clone_yield.valueChanged.connect(self.options_changed)
        settings.addWidget(self.clone_yield)
        settings.addWidget(label("株克隆"))
        settings.addStretch()
        middle.addLayout(settings)
        self.plan_button = button("自动选苗并摆入种植箱", self.start_planning, "primary")
        self.plan_button.setMinimumHeight(35)
        middle.addWidget(self.plan_button)
        self.action_notice = ActionNotice(self.plan_button, self)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(5)
        self.progress.setTextVisible(False)
        self.progress.hide()
        middle.addWidget(self.progress)
        self.status = label("自动挑选中央株和供体，也可指定中央株。", "muted", True)
        self.status.setMinimumHeight(22)
        self.status.setMaximumHeight(48)
        middle.addWidget(self.status)
        self.board = QGridLayout()
        self.board.setSpacing(6)
        self.cells = []
        self.cell_predictions = []
        for index in range(9):
            content = QVBoxLayout()
            content.setContentsMargins(8, 7, 8, 8)
            content.setSpacing(4)
            number = label(f"{index + 1:02d}" + (" · 种下的中央株" if index == 4 else ""), "cellNumber")
            genes = label("留空", "boardGene", True)
            genes.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name = label("", "muted", True)
            name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            prediction = label("", "muted", True)
            prediction.setAlignment(Qt.AlignmentFlag.AlignCenter)
            prediction.setVisible(index == 4)
            content.addWidget(number)
            content.addStretch()
            content.addWidget(genes)
            content.addWidget(name)
            content.addWidget(prediction)
            content.addStretch()
            cell = QFrame()
            cell.setObjectName("inset")
            cell.setProperty("central", index == 4)
            cell.setProperty("occupied", False)
            cell.setLayout(content)
            cell.setMinimumSize(94, 76)
            cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.cells.append((cell, genes, name))
            self.cell_predictions.append(prediction)
            self.board.addWidget(cell, index // 3, index % 3)
        middle.addLayout(self.board, 1)
        self.prediction_label = label("预计杂交结果：等待规划", "prediction", True)
        middle.addWidget(self.prediction_label)
        self.route_toggle = button("查看后续路线", name="textButton")
        self.route_toggle.setCheckable(True)
        middle.addWidget(self.route_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.route_label = label("先自动选苗，查看路线。", "tipText", True)
        self.route_label.setMaximumHeight(118)
        self.route_label.hide()
        self.route_toggle.toggled.connect(self.route_label.setVisible)
        middle.addWidget(self.route_label)
        columns.addWidget(card(middle), 2)

        right = QVBoxLayout()
        right.setSpacing(8)
        instructions = QVBoxLayout()
        instructions.setSpacing(6)
        instructions.addWidget(label("当前轮次", "sectionTitle"))
        self.round_label = label("第 1 轮 · 等待规划", "muted", True)
        instructions.addWidget(self.round_label)
        self.instruction_title = label("先点自动选苗", "stepTitle", True)
        self.instruction_body = label("按布局种植并等待杂交，完成后核对实测。", "tipText", True)
        instructions.addWidget(self.instruction_title)
        instructions.addWidget(self.instruction_body)
        self.instruction_body.setMinimumHeight(40)
        self.instruction_body.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.round_prediction = label("本轮预测：等待规划", "prediction", True)
        instructions.addWidget(self.round_prediction)
        right.addLayout(instructions)
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        right.addWidget(divider)
        observed = QVBoxLayout()
        observed.setSpacing(7)
        observed.addWidget(label("记录实际结果", "sectionTitle", True))
        self.actual_edit = QLineEdit()
        self.actual_edit.setMaxLength(32)
        self.actual_edit.setPlaceholderText("杂交后的六位基因")
        self.actual_edit.setObjectName("geneInput")
        self.actual_edit.textChanged.connect(self.draft_changed)
        observed.addWidget(self.actual_edit)
        self.actual_assessment = label("输入后会立即比较目标。", "muted", True)
        self.actual_assessment.setMinimumHeight(48)
        observed.addWidget(self.actual_assessment)
        quantities = QHBoxLayout()
        quantities.addWidget(label("取得克隆"))
        self.actual_quantity = QSpinBox()
        self.actual_quantity.setRange(1, 9999)
        self.actual_quantity.setMaximumWidth(84)
        self.actual_quantity.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)
        self.actual_quantity.valueChanged.connect(self.draft_changed)
        quantities.addWidget(self.actual_quantity)
        quantities.addWidget(label("株"))
        quantities.addStretch()
        observed.addLayout(quantities)
        self.completed_check = QComboBox()
        self.completed_check.addItem("尚未完成杂交并取得克隆", False)
        self.completed_check.addItem("已完成，基因和克隆数量已核对", True)
        self.completed_check.currentIndexChanged.connect(self.draft_changed)
        observed.addWidget(self.completed_check)
        self.commit_button = button("填写并核对本轮实测", self.complete_current_round, "primary")
        self.commit_button.setToolTip("提交后扣除本轮中央苗和供体，并按实际基因、实际数量加入新克隆。")
        observed.addWidget(self.commit_button)
        observed.addWidget(label("实测须与本轮预测一致；确认提交后才扣用苗。", "muted", True))
        right.addLayout(observed)
        right.addStretch(1)
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        right.addWidget(divider)
        history = QVBoxLayout()
        history.setSpacing(6)
        self.history_toggle = button("完成记录 · 0 轮", name="textButton")
        self.history_toggle.setCheckable(True)
        history.addWidget(self.history_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.history_scroll = QScrollArea()
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.history_scroll.setMaximumHeight(145)
        self.history_scroll.setMinimumHeight(0)
        self.history_scroll.hide()
        history_content = QWidget()
        self.history_rows = QVBoxLayout(history_content)
        self.history_rows.setContentsMargins(0, 0, 5, 0)
        self.history_rows.setSpacing(9)
        self.history_scroll.setWidget(history_content)
        self.history_toggle.toggled.connect(self.history_scroll.setVisible)
        history.addWidget(self.history_scroll)
        self.undo_button = button("撤回上轮完成，恢复用苗", self.undo_completed_round, "secondary")
        history.addWidget(self.undo_button)
        self.undo_button.setToolTip("误点完成可撤回最近一轮，恢复用苗并重新填写实测。只更正软件记录。")
        self.reset_button = button("撤销本轮规划（库存不变）", self.reset_pending, "small")
        history.addWidget(self.reset_button)
        right.addLayout(history)
        right_frame = card(right)
        right_frame.setMinimumWidth(285)
        right_frame.setMaximumWidth(315)
        columns.addWidget(right_frame, 1)
        outer.addLayout(columns, 1)
        outer.addWidget(label("G 生长  ·  Y 产量  ·  H 耐性  ·  W 耗水  ·  X 空基因     每轮按实测基因与实际库存更新安排。", "muted", True))
        self.refresh_from_owner()

    def is_busy(self):
        # A stopped native thread can still have queued result/finished signals.
        # Retain ownership until its finished callback has cleaned up that task.
        return self.worker is not None

    def current_recipe(self):
        return pending_recipe(self.state, tuple(self.owner.inventory))

    def context(self):
        return (tuple(self.owner.inventory), self.owner.target(), self.owner.project_crop,
                self.center_combo.currentData() or None, self.max_rounds.value(),
                self.clone_yield.value(), self.objective.currentData())

    def change_crop(self, *_):
        if not self.syncing:
            self.owner.crop_combo.setCurrentIndex(self.owner.crop_combo.findData(self.crop.currentData()))

    def change_target(self, *_):
        if self.syncing:
            return
        if self.state.pending is not None or not self.owner.can_edit_stock():
            self.syncing = True
            self.target_edit.setText(self.owner.target_edit.text())
            self.mode.setCurrentIndex(self.mode.findData(self.owner.mode_combo.currentData()))
            self.syncing = False
            return
        self.owner.target_edit.setText(self.target_edit.text())
        self.owner.mode_combo.setCurrentIndex(self.owner.mode_combo.findData(self.mode.currentData()))

    def center_changed(self, *_):
        if self.syncing:
            return
        self.center_preference = self.center_combo.currentData() or ""
        self.state = replace(self.state, center_id=self.center_preference, pending=None)
        self.report = None
        self.stage = 0
        self.actual_edit.clear()
        self.status.setText("中央选择已更新，点击自动选苗重新规划。")
        self.render_round()
        self.owner.changed()

    def options_changed(self, *_):
        if not self.syncing:
            self.report = None
            self.state = replace(self.state, pending=None)
            self.stage = 0
            self.render_round()
            self.owner.changed()

    def filter_rows(self):
        text = self.filter_edit.text().strip().upper()
        for row in range(self.inventory_table.rowCount()):
            self.inventory_table.setRowHidden(row, bool(text) and not any(
                text in self.inventory_table.item(row, column).text().upper() for column in (0, 2)))

    def use_selected_center(self, row=None):
        if self.is_busy() or self.state.pending is not None or not self.owner.can_plan(self):
            return
        if row is None or isinstance(row, bool):
            row = self.inventory_table.currentRow()
        if row < 0:
            self.status.setText("先在基因库里选中一行；也可直接自动选中央。")
            return
        seed_id = self.inventory_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        self.center_combo.setCurrentIndex(self.center_combo.findData(seed_id))

    def edit_selected(self):
        if self.is_busy() or self.state.pending is not None or not self.owner.can_edit_stock(exclude=self):
            return
        row = self.inventory_table.currentRow()
        if row >= 0:
            self.owner.edit_seed(self.inventory_table.item(row, 0).data(Qt.ItemDataRole.UserRole))

    def refresh_from_owner(self):
        self.syncing = True
        try:
            target = self.owner.target()
        except ValueError:
            target = None
        if target is not None and self.state.target != target:
            self.state = replace(self.state, target=target, pending=None)
            self.report = None
            self.stage = 0
        self.target_edit.setText(self.owner.target_edit.text())
        self.mode.setCurrentIndex(self.mode.findData(self.owner.mode_combo.currentData()))
        self.crop.setCurrentIndex(self.crop.findData(self.owner.project_crop))
        signature = tuple(self.owner.inventory)
        if signature != self._inventory_signature:
            self._inventory_signature = signature
            available = {item.id for item in signature}
            center = self.state.center_id
            if self.center_preference not in available:
                self.center_preference = ""
            if center and center not in available:
                self.state = replace(self.state, center_id="", pending=None)
                center = ""
                self.stage = 0
                self.report = None
            if self.state.pending is not None:
                try:
                    self.current_recipe()
                except ValueError:
                    self.state = replace(self.state, pending=None)
                    self.stage = 0
                    self.report = None
                    self.status.setText("库存已经变化，当前方案已失效，请重新自动选苗。")
            self.center_combo.clear()
            self.center_combo.addItem("自动调配中央株和供体（每轮重选）", "")
            for item in signature:
                self.center_combo.addItem(f"固定 #{self.owner.seed_number(item):02d} · {item.genes} · {item.quantity} 株", item.id)
            self.center_combo.setCurrentIndex(max(0, self.center_combo.findData(self.center_preference)))
            self.inventory_table.setRowCount(len(signature))
            for row, item in enumerate(signature):
                gene = QTableWidgetItem(item.genes)
                gene.setData(Qt.ItemDataRole.UserRole, item.id)
                self.inventory_table.setItem(row, 0, gene)
                self.inventory_table.setItem(row, 1, QTableWidgetItem(str(item.quantity)))
                self.inventory_table.setItem(row, 2, QTableWidgetItem(f"#{self.owner.seed_number(item):02d} · {item.label}"))
            self.filter_rows()
        self.inventory_count.setText(f"{len(signature)} 组 · {sum(item.quantity for item in signature)} 株")
        self.syncing = False
        self.render_round()

    def start_planning(self):
        if self.is_busy():
            LOGGER.info('Route search stop requested')
            self.worker.requestInterruption()
            self.status.setText("正在停止计算…")
            return
        if self.owner.work_busy(exclude=self) or not self.owner.can_plan(self):
            self.status.setText("另一种培育模式仍有进行中的安排，请先完成或撤销该安排。")
            return
        self.action_notice.dismiss()
        try:
            context = self.context()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        if not self.owner.inventory:
            self.status.setText("先添加或自动读取已有的克隆基因，再自动选苗。")
            self.action_notice.show_message("样本不足", "请先录入可用克隆")
            return
        if self.stage > 0 and self.state.pending is not None:
            self.status.setText("本轮已安排，请填写实测；需要重新安排可先撤销本轮规划。")
            return
        self.report = None
        self.state = replace(self.state, pending=None, target=context[1], center_id=context[3] or "")
        self.stage = 0
        self._route_context = context
        options = dict(center_id=context[3], max_rounds=context[4], clone_yield=context[5], objective=context[6],
                       max_combinations=120000, max_seconds=12, beam_width=24, donor_pool_limit=14)
        worker = RouteWorker(context[0], context[1], options, self)
        self.worker = worker
        worker.result_ready.connect(self.route_result_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self.route_failed, Qt.ConnectionType.QueuedConnection)
        worker.progress_changed.connect(self.route_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self.search_finished, Qt.ConnectionType.QueuedConnection)
        self.status.setText("正在结合已有基因组合和实际可用苗，比较后续完整路线…" if context[6] == "stock" else
                            "正在一起比较中央株与供体，并向后寻找完整路线…")
        self.set_busy(True)
        LOGGER.info('Route search starting: inventory_rows=%d plants=%d max_rounds=%d',
                    len(context[0]), sum(row.quantity for row in context[0]), context[4])
        worker.start()

    @Slot(object)
    def route_result_ready(self, report):
        if self.sender() is self.worker and self.worker is not None:
            LOGGER.info('Route result received: found=%s evaluated=%d reason=%s',
                        report.found, report.evaluated, report.reason)
            self.accept_route(report, self._route_context)

    @Slot(str)
    def route_failed(self, error):
        if self.sender() is self.worker and self.worker is not None:
            LOGGER.warning('Route worker reported a calculation failure')
            self.status.setText(f"计算未完成：{error}")

    @Slot(int, int)
    def route_progress(self, compared, round_number):
        if self.sender() is self.worker and self.worker is not None:
            self.status.setText(f"已比较 {compared:,} 组 · 正在看第 {round_number} 轮")

    def accept_route(self, report, context=None):
        try:
            if context is not None and context != self.context():
                self.status.setText("库存或目标已变化，旧结果未应用，请重新自动选苗。")
                return
        except ValueError:
            return
        self.report = report
        self.syncing = True
        self.actual_edit.clear()
        self.completed_check.setCurrentIndex(0)
        self.syncing = False
        self.status.setText(report.summary)
        # A promising single round is not evidence that the final target is
        # reachable. Only a complete route may start a new planting workflow.
        recipe = report.best if report.found else None
        self.state = replace(self.state, pending=None)
        if recipe is not None:
            self.state = replace(self.state, target=recipe.target, center_id=recipe.center_id, pending=None)
            if recipe.donors or recipe.guaranteed:
                try:
                    self.state = prepare_round(self.state, tuple(self.owner.inventory), recipe)
                except ValueError as exc:
                    self.status.setText(str(exc))
        self.stage = 2 if self.state.pending is not None else 0
        self.render_round()
        if not report.found and not report.cancelled:
            self.action_notice.show_message("暂未找到方案", "可补充样本或增加规划轮数后重试")
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
        LOGGER.info('Route worker finished and released')
        self.set_busy(False)

    def set_busy(self, busy):
        self.progress.setVisible(busy)
        self.plan_button.setText("停止计算" if busy else "自动选苗并摆入种植箱")
        self.owner.set_busy(busy)
        self.render_round()

    def render_round(self):
        try:
            recipe = self.current_recipe()
        except ValueError:
            recipe = None
        busy = self.is_busy() or self.owner.work_busy(exclude=self)
        in_progress = self.state.pending is not None
        if in_progress:
            # Older projects stored the click-through planting steps. A saved
            # arrangement is now immediately ready for actual-result entry.
            self.stage = 2
        self.summary.setText(f"第 {self.state.next_round} 轮 · " + (
            f"中央 {recipe.center.genes}" if recipe else "等待自动选苗"))
        self.round_label.setText(f"第 {self.state.next_round} 轮 · " + ("等待实测核对" if recipe else "等待规划"))
        slots = [None] * 9
        positions = ()
        self.layout_label.setText("3 × 3 · 中央为 5 号位")
        if recipe:
            layout = self.state.pending.layout if self.state.pending is not None else "auto"
            positions = positions_for(recipe.donor_count, layout)
            slots[4] = recipe.center
            for position, item in zip(positions, recipe.donors):
                slots[position] = item
            if recipe.donor_count:
                placement = ("已保存摆法" if layout == "cross" else
                             "上下对置" if layout == "auto" and recipe.donor_count == 2 else
                             "上方供体" if layout == "auto" and recipe.donor_count == 1 else
                             "四角 + 边位" if recipe.donor_count > 4 else
                             "四角供体" if recipe.donor_count == 4 else
                             "对角供体" if recipe.donor_count == 2 else "角落供体")
                self.layout_label.setText(f"{placement} · 中央 5 号位")
        for index, (frame, genes, name) in enumerate(self.cells):
            item = slots[index]
            genes.setText(item.genes if item else "留空")
            genes.setEnabled(item is not None)
            name.setText(f"#{self.owner.seed_number(item):02d} 苗 · {item.label}" if item else "")
            self.cell_predictions[index].setText(
                f"预测 → {recipe.prediction.deterministic_result or recipe.prediction.result_pattern}"
                if index == 4 and recipe else "")
            occupied = item is not None
            if frame.property("occupied") != occupied:
                frame.setProperty("occupied", occupied)
                frame.style().unpolish(frame)
                frame.style().polish(frame)
        goal = recipe is not None and recipe.guaranteed and not recipe.donors
        retained = self.owner.by_id(self.state.center_id)
        completed_goal = (recipe is None and bool(self.state.rounds) and retained is not None
                          and self.state.target.matches(retained.genes)
                          and self.state.rounds[-1].target == self.state.target
                          and self.state.rounds[-1].target_matched)
        if completed_goal:
            self.cells[4][1].setText(retained.genes)
            self.cells[4][1].setEnabled(True)
            self.cells[4][2].setText(f"#{self.owner.seed_number(retained):02d} 苗 · 实测达标")
            self.cell_predictions[4].setText("已确认的实际结果")
            self.cells[4][0].setProperty("occupied", True)
            self.cells[4][0].style().unpolish(self.cells[4][0])
            self.cells[4][0].style().polish(self.cells[4][0])
        if recipe:
            prediction = recipe.prediction.deterministic_result or recipe.prediction.result_pattern
            self.prediction_label.setText(f"本轮用苗：中央 1 株 + 供体 {recipe.donor_count} 株\n预测为模型结果，杂交完成后请核对实测。")
            self.round_prediction.setText(f"本轮预测：{prediction}")
            if goal:
                self.instruction_title.setText("已经有符合目标的苗，直接保留")
                self.instruction_body.setText(f"{recipe.center.genes} 符合 {recipe.target.label}，无需再消耗供体。")
            else:
                location_text = "、".join(str(position + 1) for position in positions)
                self.instruction_title.setText("按布局种植，杂交后填写实测")
                self.instruction_body.setText(
                    f"先种中央 #{self.owner.seed_number(recipe.center):02d} 苗。接近 Crossbreed 前，"
                    f"在 {location_text} 号位放供体，其余留空。中央完成杂交后核对六位基因，再清理供体并取克隆。")
        else:
            self.prediction_label.setText("预计杂交结果：等待规划")
            self.round_prediction.setText(f"实际达标：{retained.genes}" if completed_goal else "本轮预测：等待规划")
            self.instruction_title.setText("实际结果已经达标，保留克隆" if completed_goal else "先点自动选苗")
            self.instruction_body.setText(f"已保存实测 {retained.genes}，无需继续消耗供体。" if completed_goal else
                                          "从整个基因库挑选合适的中央株和供体；如果暂未找到路线，会说明搜索范围。")
            if completed_goal:
                self.summary.setText("已达到最终目标")
                self.round_label.setText(f"已完成 {len(self.state.rounds)} 轮 · 实测达标")
            elif self.report is not None and self.report.cancelled:
                self.summary.setText("已停止计算")
                self.round_label.setText(f"第 {self.state.next_round} 轮 · 计算已停止")
                self.prediction_label.setText("计算已停止，尚未安排用苗。")
                self.instruction_title.setText("已停止计算")
                self.instruction_body.setText("库存未更改。需要继续时，重新点击自动选苗。")
            elif self.report is not None and not self.report.found:
                self.summary.setText("未找到完整达标路线")
                self.round_label.setText(f"第 {self.state.next_round} 轮 · 等待完整路线")
                self.prediction_label.setText("尚无完整路线，暂不提供摆苗方案。")
                self.instruction_title.setText(f"未找到 {self.state.target.label}")
                self.instruction_body.setText("请补充可用克隆，或增加向后看的轮数后重新计算。")
        self.actual_edit.setEnabled(recipe is not None and not goal and not busy)
        self.actual_quantity.setEnabled(self.actual_edit.isEnabled())
        self.completed_check.setEnabled(self.actual_edit.isEnabled())
        if self.report and self.report.found and self.report.steps:
            lines = [f"预计路线 · {self.report.rounds} 轮，累计种 {self.report.plant_count} 株"]
            lines.extend(f"{i}. {step.recipe.center.genes} → {step.expected_genes}（供体 {step.recipe.donor_count} 株）"
                         for i, step in enumerate(self.report.steps, 1))
            lines.append("后续以实际结果为准；中间一轮不必马上更接近目标。")
            self.route_label.setText("\n".join(lines))
        elif recipe and not goal:
            self.route_label.setText(f"本轮模型预计达标 · 共种 {recipe.plant_count} 株，完成后仍需填写实测。" if recipe.guaranteed else
                                     "已恢复上次保存的本轮安排；请按游戏实际进度填写结果，确认后重新规划。")
        elif self.report is not None and not self.report.found and not completed_goal:
            self.route_label.setText(self.report.summary)
        else:
            self.route_label.setText("已达标，无需继续杂交。" if goal or completed_goal else "先自动选苗，查看路线。")
        editable = not busy and not in_progress and self.owner.can_plan(self)
        for widget in (self.target_edit, self.mode, self.objective, self.max_rounds, self.clone_yield, self.center_combo,
                       self.pin_button):
            widget.setEnabled(editable)
        stock_editable = editable and self.owner.can_edit_stock(exclude=self)
        for widget in (self.add_button, self.bulk_button, self.capture_button, self.edit_button):
            widget.setEnabled(stock_editable)
        self.crop.setEnabled(not self.owner.inventory and editable)
        self.plan_button.setEnabled((not in_progress or self.is_busy()) and not self.owner.work_busy(exclude=self)
                                    and self.owner.can_plan(self))
        self.reset_button.setEnabled(self.state.pending is not None and not busy)
        undo_reason = "没有可撤回的完成记录。"
        if self.state.rounds:
            try:
                undo_last_round(self.state, tuple(self.owner.inventory))
                undo_reason = ""
            except ValueError as exc:
                undo_reason = str(exc)
        self.undo_button.setEnabled(not busy and not undo_reason and self.owner.can_plan(self))
        self.undo_button.setToolTip(undo_reason or "撤回最近一轮完成，恢复当时中央苗和供体，回到等待实测。")
        self.history_toggle.setText(f"完成记录 · {len(self.state.rounds)} 轮")
        # Progress and draft input only update existing controls. Rebuilding the
        # history here would repeatedly allocate and dispose Qt objects while
        # the route worker is active.
        history_signature = tuple(self.state.rounds[-20:])
        if self._history_signature != history_signature:
            self._history_signature = history_signature
            clear_layout(self.history_rows)
            for record in history_signature:
                self.history_rows.addWidget(label(record.summary, "muted", True))
            if not history_signature:
                self.history_rows.addWidget(label("提交实际结果后，完成记录会显示在这里。", "muted", True))
        self.update_assessment()

    def draft_changed(self, *_):
        if not self.syncing:
            self.update_assessment()
            self.owner.changed()

    def _matches_prediction(self, genes):
        try:
            actual = normalize_genes(genes)
            recipe = self.current_recipe()
        except ValueError:
            return False
        return recipe is not None and all(gene in options for gene, options in zip(actual, recipe.prediction.candidates))

    def update_assessment(self):
        valid = False
        result = None
        try:
            result = assess_observed(self.actual_edit.text(), self.state.target)
            self.actual_assessment.setText(result.headline + "\n" + result.detail)
            valid = True
        except ValueError:
            self.actual_assessment.setText("请填写游戏里读到的完整六位基因。" if self.actual_edit.text() else "输入后会立即比较目标。")
        matches = valid and self._matches_prediction(self.actual_edit.text())
        mismatch = valid and self.state.pending is not None and not matches
        if matches and self.status.text() == tr("输入与本轮预测不一致，尚未记账。请核对杂交是否完成及实际基因。"):
            self.status.setText("实测须与本轮预测一致；确认提交后才扣用苗。")
        if mismatch:
            self.actual_assessment.setText(f"与本轮预测不一致，不能进入下一轮。\n本轮预测：{self.state.pending.predicted_pattern}\n请核对是否已完成杂交，以及中央苗和供体是否正确。")
        elif matches:
            self.actual_assessment.setText(f"第 {self.state.next_round} 轮实测与预测一致。\n{result.headline}")
        try:
            recipe = self.current_recipe()
        except ValueError:
            recipe = None
        can_record = recipe is not None and not (recipe.guaranteed and not recipe.donors)
        if mismatch:
            text = "实测与预测不符，请核对"
        elif matches:
            action = "确认完成培育" if self.state.target.matches(normalize_genes(self.actual_edit.text())) else "确认并进入下一轮"
            text = f"第 {self.state.next_round} 轮成功，{action}"
        else:
            text = "填写并核对本轮实测"
        self.commit_button.setText(text)
        self.commit_button.setEnabled(matches and can_record and self.state.pending is not None
                                      and self.completed_check.currentData() is True and not self.is_busy()
                                      and not self.owner.work_busy(exclude=self) and self.owner.can_plan(self))

    def complete_current_round(self):
        if (self.is_busy() or self.owner.work_busy(exclude=self) or not self.owner.can_plan(self)
                or self.completed_check.currentData() is not True):
            return
        pending = self.state.pending
        if pending is None:
            return
        try:
            recipe = self.current_recipe()
        except ValueError:
            return
        if recipe is None or (recipe.guaranteed and not recipe.donors):
            return
        if not self._matches_prediction(self.actual_edit.text()):
            self.status.setText("输入与本轮预测不一致，尚未记账。请核对杂交是否完成及实际基因。")
            self.update_assessment()
            return
        try:
            completion = complete_round(self.state, tuple(self.owner.inventory), self.actual_edit.text(),
                                        self.actual_quantity.value(), token=pending.token)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.state = completion.state
        if self.center_preference:
            self.center_preference = completion.center_id
        self.stage = 0
        self.report = None
        self.owner.inventory = list(completion.inventory)
        self.owner.slots = [None] * 9
        self.owner.reset_observation()
        self.syncing = True
        self.actual_edit.clear()
        self.completed_check.setCurrentIndex(0)
        self.syncing = False
        self.owner.refresh_inventory()
        self.owner.refresh_board()
        self.owner.changed(invalidate=True)
        self.refresh_from_owner()
        if completion.record.target_matched:
            self.status.setText(f"实测 {completion.record.actual_genes} 已达标！已保存 {completion.record.clone_quantity} 株新克隆。")
            self.summary.setText("已达到最终目标")
            self.instruction_title.setText("这株已经达标，保留克隆")
            self.instruction_body.setText("实际基因符合目标。本轮已记账，不再消耗供体。")
        else:
            next_action = "沿实测株继续规划" if self.center_preference else "按实际库存重新调配中央株和供体"
            self.status.setText(f"已记录实际 {completion.record.actual_genes}，正在{next_action}。")
            self.start_planning()

    def reset_pending(self):
        if self.is_busy() or self.owner.work_busy(exclude=self):
            return
        self.state = replace(self.state, pending=None)
        self.report = None
        self.stage = 0
        self.actual_edit.clear()
        self.completed_check.setCurrentIndex(0)
        self.status.setText("本轮规划已撤销，库存未记账。若已在游戏用苗，请核对实际余量再规划。")
        self.render_round()
        self.owner.changed()

    def undo_completed_round(self):
        if self.is_busy() or self.owner.work_busy(exclude=self) or not self.owner.can_plan(self):
            return
        try:
            undone = undo_last_round(self.state, tuple(self.owner.inventory))
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.state = undone.state
        self.report = None
        self._route_context = None
        if self.center_preference:
            self.center_preference = self.state.center_id
        self.stage = 2
        self.owner.inventory = list(undone.inventory)
        self.owner.slots = [None] * 9
        self.owner.reset_observation()
        self.syncing = True
        self.actual_edit.clear()
        self.actual_quantity.setValue(undone.record.clone_quantity)
        self.completed_check.setCurrentIndex(0)
        self.syncing = False
        self.owner.refresh_inventory()
        self.owner.refresh_board()
        self.refresh_from_owner()
        self.status.setText(f"已撤回第 {undone.record.number} 轮完成，恢复中央苗和供体。请等杂交完成后重新填写实测。")
        self.owner.changed(invalidate=True)

    def project_fields(self):
        return {"journey": self.state.to_dict(), "guided": {
            "stage": self.stage, "actual_input": self.actual_edit.text(), "actual_quantity": self.actual_quantity.value(),
            "completed": self.completed_check.currentData() is True, "max_rounds": self.max_rounds.value(),
            "clone_yield": self.clone_yield.value(), "objective": self.objective.currentData(),
            "center_mode": self.center_combo.currentData() or ""}}

    def restore_project(self, data):
        self.syncing = True
        try:
            self.state = JourneyState.from_dict(data["journey"]) if "journey" in data else JourneyState(target=self.owner.target())
            if self.state.pending is not None:
                try:
                    self.current_recipe()
                except ValueError as exc:
                    self.state = replace(self.state, pending=None)
                    self.status.setText(f"待执行方案已失效：{exc} 已保留完成的轮次，请重新规划。")
            options = data.get("guided", {})
            if not isinstance(options, dict):
                options = {}
            preference = options.get("center_mode", "")
            self.center_preference = preference if isinstance(preference, str) else ""
            self.stage = 2 if self.state.pending is not None else 0
            self.actual_edit.setText(str(options.get("actual_input", ""))[:32])
            for name in ("max_rounds", "clone_yield", "actual_quantity"):
                value = options.get(name, 3 if name == "max_rounds" else 1)
                if type(value) is int:
                    getattr(self, name).setValue(value)
            self.objective.setCurrentIndex(max(0, self.objective.findData(options.get("objective", "stock"))))
            self.completed_check.setCurrentIndex(int(options.get("completed") is True))
        except ValueError as exc:
            self.state = JourneyState(target=self.owner.target())
            self.stage = 0
            self.status.setText(f"连续记录无法恢复：{exc} 库存已保留，请重新规划。")
        self.report = None
        self._inventory_signature = None
        self.syncing = False
        self.refresh_from_owner()
