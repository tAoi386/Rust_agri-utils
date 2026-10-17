"""Stage screen-recognized genes without counting repeated frames as stock.

Recognition observations are deliberately separate from inventory quantities.
A session keeps one candidate per ordered six-gene sequence, and an import plan
only adds sequences that are not already present in the seed library.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable
import uuid

from .models import InventoryItem, normalize_genes
from .storage import MAX_INVENTORY_ITEMS


MAX_QUANTITY = 1_000_000_000


def _normalize_sequence(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("识别基因必须是文本。")
    return normalize_genes(value)


def _normalize_batch(sequences: Iterable[str]) -> tuple[str, ...]:
    if isinstance(sequences, (str, bytes)):
        raise ValueError("请提供基因序列列表，每项为一株的 6 位基因。")
    return tuple(_normalize_sequence(value) for value in sequences)


@dataclass(frozen=True)
class CaptureCandidate:
    genes: str
    occurrences: int


class CaptureSession:
    """Accumulate first-seen candidates with exact positional deduplication.

    ``occurrences`` is an observation count for review only. It must never be
    used as the quantity to import. Each batch is validated before any change.
    """

    def __init__(self) -> None:
        self._observations: Counter[str] = Counter()

    @property
    def candidates(self) -> tuple[CaptureCandidate, ...]:
        return tuple(CaptureCandidate(genes, count) for genes, count in self._observations.items())

    def recognize_many(self, sequences: Iterable[str]) -> int:
        """Return the number of newly discovered, distinct gene sequences."""
        normalized = _normalize_batch(sequences)
        new_genes = set(normalized).difference(self._observations)
        if len(self._observations) + len(new_genes) > MAX_INVENTORY_ITEMS:
            raise ValueError(f"本轮识别最多保留 {MAX_INVENTORY_ITEMS} 种基因，请先录入或清空。")
        self._observations.update(normalized)
        return len(new_genes)

    def remove(self, genes: str) -> bool:
        """Remove one candidate, returning whether it was present."""
        normalized = _normalize_sequence(genes)
        return self._observations.pop(normalized, None) is not None

    def clear(self) -> None:
        self._observations.clear()


@dataclass(frozen=True)
class CaptureImportPlan:
    """An immutable result; callers commit ``inventory`` after review."""

    inventory: tuple[InventoryItem, ...]
    items: tuple[InventoryItem, ...]
    skipped_genes: tuple[str, ...]

    @property
    def added_count(self) -> int:
        return len(self.items)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_genes)


def plan_capture_import(
    inventory: Iterable[InventoryItem],
    sequences: Iterable[str],
    *,
    quantity: int = 1,
) -> CaptureImportPlan:
    """Build an atomic append-only import, skipping all existing gene strings.

    Repeated candidates add only one item, with the explicit ``quantity``
    (default 1). Existing records retain their IDs, names and quantities, so
    references from planted slots remain valid. No input collection is mutated.
    """
    if type(quantity) is not int or not 1 <= quantity <= MAX_QUANTITY:
        raise ValueError(f"录入数量必须是 1–{MAX_QUANTITY} 的整数。")
    current = tuple(inventory)
    if len(current) > MAX_INVENTORY_ITEMS:
        raise ValueError(f"种子库最多 {MAX_INVENTORY_ITEMS} 项。")
    if any(not isinstance(item, InventoryItem) for item in current):
        raise ValueError("种子库包含无效种子记录。")
    if any(not 1 <= item.quantity <= MAX_QUANTITY for item in current):
        raise ValueError(f"库存数量必须是 1–{MAX_QUANTITY} 的整数。")
    used_ids = {item.id for item in current}
    if len(used_ids) != len(current):
        raise ValueError("种子库存在重复编号，请先修复编号。")

    # dict preserves first-seen order while dropping repeated observations.
    candidates = tuple(dict.fromkeys(_normalize_batch(sequences)))
    existing_genes = {item.genes for item in current}
    pending = tuple(genes for genes in candidates if genes not in existing_genes)
    skipped = tuple(genes for genes in candidates if genes in existing_genes)
    if len(current) + len(pending) > MAX_INVENTORY_ITEMS:
        raise ValueError(f"录入后种子库将超过 {MAX_INVENTORY_ITEMS} 项，请减少候选或清理种子库。")

    additions: list[InventoryItem] = []
    for genes in pending:
        seed_id = uuid.uuid4().hex[:12]
        while seed_id in used_ids:
            seed_id = uuid.uuid4().hex[:12]
        used_ids.add(seed_id)
        additions.append(InventoryItem(seed_id, genes, quantity, f"识别苗 {len(current) + len(additions) + 1:02d}"))
    added = tuple(additions)
    return CaptureImportPlan(current + added, added, skipped)
