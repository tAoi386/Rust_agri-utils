"""Bounded, inventory-aware routes for a user-selected central plant.

Only deterministic model results become future inventory. Search visits bounded
layers and compares the requested objective. Large inventories use a
diverse donor pool, stratified sampling and a beam; these limits are always
reported and never described as a proof of the globally shortest route.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from bisect import bisect_right
from functools import cached_property, lru_cache
from itertools import combinations
from random import Random
from time import monotonic

from .engine import (_multisets, predict, _donor_totals,
                     _deterministic_from_totals, _prediction_from_totals)
from .models import GENES, InventoryItem, Recipe, Target


@dataclass(frozen=True)
class RouteStep:
    recipe: Recipe
    expected_genes: str
    output_id: str
    clone_yield: int


@dataclass(frozen=True)
class RouteReport:
    steps: tuple[RouteStep, ...]
    recipes: tuple[Recipe, ...]
    found: bool
    complete: bool
    evaluated: int
    reason: str
    minimum_proven: bool = False
    shortest_rounds_proven: bool = False
    requested_max_rounds: int = 3
    clone_yield: int = 1
    elapsed_seconds: float = 0.0
    limits: tuple[str, ...] = ()
    objective: str = "plants"
    center_mode: str = "fixed"

    @property
    def best(self) -> Recipe | None:
        return self.steps[0].recipe if self.steps else next(iter(self.recipes), None)

    @property
    def rounds(self) -> int:
        return len(self.steps)

    @property
    def plant_count(self) -> int:
        return sum(step.recipe.plant_count for step in self.steps)

    @property
    def cancelled(self) -> bool:
        return self.reason == "cancelled"

    @property
    def center_id(self) -> str | None:
        return self.best.center_id if self.best else None

    @property
    def summary(self) -> str:
        checked = f"已检查 {self.evaluated:,} 个组合"
        if self.reason == "already_target":
            return "选中的中心株已达到目标，无需杂交。"
        if self.found:
            if self.objective == "stock":
                return (f"{checked}；智能匹配（基于已有基因库）：{self.rounds} 轮、累计用 {self.plant_count} 株。"
                        "结合已有基因组合和实际可用苗比较有限完整候选，未证明全局最优。"
                        f"每轮按获得 {self.clone_yield} 株克隆预估，实际结果需确认。")
            proven = "已证明指定范围内最少用株" if self.objective == "plants" else "已证明指定范围内最少轮次且最少用株"
            proof = proven if self.minimum_proven else "当前找到的路线，未证明全局最优"
            return f"{checked}；{self.rounds} 轮、累计用 {self.plant_count} 株；{proof}。每轮按获得 {self.clone_yield} 株克隆预估，实际结果需确认。"
        if self.cancelled:
            return f"计算已停止；{checked}，可重新自动选苗。"
        scope = "已完整搜索指定范围" if self.complete else "达到搜索边界，未穷尽所有路线"
        return f"{checked}；{scope}，暂未找到 {self.requested_max_rounds} 轮内确定达标的路线。实际结果录入后可继续计算。"


@dataclass(frozen=True)
class _State:
    inventory: tuple[InventoryItem, ...]
    center_id: str
    steps: tuple[RouteStep, ...] = ()
    plants: int = 0

    @cached_property
    def center(self) -> InventoryItem:
        return next(row for row in self.inventory if row.id == self.center_id)

    @cached_property
    def key(self) -> tuple:
        stock: Counter[str] = Counter()
        for row in self.inventory:
            stock[row.genes] += row.quantity
        return self.center.genes, tuple(sorted(stock.items()))


@lru_cache(maxsize=16_384)
def _score(genes: str, target: Target) -> int:
    if target.mode == "exact":
        return sum(a == b for a, b in zip(genes, target.genes))
    return sum((Counter(genes) & Counter(target.genes)).values())


def _state_key(state: _State) -> tuple:
    return state.key


def _state_rank(state: _State, target: Target) -> tuple:
    genes = state.center.genes
    return (-_score(genes, target), state.plants,
            sum(g in "WX" for g in genes), genes, _state_key(state))


def _bounded_count(capacities: Sequence[int], count: int, ceiling: int) -> int:
    """Count multisets cheaply; values above the needed ceiling are equivalent."""
    values = [1] + [0] * count
    for capacity in capacities:
        values = [min(ceiling, sum(values[total - used]
                                  for used in range(min(capacity, total) + 1)))
                  for total in range(count + 1)]
    return values[count]


def _sample_counts(capacities: Sequence[int], count: int, budget: int, rng: Random):
    """Visit repeats, mixtures and random multisets without low-count starvation."""
    seen: set[tuple[int, ...]] = set()
    count_types = len(capacities)

    def candidate(pairs):
        counts = [0] * count_types
        for index, amount in pairs:
            counts[index] += amount
        result = tuple(counts)
        if result in seen or any(n > cap for n, cap in zip(result, capacities)):
            return None
        seen.add(result)
        return result

    for index, capacity in enumerate(capacities):
        if capacity >= count:
            value = candidate(((index, count),))
            if value is not None:
                yield value
                if len(seen) >= budget:
                    return
    # Pair mixtures are particularly useful when cloning produced duplicate
    # donors. Rotate the pair order so a small budget is not always prefix-only.
    pairs = list(combinations(range(count_types), 2))
    rng.shuffle(pairs)
    for first, second in pairs:
        for amount in range(1, count):
            value = candidate(((first, amount), (second, count - amount)))
            if value is not None:
                yield value
                if len(seen) >= budget:
                    return
    physical = [index for index, capacity in enumerate(capacities)
                for _ in range(min(count, capacity))]
    for _ in range(max(80, budget * 12)):
        chosen = Counter(rng.sample(physical, count))
        value = candidate(chosen.items())
        if value is not None:
            yield value
            if len(seen) >= budget:
                return


def _choose_pool(genes: Sequence[str], center: str, target: Target, limit: int) -> list[int]:
    def rank(index):
        donor = genes[index]
        relevant = sum(a != wanted and b == wanted for a, b, wanted
                       in zip(center, donor, target.genes)) if target.mode == "exact" else 0
        return (-relevant, -_score(donor, target), sum(g in "WX" for g in donor), donor)

    ordered = sorted(range(len(genes)), key=rank)
    selected: list[int] = []
    # Keep donors covering individual target positions, including lower-scoring
    # donors that can preserve a currently correct position while changing others.
    if target.mode == "exact":
        for position, wanted in enumerate(target.genes):
            matching = [index for index in ordered if genes[index][position] == wanted]
            for index in matching[:2]:
                if index not in selected and len(selected) < limit:
                    selected.append(index)
    else:
        wanted_genes = [gene for gene in GENES if gene in target.genes]
        positions = sorted(range(6), key=lambda position: (center[position] in wanted_genes,
                                                          position))
        for position in positions:
            for wanted in wanted_genes:
                matching = [index for index in ordered if genes[index][position] == wanted]
                # Two green donors can overcome a red center slot. A pool made
                # only from globally high-scoring donors can otherwise omit
                # every first-position G/Y donor in a large seed collection.
                need = 2 if center[position] not in wanted_genes else 1
                for index in matching[:need]:
                    if index not in selected and len(selected) < limit:
                        selected.append(index)
    for index in ordered:
        if len(selected) >= limit:
            break
        if index not in selected:
            selected.append(index)
    return sorted(selected, key=rank)


def _green_target_patterns(target: Target) -> tuple[str, ...]:
    if any(gene in "WX" for gene in target.genes):
        return ()
    if target.mode == "exact":
        return (target.genes,)
    patterns = []
    counts = Counter(target.genes)

    def append_patterns(prefix):
        if len(prefix) == 6:
            patterns.append(prefix)
            return
        for gene in sorted(counts):
            if counts[gene]:
                counts[gene] -= 1
                append_patterns(prefix + gene)
                counts[gene] += 1

    append_patterns("")
    return tuple(patterns)


def _four_donor_candidates(genes: Sequence[str], capacities: Sequence[int],
                           target: Target, stop: Callable[[], bool], *,
                           patterns: Sequence[str] | None = None,
                           extra: str | None = None,
                           probe: Callable[[], bool] | None = None):
    """Meet pairs across the full library instead of discarding weak donors.

    For a desired green letter to win with four donors, at least two donors
    must carry it. Encode each pair's zero/one/two matches in six-bit masks,
    and join only complementary buckets. The ordinary predictor still checks
    all weights and ties before any recipe can be accepted.
    """
    patterns = _green_target_patterns(target) if patterns is None else patterns
    if not patterns:
        return
    pairs = []
    for first in range(len(genes)):
        if stop():
            return
        if capacities[first] <= 0:
            continue
        pairs.extend((first, second) for second in range(first, len(genes))
                     if capacities[second] > 0 and (first != second or capacities[first] >= 2))
    seen = set()
    probes = 0
    for pattern in patterns:
        if stop():
            return
        need_one = need_two = 0
        for position, wanted in enumerate(pattern):
            if wanted == "?":
                continue
            if extra is not None and extra[position] == wanted:
                need_one |= 1 << position
            else:
                need_two |= 1 << position
        buckets = defaultdict(list)
        for number, (first, second) in enumerate(pairs):
            if number % 500 == 0 and stop():
                return
            one = two = 0
            for position in range(6):
                if pattern[position] == "?":
                    continue
                left = genes[first][position] == pattern[position]
                right = genes[second][position] == pattern[position]
                one |= int(left or right) << position
                two |= int(left and right) << position
            buckets[one, two].append((first, second))
        keys = sorted(buckets, key=lambda key: (-key[0].bit_count() - key[1].bit_count(), key))
        for a, (one_a, two_a) in enumerate(keys):
            if stop():
                return
            for one_b, two_b in keys[a:]:
                if ((one_a | one_b) & need_one != need_one
                        or (one_a | two_b) & need_two != need_two
                        or (one_b | two_a) & need_two != need_two):
                    continue
                for pair_a in buckets[one_a, two_a]:
                    for pair_b in buckets[one_b, two_b]:
                        probes += 1
                        if (probe is not None and probe()) or (probes % 256 == 0 and stop()):
                            return
                        chosen = tuple(sorted((*pair_a, *pair_b)))
                        if chosen in seen:
                            continue
                        seen.add(chosen)
                        needed = Counter(chosen)
                        if all(amount <= capacities[index] for index, amount in needed.items()):
                            yield chosen


def _allocate_donors(grouped_rows, indices_and_counts, center_id):
    donors = []
    for index, amount in indices_and_counts:
        needed = amount
        for row in grouped_rows[index]:
            available = row.quantity - int(row.id == center_id)
            used = min(needed, available)
            donors.extend([row] * used)
            needed -= used
            if not needed:
                break
        if needed:
            return None
    return tuple(donors)


def plan_route(
    inventory: Sequence[InventoryItem], target: Target, *, center_id: str | None = None,
    max_rounds: int = 3, clone_yield: int = 1, max_donors: int = 8,
    max_combinations: int = 60_000, beam_width: int = 24,
    donor_pool_limit: int = 12, max_seconds: float = 8.0,
    objective: str = "plants",
    cancel: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> RouteReport:
    """Plan a bounded route from an automatic or explicitly selected center.

    A round consumes the center and every donor, then yields exactly
    ``clone_yield`` copies of its deterministic result for planning purposes.
    This is an explicit user estimate, not an assumption of unlimited clones.
    Counts and actual genes must be reconciled before executing another round.
    ``complete`` and minimum flags concern deterministic model routes only.
    """
    for name, value in (("轮数", max_rounds), ("克隆数量", clone_yield),
                        ("组合上限", max_combinations), ("保留路线数", beam_width),
                        ("供体类型上限", donor_pool_limit)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name}必须是大于 0 的整数。")
    if not 0 <= max_donors <= 8:
        raise ValueError("供体上限必须介于 0 和 8。")
    if max_seconds <= 0:
        raise ValueError("时间上限必须大于 0。")
    if objective not in ("plants", "rounds", "stock"):
        raise ValueError("优化目标必须是 plants（少用株）、rounds（少轮次）或 stock（基于已有基因库智能匹配）。")
    if objective == "stock":
        from .stock_planning import plan_stock_route
        return plan_stock_route(
            inventory, target, center_id=center_id, max_rounds=max_rounds,
            clone_yield=clone_yield, max_donors=max_donors,
            max_combinations=max_combinations, beam_width=beam_width,
            donor_pool_limit=donor_pool_limit, max_seconds=max_seconds,
            cancel=cancel, progress=progress, _planner=plan_route)

    def route_rank(plants, rounds):
        return (plants, rounds) if objective == "plants" else (rounds, plants)

    rows = tuple(inventory)
    if len({row.id for row in rows}) != len(rows):
        raise ValueError("每条库存的编号必须唯一。")
    if not rows:
        return RouteReport((), (), False, True, 0, "empty_inventory",
                           requested_max_rounds=max_rounds, clone_yield=clone_yield,
                           objective=objective)
    center = next((row for row in rows if row.id == str(center_id)), None)
    if center_id is not None and center is None:
        raise ValueError("指定的中心株不在当前可用库存中。")
    started = monotonic()
    evaluated = 0
    limits: set[str] = set()
    fallback: list[Recipe] = []
    fallback_keys: list[tuple] = []
    winner: _State | None = None
    minimum_found_round: int | None = None
    reason = "exhausted"
    shortest_proven = False
    prior_layers_complete = True
    if center is not None:
        centers = [center]
    else:
        # Equivalent genotype rows share physical stock; trying every row ID
        # would repeat identical breeding problems and steal the search budget.
        centers = list({row.genes: row for row in reversed(rows)}.values())
        centers.sort(key=lambda row: (-_score(row.genes, target), row.genes, row.id))
    frontier = [_State(rows, row.id) for row in centers]
    visited = {_state_key(state): 0 for state in frontier}

    def finish(found=False, steps=(), minimum=False):
        return RouteReport(
            tuple(steps), tuple(fallback), found, not limits and reason != "cancelled",
            evaluated, reason, minimum, shortest_proven, max_rounds, clone_yield,
            monotonic() - started, tuple(sorted(limits)), objective,
            "auto" if center_id is None else "fixed",
        )

    if cancel and cancel():
        reason = "cancelled"
        return finish()
    matched_center = next((row for row in centers if target.matches(row.genes)), None)
    if matched_center is not None:
        fallback.append(Recipe(matched_center, (), predict(matched_center.genes, ()), target))
        reason = "already_target"
        shortest_proven = True
        return finish(True, minimum=True)

    def interrupted():
        nonlocal reason
        if cancel and cancel():
            reason = "cancelled"
            return True
        if evaluated >= max_combinations:
            reason = "limit"
            limits.add("combinations")
            return True
        if monotonic() - started >= max_seconds:
            reason = "limit"
            limits.add("time")
            return True
        return False

    def remember_fallback(recipe):
        # A zero-donor baseline and a deterministic unchanged center are never
        # actionable advice when that center has not reached the target.
        outcome = recipe.prediction.deterministic_result
        if not recipe.donor_count or outcome == recipe.center.genes:
            return
        if recipe.best_match < _score(recipe.center.genes, target):
            return
        worst, best = recipe.prediction.match_range(target)
        key = (worst != 6, -worst, -best, outcome is None, recipe.plant_count,
               recipe.prediction.result_pattern, recipe.donor_ids)
        position = bisect_right(fallback_keys, key)
        if position >= 6:
            return
        fallback.insert(position, recipe)
        fallback_keys.insert(position, key)
        del fallback[6:]
        del fallback_keys[6:]

    def consume(state: _State, recipe: Recipe, outcome: str, depth: int) -> _State:
        spent = Counter((recipe.center_id, *recipe.donor_ids))
        remaining = [row if not spent[row.id] else
                     InventoryItem(row.id, row.genes, row.quantity - spent[row.id], row.name)
                     for row in state.inventory if row.quantity > spent[row.id]]
        output_id = f"__route_clone_{depth}"
        occupied = {row.id for row in rows}
        while output_id in occupied:
            output_id += "_"
        remaining.append(InventoryItem(output_id, outcome, clone_yield, f"第 {depth} 轮结果克隆"))
        step = RouteStep(recipe, outcome, output_id, clone_yield)
        return _State(tuple(remaining), output_id, (*state.steps, step),
                      state.plants + recipe.plant_count)

    # Seed the route search with a complementary four-donor recipe from the
    # whole real library. A poor-looking individual seed can supply the one
    # position missing from three otherwise strong donors, so a top-score pool
    # alone is insufficient. This pass adds valid candidates; it makes no
    # minimality claim and never invents inventory.
    if max_donors >= 4 and sum(row.quantity for row in rows) >= 5:
        seed_groups = defaultdict(list)
        for row in rows:
            seed_groups[row.genes].append(row)
        seed_genes = tuple(sorted(seed_groups))
        seed_rows = tuple(seed_groups[genes] for genes in seed_genes)
        seed_capacities = tuple(sum(row.quantity for row in group) for group in seed_rows)
        seed_deadline = started + max_seconds / 4

        def stop_seed():
            return ((cancel is not None and cancel()) or evaluated >= max_combinations
                    or monotonic() >= seed_deadline)

        for chosen in _four_donor_candidates(seed_genes, seed_capacities, target, stop_seed):
            if stop_seed():
                break
            needed = Counter(chosen)
            for state in frontier:
                selected_index = seed_genes.index(state.center.genes)
                if needed[selected_index] >= seed_capacities[selected_index]:
                    continue
                donors = _allocate_donors(seed_rows, needed.items(), state.center_id)
                if donors is None:
                    continue
                prediction = predict(state.center.genes, tuple(row.genes for row in donors))
                evaluated += 1
                if progress and evaluated % 250 == 0:
                    progress(evaluated, 1)
                outcome = prediction.deterministic_result
                if outcome is not None and target.matches(outcome):
                    recipe = Recipe(state.center, donors, prediction, target)
                    winner = consume(state, recipe, outcome, 1)
                    minimum_found_round = 1
                    remember_fallback(recipe)
                # Green winners have weight >= 12, so their outcome is
                # independent of the eligible center's individual defense.
                break
            if winner is not None:
                break

    # A useful intermediate can look worse than the original plant and be
    # discarded by the beam. Work backwards from a final pair of green donors:
    # matching letters overwrite any center, different letters preserve it.
    # Reserve that pair before meeting first-round donor pairs across the whole
    # library. This is a bounded source of candidates, not an exhaustive proof.
    if (winner is None and max_rounds >= 2 and max_donors >= 4
            and sum(row.quantity for row in rows) >= 7 and _green_target_patterns(target)):
        backward_deadline = started + max_seconds * .65
        backward_evaluation_limit = min(max_combinations, evaluated + max(2, max_combinations // 2))
        backward_probes = 0
        backward_probe_limit = max(256, max_combinations * 4)

        def stop_backward():
            return (interrupted() or evaluated >= backward_evaluation_limit
                    or backward_probes >= backward_probe_limit
                    or monotonic() >= backward_deadline)

        def backward_probe():
            nonlocal backward_probes
            backward_probes += 1
            return (backward_probes >= backward_probe_limit
                    or (backward_probes % 256 == 0 and stop_backward()))

        def find_backward_route():
            nonlocal evaluated
            green_indices = [i for i, genes in enumerate(seed_genes) if not any(g in "WX" for g in genes)]
            for first_index, first in enumerate(green_indices):
                for second in green_indices[first_index:]:
                    if stop_backward():
                        return None
                    reserved = Counter((first, second))
                    if any(amount > seed_capacities[index] for index, amount in reserved.items()):
                        continue
                    available = tuple(capacity - reserved[index]
                                      for index, capacity in enumerate(seed_capacities))
                    left, right = seed_genes[first], seed_genes[second]
                    for pattern in _green_target_patterns(target):
                        if any(a == b and a != wanted for a, b, wanted in zip(left, right, pattern)):
                            continue
                        required = "".join(wanted if a != b else "?"
                                           for a, b, wanted in zip(left, right, pattern))
                        # No preserved positions means this pair is already a
                        # one-round solution, which the ordinary search visits.
                        if required == "??????":
                            continue
                        for donor_count in (4, 5):
                            if donor_count > max_donors:
                                continue
                            extras = [None] if donor_count == 4 else sorted(
                                (i for i, capacity in enumerate(available) if capacity),
                                key=lambda i: (-sum(a == b for a, b in zip(seed_genes[i], required)),
                                               seed_genes[i]))
                            for extra in extras:
                                if stop_backward():
                                    return None
                                capacities = tuple(capacity - int(index == extra)
                                                   for index, capacity in enumerate(available))
                                for chosen in _four_donor_candidates(
                                        seed_genes, capacities, target, stop_backward,
                                        patterns=(required,), extra=seed_genes[extra] if extra is not None else None,
                                        probe=backward_probe):
                                    needed = Counter(chosen)
                                    if extra is not None:
                                        needed[extra] += 1
                                    for state in frontier:
                                        if stop_backward():
                                            return None
                                        center_index = seed_genes.index(state.center.genes)
                                        if needed[center_index] + reserved[center_index] >= seed_capacities[center_index]:
                                            continue
                                        donors = _allocate_donors(seed_rows, needed.items(), state.center_id)
                                        if donors is None:
                                            continue
                                        prediction = predict(state.center.genes, tuple(row.genes for row in donors))
                                        evaluated += 1
                                        if progress and evaluated % 250 == 0:
                                            progress(evaluated, 1)
                                        outcome = prediction.deterministic_result
                                        if (outcome is None or outcome == state.center.genes
                                                or any(wanted != "?" and actual != wanted
                                                       for actual, wanted in zip(outcome, required))):
                                            continue
                                        recipe = Recipe(state.center, donors, prediction, target)
                                        successor = consume(state, recipe, outcome, 1)
                                        if stop_backward():
                                            return None
                                        remaining = defaultdict(list)
                                        for row in successor.inventory:
                                            remaining[row.genes].append(row)
                                        final_donors = _allocate_donors(
                                            tuple(remaining[genes] for genes in seed_genes),
                                            reserved.items(), successor.center_id)
                                        if final_donors is None:
                                            continue
                                        final_prediction = predict(outcome, tuple(row.genes for row in final_donors))
                                        evaluated += 1
                                        if progress and evaluated % 250 == 0:
                                            progress(evaluated, 2)
                                        final = final_prediction.deterministic_result
                                        if final is not None and target.matches(final):
                                            remember_fallback(recipe)
                                            return consume(successor, Recipe(successor.center, final_donors,
                                                                            final_prediction, target), final, 2)
            return None

        winner = find_backward_route()
        if winner is not None:
            minimum_found_round = 2

    for depth in range(1, max_rounds + 1):
        if progress:
            progress(evaluated, depth)
        next_states: dict[tuple, _State] = {}
        layer_limits_before = set(limits)
        layer_budget = max(1, (max_combinations - evaluated) // (max_rounds - depth + 1))
        layer_start = evaluated
        now = monotonic()
        layer_deadline = now + max(0.0, (started + max_seconds - now)
                                  / (max_rounds - depth + 1))
        for state_index, state in enumerate(frontier):
            if interrupted():
                break
            if winner is not None and objective == "plants":
                cheapest_next = 1 if clone_yield > 1 else 2
                if route_rank(state.plants + cheapest_next, depth) >= route_rank(winner.plants, len(winner.steps)):
                    continue
            state_budget = max(1, (layer_budget - (evaluated - layer_start))
                               // (len(frontier) - state_index))
            now = monotonic()
            state_deadline = now + max(0.001, (layer_deadline - now)
                                      / (len(frontier) - state_index))
            groups: dict[str, list[InventoryItem]] = defaultdict(list)
            for row in state.inventory:
                groups[row.genes].append(row)
            all_genes = tuple(sorted(groups))
            grouped_rows = tuple(groups[genes] for genes in all_genes)
            capacities = [sum(row.quantity for row in group)
                          - int(all_genes[index] == state.center.genes)
                          for index, group in enumerate(grouped_rows)]
            usable = [i for i, capacity in enumerate(capacities) if capacity]
            pool = _choose_pool([all_genes[i] for i in usable], state.center.genes,
                                target, donor_pool_limit)
            indices = [usable[i] for i in pool]
            if len(indices) < len(usable):
                limits.add("donor_pool")
            selected_caps = tuple(capacities[i] for i in indices)
            selected_genes = tuple(all_genes[i] for i in indices)
            effective_max = min(max_donors, sum(selected_caps))
            counts_to_visit = list(range(1, effective_max + 1))
            if clone_yield > 1:
                counts_to_visit.insert(0, 0)
            if winner is not None:
                counts_to_visit = [count for count in counts_to_visit
                                   if route_rank(state.plants + count + 1, depth)
                                   < route_rank(winner.plants, len(winner.steps))]
            used_budget = 0
            rng = Random(f"{depth}:{_state_key(state)!r}")
            for count_index, donor_count in enumerate(counts_to_visit):
                if interrupted():
                    break
                count_budget = max(1, (state_budget - used_budget)
                                   // (len(counts_to_visit) - count_index))
                now = monotonic()
                count_deadline = now + max(0.001, (state_deadline - now)
                                          / (len(counts_to_visit) - count_index))
                actual_count = _bounded_count(selected_caps, donor_count, count_budget + 1)
                if actual_count > count_budget:
                    limits.add("sampling")
                    candidates = _sample_counts(selected_caps, donor_count, count_budget, rng)
                else:
                    candidates = _multisets(selected_caps, donor_count)
                for counts in candidates:
                    if interrupted():
                        break
                    if monotonic() >= count_deadline:
                        limits.add("time_sampling")
                        break
                    donor_genes = tuple(genes for genes, amount in zip(selected_genes, counts)
                                        for _ in range(amount))
                    totals = _donor_totals(donor_genes)
                    outcome = _deterministic_from_totals(state.center.genes, totals)
                    evaluated += 1
                    used_budget += 1
                    if progress and evaluated % 250 == 0:
                        progress(evaluated, depth)
                    # Preserve neutral/degrading intermediates: their exact
                    # positional pattern can unlock a later round.
                    if outcome == state.center.genes and donor_count:
                        continue
                    needs_recipe = outcome is not None or depth == 1
                    if not needs_recipe:
                        continue
                    reached_target = outcome is not None and _score(outcome, target) == 6
                    # On the final layer only a target result can be used. Do
                    # not allocate full inventory/recipe/Qt-facing predictions
                    # for thousands of results that cannot have a successor.
                    if depth == max_rounds and depth != 1 and not reached_target:
                        continue
                    prediction = _prediction_from_totals(state.center.genes, donor_genes, totals)
                    donors = _allocate_donors(grouped_rows, zip(indices, counts), state.center_id)
                    assert donors is not None
                    recipe = Recipe(state.center, donors, prediction, target)
                    if depth == 1:
                        remember_fallback(recipe)
                    if outcome is None:
                        continue
                    if reached_target:
                        successor = consume(state, recipe, outcome, depth)
                        minimum_found_round = min(minimum_found_round or depth, depth)
                        if winner is None or route_rank(successor.plants, len(successor.steps)) < route_rank(
                                winner.plants, len(winner.steps)):
                            winner = successor
                        continue
                    if depth == max_rounds:
                        continue
                    successor = consume(state, recipe, outcome, depth)
                    key = _state_key(successor)
                    if key in visited and visited[key] <= depth:
                        continue
                    previous = next_states.get(key)
                    if previous is None or successor.plants < previous.plants:
                        next_states[key] = successor
                if reason in ("cancelled", "limit"):
                    break
            if reason in ("cancelled", "limit"):
                break
        if winner is not None and objective == "rounds" and depth >= len(winner.steps):
            shortest_proven = prior_layers_complete
            reason = "found" if reason != "cancelled" else reason
            return finish(True, winner.steps, minimum=not limits and reason != "cancelled")
        if reason in ("cancelled", "limit"):
            return finish(winner is not None, winner.steps if winner is not None else ())
        if len(next_states) > beam_width:
            limits.add("beam")
        ordered = sorted(next_states.values(), key=lambda value: _state_rank(value, target))
        frontier = ordered[:beam_width]
        for state in frontier:
            visited[_state_key(state)] = depth
        prior_layers_complete = prior_layers_complete and not limits and limits == layer_limits_before
        if not frontier:
            break
    if winner is not None:
        reason = "found"
        shortest_proven = len(winner.steps) == 1 or (
            not limits and len(winner.steps) == minimum_found_round)
        return finish(True, winner.steps, minimum=not limits)
    return finish()
