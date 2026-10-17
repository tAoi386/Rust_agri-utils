"""Deterministic, bounded single-generation Rust plant breeding search.

Model: G/Y/H have weight 6 and W/X weight 10 (integer tenths). The
recipient does not vote. A winning donor total must strictly exceed the
recipient gene's own weight to replace it. Equal donor winners are reported
as alternatives, never as an assumed uniform probability distribution.

The model is a planning aid; planting geometry, plant stages, and environmental
conditions are deliberately handled by the UI's in-game instructions.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from functools import lru_cache
from time import monotonic

from .models import (
    GENES, GENE_WEIGHTS, InventoryItem, Prediction, Recipe,
    SearchReport, SlotPrediction, Target, normalize_genes,
)


@lru_cache(maxsize=16_384)
def _gene_indices(genes: str) -> tuple[int, ...]:
    """Encode already validated library genes once per genotype."""
    return tuple(GENES.index(gene) for gene in genes)


def _donor_totals(donor_genes: tuple[str, ...]) -> tuple[tuple[int, ...], ...]:
    """Small voting kernel shared by search and the public predictor.

    Search inputs come from immutable InventoryItems and are already normalized.
    Revalidating six letters for every donor in every candidate wastes most of
    the hot loop, especially when the same library genotype appears repeatedly.
    """
    totals = [[0] * len(GENES) for _ in range(6)]
    for donor in donor_genes:
        for position, index in enumerate(_gene_indices(donor)):
            totals[position][index] += 6 if index < 3 else 10
    return tuple(tuple(row) for row in totals)


def _deterministic_from_totals(center_genes: str, totals: Sequence[Sequence[int]]) -> str | None:
    """Check a search candidate without allocating six UI prediction objects."""
    result = []
    for center, weights in zip(center_genes, totals):
        maximum = max(weights)
        if maximum <= GENE_WEIGHTS[center]:
            result.append(center)
        elif weights.count(maximum) != 1:
            return None
        else:
            result.append(GENES[weights.index(maximum)])
    return "".join(result)


def predict(center_genes: str, donors: Sequence[str]) -> Prediction:
    """Predict all six slots, counting donor plants only."""
    center_genes = normalize_genes(center_genes)
    donor_genes = tuple(normalize_genes(donor) for donor in donors)
    if len(donor_genes) > 8:
        raise ValueError("一个九格种植箱最多安排 8 株供体和 1 株目标株。")
    totals = _donor_totals(donor_genes)
    return _prediction_from_totals(center_genes, donor_genes, totals)


def _prediction_from_totals(
    center_genes: str, donor_genes: tuple[str, ...], totals: Sequence[Sequence[int]]
) -> Prediction:
    slots = []
    for index, center_gene in enumerate(center_genes):
        weights = dict(zip(GENES, totals[index]))
        maximum = max(weights.values())
        center_weight = GENE_WEIGHTS[center_gene]
        if maximum > center_weight:
            candidates = tuple(gene for gene in GENES if weights[gene] == maximum)
        else:
            candidates = (center_gene,)
        slots.append(SlotPrediction(
            index=index + 1,
            center_gene=center_gene,
            weights=weights,
            candidates=candidates,
            center_weight=center_weight,
            donor_max_weight=maximum,
            overwritten=candidates != (center_gene,),
        ))
    return Prediction(center_genes, donor_genes, tuple(slots))


def _multisets(capacities: Sequence[int], count: int) -> Iterable[tuple[int, ...]]:
    """Yield feasible bounded multisets with an explicit, nonrecursive stack."""
    suffix_capacity = [0] * (len(capacities) + 1)
    for index in range(len(capacities) - 1, -1, -1):
        suffix_capacity[index] = suffix_capacity[index + 1] + capacities[index]
    counts = [0] * len(capacities)
    if count == 0:
        yield tuple(counts)
        return
    if not capacities or suffix_capacity[0] < count:
        return

    # Each frame stores index, remaining, next amount to try, and lower bound.
    # An inventory can contain thousands of genotypes; recursion depth must not
    # depend on that number or it could fail before the search budget is met.
    stack = [(0, count, min(capacities[0], count),
              max(0, count - suffix_capacity[1]))]
    while stack:
        index, remaining, used, lower = stack[-1]
        if used < lower:
            counts[index] = 0
            stack.pop()
            continue
        stack[-1] = (index, remaining, used - 1, lower)
        counts[index] = used
        remaining -= used
        if remaining == 0:
            for trailing in range(index + 1, len(counts)):
                counts[trailing] = 0
            yield tuple(counts)
            continue
        next_index = index + 1
        if next_index < len(capacities) and suffix_capacity[next_index] >= remaining:
            stack.append((next_index, remaining, min(capacities[next_index], remaining),
                          max(0, remaining - suffix_capacity[next_index + 1])))


def _rank(recipe: Recipe) -> tuple:
    if recipe.guaranteed:
        return (0, recipe.plant_count, -recipe.worst_match, -recipe.best_match,
                recipe.prediction.possible_outcome_count, recipe.center.genes, recipe.donor_ids)
    # For partial results, prefer a useful improvement over a cheap bad plant.
    return (1, -recipe.worst_match, -recipe.best_match, recipe.plant_count,
            recipe.prediction.possible_outcome_count, recipe.center.genes, recipe.donor_ids)


def plan(
    inventory: Sequence[InventoryItem],
    target: Target,
    *,
    max_donors: int = 8,
    max_combinations: int = 250_000,
    max_results: int = 8,
    cancel: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
    center_id: str | None = None,
) -> SearchReport:
    """Find the fewest plants for a guaranteed target in one generation.

    Search proceeds by donor count and respects each inventory quantity,
    including the recipient. Equivalent genotypes are merged for search but
    recipes retain the original inventory identifiers. Once a complete donor
    count has a guaranteed solution, larger counts are unnecessary. If bounded
    or cancelled, the report explicitly says that minimality is unproven.
    ``center_id`` locks the actual selected inventory row as the recipient,
    while duplicate-genotype rows remain available as separate donor stock.
    """
    if not 0 <= max_donors <= 8:
        raise ValueError("供体上限必须介于 0 和 8。")
    if max_combinations < 1 or max_results < 1:
        raise ValueError("搜索上限和保留方案数必须大于 0。")
    start = monotonic()
    rows = tuple(inventory)
    if len({row.id for row in rows}) != len(rows):
        raise ValueError("每条库存的编号必须唯一。")
    locked_center = next((row for row in rows if row.id == center_id), None)
    if center_id is not None and locked_center is None:
        raise ValueError("指定的中心株不在当前可用库存中。")
    if not rows:
        return SearchReport((), True, 0, "empty_inventory", requested_max_donors=max_donors)

    groups: dict[str, list[InventoryItem]] = defaultdict(list)
    for row in rows:
        groups[row.genes].append(row)
    genotypes = tuple(sorted(groups))
    grouped_rows = tuple(groups[genes] for genes in genotypes)
    quantities = tuple(sum(row.quantity for row in group) for group in grouped_rows)
    effective_max = min(max_donors, sum(quantities) - 1)
    gene_vectors = []
    for genes in genotypes:
        vector = [[0] * len(GENES) for _ in range(6)]
        for position, gene in enumerate(genes):
            vector[position][GENES.index(gene)] = GENE_WEIGHTS[gene]
        gene_vectors.append(vector)

    evaluated = 0
    best: list[Recipe] = []
    best_keys: list[tuple] = []
    last_completed_count = -1
    current_count = 0

    def report(reason: str, complete: bool, proven: bool = False) -> SearchReport:
        return SearchReport(
            tuple(best), complete, evaluated, reason, proven,
            max(0, last_completed_count if complete else current_count),
            max_donors, monotonic() - start,
        )

    def allocate(center_group: int, counts: tuple[int, ...]) -> tuple[InventoryItem, tuple[InventoryItem, ...]]:
        center = locked_center or grouped_rows[center_group][0]
        donors = []
        for group_index, needed in enumerate(counts):
            for row in grouped_rows[group_index]:
                available = row.quantity - int(row.id == center.id)
                used = min(needed, available)
                donors.extend([row] * used)
                needed -= used
                if not needed:
                    break
        return center, tuple(donors)

    for donor_count in range(effective_max + 1):
        current_count = donor_count
        found_at_count = False
        if progress:
            progress(evaluated, donor_count)
        for center_index, center_genes in enumerate(genotypes):
            if locked_center is not None and center_genes != locked_center.genes:
                continue
            capacities = [min(quantity, donor_count) for quantity in quantities]
            capacities[center_index] = min(quantities[center_index] - 1, donor_count)
            for counts in _multisets(capacities, donor_count):
                if cancel and cancel():
                    return report("cancelled", False)
                if evaluated >= max_combinations:
                    return report("limit", False)
                donor_genes = tuple(genes for genes, used in zip(genotypes, counts) for _ in range(used))
                totals = [[0] * len(GENES) for _ in range(6)]
                for group_index, used in enumerate(counts):
                    if used:
                        vector = gene_vectors[group_index]
                        for position in range(6):
                            for gene_index in range(len(GENES)):
                                totals[position][gene_index] += vector[position][gene_index] * used
                prediction = _prediction_from_totals(center_genes, donor_genes, totals)
                worst, best_match = prediction.match_range(target)
                evaluated += 1
                found_at_count = found_at_count or worst == 6
                # Construct inventory allocations only when a candidate can
                # make the short list. Most searched combinations stay small.
                rough_key = ((0, donor_count + 1, -worst, -best_match,
                              prediction.possible_outcome_count, center_genes)
                             if worst == 6 else
                             (1, -worst, -best_match, donor_count + 1,
                              prediction.possible_outcome_count, center_genes))
                if len(best) < max_results or rough_key <= best_keys[-1][:-1]:
                    center, donors = allocate(center_index, counts)
                    recipe = Recipe(center, donors, prediction, target)
                    key = _rank(recipe)
                    insert_at = 0
                    while insert_at < len(best_keys) and best_keys[insert_at] <= key:
                        insert_at += 1
                    best.insert(insert_at, recipe)
                    best_keys.insert(insert_at, key)
                    del best[max_results:]
                    del best_keys[max_results:]
                if progress and evaluated % 1000 == 0:
                    progress(evaluated, donor_count)
        last_completed_count = donor_count
        if found_at_count:
            if progress:
                progress(evaluated, donor_count)
            return report("found", True, True)
    if progress:
        progress(evaluated, effective_max)
    return report("exhausted", True)
