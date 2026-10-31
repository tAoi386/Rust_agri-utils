"""Read-only choice of inventory-backed Y/G targets.

The application owns both the discovery worker and stock validation. This
dialog only presents completed results and emits the selected option. It never
starts planning, changes the inventory, or closes itself on a selection.
"""

from __future__ import annotations

from math import inf

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QProgressBar, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from .i18n import localize_tree
from .models import Target
from .widgets import STYLE, GeneStrip, button, clear_layout, label


_STATUSES = frozenset({'already_owned', 'reachable', 'unknown', 'unreachable'})


def _selectable(option):
    """A display status alone is insufficient evidence of a usable route."""
    return (option.status in {'already_owned', 'reachable'}
            and bool(getattr(option, 'selectable', False))
            and bool(getattr(getattr(option, 'report', None), 'found', False)))


def _rank(option):
    available = _selectable(option)
    rounds = getattr(option, 'rounds', None)
    plants = getattr(option, 'plant_count', None)
    return (0 if available else 1 if option.status == 'unknown' else 2,
            rounds if available and rounds is not None else inf,
            plants if available and plants is not None else inf,
            abs(option.target.genes.count('Y') - 3),
            -option.target.genes.count('Y'))


class TargetOptionCard(QFrame):
    """One count target; unavailable targets have no action control."""

    chosen = Signal(object)

    def __init__(self, option, mode, parent=None):
        super().__init__(parent)
        self.option = option
        self.action_button = None
        self.setObjectName('panel')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(14)
        content = QVBoxLayout()
        content.setSpacing(5)
        y_count = option.target.genes.count('Y')
        g_count = 6 - y_count
        self.title = label(f'{y_count}Y + {g_count}G', 'sectionTitle')
        self.title.setProperty('i18nUserText', True)
        content.addWidget(self.title)
        self.genes = GeneStrip(option.target.genes)
        content.addWidget(self.genes)
        self.status_label = label('', 'tipText', True)
        self.status_label.setMinimumWidth(0)
        self.detail_label = label('', 'muted', True)
        self.detail_label.setMinimumWidth(0)
        content.addWidget(self.status_label)
        content.addWidget(self.detail_label)
        row.addLayout(content, 1)

        if _selectable(option):
            if option.status == 'already_owned':
                self.status_label.setText('库内已有 · 无需杂交')
                self.detail_label.setText('可直接使用已有优选株，不消耗培育用苗。')
                action = '选用已有优选株'
            else:
                self.status_label.setText('可达成 · 已找到完整路线')
                rounds = option.rounds
                plants = option.plant_count
                self.detail_label.setText(
                    f'预计 {rounds} 批 · 累计用苗 {plants} 株' if mode == 'parallel' else
                    f'预计 {rounds} 轮 · 累计用苗 {plants} 株')
                action = '选用并自动选苗'
            self.action_button = button(action, self._choose, 'primary')
            self.action_button.setMinimumHeight(36)
            self.action_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            row.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignVCenter)
        elif option.status == 'unreachable':
            self.status_label.setText('不可达 · 当前基因库条件不足')
            self.detail_label.setText('现有基因不满足该组合的必要条件，请补充样本后再筛选。')
        else:
            # A bounded search is not a proof of impossibility. In particular,
            # malformed reachable/owned results never expose an action button.
            self.status_label.setText('暂未找到完整路线')
            self.detail_label.setText('本次搜索未确认可达；可补充样本或调整规划设置后重新筛选。')
        localize_tree(self)

    def _choose(self):
        if self.action_button is not None and self.action_button.isEnabled() and _selectable(self.option):
            self.chosen.emit(self.option)

    def set_searching(self, searching):
        if self.action_button is not None:
            self.action_button.setEnabled(not searching)


class TargetSelectorDialog(QDialog):
    """Vertical result cards, safely separated from the calculation lifecycle."""

    option_selected = Signal(object)
    cancel_requested = Signal()

    def __init__(self, parent=None, mode='sequential'):
        super().__init__(parent)
        if mode not in {'sequential', 'parallel'}:
            raise ValueError('Unknown target discovery mode.')
        self.mode = mode
        self.cards = []
        self.options = ()
        self._searching = False
        self._cancel_sent = False
        self.setWindowTitle('当前基因库 · 优选组合')
        self.setStyleSheet(STYLE)
        self.resize(680, 650)
        self.setMinimumSize(560, 400)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)
        outer.addWidget(label('当前可以培育哪些优选组合', 'pageTitle', True))
        outer.addWidget(label('只筛选六位均为 Y、G 的组合，顺序不限。选择可达成的卡片即可自动安排种植。',
                              'muted', True))
        self.status = label('正在准备筛选…', 'tipText', True)
        outer.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 7)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(5)
        self.progress.hide()
        outer.addWidget(self.progress)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.contents = QWidget()
        self.rows = QVBoxLayout(self.contents)
        self.rows.setContentsMargins(0, 0, 8, 0)
        self.rows.setSpacing(9)
        self.rows.addStretch(1)
        self.scroll.setWidget(self.contents)
        outer.addWidget(self.scroll, 1)
        outer.addWidget(label('轮数和用苗按当前规划设置预估；每轮仍需在游戏中核对实际结果。',
                              'muted', True))
        controls = QHBoxLayout()
        self.stop_button = button('停止筛选', self._request_cancel)
        self.stop_button.hide()
        controls.addWidget(self.stop_button)
        controls.addStretch(1)
        self.close_button = button('关闭', self.reject)
        controls.addWidget(self.close_button)
        outer.addLayout(controls)
        localize_tree(self)

    @property
    def searching(self):
        return self._searching

    def _clear_cards(self):
        self.options = ()
        for card in self.cards:
            card.set_searching(True)
        self.cards.clear()
        clear_layout(self.rows)
        self.rows.addStretch(1)

    def set_searching(self, searching):
        searching = bool(searching)
        if searching:
            self._clear_cards()
            self._cancel_sent = False
            self.status.setText('正在结合已有基因和可用数量筛选…')
            self.progress.setRange(0, 7)
            self.progress.setValue(0)
            self.stop_button.setEnabled(True)
        self._searching = searching
        self.progress.setVisible(searching)
        self.stop_button.setVisible(searching)
        for card in self.cards:
            card.set_searching(searching)

    def update_progress(self, completed, total):
        if not self._searching or self._cancel_sent:
            return
        total = max(1, int(total))
        completed = max(0, min(int(completed), total))
        self.progress.setRange(0, total)
        self.progress.setValue(completed)
        self.status.setText(f'已筛选 {completed}/{total} 种组合，正在继续比较…')

    def set_options(self, options):
        """Render immutable results. The controller must end its worker first."""
        options = tuple(options)
        seen = set()
        for option in options:
            if (not isinstance(option.target, Target) or option.target.mode != 'counts'
                    or set(option.target.genes) - {'Y', 'G'} or option.status not in _STATUSES):
                raise ValueError('Target selector only accepts six-position Y/G count targets.')
            y_count = option.target.genes.count('Y')
            if y_count in seen:
                raise ValueError('Duplicate Y/G count target.')
            seen.add(y_count)
        self._clear_cards()
        self.options = tuple(sorted(options, key=_rank))
        for index, option in enumerate(self.options):
            card = TargetOptionCard(option, self.mode, self.contents)
            card.chosen.connect(self._select_option)
            card.set_searching(self._searching)
            self.rows.insertWidget(index, card)
            self.cards.append(card)
        choices = sum(_selectable(option) for option in self.options)
        self.status.setText(f'筛选完成 · {choices} 种可选组合，共 {len(self.options)} 种组合')
        self.scroll.verticalScrollBar().setValue(0)

    def _select_option(self, option):
        if not self._searching and option in self.options and _selectable(option):
            self.option_selected.emit(option)

    def show_error(self, message):
        self._clear_cards()
        self.status.setText(str(message))

    def set_empty_inventory(self):
        self.set_searching(False)
        self._clear_cards()
        self.status.setText('基因库为空。请先添加或读取已有克隆，再进行一键筛选。')

    def _request_cancel(self):
        if self._cancel_sent or not self._searching:
            return
        self._cancel_sent = True
        self.stop_button.setEnabled(False)
        self.status.setText('正在停止筛选…')
        self.cancel_requested.emit()

    def reject(self):
        self._request_cancel()
        super().reject()

    def closeEvent(self, event):
        self._request_cancel()
        super().closeEvent(event)
