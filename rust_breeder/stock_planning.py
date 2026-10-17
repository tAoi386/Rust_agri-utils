"""Compare verified routes by the share of original stock they consume.

This is a bounded comparison of existing search strategies. It deliberately
does not claim to optimize every possible route under a new global objective.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from fractions import Fraction
from time import monotonic

from .engine import predict
from .models import InventoryItem, Recipe


def stock_route_cost(steps_or_recipes, inventory) -> Fraction:
    """Return original gene-stock fractions spent; generated clones cost zero.

    ``steps_or_recipes`` accepts recipes or objects with a ``recipe`` field,
    including flattened parallel waves. Distinct original rows with the same
    genotype share a denominator, but each row's physical quantity is checked.
    """
    rows = tuple(inventory)
    original = {row.id: row for row in rows}
    if len(original) != len(rows):
        raise ValueError("每条库存的编号必须唯一。")
    totals = Counter()
    for row in rows:
        totals[row.genes] += row.quantity
    used_ids = Counter()
    used_genes = Counter()
    for entry in steps_or_recipes:
        recipe = entry if isinstance(entry, Recipe) else entry.recipe
        for item in (recipe.center, *recipe.donors):
            row = original.get(item.id)
            if row is None:
                continue
            if item.genes != row.genes:
                raise ValueError("路线中的原始种苗基因与库存不一致。")
            used_ids[item.id] += 1
            if used_ids[item.id] > row.quantity:
                raise ValueError("路线重复消耗了不足的原始种苗。")
            used_genes[row.genes] += 1
    return sum((Fraction(amount, totals[genes]) for genes, amount in used_genes.items()), Fraction())


def _verified(report, inventory, target, *, center_id, max_rounds, clone_yield, max_donors):
    """Replay the entire proposed sequence before it can become a candidate."""
    if not report.found or report.cancelled:
        return False
    stock = {row.id: row for row in inventory}
    if not report.steps:
        recipe = report.best
        return (report.reason == "already_target" and recipe is not None
                and recipe.center_id in stock and not recipe.donors
                and (center_id is None or recipe.center_id == center_id)
                and recipe.center.genes == stock[recipe.center_id].genes
                and target.matches(recipe.center.genes))
    if len(report.steps) > max_rounds:
        return False
    occupied = set(stock)
    previous_output = None
    for step in report.steps:
        recipe = step.recipe
        if ((previous_output is not None and recipe.center_id != previous_output)
                or (previous_output is None and center_id is not None and recipe.center_id != center_id)
                or recipe.target != target or len(recipe.donors) > max_donors
                or step.clone_yield != clone_yield or step.output_id in occupied):
            return False
        prediction = predict(recipe.center.genes, [row.genes for row in recipe.donors])
        if (prediction != recipe.prediction or prediction.deterministic_result is None
                or prediction.deterministic_result != step.expected_genes):
            return False
        spent = Counter()
        for item in (recipe.center, *recipe.donors):
            row = stock.get(item.id)
            if row is None or row.genes != item.genes:
                return False
            spent[item.id] += 1
            if spent[item.id] > row.quantity:
                return False
        for key, amount in spent.items():
            row = stock[key]
            if row.quantity == amount:
                del stock[key]
            else:
                stock[key] = replace(row, quantity=row.quantity - amount)
        stock[step.output_id] = InventoryItem(step.output_id, step.expected_genes, step.clone_yield)
        occupied.add(step.output_id)
        previous_output = step.output_id
    return target.matches(report.steps[-1].expected_genes)


def plan_stock_route(inventory, target, *, center_id=None, max_rounds=3, clone_yield=1,
                     max_donors=8, max_combinations=60_000, beam_width=24,
                     donor_pool_limit=12, max_seconds=8.0, cancel=None, progress=None,
                     _planner=None):
    """Compare fewest-plants/rounds candidates inside one shared total budget."""
    from .routes import RouteReport, plan_route

    if type(max_combinations) is not int or max_combinations < 1 or max_seconds <= 0:
        raise ValueError("组合数量和时间上限必须大于 0。")
    rows = tuple(inventory)
    planner = _planner or plan_route
    started = monotonic()
    evaluated = 0
    limits = {"stock_comparison"}
    reports = []
    candidates = []
    options = dict(center_id=center_id, max_rounds=max_rounds, clone_yield=clone_yield,
                   max_donors=max_donors, beam_width=beam_width, donor_pool_limit=donor_pool_limit)
    cancelled = False
    for index, objective in enumerate(("plants", "rounds")):
        if cancel and cancel():
            cancelled = True
            break
        remaining_time = max_seconds - (monotonic() - started)
        remaining_combinations = max_combinations - evaluated
        if remaining_time <= 0 or remaining_combinations <= 0:
            limits.add("time" if remaining_time <= 0 else "combinations")
            break
        remaining_passes = 2 - index
        budget = max(1, remaining_combinations // remaining_passes)
        offset = evaluated

        def advanced(count, depth, offset=offset):
            if progress:
                progress(offset + count, depth)

        report = planner(rows, target, objective=objective,
                         max_combinations=budget, max_seconds=remaining_time / remaining_passes,
                         cancel=cancel, progress=advanced, **options)
        evaluated += report.evaluated
        limits.update(report.limits)
        reports.append(report)
        if report.cancelled or (cancel and cancel()):
            cancelled = True
            break
        if _verified(report, rows, target, center_id=center_id, max_rounds=max_rounds,
                     clone_yield=clone_yield, max_donors=max_donors):
            candidates.append(report)
            if report.reason == "already_target":
                break
        elif report.found:
            limits.add("invalid_candidate")
        if progress:
            progress(evaluated, report.rounds or 1)
    if cancelled:
        selected = RouteReport((), (), False, False, evaluated, "cancelled")
    elif candidates:
        selected = min(candidates, key=lambda report: (
            stock_route_cost(report.steps, rows), report.rounds, report.plant_count))
    elif reports:
        selected = replace(reports[-1], steps=(), found=False)
    else:
        selected = RouteReport((), (), False, False, evaluated, "limit")
    return replace(selected, evaluated=evaluated, objective="stock", complete=False,
                   minimum_proven=False, shortest_rounds_proven=False,
                   requested_max_rounds=max_rounds, clone_yield=clone_yield,
                   center_mode="fixed" if center_id is not None else "auto",
                   elapsed_seconds=monotonic() - started, limits=tuple(sorted(limits)))
