"""Small immutable data objects used by the breeding model and the Qt UI.

The prediction describes possible outcomes of a community rules model.  A set of
possible outcomes is deliberately not a probability distribution.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from itertools import islice, product
from typing import Mapping


GENES = "GYHWX"
GENE_WEIGHTS: dict[str, int] = {"G": 6, "Y": 6, "H": 6, "W": 10, "X": 10}
GENE_LABELS: dict[str, str] = {
    "G": "生长", "Y": "产量", "H": "耐性", "W": "耗水", "X": "空基因"
}


def normalize_genes(value: str) -> str:
    """Accept convenient spacing/separators but require six valid genes."""
    value = "".join(ch for ch in str(value).upper() if ch not in " \t\r\n,，/、-_")
    if len(value) != 6 or any(ch not in GENES for ch in value):
        raise ValueError("每株必须是 6 位基因，只能使用 G、Y、H、W、X，例如 YYYGGG。")
    return value


@dataclass(frozen=True)
class InventoryItem:
    id: str
    genes: str
    quantity: int = 1
    name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id))
        object.__setattr__(self, "genes", normalize_genes(self.genes))
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity < 1:
            raise ValueError("可用数量必须是大于 0 的整数。")
        if not self.id:
            raise ValueError("种子编号不能为空。")

    @property
    def label(self) -> str:
        return self.name or self.id


@dataclass(frozen=True)
class Target:
    genes: str = "YYYGGG"
    mode: str = "counts"

    def __post_init__(self) -> None:
        object.__setattr__(self, "genes", normalize_genes(self.genes))
        aliases = {"any_order": "counts", "anyorder": "counts", "count": "counts", "ordered": "exact"}
        mode = aliases.get(self.mode, self.mode)
        if mode not in ("counts", "exact"):
            raise ValueError("目标模式必须是 counts（只看数量）或 exact（逐位匹配）。")
        object.__setattr__(self, "mode", mode)

    @property
    def counts(self) -> Mapping[str, int]:
        return dict(Counter(self.genes))

    @property
    def label(self) -> str:
        if self.mode == "exact":
            return f"{self.genes}（固定顺序）"
        counts = Counter(self.genes)
        return " + ".join(f"{counts[g]}{g}" for g in GENES if counts[g]) + "（顺序不限）"

    def matches(self, genes: str) -> bool:
        genes = normalize_genes(genes)
        return genes == self.genes if self.mode == "exact" else Counter(genes) == Counter(self.genes)


@dataclass(frozen=True)
class SlotPrediction:
    index: int
    center_gene: str
    weights: Mapping[str, int]
    candidates: tuple[str, ...]
    center_weight: int
    donor_max_weight: int
    overwritten: bool

    @property
    def uncertain(self) -> bool:
        return len(self.candidates) > 1

    @property
    def result(self) -> str:
        return self.candidates[0] if len(self.candidates) == 1 else "[" + "/".join(self.candidates) + "]"


@dataclass(frozen=True)
class Prediction:
    center_genes: str
    donors: tuple[str, ...]
    slots: tuple[SlotPrediction, ...]

    @property
    def candidates(self) -> tuple[tuple[str, ...], ...]:
        return tuple(slot.candidates for slot in self.slots)

    @property
    def deterministic_result(self) -> str | None:
        if any(slot.uncertain for slot in self.slots):
            return None
        return "".join(slot.candidates[0] for slot in self.slots)

    @property
    def result_pattern(self) -> str:
        return " ".join(slot.result for slot in self.slots)

    @property
    def possible_outcome_count(self) -> int:
        """Conservative Cartesian bound, not verified joint reachability in game."""
        total = 1
        for slot in self.slots:
            total *= len(slot.candidates)
        return total

    @property
    def uncertain_slots(self) -> int:
        return sum(slot.uncertain for slot in self.slots)

    def outcomes(self, limit: int = 256) -> tuple[str, ...]:
        """Return at most ``limit`` possibilities, without assigning likelihoods."""
        return tuple("".join(chars) for chars in islice(product(*self.candidates), max(0, limit)))

    def match_range(self, target: Target) -> tuple[int, int]:
        """Return the worst/best matched gene count over all model outcomes."""
        return _match_range(self.candidates, target.genes, target.mode)

    def guaranteed_for(self, target: Target) -> bool:
        return self.match_range(target)[0] == 6

    def possible_for(self, target: Target) -> bool:
        return self.match_range(target)[1] == 6


@lru_cache(maxsize=65536)
def _match_range(candidates: tuple[tuple[str, ...], ...], target_genes: str, mode: str) -> tuple[int, int]:
    if mode == "exact":
        worst = sum(options == (wanted,) for options, wanted in zip(candidates, target_genes))
        best = sum(wanted in options for options, wanted in zip(candidates, target_genes))
        return worst, best

    wanted = tuple(target_genes.count(gene) for gene in GENES)
    # Count signatures avoid enumerating up to 5**6 ordered strings. There can
    # be only 210 different count signatures for six genes and five letters.
    states = {(0, 0, 0, 0, 0)}
    for options in candidates:
        next_states = set()
        for state in states:
            for gene in options:
                index = GENES.index(gene)
                counts = list(state)
                counts[index] += 1
                next_states.add(tuple(counts))
        states = next_states
    scores = [sum(min(have, need) for have, need in zip(state, wanted)) for state in states]
    return min(scores), max(scores)


@dataclass(frozen=True)
class Recipe:
    center: InventoryItem
    donors: tuple[InventoryItem, ...]
    prediction: Prediction
    target: Target

    @property
    def center_id(self) -> str:
        return self.center.id

    @property
    def donor_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.donors)

    @property
    def donor_counts(self) -> Mapping[str, int]:
        return dict(Counter(self.donor_ids))

    @property
    def donor_count(self) -> int:
        return len(self.donors)

    @property
    def plant_count(self) -> int:
        return self.donor_count + 1

    @property
    def worst_match(self) -> int:
        return self.prediction.match_range(self.target)[0]

    @property
    def best_match(self) -> int:
        return self.prediction.match_range(self.target)[1]

    @property
    def guaranteed(self) -> bool:
        return self.worst_match == 6

    @property
    def possible_match(self) -> bool:
        return self.best_match == 6

    @property
    def result_summary(self) -> str:
        if self.guaranteed:
            return f"模型内确定达标 · 共 {self.plant_count} 株"
        if self.possible_match:
            return f"模型候选含目标，同票未验证联合可达 · 保底匹配 {self.worst_match}/6"
        return f"过渡方案 · 匹配 {self.worst_match}–{self.best_match}/6"


@dataclass(frozen=True)
class SearchReport:
    recipes: tuple[Recipe, ...]
    complete: bool
    evaluated: int
    reason: str
    minimum_proven: bool = False
    searched_max_donors: int = 0
    requested_max_donors: int = 8
    elapsed_seconds: float = 0.0

    @property
    def best(self) -> Recipe | None:
        return self.recipes[0] if self.recipes else None

    @property
    def found(self) -> bool:
        return any(recipe.guaranteed for recipe in self.recipes)

    @property
    def cancelled(self) -> bool:
        return self.reason == "cancelled"

    @property
    def summary(self) -> str:
        if self.reason == "empty_inventory":
            return "请先添加种子或克隆株。"
        if self.minimum_proven:
            return f"已核对 {self.evaluated:,} 个组合；找到本轮保守模型内、库存范围内最少用株方案。"
        if self.reason == "cancelled":
            return f"计算已停止，保留已检查 {self.evaluated:,} 个组合中的方案；未证明最少。"
        if self.reason == "limit":
            return f"已检查 {self.evaluated:,} 个组合，达到搜索上限；当前建议未证明最少。"
        return f"已检查 {self.evaluated:,} 个组合；在指定单代范围内没有确定达标方案。"
