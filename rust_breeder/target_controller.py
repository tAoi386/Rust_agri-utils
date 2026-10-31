"""Inventory-safe discovery and selection of complete Y/G breeding routes."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot

from .models import Target
from .parallel import validate_parallel_report
from .parallel_models import ParallelReport
from .planner_process import plan_targets_process
from .routes import RouteReport
from .stock_planning import _verified
from .target_selector import TargetSelectorDialog


LOGGER = logging.getLogger(__name__)


class TargetDiscoveryWorker(QThread):
    result_ready = Signal(object)
    failed = Signal(str)
    progress_changed = Signal(int, int)

    def __init__(self, inventory, mode, settings, parent=None):
        super().__init__(parent)
        self.inventory, self.mode, self.settings = tuple(inventory), mode, dict(settings)

    def run(self):
        try:
            report = plan_targets_process(
                self.inventory, mode=self.mode, **self.settings,
                cancel=self.isInterruptionRequested,
                progress=lambda completed, total: self.progress_changed.emit(completed, total),
            )
            self.result_ready.emit(report)
        except Exception as exc:
            LOGGER.exception('Target discovery failed')
            self.failed.emit(str(exc))


class TargetDiscoveryController(QObject):
    """Keep worker ownership until queued finished delivery, like the planners.

    Results belong to one complete inventory/settings snapshot. They are not
    persisted as availability claims and selecting one never spends stock.
    """

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.worker = None
        self.dialog = None
        self.page = None
        self.report = None
        self.search_context = None
        self._result = None
        self._error = ''
        self._closed = False

    def settings(self, page):
        common = dict(clone_yield=page.clone_yield.value(), objective=page.objective.currentData(),
                      max_seconds=12, max_combinations=120000)
        if page is self.owner.parallel:
            return 'parallel', dict(common, box_count=page.box_count.value(),
                                    main_box=page.main_box.currentData() or 1,
                                    max_waves=page.max_waves.value())
        return 'sequential', dict(common, center_id=page.center_combo.currentData() or None,
                                  max_rounds=page.max_rounds.value())

    def context(self, page):
        mode, settings = self.settings(page)
        return (mode, tuple(self.owner.inventory), self.owner.project_crop,
                self.owner.current_project_id, self.owner.workspaces.currentIndex(),
                tuple(sorted(settings.items())))

    def _refresh_locks(self):
        self.owner.update_locks()
        self.owner.guided.render_round()
        self.owner.parallel.render_wave()

    def _valid_route(self, option):
        """Recheck the cached complete route before changing even the target."""
        mode, settings = self.settings(self.page)
        report, target = option.report, option.target
        stock = tuple(self.owner.inventory)
        if mode == 'parallel':
            if (not isinstance(report, ParallelReport)
                    or report.box_count != settings['box_count']
                    or report.main_box != settings['main_box']
                    or report.rounds > settings['max_waves']
                    or any(step.clone_yield != settings['clone_yield']
                           for wave in report.waves for step in wave.steps)):
                return False
            try:
                validate_parallel_report(report, stock, target)
            except ValueError:
                return False
            return True
        return (isinstance(report, RouteReport) and report.best is not None
                and report.best.target == target
                and _verified(report, stock, target,
                              center_id=None if option.status == 'already_owned' else settings['center_id'],
                              max_rounds=settings['max_rounds'], clone_yield=settings['clone_yield'],
                              max_donors=8))

    def open(self, page):
        if (self.worker is not None or self.owner.work_busy()
                or any(p.state.pending is not None for p in self.owner._pages)):
            page.status.setText('请先完成或撤销当前安排，再筛选优选组合。')
            return None
        if self.dialog is not None:
            self.dialog.reject()
        self.page = page
        self.owner.workspaces.setCurrentIndex(1 if page is self.owner.parallel else 0)
        self.search_context = self.context(page)
        self.report, self._result, self._error, self._closed = None, None, '', False
        mode, settings = self.settings(page)
        dialog = TargetSelectorDialog(parent=self.owner, mode=mode)
        self.dialog = dialog
        dialog.option_selected.connect(self.apply_option)
        dialog.cancel_requested.connect(self.cancel)
        dialog.finished.connect(self.dialog_finished)
        if not self.owner.inventory:
            dialog.set_empty_inventory()
            dialog.open()
            return dialog
        dialog.set_searching(True)
        worker = TargetDiscoveryWorker(self.owner.inventory, mode, settings, self)
        self.worker = worker
        worker.result_ready.connect(self.result_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self.failed, Qt.ConnectionType.QueuedConnection)
        worker.progress_changed.connect(self.progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self.finished, Qt.ConnectionType.QueuedConnection)
        self._refresh_locks()
        dialog.open()
        worker.start()
        return dialog

    @Slot()
    def cancel(self):
        if self.worker is not None:
            self.worker.requestInterruption()

    @Slot(int)
    def dialog_finished(self, _result):
        if self.sender() is not self.dialog:
            return
        self._closed = True
        self.cancel()
        if self.worker is None:
            self.dialog.deleteLater()
            self.dialog = None

    @Slot(object)
    def result_ready(self, report):
        if self.sender() is self.worker and self.worker is not None:
            self._result = report

    @Slot(str)
    def failed(self, message):
        if self.sender() is self.worker and self.worker is not None:
            self._error = message

    @Slot(int, int)
    def progress(self, completed, total):
        if self.sender() is self.worker and not self._closed and self.dialog is not None:
            self.dialog.update_progress(completed, total)

    @Slot()
    def finished(self):
        old = self.sender()
        if old is None:
            return
        old.deleteLater()
        if old is not self.worker:
            return
        self.worker = None
        self._refresh_locks()
        if self.dialog is None:
            return
        if self._closed:
            self.dialog.deleteLater()
            self.dialog = None
            return
        self.dialog.set_searching(False)
        if self.context(self.page) != self.search_context:
            self.dialog.show_error('基因库或规划设置已变化，请重新筛选。')
        elif self._error:
            self.dialog.show_error(self._error)
        elif self._result is not None:
            self.report = self._result
            self.dialog.set_options(self.report.options)
        else:
            self.dialog.show_error('筛选未完成，请重新尝试。')

    @Slot(object)
    def apply_option(self, option):
        if (self.worker is not None or self.dialog is None or self.report is None
                or not any(option is entry for entry in self.report.options)):
            return False
        if (self.owner.work_busy() or any(p.state.pending is not None for p in self.owner._pages)
                or self.context(self.page) != self.search_context):
            self.dialog.show_error('基因库或规划设置已变化，请重新筛选。')
            return False
        if not option.selectable or option.report is None or not option.report.found:
            return False
        if not self._valid_route(option):
            self.dialog.show_error('路线与当前基因库不一致，请重新筛选。')
            return False
        target = Target(option.target.genes, 'counts')
        if set(target.genes) - set('YG'):
            return False
        # Change the shared target once, without transient exact-order events.
        self.owner.target_edit.blockSignals(True)
        self.owner.mode_combo.blockSignals(True)
        self.owner.target_edit.setText(target.genes)
        self.owner.mode_combo.setCurrentIndex(self.owner.mode_combo.findData('counts'))
        self.owner.target_edit.blockSignals(False)
        self.owner.mode_combo.blockSignals(False)
        self.owner.target_changed()
        self.page.accept_route(option.report, self.page.context())
        self.dialog.accept()
        return True
