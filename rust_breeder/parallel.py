"""Bounded breeding DAG search with shared stock and box-aware scheduling.

The catalog contains real recipes for intermediate genotypes, together with
their complete ancestry. A proposed merge must pay every original seed and
every intermediate clone across the entire DAG. Only then is it scheduled in
batches; dependencies can never consume outputs from the same batch.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from itertools import islice, product
from random import Random
from time import monotonic

from .engine import (_deterministic_from_totals, _donor_totals, _multisets,
                     _prediction_from_totals)
from .journey import _fingerprint, _integer, _rows
from .models import InventoryItem, Recipe, Target
from .parallel_models import (ParallelActual, ParallelCompletion, ParallelReport,
                              ParallelState, ParallelStep, ParallelUndo,
                              ParallelWave, PendingWave, WaveRecord, _apply,
                              cancel_wave, complete_wave, current_wave,
                              prepare_wave, undo_wave)
from .routes import _bounded_count, _choose_pool, _sample_counts, _score, plan_route
from .stock_planning import stock_route_cost


@dataclass(frozen=True)
class _Node:
    id: str
    genes: str
    recipe: Recipe
    level: int
    ancestors: frozenset[str]


def _uses(recipe):
    return Counter((recipe.center_id, *recipe.donor_ids))


def _graph_needs(recipe, catalog):
    """Union shared ancestors once, then pay all consumers of their clones."""
    dependencies = set()
    for seed_id in _uses(recipe):
        if seed_id in catalog:
            dependencies.add(seed_id)
            dependencies.update(catalog[seed_id].ancestors)
    needed = _uses(recipe)
    for seed_id in dependencies:
        needed.update(_uses(catalog[seed_id].recipe))
    return frozenset(dependencies), needed


def _schedule(final, catalog, box_count, main_box, clone_yield, max_waves):
    nodes = [catalog[x] for x in final.ancestors] + [final]
    nodes.sort(key=lambda x: (x.level, x.id))
    assigned = {}
    batches = defaultdict(list)
    main_line = {final.id}
    center = final.recipe.center_id
    while center in catalog:
        main_line.add(center)
        center = catalog[center].recipe.center_id
    # Earliest legal batch. The main recipient chain gets the named main box;
    # side branches use any remaining box in that same batch.
    for node in sorted(nodes, key=lambda x: (x.level, x.id not in main_line, x.id)):
        parents = [seed_id for seed_id in _uses(node.recipe) if seed_id in catalog]
        earliest = 1 + max((assigned[x] for x in parents), default=0)
        batch = earliest
        while len(batches[batch]) >= box_count:
            batch += 1
        if batch > max_waves:
            return None
        assigned[node.id] = batch
        batches[batch].append(node)
    waves = []
    for number in range(1, max(batches) + 1):
        group = batches[number]
        # Scheduling never creates empty interior batches.
        if not group:
            return None
        available = [main_box] + [x for x in range(1, box_count + 1) if x != main_box]
        group = sorted(group, key=lambda x: (x.id not in main_line, x.id))
        steps = tuple(ParallelStep(box, node.recipe, node.genes, node.id, clone_yield)
                      for box, node in zip(available, group))
        waves.append(ParallelWave(number, steps))
    return tuple(waves)


def _sequential_waves(steps, inventory, main_box):
    """Rebind a sequential fallback to this stock snapshot's output namespace.

    A new route search restarts its relative output numbers at one. Reusing
    those short IDs after their clones were consumed would collide with this
    parallel journey's completed history on a later batch. Every downstream
    ingredient reference must be renamed together with its producer.
    """
    prefix = 'parallel-' + _fingerprint(inventory)[:12] + '-route-'
    occupied = {row.id for row in inventory}
    references = {}
    waves = []
    for number, step in enumerate(steps, 1):
        output_id = f'{prefix}{number}'
        while output_id in occupied:
            output_id += '_'
        center = replace(step.recipe.center,
                         id=references.get(step.recipe.center_id, step.recipe.center_id))
        donors = tuple(replace(row, id=references.get(row.id, row.id))
                       for row in step.recipe.donors)
        recipe = replace(step.recipe, center=center, donors=donors)
        waves.append(ParallelWave(number, (ParallelStep(main_box, recipe,
                         step.expected_genes, output_id, step.clone_yield),)))
        references[step.output_id] = output_id
        occupied.add(output_id)
    return tuple(waves)


def validate_parallel_report(report, inventory, target):
    """Replay a complete recommendation without changing the caller's stock."""
    rows = _rows(inventory)
    occupied = {x.id for x in rows}
    if not report.found or not report.waves:
        if report.reason == 'already_target' and any(target.matches(x.genes) for x in rows):
            return rows
        raise ValueError('没有完整的并行方案可以开始。')
    for number, wave in enumerate(report.waves, 1):
        if wave.number != number or any(x.box_id > report.box_count for x in wave.steps):
            raise ValueError('并行方案批次或箱号不正确。')
        if any(x.recipe.target != target for x in wave.steps):
            raise ValueError('并行方案目标不一致。')
        if any(x.output_id in occupied for x in wave.steps):
            raise ValueError('并行方案重复使用了已有的产物编号。')
        actuals = tuple(ParallelActual(x.box_id, x.expected_genes, x.clone_yield) for x in wave.steps)
        rows = _apply(rows, WaveRecord(number, f'validate-{number}', wave, actuals))
        occupied.update(x.output_id for x in wave.steps)
    if not any(target.matches(x.genes) for x in rows):
        raise ValueError('并行方案最终没有得到目标基因。')
    return rows


def plan_parallel(inventory, target, *, box_count=3, main_box=1, max_waves=3,
                  clone_yield=1, max_donors=8, max_combinations=120_000,
                  max_seconds=12.0, catalog_limit=120, donor_pool_limit=16,
                  objective='rounds', cancel=None, progress=None):
    """Find a verified full DAG; bounded catalogs are not optimality proofs.

    Independent intermediate outputs may be merged as a later recipient and
    donors, or several outputs may be donors in the final main box. Uncertain
    predictions never become future inventory. The public report is replayed
    against actual shared stock before returning a successful recommendation.
    """
    rows = _rows(inventory)
    if not isinstance(target, Target):
        raise ValueError('多线培育目标无效。')
    _integer(box_count, '种植箱数量', 1, 6)
    _integer(main_box, '主箱编号', 1, box_count)
    _integer(max_waves, '预计批次上限', 1, 6)
    _integer(clone_yield, '预计克隆数', 1, 1_000_000_000)
    _integer(max_donors, '每箱供体上限', 0, 8)
    _integer(max_combinations, '组合上限', 1, 10_000_000)
    _integer(catalog_limit, '中间样本上限', 8, 1000)
    _integer(donor_pool_limit, '供体样本上限', 2, 100)
    if objective not in ('rounds', 'plants', 'stock'):
        raise ValueError('优化方式必须为 rounds、plants 或 stock。')
    if isinstance(max_seconds, bool) or not isinstance(max_seconds, (float, int)) or not 0 < max_seconds <= 120:
        raise ValueError('计算时间必须在 0–120 秒内。')
    started = monotonic()
    deadline = started + max_seconds
    evaluated = 0
    reason = 'not_found'
    limits = set()
    catalog = {}
    base = {x.id: x for x in rows}
    prefix = 'parallel-' + _fingerprint(rows)[:12] + '-'
    serial = 0
    def scarcity(waves):
        return stock_route_cost((step for wave in waves for step in wave.steps), rows)

    def route_rank(waves):
        produced = {x.output_id for wave in waves for x in wave.steps}
        final = waves[-1].steps[0].recipe
        # Equal duration and cost: prefer a stable final recipient fed by
        # prepared side-branch donors, which makes the merge easy to follow.
        rounds, plants = len(waves), sum(x.plant_count for x in waves)
        tie = -sum(seed_id in produced for seed_id in final.donor_ids)
        if objective == 'stock':
            return scarcity(waves), rounds, plants, tie
        if objective == 'plants':
            return plants, rounds, tie
        return rounds, plants, tie

    def finish(waves=(), found=False, complete=False, why=None):
        report = ParallelReport(tuple(waves), found, complete, evaluated, why or reason,
                                box_count, main_box, max_waves, clone_yield,
                                monotonic() - started, tuple(sorted(limits)),
                                objective=objective, stock_cost=float(scarcity(waves)))
        if found and waves:
            validate_parallel_report(report, rows, target)
        return report

    def stopped():
        nonlocal reason
        if cancel and cancel():
            reason = 'cancelled'
            return True
        if evaluated >= max_combinations:
            limits.add('combinations')
            reason = 'limit'
            return True
        if monotonic() >= deadline:
            limits.add('time')
            reason = 'limit'
            return True
        return False

    if stopped():
        return finish()
    if not rows:
        return finish(complete=True, why='empty_inventory')
    if any(target.matches(x.genes) for x in rows):
        return finish(found=True, complete=True, why='already_target')
    if max_donors == 0 and clone_yield == 1:
        return finish(complete=True)

    # Existing sequential search is a useful complete-route fallback, and its
    # full-library target prepass often finds a one-batch solution quickly.
    # It owns a small fixed slice, leaving most work for true branch merges.
    fallback = None
    initial_budget = min(max_combinations, max(1, max_combinations // 6))
    sequential = plan_route(rows, target, max_rounds=max_waves, clone_yield=clone_yield,
                            max_donors=max_donors, max_combinations=initial_budget,
                            max_seconds=max(.001, min(max_seconds / 5, deadline - monotonic())),
                            objective=objective, cancel=cancel)
    evaluated += sequential.evaluated
    if sequential.cancelled:
        return finish(why='cancelled')
    if sequential.found:
        fallback = _sequential_waves(sequential.steps, rows, main_box)
        if len(fallback) == 1 and objective == 'rounds':
            return finish(fallback, True, why='found')

    # A catalog is intentionally bounded. Even exhausted candidate generation
    # does not prove that no other intermediate genotype or allocation works.
    limits.add('intermediate_catalog')
    best = fallback
    for level in range(1, max_waves + 1):
        if stopped():
            break
        if best is not None and objective == 'rounds' and level >= len(best) + 1:
            break
        # Freeze this depth: newly discovered outputs can only be used at the
        # next depth. This is also the no-same-batch borrowing rule in search.
        previous = dict(catalog)
        virtual_rows = [*rows, *(InventoryItem(x.id, x.genes, clone_yield,
                                               f'中间样本 {index + 1}')
                                for index, x in enumerate(previous.values()))]
        # Keep alternatives for identical genes: one can save a scarce original
        # seed even when another has a slightly lower total plant count.
        groups = defaultdict(list)
        for row in virtual_rows:
            groups[row.genes].append(row)
        genes = tuple(sorted(groups, key=lambda x: (-_score(x, target), x.count('W') + x.count('X'), x)))
        grouped = tuple(tuple(sorted(groups[g], key=lambda x: (x.id not in base, x.id))) for g in genes)
        capacities = tuple(sum(x.quantity for x in group) for group in grouped)
        centers = [x for group in grouped for x in group[:2]]
        layer_budget = max(1, (max_combinations - evaluated) // (max_waves - level + 1))
        layer_deadline = monotonic() + max(0, deadline - monotonic()) / (max_waves - level + 1)
        layer_started = evaluated
        discovered = []
        seen = {(node.genes, tuple(sorted(_graph_needs(node.recipe, previous)[1].items())))
                for node in previous.values()}
        # Rotate centers by depth so a partial time budget does not repeatedly
        # privilege only the alphabetically first original seed.
        for center_index, center in enumerate(centers):
            if stopped() or monotonic() >= layer_deadline or evaluated - layer_started >= layer_budget:
                break
            remaining_centers = len(centers) - center_index
            center_budget = max(1, (layer_budget - (evaluated - layer_started)) // remaining_centers)
            center_stop = evaluated + center_budget
            pool = _choose_pool(genes, center.genes, target, min(donor_pool_limit, len(genes)))
            if len(pool) < len(genes):
                limits.add('donor_pool')
            selected_genes = tuple(genes[x] for x in pool)
            selected_caps = tuple(capacities[x] - int(genes[x] == center.genes) for x in pool)
            effective_max = min(max_donors, sum(selected_caps))
            counts = list(range(1, effective_max + 1))
            if clone_yield > 1:
                counts.insert(0, 0)
            rng = Random(f'{level}:{center.id}')
            for count_index, donor_count in enumerate(counts):
                if stopped() or evaluated >= center_stop or monotonic() >= layer_deadline:
                    break
                quota = max(1, (center_stop - evaluated) // (len(counts) - count_index))
                total = _bounded_count(selected_caps, donor_count, quota + 1)
                if total > quota:
                    limits.add('sampling')
                    candidates = _sample_counts(selected_caps, donor_count, quota, rng)
                else:
                    candidates = _multisets(selected_caps, donor_count)
                for amounts in candidates:
                    if stopped() or evaluated >= center_stop or monotonic() >= layer_deadline:
                        break
                    donor_genes = tuple(g for g, n in zip(selected_genes, amounts) for _ in range(n))
                    totals = _donor_totals(donor_genes)
                    outcome = _deterministic_from_totals(center.genes, totals)
                    evaluated += 1
                    if progress and evaluated % 250 == 0:
                        progress(evaluated, level)
                    if outcome is None or (outcome == center.genes and (donor_count or clone_yield == 1)):
                        continue
                    # Different provenance can matter. Try both base-first and
                    # produced-first allocations, rather than conflating an
                    # unlimited virtual genotype with actual clone stock.
                    group_options = []
                    for group_index, amount in zip(pool, amounts):
                        if not amount:
                            continue
                        available_rows = grouped[group_index]
                        group_capacities = tuple(x.quantity - int(x.id == center.id) for x in available_rows)
                        options = []
                        for split in islice(_multisets(group_capacities, amount), 8):
                            options.append(tuple(row for row, n in zip(available_rows, split) for _ in range(n)))
                        if _bounded_count(group_capacities, amount, 9) > 8:
                            limits.add('allocation_variants')
                        group_options.append(options)
                    variant_count = 1
                    for options in group_options:
                        variant_count *= len(options)
                    if variant_count > 32:
                        limits.add('allocation_variants')
                    for pieces in islice(product(*group_options), 32):
                        donors = tuple(row for piece in pieces for row in piece)
                        if len(donors) == donor_count:
                            prediction = _prediction_from_totals(center.genes, donor_genes, totals)
                            recipe = Recipe(center, donors, prediction, target)
                            ancestors, needed = _graph_needs(recipe, previous)
                            if len(ancestors) + 1 > box_count * max_waves:
                                continue
                            if any(amount > (base[seed_id].quantity if seed_id in base else clone_yield)
                                   for seed_id, amount in needed.items()):
                                continue
                            signature = (outcome, tuple(sorted(needed.items())))
                            if signature in seen:
                                continue
                            seen.add(signature)
                            serial += 1
                            output_id = f'{prefix}{serial}'
                            while output_id in base or output_id in catalog:
                                serial += 1
                                output_id = f'{prefix}{serial}'
                            node_level = 1 + max((previous[x].level for x in ancestors), default=0)
                            node = _Node(output_id, outcome, recipe, node_level, ancestors)
                            if target.matches(outcome):
                                waves = _schedule(node, previous, box_count, main_box, clone_yield, max_waves)
                                if waves is not None:
                                    rank = route_rank(waves)
                                    if best is None or rank < route_rank(best):
                                        best = waves
                                        # One batch is the minimum possible;
                                        # other batch lengths stay bounded.
                                        if len(waves) == 1 and objective == 'rounds':
                                            return finish(best, True, why='found')
                            elif level < max_waves:
                                discovered.append((node, needed))
        # Use coverage across positions to keep complementary branches, rather
        # than retaining only the individually closest-looking clone.
        def rank(entry):
            node, needed = entry
            return (-_score(node.genes, target), node.level,
                    sum(needed.values()), node.genes, node.id)
        discovered.sort(key=rank)
        retained = []
        chosen_ids = set()
        per_genes = Counter()

        def retain(entry):
            node, _ = entry
            if node.id not in chosen_ids and per_genes[node.genes] < 2 and len(retained) < catalog_limit:
                retained.append(node)
                chosen_ids.add(node.id)
                per_genes[node.genes] += 1

        for position in range(6):
            for letter in set(target.genes):
                for entry in [x for x in discovered if x[0].genes[position] == letter][:3]:
                    retain(entry)
        for entry in discovered:
            retain(entry)
        # Existing catalog ancestors remain available: removing them could
        # invalidate an otherwise retained descendant's provenance.
        catalog.update((x.id, x) for x in retained)
        if len(discovered) > len(retained):
            limits.add('catalog_pruning')
        if not retained:
            break
        if progress:
            progress(evaluated, level)
    if reason == 'cancelled':
        return finish()
    if best is not None:
        return finish(best, True, why='found')
    return finish()
