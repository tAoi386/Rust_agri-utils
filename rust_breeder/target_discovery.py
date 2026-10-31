"""Bounded discovery of all seven all-green Y/G count targets.

Each selectable entry carries an independently replayed complete route for the
same inventory snapshot. A bounded search failure is not impossibility proof.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from math import isfinite
from time import monotonic

from .engine import predict
from .journey import _rows
from .models import Recipe, Target
from .parallel_models import ParallelReport
from .routes import RouteReport, RouteStep, plan_route

TARGET_Y_COUNTS = (3, 4, 2, 5, 1, 6, 0)
STATUSES = frozenset(('already_owned', 'reachable', 'unknown', 'unreachable'))


def green_targets():
    return tuple(Target('Y' * count + 'G' * (6 - count), 'counts') for count in TARGET_Y_COUNTS)


@dataclass(frozen=True)
class TargetOption:
    target: Target
    status: str
    report: RouteReport | ParallelReport | None = None
    reason: str = ''

    def __post_init__(self):
        if not isinstance(self.target, Target) or self.target.mode != 'counts' or set(self.target.genes) - set('YG'):
            raise ValueError('筛选目标必须是六位 Y/G 数量组合。')
        if self.status not in STATUSES or not isinstance(self.reason, str) or len(self.reason) > 128:
            raise ValueError('无效的优选筛选状态。')
        if self.report is not None and not isinstance(self.report, (RouteReport, ParallelReport)):
            raise ValueError('无效的优选路线。')
        if self.status in ('already_owned', 'reachable'):
            if self.report is None or not self.report.found or self.report.cancelled:
                raise ValueError('可选择的优选组合必须有完整路线。')
            if self.status == 'already_owned' and (self.report.rounds or self.report.reason != 'already_target'):
                raise ValueError('已有优选株不能包含杂交步骤。')
            if self.status == 'reachable' and not self.report.rounds:
                raise ValueError('可培育组合必须包含杂交步骤。')
        elif self.report is not None and self.report.found:
            raise ValueError('未达成状态不能包含可执行路线。')

    @property
    def selectable(self):
        return self.status in ('already_owned', 'reachable') and self.report is not None and self.report.found

    @property
    def rounds(self):
        return self.report.rounds if self.report is not None and self.report.found else None

    @property
    def plant_count(self):
        return self.report.plant_count if self.report is not None and self.report.found else None

    def to_dict(self):
        report = self.report
        return {'target': asdict(self.target), 'status': self.status, 'reason': self.reason,
                'report_type': ('parallel' if isinstance(report, ParallelReport) else 'sequential') if report is not None else None,
                'report': report.to_dict() if isinstance(report, ParallelReport) else asdict(report) if report is not None else None}

    @classmethod
    def from_dict(cls, value):
        report = value.get('report')
        kind = value.get('report_type')
        if report is not None:
            if kind == 'parallel':
                report = ParallelReport.from_dict(report)
            elif kind == 'sequential':
                from .planner_process import _report_from_dict
                report = _report_from_dict(report)
            else:
                raise ValueError('未知的优选路线类型。')
        elif kind is not None:
            raise ValueError('优选路线缺少内容。')
        return cls(Target(**value['target']), value['status'], report, value.get('reason', ''))


@dataclass(frozen=True)
class TargetDiscoveryReport:
    options: tuple[TargetOption, ...]
    mode: str = 'sequential'
    evaluated: int = 0
    elapsed_seconds: float = 0.0
    cancelled: bool = False

    def __post_init__(self):
        object.__setattr__(self, 'options', tuple(self.options))
        if self.mode not in ('sequential', 'parallel'):
            raise ValueError('未知的筛选培育模式。')
        if len(self.options) != 7 or any(not isinstance(x, TargetOption) for x in self.options):
            raise ValueError('优选筛选必须包含七个 Y/G 组合。')
        if {x.target.genes.count('Y') for x in self.options} != set(range(7)):
            raise ValueError('优选筛选组合不能缺失或重复。')
        if type(self.evaluated) is not int or self.evaluated < 0 or type(self.cancelled) is not bool:
            raise ValueError('优选搜索计数或取消状态无效。')
        if isinstance(self.elapsed_seconds, bool) or not isinstance(self.elapsed_seconds, (int, float)) or not isfinite(self.elapsed_seconds) or self.elapsed_seconds < 0:
            raise ValueError('优选搜索耗时无效。')
        for option in self.options:
            if option.report is not None and isinstance(option.report, ParallelReport) != (self.mode == 'parallel'):
                raise ValueError('优选路线与培育模式不一致。')

    def to_dict(self):
        return {'options': [x.to_dict() for x in self.options], 'mode': self.mode,
                'evaluated': self.evaluated, 'elapsed_seconds': self.elapsed_seconds,
                'cancelled': self.cancelled}

    @classmethod
    def from_dict(cls, value):
        return cls(tuple(TargetOption.from_dict(x) for x in value['options']),
                   value.get('mode', 'sequential'), value.get('evaluated', 0),
                   value.get('elapsed_seconds', 0.0), value.get('cancelled', False))


def cancelled_discovery(mode='sequential', elapsed=0.0):
    return TargetDiscoveryReport(tuple(TargetOption(x, 'unknown', reason='cancelled') for x in green_targets()),
                                 mode, elapsed_seconds=elapsed, cancelled=True)


def _positional_failure(rows, target):
    # Crossbreeding cannot invent a letter at a locus absent from all initial
    # plants at that same locus, even after arbitrarily many model rounds.
    available = [set(row.genes[position] for row in rows) & set('YG') for position in range(6)]
    if any(not choices for choices in available):
        return 'missing_green_position'
    minimum = sum(choices == {'Y'} for choices in available)
    maximum = sum('Y' in choices for choices in available)
    if not minimum <= target.genes.count('Y') <= maximum:
        return 'missing_gene_positions'
    return ''


def _owned_report(rows, target, mode, settings):
    found = next((row for row in rows if target.matches(row.genes)), None)
    if found is None:
        return None
    if mode == 'parallel':
        return ParallelReport((), True, True, 0, 'already_target',
                              settings.get('box_count', 3), settings.get('main_box', 1),
                              settings.get('max_waves', 3), settings.get('clone_yield', 1),
                              minimum_proven=True, objective=settings.get('objective', 'stock'))
    # A fixed central preference affects breeding, but never makes an already
    # available matching clone disappear from the owned-target catalogue.
    recipe = Recipe(found, (), predict(found.genes, ()), target)
    return RouteReport((), (recipe,), True, True, 0, 'already_target',
                       minimum_proven=True, shortest_rounds_proven=True,
                       requested_max_rounds=settings.get('max_rounds', 3),
                       clone_yield=settings.get('clone_yield', 1), objective=settings.get('objective', 'stock'),
                       center_mode='auto')


def _validate_settings(mode, settings):
    from .planner_process import OPTION_NAMES, PARALLEL_OPTION_NAMES
    allowed = PARALLEL_OPTION_NAMES if mode == 'parallel' else OPTION_NAMES
    if mode not in ('sequential', 'parallel') or set(settings) - allowed:
        raise ValueError('未知的优选筛选参数。')
    seconds = settings.get('max_seconds', 12.0)
    budget = settings.get('max_combinations', 120000)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not isfinite(seconds) or not 0 < seconds <= 120:
        raise ValueError('筛选时间必须在 0–120 秒内。')
    if type(budget) is not int or not 1 <= budget <= 10000000:
        raise ValueError('筛选组合上限无效。')
    for key, default, low, high in (('clone_yield', 1, 1, 1000000000),
                                   ('max_donors', 8, 0, 8)):
        value = settings.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError('优选筛选的克隆数量或供体上限无效。')
    if settings.get('objective', 'stock') not in ('stock', 'rounds', 'plants'):
        raise ValueError('优选筛选的优化方式无效。')
    if mode == 'parallel':
        for key, default, low, high in (('box_count', 3, 1, 6), ('max_waves', 3, 1, 6),
                                      ('main_box', 1, 1, settings.get('box_count', 3)),
                                      ('catalog_limit', 120, 8, 1000), ('donor_pool_limit', 16, 2, 100)):
            value = settings.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError('优选筛选的并行参数无效。')
    else:
        for key, default in (('max_rounds', 3), ('beam_width', 24), ('donor_pool_limit', 14)):
            value = settings.get(key, default)
            if type(value) is not int or value < 1:
                raise ValueError('优选筛选轮数与样本上限必须是正整数。')
    return float(seconds), budget



def _candidate_rank(report, rows, objective):
    if objective == 'stock':
        from .stock_planning import stock_route_cost
        entries = (step for wave in report.waves for step in wave.steps) if isinstance(report, ParallelReport) else report.steps
        return stock_route_cost(entries, rows), report.rounds, report.plant_count
    if objective == 'plants':
        return report.plant_count, report.rounds
    return report.rounds, report.plant_count


def _reuse_candidates(options, report, rows, mode, settings):
    """Rebind and replay useful all-green route prefixes for other cards.

    A plan may create a useful Y/G plant before reaching its requested goal.
    Reusing that legal prefix saves a separate target search, but it is still
    only a bounded candidate, never a new optimality or probability claim.
    """
    by_count = {option.target.genes.count('Y'): index for index, option in enumerate(options)}
    candidates = []
    if mode == 'sequential':
        from .stock_planning import _verified
        for depth, step in enumerate(report.steps, 1):
            if set(step.expected_genes) <= set('YG'):
                target = Target('Y' * step.expected_genes.count('Y') + 'G' * step.expected_genes.count('G'))
                steps = tuple(replace(x, recipe=replace(x.recipe, target=target)) for x in report.steps[:depth])
                candidates.append((target, replace(report, steps=steps, recipes=(steps[0].recipe,), found=True,
                    complete=False, reason='found', minimum_proven=False, shortest_rounds_proven=False)))
        # First-round suggestions are not themselves full routes to the
        # original target. A deterministic all-green suggestion can however
        # be a complete route to a different card after a real stock replay.
        for recipe in report.recipes:
            outcome = recipe.prediction.deterministic_result
            if not recipe.donors or outcome is None or set(outcome) - set('YG'):
                continue
            target = Target('Y' * outcome.count('Y') + 'G' * outcome.count('G'))
            output = '__discovery_clone_1'
            while any(row.id == output for row in rows):
                output += '_'
            recipe = replace(recipe, target=target)
            step = RouteStep(recipe, outcome, output, settings.get('clone_yield', 1))
            candidates.append((target, replace(report, steps=(step,), recipes=(recipe,), found=True,
                complete=False, reason='found', minimum_proven=False, shortest_rounds_proven=False)))
        candidates = [(target, candidate) for target, candidate in candidates if _verified(candidate, rows, target,
            center_id=settings.get('center_id'), max_rounds=settings.get('max_rounds', 3),
            clone_yield=settings.get('clone_yield', 1), max_donors=settings.get('max_donors', 8))]
    else:
        from .parallel import validate_parallel_report
        from .stock_planning import stock_route_cost
        for depth, wave in enumerate(report.waves, 1):
            for step in wave.steps:
                # Preserve the user's named main planter for the final goal.
                if step.box_id != report.main_box or set(step.expected_genes) - set('YG'):
                    continue
                target = Target('Y' * step.expected_genes.count('Y') + 'G' * step.expected_genes.count('G'))
                waves = tuple(replace(w, steps=tuple(replace(x, recipe=replace(x.recipe, target=target)) for x in w.steps))
                              for w in report.waves[:depth])
                candidate = replace(report, waves=waves, found=True, complete=False, reason='found',
                                    minimum_proven=False, stock_cost=float(stock_route_cost(
                                        (x for w in waves for x in w.steps), rows)))
                validate_parallel_report(candidate, rows, target)
                candidates.append((target, candidate))
    changed = set()
    for target, candidate in candidates:
        index = by_count[target.genes.count('Y')]
        current = options[index]
        if current.status in ('already_owned', 'unreachable'):
            continue
        if current.report is not None and current.selectable and _candidate_rank(current.report, rows, settings.get('objective', 'stock')) <= _candidate_rank(candidate, rows, settings.get('objective', 'stock')):
            continue
        options[index] = TargetOption(target, 'reachable', candidate, 'verified_prefix')
        changed.add(index)
    return changed


def plan_targets(inventory, *, mode='sequential', cancel=None, progress=None, **settings):
    """Search seven targets fairly inside one total time/combination budget."""
    seconds, budget = _validate_settings(mode, settings)
    rows = _rows(inventory)
    if mode == 'sequential' and settings.get('center_id') is not None and not any(row.id == str(settings['center_id']) for row in rows):
        raise ValueError('指定的中心株不在当前可用库存中。')
    started = monotonic()
    if cancel and cancel():
        return cancelled_discovery(mode)
    options = []
    pending = []
    evaluated = 0
    cancelled = False
    for target in green_targets():
        report = _owned_report(rows, target, mode, settings)
        failure = _positional_failure(rows, target)
        if report is not None:
            option = TargetOption(target, 'already_owned', report, 'already_target')
        elif not rows:
            option = TargetOption(target, 'unreachable', reason='empty_inventory')
        elif failure:
            option = TargetOption(target, 'unreachable', reason=failure)
        elif sum(row.quantity for row in rows) < 2:
            option = TargetOption(target, 'unreachable', reason='insufficient_plants')
        else:
            option = TargetOption(target, 'unknown', reason='not_searched')
            pending.append(len(options))
        options.append(option)
    finished = {index for index, option in enumerate(options) if option.status != 'unknown'}
    if progress:
        progress(len(finished), 7)
    for offset, index in enumerate(pending):
        if options[index].selectable:
            continue
        if cancel and cancel():
            cancelled = True
            break
        remaining_time = seconds - (monotonic() - started)
        remaining_combinations = budget - evaluated
        if remaining_time <= 0 or remaining_combinations <= 0:
            break
        share = sum(not options[item].selectable for item in pending[offset:])
        search_options = dict(settings, objective=settings.get('objective', 'stock'),
                              max_seconds=remaining_time / share,
                              max_combinations=max(1, remaining_combinations // share))
        target = options[index].target
        if mode == 'parallel':
            from .parallel import plan_parallel, validate_parallel_report
            report = plan_parallel(rows, target, cancel=cancel, **search_options)
            if report.found:
                validate_parallel_report(report, rows, target)
        else:
            from .stock_planning import _verified
            report = plan_route(rows, target, cancel=cancel, **search_options)
            if report.found and not _verified(report, rows, target,
                    center_id=search_options.get('center_id'), max_rounds=search_options.get('max_rounds', 3),
                    clone_yield=search_options.get('clone_yield', 1), max_donors=search_options.get('max_donors', 8)):
                raise ValueError('优选路线未通过实际库存验证。')
        evaluated += report.evaluated
        if report.cancelled or (cancel and cancel()):
            cancelled = True
            break
        if report.found:
            status = 'already_owned' if report.reason == 'already_target' else 'reachable'
        else:
            # Even a complete depth-limited search is not proof of permanent
            # impossibility. Only the positional/stock necessary conditions
            # above receive the stronger unreachable label.
            status = 'unknown'
        options[index] = TargetOption(target, status, report,
                                      report.reason if report.found else 'search_limit')
        finished.add(index)
        finished.update(_reuse_candidates(options, report, rows, mode, settings))
        if progress:
            progress(len(finished), 7)
    if cancelled:
        options = [TargetOption(x.target, 'unknown', reason='cancelled') if x.reason == 'not_searched' else x for x in options]
    else:
        options = [TargetOption(x.target, 'unknown', reason='search_limit') if x.reason == 'not_searched' else x for x in options]
        if progress:
            progress(7, 7)
    return TargetDiscoveryReport(tuple(options), mode, evaluated, monotonic() - started, cancelled)
