"""Actual-result driven breeding journeys, independent of the desktop UI.

Planning does not reserve or consume stock. Only accepting a completed round
does so, atomically, and the next recipient always comes from the observed
result rather than an outcome predicted by the model.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
import hashlib
import json
from typing import Any, Sequence
from uuid import uuid4

from .engine import predict
from .models import InventoryItem, Recipe, Target, normalize_genes
from .planter import positions_for


MAX_ITEMS = 5_000
MAX_QUANTITY = 1_000_000_000
MAX_ROUNDS = 1_000


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label}必须是 {minimum}–{maximum} 的整数。")
    return value


def _text(value: Any, label: str, maximum: int = 128, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"{label}必须是{'非空' if not empty else ''}文本，最多 {maximum} 个字符。")
    return value


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是对象。")
    return value


def _target(data: dict) -> Target:
    mode = data.get("target_mode", "counts")
    if not isinstance(mode, str) or mode not in ("counts", "exact"):
        raise ValueError("目标匹配方式只能为 exact 或 counts。")
    return Target(data.get("target"), mode)


def _rows(inventory: Sequence[InventoryItem]) -> tuple[InventoryItem, ...]:
    rows = tuple(inventory)
    if len(rows) > MAX_ITEMS:
        raise ValueError(f"种子库最多 {MAX_ITEMS} 项。")
    ids: set[str] = set()
    for item in rows:
        if not isinstance(item, InventoryItem):
            raise ValueError("种子库包含无效记录。")
        _text(item.id, "种子编号")
        _text(item.name, "种子名称", 200, empty=True)
        _integer(item.quantity, "可用数量", 1, MAX_QUANTITY)
        if item.id in ids:
            raise ValueError("每条库存的编号必须唯一。")
        ids.add(item.id)
    return rows


def _fingerprint(rows: Sequence[InventoryItem]) -> str:
    # Names and display ordering have no effect on stock availability.
    payload = sorted((item.id, item.genes, item.quantity) for item in rows)
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


def _stock_fingerprint(value: Any, label: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(ch not in "0123456789abcdef" for ch in value)):
        raise ValueError(f"{label}无效。")
    return value


@dataclass(frozen=True)
class PlantUse:
    id: str
    genes: str
    quantity: int = 1
    name: str = ""

    def __post_init__(self) -> None:
        _text(self.id, "种子编号")
        object.__setattr__(self, "genes", normalize_genes(self.genes))
        _integer(self.quantity, "本轮用量", 1, 8)
        _text(self.name, "种子名称", 200, empty=True)

    @property
    def label(self) -> str:
        return self.name or self.id

    def to_dict(self) -> dict:
        return {"id": self.id, "genes": self.genes, "quantity": self.quantity, "name": self.name}

    @classmethod
    def from_dict(cls, data: Any) -> PlantUse:
        data = _object(data, "用株记录")
        return cls(data.get("id"), data.get("genes"), data.get("quantity", 1), data.get("name", ""))


def _uses(center: PlantUse, donors: tuple[PlantUse, ...]) -> None:
    if center.quantity != 1:
        raise ValueError("每轮必须恰好有一株中央苗。")
    if len({item.id for item in donors}) != len(donors):
        raise ValueError("供体记录必须按库存编号合并。")
    if sum(item.quantity for item in donors) > 8:
        raise ValueError("每轮最多可放 8 株供体。")
    if any(item.id == center.id and item.genes != center.genes for item in donors):
        raise ValueError("同一库存编号的基因必须一致。")


@dataclass(frozen=True)
class PendingRound:
    token: str
    center: PlantUse
    donors: tuple[PlantUse, ...]
    inventory_fingerprint: str
    layout: str = "auto"

    def __post_init__(self) -> None:
        object.__setattr__(self, "donors", tuple(self.donors))
        _text(self.token, "本轮编号")
        _uses(self.center, self.donors)
        positions_for(sum(item.quantity for item in self.donors), self.layout)
        if (not isinstance(self.inventory_fingerprint, str)
                or len(self.inventory_fingerprint) != 64
                or any(ch not in "0123456789abcdef" for ch in self.inventory_fingerprint)):
            raise ValueError("本轮库存校验记录无效，请重新规划。")

    @property
    def center_id(self) -> str:
        return self.center.id

    @property
    def donor_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.donors for _ in range(item.quantity))

    @property
    def predicted_pattern(self) -> str:
        return self.prediction.result_pattern

    @property
    def prediction(self):
        return predict(self.center.genes, [item.genes for item in self.donors for _ in range(item.quantity)])

    def to_dict(self) -> dict:
        return {"token": self.token, "center": self.center.to_dict(),
                "donors": [item.to_dict() for item in self.donors],
                "inventory_fingerprint": self.inventory_fingerprint, "layout": self.layout}

    @classmethod
    def from_dict(cls, data: Any) -> PendingRound:
        data = _object(data, "本轮方案")
        donors = data.get("donors")
        if not isinstance(donors, list) or len(donors) > 8:
            raise ValueError("供体记录必须是最多 8 项的列表。")
        return cls(data.get("token"), PlantUse.from_dict(data.get("center")),
                   tuple(PlantUse.from_dict(item) for item in donors), data.get("inventory_fingerprint"),
                   data.get("layout", "cross"))


@dataclass(frozen=True)
class RoundRecord:
    number: int
    token: str
    center: PlantUse
    donors: tuple[PlantUse, ...]
    actual_genes: str
    clone_quantity: int
    result_center_id: str
    target: Target
    layout: str = "auto"

    def __post_init__(self) -> None:
        object.__setattr__(self, "donors", tuple(self.donors))
        _integer(self.number, "轮次", 1, MAX_ROUNDS)
        _text(self.token, "本轮编号")
        _uses(self.center, self.donors)
        positions_for(sum(item.quantity for item in self.donors), self.layout)
        object.__setattr__(self, "actual_genes", normalize_genes(self.actual_genes))
        _integer(self.clone_quantity, "实际克隆数量", 1, MAX_QUANTITY)
        _text(self.result_center_id, "下一轮中央苗编号")
        if not isinstance(self.target, Target):
            raise ValueError("本轮目标无效。")

    @property
    def observed_genes(self) -> str:
        return self.actual_genes

    @property
    def center_genes(self) -> str:
        return self.center.genes

    @property
    def donor_genes(self) -> tuple[str, ...]:
        return tuple(item.genes for item in self.donors for _ in range(item.quantity))

    @property
    def prediction_pattern(self) -> str:
        return self.predicted_pattern

    @property
    def predicted_pattern(self) -> str:
        return predict(self.center.genes,
                       [item.genes for item in self.donors for _ in range(item.quantity)]).result_pattern

    @property
    def target_matched(self) -> bool:
        return self.target.matches(self.actual_genes)

    @property
    def summary(self) -> str:
        donors = "、".join(f"{item.genes} × {item.quantity}" for item in self.donors) or "无供体"
        status = "已达标" if self.target_matched else "继续改良"
        return (f"第 {self.number} 轮：中央 {self.center.genes}；周围 {donors}；"
                f"预测 {self.predicted_pattern}；实测 {self.actual_genes}，"
                f"取得克隆 {self.clone_quantity} 株 · {status}")

    def to_dict(self) -> dict:
        return {"number": self.number, "token": self.token, "center": self.center.to_dict(),
                "donors": [item.to_dict() for item in self.donors], "actual_genes": self.actual_genes,
                "clone_quantity": self.clone_quantity, "result_center_id": self.result_center_id,
                "target": self.target.genes, "target_mode": self.target.mode,
                "layout": self.layout}

    @classmethod
    def from_dict(cls, data: Any) -> RoundRecord:
        data = _object(data, "历史轮次")
        donors = data.get("donors")
        if not isinstance(donors, list) or len(donors) > 8:
            raise ValueError("供体记录必须是最多 8 项的列表。")
        return cls(data.get("number"), data.get("token"), PlantUse.from_dict(data.get("center")),
                   tuple(PlantUse.from_dict(item) for item in donors), data.get("actual_genes"),
                   data.get("clone_quantity"), data.get("result_center_id"),
                   _target(data), data.get("layout", "cross"))


def _replay_record(inventory: Sequence[InventoryItem], record: RoundRecord) -> tuple[InventoryItem, ...]:
    """Reproduce a recorded debit/credit, including its existing result ID.

    This checks a saved recovery snapshot against what completion actually
    changed. It never invents stock by reversing an unverified old record.
    """
    rows = _rows(inventory)
    by_id = {row.id: row for row in rows}
    used = Counter({item.id: item.quantity for item in record.donors})
    used[record.center.id] += 1
    for use in (record.center, *record.donors):
        row = by_id.get(use.id)
        if row is None or row.genes != use.genes or row.quantity < used[use.id]:
            raise ValueError("撤回快照与本轮用株不一致。")
    remaining = [replace(row, quantity=row.quantity - used[row.id])
                 for row in rows if row.quantity > used[row.id]]
    for index, row in enumerate(remaining):
        if row.genes != record.actual_genes:
            continue
        if row.id != record.result_center_id:
            raise ValueError("撤回快照与本轮结果入库记录不一致。")
        quantity = _integer(row.quantity + record.clone_quantity, "合并后的克隆数量", 1, MAX_QUANTITY)
        remaining[index] = replace(row, quantity=quantity)
        return tuple(remaining)
    if record.result_center_id in by_id or len(remaining) >= MAX_ITEMS:
        raise ValueError("撤回快照与本轮结果入库记录不一致。")
    remaining.append(InventoryItem(record.result_center_id, record.actual_genes,
                                   record.clone_quantity, f"第 {record.number} 轮实测克隆"))
    return tuple(remaining)


@dataclass(frozen=True)
class UndoCheckpoint:
    """One verified completion boundary; this is deliberately not a history stack."""

    inventory_before: tuple[InventoryItem, ...]
    pending: PendingRound
    after_inventory_fingerprint: str
    completed_token: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "inventory_before", _rows(self.inventory_before))
        if not isinstance(self.pending, PendingRound):
            raise ValueError("撤回快照缺少原本的待完成方案。")
        _text(self.completed_token, "撤回轮次编号")
        _stock_fingerprint(self.after_inventory_fingerprint, "撤回后的库存校验记录")
        if self.pending.token != self.completed_token:
            raise ValueError("撤回快照的轮次编号不一致。")
        if self.pending.inventory_fingerprint != _fingerprint(self.inventory_before):
            raise ValueError("撤回快照的原库存校验失败。")

    def to_dict(self) -> dict:
        return {"inventory_before": [
                    {"id": row.id, "genes": row.genes, "quantity": row.quantity, "name": row.name}
                    for row in self.inventory_before],
                "pending": self.pending.to_dict(),
                "after_inventory_fingerprint": self.after_inventory_fingerprint,
                "completed_token": self.completed_token}

    @classmethod
    def from_dict(cls, data: Any) -> UndoCheckpoint:
        data = _object(data, "撤回快照")
        rows = data.get("inventory_before")
        if not isinstance(rows, list) or len(rows) > MAX_ITEMS:
            raise ValueError(f"撤回快照的库存必须是最多 {MAX_ITEMS} 项的列表。")
        inventory = []
        for row in rows:
            row = _object(row, "撤回库存记录")
            inventory.append(InventoryItem(_text(row.get("id"), "种子编号"), row.get("genes"),
                                           row.get("quantity", 1), row.get("name", "")))
        return cls(tuple(inventory), PendingRound.from_dict(data.get("pending")),
                   data.get("after_inventory_fingerprint"), data.get("completed_token"))


@dataclass(frozen=True)
class JourneyState:
    target: Target = field(default_factory=Target)
    center_id: str = ""
    rounds: tuple[RoundRecord, ...] = ()
    pending: PendingRound | None = None
    undo_checkpoint: UndoCheckpoint | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rounds", tuple(self.rounds))
        if not isinstance(self.target, Target):
            raise ValueError("路线目标无效。")
        _text(self.center_id, "中央苗编号", empty=True)
        if len(self.rounds) > MAX_ROUNDS:
            raise ValueError(f"每个项目最多记录 {MAX_ROUNDS} 轮。")
        tokens = set()
        for number, record in enumerate(self.rounds, 1):
            if not isinstance(record, RoundRecord) or record.number != number or record.token in tokens:
                raise ValueError("轮次记录重复或顺序不正确。")
            tokens.add(record.token)
        if self.pending is not None:
            if not isinstance(self.pending, PendingRound) or self.pending.token in tokens:
                raise ValueError("本轮已经记账，请勿重复提交。")
            if self.pending.center.id != self.center_id:
                raise ValueError("本轮方案与当前中央苗不一致。")
            if not self.pending.donors and not self.target.matches(self.pending.center.genes):
                raise ValueError("周围为空不能改良中央苗，请重新规划有效方案。")
        checkpoint = self.undo_checkpoint
        if checkpoint is not None:
            if not isinstance(checkpoint, UndoCheckpoint) or not self.rounds:
                raise ValueError("撤回快照没有对应的已完成轮次。")
            last = self.rounds[-1]
            if (checkpoint.completed_token != last.token
                    or checkpoint.pending.center != last.center
                    or checkpoint.pending.donors != last.donors
                    or checkpoint.pending.layout != last.layout):
                raise ValueError("撤回快照与最近完成的轮次不一致。")
            expected = _replay_record(checkpoint.inventory_before, last)
            if _fingerprint(expected) != checkpoint.after_inventory_fingerprint:
                raise ValueError("撤回快照的完成后库存校验失败。")

    @property
    def next_round(self) -> int:
        return len(self.rounds) + 1

    def to_dict(self) -> dict:
        data = {"version": 1, "target": self.target.genes, "target_mode": self.target.mode,
                "center_id": self.center_id, "rounds": [record.to_dict() for record in self.rounds],
                "pending": self.pending.to_dict() if self.pending is not None else None}
        if self.undo_checkpoint is not None:
            data["undo_checkpoint"] = self.undo_checkpoint.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Any) -> JourneyState:
        data = _object(data, "连续育种记录")
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("不支持的连续育种记录版本。")
        rounds = data.get("rounds", [])
        if not isinstance(rounds, list) or len(rounds) > MAX_ROUNDS:
            raise ValueError(f"历史轮次必须是最多 {MAX_ROUNDS} 项的列表。")
        pending = data.get("pending")
        checkpoint = data.get("undo_checkpoint")
        return cls(_target(data),
                   data.get("center_id", ""), tuple(RoundRecord.from_dict(item) for item in rounds),
                   PendingRound.from_dict(pending) if pending is not None else None,
                   UndoCheckpoint.from_dict(checkpoint) if checkpoint is not None else None)


@dataclass(frozen=True)
class RoundCompletion:
    state: JourneyState
    inventory: tuple[InventoryItem, ...]
    center_id: str
    record: RoundRecord


@dataclass(frozen=True)
class RoundUndo:
    state: JourneyState
    inventory: tuple[InventoryItem, ...]
    record: RoundRecord


def start_journey(center_id: str, target: Target) -> JourneyState:
    """Begin following one chosen recipient. No inventory is changed."""
    return JourneyState(target=target, center_id=_text(center_id, "中央苗编号"))


def prepare_round(state: JourneyState, inventory: Sequence[InventoryItem], recipe: Recipe) -> JourneyState:
    """Bind a stock-feasible recommendation to the selected recipient."""
    rows = _rows(inventory)
    if len(state.rounds) >= MAX_ROUNDS:
        raise ValueError(f"每个项目最多记录 {MAX_ROUNDS} 轮，请新建项目。")
    if recipe.target != state.target:
        raise ValueError("方案目标已变化，请重新规划。")
    if recipe.center_id != state.center_id:
        raise ValueError("方案替换了中央苗，请按选中的中央苗重新规划。")
    by_id = {item.id: item for item in rows}
    used = Counter([recipe.center_id, *recipe.donor_ids])
    if recipe.donor_count > 8:
        raise ValueError("每轮最多可放 8 株供体。")
    for item in (recipe.center, *recipe.donors):
        current = by_id.get(item.id)
        if current is None or current.genes != item.genes or current.quantity < used[item.id]:
            raise ValueError("方案引用的库存已经变化或数量不足，请重新规划。")
    if not recipe.donors and not state.target.matches(recipe.center.genes):
        raise ValueError("周围为空不能改良中央苗，请重新规划有效方案。")
    center_row = by_id[recipe.center_id]
    center = PlantUse(center_row.id, center_row.genes, 1, center_row.name)
    donors = tuple(PlantUse(seed_id, by_id[seed_id].genes, quantity, by_id[seed_id].name)
                   for seed_id, quantity in Counter(recipe.donor_ids).items())
    pending = PendingRound(uuid4().hex, center, donors, _fingerprint(rows))
    return replace(state, pending=pending)


def pending_recipe(state: JourneyState, inventory: Sequence[InventoryItem]) -> Recipe | None:
    """Rebuild a saved pending recommendation against the current stock.

    Missing or changed stock is an explicit error; callers can then clear the
    pending round and ask for a new route while retaining completed history.
    """
    if state.pending is None:
        return None
    rows = _rows(inventory)
    pending = state.pending
    if _fingerprint(rows) != pending.inventory_fingerprint:
        raise ValueError("本轮库存已经变化，请重新规划。")
    by_id = {item.id: item for item in rows}
    try:
        center = by_id[pending.center.id]
        donors = tuple(by_id[seed_id] for seed_id in pending.donor_ids)
    except KeyError as exc:
        raise ValueError("本轮引用的中央苗或供体已不在库存，请重新规划。") from exc
    expected = Counter(pending.donor_ids)
    expected[center.id] += 1
    for reference in (pending.center, *pending.donors):
        current = by_id[reference.id]
        if current.genes != reference.genes or current.quantity < expected[reference.id]:
            raise ValueError("本轮库存基因或数量已变化，请重新规划。")
    return Recipe(center, donors, pending.prediction, state.target)


def complete_round(
    state: JourneyState,
    inventory: Sequence[InventoryItem],
    observed: str,
    clone_quantity: int,
    *,
    token: str | None = None,
) -> RoundCompletion:
    """Accept real genes and clone count, returning all changes together.

    The input state and stock remain untouched on every success or failure.
    A supplied round token guards stale UI submissions; completed states have
    no pending round, so a second submission cannot charge the same plants.
    """
    pending = state.pending
    if pending is None:
        raise ValueError("没有待完成的本轮方案，或本轮已经记账，请先规划。")
    if token is not None and token != pending.token:
        raise ValueError("本轮方案已变化，请勿提交旧轮次。")
    actual = normalize_genes(observed)
    quantity = _integer(clone_quantity, "实际克隆数量", 1, MAX_QUANTITY)
    rows = _rows(inventory)
    if _fingerprint(rows) != pending.inventory_fingerprint:
        raise ValueError("本轮库存已经变化，请核对库存并重新规划，避免重复扣除。")
    # Saved references are data too. Revalidate the selected plants as well as
    # the stock fingerprint before a result is allowed to credit any clones.
    pending_recipe(state, rows)
    used = Counter({item.id: item.quantity for item in pending.donors})
    used[pending.center.id] += 1
    remaining = []
    for row in rows:
        left = row.quantity - used[row.id]
        if left < 0:
            raise ValueError("本轮用株超过实际库存，请重新规划。")
        if left:
            remaining.append(replace(row, quantity=left))
    result_center_id = ""
    for index, row in enumerate(remaining):
        if row.genes == actual:
            total = row.quantity + quantity
            _integer(total, "合并后的克隆数量", 1, MAX_QUANTITY)
            remaining[index] = replace(row, quantity=total)
            result_center_id = row.id
            break
    if not result_center_id:
        if len(remaining) >= MAX_ITEMS:
            raise ValueError(f"录入实测结果后将超过 {MAX_ITEMS} 项，请先整理库存。")
        ids = {row.id for row in rows}
        result_center_id = f"round-{uuid4().hex}"
        while result_center_id in ids:
            result_center_id = f"round-{uuid4().hex}"
        remaining.append(InventoryItem(result_center_id, actual, quantity, f"第 {state.next_round} 轮实测克隆"))
    record = RoundRecord(state.next_round, pending.token, pending.center, pending.donors,
                         actual, quantity, result_center_id, state.target, pending.layout)
    checkpoint = UndoCheckpoint(rows, pending, _fingerprint(remaining), pending.token)
    next_state = replace(state, center_id=result_center_id, rounds=(*state.rounds, record),
                         pending=None, undo_checkpoint=checkpoint)
    return RoundCompletion(next_state, tuple(remaining), result_center_id, record)


def attach_last_round_checkpoint(
    state: JourneyState,
    inventory: Sequence[InventoryItem],
    inventory_before: Sequence[InventoryItem],
) -> JourneyState:
    """Add recovery for an older completion using an independently saved stock.

    Callers must supply the actual pre-completion snapshot. Its forward replay
    must match the complete current stock, including IDs and quantities. This
    function changes neither inventory nor history and does no reverse guess.
    """
    if not state.rounds:
        raise ValueError("没有已完成的轮次可以恢复。")
    rows = _rows(inventory)
    before = _rows(inventory_before)
    record = state.rounds[-1]
    expected = _replay_record(before, record)
    if _fingerprint(expected) != _fingerprint(rows):
        raise ValueError("备份与本轮完成后的库存不一致，不能安全恢复撤回。")
    pending = PendingRound(record.token, record.center, record.donors, _fingerprint(before), record.layout)
    checkpoint = UndoCheckpoint(before, pending, _fingerprint(rows), record.token)
    return replace(state, undo_checkpoint=checkpoint)


def undo_last_round(
    state: JourneyState,
    inventory: Sequence[InventoryItem],
    *,
    token: str | None = None,
) -> RoundUndo:
    """Undo the latest verified completion and return to waiting for its result.

    Both outputs are immutable and returned together. Later plans spend no
    stock, so a pending preview may be replaced. Any stock edit after the
    completion blocks recovery instead of overwriting newly recorded plants.
    Names and display ordering remain freely editable.
    """
    checkpoint = state.undo_checkpoint
    if checkpoint is None or not state.rounds:
        raise ValueError("没有可撤回的最近一轮；旧项目需先核对原库存备份。")
    if token is not None and token != checkpoint.completed_token:
        raise ValueError("最近完成的轮次已变化，请勿撤回旧轮次。")
    rows = _rows(inventory)
    if _fingerprint(rows) != checkpoint.after_inventory_fingerprint:
        raise ValueError("完成本轮后库存已经变化，不能撤回，以免覆盖新苗或数量修改。")
    pending = checkpoint.pending
    if not pending.donors and not state.target.matches(pending.center.genes):
        raise ValueError("目标已经变化，原本无供体的轮次无法恢复，请先恢复原目标。")
    before_by_id = {row.id: row for row in checkpoint.inventory_before}
    # Preserve harmless name/order edits to surviving rows. Fully consumed
    # rows return with their saved names, after the surviving display rows.
    restored = [replace(before_by_id[row.id], name=row.name) for row in rows if row.id in before_by_id]
    restored_ids = {row.id for row in restored}
    restored.extend(row for row in checkpoint.inventory_before if row.id not in restored_ids)
    restored_state = replace(state, center_id=pending.center.id, rounds=state.rounds[:-1],
                             pending=pending, undo_checkpoint=None)
    return RoundUndo(restored_state, tuple(restored), state.rounds[-1])
