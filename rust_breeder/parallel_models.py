"""Atomic, serializable batches for several independent breeding boxes.

All recipients and donors are paid from the stock at the start of a batch.
No output in that batch can pay for another box in the same batch.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from math import isfinite
from typing import Mapping, Sequence
from uuid import uuid4

from .engine import predict
from .journey import (_fingerprint, _integer, _object, _rows, _text,
                      _stock_fingerprint, MAX_ITEMS, MAX_QUANTITY, MAX_ROUNDS)
from .models import InventoryItem, Recipe, Target, normalize_genes


def recipe_to_dict(recipe: Recipe) -> dict:
    # Predictions are rebuilt from the ingredients, never trusted from a file.
    return {'center': asdict(recipe.center), 'donors': [asdict(x) for x in recipe.donors],
            'target': asdict(recipe.target)}


def _item(data) -> InventoryItem:
    data = _object(data, '种苗记录')
    return InventoryItem(_text(data.get('id'), '种苗编号'), data.get('genes'),
                         _integer(data.get('quantity', 1), '种苗数量', 1, MAX_QUANTITY),
                         _text(data.get('name', ''), '种苗名称', 200, empty=True))


def recipe_from_dict(data) -> Recipe:
    data = _object(data, '并行配种方案')
    donors = data.get('donors')
    if not isinstance(donors, list) or len(donors) > 8:
        raise ValueError('每个箱子的供体必须是最多 8 株的列表。')
    center = _item(data.get('center'))
    donors = tuple(_item(x) for x in donors)
    target = Target(**_object(data.get('target'), '配种目标'))
    return Recipe(center, donors, predict(center.genes, [x.genes for x in donors]), target)


@dataclass(frozen=True)
class ParallelStep:
    box_id: int
    recipe: Recipe
    expected_genes: str
    output_id: str
    clone_yield: int = 1

    def __post_init__(self):
        _integer(self.box_id, '箱子编号', 1, 6)
        _text(self.output_id, '产物编号')
        _integer(self.clone_yield, '预计克隆数', 1, MAX_QUANTITY)
        object.__setattr__(self, 'expected_genes', normalize_genes(self.expected_genes))
        if not isinstance(self.recipe, Recipe) or len(self.recipe.donors) > 8:
            raise ValueError('并行箱子配种方案无效。')
        expected = predict(self.recipe.center.genes, [x.genes for x in self.recipe.donors])
        if expected != self.recipe.prediction or expected.deterministic_result != self.expected_genes:
            raise ValueError('并行方案的预测与实际用苗不一致，或含有不确定结果。')

    def to_dict(self):
        return {'box_id': self.box_id, 'recipe': recipe_to_dict(self.recipe),
                'expected_genes': self.expected_genes, 'output_id': self.output_id,
                'clone_yield': self.clone_yield}

    @classmethod
    def from_dict(cls, data):
        data = _object(data, '并行箱子方案')
        return cls(data.get('box_id'), recipe_from_dict(data.get('recipe')),
                   data.get('expected_genes'), data.get('output_id'), data.get('clone_yield', 1))


@dataclass(frozen=True)
class ParallelWave:
    number: int
    steps: tuple[ParallelStep, ...]

    def __post_init__(self):
        _integer(self.number, '并行批次', 1, MAX_ROUNDS)
        object.__setattr__(self, 'steps', tuple(self.steps))
        if not 1 <= len(self.steps) <= 6 or any(not isinstance(x, ParallelStep) for x in self.steps):
            raise ValueError('每批必须安排 1–6 个箱子。')
        if len({x.box_id for x in self.steps}) != len(self.steps):
            raise ValueError('同一批不能重复安排同一个箱子。')
        if len({x.output_id for x in self.steps}) != len(self.steps):
            raise ValueError('并行产物编号不能重复。')
        outputs = {x.output_id for x in self.steps}
        if any(x.id in outputs for step in self.steps for x in (step.recipe.center, *step.recipe.donors)):
            raise ValueError('本批产物只能从下一批开始使用，不能在同批内借用。')

    @property
    def plant_count(self):
        return sum(x.recipe.plant_count for x in self.steps)

    def to_dict(self):
        return {'number': self.number, 'steps': [x.to_dict() for x in self.steps]}

    @classmethod
    def from_dict(cls, data):
        data = _object(data, '并行批次方案')
        steps = data.get('steps')
        if not isinstance(steps, list) or len(steps) > 6:
            raise ValueError('并行批次最多安排 6 个箱子。')
        return cls(data.get('number'), tuple(ParallelStep.from_dict(x) for x in steps))


@dataclass(frozen=True)
class ParallelReport:
    waves: tuple[ParallelWave, ...]
    found: bool
    complete: bool
    evaluated: int
    reason: str
    box_count: int = 3
    main_box: int = 1
    requested_max_waves: int = 3
    clone_yield: int = 1
    elapsed_seconds: float = 0.0
    limits: tuple[str, ...] = ()
    minimum_proven: bool = False
    objective: str = 'rounds'
    stock_cost: float = 0.0

    def __post_init__(self):
        object.__setattr__(self, 'waves', tuple(self.waves))
        object.__setattr__(self, 'limits', tuple(self.limits))
        _integer(self.box_count, '种植箱数量', 1, 6)
        _integer(self.main_box, '主箱编号', 1, self.box_count)
        _integer(self.requested_max_waves, '计划批次上限', 1, 6)
        _integer(self.clone_yield, '预计克隆数', 1, MAX_QUANTITY)
        _integer(self.evaluated, '已检查组合数', 0, 10_000_000)
        _text(self.reason, '搜索结果原因', 64)
        if any(type(x) is not bool for x in (self.found, self.complete, self.minimum_proven)):
            raise ValueError('并行搜索状态必须为布尔值。')
        if (len(self.waves) > self.requested_max_waves
                or any(not isinstance(x, ParallelWave) or x.number != number
                       for number, x in enumerate(self.waves, 1))):
            raise ValueError('并行搜索批次顺序或数量无效。')
        if (bool(self.waves) and not self.found) or (self.found and not self.waves and self.reason != 'already_target'):
            raise ValueError('并行搜索没有完整方案，不能标记为已找到。')
        if self.objective not in ('rounds', 'plants', 'stock'):
            raise ValueError('并行优化方式无效。')
        if len(self.limits) > 32:
            raise ValueError('搜索范围限制过多。')
        for label in self.limits:
            _text(label, '搜索范围限制', 128)
        for value in (self.elapsed_seconds, self.stock_cost):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not isfinite(value) or value < 0:
                raise ValueError('搜索耗时或库存成本无效。')

    @property
    def rounds(self):
        return len(self.waves)

    @property
    def plant_count(self):
        return sum(x.plant_count for x in self.waves)

    @property
    def cancelled(self):
        return self.reason == 'cancelled'

    @property
    def summary(self):
        if self.reason == 'already_target':
            return '库存中已有达标株，无需杂交。'
        if self.cancelled:
            return f'已停止计算，库存未改变；已检查 {self.evaluated:,} 个组合。'
        if self.found:
            strategy = {'rounds': '优先少批次', 'plants': '优先少用苗',
                        'stock': '智能匹配（基于已有基因库）'}.get(self.objective, '优先少批次')
            return (f'找到 {self.rounds} 批、累计用 {self.plant_count} 株的完整方案；'
                    f'最多同时使用 {self.box_count} 个箱子；{strategy}。当前方案未证明全局最优，'
                    f'每箱按获得 {self.clone_yield} 株克隆预估，逐批实测确认。')
        return (f'已检查 {self.evaluated:,} 个组合，暂未找到 {self.requested_max_waves} 批内的完整并行路线。'
                '搜索有范围限制，可增加样本或批次数；库存未改变。')

    def to_dict(self):
        return {**asdict(self), 'waves': [x.to_dict() for x in self.waves]}

    @classmethod
    def from_dict(cls, data):
        data = _object(data, '并行搜索结果')
        values = dict(data)
        waves = values.get('waves')
        if not isinstance(waves, list) or len(waves) > 12:
            raise ValueError('并行路线批次数无效。')
        values['waves'] = tuple(ParallelWave.from_dict(x) for x in waves)
        values['limits'] = tuple(values.get('limits', ()))
        return cls(**values)


@dataclass(frozen=True)
class PendingWave:
    token: str
    wave: ParallelWave
    inventory_fingerprint: str

    def __post_init__(self):
        _text(self.token, '并行批次编号')
        if not isinstance(self.wave, ParallelWave):
            raise ValueError('待确认批次无效。')
        _stock_fingerprint(self.inventory_fingerprint, '并行库存校验')

    def to_dict(self):
        return {'token': self.token, 'wave': self.wave.to_dict(),
                'inventory_fingerprint': self.inventory_fingerprint}

    @classmethod
    def from_dict(cls, data):
        data = _object(data, '待确认并行批次')
        return cls(data.get('token'), ParallelWave.from_dict(data.get('wave')),
                   data.get('inventory_fingerprint'))


@dataclass(frozen=True)
class ParallelActual:
    box_id: int
    genes: str
    quantity: int

    def __post_init__(self):
        _integer(self.box_id, '实测箱号', 1, 6)
        object.__setattr__(self, 'genes', normalize_genes(self.genes))
        _integer(self.quantity, '实际克隆数', 1, MAX_QUANTITY)


@dataclass(frozen=True)
class WaveRecord:
    number: int
    token: str
    wave: ParallelWave
    actuals: tuple[ParallelActual, ...]

    def __post_init__(self):
        _integer(self.number, '完成批次号', 1, MAX_ROUNDS)
        _text(self.token, '完成批次编号')
        object.__setattr__(self, 'actuals', tuple(self.actuals))
        if not isinstance(self.wave, ParallelWave) or self.wave.number != self.number:
            raise ValueError('完成批次顺序不一致。')
        if (any(not isinstance(x, ParallelActual) for x in self.actuals)
                or len(self.actuals) != len(self.wave.steps)
                or {x.box_id for x in self.actuals} != {x.box_id for x in self.wave.steps}):
            raise ValueError('必须记录本批每个箱子的实测结果，且箱号不得重复。')
        by_box = {x.box_id: x for x in self.actuals}
        if any(by_box[x.box_id].genes != x.expected_genes for x in self.wave.steps):
            raise ValueError('实际基因与六位预测不一致，不能进入下一批。')

    @property
    def summary(self):
        return f'第 {self.number} 批：' + '；'.join(f'{x.box_id} 号箱 {x.genes} × {x.quantity}' for x in self.actuals)

    def to_dict(self):
        return {'number': self.number, 'token': self.token, 'wave': self.wave.to_dict(),
                'actuals': [asdict(x) for x in self.actuals]}

    @classmethod
    def from_dict(cls, data):
        data = _object(data, '已完成并行批次')
        actuals = data.get('actuals')
        if not isinstance(actuals, list) or len(actuals) > 6:
            raise ValueError('并行实测记录无效。')
        return cls(data.get('number'), data.get('token'), ParallelWave.from_dict(data.get('wave')),
                   tuple(ParallelActual(**_object(x, '箱子实测')) for x in actuals))


def _validate_wave(wave, inventory, target, box_count):
    by_id = {x.id: x for x in inventory}
    used = Counter()
    for step in wave.steps:
        if step.box_id > box_count or step.recipe.target != target:
            raise ValueError('箱子配置或配种目标已改变，请重新规划。')
        if step.output_id in by_id:
            raise ValueError('并行产物编号已经存在，请重新规划。')
        for row in (step.recipe.center, *step.recipe.donors):
            actual = by_id.get(row.id)
            if actual is None or actual.genes != row.genes:
                raise ValueError('本批用苗已不在库存或基因改变，请重新规划。')
            used[row.id] += 1
    if any(by_id[seed_id].quantity < amount for seed_id, amount in used.items()):
        raise ValueError('多个箱子合计用苗超过共享库存，不能重复使用同一株苗。')
    return used


def _apply(inventory, record):
    used = _validate_wave(record.wave, inventory, record.wave.steps[0].recipe.target, 6)
    rows = [replace(x, quantity=x.quantity - used[x.id]) for x in inventory if x.quantity > used[x.id]]
    actuals = {x.box_id: x for x in record.actuals}
    for step in record.wave.steps:
        actual = actuals[step.box_id]
        rows.append(InventoryItem(step.output_id, actual.genes, actual.quantity,
                                  f'第 {record.number} 批 · {step.box_id} 号箱克隆'))
    return _rows(rows)


@dataclass(frozen=True)
class ParallelUndo:
    inventory_before: tuple[InventoryItem, ...]
    pending: PendingWave
    after_inventory_fingerprint: str

    def __post_init__(self):
        object.__setattr__(self, 'inventory_before', _rows(self.inventory_before))
        _stock_fingerprint(self.after_inventory_fingerprint, '撤回后的库存校验')
        if not isinstance(self.pending, PendingWave):
            raise ValueError('并行撤回快照缺少原待完成批次。')
        if self.pending.inventory_fingerprint != _fingerprint(self.inventory_before):
            raise ValueError('并行撤回快照的原库存校验失败。')

    def to_dict(self):
        return {'inventory_before': [asdict(x) for x in self.inventory_before],
                'pending': self.pending.to_dict(), 'after_inventory_fingerprint': self.after_inventory_fingerprint}

    @classmethod
    def from_dict(cls, data):
        data = _object(data, '并行撤回快照')
        rows = data.get('inventory_before')
        if not isinstance(rows, list) or len(rows) > MAX_ITEMS:
            raise ValueError('并行撤回库存无效。')
        return cls(tuple(_item(x) for x in rows), PendingWave.from_dict(data.get('pending')),
                   data.get('after_inventory_fingerprint'))


@dataclass(frozen=True)
class ParallelState:
    target: Target = field(default_factory=Target)
    box_count: int = 3
    main_box: int = 1
    waves: tuple[WaveRecord, ...] = ()
    pending: PendingWave | None = None
    undo_checkpoint: ParallelUndo | None = None

    def __post_init__(self):
        _integer(self.box_count, '种植箱数量', 1, 6)
        _integer(self.main_box, '主箱编号', 1, self.box_count)
        if not isinstance(self.target, Target):
            raise ValueError('并行培育目标无效。')
        object.__setattr__(self, 'waves', tuple(self.waves))
        if len(self.waves) > MAX_ROUNDS:
            raise ValueError('并行历史记录过多，请新建项目。')
        tokens = set()
        outputs = set()
        for number, record in enumerate(self.waves, 1):
            if not isinstance(record, WaveRecord) or record.number != number or record.token in tokens:
                raise ValueError('并行历史批次重复或顺序无效。')
            tokens.add(record.token)
            new_outputs = {x.output_id for x in record.wave.steps}
            if outputs & new_outputs:
                raise ValueError('历史并行批次重复使用产物编号。')
            outputs.update(new_outputs)
        if self.pending is not None:
            if not isinstance(self.pending, PendingWave):
                raise ValueError('待完成并行批次无效。')
            if self.pending.token in tokens or self.pending.wave.number != self.next_wave:
                raise ValueError('并行批次已经确认或顺序不正确。')
            if any(x.output_id in outputs for x in self.pending.wave.steps):
                raise ValueError('待完成批次与历史产物编号重复。')
            if any(x.box_id > self.box_count or x.recipe.target != self.target for x in self.pending.wave.steps):
                raise ValueError('待完成批次与当前箱子配置或目标不一致。')
        checkpoint = self.undo_checkpoint
        if checkpoint is not None:
            if not isinstance(checkpoint, ParallelUndo):
                raise ValueError('并行撤回快照无效。')
            if not self.waves or checkpoint.pending.token != self.waves[-1].token or checkpoint.pending.wave != self.waves[-1].wave:
                raise ValueError('并行撤回快照与最新完成批次不一致。')
            replay = _apply(checkpoint.inventory_before, self.waves[-1])
            if _fingerprint(replay) != checkpoint.after_inventory_fingerprint:
                raise ValueError('并行撤回快照完成后的库存校验失败。')

    @property
    def next_wave(self):
        return len(self.waves) + 1

    def to_dict(self):
        return {'version': 1, 'target': asdict(self.target), 'box_count': self.box_count,
                'main_box': self.main_box, 'waves': [x.to_dict() for x in self.waves],
                'pending': self.pending.to_dict() if self.pending else None,
                'undo_checkpoint': self.undo_checkpoint.to_dict() if self.undo_checkpoint else None}

    @classmethod
    def from_dict(cls, data):
        data = _object(data, '多线培育记录')
        if type(data.get('version')) is not int or data['version'] != 1:
            raise ValueError('不支持的多线培育版本。')
        waves = data.get('waves', [])
        if not isinstance(waves, list) or len(waves) > MAX_ROUNDS:
            raise ValueError('并行历史批次无效。')
        return cls(Target(**_object(data.get('target'), '配种目标')), data.get('box_count', 3),
                   data.get('main_box', 1), tuple(WaveRecord.from_dict(x) for x in waves),
                   PendingWave.from_dict(data['pending']) if data.get('pending') is not None else None,
                   ParallelUndo.from_dict(data['undo_checkpoint']) if data.get('undo_checkpoint') is not None else None)


@dataclass(frozen=True)
class ParallelCompletion:
    state: ParallelState
    inventory: tuple[InventoryItem, ...]
    record: WaveRecord


def prepare_wave(state: ParallelState, inventory: Sequence[InventoryItem], wave: ParallelWave) -> ParallelState:
    rows = _rows(inventory)
    if state.pending is not None:
        raise ValueError('已有待完成的并行批次，请先完成或撤销。')
    if state.next_wave > MAX_ROUNDS:
        raise ValueError('已达到批次记录上限，请新建项目。')
    # Search reports use relative wave numbers. Bind the first recommendation
    # to this project's next real batch without renumbering its ingredient IDs.
    wave = replace(wave, number=state.next_wave)
    _validate_wave(wave, rows, state.target, state.box_count)
    return replace(state, pending=PendingWave(uuid4().hex, wave, _fingerprint(rows)))


def current_wave(state: ParallelState, inventory: Sequence[InventoryItem]) -> ParallelWave | None:
    if state.pending is None:
        return None
    rows = _rows(inventory)
    if _fingerprint(rows) != state.pending.inventory_fingerprint:
        raise ValueError('待完成批次的库存已经变化，请重新规划。')
    _validate_wave(state.pending.wave, rows, state.target, state.box_count)
    return state.pending.wave


def complete_wave(state: ParallelState, inventory: Sequence[InventoryItem],
                  actuals: Mapping[int, tuple[str, int]], *, token: str | None = None) -> ParallelCompletion:
    rows = _rows(inventory)
    wave = current_wave(state, rows)
    if wave is None:
        raise ValueError('没有待完成的并行批次，请勿重复确认。')
    if token is not None and token != state.pending.token:
        raise ValueError('本批方案已改变，请勿提交旧结果。')
    if not isinstance(actuals, Mapping) or set(actuals) != {x.box_id for x in wave.steps}:
        raise ValueError('必须填写本批每个箱子的实际基因和克隆数后才能确认。')
    observations = []
    for box_id, value in actuals.items():
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValueError('箱子实测必须包含基因和克隆数量。')
        observations.append(ParallelActual(box_id, value[0], value[1]))
    record = WaveRecord(state.next_wave, state.pending.token, wave, tuple(sorted(observations, key=lambda x: x.box_id)))
    after = _apply(rows, record)
    checkpoint = ParallelUndo(rows, state.pending, _fingerprint(after))
    return ParallelCompletion(replace(state, waves=(*state.waves, record), pending=None,
                                      undo_checkpoint=checkpoint), after, record)


def cancel_wave(state: ParallelState) -> ParallelState:
    return replace(state, pending=None)


def undo_wave(state: ParallelState, inventory: Sequence[InventoryItem], *, token: str | None = None) -> ParallelCompletion:
    checkpoint = state.undo_checkpoint
    if checkpoint is None or not state.waves:
        raise ValueError('没有可以撤回的最近一批。')
    if token is not None and token != checkpoint.pending.token:
        raise ValueError('最近完成的批次已改变。')
    rows = _rows(inventory)
    if _fingerprint(rows) != checkpoint.after_inventory_fingerprint:
        raise ValueError('完成后库存已经变化，不能撤回覆盖新的用苗记录。')
    before_by_id = {x.id: x for x in checkpoint.inventory_before}
    restored = [replace(before_by_id[x.id], name=x.name) for x in rows if x.id in before_by_id]
    restored_ids = {x.id for x in restored}
    restored.extend(x for x in checkpoint.inventory_before if x.id not in restored_ids)
    pending = checkpoint.pending
    if any(x.box_id > state.box_count or x.recipe.target != state.target for x in pending.wave.steps):
        raise ValueError('目标或箱子配置已经改变，无法恢复原批次。')
    return ParallelCompletion(replace(state, waves=state.waves[:-1], pending=pending,
                                      undo_checkpoint=None), tuple(restored), state.waves[-1])
